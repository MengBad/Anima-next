"""共享媒体工具：图片下载 + data URI 内联 + 多模态消息构造。

用途：全量多模态视觉理解（让 bot 真·看见图片）、表情看图打标。
避免在 handler / stages / vision / stickers 各处重复下载逻辑。

隐私与成本：图片仅下载到本机内存，内联为 base64 data URI 交给 LLM 网关
（gemini-antigravity 已验证透传 image_url）。超过 max_inline_bytes 的图
不当作视觉输入，避免爆 token / 超时。图片本身不落盘（sticker 收集是另一回事，
那是用户明确要的素材库）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("anima.media")

# 大图转码结果缓存（进程内 LRU）：同内容哈希不重复 Pillow 压缩
_PREPARE_CACHE_MAX = 64
_prepare_cache: "OrderedDict[str, list[PreparedImage]]" = OrderedDict()
_prepare_cache_hits = 0
_prepare_cache_misses = 0
# P2-G：prepare_images_for_llm 会被工作线程并发调用，LRU 读写/淘汰/计数器须同锁，
# 否则 check→insert→popitem 与计数更新存在数据竞争（超上限、计数丢减）。
_CACHE_LOCK = threading.Lock()

# 默认内联上限；vision 配置可覆盖。2026-07 再压一档，降低 27MB 原图触发 413 后丢视觉的概率。
DEFAULT_MAX_INLINE_BYTES = 2 * 1024 * 1024

# 图片源文件与发往 LLM 的 data URI 不是同一个边界：QQ 原图可以很大，
# 但网关请求体通常有 10MB 左右上限。先受限地取回原图，再本地缩放/转码，
# 可以保留视觉理解而不是把整张图直接丢弃。
DEFAULT_MAX_SOURCE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_LLM_IMAGE_BYTES = 1024 * 1024
DEFAULT_MAX_LLM_REQUEST_IMAGE_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_IMAGE_DIMENSION = 1280
DEFAULT_MAX_ANIMATION_FRAMES = 3

_EXT_BY_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF89a", ".gif"),
    (b"GIF87a", ".gif"),
]

# 模块级共享 ClientSession，避免每次下载图片都新建连接（高频图片场景连接风暴）
_shared_session: Optional["aiohttp.ClientSession"] = None


async def _get_session():
    """获取或创建模块级共享 aiohttp ClientSession。"""
    global _shared_session
    import aiohttp
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, sock_connect=8),
            connector=aiohttp.TCPConnector(limit=8, force_close=True, enable_cleanup_closed=True),
        )
    return _shared_session


async def close_media_session() -> None:
    """关闭模块级共享 ClientSession，避免进程退出时 aiohttp 报
    'Unclosed client session'。由 AnimaCore.stop() 在优雅停机时调用。"""
    global _shared_session
    session = _shared_session
    _shared_session = None
    if session is None:
        return
    try:
        if not session.closed:
            await session.close()
    except Exception:
        logger.debug("media session 关闭异常（可忽略）", exc_info=True)


@dataclass(frozen=True)
class PreparedImage:
    """已经缩放并可安全内联给 LLM 的单帧图片。"""

    data: bytes
    ext: str
    original_bytes: int
    frame_index: int = 0
    frame_count: int = 1


def guess_ext(data: bytes, img: dict) -> str:
    """按 magic number 推断图片后缀，回退到 url/file 后缀。"""
    for magic, ext in _EXT_BY_MAGIC:
        if data[: len(magic)] == magic:
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for src in (img.get("url", ""), img.get("file", "")):
        if "." in src:
            e = Path(src).suffix.lower()
            if e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                return ".jpg" if e == ".jpeg" else e
    return ".jpg"


async def fetch_image_bytes(img: dict, max_bytes: int = DEFAULT_MAX_INLINE_BYTES,
                            resolver=None) -> Optional[bytes]:
    """下载/复制一张图，返回字节；失败或超限返回 None（绝不抛）。

    resolver: 可选 async callable(img)->bytes，url/本地 file 都失败时的兜底
              （通常走 NapCat get_image API 解析裸 GUID）。
    """
    url = (img.get("url") or "").strip()
    fpath = (img.get("file") or "").strip()
    # 优先 url 下载（NapCat 可能给 QQ CDN 或本机 url）
    if url:
        try:
            session = await _get_session()
            async with session.get(url) as r:
                if r.status == 200:
                    data = await _read_limited(r, max_bytes)
                    if data:
                        return data
        except Exception:
            logger.debug("视觉图片 url 下载失败: %s", url[:60])
    # fallback：本地 file 路径复制（同机 NapCat 缓存）
    if fpath and Path(fpath).exists():
        try:
            if max_bytes > 0 and Path(fpath).stat().st_size > max_bytes:
                logger.debug("视觉图片本地文件过大跳过: %s", fpath[:80])
                return None
            data = Path(fpath).read_bytes()
            if data and len(data) <= max_bytes:
                return data
        except Exception:
            logger.exception("视觉图片 file 复制失败")
    # 兜底：resolver（get_image API）
    if resolver is not None:
        try:
            try:
                data = await resolver(img, max_bytes=max_bytes)
            except TypeError:
                # 兼容旧的单参数 resolver 测试桩/第三方渠道适配器。
                data = await resolver(img)
            if data and len(data) <= max_bytes:
                return data
        except Exception:
            logger.exception("视觉图片 resolver 兜底失败")
    return None


async def _read_limited(response, max_bytes: int) -> Optional[bytes]:
    """流式读取 HTTP 图片，不能先把异常大文件完整读进内存。"""
    if max_bytes > 0 and response.content_length and response.content_length > max_bytes:
        logger.debug("视觉图片 Content-Length 超限跳过: %d bytes", response.content_length)
        return None
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await response.content.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if max_bytes > 0 and total > max_bytes:
            logger.debug("视觉图片流式读取超限跳过: %d bytes", total)
            return None
        chunks.append(chunk)
    return b"".join(chunks) if total else None


def to_data_uri(data: bytes, img: dict) -> str:
    """把图片字节编码为 data:image/<ext>;base64,<...> 内联 URI。"""
    ext = guess_ext(data, img)[1:]  # 去点
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


def _animation_indices(frame_count: int, max_frames: int) -> list[int]:
    """均匀保留动图的首/中/尾关键帧，避免只看 GIF 第一帧。"""
    if frame_count <= 1 or max_frames <= 1:
        return [0]
    count = min(frame_count, max_frames)
    if count == frame_count:
        return list(range(frame_count))
    return sorted({round(i * (frame_count - 1) / (count - 1)) for i in range(count)})


def _encode_for_llm(image, max_bytes: int, max_dimension: int) -> bytes | None:
    """转为大小受控的 WebP；逐步降低质量/尺寸而非牺牲整张图。"""
    try:
        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(image)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "transparency" in image.info else "RGB")
        # 首轮缩至可读、适合视觉模型的边长；后续若仍大再等比缩小。
        if max(image.size, default=1) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        for scale in (1.0, 0.82, 0.66, 0.50):
            candidate = image
            if scale != 1.0:
                size = (
                    max(1, int(image.width * scale)),
                    max(1, int(image.height * scale)),
                )
                candidate = image.resize(size, Image.Resampling.LANCZOS)
            for quality in (80, 70, 58, 46, 36, 28):
                buf = io.BytesIO()
                candidate.save(buf, format="WEBP", quality=quality, method=4)
                data = buf.getvalue()
                if len(data) <= max_bytes:
                    return data
    except Exception:
        logger.debug("图片转码失败，无法缩放后内联", exc_info=True)
    return None


def _prepare_cache_key(
    data: bytes, max_bytes: int, max_dimension: int, max_frames: int,
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{digest}:{int(max_bytes)}:{int(max_dimension)}:{int(max_frames)}"


def prepare_cache_stats() -> dict:
    """进程内转码缓存命中统计（测试 / ops-quality）。"""
    with _CACHE_LOCK:
        return {
            "size": len(_prepare_cache),
            "max": _PREPARE_CACHE_MAX,
            "hits": _prepare_cache_hits,
            "misses": _prepare_cache_misses,
        }


def clear_prepare_cache() -> None:
    """测试钩子：清空转码 LRU。"""
    global _prepare_cache_hits, _prepare_cache_misses
    with _CACHE_LOCK:
        _prepare_cache.clear()
        _prepare_cache_hits = 0
        _prepare_cache_misses = 0


def prepare_images_for_llm(
    data: bytes,
    img: dict,
    *,
    max_bytes: int = DEFAULT_MAX_LLM_IMAGE_BYTES,
    max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
    max_frames: int = DEFAULT_MAX_ANIMATION_FRAMES,
    use_cache: bool = True,
) -> list[PreparedImage]:
    """将静态图或动图转成满足网关载荷限制的视觉输入。

    小图保留原始字节以避免不必要的失真；大图则在本地转为 WebP。动图选取
    关键帧，每帧平分预算。任何解码失败都不会抛给 QQ 主流水线。
    同内容哈希 + 同预算参数命中进程内 LRU，避免重复转码烧 CPU。
    """
    global _prepare_cache_hits, _prepare_cache_misses
    if not data or max_bytes <= 0:
        return []
    key = _prepare_cache_key(data, max_bytes, max_dimension, max_frames)
    if use_cache:
        with _CACHE_LOCK:
            cached = _prepare_cache.get(key)
        if cached is not None:
            with _CACHE_LOCK:
                _prepare_cache.move_to_end(key)
                _prepare_cache_hits += 1
            return list(cached)

    result = _prepare_images_for_llm_uncached(
        data, img, max_bytes=max_bytes, max_dimension=max_dimension, max_frames=max_frames,
    )
    if use_cache:
        with _CACHE_LOCK:
            if result:
                if key not in _prepare_cache:  # P2-G：double-check，不覆盖并发刚算好的结果
                    _prepare_cache[key] = list(result)
                _prepare_cache.move_to_end(key)
                while len(_prepare_cache) > _PREPARE_CACHE_MAX:
                    _prepare_cache.popitem(last=False)
            _prepare_cache_misses += 1
    return result


def _prepare_images_for_llm_uncached(
    data: bytes,
    img: dict,
    *,
    max_bytes: int,
    max_dimension: int,
    max_frames: int,
) -> list[PreparedImage]:
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as source:
            frame_count = int(getattr(source, "n_frames", 1) or 1)
            animated = bool(getattr(source, "is_animated", False) and frame_count > 1)
            # 静态且本身已在预算内时保留原样，文本梗图不会因为转码变糊。
            if (not animated and len(data) <= max_bytes
                    and max(source.size, default=1) <= max_dimension):
                return [PreparedImage(data, guess_ext(data, img), len(data))]

            indices = _animation_indices(frame_count, max_frames) if animated else [0]
            per_frame_budget = max(64 * 1024, max_bytes // max(1, len(indices)))
            result: list[PreparedImage] = []
            for index in indices:
                source.seek(index)
                frame = ImageOps.exif_transpose(source.copy())
                encoded = _encode_for_llm(frame, per_frame_budget, max_dimension)
                if encoded:
                    result.append(PreparedImage(
                        encoded, ".webp", len(data), index, frame_count,
                    ))
            return result
    except Exception:
        # 不能由 Pillow 解析的图，只允许在原始体积已经安全时走原始 data URI。
        if len(data) <= max_bytes:
            return [PreparedImage(data, guess_ext(data, img), len(data))]
        logger.info("视觉图片无法解码且超过内联预算，跳过 LLM 图像输入: %d bytes", len(data))
        return []


async def fetch_prepared_images(
    img: dict,
    *,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_bytes: int = DEFAULT_MAX_LLM_IMAGE_BYTES,
    max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
    max_frames: int = DEFAULT_MAX_ANIMATION_FRAMES,
    resolver=None,
) -> list[PreparedImage]:
    """获取 QQ 图并在线程池完成 CPU 转码，避免大图卡住事件循环。"""
    source = await fetch_image_bytes(img, max_source_bytes, resolver=resolver)
    if not source:
        return []
    return await asyncio.to_thread(
        prepare_images_for_llm,
        source,
        img,
        max_bytes=max_bytes,
        max_dimension=max_dimension,
        max_frames=max_frames,
    )


def build_multimodal_user_content(text: str, data_uris: list[str]) -> list[dict]:
    """构造 OpenAI 视觉格式的多模态 user content（文本 + 若干图）。"""
    parts: list[dict] = [{"type": "text", "text": text or ""}]
    for u in data_uris:
        if u:
            parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts


def data_uri_wire_bytes(uri: str) -> int:
    """估算 data URI 进入 JSON 请求后的近似字节数（含 base64 膨胀）。"""
    text = str(uri or "")
    if not text:
        return 0
    if ";base64," in text:
        header, _, payload = text.partition(";base64,")
        return len(header) + 8 + len(payload.encode("ascii", "ignore"))
    return len(text.encode("utf-8", "ignore"))


def prepared_frame_wire_budget(raw_bytes: int) -> int:
    """把解码后的原始帧预算换算为 data URI 线缆预算。"""
    return max(0, int(raw_bytes) * 4 // 3 + 96)


def reencode_data_uri(
    uri: str,
    *,
    max_bytes: int,
    max_dimension: int = DEFAULT_MAX_IMAGE_DIMENSION,
) -> str | None:
    """将 data URI 再压缩为更小的 WebP；失败时返回 None。"""
    text = str(uri or "")
    marker = ";base64,"
    if marker not in text or max_bytes <= 0:
        return None
    _header, _, payload = text.partition(marker)
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    if not raw:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as source:
            encoded = _encode_for_llm(source.copy(), max_bytes, max_dimension)
    except Exception:
        encoded = None
    if not encoded:
        return None
    b64 = base64.b64encode(encoded).decode("ascii")
    return f"data:image/webp;base64,{b64}"


def shrink_message_images(
    messages: list[dict],
    *,
    max_bytes_per_image: int = 512 * 1024,
    max_dimension: int = 1024,
) -> tuple[list[dict], bool]:
    """对 messages 内所有 data URI 图片做一轮更激进的转码，尽量保留视觉。

    返回 (新 messages, 是否发生改动)。解码失败的图片原样保留，由上层决定是否剥离。
    """
    changed = False
    out: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        new_parts: list[dict] = []
        for part in content:
            if not (isinstance(part, dict) and part.get("type") == "image_url"):
                new_parts.append(part)
                continue
            image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            uri = str(image.get("url") or "")
            if not uri.startswith("data:"):
                new_parts.append(part)
                continue
            smaller = reencode_data_uri(
                uri, max_bytes=max_bytes_per_image, max_dimension=max_dimension,
            )
            if smaller and smaller != uri and data_uri_wire_bytes(smaller) < data_uri_wire_bytes(uri):
                new_part = dict(part)
                new_part["image_url"] = {**image, "url": smaller}
                new_parts.append(new_part)
                changed = True
            else:
                new_parts.append(part)
        new_message = dict(message)
        new_message["content"] = new_parts
        out.append(new_message)
    return out, changed

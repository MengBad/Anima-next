"""产物真实校验：用磁盘/网络上的事实判断，不让 LLM 读文本猜。

为什么需要（2026-11-04 生产事故 01f70c6a75 / e7d6a67518）：
    executor._verify_step 把 step.result（一段字符串）丢给 LLM 判 PASS/FAIL。
    华为云任务第 2 步的真实产物是**一张登录页截图**，路径在 task.context 里，
    而 step.result 可能只有「已截图：/path」，甚至被脱敏成一句占位符 ——
    验收器看不见图，只能判 FAIL，任务就此卡死。

这里只做纯函数、只读事实：
    · 文件存在吗？多大？是不是真的 PNG？分辨率多少？
    · HTTP 状态码多少？正文里有没有该有的关键词？
不做任何 LLM 调用，可完整单测。
"""

from __future__ import annotations

import pathlib
from typing import Iterable, Optional, Tuple

#: 截图最小体积。实测华为云登录页 1280x900 全页截图约 50-90KB；
#: 空白页约 5KB；1x1 占位图 68 字节。低于这个数一律视为没截到东西。
MIN_SCREENSHOT_BYTES = 8192
#: 截图最小边长（像素）。太小的图说明视口没渲染出来。
MIN_SCREENSHOT_EDGE = 200
#: 一般产物文件的最小体积（空文件/只有换行都算没产出）。
MIN_FILE_BYTES = 64

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def image_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从文件头读出 (宽, 高)，不依赖 PIL。认不出来返回 None。"""
    try:
        if data[:8] == _PNG_MAGIC and len(data) >= 24 and data[12:16] == b"IHDR":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:3] == _JPEG_MAGIC:
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                # SOF0..SOF15（排除 DHT/JPG/DAC 那几个非 SOF 编号）
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h)
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = int.from_bytes(data[i + 2:i + 4], "big")
                i += 2 + seg
    except Exception:
        return None
    return None


def verify_screenshot(path: str, *,
                      min_bytes: int = MIN_SCREENSHOT_BYTES,
                      min_edge: int = MIN_SCREENSHOT_EDGE) -> Tuple[bool, str]:
    """截图是否**真的**截到了东西。返回 (通过, 人话原因)。"""
    p = pathlib.Path(str(path or ""))
    if not p.is_absolute():
        return False, f"截图路径不是绝对路径：{p}"
    if not p.is_file():
        return False, f"截图文件不存在：{p}"
    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"读不到截图大小：{type(exc).__name__}"
    if size < min_bytes:
        return False, f"截图只有 {size} 字节（< {min_bytes}），多半是空白页"
    try:
        head = p.read_bytes()[:65536]
    except OSError as exc:
        return False, f"读不到截图内容：{type(exc).__name__}"
    dim = image_size(head)
    if dim is None:
        return False, f"文件不是可识别的 PNG/JPEG（{size} 字节）"
    w, h = dim
    if w < min_edge or h < min_edge:
        return False, f"截图只有 {w}x{h}，视口没渲染出来"
    return True, f"截图有效：{w}x{h}，{size // 1024}KB"


def verify_file(path: str, *, min_bytes: int = MIN_FILE_BYTES) -> Tuple[bool, str]:
    """普通产物文件是否存在且有内容。"""
    p = pathlib.Path(str(path or ""))
    if not p.is_file():
        return False, f"文件不存在：{p}"
    try:
        size = p.stat().st_size
    except OSError as exc:
        return False, f"读不到文件大小：{type(exc).__name__}"
    if size < min_bytes:
        return False, f"文件只有 {size} 字节（< {min_bytes}），等于没内容"
    return True, f"文件有效：{size} 字节"


def verify_http(status: object, body: str = "", *,
                expect: str = "",
                min_chars: int = 0) -> Tuple[bool, str]:
    """HTTP 响应是否拿到真东西。"""
    try:
        code = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, f"拿不到 HTTP 状态码（{status!r}）"
    if code < 200 or code >= 400:
        return False, f"HTTP {code}，不是成功响应"
    text = str(body or "")
    if min_chars and len(text) < min_chars:
        return False, f"响应正文只有 {len(text)} 字（< {min_chars}），多半是空壳"
    if expect and expect not in text:
        return False, f"响应正文里没有「{expect}」"
    return True, f"HTTP {code}，正文 {len(text)} 字"


def verify_artifact(kind: str, value: object, *,
                    expect: str = "",
                    min_bytes: int = MIN_FILE_BYTES) -> Tuple[bool, str]:
    """按类型校验一个产物。未知 kind 一律不通过（fail-closed）。"""
    k = str(kind or "").strip().lower()
    if k == "screenshot":
        return verify_screenshot(str(value or ""))
    if k == "file":
        return verify_file(str(value or ""), min_bytes=min_bytes)
    if k == "http":
        if isinstance(value, dict):
            return verify_http(value.get("status"), value.get("body") or "",
                               expect=expect)
        return False, "http 产物必须是 {status, body}"
    if k == "text":
        text = str(value or "")
        if not text.strip():
            return False, "文本产物是空的"
        if expect and expect not in text:
            return False, f"文本里没有「{expect}」"
        return True, f"文本 {len(text)} 字"
    return False, f"未知的产物类型：{kind!r}"


def media_paths(context: dict) -> list:
    """从 task.context 里取出当前仍在磁盘上的媒体路径（去重保序）。"""
    out: list = []
    try:
        raw = (context or {}).get("generated_image_paths") or []
    except Exception:
        return out
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        s = str(item or "")
        if not s or s in out:
            continue
        try:
            if pathlib.Path(s).is_file():
                out.append(s)
        except OSError:
            continue
    return out


def describe(paths: Iterable[str]) -> str:
    """把产物列表渲染成给模型看的一小段事实描述。"""
    rows: list = []
    for p in paths:
        ok, why = verify_screenshot(str(p))
        rows.append(f"  - {p}" + ("" if ok else f"（{why}）"))
    if not rows:
        return "（本轮还没有任何截图/文件产物）"
    return "\n".join(rows)
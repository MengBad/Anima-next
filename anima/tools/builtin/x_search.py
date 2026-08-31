"""x_search 工具：让 Agent 随时调用 X（Twitter）搜索/查内容，不依赖特定触发词。

Agent 在需要实时、X 平台专属、或用户明确要求「去 X 查/在 X 上搜」的信息时使用。
结果经 x_browser 的合规闸门（政治拒、违法R18 拒）后返回文本摘要。
未登录态时结果可能受限，会在返回里注明。

当用户要「照片 / 图片 / 拿图」时，用 want_images=True：工具会把推文配图下载到
本地，并写入调用上下文 generated_image_paths，由出站层作为 QQ 图片段发送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from anima.media import build_multimodal_user_content

logger = logging.getLogger("anima.tools.x_search")

# 视觉相关性过滤总超时（秒）：视觉慢/网关挂不阻塞拿图流程，超时即回退全发。
_RELEVANCE_TIMEOUT = 20.0


async def _download_image(url: str, dest_dir: Path, proxy: str | None = None) -> str | None:
    """下载单张图片到本地，返回本地路径；失败返回 None。

    下载顺序（针对 X 风控下的真实情况）：
    1. 若配置了代理，先走代理（X 要求代理出口）；
    2. 代理失败/异常时**回退直连** —— pbs.twimg.com 图片 CDN 对 VPS 直连出口
       通常放行（TCP/HTTPS 直连可通），而家宽代理节点转发时反而常被 CDN 拒或断连。
    这样在代理节点异常时仍能从直连拿到图，提高抓取成功率。

    文件名基于 URL 内容 hash 稳定生成：同一张图重复下载会覆盖已有文件，
    不再每次堆积带时间戳的新文件。
    """
    import aiohttp
    import hashlib

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Referer": "https://x.com/",
    }

    async def _one_attempt(use_proxy: bool) -> str | None:
        # 返回 (本地路径 | None, 是否应继续尝试下一种)
        kw = dict(headers=headers)
        if use_proxy and proxy:
            kw["proxy"] = proxy
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=25, sock_connect=10)
        ) as sess:
            async with sess.get(url, **kw) as resp:
                if resp.status != 200:
                    # 代理 404/403 可能是节点被 CDN 挡，值得回退直连；直连非200则终局
                    logger.warning("X 图片下载 HTTP %s (%s): %s",
                                   resp.status, "proxy" if use_proxy else "direct", url[:80])
                    return None
                data = await resp.read()
                if not data:
                    return None
                base = os.path.basename(url.split("?")[0])
                ext = Path(base).suffix.lower()
                if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    ext = ".jpg"
                digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
                fname = f"x_{digest}.{ext.lstrip('.')}"
                dest = dest_dir / fname
                dest.write_bytes(data)
                return str(dest.resolve())

    try:
        if proxy:
            try:
                got = await _one_attempt(use_proxy=True)
                if got:
                    return got
            except Exception as e:  # noqa: BLE001
                logger.warning("X 图片下载走代理失败，回退直连: %s", e)
            # 代理路径失败/异常 -> 回退直连
            got = await _one_attempt(use_proxy=False)
            if got:
                return got
            return None
        # 无代理：直连
        return await _one_attempt(use_proxy=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("X 图片下载失败（已隔离）: %s", e)
        return None


# 同回复节流：同一回复内（同一 call context）对相同 query 的重复抓图不重复下载/视觉过滤。
# 观测到 LLM 一次回复内对相同 X 查询调两次 x_search，第二次走 text 分支即可，省视觉+下载成本。
_SAME_REPLY_THROTTLE_SEC = 30.0


def _normalize_fetch_query(query: str) -> str:
    """归一化 query 作节流键：去空白/大小写，避免细微差异导致漏节流。"""
    return " ".join(str(query or "").strip().split()).lower()[:80]


def _record_reply_fetch(query: str) -> None:
    """记录本次回复已抓取的 query + 时间。"""
    try:
        from anima.tools import get_call_context
        ctx = get_call_context()
        recs = ctx.setdefault("x_fetch_throttle", {})
        if isinstance(recs, dict):
            recs[_normalize_fetch_query(query)] = time.time()
    except Exception:  # noqa: BLE001
        pass


def _existing_gen_paths() -> list[str]:
    """返回调用上下文里已写入的 generated_image_paths（去重保序）。"""
    try:
        from anima.tools import get_call_context
        ctx = get_call_context()
        paths = ctx.get("generated_image_paths") or []
        return [str(p) for p in paths] if isinstance(paths, list) else []
    except Exception:  # noqa: BLE001
        return []


def _is_reply_fetch_duplicate(query: str) -> bool:
    """同回复内是否已抓取过该 query（节流命中）。"""
    try:
        from anima.tools import get_call_context
        ctx = get_call_context()
        recs = ctx.get("x_fetch_throttle") or {}
        if not isinstance(recs, dict):
            return False
        last = recs.get(_normalize_fetch_query(query))
        return bool(last) and (time.time() - last) < _SAME_REPLY_THROTTLE_SEC
    except Exception:  # noqa: BLE001
        return False


def _write_image_paths(paths: list[str]) -> None:
    """把已下载图片路径写入调用上下文 generated_image_paths，供出站发送。"""
    try:
        from anima.tools import get_call_context
        ctx = get_call_context()
        existing = ctx.setdefault("generated_image_paths", [])
        if isinstance(existing, list):
            for p in paths:
                if p not in existing:
                    existing.append(p)
            logger.debug("[IMGDBG] _write_image_paths 写入 %d 条 -> generated_image_paths(len=%d) first=%s",
                        len(paths), len(existing),
                        existing[0] if existing else None)
        else:
            logger.warning("[IMGDBG] _write_image_paths: call_context.generated_image_paths 非 list，跳过 (%s)",
                           type(existing).__name__)
    except Exception as e:  # noqa: BLE001
        logger.warning("[IMGDBG] 写入 generated_image_paths 失败（忽略）: %s", e)


# Gemini 看图判断「图与搜索主题是否相关」的多选过滤。
# 输入：候选本地图路径 + 查询词；输出：过滤后（保持原顺序）的路径列表。
# 任何异常 fail-soft 返回空表（调用方回退为全发，不因视觉失败而空手）。
_RELEVANCE_SYSTEM = (
    "你是一名配图相关性审校员。下面给出若干张候选图片（已按 0..N-1 编号），"
    "以及用户搜索/想要的主题。请判断每张图的内容是否与主题相关，"
    "只保留真正相关的图（画面内容贴合主题，不是模糊/截图/无关/广告/头像）。"
    "用 choose_relevant 工具返回相关图片的编号列表；都不相关返回空列表。"
)
_RELEVANCE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "choose_relevant",
            "description": "返回与主题相关的图片编号列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "与主题相关的图片编号（0 基）。",
                    }
                },
                "required": ["indices"],
            },
        },
    }
]


async def _filter_images_by_relevance(session, query: str,
                                      candidates: list[str]) -> tuple[bool, list[str]]:
    """用 Gemini 判断候选图哪些与 query 主题相关，返回相关路径（保持原顺序）。

    返回 (ok, filtered)：
    - ok=True,  filtered=[相关图]：视觉成功，filtered 即判定相关（可能为空=都不相关）。
    - ok=False, filtered=[]：视觉不可用/失败/超时。调用方据此**保底全发**，
      不能因视觉问题而空手，但也不能把「判定都不相关」当成失败去全发。

    鲁棒性：全程加总超时（_RELEVANCE_TIMEOUT），视觉慢/挂不阻塞整个拿图流程。
    """
    core = getattr(session, "_core", None)
    llm = getattr(core, "llm", None) if core is not None else None
    if llm is None or not candidates:
        return False, []

    async def _do():
        from anima.cognitive.vision import get_vision_manager
        vmgr = get_vision_manager()
        # 逐张读图并准备 URIs；超预算/失败的单张跳过（保留 index 映射）
        uris: list[str] = []
        valid: list[int] = []
        per_budget = max(64 * 1024, 8 * 1024 * 1024 // max(1, len(candidates)))
        for i, path in enumerate(candidates):
            try:
                data = Path(path).read_bytes()
            except Exception:
                continue
            try:
                prep = await vmgr._prepared_uris(data, {"path": path}, max_bytes=per_budget, max_frames=1)
            except Exception:
                continue
            if prep:
                uris.append(prep[0])
                valid.append(i)
        if not uris:
            return False, []
        numbered = "\n".join(f"[{i}] 图片 {i}" for i in valid)
        prompt = f"候选图片（编号）：\n{numbered}\n\n用户搜索/想要的主题：{query or ''}\n请返回与主题相关的图片编号。"
        messages = [
            {"role": "system", "content": _RELEVANCE_SYSTEM},
            {"role": "user", "content": build_multimodal_user_content(prompt, uris)},
        ]
        resp = await llm.chat_structured(
            messages=messages, tools=_RELEVANCE_TOOLS,
            temperature=0.0, max_tokens=128,
        )
        sel: list[int] = []
        if isinstance(resp, dict):
            for tc in (resp.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                if fn.get("name") != "choose_relevant":
                    continue
                args = fn.get("arguments", "{}")
                try:
                    data = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    continue
                if isinstance(data, dict):
                    for x in (data.get("indices") or []):
                        try:
                            sel.append(int(x))
                        except (TypeError, ValueError):
                            pass
        valid_set = set(valid)
        picked = [i for i in sel if i in valid_set]
        # 保持原候选顺序
        ordered = [candidates[i] for i in valid if i in picked]
        logger.debug("[IMGDBG] 视觉相关性过滤: %d 候选 -> %d 相关 (query=%s)",
                    len(candidates), len(ordered), query[:20])
        return True, ordered

    try:
        return await asyncio.wait_for(_do(), timeout=_RELEVANCE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("[IMGDBG] 视觉相关性过滤超时(%ds)，回退全发", _RELEVANCE_TIMEOUT)
        return False, []
    except Exception as e:  # noqa: BLE001
        logger.warning("[IMGDBG] 视觉相关性过滤失败（回退全发）: %s", e)
        return False, []


async def tool_x_search(query: str, limit: int = 5, want_images: bool = False,
                       fresh: bool = True) -> str:
    """在 X（Twitter）上搜索内容并返回摘要；want_images=True 时同时返回推文配图。

    适用于：需要 X 平台实时/专属信息（如某账号动态、某话题讨论、用户要求
    「去 X 查一下 XX」）。返回命中的若干条推文文本（已过滤政治与违法内容）。
    注意：结果受账号登录态与每日额度限制；若返回为空可能是额度用尽或未登录。

    Args:
        query: 搜索关键词（中文/英文均可）。
        limit: 返回条数，默认 5，范围 1-10。
        want_images: 用户要「照片/图片/拿图」，或抱怨「图发不出来/没收到/图呢」
            时，必须为 True——工具会下载推文配图并放入出站图片段（回复时作为图片
            消息发送）。不要把「用户抱怨图发不出」当成不传 True 的理由，那正是
            要你重新抓图发一次。
        fresh: 是否避开本会话已发过的图（默认 True）。用户说「换一批/从头来」
            时传 False，允许重新发同一批。
    """
    query = str(query or "").strip()
    if not query:
        return "未提供搜索关键词。"
    try:
        from anima.cognitive.x_browser import get_x_browser
    except Exception as e:  # noqa: BLE001
        logger.warning("x_browser 不可用: %s", e)
        return "X 搜索模块不可用（x_browser 未加载）。"

    limit = max(1, min(10, int(limit or 5)))
    want_images = bool(want_images)
    fresh = bool(fresh)
    try:
        session = get_x_browser()
        if session is None or not session.enabled:
            return "X 浏览器未启用（config.x_browser.enabled=false）。"
        res = await session.search(query, limit=limit, fresh=fresh, images_only=want_images)
    except Exception as e:  # noqa: BLE001
        logger.warning("x_search 调用失败（已隔离）: %s", e)
        return f"X 搜索失败：{e}"

    if not res.get("ok"):
        note = res.get("note", "")
        return f"X 搜索未完成：{note}" if note else "X 搜索未完成（未知原因）。"

    tweets = res.get("tweets", [])
    tweet_images = res.get("tweet_images", [])
    # 优先发未发过的图；fresh=False 时 fresh_images == tweet_images（已 reset）
    fresh_images = res.get("fresh_images", []) or tweet_images

    # 用户要照片：下载推文配图并写入出站图片段
    downloaded: list[str] = []
    if want_images and fresh_images:
        # 同回复节流：同一回复内已抓取过相同 query 则不再重复下载/视觉过滤
        # （复用已写入 generated_image_paths 的图）。省视觉调用+下载成本。
        if _is_reply_fetch_duplicate(query):
            logger.debug("[IMGDBG] 同回复节流命中（query 已抓过），跳过重复下载/视觉过滤: %s", query[:30])
            _ctx_paths = _existing_gen_paths()
            if _ctx_paths:
                downloaded = _ctx_paths
                _record_reply_fetch(query)  # 刷新节流时间窗
            else:
                # 上下文中没有图（可能未下载成功），不节流，正常走下载
                downloaded = []
        else:
            proxy = getattr(session, "proxy", "") or None
            dest_dir = Path(getattr(session, "data_dir", "data/x_browser")) / "downloaded"
            dest_dir.mkdir(parents=True, exist_ok=True)
            # 候选池放大：多下载一些，视觉相关性过滤后仍有余量可选。
            _candidate_urls = fresh_images[: max(limit * 2, limit + 2)]
            _cand_paths: list[str] = []
            for url in _candidate_urls:
                p = await _download_image(url, dest_dir, proxy)
                if p:
                    _cand_paths.append(p)
                if len(_cand_paths) >= limit * 2:
                    break
            # 更新会话级下载统计（P2-2 失败可视化）
            try:
                s = session._stats
                s["images_downloaded"] = s.get("images_downloaded", 0) + len(_cand_paths)
                s["images_failed"] = s.get("images_failed", 0) + max(0, len(_candidate_urls) - len(_cand_paths))
            except Exception:  # noqa: BLE001
                pass
            # 视觉相关性过滤：用 Gemini 看图判断是否与 query 相关，只发相关图。
            if _cand_paths:
                _ok, _filtered = await _filter_images_by_relevance(session, query, _cand_paths)
                if _ok:
                    # 视觉成功：信任判定。filtered 可能为空（都不相关）——宁可不发，也不发无关图。
                    downloaded = _filtered[: limit]
                else:
                    # 视觉不可用/失败/超时：保底全发，不因视觉问题空手。
                    downloaded = _cand_paths[: limit]
        if downloaded:
            _write_image_paths(downloaded[: limit])
            # 记录这些 URL 已发过（会话内去重，下次自动避开）
            try:
                session.mark_images_seen(_candidate_urls[: limit])
            except Exception:  # noqa: BLE001
                logger.debug("mark_images_seen 失败（忽略）")
        # 同回复节流：记录本次已抓取的 query，后续同回复内重复调用不再重复下载/视觉过滤
        _record_reply_fetch(query)

    # 构造文本摘要
    lines = []
    for i, t in enumerate(tweets[:limit], 1):
        lines.append(f"{i}. {t}")
    header = "X 搜索结果（已过滤政治/违法内容）："
    note = res.get("note", "")
    if note:
        header += "\n（" + str(note) + "）"

    if want_images:
        if downloaded:
            header += "\n（已抓取 " + str(len(downloaded)) + " 张配图，将随回复发送）"
        elif tweet_images:
            header += "\n（推文含图但抓取失败，请稍后再试）"
        else:
            header += "\n（本批结果无配图）"

    return header + "\n" + "\n".join(lines)

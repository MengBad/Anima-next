"""x_search 工具：让 Agent 随时调用 X（Twitter）搜索/查内容，不依赖特定触发词。

Agent 在需要实时、X 平台专属、或用户明确要求「去 X 查/在 X 上搜」的信息时使用。
结果经 x_browser 的合规闸门（政治拒、违法R18 拒）后返回文本摘要。
未登录态时结果可能受限，会在返回里注明。

当用户要「照片 / 图片 / 拿图」时，用 want_images=True：工具会把推文配图下载到
本地，并写入调用上下文 generated_image_paths，由出站层作为 QQ 图片段发送。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("anima.tools.x_search")


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
            logger.info("[IMGDBG] _write_image_paths 写入 %d 条 -> generated_image_paths(len=%d) first=%s",
                        len(paths), len(existing),
                        existing[0] if existing else None)
        else:
            logger.warning("[IMGDBG] _write_image_paths: call_context.generated_image_paths 非 list，跳过 (%s)",
                           type(existing).__name__)
    except Exception as e:  # noqa: BLE001
        logger.warning("[IMGDBG] 写入 generated_image_paths 失败（忽略）: %s", e)


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
        proxy = getattr(session, "proxy", "") or None
        dest_dir = Path(getattr(session, "data_dir", "data/x_browser")) / "downloaded"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for url in fresh_images[: limit]:
            p = await _download_image(url, dest_dir, proxy)
            if p:
                downloaded.append(p)
        if downloaded:
            _write_image_paths(downloaded)
            # 记录这些 URL 已发过（会话内去重，下次自动避开）
            try:
                session.mark_images_seen(fresh_images[: limit])
            except Exception:  # noqa: BLE001
                logger.debug("mark_images_seen 失败（忽略）")

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

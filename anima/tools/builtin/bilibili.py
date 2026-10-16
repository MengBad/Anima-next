"""内置 Bilibili 工具：视频字幕理解 + 直播订阅。"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("anima.tools.bilibili")


def make_subscribe_bilibili_live(core):
    """创建直播订阅工具 handler（需要 core）。"""
    from anima.tools import get_call_context

    async def _subscribe_bilibili_live(room: str, target_group: int | None = None) -> str:
        """创建直播订阅；真正写入前仍由 danger 审批门控。"""
        value = str(room).strip()
        match = re.fullmatch(r"\d{1,15}", value) or re.fullmatch(
            r"https?://live\.bilibili\.com/(\d{1,15})/?(?:\?.*)?", value
        )
        if match is None:
            raise ValueError("请提供 B 站直播间数字 ID 或 https://live.bilibili.com/<ID> 链接")
        room_id = match.group(1) if match.lastindex else match.group(0)
        context = get_call_context()
        group_id = int(target_group or context.get("group_id") or 0)
        if group_id <= 0:
            raise ValueError("私聊创建订阅时必须明确提供目标群号")
        monitors = getattr(core, "monitors", None)
        store = getattr(monitors, "store", None)
        if store is None:
            raise RuntimeError("订阅调度器尚未初始化")
        from anima.monitors import Subscription
        sub = store.add(Subscription(
            platform="bilibili_live", subject_id=room_id, target_groups=[group_id],
        ))
        audit = getattr(core, "audit", None)
        if audit:
            audit.emit("bilibili_subscription_created_from_tool", subscription_id=sub.id,
                       room_id=room_id, group_id=group_id)
        return f"已订阅 B 站直播间 {room_id}；开播时会通知群 {group_id}（订阅 ID：{sub.id}）"

    return _subscribe_bilibili_live


def make_bilibili_read(core):
    """创建 bilibili_read 工具 handler（需要 core）。"""

    async def _tool_bilibili_read(bvid: str) -> str:
        """理解一个哔哩哔哩视频：抓取字幕并总结，让回复贴合视频内容。

        当消息中出现 B 站视频链接 / BV 号 / b23.tv 短链时调用。
        bvid 可传 BV 号、完整视频链接或 b23.tv 短链。若无字幕会如实说明，
        不要把"无字幕"说成"已看完视频"。

        若传入 space.bilibili.com 用户主页，则列出最近投稿（不是读单个视频）。
        """
        import re as _re
        text = str(bvid or "").strip()
        if not text:
            return "请提供 B 站视频链接或 BV 号。"
        BVID = _re.compile(r"BV[0-9A-Za-z]{10,12}")
        SPACE = _re.compile(
            r"(?:space\.bilibili\.com|bilibili\.com/space)/(\d+)",
            _re.I,
        )

        async def _resolve(raw: str) -> str:
            # b23.tv 短链：跟重定向拿真实 URL 再抽 BV
            if "b23.tv" in raw or raw.startswith("b23"):
                try:
                    import aiohttp
                    to = aiohttp.ClientTimeout(total=10)
                    async with aiohttp.ClientSession(timeout=to) as s:
                        real = raw if raw.startswith("http") else "https://" + raw
                        for _ in range(10):
                            async with s.get(real, allow_redirects=False) as r:
                                loc = r.headers.get("Location")
                            if not loc or not loc.startswith("http"):
                                break
                            real = loc
                    m = BVID.search(real)
                    if m:
                        return m.group(0)
                    sm = SPACE.search(real)
                    if sm:
                        return f"space:{sm.group(1)}"
                except Exception:
                    logger.exception("解析 b23.tv 短链失败")
                return ""
            sm = SPACE.search(raw)
            if sm:
                return f"space:{sm.group(1)}"
            m = BVID.search(raw)
            return m.group(0) if m else ""

        async def _list_space(mid: str) -> str:
            """公开接口拉 UP 最近投稿；多层回退，失败时给可执行建议。"""
            import aiohttp

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://space.bilibili.com/{mid}",
                "Origin": "https://space.bilibili.com",
            }
            to = aiohttp.ClientTimeout(total=12)
            name = f"UID {mid}"
            last_err = ""
            try:
                async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                    async def _json(url: str, params: dict) -> dict:
                        async with s.get(url, params=params) as r:
                            if r.status != 200:
                                raise RuntimeError(f"HTTP {r.status}")
                            ctype = (r.headers.get("Content-Type") or "").lower()
                            if "json" not in ctype and "javascript" not in ctype:
                                text = await r.text()
                                if text.lstrip().startswith("<"):
                                    raise RuntimeError(f"HTTP {r.status} HTML challenge")
                                try:
                                    return json.loads(text)
                                except Exception as exc:  # noqa: BLE001
                                    raise RuntimeError(f"non-json body: {exc}") from exc
                            return await r.json()

                    # 昵称（尽力；acc/info 常被拦）
                    for url, params in (
                        ("https://api.bilibili.com/x/web-interface/card", {"mid": mid}),
                        ("https://api.bilibili.com/x/space/acc/info", {"mid": mid}),
                    ):
                        try:
                            payload = await _json(url, params)
                            if payload.get("code") == 0:
                                data = payload.get("data") or {}
                                card = data.get("card") if isinstance(data, dict) else None
                                if isinstance(card, dict) and card.get("name"):
                                    name = str(card["name"])
                                    break
                                if isinstance(data, dict) and data.get("name"):
                                    name = str(data["name"])
                                    break
                        except Exception as exc:  # noqa: BLE001
                            last_err = str(exc)[:80]

                    vlist: list[dict] = []
                    # 1) 经典投稿列表（常被拦）
                    for url in (
                        "https://api.bilibili.com/x/space/wbi/arc/search",
                        "https://api.bilibili.com/x/space/arc/search",
                    ):
                        try:
                            payload = await _json(
                                url, {"mid": mid, "ps": 5, "pn": 1, "order": "pubdate"},
                            )
                            if payload.get("code") == 0:
                                vlist = (
                                    ((payload.get("data") or {}).get("list") or {}).get("vlist")
                                    or []
                                )
                                if vlist:
                                    break
                            last_err = f"code={payload.get('code')}"
                        except Exception as exc:  # noqa: BLE001
                            last_err = str(exc)[:80]

                    # 2) polymer 合集/系列归档（VPS 实测常可用）
                    if not vlist:
                        try:
                            payload = await _json(
                                "https://api.bilibili.com/x/polymer/web-space/home/seasons_series",
                                {"mid": mid, "page_num": 1, "page_size": 20},
                            )
                            if payload.get("code") == 0:
                                items = ((payload.get("data") or {}).get("items_lists") or {})
                                seen: set[str] = set()
                                for season in (items.get("seasons_list") or []) + (items.get("series_list") or []):
                                    meta = season.get("meta") if isinstance(season, dict) else None
                                    archives = season.get("archives") if isinstance(season, dict) else None
                                    if not isinstance(archives, list):
                                        continue
                                    for arc in archives:
                                        if not isinstance(arc, dict):
                                            continue
                                        bvid_val = str(arc.get("bvid") or "").strip()
                                        if not bvid_val or bvid_val in seen:
                                            continue
                                        seen.add(bvid_val)
                                        title = str(arc.get("title") or "无标题")[:80]
                                        vlist.append({"bvid": bvid_val, "title": title})
                                        if len(vlist) >= 5:
                                            break
                                    if len(vlist) >= 5:
                                        break
                            else:
                                last_err = f"polymer code={payload.get('code')}"
                        except Exception as exc:  # noqa: BLE001
                            last_err = str(exc)[:80]

                    # 3) 动态流里抽视频卡片
                    if not vlist:
                        try:
                            payload = await _json(
                                "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
                                {"host_mid": mid, "offset": "", "page": 1},
                            )
                            if payload.get("code") == 0:
                                items = ((payload.get("data") or {}).get("items") or [])
                                seen: set[str] = set()
                                for item in items:
                                    modules = (item.get("modules") or {}) if isinstance(item, dict) else {}
                                    major = (modules.get("module_dynamic") or {}).get("major") or {}
                                    archive = major.get("archive") if isinstance(major, dict) else None
                                    if not isinstance(archive, dict):
                                        continue
                                    bvid_val = str(archive.get("bvid") or "").strip()
                                    if not bvid_val or bvid_val in seen:
                                        continue
                                    seen.add(bvid_val)
                                    title = str(archive.get("title") or "无标题")[:80]
                                    vlist.append({"bvid": bvid_val, "title": title})
                                    if len(vlist) >= 5:
                                        break
                            else:
                                last_err = f"dynamic code={payload.get('code')}"
                        except Exception as exc:  # noqa: BLE001
                            last_err = str(exc)[:80]

                    if not vlist:
                        return (
                            f"拿到了 UP 主页（{name} / mid={mid}），但最近投稿列表接口暂时不可用"
                            f"（{last_err or 'unknown'}）。"
                            "请用户直接发某个视频的 BV 链接，本喵再用 bilibili_read 读字幕。"
                        )
                    lines = [f"UP 主「{name}」最近投稿："]
                    for item in vlist[:5]:
                        bv = item.get("bvid") or ""
                        title = str(item.get("title") or "无标题")[:80]
                        lines.append(f"- {title} ({bv})" if bv else f"- {title}")
                    lines.append(
                        "若要本喵看某一期内容，请直接发对应 BV 链接；"
                        "主页链接本身不含视频字幕。"
                    )
                    return "\n".join(lines)
            except Exception as exc:
                logger.exception("读取 B 站主页失败 mid=%s", mid)
                return (
                    f"这是 B 站用户主页链接（mid={mid}），不是单个视频。"
                    f"主页最近投稿暂时读不到。"
                    "请发具体视频的 BV 链接，或告诉本喵要看哪一期。"
                )

        def _record_space(ok: bool) -> None:
            try:
                quality = getattr(core, "reply_quality", None)
                if quality is not None:
                    quality.record_space_link(ok=ok)
            except Exception:
                logger.exception("space 链接遥测记录失败")

        bv = await _resolve(text)
        if not bv:
            if "space.bilibili.com" in text.lower() or "bilibili.com/space" in text.lower():
                _record_space(False)
            return ("没认出有效的 B 站视频 BV 号或用户主页，请检查链接"
                    "（支持 bilibili.com/video/BVxxx、space.bilibili.com/{mid} 与 b23.tv 短链）。")
        if bv.startswith("space:"):
            space_result = await _list_space(bv.split(":", 1)[1])
            ok = bool(space_result) and (
                "最近投稿" in space_result
                or "没有公开投稿" in space_result
                or "请用户直接发" in space_result
                or "请发具体视频" in space_result
                or "请发对应 BV" in space_result
            )
            _record_space(ok)
            return space_result

        cfg = getattr(core, "config", None)
        bcfg = getattr(cfg, "bilibili_read", None) if cfg else None
        if not (bcfg and getattr(bcfg, "enabled", True)):
            return "B 站视频理解功能未启用。"

        async def _http_view_and_subs(bvid_val: str) -> tuple[str, list[dict], str]:
            """不依赖 bilibili_api 包：view + player/v2 拉标题与字幕列表。"""
            import aiohttp
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.bilibili.com/video/{bvid_val}",
            }
            to = aiohttp.ClientTimeout(total=15)
            title = "未知标题"
            async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                async with s.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bvid_val},
                ) as r:
                    if r.status != 200:
                        return title, [], f"view HTTP {r.status}"
                    payload = await r.json()
                if payload.get("code") != 0:
                    return title, [], f"view code={payload.get('code')}"
                data = payload.get("data") or {}
                title = str(data.get("title") or title)
                pages = data.get("pages") or []
                cid = None
                if pages and isinstance(pages[0], dict):
                    cid = pages[0].get("cid")
                if not cid:
                    cid = data.get("cid")
                if not cid:
                    return title, [], "缺少 cid"
                async with s.get(
                    "https://api.bilibili.com/x/player/v2",
                    params={"bvid": bvid_val, "cid": cid},
                ) as r2:
                    if r2.status != 200:
                        return title, [], f"player HTTP {r2.status}"
                    p2 = await r2.json()
                if p2.get("code") != 0:
                    return title, [], f"player code={p2.get('code')}"
                sub = (((p2.get("data") or {}).get("subtitle") or {}).get("subtitles")
                       or [])
                norm: list[dict] = []
                for item in sub:
                    if not isinstance(item, dict):
                        continue
                    u = item.get("subtitle_url") or item.get("url") or ""
                    norm.append({
                        "lan": item.get("lan") or item.get("lan_doc") or "",
                        "subtitle_url": u,
                    })
                return title, norm, ""

        async def _lib_view_and_subs(bvid_val: str) -> tuple[str, list[dict], str]:
            """优先尝试 bilibili_api（新 Credential / 旧 Verify 均兼容）。"""
            try:
                from bilibili_api import video as bili_video
            except ImportError:
                return "未知标题", [], "no bilibili_api module"

            sessdata = getattr(bcfg, "sessdata", "") or ""
            bili_jct = getattr(bcfg, "bili_jct", "") or ""
            credential = None
            try:
                from bilibili_api import Credential
                credential = Credential(sessdata=sessdata, bili_jct=bili_jct)
            except ImportError:
                try:
                    from bilibili_api import Verify
                    credential = Verify(sessdata, bili_jct) if (sessdata or bili_jct) else None
                except Exception:
                    credential = None

            try:
                if credential is not None:
                    try:
                        v = bili_video.Video(bvid=bvid_val, credential=credential)
                    except TypeError:
                        v = bili_video.Video(bvid_val, credential)
                else:
                    try:
                        v = bili_video.Video(bvid=bvid_val)
                    except TypeError:
                        v = bili_video.Video(bvid_val)
                info = await v.get_info()
                title = str((info or {}).get("title") or "未知标题")
                cid = None
                if hasattr(v, "get_cid"):
                    try:
                        cid = await v.get_cid(0)
                    except Exception:
                        cid = None
                if cid is None and isinstance(info, dict):
                    pages = info.get("pages") or []
                    if pages and isinstance(pages[0], dict):
                        cid = pages[0].get("cid")
                    cid = cid or info.get("cid")
                subs: list[dict] = []
                if cid is not None and hasattr(v, "get_subtitle"):
                    try:
                        sub_info = await v.get_subtitle(cid)
                        raw = (sub_info or {}).get("subtitles") or []
                        for item in raw:
                            if isinstance(item, dict):
                                subs.append({
                                    "lan": item.get("lan") or "",
                                    "subtitle_url": item.get("subtitle_url")
                                    or item.get("url") or "",
                                })
                    except Exception:
                        pass
                return title, subs, ""
            except Exception as exc:  # noqa: BLE001
                return "未知标题", [], str(exc)[:120]

        try:
            title, subs, err = await _lib_view_and_subs(bv)
            if err or not title or title == "未知标题" or not subs:
                t2, s2, e2 = await _http_view_and_subs(bv)
                if t2 and t2 != "未知标题":
                    title = t2
                if s2:
                    subs = s2
                elif err and e2:
                    err = f"{err}; http={e2}"
                elif e2:
                    err = e2

            if not title or title == "未知标题":
                return f"读取 B 站视频失败：{err or '无法获取标题'}（bvid={bv}）"

            if not subs:
                return (f"视频《{title}》暂无可用字幕，无法生成总结"
                        f"（可凭标题/封面和你的了解聊聊，别假装看过内容）。")
            target = None
            for s in subs:
                if str(s.get("lan", "")).startswith("zh"):
                    target = s
                    break
            if target is None:
                target = subs[0]
            url = target.get("subtitle_url", "")
            if not url:
                return f"错误：视频《{title}》字幕元数据缺失地址。"
            if not url.startswith("http"):
                url = "https:" + url
            import aiohttp
            to = aiohttp.ClientTimeout(total=15)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": f"https://www.bilibili.com/video/{bv}",
            }
            async with aiohttp.ClientSession(timeout=to, headers=headers) as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        return f"下载字幕失败（HTTP {r.status}）。"
                    sj = await r.json()
            raw_sub = "\n".join(i.get("content", "") for i in sj.get("body", []))
            maxlen = int(getattr(bcfg, "max_subtitle_length", 4000) or 4000)
            if len(raw_sub) > maxlen:
                raw_sub = raw_sub[:maxlen] + "\n...(后续内容已省略)"
            if not raw_sub.strip():
                return f"视频《{title}》字幕内容解析为空。"

            llm = getattr(core, "llm", None)
            if llm is None:
                return f"视频《{title}》字幕（未启用总结模型）：\n{raw_sub[:maxlen]}"
            smodel = getattr(bcfg, "summary_model", "") or None
            prompt = (
                f"视频标题：《{title}》\n字幕内容：\n{raw_sub}\n\n"
                "请根据上述字幕总结视频的核心观点，保留关键信息，用自然口语表达，"
                "不要堆砌要点序号，像随口跟朋友复述一样。"
            )
            from anima.llm.usage import llm_scope
            with llm_scope("bilibili", "summary"):
                summary = await llm.chat(
                    [{"role": "user", "content": prompt}],
                    max_tokens=600, model=smodel or None,
                )
            return f"《{title}》视频概要：\n{summary}"
        except Exception as e:
            logger.exception("bilibili_read 处理失败 bvid=%s", bv)
            return "读取 B 站视频失败：暂时拿不到内容，可稍后再试"

    return _tool_bilibili_read

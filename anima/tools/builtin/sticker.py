"""表情包选图工具：让 LLM 在调侃/情绪化语境自主决定带哪张图。"""

from __future__ import annotations

import logging

from anima.tools import Tool, LEVEL_USER, get_call_context

logger = logging.getLogger("anima.tools.sticker")


def make_sticker_tools(core) -> list[Tool]:
    """表情包选图工具工厂（仅在 sticker 启用时返回非空列表）。"""
    cfg = getattr(core, "config", None)
    cfg = getattr(cfg, "sticker", None) if cfg else None
    if not (cfg and getattr(cfg, "enabled", True)):
        return []

    async def _pick(query: str = "") -> str:
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        group_id = context.get("group_id")
        allow_cross = bool(context.get("share_stickers", False))
        path = None
        # smart_select：BM25+混合相似度+嵌入语义 加权随机（对标 stealer），零 LLM 视觉终选
        if getattr(cfg, "smart_select", False):
            try:
                path = await mgr.smart_pick(
                    query, group_id=group_id, allow_cross_group=allow_cross,
                    core=core, target_category="")
            except Exception:
                logger.exception("smart_select 失败，退回旧路径")
                path = None
        if path is None and getattr(cfg, "llm_select", True) and getattr(core, "llm", None) is not None:
            try:
                path = await mgr.llm_select(
                    core.llm, query, group_id=group_id, allow_cross_group=allow_cross,
                    k=8, model=getattr(cfg, "select_model", ""))
            except Exception:
                logger.exception("LLM 终选失败，退回本地选图")
                path = None
        if path is None:
            path = mgr.pick(query, group_id=group_id, allow_cross_group=allow_cross)
        if path:
            return f"已选图：{path}（请在回复末尾自然附带这张表情，不要特意解释）"
        return ("库里暂无贴合《%s》的表情，可改调 random_sticker 随便带一张，"
                "或本次回复不带图。" % (query or ""))

    async def _random() -> str:
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        group_id = context.get("group_id")
        allow_cross = bool(context.get("share_stickers", False))
        path = None
        if getattr(cfg, "llm_select", True) and getattr(core, "llm", None) is not None:
            try:
                path = await mgr.llm_random(
                    core.llm, group_id=group_id, allow_cross_group=allow_cross,
                    k=8, model=getattr(cfg, "select_model", ""))
            except Exception:
                logger.exception("LLM 终选失败，退回本地随机")
                path = None
        if path is None:
            path = mgr.random_one(group_id=group_id, allow_cross_group=allow_cross)
        if path:
            return f"已选图：{path}（请在回复末尾自然附带这张表情）"
        return "表情库为空，暂无表情可用，本次回复不带图。"

    async def _search(query: str = "", limit: int = 5) -> str:
        # LLM 工具 search_meme：只读检索，不发送
        try:
            from anima.cognitive.stickers import get_sticker_manager
            mgr = get_sticker_manager(
                core.config.persistence.data_dir,
                recent_window=cfg.recent_window,
                gif_min_interval_sec=cfg.gif_min_interval_sec,
            )
            limit = max(1, min(int(limit or 5), 20))
            rows = mgr.search_assets(query=query, status="approved", sort="newest")[:limit]
            if not rows:
                return "表情库中没有匹配《%s》的表情。" % (query or "")
            lines = []
            for r in rows:
                p = mgr.asset_path(r.get("hash", ""))
                lines.append("%s | 分类=%s 情绪=%s 标签=%s"
                             % (str(p), r.get("category", ""), r.get("emotion", ""),
                                ",".join(r.get("labels") or [])))
            return "找到 %d 张：\n%s" % (len(lines), "\n".join(lines))
        except Exception:
            logger.exception("search_meme 失败")
            return "表情检索暂不可用，本次可改用 send_meme 或不带图。"

    async def _send(query: str = "") -> str:
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        group_id = context.get("group_id")
        allow_cross = bool(context.get("share_stickers", False))
        path = None
        if getattr(cfg, "smart_select", False):
            try:
                path = await mgr.smart_pick(
                    query, group_id=group_id, allow_cross_group=allow_cross,
                    core=core, target_category="")
            except Exception:
                logger.exception("send_meme smart_pick 失败，退回 pick")
                path = None
        if path is None:
            path = mgr.pick(query, group_id=group_id, allow_cross_group=allow_cross)
        if path is None:
            path = mgr.random_one(group_id=group_id, allow_cross_group=allow_cross)
        if path:
            return "已选图：%s" % path
        return "表情库为空或没有匹配，本次回复不带图。"

    async def _steal(desc: str = "", category: str = "") -> str:
        import binascii
        from anima.cognitive.stickers import get_sticker_manager
        mgr = get_sticker_manager(
            core.config.persistence.data_dir,
            recent_window=cfg.recent_window,
            gif_min_interval_sec=cfg.gif_min_interval_sec,
        )
        context = get_call_context()
        imgs = context.get("input_images_b64") or []
        if not imgs:
            return "这条消息里没带图，没得偷。请对方先发一张表情，或用 search_meme/send_meme 从库里选。"
        uploads = []
        for i, b64 in enumerate(imgs[:2]):
            try:
                data = binascii.a2b_base64(b64)
            except Exception:
                continue
            if data:
                uploads.append(("steal_%d.bin" % i, data))
        if not uploads:
            return "图片数据无法解析，偷取失败。"
        approval_required = bool(getattr(cfg, "require_owner_review", True))
        result = await mgr.import_uploads(
            uploads, category=category, scope="local",
            approval_required=approval_required, auto_analyze=False, llm=None)
        added = int(result.get("added", 0)); skipped = int(result.get("skipped", 0))
        if added:
            where = "待审核区" if approval_required else "图库"
            return "已把 %d 张表情收入%s（重复/跳过 %d）。%s" % (added, where, skipped, desc or "")
        return "没有新增表情：可能是重复、数据无效，或已超出待审核容量。"

    return [
        Tool(
            "pick_sticker",
            "选择一张贴合语境的表情包图片，附带在回复之后。仅在回复轻松、调侃、玩梗、情绪化、想活跃气氛时使用；"
            "正式问答、严肃说明、纯信息回复不要调用。query 用中文描述想要的情绪/用途，如『无语』『狗头』『开心』『赞同』。",
            {"query": {"type": "string", "description": "想要的情绪/梗，如 无语/狗头/开心"}},
            _pick, min_level=LEVEL_USER,
        ),
        Tool(
            "random_sticker",
            "随机选一张近期没发过的表情包图片，附带在回复之后（不在乎具体哪张时）。同样仅在调侃/情绪化语境使用，严肃场景不要调用。",
            {},
            _random, min_level=LEVEL_USER,
        ),
        Tool(
            "search_meme",
            "在表情库里按中文描述检索表情（不发送），返回匹配项的路径/分类/情绪/标签。先查库里有啥再说。",
            {"query": {"type": "string", "description": "想找的情绪/梗，如 狗头/无语/开心"},
             "limit": {"type": "integer", "description": "最多返回几条，默认5，上限20"}},
            _search, min_level=LEVEL_USER,
        ),
        Tool(
            "send_meme",
            "按中文描述选一张表情图片，在本次回复里实际附带发出。仅在轻松、调侃、玩梗、情绪化语境使用，严肃问答不要调用。",
            {"query": {"type": "string", "description": "想要的情绪/梗，如 无语/狗头/开心"}},
            _send, min_level=LEVEL_USER,
        ),
        Tool(
            "steal_meme",
            "把当前这条消息里对方发的图片（最多2张）收进表情库。需刚收到带图消息；审核开启时进待审核区。",
            {"desc": {"type": "string", "description": "对这张图的一句话描述/想打的标签(可选)"},
             "category": {"type": "string", "description": "给图分的分类，如 狗头/梗图/日常(可选)"}},
            _steal, min_level=LEVEL_USER,
        ),
    ]

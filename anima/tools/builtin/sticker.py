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
    ]

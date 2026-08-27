# -*- coding: utf-8 -*-
"""L6 三份关系「只读总览」→ 脊聚合（2026-08-28 点亮，视角二·甲为主乙为辅）。

- 只读：尽力读取、失败给默认、不抛错、不写任何存储。constitution 关系段不碰。
- 甲（主）：只聚合「当前对话者这条」（stage/trust，复用 relation_clause）。
- 乙（辅）：当前关系深(FRIEND+) + 语境自然 → 偶带「还惦记着别人 / 谁与谁很熟」一角。
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("anima.inner_state.relation_overview")

_DEEP_STAGES = {"FRIEND", "CLOSE"}


def current_relation_line(data_dir: str, user_id: int) -> str:
    """甲（主）：当前对话者这条关系句。失败/无用户 → 空串(fail-closed)。"""
    if user_id is None:
        return ""
    try:
        from anima.cognitive.relation import get_relation_engine
        rel = get_relation_engine(data_dir).get(int(user_id))
        stage = getattr(getattr(rel, "stage", None), "name", "") or ""
        trust = getattr(rel, "trust", None)
        from anima.cognitive.inner_state import relation_clause
        base = relation_clause(stage)
        if not base:
            return ""
        return base
    except Exception:
        return ""


def people_corner(data_dir: str, user_id: int) -> str:
    """乙（辅）：深关系时偶带「人际一角」；否则空。只读、不写。"""
    try:
        from anima.cognitive.relation import get_relation_engine
        rel = get_relation_engine(data_dir).get(int(user_id))
        stage = getattr(getattr(rel, "stage", None), "name", "") or ""
        if stage.upper() not in _DEEP_STAGES:
            return ""
    except Exception:
        return ""
    try:
        from anima.crossgroup.memory.user_relations import get_user_relation_store
        store = get_user_relation_store(data_dir)
        edges = store.query(str(user_id)) if hasattr(store, "query") else []
        if not edges:
            return ""
        e = edges[0]
        a, b = e.get("a", ""), e.get("b", "")
        rel_type = e.get("relation", "") or "认识"
        other = b if str(a).strip() == str(user_id).strip() else a
        if not other:
            return ""
        return f"还惦记着 {other}（{rel_type}）"
    except Exception:
        return ""


def overview_fragment(data_dir: str, user_id: int, with_corner: bool = False) -> str:
    """聚合供给脊：甲为主；乙（corner）仅在 with_corner=True 且深关系时拼接。"""
    line = current_relation_line(data_dir, user_id)
    if not line:
        return ""
    if with_corner:
        corner = people_corner(data_dir, user_id)
        if corner:
            return line + "；" + corner
    return line
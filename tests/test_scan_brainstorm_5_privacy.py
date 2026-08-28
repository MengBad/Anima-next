# -*- coding: utf-8 -*-
"""#5 跨群群隐私测试。"""
from anima.crossgroup.memory.user_memory import UserMemory


def _mk():
    um = UserMemory(user_id="7")
    um.add_fact("A群说的私事A", source_group="100")
    um.add_fact("B群说的私事B", source_group="200")
    um.add_fact("私聊说的秘密", source_group="private")
    um.add_fact("A群另一件", source_group="100")
    return um


def test_recall_in_group_only_own_group():
    um = _mk()
    got = um.recall_in_group("100", k=10)
    srcs = {f.source_group for f in got}
    assert srcs == {"100"}
    assert len(got) == 2
    assert not any(f.source_group in ("200", "private") for f in got)


def test_recall_in_group_empty_when_no_own_facts():
    um = _mk()
    assert um.recall_in_group("999", k=10) == []


def test_recall_public_still_excludes_private():
    um = _mk()
    got = um.recall_public(k=10)
    assert all(f.source_group != "private" for f in got)
    assert len(got) == 3


def test_social_wiring_layered_memory():
    """分层记忆接线：默认注入按群隔离，按需查询跨群公开。

    - 被动上下文注入（summary_in_group / build_crossgroup_fragment）→ recall_in_group，只本群
    - 显式"问他信息"的查询工具（social query/summarize）→ recall_public，跨群公开（排除私聊）
      → 在别的群问一个老熟人也能答，而非一问三不知
    """
    import io
    src = io.open("anima/tools/builtin/social.py", encoding="utf-8").read()
    # 被动注入：分层仍走 group 作用域
    um_src = io.open("anima/crossgroup/memory/user_memory.py", encoding="utf-8").read()
    assert "def recall_in_group" in um_src
    assert "def summary_in_group" in um_src
    # 查询工具转跨群公开（显式询问不再只回本群）
    assert "um.recall_public" in src
    # 不再仅靠 recall_in_group（显式查询已放开）
    assert "recall_in_group" not in src
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


def test_social_wiring_uses_recall_in_group():
    import io
    src = io.open("anima/tools/builtin/social.py", encoding="utf-8").read()
    assert "recall_in_group" in src
    assert "get_call_context()" in src
    assert "recall_public" in src
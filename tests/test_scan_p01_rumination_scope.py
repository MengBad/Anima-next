# -*- coding: utf-8 -*-
"""P0-B1：反刍取样默认仅群聊桶且限 max_sessions 个会话——私聊一对一语境不得卷入跨上下文反思。"""
import asyncio
from types import SimpleNamespace

import anima.cognitive.rumination as rm


def _engine(monkeypatch, store, cfg=None):
    captured = {}

    class _Learning:
        def is_rumination_paused(self):
            return False
        async def ruminate(self, llm, text):
            captured["text"] = text
            return None

    monkeypatch.setattr("anima.memory.chat_memory.get_chat_memory",
                        lambda *a, **k: SimpleNamespace(_store=store))
    # get_learning 在 rumination 模块顶部已绑定，须 patch 其命名空间引用
    monkeypatch.setattr(rm, "get_learning", lambda dd: _Learning())
    monkeypatch.setattr(rm, "get_world_model", lambda dd: object())
    eng = object.__new__(rm.RuminationEngine)
    eng._last_run = 0.0
    eng.interval_sec = 900.0
    eng._persist = SimpleNamespace(write_json=lambda *a, **k: None)
    eng._state_path = "state.json"
    eng.core = SimpleNamespace(
        llm=object(),
        config=SimpleNamespace(persistence=SimpleNamespace(data_dir="data"), rumination=cfg),
    )
    async def _noop(text):
        return None
    eng._self_growth_dispatch = _noop
    return eng, captured


def test_default_scope_excludes_private_sessions(monkeypatch):
    store = {
        "group_111_222": {"a": [{"role": "user", "name": "A", "content": "群里聊钓鱼周末去水库钓鲫东风很大浮漂都被吹得乱晃但是收获不错钓了七八条鲫鱼还拍了照片准备发帖分享给别的钓友看看收成如何"}]},
        "group_333_444": {"a": [{"role": "user", "name": "B", "content": "群里聊新一代显卡功耗高得离谱电源带不动讨论半天预算从五千涨到八千最后大家决定再等等第三方评测出炉再做购买决定不着急这两周"}]},
        "private_999": {"a": [{"role": "user", "name": "X", "content": "私聊秘密工资单银行流水明细这些非常隐私的内容绝对不该出现在任何反思取样上下文里才对"}]},
    }
    eng, cap = _engine(monkeypatch, store)
    asyncio.run(eng.maybe_ruminate())
    t = cap["text"]
    assert "浮漂" in t and "显卡" in t
    assert "工资单" not in t and "银行流水" not in t, "私聊内容不得进入反思取样"


def test_max_sessions_caps_and_all_scope_keeps_old_behavior(monkeypatch):
    tag = "这是一条足够长的群聊消息用来凑过一百字门槛" * 2   # ~44字/条，两条+名字>100
    store = {f"group_{i}_u": {"a": [
                {"role": "user", "name": f"n{i}", "content": f"{tag}甲{i}"},
                {"role": "user", "name": f"n{i}", "content": f"{tag}乙{i}"},
              ]} for i in range(10)}
    store["private_1"] = {"a": [{"role": "user", "name": "x", "content": "私聊标记字串应当只在all模式出现" + tag}]}
    cfg_all = SimpleNamespace(scope="all", max_sessions=99)
    eng, cap = _engine(monkeypatch, dict(store), cfg_all)
    asyncio.run(eng.maybe_ruminate())
    assert "私聊标记字串" in cap["text"], "scope=all 应保留旧行为"
    # 默认 group_only + max_sessions=6：取末尾6个桶（dict 插入序尾部），前4个不占预算
    eng2, cap2 = _engine(monkeypatch, dict(store), None)
    asyncio.run(eng2.maybe_ruminate())
    assert "私聊标记字串" not in cap2["text"]
    for i in range(0, 4):
        assert f"甲{i}" not in cap2["text"], "超出 max_sessions 的旧桶不应占预算"
    for i in range(4, 10):
        assert f"甲{i}" in cap2["text"]

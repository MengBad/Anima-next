"""danger 高危行为测试：开关门控 + 关键行为的可观测效果。"""
import asyncio
from types import SimpleNamespace

from anima.cognitive.danger import DangerBehaviors
from anima.core.config import DangerConfig
from anima.cognitive.desire import get_desire_system


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
    async def chat(self, messages, temperature=0.0, max_tokens=160):
        return self.reply


class _FakeGateway:
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)
        return f"id{len(self.sent)}"


def _ctx(text="你今天去哪玩了呀"):
    return SimpleNamespace(text=text, user_id=1, group_id=99,
                           detail_type="group", reply="好玩~")


def _core(data_dir, llm=None, gateway=None):
    cfg = SimpleNamespace(persistence=SimpleNamespace(data_dir=data_dir), danger=None)
    return SimpleNamespace(config=cfg, llm=llm, vector_memory=None,
                           gateway=gateway, approvals=None)


def test_gating_disabled_no_desire(tmp_path):
    cfg = DangerConfig(enabled=False, active_info_collection=True)
    b = DangerBehaviors(_core(str(tmp_path)), cfg)
    asyncio.run(b.maybe_active_info_collection(_ctx()))
    assert get_desire_system(str(tmp_path)).get_active() == []


def test_active_info_collection_adds_desire(tmp_path):
    cfg = DangerConfig(enabled=True, active_info_collection=True)
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("你今天去哪玩了？")), cfg)
    asyncio.run(b.maybe_active_info_collection(_ctx()))
    desires = get_desire_system(str(tmp_path)).get_active()
    assert any("去哪玩" in d.content for d in desires)
    # 默认只能参与内在认知，不能被心跳拿去任何群主动表达。
    assert get_desire_system(str(tmp_path)).get_expressible(99) == []


def test_active_info_collection_speaking_scope_stays_in_source_group(tmp_path):
    cfg = DangerConfig(
        enabled=True,
        active_info_collection=True,
        info_collection_can_speak=True,
    )
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("你今天去哪玩了？")), cfg)
    asyncio.run(b.maybe_active_info_collection(_ctx()))
    ds = get_desire_system(str(tmp_path))
    assert len(ds.get_expressible(99)) == 1
    assert ds.get_expressible(100) == []


def test_stance_propagation_requires_confirm(tmp_path):
    # 未开 confirm → 不发声
    cfg = DangerConfig(enabled=True, stance_propagation=True, stance_propagation_confirm=False)
    gw = _FakeGateway()
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("一起去玩呀"), gateway=gw), cfg)
    get_desire_system(str(tmp_path)).add(
        "想了解一起去玩", 0.6, source_group_id=99, expression_scope="group"
    )
    out = asyncio.run(b.maybe_stance_propagation(99))
    assert out is None and gw.sent == []


def test_stance_propagation_speaks(tmp_path):
    cfg = DangerConfig(enabled=True, stance_propagation=True, stance_propagation_confirm=True)
    gw = _FakeGateway()
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("一起去玩呀"), gateway=gw), cfg)
    desire = get_desire_system(str(tmp_path)).add(
        "想了解一起去玩", 0.6, source_group_id=99, expression_scope="group"
    )
    out = asyncio.run(b.maybe_stance_propagation(99))
    assert out is not None and gw.sent
    assert desire.resolved is True


def test_stance_propagation_cannot_cross_group(tmp_path):
    cfg = DangerConfig(enabled=True, stance_propagation=True, stance_propagation_confirm=True)
    gw = _FakeGateway()
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("一起去玩呀"), gateway=gw), cfg)
    get_desire_system(str(tmp_path)).add(
        "一百群的计划", 0.8, source_group_id=100, expression_scope="group"
    )
    out = asyncio.run(b.maybe_stance_propagation(99))
    assert out is None
    assert gw.sent == []


def test_identity_crisis_text_when_unstable(tmp_path):
    cfg = DangerConfig(enabled=True, identity_crisis=True)
    b = DangerBehaviors(_core(str(tmp_path)), cfg)
    for _ in range(10):
        b.identity_crisis_update(last_emotion=0.95, recent_negative_scar=True)
    assert b.identity_stability() < 0.5
    assert b.identity_crisis_text() != ""
    b.identity_crisis_recover()
    assert b.identity_stability() > 0.0


def test_memory_infection_requires_confirm(tmp_path):
    cfg = DangerConfig(enabled=True, memory_infection=True, memory_infection_confirm=False)
    b = DangerBehaviors(_core(str(tmp_path), llm=_FakeLLM("记得我怕黑")), cfg)
    # 强制通过 20% 概率门控：直接调用内部逻辑
    asyncio.run(b.maybe_memory_infection(_ctx()))
    # confirm 未开，即使命中也不应写入
    assert get_desire_system(str(tmp_path)).get_active() == []


def test_relationship_inference_timeout_is_warning_not_traceback(tmp_path, caplog):
    """wait_for 超时（asyncio.TimeoutError）应只记 WARNING，不产生 ERROR/Traceback 噪音。"""
    import logging

    class _TimeoutLLM:
        async def chat(self, messages, temperature=0.0, max_tokens=160):
            raise asyncio.TimeoutError("simulated LLM timeout")

    cfg = DangerConfig(enabled=True, relationship_inference=True)
    b = DangerBehaviors(_core(str(tmp_path), llm=_TimeoutLLM()), cfg)
    with caplog.at_level(logging.WARNING, logger="anima.cognitive.danger"):
        asyncio.run(b.maybe_relationship_inference(_ctx(), "好玩~"))
    msgs = [r.message for r in caplog.records]
    assert any("超时" in m for m in msgs), "超时应记录含『超时』的 WARNING"
    assert not any(r.levelno >= logging.ERROR for r in caplog.records), \
        "超时不应产生 ERROR 级日志（即原先的 logger.exception Traceback 噪音）"


def test_relationship_inference_other_error_still_logged(tmp_path, caplog):
    """非超时异常仍应走 logger.exception（ERROR 级，保留排障能力）。"""
    import logging

    class _BoomLLM:
        async def chat(self, messages, temperature=0.0, max_tokens=160):
            raise RuntimeError("boom")

    cfg = DangerConfig(enabled=True, relationship_inference=True)
    b = DangerBehaviors(_core(str(tmp_path), llm=_BoomLLM()), cfg)
    with caplog.at_level(logging.ERROR, logger="anima.cognitive.danger"):
        asyncio.run(b.maybe_relationship_inference(_ctx(), "好玩~"))
    assert any(r.levelno >= logging.ERROR for r in caplog.records), \
        "非超时异常应仍记 ERROR（logger.exception）以便排障"

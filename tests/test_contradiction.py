"""矛盾检测测试：开关门控、语义去重、环形上限裁剪。"""
import asyncio

from anima.cognitive.contradiction import ContradictionDetector
from anima.core.config import ContradictionConfig


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
    async def chat(self, messages, temperature=0.0, max_tokens=160):
        return self.reply


class _FakeVector:
    def __init__(self, hits):
        self._hits = hits
    async def search(self, *a, **k):
        return self._hits


def _hit(sim=0.9, text="用户以前说过他不住北京"):
    return [{"text": text, "similarity": sim}]


def test_disabled_returns_none(tmp_path):
    cfg = ContradictionConfig(enabled=False)
    d = ContradictionDetector(str(tmp_path), cfg)
    out = asyncio.run(d.detect_user_contradiction(
        _FakeLLM("矛盾：X"), _FakeVector(_hit()), "我现在住北京", 1, None, "group"))
    assert out is None


def test_no_prior_memory_returns_none(tmp_path):
    cfg = ContradictionConfig(enabled=True)
    d = ContradictionDetector(str(tmp_path), cfg)
    out = asyncio.run(d.detect_user_contradiction(
        _FakeLLM("矛盾：X"), _FakeVector([]), "我现在住北京", 1, None, "group"))
    assert out is None


def test_detects_and_records(tmp_path):
    cfg = ContradictionConfig(enabled=True, max=10)
    d = ContradictionDetector(str(tmp_path), cfg)
    out = asyncio.run(d.detect_user_contradiction(
        _FakeLLM("矛盾：用户现在说住北京，但之前说不住北京"),
        _FakeVector(_hit()), "我现在住北京", 1, None, "group"))
    assert out is not None
    assert "北京" in out
    data = d._read()
    assert len(data) == 1


def test_dedup_skips_duplicate(tmp_path):
    cfg = ContradictionConfig(enabled=True, max=10)
    d = ContradictionDetector(str(tmp_path), cfg)
    desc = "用户现在说住北京，但之前说不住北京"
    r1 = asyncio.run(d.detect_user_contradiction(
        _FakeLLM(f"矛盾：{desc}"), _FakeVector(_hit()), "我现在住北京", 1, None, "group"))
    r2 = asyncio.run(d.detect_user_contradiction(
        _FakeLLM(f"矛盾：{desc}"), _FakeVector(_hit()), "我现在住北京", 1, None, "group"))
    assert r1 is not None
    assert r2 is None  # 语义去重命中
    assert len(d._read()) == 1


def test_ring_cap(tmp_path):
    cfg = ContradictionConfig(enabled=True, max=3, dedup_recent=2)
    d = ContradictionDetector(str(tmp_path), cfg)
    # 5 条彼此语义不同的矛盾（避免触发语义去重），current_text 需 ≥6 字符才过门控
    facts = [
        "用户说住北京但之前说不住北京",
        "用户说养猫但之前说很怕猫",
        "用户说在上海工作但简历写广州",
        "用户说一直单身但之前秀过婚戒",
        "用户说不吃辣但上次点了特辣锅",
    ]
    for i, fact in enumerate(facts):
        asyncio.run(d.detect_user_contradiction(
            _FakeLLM(f"矛盾：{fact}"),
            _FakeVector(_hit(text=f"旧事实编号{i}")),
            f"我现在要陈述第{i}件关于事实的事", 1, None, "group"))
    assert len(d._read()) == 3  # 环形裁剪到最近 3 条

# -*- coding: utf-8 -*-
"""P1-A：society_debate 在运行中 loop 内不再 run_until_complete(RuntimeError 地雷)，
异常落留痕并诚实降级。"""
import anima.agent.agent_society as society_mod
import anima.os.control_surface as surf_mod


class _V:
    def to_dict(self): return {"ok": True}


async def _fake_debate(question, context="", *a, **k):
    return _V(), _V()


def _mk_control():
    return object.__new__(surf_mod.HumanAIControlSurface)


def test_society_debate_within_running_loop(monkeypatch):
    monkeypatch.setattr(society_mod, "run_society_debate", _fake_debate)
    surf = _mk_control()
    out = surf.society_debate("测试议题")
    assert out.get("question") == "测试议题"
    assert out["verdict"]["ok"] is True


def test_society_debate_failure_is_logged_and_degrades(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("后端崩了")
    monkeypatch.setattr(society_mod, "run_society_debate", _boom)
    surf = _mk_control()
    out = surf.society_debate("坏议题")
    assert out.get("available") is False
    assert out.get("reason")

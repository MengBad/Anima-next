"""观察驱动循环：用事实判完成，不靠 LLM 猜文本。"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

from anima.agent import artifact
from anima.agent.loop import ObserveLoop, Observation, RoundRecord


class _Task:
    def __init__(self, request="做点事", ctx=None, status="executing"):
        self.request = request
        self.status = status
        self.context = dict(ctx or {})


class _FakeLoop:
    def __init__(self, replies, tools_per_round=None):
        self._replies = list(replies)
        self._tools = list(tools_per_round or [])
        self.tool_trace = []
        self.calls = 0

    async def run(self, messages, temperature=0.8, max_tokens=512):
        self.calls += 1
        name = self._tools[self.calls - 1] if self.calls <= len(self._tools) else "noop"
        self.tool_trace = [{"name": name}]
        return self._replies[(self.calls - 1) % len(self._replies)]


def _loop(replies, tools=None, **kw):
    fake = _FakeLoop(replies, tools)
    return ObserveLoop(
        core=object(), llm=object(), tools_for=lambda: object(),
        react_factory=lambda llm, t: fake, **kw), fake


# ---- artifact ----

def test_verify_screenshot_rejects_relative_path():
    ok, why = artifact.verify_screenshot("shot.png")
    assert not ok and "绝对路径" in why


def test_verify_screenshot_rejects_missing():
    import pathlib as _pl
    missing = str(_pl.Path(tempfile.gettempdir()) / 'anima-definitely-missing.png')
    ok, why = artifact.verify_screenshot(missing)
    assert not ok and '不存在' in why


def test_verify_screenshot_rejects_tiny(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    ok, why = artifact.verify_screenshot(str(p))
    assert not ok and "字节" in why


def test_verify_screenshot_accepts_real_png(tmp_path):
    # 造一个 800x600 的合法 PNG 头，再补够体积
    head = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (800).to_bytes(4, "big") + (600).to_bytes(4, "big"))
    p = tmp_path / "b.png"
    p.write_bytes(head + b"\x00" * 20000)
    ok, why = artifact.verify_screenshot(str(p))
    assert ok, why
    assert "800x600" in why


def test_verify_http_rejects_error_status():
    ok, why = artifact.verify_http(404, "nope")
    assert not ok and "404" in why


def test_verify_http_requires_keyword():
    ok, why = artifact.verify_http(200, "hello", expect="积分")
    assert not ok and "积分" in why


def test_verify_artifact_unknown_kind_fails_closed():
    ok, _ = artifact.verify_artifact("quantum", "x")
    assert ok is False


def test_media_paths_skips_deleted(tmp_path):
    alive = tmp_path / "k.png"
    alive.write_bytes(b"x" * 100)
    ctx = {"generated_image_paths": [str(alive), "/gone/zzz.png"]}
    assert artifact.media_paths(ctx) == [str(alive)]


# ---- loop 完成判据 ----

def test_completion_requires_screenshot_when_declared():
    t = _Task(ctx={"done_when_kind": "screenshot"})
    loop, _ = _loop(["做点事"])
    ok, why = loop.check_completion(t, [], [])
    assert not ok and "截图" in why


def test_completion_passes_with_valid_screenshot(tmp_path):
    head = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (800).to_bytes(4, "big") + (600).to_bytes(4, "big"))
    p = tmp_path / "ok.png"
    p.write_bytes(head + b"\x00" * 20000)
    t = _Task(ctx={"done_when_kind": "screenshot",
                   "generated_image_paths": [str(p)]})
    loop, _ = _loop(["做点事"])
    obs = loop.collect_observations(t)
    ok, why = loop.check_completion(t, obs, [])
    assert ok, why


def test_completion_without_criteria_needs_done_word():
    t = _Task()
    loop, _ = _loop(["做了点事"])
    obs = [Observation(kind="text", ok=True)]
    ok, _ = loop.check_completion(t, obs, [RoundRecord(index=1, reply="继续")])
    assert not ok
    ok2, _ = loop.check_completion(t, obs, [RoundRecord(index=1, reply="都做完了收工")])
    assert ok2


# ---- loop 行为 ----

@pytest.mark.asyncio
async def test_loop_stops_when_done_word_and_artifact():
    loop, fake = _loop(["都做完了，收工"])
    t = _Task(ctx={"generated_image_paths": []})
    # 造一个真实产物，否则不算完成
    import pathlib, tempfile
    d = pathlib.Path(tempfile.mkdtemp())
    head = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (800).to_bytes(4, "big") + (600).to_bytes(4, "big"))
    p = d / "x.png"
    p.write_bytes(head + b"\x00" * 20000)
    t.context["generated_image_paths"] = [str(p)]

    out = await loop.run(t)
    assert "收工" in out
    # 首轮还没有 round 记录，按设计先跑一轮拿到「模型自己说收工」；
    # 第二轮开头 collect+check 就该判定完成，不再调模型。
    assert fake.calls == 1, f"应只跑 1 轮，实际 {fake.calls}"


@pytest.mark.asyncio
async def test_loop_runs_until_max_rounds():
    loop, fake = _loop(["还在做"], max_rounds=3)
    t = _Task()
    out = await loop.run(t)
    assert fake.calls == 3
    assert out


@pytest.mark.asyncio
async def test_loop_stops_on_waiting_user():
    loop, fake = _loop(["问主人"])
    t = _Task(status="waiting_user")
    out = await loop.run(t)
    assert "等主人回答" in out
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_loop_stops_when_stalled():
    loop, fake = _loop(["还在做"], max_rounds=40)
    t = _Task()
    out = await loop.run(t)
    assert fake.calls <= 5, f"打转保护没生效，跑了 {fake.calls} 轮"
    assert out


def test_prompt_includes_user_answers():
    t = _Task(ctx={"user_answers": [{"question": "怎么登录", "answer": "用二维码"}]})
    loop, _ = _loop(["x"])
    prompt = loop.build_prompt(t, [])
    assert "用二维码" in prompt
    assert "不要重复问" in prompt


def test_prompt_includes_history():
    loop, _ = _loop(["x"])
    loop.rounds.append(RoundRecord(index=1, reply="打开了登录页", tools=["browse_web_act"]))
    prompt = loop.build_prompt(_Task(), [])
    assert "打开了登录页" in prompt
    assert "browse_web_act" in prompt

def test_harvest_collects_media_from_call_context():
    """回归：工具产出的截图必须被收回 task.context，否则循环看不到产物。

    生产实测（P1 首跑）：108 秒 / 3 轮 / new=0 —— 截图写进了
    get_call_context() 的临时 dict，reset 后永久丢失。
    """
    class _Tools:
        def __init__(self, paths):
            self._paths = paths

        def get_call_context(self):
            return {"generated_image_paths": list(self._paths)}

    class _T:
        def __init__(self):
            self.context = {}

    t = _T()
    loop = ObserveLoop(core=object(), llm=object(), tools_for=lambda: object(),
                       react_factory=lambda a, b: None)
    loop._harvest(_Tools(["/a/shot_1.png", "/a/shot_2.png"]), t)

    assert t.context["generated_image_paths"] == ["/a/shot_1.png", "/a/shot_2.png"]


def test_harvest_is_idempotent_and_dedups():
    class _Tools:
        def get_call_context(self):
            return {"generated_image_paths": ["/a/shot_1.png"]}

    class _T:
        def __init__(self):
            self.context = {"generated_image_paths": ["/a/shot_1.png"]}

    t = _T()
    loop = ObserveLoop(core=object(), llm=object(), tools_for=lambda: object(),
                       react_factory=lambda a, b: None)
    loop._harvest(_Tools(), t)
    loop._harvest(_Tools(), t)

    assert t.context["generated_image_paths"] == ["/a/shot_1.png"]


def test_harvest_never_raises():
    class _Bad:
        def get_call_context(self):
            raise RuntimeError("boom")

    loop = ObserveLoop(core=object(), llm=object(), tools_for=lambda: object(),
                       react_factory=lambda a, b: None)
    loop._harvest(_Bad(), type("T", (), {"context": {}})())

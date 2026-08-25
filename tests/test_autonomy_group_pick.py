"""心跳群号解析 + 主动发言人格回归测试（工单1 / 工单2，P0-1 / P0-2）。

- P0-1：_pick_group 对 group_{gid}_{uid} 误把 uid 段当数字分隔符放大会得到错误群号。
- P0-2：_speak 的 system 必须与哈基米人格一致，禁止「你是 Anima，一个 QQ 群里的 AI」。
"""
from __future__ import annotations

import time
from collections import OrderedDict

import pytest

import anima.cognitive.desire as desire_mod
import anima.cognitive.time_sense as time_sense_mod
import anima.memory.chat_memory as cm_mod
from anima.core.autonomy import (
    Heartbeat,
    score_active_context,
    _is_low_value_active_reply,
    _clean_active_reply,
)
from anima.memory.chat_memory import ChatMemory


# ---------------------------------------------------------------------------
# 工单1 · P0-1 群号解析
# ---------------------------------------------------------------------------

def test_pick_group_parses_gid_correctly(monkeypatch):
    """group_{gid}_{uid} 应解析出正确的 gid，而非被 Python 数字分隔符放大。"""
    mem = ChatMemory()  # 无 persist_path → save 为 no-op
    monkeypatch.setattr(cm_mod, "_chat_memory", mem)

    gid = 727631526
    uid = 10001
    mem.add("group", gid, "user", "小明", "在吗", user_id=uid)
    mem.add("group", gid, "bot", "Anima", "在的", user_id=uid)

    class _FakeHB:
        cooldown_sec = 90
        active_silence_min_sec = 300
        daily_max = 20
        ratio_max = 0.30

        def __init__(self):
            # 满足沉默门控 + 冷却 + 预算
            self._group_last_speak = {}
            self._group_last_msg = {gid: time.time() - 1000}
            self._group_speak_count = {}
            self._group_total_msgs = {gid: 10}

    now = time.time()
    picked = Heartbeat._pick_group(_FakeHB(), now)

    assert picked == gid, f"应解析出正确群号 {gid}，实际 {picked}"
    assert picked != 72763152610001, "不应被数字分隔符放大成错误群号"


def test_pick_group_skip_old_bucket_without_uid(monkeypatch):
    """旧格式 group_{gid}（无 uid 段）不应被误当作候选群。"""
    mem = ChatMemory()
    monkeypatch.setattr(cm_mod, "_chat_memory", mem)
    # 仅有一个旧格式桶，无 uid
    mem._store["group_555"] = OrderedDict()

    class _FakeHB:
        cooldown_sec = 90
        active_silence_min_sec = 300
        daily_max = 20
        ratio_max = 0.30

        def __init__(self):
            self._group_last_speak = {}
            self._group_last_msg = {}
            self._group_speak_count = {}
            self._group_total_msgs = {}

    assert Heartbeat._pick_group(_FakeHB(), time.time()) is None


def test_pick_group_requires_enough_human_activity(monkeypatch):
    """低活跃群不能仅因沉默就被主动插话。"""
    mem = ChatMemory()
    monkeypatch.setattr(cm_mod, "_chat_memory", mem)
    gid = 727631526
    mem.add("group", gid, "user", "小明", "刚说过话", user_id=10001)

    class _FakeHB:
        cooldown_sec = 90
        active_silence_min_sec = 300
        active_min_group_messages = 12
        active_daily_max_per_group = 3
        daily_max = 20
        ratio_max = 0.30

        def __init__(self):
            self._group_last_speak = {}
            self._group_last_msg = {gid: time.time() - 1000}
            self._group_speak_count = {}
            self._group_total_msgs = {gid: 11}

    assert Heartbeat._pick_group(_FakeHB(), time.time()) is None
    hb = _FakeHB()
    hb._group_total_msgs[gid] = 12
    assert Heartbeat._pick_group(hb, time.time()) == gid


def test_active_context_score_prefers_unanswered_question():
    chatter = [
        {"role": "user", "content": "[甲(1)]: 今天吃啥"},
        {"role": "user", "content": "[乙(2)]: 不知道"},
        {"role": "user", "content": "[甲(1)]: 哈哈哈"},
    ]
    question = [
        *chatter,
        {"role": "user", "content": "[乙(2)]: 有没有人知道这个怎么弄？"},
    ]
    bot_finished = [
        *question,
        {"role": "assistant", "content": "[哈基米]: 先试试重启"},
    ]
    assert score_active_context(question) >= 0.55
    assert score_active_context(chatter) < 0.55
    assert score_active_context(bot_finished) < 0.55


def test_active_feedback_engagement_and_low_acceptance_gate():
    class _Audit:
        events = []

        def emit(self, event, **fields):
            self.events.append((event, fields))

    hb = object.__new__(Heartbeat)
    hb.core = type("Core", (), {"audit": _Audit()})()
    hb.active_feedback_window_sec = 900
    hb.active_feedback_min_samples = 4
    hb.active_feedback_min_acceptance = 0.30
    hb._active_feedback_pending = {100: {"sent_at": 1000.0, "source": "heartbeat"}}
    hb._active_feedback_stats = {}

    hb._record_active_engagement(100, 1100.0)
    assert hb.active_acceptance(100)["engaged"] == 1
    assert 100 not in hb._active_feedback_pending

    hb._active_feedback_stats[100] = {"engaged": 0, "ignored": 4}
    assert hb.active_acceptance(100)["limited"] is True
    assert hb._feedback_allows_group(100) is False


def test_active_feedback_expires_as_ignored():
    hb = object.__new__(Heartbeat)
    hb.core = type("Core", (), {"audit": None})()
    hb.active_feedback_window_sec = 60
    hb.active_feedback_min_samples = 2
    hb.active_feedback_min_acceptance = 0.2
    hb._active_feedback_pending = {100: {"sent_at": 1000.0, "source": "agenda"}}
    hb._active_feedback_stats = {}

    hb._expire_active_feedback(1061.0)

    assert hb.active_acceptance(100)["ignored"] == 1
    assert 100 not in hb._active_feedback_pending


# ---------------------------------------------------------------------------
# 工单2 · P0-2 主动发言人格
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_speak_system_is_hakimi(monkeypatch):
    """主动发言 system 必须是哈基米，且不得包含「你是 Anima…AI」禁句。"""
    monkeypatch.setattr(time_sense_mod, "time_description", lambda: "中午")

    class _Desire:
        def prompt_fragment(self, group_id=None):
            return ""

    monkeypatch.setattr(desire_mod, "get_desire_system", lambda: _Desire())

    class _Mem:
        def get_group_context(self, gid, recent_rounds=6):
            return []

        def get_recent_group_context(self, gid, limit=12):
            return [{"role": "user", "name": "小明", "content": "今天这局怎么打比较好？"}]

        def add_group_bot_reply(self, gid, reply):
            pass

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _Mem())

    class _LLM:
        model = "fake"
        captured: list = []

        async def chat(self, messages, **kw):
            _LLM.captured = messages
            return "喵，接一句"

    class _Gateway:
        async def send(self, msg):
            return 1

    class _Core:
        llm = _LLM()
        gateway = _Gateway()

    class _HB:
        _group_last_speak: dict = {}
        _group_last_msg: dict = {727631526: time.time() - 1000}
        _last_active_speak = 0.0
        _group_speak_count: dict = {}
        _tokens_today = 0
        token_budget = 200_000
        active_silence_min_sec = 300
        active_context_score_threshold = 0.40
        core = _Core()

    await Heartbeat._speak(_HB(), 727631526, time.time())

    assert _LLM.captured, "应调用 LLM 生成主动发言"
    sys_msg = _LLM.captured[0]["content"]
    assert "哈基米" in sys_msg, "主动发言 system 必须对哈基米身份"
    assert "你是 Anima，一个 QQ 群里的 AI" not in sys_msg, "禁止客服 AI 口吻"


@pytest.mark.asyncio
async def test_explicit_agenda_uses_committed_prompt_not_random_skip_gate(monkeypatch):
    """Owner/日历议程必须执行明确意图，不能被随机接话的 [skip] 规则抵消。"""
    monkeypatch.setattr(time_sense_mod, "time_description", lambda: "清晨")

    class _Desire:
        def prompt_fragment(self, group_id=None):
            return ""

    monkeypatch.setattr(desire_mod, "get_desire_system", lambda: _Desire())

    class _Mem:
        def get_recent_group_context(self, gid, limit=12):
            return [{"role": "user", "content": "群里好安静，大家是不是去吃东西了"}]

        def get_group_context(self, *args, **kwargs):
            raise AssertionError("明确议程不应回捞过期持久化群聊")

        def add_group_bot_reply(self, gid, reply):
            pass

    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: _Mem())

    class _LLM:
        captured = []

        async def chat(self, messages, **kwargs):
            self.captured = messages
            return "本喵来报个到，验收已经完成啦。"

    class _Gateway:
        async def send(self, msg):
            return 42

    class _HB:
        core = type("Core", (), {"llm": _LLM(), "gateway": _Gateway()})()
        token_budget = 0
        _tokens_today = 0
        _group_last_speak = {}
        _group_speak_count = {}
        _last_active_speak = 0.0
        _recent_active_replies = {}

    heartbeat = _HB()
    assert await Heartbeat._speak(
        heartbeat, 727631526, time.time(), "说明系统验收完成"
    ) is True
    system = heartbeat.core.llm.captured[0]["content"]
    request = heartbeat.core.llm.captured[-1]["content"]
    assert "明确表达议程" in system
    assert "一律说「[skip]」" not in system
    assert "不要输出「[skip]」" in system
    assert "说明系统验收完成" in request
    assert "看看群里最近在聊什么" not in request
    assert not any("群里好安静" in message["content"] for message in heartbeat.core.llm.captured)


@pytest.mark.asyncio
async def test_speak_strips_context_label_and_deduplicates(monkeypatch):
    """主动发言不得带出历史标签，也不得在短时间内换词复读。"""
    monkeypatch.setattr(time_sense_mod, "time_description", lambda: "中午")

    class _Desire:
        def prompt_fragment(self, group_id=None):
            return ""

    monkeypatch.setattr(desire_mod, "get_desire_system", lambda: _Desire())

    class _Mem:
        replies = []

        def get_group_context(self, gid, recent_rounds=6):
            return [{"role": "user", "content": "[你]: 下水玩啊"}]

        def get_recent_group_context(self, gid, limit=12):
            return [{"role": "user", "name": "你", "content": "要不要一起下水玩啊？"}]

        def add_group_bot_reply(self, gid, reply):
            self.replies.append(reply)

    memory = _Mem()
    monkeypatch.setattr(cm_mod, "get_chat_memory", lambda: memory)

    class _LLM:
        replies = iter([
            "[哈基米]: 别骗本喵下水，猫咪沾水会炸毛的！",
            "[哈基米]: 别骗本喵下水，猫咪碰水可是会炸毛的！",
        ])

        async def chat(self, messages, **kw):
            return next(self.replies)

    class _Gateway:
        sent = []

        async def send(self, msg):
            self.sent.append(msg)
            return 1

    class _HB:
        core = type("Core", (), {"llm": _LLM(), "gateway": _Gateway()})()
        token_budget = 200_000
        _tokens_today = 0
        _group_last_speak = {}
        _group_last_msg = {727631526: time.time() - 1000}
        _group_speak_count = {}
        _last_active_speak = 0.0
        _recent_active_replies = {}
        active_silence_min_sec = 300
        active_context_score_threshold = 0.40

    heartbeat = _HB()
    now = time.time()
    assert await Heartbeat._speak(heartbeat, 727631526, now) is True
    assert heartbeat.core.gateway.sent[0].message == "别骗本喵下水，猫咪沾水会炸毛的！"
    assert memory.replies == ["别骗本喵下水，猫咪沾水会炸毛的！"]
    assert await Heartbeat._speak(heartbeat, 727631526, now + 30) is False
    assert len(heartbeat.core.gateway.sent) == 1

def test_score_active_context_rejects_noise_only_last_message():
    history = [
        {"role": "user", "name": "a", "content": "哈哈"},
        {"role": "user", "name": "b", "content": "????"},
    ]
    assert score_active_context(history) == 0.0


def test_score_active_context_boosts_open_question():
    history = [
        {"role": "user", "name": "a", "content": "这游戏为什么这么难啊？"},
    ]
    assert score_active_context(history) >= 0.55


def test_low_value_active_reply_filter():
    assert _is_low_value_active_reply("群里怎么突然这么安静，是不是都在偷偷背着本喵吃好吃的呢？")
    assert _is_low_value_active_reply("大半夜的怎么突然发这种乱码，是不是键盘被你当枕头睡了喵？")
    assert not _is_low_value_active_reply("刚才那个梗也太好笑了，本喵还在回味喵")
    # 劝睡(劝人睡觉)类水话：自主总爱劝人去睡觉，不是真接话，拦截
    assert _is_low_value_active_reply("拯救世界哪有窝着睡觉舒服……特批你赶紧滚去睡大觉喵，明天起不来本喵可要踩你脸了")
    assert _is_low_value_active_reply("你累得够呛，快去乖乖睡觉喵")


def test_clean_active_reply_strips_labels():
    assert _clean_active_reply("[你]: 活得好好的呢喵") == "活得好好的呢喵"


def test_autonomy_tick_single_definition_and_drift_check():
    """T0.1 回归：autonomy._tick 必须只定义一次，且存活版本保留 config 漂移自检与模拟生活节律。"""
    import pathlib
    src = pathlib.Path("anima/core/autonomy.py").read_text(encoding="utf-8")
    assert src.count("    async def _tick(self) -> None:") == 1, "重复定义 _tick"
    assert "_config_drift_check()" in src, "存活 _tick 丢失配置漂移自检"
    assert "_life_checkpoints()" in src, "存活 _tick 应保留模拟生活节律"


def test_constitution_drive_shapes_and_sign():
    """T1.1：轻量宪法桥产出合法 Drive；正情绪+高唤醒 → activated 且 approach>avoid。"""
    from anima.core.autonomy import _constitution_drive, _drive_gate_ok
    d = _constitution_drive(0.8, 0.6, relation_depth=0.2, threat=0.0)
    assert d is not None
    assert set(d) >= {"activated", "approach", "avoid", "intensity"}
    assert _drive_gate_ok(d) is True  # 趋近驱动 → 允许主动表达
    d_avoid = _constitution_drive(0.1, -0.8, relation_depth=0.2, threat=0.9)
    assert d_avoid is not None
    assert _drive_gate_ok(d_avoid) is False  # 回避主导 → fail-closed 压制


def test_drive_gate_ok_fail_closed():
    from anima.core.autonomy import _drive_gate_ok
    assert _drive_gate_ok(None) is False
    assert _drive_gate_ok({}) is False
    assert _drive_gate_ok({"activated": False, "approach": 0.9, "avoid": 0.0}) is False
    assert _drive_gate_ok({"activated": True, "approach": 0.9, "avoid": 0.0}) is True


def test_drive_gate_default_off():
    """T1.1：drive_gate 默认 False，生产行为保持不变(fail-closed)。"""
    from anima.core.config import AutonomyConfig
    assert isinstance(AutonomyConfig().drive_gate, bool)
    assert AutonomyConfig().drive_gate is False

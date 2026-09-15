"""会话连续性回归（R-continuity，2026-09-05 使用者视角体验体检）。

覆盖体检发现的三类真问题：
- P0-1 防抖合并不丢 @ 归属：@在前+补句在后不再整轮静默（mention/idle 误判）
- P1-2 唤醒窗内纠正/补充豁免每用户被动回复冷却（双闸门=词表∧窗活跃）
- P1-1 生成期间撤销 → 在途回复弃答；分段发送中撤销/纠正 → 停止剩余分段
"""
import asyncio
import time

import pytest

from anima.core.config import AnimaConfig
from anima.core.anima_core import AnimaCore
from anima.channels.onebot11 import parse_onebot_event
from anima.llm.handler import LLMHandler, _classify_send_interrupt
from anima.memory.chat_memory import reset_chat_memory
from anima.core.conversation_assembler import GroupDebouncer
from anima.llm.group_gate import (
    is_cancel_signal,
    is_conversation_continuation,
    is_correction_signal,
)

SELF_ID = 88888
GID = 20001
UID = 10001


@pytest.fixture(autouse=True)
def _isolate_chat_memory_singleton():
    """与 test_integration 相同：丢弃进程级 chat_memory 单例防跨测试污染。"""
    reset_chat_memory()
    yield
    reset_chat_memory()


def _grp_raw(text: str, at_self: bool, mid: int, uid: int = UID, gid: int = GID) -> dict:
    segs = ([{"type": "at", "data": {"qq": str(SELF_ID)}}] if at_self else []) + [
        {"type": "text", "data": {"text": text}},
    ]
    return {
        "post_type": "message", "message_type": "group", "message_id": mid,
        "user_id": uid, "group_id": gid,
        "raw_message": (f"[CQ:at,qq={SELF_ID}]" if at_self else "") + text,
        "message": segs,
        "sender": {"user_id": uid, "nickname": "小明", "role": "member"},
        "self_id": SELF_ID,
    }


def _last_user(messages) -> str:
    texts = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, list):
                texts.append(" ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)))
    return texts[-1] if texts else ""


class FakeLLM:
    """按匹配子串取 (延迟, 回复) 的桩；跨群事实抽取等后台调用快速空手返回。"""

    def __init__(self):
        self.model = "fake-model"
        self.script: dict[str, tuple[float, str]] = {}
        self.calls = 0

    async def chat(self, messages, **kw):
        return (await self._respond(messages, **kw))["content"]

    async def chat_with_tools(self, messages, **kw):
        return await self._respond(messages, **kw)

    async def chat_structured(self, messages, tools=None, **kw):
        return await self._respond(messages, tools=tools, **kw)

    async def _respond(self, messages, tools=None, **kw):
        self.calls += 1
        if tools and any("record_user_facts" in str(t) for t in tools or []):
            return {"content": "", "tool_calls": []}
        text = _last_user(messages)
        for pat, (delay, reply) in self.script.items():
            if pat in text:
                await asyncio.sleep(delay)
                return {"content": reply, "tool_calls": []}
        await asyncio.sleep(0.2)
        return {"content": "好哒，本喵想想喵～", "tool_calls": []}


class FakeChannel:
    """渠道入站缓冲桩：镜像 OneBot11Adapter._recent_inbound 的语义。"""

    def __init__(self):
        self._buf: dict[int, list[tuple[float, int, str]]] = {}

    def record_inbound(self, gid, uid, text):
        self._buf.setdefault(int(gid), []).append(
            (time.time(), int(uid), (text or "").strip()[:200]))

    def latest_inbound_since(self, gid, ts):
        items = [x for x in self._buf.get(int(gid), []) if x[0] > ts]
        return (items[-1][1], items[-1][2]) if items else None

    def inbounds_since(self, gid, ts):
        return [(u, x) for (t, u, x) in self._buf.get(int(gid), []) if t > ts]


class FakeGateway:
    def __init__(self, channel: FakeChannel):
        self.channel = channel
        self.sent = []

    async def on_event(self, event):
        return None

    async def send(self, msg):
        self.sent.append(msg)
        return len(self.sent)


def _make_core() -> AnimaCore:
    cfg = AnimaConfig.model_validate({
        "autonomy": {"sleep_enabled": False},
        # 快速防抖：保持合并语义但让测试不必真等 2~3s
        "debounce": {"group_enabled": True, "group_wait_sec": 0.3,
                     "group_max_wait_sec": 0.8},
    })
    core = AnimaCore(cfg)
    core.llm = FakeLLM()
    core.gateway = FakeGateway(FakeChannel())
    from anima.tools import create_default_registry
    core.tools = create_default_registry(core)
    return core


# ---------------------------------------------------------------------------
# 信号词表判定
# ---------------------------------------------------------------------------
def test_continuation_words():
    assert is_conversation_continuation("不对，我问的是历史题")
    assert is_conversation_continuation("还有啊，那2+2呢")
    assert is_conversation_continuation("等等，我是说历史的")
    # 只认句首引导词：句中出现的普通聊天不豁免（2026-09-06 收窄）
    assert not is_conversation_continuation("我感觉不对")
    assert not is_conversation_continuation("哈哈哈哈笑死我了")
    assert not is_conversation_continuation("")


def test_cancel_words():
    assert is_cancel_signal("算了不用了")
    assert is_cancel_signal("当我没说")
    assert not is_cancel_signal("帮我分析一下这道题")


def test_correction_words():
    assert is_correction_signal("等等，我说的是历史")
    assert is_correction_signal("你理解错了")
    assert not is_correction_signal("哈哈哈这个好笑")


# ---------------------------------------------------------------------------
# P0-1：合并不丢 @ 归属
# ---------------------------------------------------------------------------
def test_merge_keeps_attribution_both_orders():
    e_at = parse_onebot_event(_grp_raw("在吗", True, mid=1))
    e_plain = parse_onebot_event(_grp_raw("1+1等于几", False, mid=2))
    # @ 在前（群友最自然的连发习惯）——曾因继承最后一条而整轮静默
    assert GroupDebouncer._merge([e_at, e_plain])["mentions_self"] is True
    # @ 在后（原本就正常的顺序）不回归
    assert GroupDebouncer._merge([e_plain, e_at])["mentions_self"] is True
    assert GroupDebouncer._merge([e_at, e_plain])["reply_to_self"] is False


@pytest.mark.asyncio
async def test_at_then_followup_gets_reply():
    """体检 T1 场景：@在前+补句在后，合并后仍应得到回复。"""
    core = _make_core()
    h = LLMHandler(core)
    t1 = asyncio.create_task(
        h.handle(parse_onebot_event(_grp_raw("今天天气怎么样", True, mid=101))))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(
        h.handle(parse_onebot_event(_grp_raw("1+1等于几呀", False, mid=102))))
    await asyncio.gather(t1, t2)
    await asyncio.sleep(1.0)
    assert core.gateway.sent, "@+补句不应整轮静默"
    texts = "".join(
        "".join(sg.get("data", {}).get("text", "") for sg in m.message
                if isinstance(m.message, list) and isinstance(sg, dict)
                and sg.get("type") == "text")
        for m in core.gateway.sent)
    assert "好哒" in texts or "天气" in texts or "喵" in texts


# ---------------------------------------------------------------------------
# P1-2：唤醒窗内纠正豁免被动冷却
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_correction_after_reply_gets_second_reply():
    core = _make_core()
    h = LLMHandler(core)
    await h.handle(parse_onebot_event(_grp_raw("今天天气怎么样", True, mid=201)))
    first = len(core.gateway.sent)
    assert first >= 1
    await h.handle(parse_onebot_event(_grp_raw("不对，我问的是历史题", False, mid=202)))
    await asyncio.sleep(1.5)
    assert len(core.gateway.sent) > first, "唤醒窗内的纠正应豁免被动冷却并得到回复"


@pytest.mark.asyncio
async def test_plain_chatter_still_cooldown_skipped():
    core = _make_core()
    h = LLMHandler(core)
    await h.handle(parse_onebot_event(_grp_raw("今天天气怎么样", True, mid=301)))
    first = len(core.gateway.sent)
    assert first >= 1
    await h.handle(parse_onebot_event(_grp_raw("今天风好大呀", False, mid=302)))
    await asyncio.sleep(1.5)
    assert len(core.gateway.sent) == first, "非纠正的被动接话仍应被冷却跳过（不回归刷屏）"


# ---------------------------------------------------------------------------
# P1-1：生成期间撤销 → 弃答；分段中断分类
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_drops_pending_reply():
    core = _make_core()
    core.llm.script = {"帮我分析": (2.5, "这道题的解法如下喵：第一步……")}
    h = LLMHandler(core)
    ch = core.gateway.channel
    ev1 = parse_onebot_event(_grp_raw("帮我分析一下这道题", True, mid=401))
    ch.record_inbound(GID, UID, "帮我分析一下这道题")  # 渠道层在入站时已记录
    task = asyncio.create_task(h.handle(ev1))
    await asyncio.sleep(1.0)  # 已进入生成期（防抖≈0.45s + 管线）
    ch.record_inbound(GID, UID, "算了不用了")
    # 撤销消息本身走正常闸门（可能得到自己的新回复），但渠道缓冲已记录——
    # 弃答检查看的是缓冲。断言：生成期前的那个问题答案绝不再发出。
    await h.handle(parse_onebot_event(_grp_raw("算了不用了", False, mid=402)))
    await task
    await asyncio.sleep(0.5)
    texts = []
    for m in core.gateway.sent:
        if isinstance(m.message, list):
            texts.extend(sg.get("data", {}).get("text", "") for sg in m.message
                         if isinstance(sg, dict) and sg.get("type") == "text")
        else:
            texts.append(str(m.message))
    assert not any("解法如下" in t for t in texts), "生成期间用户撤销，原问题的在途回复应弃答"


class _Ch:
    def __init__(self, text):
        self._t = text

    def latest_inbound_since(self, gid, ts):
        return (999, self._t) if self._t is not None else None


def test_classify_interrupt_cancel():
    assert _classify_send_interrupt(_Ch("算了不用了"), 1, 0.0, user_id=999) == "cancel"


def test_classify_interrupt_correction():
    assert _classify_send_interrupt(_Ch("等等，我说的是历史"), 1, 0.0, user_id=999) == "correction"


def test_classify_interrupt_teasing():
    assert _classify_send_interrupt(_Ch("你心里话别说出来"), 1, 0.0, user_id=999) == "teasing"
    # 调侃保留任意群成员语义（对 bot 的社会性打断）
    assert _classify_send_interrupt(_Ch("你心里话别说出来"), 1, 0.0, user_id=777) == "teasing"


def test_classify_interrupt_cross_user_cancel_blocked():
    """复审 R8：跨用户不能取消别人正在接收的回复"""
    assert _classify_send_interrupt(_Ch("算了不用了"), 1, 0.0, user_id=777) is None
    assert _classify_send_interrupt(_Ch("不对，我说的是X"), 1, 0.0, user_id=777) is None
    # 未提供 user_id（旧调用兼容面）时同样拒绝，宁可不断不误断
    assert _classify_send_interrupt(_Ch("算了不用了"), 1, 0.0) is None


def test_classify_interrupt_none():
    assert _classify_send_interrupt(_Ch("哈哈这个好笑"), 1, 0.0, user_id=999) is None
    assert _classify_send_interrupt(_Ch(None), 1, 0.0, user_id=999) is None
    assert _classify_send_interrupt(None, 1, 0.0, user_id=999) is None


def test_continuation_exempt_rate_capped():
    """复审 R8：纠正豁免 60s 内每 (群,用户) 最多 3 次，防刷屏绕过"""
    core = _make_core()
    h = LLMHandler(core)
    for _ in range(3):
        assert h._continuation_exempt_ok(20001, 10001) is True
    assert h._continuation_exempt_ok(20001, 10001) is False
    # 其他用户/其他群不受影响
    assert h._continuation_exempt_ok(20001, 10002) is True
    assert h._continuation_exempt_ok(20002, 10001) is True

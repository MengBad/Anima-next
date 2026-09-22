"""群上下文「归属」标注（P0）与 CQ 传输码按类型枚举清洗（P1）。

生产事故（2026-09-21 15:00，定位见
`reports/2026-09-21_answer_mismatch_cross_user_context.md`）：
两条同群不同用户的消息相隔 4.1s 各自 @bot，因 `_turn_lock` / `_reply_inflight`
的 key 都含 user_id 而被**并发**处理，且回复要到最后一段（guardrail）才写回群缓冲。
后处理的回合拿到 messages 时，里面除了自己这条，还带着一条
「**别人**问 bot、且**尚未**被回复」的 user turn —— 两者同形（都 role=user、
都像在问 bot）、中间没有 assistant 隔断、也没有任何标记。

⇒ 模型被结构诱导，把别人的问题当成要回的那条：出站 reply 引用锚正确、正文却在
答上一条（「答非所问」）。

修法：
- 写入侧：`observe_group_message(..., addressed_to_bot=…)` 记录"这句是否直接对 bot 说"；
- 注入侧：`get_recent_group_context(..., annotate=True)`（**opt-in**）为「非本轮」条目
  加前缀标记，`role` 仍是 user/assistant（网关只认三种 role，且多轮交替结构不变）；
- 提示词侧：在既有 `## 话题纪律` 里说明「旁听」的含义（不新增行，省 token）。

⚠️ `annotate` 默认 False 是**硬约束**：`get_recent_group_context` 有 5 个消费者
（`pipeline/stages/memory.py:160`、`core/autonomy.py:1558/1598`、
`constitution/memory_engine.py:78`、`chat_memory.get_group_context:444`），
只有第 1 个需要标注，其余必须逐字不变。

P1 背景：`constants.CQ_TRANSPORT_RE` 的类型清单漏了 `reply`（`json/xml/forward/
node/file/markdown` 亦然），于是带引用回复的消息会把 `[CQ:reply,id=…]` 残留在
`ctx.text` 与短时群缓冲里，并原样写进向量记忆。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from anima.constants import CQ_TRANSPORT_RE
from anima.memory.chat_memory import ChatMemory
from anima.pipeline import PipelineContext

_ROOT = Path(__file__).resolve().parents[1]

_PENDING = "[旁听·他人提问·非本轮] "
_BYSTAND = "[旁听] "


def _code(rel: str) -> str:
    """剥注释后再断言 —— 解释性注释会把源码守卫变成假红/假绿。"""
    src = (_ROOT / rel).read_text(encoding="utf-8-sig")
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


@pytest.fixture()
def memory(tmp_path):
    return ChatMemory(persist_path=str(tmp_path / "chat.json"))


# ---------------------------------------------------------------------------
# P0-① 写入侧：记录"是否直接对 bot 说"
# ---------------------------------------------------------------------------

def test_observe_group_message_records_addressed_flag(memory):
    memory.observe_group_message(100, 1, "甲", "@哈基米 在吗", 1, addressed_to_bot=True)
    memory.observe_group_message(100, 2, "乙", "今天天气不错", 2)

    rows = memory._group_recent[100]
    assert rows[0]["to_bot"] is True
    assert rows[1]["to_bot"] is False


def test_addressed_to_bot_defaults_false(memory):
    """向后兼容：不传该参数时视为"没对 bot 说"，不得改变既有语义。"""
    memory.observe_group_message(100, 1, "甲", "随便说句", 1)
    assert memory._group_recent[100][0]["to_bot"] is False


# ---------------------------------------------------------------------------
# P0-② 注入侧：opt-in 归属标注
# ---------------------------------------------------------------------------

def test_annotate_marks_other_pending_question(memory):
    """他人直接对 bot 说、其后没有任何 assistant 回复 ⇒ 标为"非本轮"。"""
    memory.observe_group_message(100, 728536612, "哈基岸", "核灾呢", 11,
                                 addressed_to_bot=True)
    memory.observe_group_message(100, 2038678896, "Ashymars", "黑影改成奶位了", 12,
                                 addressed_to_bot=True)

    rows = memory.get_recent_group_context(100, exclude_message_id=12, annotate=True)

    assert [m["role"] for m in rows] == ["user"]
    assert rows[0]["content"].startswith(_PENDING)
    assert "核灾呢" in rows[0]["content"]


def test_annotate_leaves_already_answered_question_unmarked(memory):
    """已有 assistant 回复的旧提问是真实对话史，不应被标成"待答"。"""
    memory.observe_group_message(100, 1, "甲", "@哈基米 在吗", 1, addressed_to_bot=True)
    memory.observe_group_bot_reply(100, "在的喵")
    memory.observe_group_message(100, 2, "乙", "@哈基米 帮我查下", 2, addressed_to_bot=True)

    rows = memory.get_recent_group_context(100, exclude_message_id=2, annotate=True)

    assert [m["role"] for m in rows] == ["user", "assistant"]
    assert not rows[0]["content"].startswith(_PENDING)
    assert not rows[0]["content"].startswith(_BYSTAND)


def test_annotate_marks_plain_group_chatter_as_bystander(memory):
    """没对 bot 说的群友闲聊只标「旁听」（不是待答问题）。"""
    memory.observe_group_message(100, 1, "甲", "你们吃了吗", 1)
    memory.observe_group_message(100, 2, "乙", "@哈基米 在吗", 2, addressed_to_bot=True)

    rows = memory.get_recent_group_context(100, exclude_message_id=2, annotate=True)

    assert rows[0]["content"].startswith(_BYSTAND)
    assert "你们吃了吗" in rows[0]["content"]


def test_annotate_default_is_opt_in_and_byte_identical(memory):
    """🔴 硬约束：默认不标注 ⇒ 输出与旧实现逐字一致（其余 4 个消费者零影响）。"""
    memory.observe_group_message(100, 1, "甲", "核灾呢", 11, addressed_to_bot=True)
    memory.observe_group_message(100, 2, "乙", "@哈基米 在吗", 12, addressed_to_bot=True)

    default_rows = memory.get_recent_group_context(100, exclude_message_id=12)
    explicit_rows = memory.get_recent_group_context(100, exclude_message_id=12,
                                                    annotate=False)

    assert default_rows == explicit_rows
    for row in default_rows:
        assert not row["content"].startswith("[旁听")
        assert "旁听" not in row["content"]


def test_annotate_keeps_gateway_safe_roles(memory):
    """标注只改 content，不得引入第三种 role（网关只认 system/user/assistant）。"""
    memory.observe_group_message(100, 1, "甲", "核灾呢", 11, addressed_to_bot=True)
    memory.observe_group_bot_reply(100, "喵")
    memory.observe_group_message(100, 2, "乙", "黑影改成奶位了", 12, addressed_to_bot=True)

    rows = memory.get_recent_group_context(100, exclude_message_id=12, annotate=True)

    assert {m["role"] for m in rows} <= {"user", "assistant"}


# ---------------------------------------------------------------------------
# 可观测性：命中"他人待答"必须留痕
# ---------------------------------------------------------------------------

def _annotate(memory, exclude, **kw):
    return memory.get_recent_group_context(100, exclude_message_id=exclude,
                                           annotate=True, **kw)


def test_pending_annotation_is_logged(memory, caplog):
    """🔴 这是本修复**唯一**的运行时证据面。

    群缓冲不落盘 ⇒ 否则只能"等下一次同类事故"才知道标注是否真的生效。
    该日志只在命中**他人待答**时打，即事故条件本身（低频、高信号），
    同时也是观察 P2「并发窗口」是否被踩到的探针。
    """
    memory.observe_group_message(100, 1, "甲", "核灾呢", 11, addressed_to_bot=True)
    memory.observe_group_message(100, 2, "乙", "@哈基米 本轮", 12, addressed_to_bot=True)

    with caplog.at_level(logging.INFO, logger="anima.memory.chat_memory"):
        _annotate(memory, 12)

    assert "归属标注" in caplog.text, f"未留痕: {caplog.text!r}"


def test_bystander_only_does_not_log(memory, caplog):
    """纯闲聊旁听是常态，不得刷日志（否则 INFO 面被淹没）。"""
    memory.observe_group_message(100, 1, "甲", "你们吃了吗", 11)
    memory.observe_group_message(100, 2, "乙", "@哈基米 本轮", 12, addressed_to_bot=True)

    with caplog.at_level(logging.INFO, logger="anima.memory.chat_memory"):
        rows = _annotate(memory, 12)

    assert rows[0]["content"].startswith(_BYSTAND)
    assert "归属标注" not in caplog.text


def test_no_log_when_annotate_disabled(memory, caplog):
    """其余 4 个消费者不标注 ⇒ 不得因它们而打日志。"""
    memory.observe_group_message(100, 1, "甲", "核灾呢", 11, addressed_to_bot=True)

    with caplog.at_level(logging.INFO, logger="anima.memory.chat_memory"):
        memory.get_recent_group_context(100)

    assert "归属标注" not in caplog.text


# ---------------------------------------------------------------------------
# 端到端：走真实 memory_stage，组帧结果里不得有"裸的他人待答提问"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memory_stage_end_to_end_annotates_other_pending_question(tmp_path,
                                                                        monkeypatch):
    """批准计划 §5-6：**唯一**能挡住"未来重构把 `annotate=True` 丢掉"的行为级护栏
    —— 源码字符串守卫只能证明字面量还在，证明不了它真的生效。
    """
    import anima.memory.chat_memory as chat_memory_mod
    from anima.pipeline.stages import memory_stage

    memory = ChatMemory(persist_path=str(tmp_path / "chat.json"))
    monkeypatch.setattr(chat_memory_mod, "get_chat_memory", lambda: memory)

    # 别人先 @bot（其后不会有 assistant —— 正是并发窗口的形态）
    memory.observe_group_message(100, 728536612, "哈基岸",
                                 "[CQ:reply,id=1132969659] 核灾呢", 11,
                                 addressed_to_bot=True)
    # 本轮：另一位用户 @bot（在途，尚未回复）
    memory.observe_group_message(100, 2038678896, "Ashymars",
                                 "[CQ:at,qq=10001] 黑影下个版本改成奶位了", 12,
                                 addressed_to_bot=True)

    ctx = PipelineContext(
        event={"message_id": 12}, text="[CQ:at,qq=10001] 黑影下个版本改成奶位了",
        user_id=2038678896, group_id=100, detail_type="group",
        sender_name="Ashymars", meta={},
    )
    await memory_stage(ctx)

    rows = [m for m in ctx.messages if "核灾呢" in m["content"]]
    assert rows, "他人的待答提问根本没进上下文 ⇒ 本测试失去意义"
    assert all(m["content"].startswith(_PENDING) for m in rows), \
        [m["content"] for m in rows]
    # 非平凡性证明：同一条在"不标注"（= 修复前的形态）时确实是裸的。
    bare = memory.get_recent_group_context(100, exclude_message_id=12)
    assert any("核灾呢" in m["content"] and not m["content"].startswith("[旁听")
               for m in bare), "旧行为下就已带标记？那本断言无意义"
    # P1 端到端：注入内容不得残留 CQ 码
    assert all("CQ:" not in m["content"] for m in ctx.messages), \
        [m["content"] for m in ctx.messages if "CQ:" in m["content"]]
    assert {m["role"] for m in ctx.messages} <= {"user", "assistant"}


# ---------------------------------------------------------------------------
# P0 集成：并发第二回合里不再出现"裸的他人待答提问"
# ---------------------------------------------------------------------------

def test_concurrent_second_turn_has_no_bare_pending_question(memory):
    """复现 15:00 事故的上下文形态：修正前有裸 user turn，修正后被标注。

    这里断言的是**修复后的必要性**：若 `annotate=False`（旧行为）则裸 turn 存在，
    也就是模型看到的就是那个歧义结构 —— 反向证明标注确实改变了 messages。
    """
    # 甲先 @bot（其后不会被回复：回合甲的回复在回合乙组帧之后才产生）
    memory.observe_group_message(100, 728536612, "哈基岸",
                                 "[CQ:reply,id=1132969659] 核灾呢", 11,
                                 addressed_to_bot=True)
    # 乙随后 @bot，也就是"本轮"
    memory.observe_group_message(100, 2038678896, "Ashymars",
                                 "[CQ:at,qq=10001] 黑影下个版本改成奶位了", 12,
                                 addressed_to_bot=True)

    old = memory.get_recent_group_context(100, exclude_message_id=12)
    new = memory.get_recent_group_context(100, exclude_message_id=12, annotate=True)

    # 旧结构：一个没有任何标记的 "别人问 bot" 的 user turn
    assert len(old) == 1 and not old[0]["content"].startswith("[旁听")
    # 新结构：同一条被明确标注为"非本轮"
    assert len(new) == 1 and new[0]["content"].startswith(_PENDING)


# ---------------------------------------------------------------------------
# P1：CQ 传输码按类型枚举
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seg", [
    "image", "face", "bface", "sface", "record", "video", "poke", "share",
    "music", "location", "at",
    # ↓ 本轮的修复面：这些类型过去漏网
    "reply", "json", "xml", "forward", "node", "file", "markdown",
])
def test_cq_types_are_all_stripped(seg):
    payload = f"[CQ:{seg},id=1,x=2]"
    assert payload not in CQ_TRANSPORT_RE.sub("", payload)


def test_reply_cq_is_stripped_keeping_text():
    """回归本条事故的原始残留：`[CQ:reply,id=…] 核灾呢` 必须只剩正文。"""
    assert CQ_TRANSPORT_RE.sub("", "[CQ:reply,id=1132969659] 核灾呢").strip() == "核灾呢"


def test_group_buffer_content_is_cq_cleaned(memory):
    """短时群缓冲过去存的是**未清洗**的 raw ⇒ CQ 码会进上下文。"""
    memory.observe_group_message(100, 1, "甲", "[CQ:reply,id=9] 核灾呢", 1)

    rows = memory.get_recent_group_context(100)
    assert "CQ:" not in rows[0]["content"]
    assert "核灾呢" in rows[0]["content"]


# ---------------------------------------------------------------------------
# 源码级守卫：接线必须存在，且不得回退
# ---------------------------------------------------------------------------

def test_constants_regex_declares_every_required_type():
    pattern = CQ_TRANSPORT_RE.pattern
    for seg in ("reply", "json", "xml", "forward", "node", "file", "markdown"):
        assert re.search(r"(?:\||:)" + seg + r"(?:\||\]|\))", pattern), (
            f"CQ_TRANSPORT_RE 缺少类型 {seg}: {pattern!r}"
        )


def test_wiring_is_present_in_production_code():
    """接线守卫：写入侧带 to_bot、注入侧 opt-in、只有 memory_stage 传 True。"""
    handler = _code("anima/llm/handler.py")
    assert handler.count("addressed_to_bot=is_direct_group_call(") == 1, (
        "handler 群消息入库处必须用 is_direct_group_call 传 addressed_to_bot"
    )

    mem_stage = _code("anima/pipeline/stages/memory.py")
    assert mem_stage.count("annotate=True") == 1, (
        "只有 memory_stage 这一个消费者应开启标注"
    )

    chat = _code("anima/memory/chat_memory.py")
    assert chat.count("annotate: bool = False") == 1
    assert chat.count("addressed_to_bot: bool = False") == 1


def test_topic_discipline_mentions_bystander_and_keeps_single_line():
    """提示词侧：改的是**已有那一行**（不新增行，省预算），且说明「旁听」含义。"""
    cog = _code("anima/pipeline/stages/cognition.py")
    assert cog.count("只回答【当前这句话】") == 1, "不得新增重复的纪律行"
    assert "旁听" in cog, "话题纪律必须说明「旁听」标记的含义"

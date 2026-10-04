"""回避期回复契约：回避期内**一律静默**（2026-09-28 变更）。

## 变更背景（用户指令）

此前回避期的处理是两分支：

- 对方示意结束（转私聊 / 别说了 / 晚安…）→ 彻底沉默；
- 未示意结束 → 从 `COLD_RESPONSES` 补一句极短冷回（「本喵不想说话了。」等）。

生产实证（2026-09-28 全量原始日志审核，09-25→09-28）暴露该设计的问题：
进入回避期的用户此后**所有**消息都吃同一句冷脸，**不判当前消息是否友好** ——
产创作者发的宇智波斑长文、抖音视频分享、甚至「这里是新生群吗」全被冷回。
用户据此判定为「闹脾气」而非「不想接这个话题」。且 `[霸凌] 主人豁免
user=1562290139` ⇒ **owner 在群里看不到该现象**（幸存者偏差）。

用户指令（2026-09-28）：**「bot 遇到那种情况可以不回复」** ⇒ 回避期内不再发
冷回话术，直接 `SKIP_REPLY` 真静默。

## 「真静默」的判据（不是"少说一句"）

- `generation_stage` 返回 `SKIP_REPLY` 且 `ctx.reply is None`、`ctx.should_reply is False`；
- 不调用 LLM（`core.llm.chat` 未被调用）；
- 不产生 `fallback_*` 兜底出站计数（`COLD_RESPONSES` 池不再被回避路径消费）。

下游闭合（改前已逐一核实，无需信任）：
`pipeline/__init__.py:105-107`（SKIP_REPLY 只置标志、不 break）→
`handler.py:1060-1083`（`should_reply=False` **直接 return**：无出站、无兜底话术、
不计入 `unavailable`）→ `guardrail.py:472` 的贴纸兜底「喏，给你。」不可达
（`sticker_to_send` 唯一赋值点在 `generation.py:959`，位于 ReAct 之后）。
"""
from unittest.mock import MagicMock
import asyncio

from anima.cognitive import scar as scar_mod
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.generation import generation_stage


def _clear_scar_cache(data_dir):
    scar_mod._scar_systems.pop(data_dir, None)


def _make_core(data_dir):
    llm = MagicMock()
    llm.model = "test-model"
    llm.circuit_open_until = 0
    core = MagicMock()
    core.llm = llm
    core.config.persistence.data_dir = data_dir
    core.config.sticker.share_across_groups = False
    return core


def _make_scar(data_dir):
    _clear_scar_cache(data_dir)
    return scar_mod.ScarSystem(data_dir)


def _make_ctx(core, text):
    return PipelineContext(
        event={"reply_text": None},
        text=text,
        user_id=123,
        group_id=456,
        detail_type="group",
        sender_name="测试",
        meta={"core": core},
    )


def _enter_avoidance(data_dir):
    """让用户 123 占用 2 条伤痕进入 24h 回避期，并让单例缓存指向同一 system。"""
    scar = _make_scar(data_dir)
    scar.add(123, "滚", severity=0.6)
    scar.add(123, "你sb", severity=0.6)
    assert scar.is_avoiding(123), "前置：用户应处于回避期"
    scar_mod._scar_systems[data_dir] = scar
    return scar


# ── 1. 示意结束 → 静默（原有行为，保持）───────────────────────────────

async def test_avoid_end_signal_silence(tmp_path):
    data_dir = str(tmp_path / "s1")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "行了 转私聊了 别再说了")

    result = await generation_stage(ctx)
    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None
    assert core.llm.chat.called is False


# ── 2. 未示意结束 → **同样静默**（2026-09-28 反转）─────────────────────

async def test_avoid_no_end_signal_also_silent(tmp_path):
    """🔴 契约反转：回避期不再分「是否示意结束」，未示意结束也静默。

    旧契约（已被本测试取代）断言此处返回 `CONTINUE` + 一条 `<=30` 字的冷回。
    生产实证表明那正是一句冷脸被读到「闹脾气」的来源。
    """
    data_dir = str(tmp_path / "s2")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "随便说点什么别的")

    result = await generation_stage(ctx)
    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None, "回避期不得再补冷回话术"
    assert core.llm.chat.called is False


async def test_avoidance_friendly_message_also_silent(tmp_path):
    """生产实证里最刺眼的形态：完全友好的消息在回避期也吃冷脸。

    抖音视频分享 / 新人提问这类**零攻击性**内容，回避期内同样静默（不再冷回）。
    """
    data_dir = str(tmp_path / "s2b")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "https://v.douyin.com/xxxxx 这个好好笑")

    result = await generation_stage(ctx)
    assert result == StageResult.SKIP_REPLY
    assert ctx.reply is None


# ── 3. 计数口径：静默不是「兜底出站」───────────────────────────────

async def test_avoidance_silence_counts_no_fallback(tmp_path):
    """关避出口不再消耗 `COLD_RESPONSES` ⇒ `fallback_cold_avoidance`/`total` 均为 0。

    口径意义：`fallback_*` 统计的是"**有出站**但为固定话术"。静默没有出站，
    计入会把占比分母弄脏（与 `_resolve_bullying_reply` 里"沉默不计"同一原则）。
    """
    from anima.cognitive.reply_quality import ReplyQuality

    data_dir = str(tmp_path / "s3")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    rq = ReplyQuality(data_dir)
    core.reply_quality = rq
    ctx = _make_ctx(core, "随便聊聊")

    await generation_stage(ctx)

    rep = rq.report()
    assert rep["fallback_cold_avoidance"] == 0
    assert rep["fallback_total"] == 0


# ── 4. 反向对照：静默不得外溢到未回避用户 ─────────────────────────────

async def test_non_avoiding_user_not_silenced(tmp_path, monkeypatch):
    """未处于回避期的用户**不得**被静默。

    判据：把 `ReActLoop` 打桩成立即抛错 ⇒ 走 `react_error` 降级分支
    （`generation.py:848-853`）⇒ `CONTINUE` + 有正文。
    若该分支被误写进回避静默，这里会看到 `SKIP_REPLY` + 空正文。
    """
    data_dir = str(tmp_path / "s4")
    scar = _make_scar(data_dir)
    assert not scar.is_avoiding(123), "前置：本用例用户未处于回避期"
    scar_mod._scar_systems[data_dir] = scar

    class _FakeReAct:
        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            raise RuntimeError("打桩：已越过回避分支并进入 ReAct")

    monkeypatch.setattr("anima.agent.react.ReActLoop", _FakeReAct)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "今天天气不错")

    result = await generation_stage(ctx)

    assert result is not StageResult.SKIP_REPLY, "非回避用户不得被静默"
    assert result == StageResult.CONTINUE
    assert ctx.should_reply is True
    assert ctx.reply, "降级分支应给出可见正文（证明确实走到了 ReAct）"

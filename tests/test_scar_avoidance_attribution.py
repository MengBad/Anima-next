"""回避期静默的**归因**契约（2026-10-21）：保留静默，但私聊告诉 owner 原因。

## 背景（owner 拍板的方案 A）

`generation.py` 的伤痕回避分支（`is_avoiding` → `ctx.reply=None` +
`should_reply=False` → `SKIP_REPLY`）**没有 owner 豁免，且静默不可归因**。

owner 是自己最常直接对话的人、也是唯一会说狠话测试它的人 ⇒ 这条路径一触发就是
「私聊发消息完全无回音、无日志解释原因」。09-28 的注释已承认该盲区：
`[霸凌] 主人豁免 user=1562290139` ⇒ owner 在群里看不到该现象（幸存者偏差）。

方案是 **归因，不是豁免**：静默行为一字不改，只在静默时往
`ctx.meta["owner_notice"]` 塞一句说明，由 `handler.py:1242 _flush_owner_notice`
无条件私聊给 owner（复用既有通道：不新建通知函数、不新增配置键）。

## 本测试锁死的三条边界

1. owner（`permission_level >= 4`）在回避期 → `owner_notice` 被设置，
   **`should_reply` 仍为 False**、`reply` 仍为 None（静默未被改成开口）；
2. 非 owner 在回避期 → **没有** `owner_notice`，`should_reply` 仍为 False
   （不得把归因泄漏给普通人）；
3. 不在回避期 → 不触发（无 `owner_notice`）。

另锁死文案口径：数字/时间取自 `ScarSystem` 真实数据，不是写死的。
"""
from unittest.mock import MagicMock

import pytest

from anima.cognitive import scar as scar_mod
from anima.pipeline import PipelineContext, StageResult
from anima.pipeline.stages.generation import generation_stage

OWNER_LEVEL = 4
STRANGER_LEVEL = 2
USER_ID = 123


def _make_core(data_dir):
    llm = MagicMock()
    llm.model = "test-model"
    llm.circuit_open_until = 0
    core = MagicMock()
    core.llm = llm
    core.config.persistence.data_dir = data_dir
    core.config.sticker.share_across_groups = False
    return core


def _make_ctx(core, text, permission_level):
    return PipelineContext(
        event={"reply_text": None},
        text=text,
        user_id=USER_ID,
        group_id=456,
        detail_type="group",
        sender_name="主人",
        meta={"core": core, "permission_level": permission_level},
    )


def _enter_avoidance(data_dir):
    """让用户进入 24h 回避期（2 条未淡化伤痕），并让单例缓存指向同一 system。"""
    scar_mod._scar_systems.pop(data_dir, None)
    scar = scar_mod.ScarSystem(data_dir)
    scar.add(USER_ID, "滚", severity=0.6)
    scar.add(USER_ID, "你sb", severity=0.6)
    assert scar.is_avoiding(USER_ID), "前置：用户应处于回避期"
    scar_mod._scar_systems[data_dir] = scar
    return scar


# ── 1. owner 在回避期：静默照旧，但拿到归因 ─────────────────────────────


async def test_owner_in_avoidance_gets_attribution(tmp_path):
    data_dir = str(tmp_path / "owner")
    scar = _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "你怎么不理我", OWNER_LEVEL)

    result = await generation_stage(ctx)

    # 静默一字不改
    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None
    assert core.llm.chat.called is False

    notice = ctx.meta.get("owner_notice") or ""
    assert notice, "owner 在回避期必须拿到归因说明（否则仍是静默不可归因）"

    # 四项必备信息：① 回避期 ② 伤痕条数 ③ 到什么时候 ④ 不是故障
    assert "回避" in notice
    assert str(len(scar.get_for_user(USER_ID))) in notice, (
        f"文案没带真实伤痕数（应为 {len(scar.get_for_user(USER_ID))}）：{notice!r}"
    )
    import time as _t
    assert _t.strftime("%m-%d %H:%M", _t.localtime(scar.avoid_until_for(USER_ID))) in notice, (
        f"文案没带真实回避截止时间：{notice!r}"
    )
    assert "不是" in notice and ("坏" in notice or "故障" in notice)
    # 猫娘口吻
    assert "本喵" in notice


# ── 2. 非 owner 在回避期：静默，且不泄漏归因 ────────────────────────────


@pytest.mark.parametrize("level", [0, 1, 2, 3])
async def test_non_owner_in_avoidance_gets_no_attribution(tmp_path, level):
    data_dir = str(tmp_path / f"stranger{level}")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "随便说点什么", level)

    result = await generation_stage(ctx)

    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None
    assert not ctx.meta.get("owner_notice"), (
        "归因只给 owner，不得泄漏给普通用户"
    )


async def test_missing_permission_level_is_treated_as_non_owner(tmp_path):
    """`permission_level` 缺省（旧 ctx / 早期 stage 早退）≠ owner（fail-closed）。"""
    data_dir = str(tmp_path / "nolevel")
    _enter_avoidance(data_dir)

    core = _make_core(data_dir)
    ctx = PipelineContext(
        event={"reply_text": None},
        text="在吗",
        user_id=USER_ID,
        group_id=456,
        detail_type="group",
        sender_name="测试",
        meta={"core": core},  # 无 permission_level
    )

    result = await generation_stage(ctx)

    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert not ctx.meta.get("owner_notice")


# ── 3. 不在回避期：不触发 ───────────────────────────────────────────────


async def test_owner_not_avoiding_is_not_attributed(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "ok")
    scar_mod._scar_systems.pop(data_dir, None)
    scar = scar_mod.ScarSystem(data_dir)
    assert not scar.is_avoiding(USER_ID), "前置：用户未处于回避期"
    scar_mod._scar_systems[data_dir] = scar

    class _FakeReAct:
        def __init__(self, *a, **k):
            pass

        async def run(self, *a, **k):
            raise RuntimeError("打桩：已越过回避分支并进入 ReAct")

    monkeypatch.setattr("anima.agent.react.ReActLoop", _FakeReAct)

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "今天天气不错", OWNER_LEVEL)

    result = await generation_stage(ctx)

    assert result is not StageResult.SKIP_REPLY, "未回避的 owner 不得被静默"
    assert not ctx.meta.get("owner_notice"), "未回避时不得产生归因通知"


# ── 4. 静默不可被归因逻辑破坏（fail-soft）────────────────────────────────


async def test_attribution_failure_keeps_silence(tmp_path, monkeypatch):
    """文案生成抛错 ⇒ 仍然静默，绝不因归因失败而开口。"""
    data_dir = str(tmp_path / "failsoft")
    _enter_avoidance(data_dir)

    monkeypatch.setattr(
        scar_mod.ScarSystem,
        "get_for_user",
        lambda self, uid: (_ for _ in ()).throw(RuntimeError("打桩：归因失败")),
        raising=False,
    )

    core = _make_core(data_dir)
    ctx = _make_ctx(core, "在吗", OWNER_LEVEL)

    result = await generation_stage(ctx)

    assert result == StageResult.SKIP_REPLY
    assert ctx.should_reply is False
    assert ctx.reply is None
    assert core.llm.chat.called is False

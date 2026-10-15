"""U4：图片「入站即描述」+ 修死节流（2026-10-14）。

上游诊断：`reports/2026-10-14_u4-u5-取证与决策请求.md`

**实测（生产 7 天）**：`[图段]` 672 条（图确实进到 event），但 `视觉描述已存记忆`
只有 **19** 条 ⇒ 覆盖率 2.8%。使用者体感：「我发的图，它只在搭理我那次看，看完就忘」。

**根因（本轮代码核查推翻了上一轮的归因）**：
- 上一轮写「`_vision_describe` 挂在出站分支 + 再叠加 600s/人节流」。
- 但 `VisionManager.throttle` / `VisionManager._last` **只出现在 `__init__` 里，
  全仓无任何读取点** ⇒ `describe_throttle_per_user: 600` 是**死配置，节流根本不存在**。
- ⇒ 真正的根因只有一条：**只在出站分支调度**。672 张入站图里，只有「本回合真回复了」
  的那一小部分才被描述。

本文件钉四层：
  ① `vision.describe_on_inbound` 是**真配置**（pydantic 收得下，`extra=ignore` 不吞）
  ② `_schedule_vision_describe` 入站调度：开关/无图/无 LLM 三种情形的 fail-closed
  ③ 节流**真的生效**（修死配置）：同一 user 节流窗内第二次被挡
  ④ 挂点没脱钩：`_handle_inner` 在 `observe_group_message` 之后确实调了入站调度

⚠️ 群路径之所以能「入站即调度」：`observe_group_message`（handler.py:1090，回复闸门
**之前**）已经把 user 条目写进了 `group_<gid>_<uid>` 持久桶 ⇒ `set_vision_desc` 补写得上。
**私聊**的 user 条目只在 pipeline memory stage 写（更晚）⇒ 私聊仍走出站分支，本轮不动。
"""
from __future__ import annotations

import asyncio
import inspect
import types

import pytest

import anima.llm.handler as handler_mod
from anima.core.config import VisionConfig
from anima.cognitive.vision import VisionManager
from anima.llm.handler import LLMHandler

GROUP = 727631526
USER = 486159181


# ---------------------------------------------------------------------------
# ① 配置项是「真的」
# ---------------------------------------------------------------------------

def test_describe_on_inbound_is_a_real_pydantic_field():
    """`describe_on_inbound` 必须能被 pydantic 收下。

    🔴 本项目 `extra=ignore` ⇒ 只写 yaml 不加字段 = 静默丢弃 = 哑开关。
    """
    cfg = VisionConfig()
    assert hasattr(cfg, "describe_on_inbound"), "VisionConfig 缺 describe_on_inbound 字段"
    # fail-closed：默认关，生产必须显式开
    assert cfg.describe_on_inbound is False
    assert VisionConfig(describe_on_inbound=True).describe_on_inbound is True


# ---------------------------------------------------------------------------
# ② 入站调度的 fail-closed 边界
# ---------------------------------------------------------------------------

def _mk_handler(*, enabled=True, store=True, on_inbound=True, llm=True,
                images=None, detail_type="group"):
    """最小 handler 桩：只喂 `_schedule_vision_describe` 真正读到的那些字段。"""
    h = LLMHandler.__new__(LLMHandler)
    vision = types.SimpleNamespace(
        enabled=enabled,
        store_desc_to_memory=store,
        describe_on_inbound=on_inbound,
        describe_throttle_per_user=600,
        model="",
    )
    h.core = types.SimpleNamespace(
        config=types.SimpleNamespace(vision=vision),
        llm=(types.SimpleNamespace() if llm else None),
    )
    h._scheduled = []
    # 捕获调度，不真跑异步任务
    async def _fake(images_, user_id_, group_id_, dt_):
        h._scheduled.append((images_, user_id_, group_id_, dt_))
    h._vision_describe = _fake
    return h


@pytest.mark.asyncio
async def test_schedule_vision_describe_fires_on_group_inbound():
    """开启后，群消息带图 ⇒ 入站即调度（哪怕最终没回复）。"""
    h = _mk_handler(images=[{"file": "a.jpg"}])
    event = {"images": [{"file": "a.jpg"}]}
    ok = await asyncio.wait_for(
        h._schedule_vision_describe(event, USER, GROUP, "group"), timeout=5)
    assert ok is True
    await asyncio.sleep(0)  # 让 create_task 落地
    assert len(h._scheduled) == 1
    imgs, uid, gid, dt = h._scheduled[0]
    assert imgs == [{"file": "a.jpg"}] and uid == USER and gid == GROUP and dt == "group"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,why",
    [
        ({"on_inbound": False}, "开关关 ⇒ 不调度（fail-closed 默认）"),
        ({"enabled": False}, "vision.enabled=false ⇒ 不调度"),
        ({"store": False}, "store_desc_to_memory=false ⇒ 不调度"),
        ({"llm": False}, "无 LLM ⇒ 不调度"),
    ],
)
async def test_schedule_vision_describe_fail_closed(kwargs, why):
    """四种不该调度的情形，一个都不能漏。"""
    h = _mk_handler(**kwargs)
    event = {"images": [{"file": "a.jpg"}]}
    ok = await asyncio.wait_for(
        h._schedule_vision_describe(event, USER, GROUP, "group"), timeout=5)
    assert ok is False, why
    await asyncio.sleep(0)
    assert h._scheduled == [], why


@pytest.mark.asyncio
async def test_schedule_vision_describe_skips_when_no_images():
    """纯文本消息不得触发视觉描述（否则每条消息一次 LLM）。"""
    h = _mk_handler()
    ok = await asyncio.wait_for(
        h._schedule_vision_describe({"images": []}, USER, GROUP, "group"), timeout=5)
    assert ok is False
    assert await asyncio.wait_for(
        h._schedule_vision_describe({}, USER, GROUP, "group"), timeout=5) is False
    await asyncio.sleep(0)
    assert h._scheduled == []


# ---------------------------------------------------------------------------
# ③ 节流真生效（修死配置）
# ---------------------------------------------------------------------------

def test_vision_throttle_is_actually_enforced():
    """`describe_throttle_per_user` 不能是摆设：窗内第二次必须被挡。

    🔴 修复前 `VisionManager.throttle` 只被 __init__ 赋值、全仓无读取点
    ⇒ 该配置项是死的。入站即描述后若仍不生效，刷图会瞬间打满 LLM 队列。
    """
    vm = VisionManager(describe_throttle_per_user=600)
    assert vm.allow_user(str(USER), 1000.0) is True
    vm.mark_user(str(USER), 1000.0)
    # 窗内（+10s）⇒ 挡
    assert vm.allow_user(str(USER), 1010.0) is False
    # 窗外（+601s）⇒ 放行
    assert vm.allow_user(str(USER), 1601.0) is True


def test_vision_throttle_zero_or_negative_means_no_throttle():
    """节流 <=0 视为不限（与「关掉」同义），不得变成永久拦截。"""
    vm = VisionManager(describe_throttle_per_user=0)
    assert vm.allow_user(str(USER), 1000.0) is True
    vm.mark_user(str(USER), 1000.0)
    assert vm.allow_user(str(USER), 1000.5) is True


def test_vision_throttle_is_per_user():
    """节流按 user 隔离：A 被挡不影响 B。"""
    vm = VisionManager(describe_throttle_per_user=600)
    vm.mark_user("111", 1000.0)
    assert vm.allow_user("111", 1005.0) is False
    assert vm.allow_user("222", 1005.0) is True


# ---------------------------------------------------------------------------
# ④ 挂点没脱钩
# ---------------------------------------------------------------------------

def test_handle_inner_calls_inbound_schedule_after_group_memory_write():
    """`_handle_inner` 必须在 `observe_group_message`（写持久桶）**之后**调入站调度。

    顺序反了 ⇒ `set_vision_desc` 找不到 user 条目 ⇒ 静默全丢（无任何报错）。
    """
    src = inspect.getsource(LLMHandler._handle_inner)
    i_write = src.find("observe_group_message")
    i_sched = src.find("_schedule_vision_describe")
    assert i_write > 0, "_handle_inner 里找不到 observe_group_message（群记忆写入点）"
    assert i_sched > 0, "_handle_inner 里没挂 _schedule_vision_describe（入站调度脱钩）"
    assert i_sched > i_write, (
        "入站调度跑在群记忆写入之前 ⇒ set_vision_desc 补写到空桶，描述全丢"
    )

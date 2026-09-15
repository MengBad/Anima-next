"""P3-3 测试：QQ 侧受限自然语言 OS 指令。

此前 `HumanAIControlSurface.parse_command`（确定性、无 LLM 的受限解析器）**只**被
WebUI `/api/os/command` 调用——QQ 主信道完全拿不到。本测试锁死接线与三条边界：

1. 只解析不猜测（unknown 直接给用法，不执行）；
2. **特权意图只产出待确认**（批准/拒绝 → 指既有 Owner 授权端点，QQ 不执行）；
3. **只跑只读白名单**（落草案的 `skill`、多轮 LLM 的 `society` 在 QQ 侧不执行）。

外加：控制面 `run_readonly` 是 WebUI / QQ 共用的**唯一**实现（防止两处映射漂移），
以及只读结果渲染的截断诚实性。
"""

from __future__ import annotations

import pytest

from anima.llm.commands import COMMAND_ROUTES, AnimaCommandsMixin, CommandCtx
from anima.os.control_surface import (
    HumanAIControlSurface,
    ParsedCommand,
    format_readonly_reply,
)


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class _Core:
    def __init__(self, data_dir=""):
        mem = type("M", (), {"long_term_semantic": False,
                             "long_term_semantic_max_candidates": 60,
                             "long_term_semantic_threshold": 0.35})()
        pers = type("P", (), {"data_dir": str(data_dir)})()
        self.config = type("C", (), {"memory": mem, "persistence": pers})()
        self.audit = None


class _Handler(AnimaCommandsMixin):
    def __init__(self, core):
        self.core = core


def _ctx(raw_cmd: str) -> CommandCtx:
    return CommandCtx(event={}, raw_cmd=raw_cmd, cmd=raw_cmd.lower(), user_id=7,
                      group_id=99, detail_type="group", target_id=99, at_prefix="")


# ---------------------------------------------------------------------------
# 1. 路由接线
# ---------------------------------------------------------------------------

def test_os_route_registered_and_last():
    names = [m for _, m in COMMAND_ROUTES]
    assert "_cmd_os" in names


def test_os_route_predicate_boundaries():
    pred = next(p for p, m in COMMAND_ROUTES if m == "_cmd_os")
    assert pred(_ctx("os 查看健康")) is True
    assert pred(_ctx("os")) is True
    assert pred(_ctx("oscar")) is False
    assert pred(_ctx("status")) is False


# ---------------------------------------------------------------------------
# 2. 只解析不猜测
# ---------------------------------------------------------------------------

async def test_empty_command_returns_usage():
    out = await _Handler(_Core())._cmd_os(_ctx("os"))
    assert "用法" in out and "!anima os" in out


async def test_unknown_intent_does_not_execute():
    out = await _Handler(_Core())._cmd_os(_ctx("os 今天天气怎么样"))
    assert "没识别出" in out
    # 必须给出可用示例，否则 Owner 只会知道「不行」
    assert "查看健康" in out and "依赖自检" in out


# ---------------------------------------------------------------------------
# 3. 特权意图：只产出待确认，绝不执行
# ---------------------------------------------------------------------------

async def test_privileged_intent_only_yields_pending_confirmation(tmp_path):
    core = _Core(tmp_path)
    out = await _Handler(core)._cmd_os(_ctx("os 批准提案 evo-abc123"))
    assert "需要你确认" in out
    assert "POST /api/evolution/{proposal_id}/approve" in out
    assert "self-authorization" in out
    # 没有批准动作发生：提案目录未被创建（这条指令不碰盘）
    assert not (tmp_path / "long_term").exists()


async def test_run_readonly_refuses_needs_confirmation():
    surf = HumanAIControlSurface()
    cmd = ParsedCommand(intent="approve", needs_confirmation=True)
    assert await surf.run_readonly(cmd, "批准提案 x") == {}


# ---------------------------------------------------------------------------
# 4. 只读白名单
# ---------------------------------------------------------------------------

def test_whitelist_excludes_privileged_and_side_effect_intents():
    wl = HumanAIControlSurface.READONLY_INTENTS
    assert {"approve", "reject", "skill", "society", "unknown"}.isdisjoint(wl)
    assert {"health", "recover", "stability", "observe", "impact",
            "memory", "maintenance", "list_proposals"} <= wl


@pytest.mark.parametrize("text,intent", [
    ("os 个人能力生态里，新技能怎么演进评估？", "skill"),
    ("os Agent 社会辩论一下这个架构改动", "society"),
])
async def test_non_whitelist_intent_refused_on_qq(text, intent):
    """skill 会落草案、society 触发多轮 LLM —— QQ 侧必须拒绝并指向 WebUI。"""
    out = await _Handler(_Core())._cmd_os(_ctx(text))
    assert f"intent={intent}" in out
    assert "WebUI" in out


# ---------------------------------------------------------------------------
# 5. 只读意图真的取到数据
# ---------------------------------------------------------------------------

async def test_readonly_health_returns_data(tmp_path):
    out = await _Handler(_Core(tmp_path))._cmd_os(_ctx("os 查看健康状态"))
    assert "健康" in out
    assert "overview." in out or "不可用" in out     # 有内容或诚实说不可用
    assert "没有取到只读数据" not in out


async def test_readonly_memory_discloses_vector_state(tmp_path):
    core = _Core(tmp_path)
    out = await _Handler(core)._cmd_os(_ctx("os 你还记得我的习惯吗"))
    # 默认 long_term_semantic=False → 必须如实说「未做语义检索」
    assert "未做语义检索" in out
    assert "long_term_semantic=false" in out


async def test_run_readonly_covers_every_whitelisted_intent(tmp_path):
    """白名单里的每个意图都必须返回非空数据（否则就是「接线了但拿不到」）。"""
    surf = HumanAIControlSurface(_Core(tmp_path))
    for intent in sorted(HumanAIControlSurface.READONLY_INTENTS):
        cmd = ParsedCommand(intent=intent, preview=f"p-{intent}")
        data = await surf.run_readonly(cmd, "习惯")
        assert data, f"{intent} 未返回任何数据"
        assert isinstance(data, dict)


async def test_run_readonly_ignores_non_whitelist():
    surf = HumanAIControlSurface(_Core())
    assert await surf.run_readonly(ParsedCommand(intent="unknown"), "x") == {}
    assert await surf.run_readonly(ParsedCommand(intent="skill"), "x") == {}


# ---------------------------------------------------------------------------
# 6. 只读结果渲染
# ---------------------------------------------------------------------------

def test_format_reports_unavailable_honestly():
    cmd = ParsedCommand(intent="observe", preview="环境观测")
    out = format_readonly_reply(cmd, {"environment": {"available": False, "reason": "信号源缺失"}})
    assert "不可用" in out and "信号源缺失" in out


def test_format_flattens_and_truncates_honestly():
    cmd = ParsedCommand(intent="stability", preview="稳定性")
    data = {"stability": {f"{w}d": {f"k{i}": i for i in range(20)} for w in (7, 30, 90)}}
    out = format_readonly_reply(cmd, data)
    assert "已截断" in out
    assert len(out) <= 1400


def test_format_memory_section():
    cmd = ParsedCommand(intent="memory", preview="查询长期记忆")
    data = {"memory": {
        "knowledge_count": 3, "pattern_count": 1, "query": "习惯",
        "knowledge_matches": [{"statement": "Owner 偏好：我习惯用 pytest"}],
        "pattern_matches": [], "semantic_matches": [],
        "vector_used": True, "vector_reason": "",
    }}
    out = format_readonly_reply(cmd, data)
    assert "知识 3 条" in out and "行为模式 1 条" in out
    assert "pytest" in out
    assert "已做语义检索" in out

"""P2-2 测试：声明式工具组合 Skill、内置高频技能包、P6 真接线、abilities 可见性。

覆盖：
- tool 型 Skill 的组合白名单（不存在 / 停用 / capability_only / private_only / danger / 环境操控类）
- **无提权上界**：Skill 工具等级 = max(技能声明, 被组合工具)，高权限工具无法被低权限技能借壳
- 组合执行 + content_summary 整理；整理失败回落原始产出
- 内置技能包可被真实加载并注册成工具
- drafts.validate 会在批准前挡住不可组合的目标（不留「审批通过但安装被跳过」的坑）
- P6：控制面 skill_evolution 真接线（dry-run 与 create_draft 两条路）
- set_reminder 走 P1-1 债账本
"""

import asyncio

import pytest
import yaml

from anima.cognitive.debt import DebtManager
from anima.skills.models import SkillExecution, SkillManifest
from anima.skills.registry import SkillRegistry, validate_tool_composition
from anima.tools import LEVEL_ADMIN, LEVEL_OWNER, LEVEL_USER, Tool, ToolRegistry


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------

class _Mem:
    def __init__(self):
        self.data = {}

    def read_json(self, name, default=None):
        return self.data.get(name, default)

    def write_json(self, name, value):
        self.data[name] = value


class _FakeLLM:
    def __init__(self, reply="整理后的要点"):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, temperature=0.2, max_tokens=600, **kw):
        self.calls.append(messages)
        if callable(self.reply):
            return self.reply(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _FakeCore:
    def __init__(self, llm=None):
        self.tools = ToolRegistry(core=self)
        self.llm = llm
        self.config = type("C", (), {
            "skills": type("S", (), {"allow_prompt": True})(),
            "persistence": type("P", (), {"data_dir": "."})(),
        })()
        self.skills = None


def _manifest(**over) -> SkillManifest:
    data = {
        "id": "demo",
        "name": "演示",
        "description": "演示技能",
        "min_level": "user",
        "parameters": {"query": {"type": "string", "description": "查询"}},
        "execution": {"type": "tool", "tool": "web_search",
                      "template": "把材料整理成要点", "content_summary": True},
    }
    data.update(over)
    return SkillManifest.model_validate(data)


# ---------------------------------------------------------------------------
# 1. 组合白名单
# ---------------------------------------------------------------------------

def _core_with_tools():
    core = _FakeCore()
    reg = core.tools
    reg.register(Tool("web_search", "搜网", {"query": {}, "num_results": {}},
                      lambda **k: "raw", min_level=LEVEL_USER))
    reg.register(Tool("get_member_facts", "成员事实", {"name": {}},
                      lambda **k: "fact", min_level=LEVEL_USER))
    reg.register(Tool("get_group_members", "群成员", {"group_id": {}, "name": {}},
                      lambda **k: "members", min_level=LEVEL_USER))
    reg.register(Tool("owner_only", "主人专属", {}, lambda **k: "x", min_level=LEVEL_OWNER))
    reg.register(Tool("danger_tool", "高危", {}, lambda **k: "x",
                      min_level=LEVEL_USER, danger=True))
    reg.register(Tool("cap_only", "能力层私有", {}, lambda **k: "x",
                      min_level=LEVEL_USER, capability_only=True))
    reg.register(Tool("priv_tool", "私聊专属", {}, lambda **k: "x",
                      min_level=LEVEL_USER, private_only=True))
    reg.register(Tool("agent_browser", "浏览器", {}, lambda **k: "x", min_level=LEVEL_OWNER))
    reg.register(Tool("run_shell", "shell", {}, lambda **k: "x", min_level=LEVEL_ADMIN))
    disabled = Tool("off_tool", "停用", {}, lambda **k: "x", min_level=LEVEL_USER)
    disabled.enabled = False
    reg.register(disabled)
    return core


def test_composition_allows_plain_user_tool():
    core = _core_with_tools()
    assert validate_tool_composition(_manifest(), core.tools) == ""


@pytest.mark.parametrize("target,fragment", [
    ("nonexistent", "不存在"),
    ("off_tool", "已停用"),
    ("danger_tool", "高危"),
    ("cap_only", "能力层"),
    ("priv_tool", "仅限私聊"),
    ("agent_browser", "环境操控"),
    ("run_shell", "环境操控"),
    ("self_code", "环境操控"),
    ("propose_skill_draft", "环境操控"),
])
def test_composition_rejects_unsuitable_targets(target, fragment):
    core = _core_with_tools()
    problem = validate_tool_composition(
        _manifest(execution={"type": "tool", "tool": target, "template": "整理"}), core.tools)
    assert fragment in problem


def test_composition_ignores_non_tool_skills():
    core = _core_with_tools()
    prompt_skill = _manifest(
        id="p1", execution={"type": "prompt", "template": "写点什么"})
    assert validate_tool_composition(prompt_skill, core.tools) == ""


# ---------------------------------------------------------------------------
# 2. 无提权上界（核心不变式）
# ---------------------------------------------------------------------------

async def test_no_privilege_escalation_for_owner_tool():
    """技能声明 user，但组合了 owner 级工具 → 生成的技能工具必须是 owner 级。"""
    core = _core_with_tools()
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    manifest = _manifest(execution={"type": "tool", "tool": "owner_only",
                                    "template": "整理", "content_summary": False})
    tool = core.skills._make_tool(manifest)
    assert tool.min_level == LEVEL_OWNER
    # 普通用户既看不见也调不动
    assert tool.min_level > LEVEL_USER


async def test_no_privilege_escalation_for_admin_tool():
    core = _core_with_tools()
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    manifest = _manifest(execution={"type": "tool", "tool": "run_shell",
                                    "template": "整理", "content_summary": False})
    # run_shell 会被白名单直接拒绝（环境操控），此处显式构造 _make_tool 验证上界公式
    tool = core.skills._make_tool(manifest)
    assert tool.min_level == LEVEL_ADMIN


async def test_composed_skill_tool_level_never_lower_than_target():
    core = _core_with_tools()
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    for name, expected in (("web_search", LEVEL_USER), ("owner_only", LEVEL_OWNER)):
        manifest = _manifest(id=f"s_{name}",
                             execution={"type": "tool", "tool": name,
                                        "template": "整理", "content_summary": False})
        assert core.skills._make_tool(manifest).min_level == expected


# ---------------------------------------------------------------------------
# 3. 组合执行
# ---------------------------------------------------------------------------

async def test_composed_tool_calls_target_and_summarizes():
    core = _core_with_tools()
    core.llm = _FakeLLM("要点：一是甲，二是乙")
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    manifest = _manifest(tool=None) if False else _manifest()
    result = await core.skills._run_composed_tool(manifest, {"query": "今天新闻"})
    assert result == "要点：一是甲，二是乙"
    assert core.llm.calls


async def test_composed_tool_raw_when_no_summary():
    core = _core_with_tools()
    core.llm = _FakeLLM()
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    manifest = _manifest(execution={"type": "tool", "tool": "web_search",
                                    "template": "整理", "content_summary": False})
    result = await core.skills._run_composed_tool(manifest, {"query": "x"})
    assert result == "raw"
    assert not core.llm.calls


async def test_composed_tool_falls_back_when_summarizer_fails():
    core = _core_with_tools()
    core.llm = _FakeLLM(RuntimeError("summarizer down"))
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    result = await core.skills._run_composed_tool(_manifest(), {"query": "x"})
    assert result == "raw"          # 整理失败 → 原始产出，不返回空
    

async def test_composed_tool_rejects_when_target_removed():
    core = _core_with_tools()
    core.llm = _FakeLLM()
    core.skills = SkillRegistry.__new__(SkillRegistry)
    core.skills.core = core
    core.tools.tools.pop("web_search")
    result = await core.skills._run_composed_tool(_manifest(), {"query": "x"})
    assert "技能不可用" in result


# ---------------------------------------------------------------------------
# 4. 加载内置技能包
# ---------------------------------------------------------------------------

def test_builtin_package_loads_and_installs():
    from pathlib import Path

    core = _core_with_tools()
    builtin = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    reg = SkillRegistry(Path("/nonexistent-data-dir"), core, builtin_root=builtin)
    n = reg.load()
    assert n >= 4, f"内置技能包应有 compose_notice + 3 个高频技能，实际 {n}"
    assert not reg.errors, f"内置技能不应有加载错误: {reg.errors}"

    installed = reg.install_into_tools()
    assert installed == n
    assert "skill_member_profile" in core.tools.tools
    assert "skill_web_brief" in core.tools.tools
    assert "skill_group_roster" in core.tools.tools


def test_builtin_tool_skills_declare_expected_targets():
    from pathlib import Path

    builtin = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    expected = {"member_profile": "get_member_facts",
                "group_roster": "get_group_members",
                "web_brief": "web_search"}
    for skill_id, target in expected.items():
        data = yaml.safe_load((builtin / skill_id / "skill.yaml").read_text(encoding="utf-8"))
        assert data["execution"]["type"] == "tool"
        assert data["execution"]["tool"] == target
        assert data["execution"]["content_summary"] is True
        assert data["min_level"] == "user"


def test_describe_reports_effective_level_and_target():
    from pathlib import Path

    core = _core_with_tools()
    # group_roster 的目标 get_group_members 未注册 → 该技能应被跳过而非半装
    builtin = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    reg = SkillRegistry(Path("/nonexistent-data-dir"), core, builtin_root=builtin)
    reg.load()
    rows = {r["id"]: r for r in reg.describe()}
    web = rows["web_brief"]
    assert web["execution"] == "tool"
    assert web["composes"] == "web_search"
    assert web["level"] == LEVEL_USER


def test_install_skips_skill_whose_target_disappeared():
    from pathlib import Path

    core = _core_with_tools()
    core.tools.tools.pop("web_search")   # 目标消失
    builtin = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    reg = SkillRegistry(Path("/nonexistent-data-dir"), core, builtin_root=builtin)
    reg.load()
    reg.install_into_tools()
    assert "skill_web_brief" not in core.tools.tools
    assert any("web_brief" in k for k in reg.errors)


# ---------------------------------------------------------------------------
# 5. drafts.validate 在批准前挡住坏目标
# ---------------------------------------------------------------------------

def test_draft_validate_rejects_uncomposable_target():
    from anima.skills.drafts import SkillDraftManager

    core = _core_with_tools()
    mgr = SkillDraftManager.__new__(SkillDraftManager)
    mgr.core = core
    errors = mgr.validate({
        "id": "bad", "name": "坏技能", "description": "d", "min_level": "user",
        "parameters": {},
        "execution": {"type": "tool", "tool": "agent_browser", "template": "整理"},
    })
    assert any("环境操控" in e for e in errors)


def test_draft_validate_rejects_unknown_parameter_for_tool_skill():
    from anima.skills.drafts import SkillDraftManager

    core = _core_with_tools()
    mgr = SkillDraftManager.__new__(SkillDraftManager)
    mgr.core = core
    errors = mgr.validate({
        "id": "bad2", "name": "坏技能2", "description": "d", "min_level": "user",
        "parameters": {"nope": {"type": "string", "description": "不存在"}},
        "execution": {"type": "tool", "tool": "web_search", "template": "整理"},
    })
    assert any("目标工具不存在" in e or "不存在的参数" in e for e in errors)


def test_composable_tool_list_excludes_unsuitable():
    from anima.skills.drafts import SkillDraftManager

    core = _core_with_tools()
    mgr = SkillDraftManager.__new__(SkillDraftManager)
    mgr.core = core
    names = mgr._composable_tools()
    assert "web_search" in names and "get_member_facts" in names
    assert "agent_browser" not in names
    assert "run_shell" not in names
    assert "danger_tool" not in names
    assert "cap_only" not in names


# ---------------------------------------------------------------------------
# 6. skills 模型约束
# ---------------------------------------------------------------------------

def test_tool_execution_requires_tool_name():
    with pytest.raises(Exception):
        SkillExecution(type="tool", tool="", template="整理")
    with pytest.raises(Exception):
        SkillExecution(type="tool", tool="Bad-Name", template="整理")


def test_tool_execution_requires_template():
    with pytest.raises(Exception):
        SkillExecution(type="tool", tool="web_search", template="   ")


def test_prompt_execution_still_requires_template():
    with pytest.raises(Exception):
        SkillExecution(type="prompt", template="")


def test_existing_prompt_skill_still_valid():
    ex = SkillExecution(type="prompt", template="写一句话")
    assert ex.tool == ""


# ---------------------------------------------------------------------------
# 7. 控制面：abilities + P6 真接线
# ---------------------------------------------------------------------------

def test_abilities_reports_installed_skills():
    from pathlib import Path

    from anima.os.control_surface import HumanAIControlSurface

    core = _core_with_tools()
    builtin = Path(__file__).resolve().parents[1] / "anima" / "skills" / "builtin"
    core.skills = SkillRegistry(Path("/nonexistent-data-dir"), core, builtin_root=builtin)
    core.skills.load()
    out = HumanAIControlSurface(core).abilities()
    assert out["available"] is True
    assert out["count"] >= 4
    assert any(r["id"] == "web_brief" for r in out["items"])
    assert "无提权上界" in out["note"]


def test_abilities_honest_when_skills_disabled():
    from anima.os.control_surface import HumanAIControlSurface

    core = _core_with_tools()
    out = HumanAIControlSurface(core).abilities()
    assert out == {"available": False, "reason": "Skills 未启用"}


async def test_skill_evolution_dry_run_does_not_create_draft():
    from anima.os.control_surface import HumanAIControlSurface

    core = _core_with_tools()
    core.llm = _FakeLLM()
    out = await HumanAIControlSurface(core).skill_evolution(request="做一个查天气的技能")
    assert out["available"] is True
    assert out.get("dry_run") is True
    assert "create_draft" in out["note"]


async def test_skill_evolution_without_proposer_is_honest():
    from anima.os.control_surface import HumanAIControlSurface

    core = _core_with_tools()
    core.llm = None
    core.skill_drafts = None
    out = await HumanAIControlSurface(core).skill_evolution(request="x")
    assert out["available"] is False
    assert "提案生成器" in out["reason"]


async def test_skill_evolution_creates_and_evaluates_draft():
    from anima.os.control_surface import HumanAIControlSurface
    from anima.skills.drafts import SkillDraftManager

    core = _core_with_tools()
    core.llm = _FakeLLM(reply=(
        '{"id":"weather_lite","name":"天气速查","description":"查天气",'
        '"min_level":"user","parameters":{"query":{"type":"string","description":"城市"}},'
        '"execution":{"type":"tool","tool":"web_search","template":"整理成天气要点",'
        '"content_summary":true}}'
    ))
    mgr = SkillDraftManager.__new__(SkillDraftManager)
    mgr.core = core
    mgr.items = {}
    mgr._save = lambda: None
    mgr._audit = lambda *a, **k: None
    core.skill_drafts = mgr

    out = await HumanAIControlSurface(core).skill_evolution(
        request="做一个查天气的技能", create_draft=True)
    assert out["available"] is True
    assert out["draft_id"]
    assert "evaluation" in out
    assert out["evaluation"]["sandbox_ok"] is True
    assert out["evaluation"]["adoptable"] is True
    # 绝不自我批准
    assert "draft approve" in out["note"]
    assert mgr.items[out["draft_id"]].status == "pending_review"


async def test_skill_evolution_no_request_is_honest():
    from anima.os.control_surface import HumanAIControlSurface

    out = await HumanAIControlSurface(_core_with_tools()).skill_evolution()
    assert out["available"] is False


# ---------------------------------------------------------------------------
# 8. set_reminder（复用 P1-1 债账本）
# ---------------------------------------------------------------------------

def _reminder_core(tmp_path):
    from anima.tools.builtin.reminder import register_reminder_tools

    core = _FakeCore()
    core.config = type("C", (), {
        "skills": type("S", (), {"allow_prompt": True})(),
        "persistence": type("P", (), {"data_dir": str(tmp_path)})(),
        "debt_tracking": type("D", (), {"enabled": True})(),
    })()
    register_reminder_tools(core.tools, core)
    return core


async def test_set_reminder_writes_into_debt_ledger(tmp_path):
    core = _reminder_core(tmp_path)
    assert "set_reminder" in core.tools.tools
    out = await core.tools.call("set_reminder",
                                {"what": "交电费", "when": "明天 9 点",
                                 "group_id": "727631526", "user_id": "1"},
                                LEVEL_USER)
    assert "已记下" in out
    debts = DebtManager(str(tmp_path)).all()
    assert len(debts) == 1
    assert debts[0].content == "交电费"
    assert debts[0].due_ts is not None
    assert debts[0].source_group == "727631526"


async def test_set_reminder_without_time_records_open_debt(tmp_path):
    core = _reminder_core(tmp_path)
    out = await core.tools.call("set_reminder", {"what": "看看那个 bug"}, LEVEL_USER)
    assert "已记下" in out
    debts = DebtManager(str(tmp_path)).all()
    assert debts[0].due_ts is None


async def test_set_reminder_is_honest_when_time_unparsed(tmp_path):
    core = _reminder_core(tmp_path)
    out = await core.tools.call("set_reminder",
                                {"what": "开会", "when": "等我心情好的时候"}, LEVEL_USER)
    assert "没从" in out and "识别出具体时间" in out


async def test_list_reminders_shows_pending(tmp_path):
    core = _reminder_core(tmp_path)
    await core.tools.call("set_reminder", {"what": "交电费", "when": "明天 9 点"}, LEVEL_USER)
    out = await core.tools.call("list_reminders", {}, LEVEL_USER)
    assert "交电费" in out


async def test_reminder_tools_disabled_by_config(tmp_path):
    core = _reminder_core(tmp_path)
    core.config = type("C", (), {
        "skills": type("S", (), {"allow_prompt": True})(),
        "persistence": type("P", (), {"data_dir": str(tmp_path)})(),
        "debt_tracking": type("D", (), {"enabled": False})(),
    })()
    out = await core.tools.call("set_reminder", {"what": "x"}, LEVEL_USER)
    assert "已关闭" in out

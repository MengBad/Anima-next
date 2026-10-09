# -*- coding: utf-8 -*-
"""`skill_web_brief` 路由提示（2026-10-10）。

## 为什么需要它
`skill_web_brief` 在生产**近 40 天 0 次调用**，而同期联网检索发生 177+ 次。
根因已定位到**结构性位置**（不是没加载、没权限、没注册）：

- 生产 `capability_mode=true` ⇒ `get_schemas()` 返回
  `capability_schemas() + legacy_only_schemas()`，**capability 卡恒前、legacy 卡恒后**；
- `sort_by_priority`（Memory-First 排序）**只在非 capability 分支生效**；
- 实测：`skill_web_brief` 排 **29/29（群聊）／48/48（owner）**，竞品 `search_web` 排 **第 1**。

⇒ 光靠 priority 抬不动它，只能走 system 提示这条既有通道（与「长度预算」同机制）。

## 判据
1. 默认关 ⇒ 不注入（零行为变更）。
2. 防哑开关：`SkillRouteHintConfig` 必须是 `ReactConfig` 的**显式字段**
   （pydantic 在本项目 `extra=ignore` ⇒ 只写 yaml 会被静默丢弃）。
3. 开启 + 技能已注册 ⇒ 注入，且片段里**真的点名**该工具。
4. 开启 + 技能**未注册** ⇒ 不注入（fail-closed：不许向 LLM 提一个它调不到的工具）。
5. 开启 + 技能已停用 ⇒ 不注入（同上）。
6. 片段有长度上界（system 预算不是无限的）。
7. 注入点必须 `protected=True`（被预算器静默裁掉 = 规则没生效且无人知道）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from anima.core.config import ReactConfig, SkillRouteHintConfig
from anima.pipeline import PipelineContext
from anima.pipeline.stages.cognition import _skill_route_hint_fragment
from anima.tools import LEVEL_USER, Tool

SRC = Path(__file__).resolve().parents[1] / "anima" / "pipeline" / "stages" / "cognition.py"


def _ctx(text: str) -> PipelineContext:
    return PipelineContext(event={}, text=text, user_id=1, group_id=2,
                           detail_type="group", sender_name="A", meta={})


def _core(*, enabled: bool, register: bool = True, tool_enabled: bool = True,
          skill: str = "skill_web_brief"):
    async def _h(**_kw):
        return "ok"

    tools = {}
    if register:
        tools["skill_web_brief"] = Tool("skill_web_brief", "联网速览", {}, _h,
                                        min_level=LEVEL_USER, enabled=tool_enabled)
    return SimpleNamespace(
        config=SimpleNamespace(react=ReactConfig(
            skill_route_hint={"enabled": enabled, "skill": skill})),
        tools=SimpleNamespace(tools=tools),
    )


# --- 1. 默认关 ⇒ 不注入（零行为变更）----------------------------------------
async def test_disabled_by_default():
    assert _skill_route_hint_fragment(_ctx("甲公司最新版本是多少"), _core(enabled=False)) == ""


async def test_absent_config_is_treated_as_off():
    """生产 config 里根本没这一段时（旧配置）也必须零行为变更。"""
    core = SimpleNamespace(config=SimpleNamespace(react=ReactConfig()),
                           tools=SimpleNamespace(tools={}))
    assert _skill_route_hint_fragment(_ctx("查一下"), core) == ""


# --- 2. 防哑开关 ------------------------------------------------------------
def test_config_field_exists_or_yaml_is_silently_dropped():
    """pydantic extra=ignore ⇒ 没有显式字段，yaml 里写了也会被丢弃成哑开关。"""
    cfg = ReactConfig(skill_route_hint={"enabled": True})
    assert isinstance(cfg.skill_route_hint, SkillRouteHintConfig)
    assert cfg.skill_route_hint.enabled is True


def test_unknown_key_is_ignored_not_accepted():
    """反证：真的是 extra=ignore，所以上面那条断言才是有意义的守卫。"""
    cfg = ReactConfig(skill_route_hint_typo={"enabled": True})
    assert not hasattr(cfg, "skill_route_hint_typo")


def test_default_is_off():
    assert ReactConfig().skill_route_hint.enabled is False
    assert SkillRouteHintConfig().enabled is False


# --- 3. 开启 + 技能已注册 ⇒ 注入且点名 ---------------------------------------
async def test_enabled_and_registered_names_the_tool():
    frag = _skill_route_hint_fragment(_ctx("甲公司最新版本是多少"), _core(enabled=True))
    assert frag, "开了却没注入 ⇒ 提示是哑的"
    assert "skill_web_brief" in frag, "片段必须点名工具，否则模型不知道该调谁"


# --- 4/5. fail-closed：技能不可达就不许提 -----------------------------------
async def test_not_registered_is_not_advertised():
    core = _core(enabled=True, register=False)
    assert _skill_route_hint_fragment(_ctx("查一下"), core) == ""


async def test_disabled_tool_is_not_advertised():
    core = _core(enabled=True, register=True, tool_enabled=False)
    assert _skill_route_hint_fragment(_ctx("查一下"), core) == ""


async def test_no_tools_surface_at_all_is_safe():
    core = SimpleNamespace(
        config=SimpleNamespace(react=ReactConfig(skill_route_hint={"enabled": True})),
        tools=None,
    )
    assert _skill_route_hint_fragment(_ctx("查一下"), core) == ""


# --- 6. 长度上界 ------------------------------------------------------------
async def test_fragment_is_short():
    frag = _skill_route_hint_fragment(_ctx("查一下"), _core(enabled=True))
    assert len(frag) <= 220, f"片段 {len(frag)} 字，太长会挤占 system 预算"


# --- 7. 注入点必须 protected ------------------------------------------------
def test_injection_is_protected():
    """被预算器静默裁掉 = 这条规则没生效且没人知道（与「判据锚不存在」同类）。"""
    src = SRC.read_text(encoding="utf-8")
    marker = 'add_system_fragment("skill_route_hint"'
    idx = src.find(marker)
    assert idx != -1, "没有在 compose_system 里注入 skill_route_hint 片段"
    # 调用可能被折行 ⇒ 取标记后一段而不是「到行尾」
    call = src[idx:idx + 160]
    assert "protected=True" in call, f"注入行缺少 protected=True: {call!r}"

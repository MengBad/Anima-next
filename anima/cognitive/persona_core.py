"""人格单源装配器（队列②，2026-09-12）。

``config/persona_core.yaml`` 是人格唯一正本：
- 被动回复（pipeline/stages/cognition）与主动发言（core/autonomy）共用本模块装配；
- 任何加载失败直接抛 ``RuntimeError``，由启动流程 fail-closed——
  宪法约束：人格不能静默变味（宁可拒绝启动，也不回退到过期的内联副本）。

路径锚定与 ``cognitive/persona_checkpoint.py``、``cognitive/danger.py`` 一致
（仓库/部署根的 ``config/``）；persona_checkpoint 的快照/回滚、danger 的
proposed.yaml 提案链均只围绕这一正本工作。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def persona_yaml_path() -> Path:
    """persona_core.yaml 绝对路径（锚定仓库/部署根的 config/）。"""
    return Path(__file__).resolve().parents[2] / "config" / "persona_core.yaml"


def load_persona_raw() -> dict[str, Any]:
    """读取人格正本并解析为 dict；不可读/解析失败/结构异常一律抛 RuntimeError。"""
    path = persona_yaml_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"人格正本不可读: {path} ({exc})") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"人格正本 YAML 解析失败: {path} ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"人格正本结构异常（期望 mapping）: {path}")
    return data


# 人格模板的动态片段占位符（模板侧与 config/persona_core.yaml 保持一致）。
SECTION_KEYS = ("time_context", "desire_context", "self_notes", "world_context", "scar_context")


def load_persona_template() -> str:
    """人格核心提示词模板；缺失/为空/解析失败抛 RuntimeError（fail-closed）。"""
    tmpl = str(load_persona_raw().get("system_prompt") or "").strip()
    if not tmpl:
        raise RuntimeError(f"人格正本缺少 system_prompt: {persona_yaml_path()}")
    return tmpl


def assemble_persona_system(extra_rules: str = "", **sections: str | None) -> str:
    """装配人格 system：正本模板 + 动态片段填充 + 出口专属守则追加。

    - 提供的片段（如 ``time_context``）替换对应占位符；
    - 未提供的 SECTION_KEYS 片段替换为空串（不留裸花括号）；
    - 模板中的其他花括号原样保留（与旧 _safe_format 语义一致）；
    - ``extra_rules``（发言守则/身份锁定等出口专属内容）追加在模板之后。
    """
    template = load_persona_template()
    for key in SECTION_KEYS:
        template = template.replace("{" + key + "}", str(sections.get(key) or ""))
    if extra_rules:
        template = template.rstrip() + "\n\n" + extra_rules.strip()
    return template

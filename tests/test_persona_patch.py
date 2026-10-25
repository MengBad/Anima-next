"""persona_core.yaml 补丁回归测试。

守护 2026-07-11 审查发现的三类回复退化：
- 模糊输入被脑补成长篇（anti_derailment）
- 破角色结构化解剖/社会学分析（anti_derailment + 角色守卫）
- 主动外泄用户私人事实（anti_derailment）
以及人格单源（队列②）：内联兜底已删除，YAML 不可加载必须 fail-closed。
"""
import yaml
import pytest
from pathlib import Path

from anima.cognitive.persona_core import load_persona_template
from anima.cognitive import persona_core as pc


def _load_persona() -> str:
    p = Path(__file__).resolve().parent.parent / "config" / "persona_core.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data["system_prompt"]


def test_persona_has_grounded_catgirl_voice():
    sp = _load_persona()
    assert "哈基米" in sp
    assert "克制" in sp  # 对陌生人保持克制
    assert "不主动披露记忆" in sp
    assert "对陌生人" in sp
    assert "瞬间越级" in sp
    assert "主体性" in sp  # 新版人格核心：主体性


def test_persona_keeps_task_and_uncertainty_rules():
    """🔴 2026-10-25：原判据是「明确要解析/步骤时才展开 **或** 任务类该详细就详细」。

    第二个分支是个**模糊例外** —— 模型分不清什么算「任务类」，于是整体走长。
    生产实证（50 天 1422 条）：平均 102.4 字、中位 84、89% 含 2 句以上，
    而人格要求 25 字 / 最多两句 ⇒ 这个后门必须关掉。

    但**意图**要保留：任务类确实需要展开。⇒ 判据改为
    「展开只在对方**明确说**要详细时发生」+「复杂话题也只回一句最要紧的」。
    """
    sp = _load_persona()
    # 新判据：展开的触发条件必须**明确**（不是我方猜测的「任务类」）
    assert "只发生在对方明确说" in sp, "「展开」的后门没关"
    assert "任务类该详细就详细" not in sp, "旧的模糊例外还在"
    # 保留：复杂话题照样能答，只是要短
    assert "复杂的话题也只回一句最要紧的" in sp
    # 不确定性规则不变
    assert "不编实时事实" in sp
    assert "优先走工具拿事实" in sp


def test_template_placeholders_intact():
    """占位符与 SECTION_KEYS 对齐，避免装配阶段占位段丢失。"""
    sp = load_persona_template()
    assert "哈基米" in sp
    for ph in pc.SECTION_KEYS:
        assert "{" + ph + "}" in sp


def test_missing_yaml_fails_closed(tmp_path, monkeypatch):
    """内联兜底已删除（队列②）：正本不可读必须抛错（拒绝启动），而非静默换人格。"""
    monkeypatch.setattr(pc, "persona_yaml_path", lambda: tmp_path / "missing.yaml")
    with pytest.raises(RuntimeError):
        load_persona_template()


def test_broken_yaml_fails_closed(tmp_path, monkeypatch):
    p = tmp_path / "persona_core.yaml"
    p.write_text("system_prompt: [unclosed", encoding="utf-8")
    monkeypatch.setattr(pc, "persona_yaml_path", lambda: p)
    with pytest.raises(RuntimeError):
        load_persona_template()

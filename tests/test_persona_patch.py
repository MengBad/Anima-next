"""persona_core.yaml 补丁回归测试。

守护 2026-07-11 审查发现的三类回复退化：
- 模糊输入被脑补成长篇（anti_derailment）
- 破角色结构化解剖/社会学分析（anti_derailment + 角色守卫）
- 主动外泄用户私人事实（anti_derailment）
以及内联兜底人格必须对齐哈基米（防 YAML 加载失败静默切换身份）。
"""
import yaml
from pathlib import Path

from anima.pipeline.stages import SYSTEM_PROMPT


def _load_persona() -> str:
    p = Path(__file__).resolve().parent.parent / "config" / "persona_core.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data["system_prompt"]


def test_persona_has_grounded_catgirl_voice():
    sp = _load_persona()
    assert "哈基米" in sp
    assert "琪诺露式的克制、清醒和观察力" in sp
    assert "不主动披露记忆" in sp
    assert "不拿身份、性别表达或群体当贬损梗" in sp
    assert "对陌生群友" in sp
    assert "不无条件站队" in sp
    assert "瞬间越级" in sp


def test_persona_keeps_task_and_uncertainty_rules():
    sp = _load_persona()
    assert "用户明确要解析/总结/步骤时才展开" in sp or "任务类该详细就详细" in sp
    assert "不编实时事实" in sp
    assert "优先走工具拿事实" in sp


def test_fallback_prompt_is_hakimi_not_anima():
    """内联兜底人格必须对齐哈基米，避免 persona_core.yaml 加载失败时静默切换身份。"""
    assert "哈基米" in SYSTEM_PROMPT
    assert "你是 Anima" not in SYSTEM_PROMPT
    # 占位符保持一致，避免 .format() 抛 KeyError
    for ph in ("{time_context}", "{desire_context}", "{self_notes}",
               "{world_context}", "{scar_context}"):
        assert ph in SYSTEM_PROMPT

"""漂移配置接线 + 人格「有温度解题」路径（shipped load_persona / apply_drift）。"""

from types import SimpleNamespace

from anima.cognitive.learning import SelfLearning
from anima.pipeline.stages import load_persona_template


def test_apply_drift_config_updates_budgets(tmp_path):
    sl = SelfLearning(str(tmp_path))
    # 忽略可能从全局 config 读到的值，显式注入
    sl.apply_drift_config(SimpleNamespace(
        delta_max=0.08,
        daily_budget=1.2,
        self_correction_daily_budget=0.4,
        window_ceiling=0.7,
        similarity_floor=0.7,
    ))
    assert sl.delta_max == 0.08
    assert sl.daily_budget == 1.2
    assert sl.self_correction_daily_budget == 0.4
    assert sl.window_ceiling == 0.7
    assert sl.similarity_floor == 0.7


def test_check_drift_respects_injected_budget(tmp_path):
    sl = SelfLearning(str(tmp_path))
    sl.apply_drift_config(SimpleNamespace(
        delta_max=0.10,
        daily_budget=0.10,
        self_correction_daily_budget=0.5,
        window_ceiling=0.8,
        similarity_floor=0.75,
    ))
    sl.daily_spent = 0.0
    assert sl.check_drift(0.05) is True
    assert sl.check_drift(0.05) is True
    # 第三笔 0.05 应超过 0.10
    assert sl.check_drift(0.05) is False


def test_persona_template_has_emotional_problem_solving_path():
    tmpl = load_persona_template()
    assert "解决问题时" in tmpl
    assert ("本喵帮你瞅瞅" in tmpl) or ("别慌" in tmpl)
    assert "工具失败" in tmpl


def test_persona_keeps_identity_and_not_assistant():
    tmpl = load_persona_template()
    assert "哈基米" in tmpl
    assert "不自称 AI" in tmpl or "不自称 AI、助手" in tmpl

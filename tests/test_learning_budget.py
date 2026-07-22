"""漂移预算测试：总预算加大 + 独立的「自我修正」预算池互不挤占。

隔离 tmp_path，不触碰生产数据。
"""
import time

from anima.cognitive.learning import SelfLearning


def test_general_pool_full_still_allows_self_correction(tmp_path):
    """普通反思池耗尽后，自我修正笔记仍能写入（独立池）。"""
    sl = SelfLearning(str(tmp_path))
    n = 0
    while sl.check_drift(0.05, "general"):
        n += 1
    assert n == 30, f"通用池应为 30 条 (1.5/0.05)，实际 {n}"

    ok = sl.add_note("我提那个八卦太频繁了，要克制收敛", delta=0.05,
                     category="self_correction")
    assert ok is True
    assert sl.self_correction_spent == 0.05


def test_self_correction_pool_full_still_allows_general(tmp_path):
    """自我修正池耗尽后，普通反思仍能写入（独立池）。"""
    sl = SelfLearning(str(tmp_path))
    m = 0
    while sl.check_drift(0.05, "self_correction"):
        m += 1
    assert m == 10, f"自我修正池应为 10 条 (0.5/0.05)，实际 {m}"

    ok = sl.add_note("今天和用户聊得很愉快", delta=0.05, category="general")
    assert ok is True
    assert sl.daily_spent == 0.05


def test_classify_heuristic(tmp_path):
    """启发式分类：命中信号词→self_correction，否则 general。"""
    sl = SelfLearning(str(tmp_path))
    assert sl._classify("我太频繁提那个话题了，要收敛") == "self_correction"
    assert sl._classify("意识到自己不该那样说") == "self_correction"
    assert sl._classify("今天天气不错，聊得开心") == "general"


def test_auto_classify_on_add_note(tmp_path):
    """add_note 不传 category 时按内容自动分类，并打【自我修正】前缀。"""
    sl = SelfLearning(str(tmp_path))
    ok = sl.add_note("我反省自己话太多，要少提")  # 含信号词
    assert ok is True
    notes = sl.get_notes()
    assert "【自我修正】" in notes


def test_delta_max_enforced(tmp_path):
    """单次 Δ 超过 Δ_max 一律拒绝。"""
    sl = SelfLearning(str(tmp_path))
    assert sl.check_drift(0.11) is False
    assert sl.check_drift(0.10) is True


def test_daily_reset(tmp_path):
    """跨日（>86400s）后两池清零。"""
    sl = SelfLearning(str(tmp_path))
    sl.daily_spent = 1.4
    sl.self_correction_spent = 0.4
    sl.day_start = time.time() - 90000  # 超过一天
    assert sl.check_drift(0.05, "general") is True
    assert sl.daily_spent == 0.05
    assert sl.self_correction_spent == 0.0


def test_persist_round_trip(tmp_path):
    """两池消费额持久化到 learning_state.json，重启实例可读回。"""
    sl = SelfLearning(str(tmp_path))
    sl.add_note("自我修正：不该过度纠正用户", delta=0.05, category="self_correction")
    sl2 = SelfLearning(str(tmp_path))
    assert sl2.self_correction_spent == 0.05
    assert sl2.daily_spent == 0.0


def test_can_afford_any(tmp_path):
    """任一池有余额即可生成（ruminate 预检语义）。"""
    sl = SelfLearning(str(tmp_path))
    # 先耗尽通用池
    while sl.check_drift(0.05, "general"):
        pass
    # 自我修正池还有余额 → 仍可生成
    assert sl.can_afford_any(0.05) is True

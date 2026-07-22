"""群氛围分析：差异化、节流观察、防抖。"""

from anima.crossgroup.atmosphere.analyzer import analyze, merge
from anima.crossgroup.atmosphere.profiles import AtmosphereStyle


def test_tech_group_formal():
    prof = analyze(["这段rust代码有bug", "git冲突怎么解决", "k8s部署优化", "数据库索引优化"])
    assert prof.style == AtmosphereStyle.FORMAL


def test_game_group_humorous():
    prof = analyze(["哈哈今晚开黑上分666", "这boss太菜鸡了卧槽", "队友carry我yyds"])
    assert prof.style == AtmosphereStyle.HUMOROUS


def test_two_groups_differ():
    """技术群与游戏群氛围应区分——G1 氛围差异化的核心。"""
    tech = analyze(["rust代码bug", "git冲突", "k8s部署"])
    game = analyze(["开黑上分666", "菜鸡卧槽", "carry yyds"])
    assert tech.style != game.style


def test_merge_anti_jitter():
    """窗口以技术为主、混入少量噪声时，画像应保持 formal（防抖）。

    注：单条短消息会得高 casual 置信度（avg_len<40）从而能翻转——这是分析器
    特性。线上防抖靠 observe_message 的节流 + 窗口（最近 N 条整体分析），
    故此处用"窗口"而非单条来验证防抖语义。
    """
    base = analyze(["rust代码", "git冲突", "k8s部署"])
    merged = merge(base, ["rust代码", "git冲突", "哈哈哈", "数据库优化"], weight=0.3)
    assert merged.style == AtmosphereStyle.FORMAL


def test_merge_allows_evolution():
    """后续窗口风格明确改变且高置信时，画像应演进（非一成不变）。"""
    base = analyze(["rust代码", "git冲突", "k8s部署"])  # formal
    merged = merge(base, ["开黑上分666", "菜鸡卧槽", "carry yyds"], weight=0.5)
    assert merged.style == AtmosphereStyle.HUMOROUS


def test_observe_message_throttle(crossgroup_tmp):
    """逐条消息观察：未满节流阈值不落盘画像，满后分析。"""
    cgm = crossgroup_tmp
    for _ in range(4):
        cgm.observe_message("g1", "这段rust代码有bug")
    # 默认画像（未触发 observe_group）
    assert cgm.get_atmosphere("g1").style == AtmosphereStyle.NEUTRAL
    cgm.observe_message("g1", "git冲突怎么解决")  # 第 5 条触发
    assert cgm.get_atmosphere("g1").style == AtmosphereStyle.FORMAL

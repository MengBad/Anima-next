"""find_mentioned / 昵称清洗 单元测试：覆盖三源判定、发言者排除、短名防护、预算、清洗匹配。"""
import pytest

from anima.crossgroup.memory.manager import CrossGroupManager
from anima.util.name_clean import clean_display_name


def test_clean_display_name():
    assert clean_display_name("[中国翻訳] 吳雨萌貓爪貓爪prpr [DL版]") == "吳雨萌貓爪貓爪prpr"
    assert clean_display_name("  张三  ") == "张三"
    # 名字全在括号里 → 清洗后为空，回退原名
    assert clean_display_name("[only]") == "[only]"


def test_find_mentioned_text_substring(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("3509574818", "喜欢画画", "727631526", name="亦萱")
    hits = mgr.find_mentioned("这个亦萱有多少可能是男娘", at_uids=[], exclude_uid="1562290139")
    assert ("3509574818", "亦萱", 2) in [(u, n, p) for u, n, p in hits]


def test_find_mentioned_excludes_speaker(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("3509574818", "x", "727631526", name="亦萱")
    hits = mgr.find_mentioned("亦萱", at_uids=[], exclude_uid="3509574818")
    assert not any(u == "3509574818" for u, _, _ in hits)


def test_find_mentioned_short_name_guard(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("111", "x", "727631526", name="小宝")
    mgr.add_user_fact("222", "x", "727631526", name="宝")  # 单字，应被短名阈值跳过
    hits = mgr.find_mentioned("我的小宝贝来了", at_uids=[], exclude_uid="x")
    uids = [u for u, _, _ in hits]
    assert "222" not in uids   # 单字「宝」不误命中「小宝贝」
    assert "111" in uids       # 「小宝」正常命中


def test_find_mentioned_at_uids(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("3509574818", "x", "727631526", name="亦萱")
    # 文本里没名字，但 @ 段带了 qq → 直接命中
    hits = mgr.find_mentioned("他在哪", at_uids=["3509574818"], exclude_uid="x")
    assert ("3509574818", "亦萱", 3) in [(u, n, p) for u, n, p in hits]


def test_find_mentioned_limit(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    for i, nm in enumerate(["张三", "李四", "王五", "赵六"]):
        mgr.add_user_fact(str(1000 + i), "x", "727631526", name=nm)
    hits = mgr.find_mentioned("张三 李四 王五 赵六", at_uids=[], exclude_uid="x", limit=2)
    assert len(hits) == 2


def test_find_mentioned_cleaned_match(tmp_path):
    mgr = CrossGroupManager(str(tmp_path))
    # 记忆里存的名字带装饰括号（存量数据形态），文本用核心名 → 清洗后仍应命中
    mgr.add_user_fact("555", "x", "727631526", name="[中国翻訳]亦萱[x]")
    hits = mgr.find_mentioned("亦萱今天咋样", at_uids=[], exclude_uid="x")
    assert any(u == "555" for u, _, _ in hits)


def test_find_mentioned_group_priority(tmp_path):
    """ANM-001：跨群同名碰撞时，当前群成员应排在前面。"""
    mgr = CrossGroupManager(str(tmp_path))
    # 同名「小明」两人：A 在当前群 727631526 有交互，B 只在别的群
    mgr.add_user_fact("2001", "x", "727631526", name="小明")  # 本群成员
    mgr.add_user_fact("2002", "x", "999999999", name="小明")  # 其他群
    hits = mgr.find_mentioned("小明来了", at_uids=[], exclude_uid="x",
                              group_id="727631526", limit=2)
    assert hits[0][0] == "2001", "本群成员应在跨群同名碰撞时优先"


def test_find_mentioned_no_group_priority_without_group_id(tmp_path):
    """对照：不传 group_id 时不做群优先提升（两人同优先级，均命中）。"""
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("2001", "x", "727631526", name="小明")
    mgr.add_user_fact("2002", "x", "999999999", name="小明")
    hits = mgr.find_mentioned("小明来了", at_uids=[], exclude_uid="x", limit=2)
    uids = [u for u, _, _ in hits]
    assert "2001" in uids and "2002" in uids

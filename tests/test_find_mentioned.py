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
    assert "111" not in uids   # F4：两字「小宝」不应作为「小宝贝」子串误命中


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


def test_find_mentioned_alias_injection_not_substring_matched(tmp_path):
    """ANM-006：攻击者被注入别名「阿布」后，不应在子串语义下劫持对真实阿布的提及。

    攻击路径：攻击者发「大家都叫我阿布」→ bot 将其记入 aliases；随后任何人说
    「阿布在吗」，旧子串匹配会让攻击者的 alias 命中，导致 bot 把内容关联到攻击者。
    修复后别名仅精确匹配（q == al），子串语境不再劫持。
    """
    mgr = CrossGroupManager(str(tmp_path))
    # 真实用户：本名「阿布」
    mgr.add_user_fact("real", "x", "727631526", name="阿布")
    # 攻击者：本名「黑客」，在某群以「阿布」自称 → 进入 aliases（非 name）
    mgr.get_user("attacker", name="黑客")
    mgr.get_user("attacker", name="阿布")  # 与已有 name 不同 → 记入 aliases
    assert "阿布" in mgr._users["attacker"].aliases

    # 整句就是「阿布」：精确匹配下真实阿布(name, prio 3) 高于攻击者(alias, prio 1)
    hits = mgr.find_mentioned("阿布", at_uids=[], exclude_uid="x")
    assert hits[0][0] == "real", "真实阿布应排在注入别名的攻击者之前"

    # 子串语境「阿布在吗」：攻击者别名仅精确匹配，不得命中；真实阿布(name)命中
    hits2 = mgr.find_mentioned("阿布在吗", at_uids=[], exclude_uid="x")
    uids2 = [u for u, _, _ in hits2]
    assert "attacker" not in uids2, "攻击者的注入别名不应被子串劫持"
    assert "real" in uids2, "真实阿布应正常命中"


def test_find_mentioned_two_char_boundary_excludes_longer_word(tmp_path):
    """F4：两字名不应作为更长词（小宝贝）的子串误命中。"""
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("111", "x", "727631526", name="小宝")
    hits = mgr.find_mentioned("小宝贝在吗", at_uids=[], exclude_uid="x")
    assert not any(u == "111" for u, _, _ in hits)


def test_find_mentioned_two_char_boundary_allows_standalone(tmp_path):
    """F4：两字名在边界安全的位置（小宝在吗）应正常命中。"""
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("111", "x", "727631526", name="小宝")
    hits = mgr.find_mentioned("小宝在吗", at_uids=[], exclude_uid="x")
    assert any(u == "111" for u, _, _ in hits)


def test_find_mentioned_two_char_boundary_punctuation_ok(tmp_path):
    """F4：名字后紧跟标点/空格视为边界安全，应正常命中。"""
    mgr = CrossGroupManager(str(tmp_path))
    mgr.add_user_fact("111", "x", "727631526", name="小宝")
    for msg in ["小宝，在吗", "小宝！", "小宝 在吗", "小宝。"]:
        hits = mgr.find_mentioned(msg, at_uids=[], exclude_uid="x")
        assert any(u == "111" for u, _, _ in hits), f"「{msg}」应命中小宝"

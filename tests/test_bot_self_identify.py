"""让 bot 自己判断谁是 bot（2026-10-28）。

## owner 的原话

> 这几个不存在群里啊……而且可以让 bot 自己判断谁是 bot 的

## 旧设计的两个极端

`bot_social.py` 的老注释写着「昵称启发式 NEVER 写入注册表，UID 才是唯一主键」。
方向是对的（把真人误判成 bot 的代价很大：bot 会突然不理一个 trust 1.0 的老朋友），
但它走得**太远** —— 连真实行为证据也不自动采纳：

    - 只看 `role == 'bot'`  ⇒ NapCat 实测**从不标记** ⇒ 自动确认恒空集
    - `known_bots.json` mtime 停在 10-15，那三条全是 owner 手工填的

= 一个明显是 bot 的账号，必须 owner 手工填 `known_bot_ids` 才生效。

## 也不能只靠昵称

`552994229` 的 `3889024489(nick=可怜BOT)`、`1093714384` 的
`3695244487(nick=Sasaki Yuto)` 近 7 天**零发言** ⇒ 昵称无从验证。
真人叫「可怜BOT」也完全可能。

## 所以：行为证据，且要求**多个独立维度**

只采纳「人类几乎不可能同时满足」的组合，任何单条都不够（除平台标记）：
    ① platform_role_bot（平台真相，单独即可）
    ② low_qq_level(<4)     —— 新注册号
    ③ fast_reply(<1.5s)    —— 机器速度
    ④ cross_group_broadcast —— 同文案广播多群
"""
from __future__ import annotations

import pytest

from anima.cognitive.bot_social import (
    KnownBotsStore,
    collect_bot_evidence,
    is_bot_by_behavior,
)


# ── 1. 证据收集与阈值 ────────────────────────────────────────────────

def test_platform_role_alone_is_enough():
    """平台标记 role=bot 是真相，单独即可确认。"""
    ev = collect_bot_evidence({"sender": {"role": "bot"}})
    assert ev == ["platform_role_bot"]
    assert is_bot_by_behavior(ev) is True


def test_no_evidence_is_not_bot():
    assert collect_bot_evidence({"sender": {"role": "member"}}) == []
    assert is_bot_by_behavior([]) is False


@pytest.mark.parametrize("kw", [
    {"member_level": 2},                                  # 只有低等级
    {"reply_lag_sec": 0.5},                               # 只有秒回
    {"same_text_group_count": 3},                         # 只有跨群
])
def test_single_evidence_is_not_enough(kw):
    """任何**单条**非平台证据都不足以确认 —— 宁可漏判。"""
    ev = collect_bot_evidence({"sender": {"role": "member"}}, **kw)
    assert ev, f"应收集到证据：{kw}"
    assert is_bot_by_behavior(ev) is False, f"单条证据不该确认：{ev}"


def test_low_level_plus_fast_reply():
    """低等级 + 秒回 = 2 条独立证据 ⇒ 确认。

    ⚠️ 这两条必须**分开计数**：合并成 `low_level_fast_reply` 一条会
    只算 1 条证据、达不到阈值 2 ⇒ 该判 bot 的判不出来
    （首版就是这么写的，被本测试抓出）。
    """
    ev = collect_bot_evidence(
        {"sender": {"role": "member"}},
        member_level=2, reply_lag_sec=0.8)
    assert len(ev) >= 2, f"低等级+秒回应算 2 条独立证据，实得 {ev}"
    assert is_bot_by_behavior(ev) is True


def test_cross_group_plus_fast_reply():
    ev = collect_bot_evidence(
        {"sender": {"role": "member"}},
        same_text_group_count=3, reply_lag_sec=0.5)
    assert is_bot_by_behavior(ev) is True


def test_old_account_fast_reply_is_not_bot():
    """老号（高等级）秒回 = 真人手快，不是 bot。"""
    ev = collect_bot_evidence(
        {"sender": {"role": "member"}},
        member_level=30, reply_lag_sec=0.5)
    assert is_bot_by_behavior(ev) is False


def test_slow_reply_low_level_is_not_bot():
    """低等级但慢回（30s）—— 可能就是新注册的真人。"""
    ev = collect_bot_evidence(
        {"sender": {"role": "member"}},
        member_level=2, reply_lag_sec=30)
    assert is_bot_by_behavior(ev) is False


# ── 2. 注册表：自动确认 + 可回滚 ─────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    return KnownBotsStore(str(tmp_path))


def test_register_from_evidence_needs_enough(store):
    assert store.register_from_evidence("111", "x", ["fast_reply(0.5s)"]) is False
    assert store.uids() == set()

    assert store.register_from_evidence(
        "222", "y", ["low_qq_level(lv=2)", "fast_reply(0.5s)"]) is True
    assert 222 in store.uids()


def test_register_does_not_overwrite_confirmed(store):
    """已在册（含 owner 手工确认）不覆盖 —— 手工优先。"""
    store.add("333", "manual", confirmed=True)
    assert store.register_from_evidence(
        "333", "manual", ["low_qq_level(lv=1)", "fast_reply(0.3s)"]) is False
    # 手工条目不该被标成 auto
    assert "333" not in {str(u) for u in store.auto_uids()}


def test_auto_flag_survives_reload(store, tmp_path):
    """`auto`/`evidence` 必须能落盘并读回，否则回滚会失效。

    首版 `load()` 只保留 `bot_name`/`confirmed` ⇒ `auto` 读回来就丢了
    ⇒ `revert_auto_bots()` 找不到要删的条目。
    """
    store.register_from_evidence(
        "444", "a", ["low_qq_level(lv=1)", "fast_reply(0.3s)"])
    reloaded = KnownBotsStore(str(tmp_path))
    assert 444 in reloaded.auto_uids(), "auto 标记没落盘"
    assert reloaded._bots["444"].get("evidence")


def test_revert_auto_bots_keeps_manual(store):
    """回滚只删自动判定的，owner 手工确认的必须保留。"""
    store.add("500", "manual", confirmed=True)
    store.register_from_evidence(
        "600", "auto", ["low_qq_level(lv=1)", "fast_reply(0.3s)"])
    assert store.uids() == {500, 600}

    removed = store.revert_auto_bots()
    assert removed == ["600"]
    assert store.uids() == {500}, "手工确认的条目被误删"


def test_manual_confirm_clears_auto_flag(store):
    """owner 手工确认一个自动判定的号 ⇒ 摘掉 auto（不再被回滚误删）。"""
    store.register_from_evidence(
        "700", "x", ["low_qq_level(lv=1)", "fast_reply(0.3s)"])
    assert 700 in store.auto_uids()
    store.add("700", "x", confirmed=True)      # owner 手工确认
    assert 700 not in store.auto_uids(), "手工确认后仍是 auto"
    assert store.revert_auto_bots() == []
    assert 700 in store.uids()


# ── 3. 反向守卫：不能退化成「只看昵称」───────────────────────────────

def test_nickname_alone_never_confirms():
    """昵称单独**绝不能**确认（那正是老设计刻意避免的误判）。

    生产里有真人叫「可怜BOT」，也有 bot 叫「小明」。
    """
    ev = collect_bot_evidence(
        {"sender": {"role": "member", "nickname": "可怜BOT"}})
    assert ev == [], "昵称不该产生证据"
    assert is_bot_by_behavior(ev) is False


# ── 4. 🔴 平台真相字段 `is_robot`（2026-10-28 关键发现）───────────────

def test_is_robot_field_is_used():
    """NapCat 成员对象里有 `is_robot` 字段，必须采信。

    ## 这个字段被浪费了整整两周

    老注释写着「2026-10-14 实测 NapCat 对第三方 bot 不标 role
    （3 群 576 人 role=='bot' 为 0）⇒ 当前自动确认恒为空集」。

    **诊断是错的**：NapCat 确实不标 `role='bot'`，
    但它在成员对象里给了独立的 `is_robot`，代码一行都没读。

    实测（10-28，群 727631526，157 人）：
        is_robot=True : 3889239358(chaob) / 3889001741(小小) /
                        2854202507(可怜Bot) / 3889001246(幽幽子) / 3889000770(Tsugu)
        is_robot=False: 3852906976(莉顺卡) / 838916454(沙琪玛)

    而 `known_bots.json` 的 mtime 停在 10-15，三条全是 owner 手工填的 ——
    平台早就把答案给了。
    """
    from anima.cognitive.bot_social import discover_bots_from_members

    members = [
        {"user_id": 1, "nickname": "chaob",   "is_robot": True,  "role": "member"},
        {"user_id": 2, "nickname": "可怜Bot", "is_robot": True,  "role": "member"},
        {"user_id": 3, "nickname": "幽幽子",  "is_robot": True,  "role": "member"},
    ]
    assert discover_bots_from_members(members) == {1, 2, 3}


def test_is_robot_false_not_auto_confirmed():
    """`is_robot=False` 的（莉顺卡/沙琪玛这种）**不自动确认**。

    它们确实是 bot，但平台没标 —— 需要 owner 手工确认（已在 known_bot_ids 里）。
    自动确认只吃「平台说它是」的证据，不吃推测。
    """
    from anima.cognitive.bot_social import discover_bots_from_members

    members = [
        {"user_id": 3852906976, "nickname": "1",        "is_robot": False, "level": 20},
        {"user_id": 838916454,  "nickname": "丰川沙琪玛", "is_robot": False, "level": 68},
    ]
    assert discover_bots_from_members(members) == set()


def test_real_human_never_auto_confirmed():
    """真人绝不能被自动确认（含昵称像 bot 的真人）。"""
    from anima.cognitive.bot_social import discover_bots_from_members

    members = [
        {"user_id": 1562290139, "nickname": "吴雨萌",     "is_robot": False, "level": 40},
        {"user_id": 999,        "nickname": "机器人小王", "is_robot": False, "level": 30},
        {"user_id": 888,        "nickname": "可怜BOT",    "is_robot": False, "level": 25},
    ]
    assert discover_bots_from_members(members) == set()


def test_role_bot_still_works_for_compat():
    """兼容：换网关后若只给 role='bot' 也应采信。"""
    from anima.cognitive.bot_social import discover_bots_from_members
    assert discover_bots_from_members(
        [{"user_id": 7, "role": "bot", "nickname": "x"}]) == {7}


# ── 5. 🔴 评分制（2026-10-28，owner 要求）─────────────────────────

def test_score_weights_are_regression_calibrated():
    """权重必须来自实测校准，不是拍脑袋。

    ## 校准依据（422 行真实成员数据，去重后 bot=8 / human=386）

        特征            bot 命中   human 命中   human 误命中率
        is_robot=True     8/8        0/386          0.0%
        uid 前缀 3889     7/8        0/386          0.0%
        昵称含 bot        2/8        0/386          0.0%
        level == 0        8/8       28/386          7.3%   ← 弱
        从未发言          1/8       28/386          7.3%   ← 弱
        无群名片          8/8      320/386         82.9%   ← **几乎无用**
        qq_level == 0     8/8      386/386        100.0%   ← **完全无用**

    ⇒ `no_card` 只能给 2 分、`qq_level_zero` 只能给 1 分。
      首版若按「看起来像 bot」给它们 15 分，human 会和 bot 混在一起。

    ## 校准结果

        bot   最低 141 分
        human 最高  26 分      ← 间隔 115 分、零重叠
        阈值 60 ⇒ bot 8/8 确认，真人 0/386 误判

    这条测试锁死「弱信号权重必须远低于强信号」，防止有人凭感觉调权重。
    """
    from anima.cognitive.bot_social import _BOT_SCORE_WEIGHTS as W

    # 平台真相是决定性证据
    assert W["is_robot"] >= 100 and W["role_bot"] >= 100
    # 强信号（实测 0% 误命中）
    assert W["uid_known_prefix"] >= 30
    assert W["nickname_hint"] >= 10
    # 弱/无用信号必须压到极低
    assert W["level_zero"] <= 20, "level_zero 只覆盖 7.3% 真人，不能给高权重"
    assert W["never_spoke"] <= 12
    assert W["no_card"] <= 3, "no_card 命中 82.9% 真人，几乎是噪声"
    assert W["qq_level_zero"] <= 2, "qq_level_zero 命中 100% 真人，纯噪声"


def test_score_threshold_sits_between_measured_bounds():
    """确认阈值必须落在实测的 bot 下界与 human 上界之间。"""
    from anima.cognitive.bot_social import _BOT_SCORE_CONFIRM, _BOT_SCORE_SUSPECT
    assert 26 < _BOT_SCORE_SUSPECT <= _BOT_SCORE_CONFIRM
    assert _BOT_SCORE_CONFIRM < 141, "阈值太高会漏掉真实 bot（实测 bot 最低 141）"


def test_real_bot_profile_scores_confirmed():
    """真实 bot 画像（取自生产成员数据）必须判 confirmed。"""
    from anima.cognitive.bot_social import bot_score, classify_bot_score

    # 3889024489 可怜BOT（在 552994229）
    m1 = {"user_id": 3889024489, "nickname": "可怜BOT", "card": "",
          "level": "0", "qq_level": 0, "is_robot": True,
          "role": "member", "join_time": 1753071305, "last_sent_time": 1772517189}
    s1, h1 = bot_score(m1)
    assert s1 >= 60 and classify_bot_score(s1) == "confirmed", f"{s1} {h1}"

    # 2854202507 可怜Bot（在 727631526）—— 无 3889 段，靠 is_robot + 昵称
    m2 = {"user_id": 2854202507, "nickname": "可怜Bot", "card": "",
          "level": "0", "qq_level": 0, "is_robot": True,
          "role": "member", "join_time": 1709376823, "last_sent_time": 1709376823}
    s2, h2 = bot_score(m2)
    assert s2 >= 60 and classify_bot_score(s2) == "confirmed", f"{s2} {h2}"


def test_hardest_real_human_stays_human():
    """最难区分的真人（level=0 + 从未发言 + 无名片）仍须判 human。

    这类样本在生产里有 28 个（如 𝐼𝑐𝑒𝑠𝑝𝑎𝑟 / 奶兽兽 / Nieve.），
    它们满足 3 个弱信号，但缺 2 个强信号 ⇒ 26 分，远低于 60。
    """
    from anima.cognitive.bot_social import bot_score, classify_bot_score

    m = {"user_id": 3609970062, "nickname": "𝐼𝑐𝑒𝑠𝑝𝑎𝑟", "card": "",
         "level": "0", "qq_level": 0, "is_robot": False,
         "role": "member", "join_time": 1787285691, "last_sent_time": 1787285691}
    s, h = bot_score(m)
    assert s < 60, f"最难真人被误判为 bot：{s} {h}"
    assert classify_bot_score(s) == "human"


def test_nickname_only_human_is_not_confirmed():
    """昵称像 bot 的真人（昵称 15 分 + 弱信号）不得越过 60。"""
    from anima.cognitive.bot_social import bot_score, classify_bot_score

    m = {"user_id": 999, "nickname": "机器人小王", "card": "",
         "level": "30", "qq_level": 0, "is_robot": False,
         "role": "member", "join_time": 1, "last_sent_time": 9}
    s, h = bot_score(m)
    assert classify_bot_score(s) != "confirmed", f"仅昵称像就被确认：{s} {h}"


def test_uid_prefix_alone_cannot_confirm():
    """号段单独 40 分 < 60 ⇒ 不确认。

    这是刻意的安全设计：QQ 号段属平台分配，不保证长期独占。
    即便某天普通用户占用了 3889 段，也不会造成误判。
    """
    from anima.cognitive.bot_social import bot_score, classify_bot_score

    m = {"user_id": 3889000001, "nickname": "普通人", "card": "名片",
         "level": "50", "qq_level": 5, "is_robot": False,
         "role": "member", "join_time": 1, "last_sent_time": 9}
    s, h = bot_score(m)
    assert s < 60, f"号段单独就确认了：{s} {h}"


def test_score_returns_explainable_factors():
    """评分必须能解释「凭什么」—— 每个因子带分值和名字。"""
    from anima.cognitive.bot_social import bot_score

    m = {"user_id": 3889024489, "nickname": "可怜BOT", "card": "",
         "level": "0", "qq_level": 0, "is_robot": True,
         "role": "member", "join_time": 1, "last_sent_time": 1}
    s, hits = bot_score(m)
    assert hits, "没有解释因子"
    assert s == sum(int(x.rsplit("+", 1)[1].rstrip(")")) for x in hits), \
        f"分数与因子之和不一致：{s} vs {hits}"


def test_bot_score_handles_bad_input():
    """脏输入不得抛异常。"""
    from anima.cognitive.bot_social import bot_score, classify_bot_score
    assert bot_score(None) == (0, [])
    assert bot_score("not-a-dict") == (0, [])
    assert bot_score({})[0] >= 0
    assert classify_bot_score("garbage") == "human"

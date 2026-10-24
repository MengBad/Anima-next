"""P3-4：探索结果的「按群分享」必须默认关闭、且只发往来源群。

设计意图（防自授权）：
    Anima 探索的是外部公开信息，属于"未经 Owner 审阅的内容"。
    因此分享必须满足 fail-closed 默认：
      1. share_enabled 默认 False —— 不配置就永远不外发；
      2. 只在欲望自带 source_group_id 且 expression_scope 属于 {group, global} 时考虑；
      3. share_groups 非空时形成白名单，非白名单群直接拒绝；
      4. 分数阈值 + 每日限额双重限流；
      5. 只会 propose_agenda 到来源群 —— 绝不广播、绝无 wildcard。

本文件锁定上述五条，防止未来重构把"默认关闭"改成"默认打开"。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from anima.cognitive.exploration import ExplorationManager, ExplorationNote
from anima.core.config import ExplorationConfig
from anima.core.persistence import PersistenceProvider


class Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def make_manager(tmp_path, **overrides):
    """构造一个只依赖 explore 分享字段的 manager（不触碰网络/LLM）。"""
    fields = {
        "enabled": True,
        "share_enabled": False,
        "daily_share_limit": 1,
        "share_min_score": 0.75,
        "share_groups": [],
    }
    fields.update(overrides)
    core = SimpleNamespace(
        config=SimpleNamespace(
            persistence=SimpleNamespace(data_dir=str(tmp_path)),
            exploration=SimpleNamespace(**fields),
        ),
        persistence=PersistenceProvider(str(tmp_path)),
        audit=Audit(),
        agendas=[],
    )
    core.propose_agenda = lambda group_id, text, **kw: core.agendas.append(
        (group_id, text, kw)
    )
    manager = ExplorationManager(core, core.config.exploration)
    return core, manager


def make_note(*, score=0.9, group_id=123):
    return ExplorationNote(
        id="explore_test", topic="测试主题", title="测试标题",
        summary="测试摘要内容", source="B站热门",
        url="https://www.bilibili.com/video/BV1TEST",
        value_score=score, source_group_id=group_id,
    )


def make_desire(*, group_id=123, scope="group"):
    return SimpleNamespace(source_group_id=group_id, expression_scope=scope)


def _pin_today(manager):
    """把 manager 的「当天」钉成今天，避免 _reset_day() 清掉手工设置的计数器。

    _reset_day 用北京时区日期判断是否跨天；计数器测试必须在同一天内才有意义。
    """
    from datetime import datetime
    from anima.cognitive.exploration import _BJ

    manager.state["day"] = datetime.now(_BJ).date().isoformat()
    return manager


# --------------------------------------------------------------------------
# 1. 默认值必须关闭
# --------------------------------------------------------------------------

def test_config_default_is_share_disabled():
    """出厂默认：share_enabled=False，白名单为空（不配置=不外发）。"""
    cfg = ExplorationConfig()
    assert cfg.share_enabled is False
    assert cfg.share_groups == []
    assert cfg.daily_share_limit >= 1
    assert 0.0 < cfg.share_min_score <= 1.0


def test_share_disabled_by_default_even_with_group_desire(tmp_path):
    """最关键的回归：即使欲望明确带群、带 group scope，默认也不分享。"""
    core, manager = make_manager(tmp_path)  # share_enabled 默认 False
    note = make_note()
    manager._maybe_share(note, make_desire())

    assert core.agendas == []
    assert note.shared is False
    assert int(manager.state.get("shared_today", 0) or 0) == 0


# --------------------------------------------------------------------------
# 2. 缺少来源群 / scope 不合法 —— 一律不分享
# --------------------------------------------------------------------------

def test_enabled_but_without_source_group_never_shares(tmp_path):
    core, manager = make_manager(tmp_path, share_enabled=True)
    note = make_note()
    manager._maybe_share(note, make_desire(group_id=None))

    assert core.agendas == []
    assert note.shared is False


def test_enabled_but_internal_scope_never_shares(tmp_path):
    """scope=internal 表示"自己知道就好"，绝不能外发。"""
    core, manager = make_manager(tmp_path, share_enabled=True)
    for scope in ("internal", "private", ""):
        note = make_note()
        manager._maybe_share(note, make_desire(scope=scope))
    assert core.agendas == []
    assert manager.state.get("shared_today", 0) == 0


def test_enabled_but_no_desire_never_shares(tmp_path):
    """没有欲望来源（自主内驱探索）时不分享。"""
    core, manager = make_manager(tmp_path, share_enabled=True)
    note = make_note()
    manager._maybe_share(note, None)
    assert core.agendas == []


# --------------------------------------------------------------------------
# 3. 按群：白名单 + 来源群定向
# --------------------------------------------------------------------------

def test_non_allowlisted_group_is_refused(tmp_path):
    """share_groups 非空 => 白名单生效；不在名单里的群拒绝。"""
    core, manager = make_manager(
        tmp_path, share_enabled=True, share_groups=[111, 222]
    )
    note = make_note(group_id=999)
    manager._maybe_share(note, make_desire(group_id=999))

    assert core.agendas == []
    assert note.shared is False


def test_allowlisted_group_is_allowed(tmp_path):
    core, manager = make_manager(tmp_path, share_enabled=True, share_groups=[111])
    note = make_note(group_id=111)
    manager._maybe_share(note, make_desire(group_id=111))

    assert len(core.agendas) == 1
    assert core.agendas[0][0] == 111
    assert note.shared is True


def test_empty_allowlist_follows_source_group_only(tmp_path):
    """白名单为空=仅沿欲望来源群，仍然只发那一个群，绝不广播。"""
    core, manager = make_manager(tmp_path, share_enabled=True, share_groups=[])
    note = make_note(group_id=456)
    manager._maybe_share(note, make_desire(group_id=456))

    assert [row[0] for row in core.agendas] == [456]


def test_share_never_targets_broadcast_or_wildcard(tmp_path):
    """分享目标恒为具体群号（正整数），排除 0/负数/None。"""
    core, manager = make_manager(tmp_path, share_enabled=True, share_groups=[777])
    manager._maybe_share(make_note(group_id=777), make_desire(group_id=777))

    assert len(core.agendas) == 1
    target = core.agendas[0][0]
    assert isinstance(target, int) and target > 0


# --------------------------------------------------------------------------
# 4. 限流：分数阈值 + 每日限额
# --------------------------------------------------------------------------

def test_below_score_threshold_is_refused(tmp_path):
    core, manager = make_manager(
        tmp_path, share_enabled=True, share_min_score=0.75
    )
    note = make_note(score=0.74)
    manager._maybe_share(note, make_desire())

    assert core.agendas == []
    assert note.shared is False


def test_exactly_at_score_threshold_is_allowed(tmp_path):
    """边界：等于阈值即放行（>= 语义，不是 >）。"""
    core, manager = make_manager(
        tmp_path, share_enabled=True, share_min_score=0.75
    )
    manager._maybe_share(make_note(score=0.75), make_desire())
    assert len(core.agendas) == 1


def test_daily_share_limit_is_enforced(tmp_path):
    core, manager = make_manager(
        tmp_path, share_enabled=True, daily_share_limit=1
    )
    first = make_note()
    manager._maybe_share(first, make_desire())
    assert first.shared is True

    second = make_note()
    manager._maybe_share(second, make_desire())

    assert len(core.agendas) == 1  # 第二条被当日限额挡住
    assert second.shared is False
    assert int(manager.state.get("shared_today", 0) or 0) == 1


def test_zero_daily_limit_disables_sharing(tmp_path):
    """daily_share_limit=0 语义为"当天完全不分享"（外发闸门 → fail-closed）。"""
    core, manager = make_manager(
        tmp_path, share_enabled=True, daily_share_limit=0
    )
    manager._maybe_share(make_note(), make_desire())
    assert core.agendas == []


# --------------------------------------------------------------------------
# 4b. 同一个 0，两个字段语义相反 —— 必须双向锁死，避免被"统一"掉
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_exploration_daily_limit_means_unlimited(tmp_path, monkeypatch):
    """daily_limit=0 语义为"不限制"，与 image_generation.daily_limit 一致。

    历史 bug：原实现 `getattr(..., 3) or 3` 中 0 是 falsy，被静默吞成 3，
    使 0 成为既非"不限"也非"关闭"的未定义值（实际等于限额 3）。

    注意：必须把 state["day"] 钉成今天，否则 _reset_day() 会把计数器清零，
    测试就会"因为错误的原因通过"——旧代码同样能过，等于没测。
    """
    core, manager = make_manager(tmp_path, daily_limit=0)
    _pin_today(manager)
    manager.state["explored_today"] = 999  # 远超任何正数限额
    called = []

    async def pick_topic():
        return None, "测试主题"

    async def collect(topic):
        called.append(topic)
        return []

    monkeypatch.setattr(manager, "_pick_topic", pick_topic)
    monkeypatch.setattr(manager, "_collect", collect)

    # 🔴 2026-10-24：用 force=True —— 本测试验证的是**配额语义**，
    #   不该被「活跃时段」闸门（8–23 点）挡住；否则凌晨跑测试会假失败。
    await manager.maybe_run(force=True)
    assert called, "daily_limit=0 应视为不限制，配额不得拦截"


@pytest.mark.asyncio
async def test_positive_exploration_daily_limit_still_blocks(tmp_path, monkeypatch):
    """对照：正数配额必须照常拦截，确认上面不是把闸门整体拆了。"""
    core, manager = make_manager(tmp_path, daily_limit=3)
    _pin_today(manager)
    manager.state["explored_today"] = 3  # 已用满
    called = []

    async def collect(topic):
        called.append(topic)
        return []

    monkeypatch.setattr(manager, "_collect", collect)

    result = await manager.maybe_run()
    assert result is None
    assert called == [], "达到 daily_limit 后不应再发起探索"


def test_quota_counter_survives_within_same_day(tmp_path):
    """复核 _reset_day 不会在同一天清零——上面两条测试的前提。"""
    core, manager = make_manager(tmp_path, daily_limit=3)
    _pin_today(manager)
    manager.state["explored_today"] = 2
    manager._reset_day()
    assert manager.state["explored_today"] == 2


# --------------------------------------------------------------------------
# 5. 成功路径的账目一致性
# --------------------------------------------------------------------------

def test_successful_share_marks_state_and_counter(tmp_path):
    """note 已序列化入 state，必须同步把最后一条标记为 shared。"""
    core, manager = make_manager(tmp_path, share_enabled=True)
    note = make_note()
    manager.state.setdefault("notes", []).append(
        {"id": note.id, "shared": False}
    )
    manager._maybe_share(note, make_desire())

    assert note.shared is True
    assert manager.state["notes"][-1]["shared"] is True
    assert int(manager.state["shared_today"]) == 1


def test_share_agenda_is_low_priority_expiring_and_sourced(tmp_path):
    """分享只是"顺嘴提一嘴"：低优先级 + 会过期 + 标注来源。"""
    core, manager = make_manager(tmp_path, share_enabled=True)
    manager._maybe_share(make_note(), make_desire())

    _, _, kwargs = core.agendas[0]
    assert kwargs.get("source") == "external_exploration"
    assert kwargs.get("priority", 1.0) < 0.7
    assert "expires_at" in kwargs and kwargs["expires_at"] > 0


def test_share_agenda_text_is_not_news_anchor_style(tmp_path):
    """文案约束：口语化、不新闻播报腔。"""
    core, manager = make_manager(tmp_path, share_enabled=True)
    manager._maybe_share(make_note(), make_desire())

    text = core.agendas[0][1]
    assert "测试标题" in text
    assert "新闻播报" in text  # 提示词内嵌的负向约束仍在


# ── 无来源群的好奇探索：仅在 share_groups 显式指定时才分享（2026-10-24）──


def _mk_manager(tmp_path, *, share_enabled=True, share_groups=None,
                daily_share_limit=1, share_min_score=0.75):
    """构造只用于 _maybe_share 的最小 manager。"""
    import types
    from anima.cognitive.exploration import ExplorationManager

    cfg = types.SimpleNamespace(
        share_enabled=share_enabled,
        daily_share_limit=daily_share_limit,
        share_min_score=share_min_score,
        share_groups=list(share_groups or []),
        sources=["bilibili_popular"],
    )
    proposed = []

    core = types.SimpleNamespace(
        config=types.SimpleNamespace(persistence=types.SimpleNamespace(data_dir=str(tmp_path)),
                                     exploration=cfg),
        propose_agenda=lambda gid, intent, **kw: proposed.append((gid, intent, kw)),
    )
    m = ExplorationManager(core, cfg)
    return m, proposed


def _note(score=0.9, title="有意思的事", summary="摘要"):
    from anima.cognitive.exploration import ExplorationNote
    return ExplorationNote(
        id="n1", topic="t", title=title, summary=summary,
        source="bilibili_popular", url="https://example.com", value_score=score,
    )


def _desire(group_id=None, scope="internal"):
    import types
    return types.SimpleNamespace(source_group_id=group_id, expression_scope=scope)


def test_internal_curiosity_not_shared_without_allowlist(tmp_path):
    """🔴 没配 share_groups ⇒ **绝不凭空选群**，维持原行为（不分享）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=None)
    m._maybe_share(_note(), _desire())
    assert proposed == [], "没有显式目标群时不得分享"


def test_internal_curiosity_shared_when_allowlist_set(tmp_path):
    """🔴 生产实证的修复：`_make_curiosity_desire()` 生成的欲望一律
    `expression_scope="internal"` 且无 source_group_id
    ⇒ 老判定下「探索→分享」链**永不可达**（shared_today 恒 0、164 条笔记全未分享）。

    修法：owner 在 `share_groups` 显式指定目标群时，用它作落点。
    """
    m, proposed = _mk_manager(tmp_path, share_groups=[727631526])
    m._maybe_share(_note(score=0.9), _desire())
    assert len(proposed) == 1
    assert proposed[0][0] == 727631526


def test_allowlist_choice_is_deterministic(tmp_path):
    """多个候选群时取最小值（不随 set 迭代顺序漂移）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=[999, 111, 555])
    m._maybe_share(_note(), _desire())
    assert proposed[0][0] == 111


def test_below_threshold_still_blocked(tmp_path):
    """分数不够仍不分享（阈值语义不变）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=[111], share_min_score=0.9)
    m._maybe_share(_note(score=0.5), _desire())
    assert proposed == []


def test_daily_limit_still_applies(tmp_path):
    """每日上限语义不变。"""
    m, proposed = _mk_manager(tmp_path, share_groups=[111], daily_share_limit=1)
    m._maybe_share(_note(), _desire())
    m._maybe_share(_note(), _desire())
    assert len(proposed) == 1, "超过每日上限不得再分享"


def test_explicit_group_desire_unaffected(tmp_path):
    """有来源群的欲望走原路径（不受新分支影响）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=[111])
    m._maybe_share(_note(), _desire(group_id=777, scope="group"))
    # 777 不在 allowlist ⇒ 不分享（原语义）
    assert proposed == []


# ── desire=None 也可分享（2026-10-24，164 条 0 分享的真正原因）──────────


def test_share_when_desire_is_none_but_allowlist_set(tmp_path):
    """🔴 生产实证：164 条笔记 0 条分享的**真正原因**不是评分，是这里 ——

    `_pick_topic()` 在没有好奇欲望时返回 `(None, 话题)`，
    而 `_generate_curiosity_desire()` 有 **12 小时冷却**
    ⇒ 一天里大部分探索 `desire=None` ⇒ 被 `if desire is None: return` 直接挡掉。

    修法：desire=None 时，只要 owner 配了 `share_groups` 且分数够，就允许分享。
    内容质量由**模型自评分**把关，不再依赖「有没有欲望」这个与内容无关的条件。
    """
    m, proposed = _mk_manager(tmp_path, share_groups=[727631526])
    note = _note(score=0.85)
    m._maybe_share(note, None)          # desire 为 None
    assert len(proposed) == 1, "有显式目标群时，desire=None 也应能分享"
    assert proposed[0][0] == 727631526


def test_no_share_when_desire_none_and_no_allowlist(tmp_path):
    """没配 allowlist ⇒ 仍不分享（绝不凭空选群）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=None)
    m._maybe_share(_note(score=0.95), None)
    assert proposed == []


def test_desire_none_still_respects_threshold(tmp_path):
    """desire=None 时阈值仍然生效（质量把关不能松）。"""
    m, proposed = _mk_manager(tmp_path, share_groups=[111], share_min_score=0.9)
    m._maybe_share(_note(score=0.6), None)
    assert proposed == [], "分数不够仍不得分享"


def test_desire_none_still_respects_daily_limit(tmp_path):
    m, proposed = _mk_manager(tmp_path, share_groups=[111], daily_share_limit=1)
    m._maybe_share(_note(score=0.95), None)
    m._maybe_share(_note(score=0.95), None)
    assert len(proposed) == 1, "每日上限仍生效"

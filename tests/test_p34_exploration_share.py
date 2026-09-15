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

    await manager.maybe_run()
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

"""自治自检建单的**可自愈性**契约（2026-10-21）。

## 背景（生产实证：34 天僵尸单）

`autonomy._tick` 的自治自检此前对所有 `DevIssue` 一律建 `diagnostic` 待办。
但自愈机制 `healed_fault_tickets` 有三道硬条件：

1. 必须是 `fault_ticket`（旧代码 `if kind != "fault_ticket": continue`）；
2. `proposal` 里必须有 `date` 与 `signals`；
3. **每个 signal 必须在 `_FAULT_RULES` 规则表内**，否则 `sig not in thresholds`
   ⇒ `continue`。

而建单时 proposal 是空的 ⇒ 条件 2/3 永不满足 ⇒ **一旦开出即终身挂着**。

生产后果（2026-10-21 owner 首次收到主动推送时发现）：
09-17 14:58 / 16:58 / 20:59 六小时内连开 **3 张**「回复质量退化」单
（标题带计数 `3/18` `4/23` `4/24`，旧去重是全等比较 ⇒ 计数一变即判新单），
加上 1 张 health_alerts 单，共 **4 张 diagnostic 挂了 34 天**，每天污染推送。

## 本次修法

* 只对**能映射到 `_FAULT_RULES` 真实键**的议题建单（其余仅记日志，不建单）；
* 建单时补 `date` + `signals` 元数据，使其可被自愈判定；
* 去重改用**主题键**（剥掉计数），同病灶只保留一张在办单。

## 本测试锁死的边界

1. `_issue_signal_key` 的返回值**要么为空、要么必在规则表内**（绝不映射到表外键）；
2. 「回复质量退化」这类不在规则表的议题 → **不建单**（返回空键）；
3. 计数不同的同一病灶 → 主题键相同（不会被判成两张单）；
4. 已知可自愈议题 → 映射到正确键。
"""
import pytest

from anima.cognitive.controlled_iteration import _FAULT_RULES
from anima.core.autonomy import _issue_signal_key, _issue_topic_key

VALID_KEYS = {str(r["key"]) for r in _FAULT_RULES}


# ── 1. 绝不允许映射到规则表外的键 ─────────────────────────────────────


@pytest.mark.parametrize("title", [
    "回复质量退化：工具失败 3/18，不可用回复 0",
    "[health_alerts] 窗口内检测到 1 次权限漂移事件（安全关注）",
    "某个完全未知的议题",
    "",
    "应回却空：进入回复路径但无可用正文",
    "工具空转：同工具同参重复调用",
    "QQ 出站失败",
])
def test_signal_key_is_either_empty_or_valid(title):
    """🔴 核心：映射到表外键 == 永远不自愈 == 又造僵尸单。"""
    key = _issue_signal_key(title)
    assert key == "" or key in VALID_KEYS, (
        f"映射出了规则表里不存在的键 {key!r}（标题={title!r}）——"
        f"它永远无法被 healed_fault_tickets 关闭。有效键：{sorted(VALID_KEYS)}"
    )


# ── 2. 不可自愈的议题必须返回空 → 调用方据此不建单 ──────────────────────


@pytest.mark.parametrize("title", [
    "回复质量退化：工具失败 3/18，不可用回复 0",
    "回复质量退化：工具失败 4/23，不可用回复 0",
    "[health_alerts] 窗口内检测到 1 次权限漂移事件（安全关注）",
])
def test_unhealable_issues_return_empty_key(title):
    """这些指标不属 `_FAULT_RULES` ⇒ 不该建单（否则又是终身僵尸）。"""
    assert _issue_signal_key(title) == "", (
        f"{title!r} 不可自愈，必须返回空键以阻止建单"
    )


# ── 3. 可自愈议题映射正确 ─────────────────────────────────────────────


@pytest.mark.parametrize("title,expected", [
    ("应回却空：进入回复路径但无可用正文", "empty_should_reply"),
    ("工具空转：同工具同参重复调用", "tool_spins"),
    ("ReAct 频繁触顶", "react_max_rounds"),
    ("QQ 出站失败", "outbound_failures"),
    ("身份泄漏拦截", "identity_leaks"),
    ("B 站主页链接处理失败", "space_link_fail"),
])
def test_known_issues_map_to_expected_keys(title, expected):
    assert _issue_signal_key(title) == expected
    assert expected in VALID_KEYS, f"映射目标 {expected} 必须在规则表内"


# ── 4. 主题去重：计数不同 → 同一主题 ───────────────────────────────────


def test_topic_key_ignores_counts():
    """「3/18」与「4/23」是同一个病灶，必须判为同主题（旧去重失效的根因）。"""
    a = _issue_topic_key("回复质量退化：工具失败 3/18，不可用回复 0")
    b = _issue_topic_key("回复质量退化：工具失败 4/23，不可用回复 0")
    c = _issue_topic_key("回复质量退化：工具失败 4/24，不可用回复 0")
    assert a == b == c, f"同一病灶应判同主题：{a!r} / {b!r} / {c!r}"


def test_topic_key_distinguishes_different_issues():
    a = _issue_topic_key("回复质量退化：工具失败 3/18")
    b = _issue_topic_key("QQ 出站失败")
    assert a != b, "不同议题不得误判为同一主题（否则会漏报）"


def test_topic_key_handles_empty():
    assert _issue_topic_key("") == ""
    assert _issue_topic_key(None) == ""

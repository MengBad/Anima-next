"""P0（2026-09-18）：失败结果「保留原因」——消除「额度用尽」被误读成「接口挂了」。

生产实测背景
------------
`config.x_browser.search_daily_limit = 10`。额度用尽时 `x_browser.search()` 毫秒级
返回 ``{"ok": False, "note": "今日搜索额度已用尽"}``（**不启动浏览器 → 零日志**）。
该 note 经 ``x_search.py`` 包成 ``"X 搜索未完成：今日搜索额度已用尽"``，再被
``normalize_tool_result()`` 命中 ``_TOOL_FAILURE_PREFIXES`` 中的 ``"X 搜索未完成"``
→ **整句替换成 17 字通用串** ``"工具暂时不可用，未获得可验证结果。"``。

后果：模型**无法区分**「额度用完（次日自动恢复）」与「接口真挂了（故障）」，
于是对用户输出「推特检索接口现在挂了调不通」——**错误归因**。

本文件钉死的新行为（**只改「模型可见文本」，不改任何判成功/失败语义**）：

1. 工具层用 ``tool_unavailable(reason)`` 构造失败结果；``reason`` 必须取自
   白名单 ``TOOL_REASON_*``，否则退化为通用串；
2. ``normalize_tool_result()`` 对白名单原因**原样放行**，其余仍折叠为通用串；
3. ``tool_result_ok()`` 仍判该结果为**失败**（``ok=False``）——审计、遥测、
   ``reply_quality.tool_failures`` 口径**逐字节不变**；
4. 跨层一致：governance ``OutcomeNormalizer`` 仍归 FAILURE；
5. **异常文本 / 堆栈 / 白名单外原因永不进入模型可见文本**。

R1 边界：这是 owner 授权的**显式模型面行为变更**（09-18 P0）。只新增「白名单原因
透传」一条通道，未删除、未改名任何既有失败前缀。
"""

from __future__ import annotations

import pytest

from anima.tools import (
    _SAFE_TOOL_FAILURE,
    TOOL_REASON_X_QUOTA_SEARCH,
    TOOL_REASON_X_QUOTA_TIMELINE,
    TOOL_REASON_X_RISK_COOLDOWN,
    normalize_tool_result,
    tool_result_ok,
    tool_unavailable,
)

#: 既有失败前缀面（``tests/test_react.py`` 断言过的形态）——改完必须零回归。
_LEGACY_FAILURES = (
    "X 搜索失败：Page.goto: net::ERR_CONNECTION_CLOSED",
    "X 搜索未完成：X 暂被风控拦截（页面空壳）",
    "X 搜索模块不可用（x_browser 未加载）。",
    "X 浏览器未启用（config.x_browser.enabled=false）。",
    "[沙箱] 命令 'ls' 不在安全白名单中",
    "错误: 你的权限不足",
)


# ---------------------------------------------------------------------------
# RED 首测（用户谓词）：额度用尽必须能被模型看见
# ---------------------------------------------------------------------------
def test_red_quota_reason_visible_to_model():
    """额度用尽 → 模型可见文本里必须出现「额度」，而不是通用串。"""
    kept = tool_unavailable(TOOL_REASON_X_QUOTA_SEARCH)
    text, ok = normalize_tool_result(kept)
    assert "额度" in text, f"额度原因被吞掉了: {text!r}"
    assert text != _SAFE_TOOL_FAILURE
    assert ok is False  # 语义不变：它仍然是失败


def test_red_timeline_and_cooldown_also_visible():
    for reason, needle in ((TOOL_REASON_X_QUOTA_TIMELINE, "额度"),
                           (TOOL_REASON_X_RISK_COOLDOWN, "风控")):
        text, ok = normalize_tool_result(tool_unavailable(reason))
        assert needle in text and ok is False


# ---------------------------------------------------------------------------
# 白名单是硬边界：非白名单原因 -> 通用串（fail-closed）
# ---------------------------------------------------------------------------
def test_non_whitelisted_reason_collapses_to_generic():
    for bad in ("X 搜索异常: KeyError('a')",
                "内部堆栈 0xdeadbeef",
                "工具暂时不可用（原因：rc=-9）",
                "",
                None):
        assert tool_unavailable(bad) == _SAFE_TOOL_FAILURE


def test_exception_text_never_reaches_model():
    """异常文本永不进入模型可见文本（伪造原因码 / 堆栈 / 裸异常串）。"""
    for bad in ("Traceback (most recent call last):\n  File \"x.py\", line 1",
                "工具暂时不可用（原因：X 搜索异常: boom）",
                "工具暂时不可用（原因：内部堆栈 0xdeadbeef）",
                "工具暂时不可用（原因：）"):
        text, ok = normalize_tool_result(bad)
        assert text == _SAFE_TOOL_FAILURE, f"泄漏: {text!r}"
        assert ok is False


# 已收口（D-18，2026-09-18）：
# ``_TOOL_FAILURE_PREFIXES``（冻结表）收的是 ``X 搜索失败 / X 搜索未完成 /
# X 搜索模块不可用 / X 浏览器未启用``，**不含** ``X 搜索异常``。台账曾把它定性为
# 「属产品判断」并由 owner 决定 —— 该定性被推翻两次：
#   (a) 它产自 ``x_browser`` 的 ``except Exception`` 分支，是**客观异常信号**，
#       不是措辞偏好；
#   (b) 它**不能**进 ``_TOOL_FAILURE_PREFIXES_EXT``（那张表的语义是"判失败但保留
#       文本"，放进去会把 ``TypeError: ...`` 照样送进模型上下文）。
#
# 正解是独立新表 ``_TOOL_COLLAPSE_PREFIXES``（判失败 **且** 折叠），冻结表逐字节未动。
def test_exception_prefixes_are_registered_as_collapsing():
    from anima.tools import (
        _TOOL_COLLAPSE_PREFIXES, _TOOL_FAILURE_PREFIXES, _TOOL_FAILURE_PREFIXES_EXT,
    )

    # 冻结表仍然逐字节未动（F8.11 R1）
    assert not any(p.startswith("X 搜索异常") for p in _TOOL_FAILURE_PREFIXES)
    # 也不在"保留文本"的 EXT 表里（那会导致异常文本入上下文）
    assert not any(p.startswith("X 搜索异常") for p in _TOOL_FAILURE_PREFIXES_EXT)
    # 而在"必须折叠"表里
    for prefix in ("X 搜索异常", "X 时间线异常"):
        assert prefix in _TOOL_COLLAPSE_PREFIXES, f"{prefix} 未收进折叠表"

    # 裸串：判失败 **且** 异常文本被折叠掉
    for raw in ("X 搜索异常: TypeError: boom",
                "X 时间线异常: ConnectionResetError(104, 'Connection reset by peer')"):
        assert tool_result_ok(raw) is False, f"裸异常串仍被当成功: {raw!r}"
        text, ok = normalize_tool_result(raw)
        assert ok is False
        assert text == _SAFE_TOOL_FAILURE, f"异常文本泄漏: {text!r}"

    # 反向：EXT 表的语义（保留文本）没有被这次改动影响
    ext_sample = "计算出错：除数为零"     # system.py:37
    text, ok = normalize_tool_result(ext_sample)
    assert ok is False and text == ext_sample, "EXT 表应保留文本（信息量）"


# ---------------------------------------------------------------------------
# 既有失败语义零回归
# ---------------------------------------------------------------------------
def test_legacy_failure_semantics_unchanged():
    for s in _LEGACY_FAILURES:
        assert tool_result_ok(s) is False, s
        assert normalize_tool_result(s)[0] == _SAFE_TOOL_FAILURE, s
    # 成功面不受影响
    assert tool_result_ok("X 搜索结果（已过滤政治/违法内容）：") is True
    assert tool_result_ok("正常结果") is True


def test_cross_layer_governance_still_failure():
    """跨层一致：governance ``OutcomeNormalizer`` 仍把「保留原因」串归 FAILURE。"""
    from anima.capability.health.normalizer import (
        NormalizedExecutionOutcome,
        OutcomeNormalizer,
    )

    kept = tool_unavailable(TOOL_REASON_X_QUOTA_SEARCH)
    assert OutcomeNormalizer.normalize_text(kept)[0] is NormalizedExecutionOutcome.FAILURE


# ---------------------------------------------------------------------------
# 端到端：x_search 把 x_browser 的 note 映射成白名单原因码
# ---------------------------------------------------------------------------
class _QuotaSess:
    enabled = True
    proxy = ""

    def __init__(self, note: str, ok: bool = False) -> None:
        self._note = note
        self._ok = ok

    async def search(self, query, limit=5, fresh=True, images_only=False):
        return {"ok": self._ok, "logged_in": False, "tweets": [],
                "tweet_images": [], "fresh_images": [], "note": self._note}


async def _call_x_search(monkeypatch, note, ok=False):
    import anima.cognitive.x_browser as xb
    from anima.tools.builtin.x_search import tool_x_search

    monkeypatch.setattr(xb, "get_x_browser", lambda: _QuotaSess(note, ok))
    return await tool_x_search("男娘图", want_images=True)


async def test_e2e_quota_note_reaches_model(monkeypatch):
    raw = await _call_x_search(monkeypatch, "今日搜索额度已用尽")
    assert tool_result_ok(raw) is False
    text, ok = normalize_tool_result(raw)
    assert ok is False
    assert "额度" in text, f"端到端被吞: {text!r}"


async def test_e2e_unmapped_note_still_collapses(monkeypatch):
    """未定性的 note（如异常文本）仍走老路径 → 通用串，不泄漏。"""
    raw = await _call_x_search(monkeypatch, "X 搜索异常: KeyError('boom')")
    assert normalize_tool_result(raw)[0] == _SAFE_TOOL_FAILURE


async def test_e2e_cooldown_note_reaches_model(monkeypatch):
    raw = await _call_x_search(monkeypatch, "X 暂被风控拦截，约 240s 后恢复（会话级退避）")
    text, ok = normalize_tool_result(raw)
    assert ok is False and "风控" in text


async def test_e2e_success_path_unchanged(monkeypatch):
    """成功路径（ok=True）不经过失败分支，行为逐字节不变。"""
    import anima.cognitive.x_browser as xb
    from anima.tools.builtin.x_search import tool_x_search

    class _OkSess:
        enabled = True
        proxy = ""

        async def search(self, query, limit=5, fresh=True, images_only=False):
            return {"ok": True, "logged_in": True,
                    "tweets": [{"text": "hi", "url": "https://x.com/a/1"}],
                    "tweet_images": [], "fresh_images": [], "note": ""}

    monkeypatch.setattr(xb, "get_x_browser", lambda: _OkSess())
    out = await tool_x_search("测试")
    assert tool_result_ok(out) is True


@pytest.mark.parametrize("note,needle", [
    ("x_browser 未启用", "未启用"),
    ("未登录态（时间线不可见，请先人工登录）", "登录"),
])
async def test_e2e_disabled_and_not_logged_in(monkeypatch, note, needle):
    raw = await _call_x_search(monkeypatch, note)
    assert tool_result_ok(raw) is False
    text, ok = normalize_tool_result(raw)
    assert ok is False and needle in text, f"未定性原因被吞: {text!r}"


async def test_e2e_session_disabled_returns_whitelisted_reason(monkeypatch):
    """``session.enabled=False`` 是**另一条早返回**（在 ``search()`` 之前），
    曾经返回裸串被折叠；现在同样走白名单原因码。"""
    import anima.cognitive.x_browser as xb
    from anima.tools.builtin.x_search import tool_x_search

    class _Off:
        enabled = False
        proxy = ""

        async def search(self, *a, **k):  # pragma: no cover - 不该被调用
            raise AssertionError("enabled=False 时不应进入 search()")

    monkeypatch.setattr(xb, "get_x_browser", lambda: _Off())
    raw = await tool_x_search("男娘图")
    assert tool_result_ok(raw) is False
    text, ok = normalize_tool_result(raw)
    assert ok is False and "未启用" in text, f"未启用原因被吞: {text!r}"

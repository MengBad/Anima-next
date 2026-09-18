"""X 每日额度的**持久性**守卫（D-20，2026-09-18）。

背景（为什么必须有这组测试）：
    `XBrowserSession._daily` 原先是纯内存字典，进程一重启就归零。生产实测曾在
    2026-09-18 00:00–06:48 之间被重启 **6 次** ⇒ 配置上的 `search_daily_limit: 10`
    的事实上限不成立，且用尽时返回是「毫秒级、零日志」的静默失败 —— 模型据此误判
    「接口挂了」。本组测试钉住三件事：

    1. 计数跨实例（≈跨重启）续用；
    2. 跨日归零、损坏文件容错，且两者都**不留静默**；
    3. 额度用尽必须留一条显式 warning 日志（每天一次，不刷屏）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from anima.cognitive.x_browser import XBrowserSession


class _Cfg:
    """最小 config 形状：只提供 XBrowserSession 限流路径真正读的字段。"""

    enabled = True
    proxy = ""
    search_daily_limit = 3
    timeline_daily_limit = 2


def _mk(tmp_path: Path, **over) -> XBrowserSession:
    cfg = _Cfg()
    cfg.data_dir = str(tmp_path)
    for k, v in over.items():
        setattr(cfg, k, v)
    return XBrowserSession(cfg)


def _quota_file(tmp_path: Path) -> Path:
    return tmp_path / "daily_quota.json"


def test_quota_is_persisted_and_survives_new_instance(tmp_path):
    """核心回归：计数必须跨实例续用（修复前纯内存 ⇒ 必然失败）。"""
    s1 = _mk(tmp_path)
    s1._bump("search")
    s1._bump("search")
    assert _quota_file(tmp_path).exists(), "计数未落盘"

    s2 = _mk(tmp_path)  # 模拟进程重启后重新构造会话
    assert s2._daily["search"] == 2, f"新实例未续用计数: {s2._daily}"
    assert s2._within_limit("search") is True   # 2 < 3
    s2._bump("search")
    assert s2._within_limit("search") is False  # 3 >= 3 触顶


def test_quota_exhausted_crosses_instances(tmp_path):
    """一个实例把额度用完，另一个实例必须也看到「已用尽」。"""
    s1 = _mk(tmp_path, search_daily_limit=1)
    assert s1._within_limit("search") is True
    s1._bump("search")
    assert s1._within_limit("search") is False

    s2 = _mk(tmp_path, search_daily_limit=1)
    assert s2._within_limit("search") is False, "跨实例未继承「已用尽」状态"


def test_quota_resets_on_new_day(tmp_path):
    """跨日归零：昨天用满不影响今天。"""
    _quota_file(tmp_path).write_text(
        json.dumps({"date": "2000-01-01", "search": 999, "timeline": 999}),
        encoding="utf-8",
    )
    s = _mk(tmp_path)
    assert s._daily["date"] == time.strftime("%Y-%m-%d")
    assert s._daily["search"] == 0
    assert s._within_limit("search") is True


def test_corrupt_quota_file_is_tolerated_and_logged(tmp_path, caplog):
    """损坏文件：不抛异常、不锁死能力，但必须留日志（把静默变有痕）。"""
    _quota_file(tmp_path).write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="anima.x_browser"):
        s = _mk(tmp_path)
    assert s._daily["search"] == 0
    assert s._within_limit("search") is True
    assert any("额度计数读取失败" in r.getMessage() for r in caplog.records), \
        "损坏时应留 warning（不得静默归零）"


def test_exhaustion_emits_explicit_warning_once(tmp_path, caplog):
    """用尽必须留显式日志，且每天只告警一次（防刷屏）。"""
    s = _mk(tmp_path, search_daily_limit=1)
    s._bump("search")
    with caplog.at_level(logging.WARNING, logger="anima.x_browser"):
        assert s._within_limit("search") is False
        first = [r for r in caplog.records if "今日额度已用尽" in r.getMessage()]
        assert len(first) == 1, "用尽时未留显式日志"
        assert "1/1" in first[0].getMessage()
        # 再次检查不应重复告警
        assert s._within_limit("search") is False
        again = [r for r in caplog.records if "今日额度已用尽" in r.getMessage()]
        assert len(again) == 1, "同一 kind 每天应只告警一次"


def test_zero_limit_means_unlimited_and_writes_nothing(tmp_path):
    """limit<=0 = 不限制（与 image_generation.daily_limit 语义一致），且不应写计数文件。"""
    s = _mk(tmp_path, search_daily_limit=0)
    assert s._within_limit("search") is True
    s._bump("search")
    assert not _quota_file(tmp_path).exists(), "不限制时不应产生计数文件"


def test_concurrent_instances_merge_instead_of_overwrite(tmp_path):
    """两个实例（cli.py:806 与惰性单例）共用 data_dir 时，计数取 max 而非互相覆盖。"""
    s1 = _mk(tmp_path)
    s2 = _mk(tmp_path)
    s1._bump("search")
    s1._bump("search")
    s1._bump("search")
    s2._bump("search")  # s2 内存里只有 1，但磁盘是 3 ⇒ 应为 4
    assert json.loads(_quota_file(tmp_path).read_text(encoding="utf-8"))["search"] == 4


def test_timeline_and_search_counters_are_independent(tmp_path):
    """两个 kind 各自独立计数，互不污染。"""
    s = _mk(tmp_path)
    s._bump("timeline")
    s._bump("timeline")
    assert s._within_limit("timeline") is False   # 2 >= 2
    assert s._within_limit("search") is True      # 未动
    data = json.loads(_quota_file(tmp_path).read_text(encoding="utf-8"))
    assert data["timeline"] == 2 and data["search"] == 0


def test_repo_data_dir_session_does_not_write_into_repo():
    """守卫：用仓库 ``data/x_browser`` 构造会话时，额度文件**不得落进仓库**。

    这是那条事故的直接回归守卫 —— 没有它，任何人重跑两次全量就会再撞一次。

    刻意写成**幂等**的：不假设仓库里没有历史残留，只断言
    (1) 会话给出的额度路径不在仓库 ``data/`` 内；
    (2) 本次 ``_bump`` 没有改动仓库内那份文件（若有）。
    """
    import pathlib
    from types import SimpleNamespace

    from anima.cognitive.x_browser import XBrowserSession

    repo_root = pathlib.Path.cwd().resolve() / "data"
    repo_file = repo_root / "x_browser" / "daily_quota.json"
    before = repo_file.read_bytes() if repo_file.exists() else None

    cfg = SimpleNamespace(enabled=True, proxy="", username="", password="",
                          data_dir="data/x_browser", search_daily_limit=10,
                          timeline_daily_limit=10, warmup_days=0, explore_keywords=[])
    session = XBrowserSession(cfg)
    session._bump("search")

    resolved = pathlib.Path(session._quota_path()).resolve()
    assert repo_root not in resolved.parents, (
        "额度路径仍落在仓库 data/ 内：%s —— 会跨测试进程累积并最终跑满上限" % resolved
    )
    after = repo_file.read_bytes() if repo_file.exists() else None
    assert after == before, "仓库 data/x_browser/daily_quota.json 被测试写入了"

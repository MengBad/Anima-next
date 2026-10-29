# -*- coding: utf-8 -*-
"""x_browser 磁盘回收（2026-10-29）。

## 问题来源（生产实测）

`data/` 总占用 **1.7G**，其中：

    x_browser/profile            1.2G   ← Chromium 缓存
      └ Default/Cache/Cache_Data   870M  ← 纯缓存
      └ Default/Code Cache/js      196M  ← 纯缓存
    x_browser/downloaded          25M   ← 495 个图，2026-09-02 起累积
    stickers                      242M

两处都**只增不减**：全仓 `grep -rn "downloaded"` 只有写入点，
`profile` 目录连名字都没出现在任何清理代码里。

对比 `anima/media.py` 早就做了正确示范
（`_DISK_CACHE_MAX_BYTES = 128MB`，超限即清）——
同一个项目里既有正例又有缺口，说明缺的是**约束**而不是认知。

## 本测试要钉住的三件事

1. 超限才清（不超限不能乱删，否则每次调用都在扫盘删文件）
2. **只删缓存、保留登录态**（Cookies / Local Storage / Network / IndexedDB）
   —— 删了登录态要重新过验证码，代价远大于省下的空间
3. 按 mtime **最旧优先**删，保留最近的图
"""
from __future__ import annotations

import os
import shutil

import pytest

from anima.cognitive.x_browser import XBrowserSession
from anima.core.config import XBrowserConfig


def _session(tmp_path) -> XBrowserSession:
    s = XBrowserSession(XBrowserConfig())
    s.data_dir = tmp_path
    s.profile_dir = tmp_path / "profile"
    return s


def _mkfile(p, size):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


# ── profile 缓存 ─────────────────────────────────────────────────────

def test_profile_cache_pruned_when_over_cap(tmp_path):
    s = _session(tmp_path)
    prof = s.profile_dir / "Default"
    _mkfile(prof / "Cache" / "Cache_Data" / "big.dat", 300 * 1024 * 1024)
    s.PROFILE_CACHE_MAX_BYTES = 256 * 1024 * 1024

    r = s._prune_profile_cache()
    assert r["before"] >= 300 * 1024 * 1024
    assert not (prof / "Cache").exists(), "纯缓存目录应被删除"
    assert r["after"] < r["before"]


def test_profile_untouched_when_under_cap(tmp_path):
    """不超限时**不得**做任何删除 —— 否则每次调用都白扫一遍盘。"""
    s = _session(tmp_path)
    prof = s.profile_dir / "Default"
    _mkfile(prof / "Cache" / "Cache_Data" / "small.dat", 1024)
    cache = prof / "Cache"
    s.PROFILE_CACHE_MAX_BYTES = 256 * 1024 * 1024

    r = s._prune_profile_cache()
    assert cache.exists(), "未超限却删了缓存"
    assert r["removed"] == 0


@pytest.mark.parametrize("keep", ["Cookies", "Local Storage", "Network",
                                  "IndexedDB", "Session Storage"])
def test_login_state_survives_prune(tmp_path, keep):
    """**登录态一个都不能丢。**

    X 的登录态在 `Cookies` / `Local Storage` / `IndexedDB` / `Network` 里；
    删了就要重新登录 + 重新过验证码。这是本修复最大的风险点。
    """
    s = _session(tmp_path)
    prof = s.profile_dir / "Default"
    _mkfile(prof / "Cache" / "Cache_Data" / "big.dat", 300 * 1024 * 1024)
    if "/" in keep:
        _mkfile(prof / keep / "state.db", 128)
    else:
        _mkfile(prof / keep, 128)
    s.PROFILE_CACHE_MAX_BYTES = 256 * 1024 * 1024

    s._prune_profile_cache()
    assert (prof / keep).exists(), f"{keep} 是登录态，被误删了"


def test_prune_never_touches_unknown_dirs(tmp_path):
    """白名单之外的新目录**默认保留**。

    Chromium 会新增各种缓存目录名（Dawn*Cache / GrShaderCache / …）。
    用黑名单的话，新名字出现时要么漏清、要么误删；
    用白名单则最坏情况是「没清干净」，而不是「删掉不该删的」。
    """
    s = _session(tmp_path)
    prof = s.profile_dir / "Default"
    _mkfile(prof / "Cache" / "Cache_Data" / "big.dat", 300 * 1024 * 1024)
    unknown = prof / "SomeFutureThing"
    _mkfile(unknown / "data.bin", 1000)
    s.PROFILE_CACHE_MAX_BYTES = 256 * 1024 * 1024

    s._prune_profile_cache()
    assert unknown.exists(), "白名单机制失效：未知目录被删了"


# ── 下载图 ───────────────────────────────────────────────────────────

def test_downloaded_pruned_oldest_first(tmp_path):
    s = _session(tmp_path)
    dl = tmp_path / "downloaded"
    dl.mkdir()
    for i in range(100):
        p = dl / f"img_{i:03d}.jpg"
        p.write_bytes(b"x" * (1024 * 1024))
        os.utime(p, (1000 + i, 1000 + i))
    s.DOWNLOADED_MAX_BYTES = 64 * 1024 * 1024

    r = s._prune_downloaded()
    assert r["deleted"] > 0
    assert r["after"] <= 64 * 1024 * 1024
    remain = sorted(p.name for p in dl.iterdir())
    assert "img_099.jpg" in remain, "最新的图必须保留"
    assert "img_000.jpg" not in remain, "应按 mtime 删最旧的"


def test_downloaded_untouched_when_under_cap(tmp_path):
    s = _session(tmp_path)
    dl = tmp_path / "downloaded"
    dl.mkdir()
    for i in range(3):
        (dl / f"a{i}.jpg").write_bytes(b"x" * 1024)
    s.DOWNLOADED_MAX_BYTES = 64 * 1024 * 1024

    r = s._prune_downloaded()
    assert r["deleted"] == 0
    assert len(list(dl.iterdir())) == 3


def test_downloaded_missing_dir_is_not_error(tmp_path):
    """目录不存在（从未下载过图）不能抛。"""
    s = _session(tmp_path)
    r = s._prune_downloaded()
    assert r["before"] == 0 and r["deleted"] == 0


# ── 总入口 ───────────────────────────────────────────────────────────

def test_prune_disk_dry_run_changes_nothing(tmp_path):
    s = _session(tmp_path)
    prof = s.profile_dir / "Default"
    _mkfile(prof / "Cache" / "Cache_Data" / "big.dat", 300 * 1024 * 1024)
    s.PROFILE_CACHE_MAX_BYTES = 256 * 1024 * 1024
    before = s._dir_bytes(s.profile_dir)

    r = s.prune_disk(dry_run=True)
    assert s._dir_bytes(s.profile_dir) == before, "dry_run 竟然真的删了文件"
    assert r["profile"]["removed"] > 0, "dry_run 也应报告可回收量"


def test_prune_disk_never_raises(tmp_path):
    """损坏的路径不能影响退出流程。"""
    s = _session(tmp_path)
    (tmp_path / "profile").mkdir()
    s.profile_dir = tmp_path / "profile"    # 空目录
    out = s.prune_disk()
    assert isinstance(out, dict)


def test_get_status_reports_disk(tmp_path):
    """WebUI 要能直接看到「这个模块在吃多少盘」。"""
    s = _session(tmp_path)
    _mkfile(s.profile_dir / "Default" / "Cache" / "x.dat", 2 * 1024 * 1024)
    (tmp_path / "downloaded").mkdir()
    (tmp_path / "downloaded" / "a.jpg").write_bytes(b"x")
    st = s.get_status()
    assert st["profile_mb"] > 0
    assert st["downloaded_count"] == 1
    assert st["profile_cap_mb"] == s.PROFILE_CACHE_MAX_BYTES // 1048576

# -*- coding: utf-8 -*-
"""`generated/` 出图目录保留期清理（2026-10-29）。

## 问题来源（生产实测 08:52）

    data/generated/   29M / 34 个日期目录   最老 2026-08-31

从上线起**零清理**。审阅发现同一个每日任务里已经有 `audit.prune(90)`
（17M / 60 个文件，正常回收），却漏了 `generated/` ——
缺的是覆盖度，不是认知。本测试把覆盖钉住。

## 三条判据

1. **过期才删**（按目录名 `YYYY-MM-DD` 判年龄，不看 mtime）
2. **非日期目录不碰**（人工放的/别的东西不能误删）
3. dry_run 不实际删除
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from anima.media_generation import ImageGenerationManager


class _Core:
    class config:
        class persistence:
            data_dir = ""


def _mgr(tmp_path) -> ImageGenerationManager:
    _Core.config.persistence.data_dir = str(tmp_path)
    return ImageGenerationManager(_Core())


def _mkday(root, day: date, size=1024):
    d = root / day.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    (d / "image_abc.png").write_bytes(b"x" * size)
    return d


def test_prunes_expired_dirs(tmp_path):
    m = _mgr(tmp_path)
    root = tmp_path / "generated"
    old = _mkday(root, date.today() - timedelta(days=60))
    new = _mkday(root, date.today() - timedelta(days=1))

    r = m.prune_generated(retention_days=30)
    assert r["removed_dirs"] == 1
    assert not old.exists(), "过期目录应被删除"
    assert new.exists(), "未过期目录不能删"


def test_keeps_dirs_within_retention(tmp_path):
    m = _mgr(tmp_path)
    root = tmp_path / "generated"
    _mkday(root, date.today() - timedelta(days=29))
    r = m.prune_generated(retention_days=30)
    assert r["removed_dirs"] == 0


def test_non_date_dirs_untouched(tmp_path):
    """目录名不是 `YYYY-MM-DD` 的，一律不碰（可能是人工放的资料）。"""
    m = _mgr(tmp_path)
    root = tmp_path / "generated"
    manual = root / "manual_notes"
    manual.mkdir(parents=True)
    (manual / "keep.txt").write_bytes(b"K")
    _mkday(root, date.today() - timedelta(days=60))

    m.prune_generated(retention_days=30)
    assert (manual / "keep.txt").exists(), "非日期目录被误删了"


def test_dry_run_deletes_nothing(tmp_path):
    m = _mgr(tmp_path)
    root = tmp_path / "generated"
    old = _mkday(root, date.today() - timedelta(days=60))

    r = m.prune_generated(retention_days=30, dry_run=True)
    assert r["removed_dirs"] == 1
    assert r["removed_bytes"] >= 1024
    assert old.exists(), "dry_run 竟然真的删了"


def test_missing_dir_is_not_error(tmp_path):
    m = _mgr(tmp_path)
    r = m.prune_generated()
    assert r["removed_dirs"] == 0


def test_zero_or_negative_retention_disables(tmp_path):
    """`retention_days <= 0` 表示关闭清理（与 capability audit 的约定一致）。"""
    m = _mgr(tmp_path)
    root = tmp_path / "generated"
    old = _mkday(root, date.today() - timedelta(days=999))
    m.prune_generated(retention_days=0)
    assert old.exists()


def test_never_raises_on_broken_core(tmp_path):
    """清理失败绝不能影响出图主流程。"""
    m = _mgr(tmp_path)
    m.core = object()          # 没有 config
    r = m.prune_generated()
    assert isinstance(r, dict)

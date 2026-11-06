# -*- coding: utf-8 -*-
"""JSON 读取硬上限（2026-11-06）。

## 起因

本地开发副本 `data/desires.json` 膨胀到 **5.09 GB**（旧版本无
`max_desires` 裁剪时累积）。`PersistenceProvider.read_json()` 是无界
`json.load` ⇒ 全量测试卡死在 `test_core.py::test_lifecycle` 的
`AnimaCore.start()`，跑了 40 分钟都出不来。

faulthandler 定位：

    File "json/__init__.py", line 298 in load
    File "anima/core/persistence.py", line 122 in read_json
    File "anima/cognitive/desire.py", line 185 in restore
    File "anima/core/anima_core.py", line 550 in _init_cognitive_state

生产同文件只有 3.4KB / 10 条、当前代码也有 `max_desires=10` 裁剪
（实测 add 200 次只保留 10 条），**不会再生**。但读取路径不该信任文件大小 ——
任何历史数据膨胀、日志误写或磁盘异常都会让进程在启动时挂死。

## 本测试锁死

1. 超限时**不得**调用 `json.load`（不能把 5GB 读进内存）
2. 超限时留证 `.oversize.bak`（不删数据）
3. 超限时返回 default
4. 正常大小的文件照常读
5. 上限是**类属性**、可调节
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from anima.core.persistence import PersistenceProvider


def _provider(tmp_path: Path) -> PersistenceProvider:
    return PersistenceProvider(str(tmp_path))


def _write(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_oversize_returns_default_without_parsing(tmp_path):
    """超大文件必须**在 json.load 之前**被拦下。"""
    prov = _provider(tmp_path)
    target = tmp_path / "big.json"
    _write(target, [{"x": i} for i in range(3)])

    # 伪造一个「超大」stat（不真写 5GB，省磁盘）
    import os
    real_stat = os.stat

    class _FakeStat:
        def __init__(self, orig):
            self.st_mtime = orig.st_mtime
            self.st_size = prov.MAX_JSON_READ_BYTES + 1

    with patch("os.stat", side_effect=lambda p, *a, **k: (
            _FakeStat(real_stat(p)) if str(p) == str(target) else real_stat(p, *a, **k))):
        with patch("json.load", side_effect=AssertionError("不该解析超大文件")):
            got = prov.read_json("big.json", default=["SAFE"])
    assert got == ["SAFE"], f"应返回 default，实得 {got!r}"


def test_oversize_keeps_evidence(tmp_path):
    """超限文件改名 .oversize.bak 留证，不删数据。"""
    prov = _provider(tmp_path)
    target = tmp_path / "big.json"
    _write(target, [{"x": 1}])

    import os
    real_stat = os.stat

    class _FakeStat:
        def __init__(self, orig):
            self.st_mtime = orig.st_mtime
            self.st_size = prov.MAX_JSON_READ_BYTES + 1

    def _fake(p, *a, **k):
        return _FakeStat(real_stat(p)) if str(p) == str(target) else real_stat(p, *a, **k)

    with patch("os.stat", side_effect=_fake):
        prov.read_json("big.json", default=[])

    bak = tmp_path / "big.json.oversize.bak"
    assert bak.exists(), "必须留证，不能直接删"
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"x": 1}]
    assert not target.exists(), "原文件应已被移走，避免下次启动再挂死"


def test_normal_file_reads_normally(tmp_path):
    """正常大小的文件不受影响。"""
    prov = _provider(tmp_path)
    _write(tmp_path / "ok.json", {"a": 1, "b": [1, 2, 3]})
    assert prov.read_json("ok.json", default={}) == {"a": 1, "b": [1, 2, 3]}


def test_missing_file_returns_default(tmp_path):
    assert _provider(tmp_path).read_json("nope.json", default=42) == 42


def test_corrupt_file_still_goes_to_corrupt_bak(tmp_path):
    """原有「损坏留证」行为不能被破坏。"""
    prov = _provider(tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert prov.read_json("bad.json", default=[]) == []
    assert (tmp_path / "bad.json.corrupt.bak").exists()


def test_limit_is_configurable():
    """上限是类属性，可按需调（不必改代码）。"""
    assert PersistenceProvider.MAX_JSON_READ_BYTES > 0
    assert PersistenceProvider.MAX_JSON_READ_BYTES >= 16 * 1024 * 1024

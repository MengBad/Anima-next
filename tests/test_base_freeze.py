# -*- coding: utf-8 -*-
"""B5 — 底座冻结核 (anti-entropy): 唯一源在场, 遗留重复层冻结, 防漂移。

新增顶层 anima/capability_*.py 或向 anima/capability_contracts 落文件即 CI 红(fail-closed),
承接「CI 永不自动改码/部署/绕过失败」。

2026-09-12（队列①）：16 个顶层 B5-merge shim 已整体 git mv 至 anima/capability/_archive/，
冻结语义反转为「顶层不得再出现任何 capability_* 文件」。
"""
import glob
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _files(root: Path, pattern: str) -> set[str]:
    return {Path(p).name for p in glob.glob(str(root / pattern))}


def test_top_level_capability_layer_is_frozen():
    actual = _files(ROOT, "anima/capability_*.py")
    assert actual == set(), ("顶层 capability_* 已全部归档且不得回填! 新能力代码必须进 anima/capability/。",)


def test_capability_contracts_freeze_store_empty():
    store = ROOT / "anima" / "capability_contracts"
    if store.exists():
        assert (not any(store.rglob("*")) or all(
        p.suffix == ".pyc" or "__pycache__" in str(p) for p in store.rglob("*"))),\
        "anima/capability_contracts 是冻结空存储, 不得落文件。"


def test_canonical_capability_layer_present():
    core = ROOT / "anima" / "capability"
    for name in ("rbac.py", "scopes.py", "contract.py", "schema.py", "resolver.py",
                 "evidence.py", "isolation.py", "index.py"):
        assert (core / name).exists(), f"规范能力层缺 {name}"


def test_rfc0001_source_of_truth_present():
    assert (ROOT / "docs" / "rfc" / "RFC-0001-Capability.md").exists(), "RFC-0001 唯一源缺失"

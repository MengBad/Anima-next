# -*- coding: utf-8 -*-
"""P2-C 方向 A：权限判定唯一权威 = pipeline.stages.permission._check_permission；
capability.rbac 是未接线 spec，运行时路径不得引用（防漂移契约测试）。"""
import inspect
from pathlib import Path

import anima.capability.rbac as rbac_mod
import anima.pipeline.stages.permission as perm_mod


def test_rbac_is_flagged_unwired():
    assert rbac_mod.RUNTIME_WIRED is False, "rbac 是 spec/未接线，必须保持 False"


def test_runtime_decision_funs_to_check_permission():
    """permission_stage 的判定必须经 _check_permission（唯一判定函数）。"""
    assert hasattr(perm_mod, "_check_permission")
    src = inspect.getsource(perm_mod.permission_stage)
    assert "_check_permission(" in src, "permission_stage 必须委托 _check_permission"


def test_runtime_source_never_imports_rbac():
    """运行时源码（anima/）不得 import anima.capability.rbac——防漂移。"""
    root = Path("anima")
    banned = (
        "from anima.capability.rbac import",
        "from anima.capability import rbac",
        "import anima.capability.rbac",
        "import rbac",
    )
    importers = []
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        head = "\n".join(text.splitlines()[:25])
        for pat in banned:
            if pat in head:
                importers.append(f"{p}: {pat}")
    assert not importers, f"运行时源码不得引用 rbac.py（未接线 spec）:\n" + "\n".join(importers)

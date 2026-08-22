# -*- coding: utf-8 -*-
"""health_regression 脚本的 pytest 包装 —— 让 P0-P5 健康回归纳入测试基线。

脚本本体（scripts/health_regression.py）驱动真实组件做逐阶段健康回归；
本文件保证它被打进 CI/常规 pytest，一旦某环节退化立即红。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "health_regression.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("health_regression", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["health_regression"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_health_regression_all_stages_ok():
    mod = _load_module()
    results = mod.run_all()
    lines = "\n".join(line for _, _, line in results)
    assert results, "回归脚本未产出任何阶段"
    failed = [name for name, ok, _ in results if not ok]
    assert not failed, f"P0-P5 健康回归存在失败阶段：{failed}\n{lines}"
    # 至少关键的 P0/P2/P3/P5 阶段都在且通过（online 可 skipped）
    names = {name for name, ok, _ in results if ok}
    for key in ("P0 基础聊天", "P2 Agentic路由+规划", "P3 反馈闭环", "P5 交互质量评估"):
        assert any(key in n for n in names), f"缺通过的关键阶段：{key}"

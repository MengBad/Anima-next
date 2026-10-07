# -*- coding: utf-8 -*-
"""测试隔离：mood 单例不得绑定仓库 ``data/``（2026-10-07）

## 为什么需要这个判据

`anima/cognitive/mood.py:307` 的 `get_mood_system(data_dir)` 是**全局单例**：

```python
def get_mood_system(data_dir=None):
    global _mood_system
    if _mood_system is None:
        _mood_system = MoodSystem(data_dir)
    return _mood_system          # ← 首次构造后忽略 data_dir
```

⇒ 与 `world_model._world_model` 同类：**构造后忽略 data_dir**。而
`tests/conftest.py::_reset_data_dir_ignoring_singletons` 的注释明确要求
「只收录**已证**『构造后忽略 data_dir』的单例」—— mood 属于此类但**当时未收录**。

## 生产实证（2026-10-07 全量回归）

`test_guardrail_analysis.py` / `test_guardrail_false_kill_fix.py` /
`test_guardrail_truncation_boundary.py` / `test_sticker_outbound.py` 共 4 条
**稳定失败**，且与当时的工作树改动无关。两次对照实验定位真因：

1. `git stash` 撤掉 guardrail 改动 → 仍失败（排除代码回归）；
2. 把本地 `data/mood.json` 从 `tired` 改成 `neutral` → 91 条全通过。

⇒ 某测试把 mood 写成 `tired` 落盘到**仓库 `data/mood.json`**，而 guardrail 的
`C1 情绪→回复长度约束`（`guardrail.py:804-841`）读同一个单例 ⇒ `tired` 上限
180 字把 277 字的正常回复截断 ⇒ 断言失败。

**这是既有测试隔离缺陷，不是产品缺陷**；但会长期制造假失败并掩盖真回归。

## 判据

测试期 `get_mood_system()` 拿到的实例，其落盘目录**不得**是仓库 ``data/``。
"""

from __future__ import annotations

import pathlib

import pytest


def test_mood_singleton_not_bound_to_repo_data():
    """mood 单例的落盘目录不得是仓库 ``data/``。

    🔴 必须先置 ``_mood_system = None`` 强制重建：否则若前面的测试已构造过
    指向别处的单例，本断言会「因为拿到旧实例」而**假绿**。
    """
    import anima.cognitive.mood as mood_mod

    saved = mood_mod._mood_system
    mood_mod._mood_system = None
    try:
        ms = mood_mod.get_mood_system("data")
        got = pathlib.Path(ms._persist.data_dir).resolve()
        repo_data = (pathlib.Path.cwd() / "data").resolve()
        assert got != repo_data, (
            f"mood 单例仍绑定仓库 data/：{got}\n"
            "⇒ 测试会读到/写到仓库 data/mood.json，跨测试污染（见模块 docstring）"
        )
    finally:
        mood_mod._mood_system = saved


def test_mood_system_default_dir_also_isolated():
    """不传参（走 `_default_dir`）时同样不得落在仓库 ``data/``。"""
    import anima.cognitive.mood as mood_mod

    saved = mood_mod._mood_system
    mood_mod._mood_system = None
    try:
        ms = mood_mod.get_mood_system()
        got = pathlib.Path(ms._persist.data_dir).resolve()
        repo_data = (pathlib.Path.cwd() / "data").resolve()
        assert got != repo_data, f"默认 data_dir 仍落仓库 data/：{got}"
    finally:
        mood_mod._mood_system = saved


def test_explicit_tmp_dir_still_honoured(tmp_path):
    """🔴 反向保护：显式传 **非仓库** 目录时必须尊重调用方（不误伤真实落盘语义）。

    隔离只应拦「落到仓库 data/」的请求；传 tmp_path 的测试必须仍能验证
    真实落盘行为，否则就是过度隔离。
    """
    import anima.cognitive.mood as mood_mod

    saved = mood_mod._mood_system
    mood_mod._mood_system = None
    try:
        ms = mood_mod.get_mood_system(str(tmp_path))
        assert pathlib.Path(ms._persist.data_dir).resolve() == tmp_path.resolve()
    finally:
        mood_mod._mood_system = saved

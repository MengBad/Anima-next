# -*- coding: utf-8 -*-
"""模块导入健康守卫（2026-10-17 建）。

背景：生产「确保各模块能正常工作」的一次实测发现，
`anima/**/*.py` 中有 15 个模块**无法 import** —— 全部落在 `anima/capability/_archive/`
（两代过期 compat shim，指向已迁移的旧路径）。经核查 `_archive` **零生产引用**
（无外部 import、`capability/__init__.py` 不导出、无动态 `iter_modules/import_module` 遍历），
属**归档死代码**，不影响运行。

本守卫的作用：把「**所有活跃模块必须可导入**」变成常态判据，
让未来任何一次模块搬迁 / 改名 / 删依赖造成的 breakout 立刻红。

设计：
  · 扫描 `anima/**/*.py`，**按路径分量排除 `_archive/`**（归档死代码，已文档化）；
  · **元守卫**：扫描面必须 ≥ 400 模块且含若干关键模块 ⇒ 防 rglob 路径写错致「零命中假绿」；
  · `_archive` 可达性守卫：若将来有非归档模块引用 `_archive`，本测试失败以提醒。

🔴 性能：全量导入**在子进程里跑**（`__main__` 分支）。若直接在 pytest 进程内 importlib
逐个导入，pytest 的断言重写 hook 会为每个 anima 模块生成改写版并落 .pyc ⇒ 实测 **330s**；
子进程 `python -B 本文件` 同样逻辑只需 **~2s**。
"""
from __future__ import annotations

import importlib
import pathlib
import re
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PKG = _ROOT / "anima"
_ARCHIVE_DIRNAME = "_archive"


def _iter_live_modules() -> list[str]:
    """返回所有**非归档**的 anima 模块全名（去重、排序）。

    🔴 排除按**路径分量**判定（`_archive` 出现在 parts 里），
    不能用子串 —— `anima.webui.life_archive` 是合法活跃模块，子串判定会误伤。
    """
    mods: list[str] = []
    for p in _PKG.rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        parts = list(p.relative_to(_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        if _ARCHIVE_DIRNAME in parts:
            continue           # 归档死代码：按分量排除
        mods.append(".".join(parts))
    return sorted(set(mods))


def _scan_in_subprocess() -> subprocess.CompletedProcess:
    """以子进程跑本文件的 `__main__` 分支做全量导入（规避 pytest 重写开销）。"""
    return subprocess.run(
        [sys.executable, "-B", str(pathlib.Path(__file__).resolve())],
        capture_output=True, text=True, timeout=300,
        cwd=str(_ROOT),
    )


# ── 核心：所有活跃模块必须可导入 ─────────────────────────────────────────
def test_all_live_modules_import():
    r = _scan_in_subprocess()
    assert r.returncode == 0, (
        "活跃模块导入扫描失败（returncode=%s）\n--- stdout ---\n%s\n--- stderr ---\n%s"
        % (r.returncode, (r.stdout or "")[-2500:], (r.stderr or "")[-800:])
    )
    # 自证扫描真的跑了（防「零命中」来自空跑）
    assert "活跃模块" in (r.stdout or ""), "扫描无输出 ⇒ 可能空跑"


# ── 元守卫：扫描面必须够宽 ──────────────────────────────────────────────
def test_scanner_reach_is_sane():
    mods = _iter_live_modules()
    assert len(mods) >= 400, (
        "扫描到的活跃模块仅 %d 个（<400）⇒ rglob 路径可能写错，零命中不可信" % len(mods)
    )
    for m in ["anima.cli", "anima.memory.vector_memory", "anima.llm.handler",
              "anima.pipeline.stages.prefilter"]:
        assert m in mods, "关键模块 %s 未被纳入扫描 ⇒ 扫描面异常" % m


def test_archive_is_excluded_from_scan():
    """元守卫：扫描集里不得出现归档模块（否则会因归档死代码而恒红）。

    按**路径分量**判定，避免误伤 `anima.webui.life_archive`。
    """
    mods = _iter_live_modules()
    offenders = [m for m in mods if _ARCHIVE_DIRNAME in m.split(".")]
    assert not offenders, "归档模块混入扫描集: %s" % offenders[:5]
    # 反向自检：确有一个同族合法模块被正确保留（证明用的是分量判定而非子串）
    assert any("life_archive" in m for m in mods), (
        "anima.webui.life_archive 是合法活跃模块，不应被排除 —— 排除逻辑可能过度"
    )


# ── `_archive` 若被引用 ⇒ 不再是死代码 ──────────────────────────────────
def test_archive_has_no_live_referrers():
    """归档目录的 15 个模块导入失败是**预期**（无人使用）。

    一旦有活跃模块 import 它，import 期即会炸 ⇒ 本守卫在此之前先失败提醒。
    """
    pat = re.compile(r"""(?:from|import)\s+[\w.]*\b_archive\b|"""
                     r"""["']anima\.[\w.]*\.?_archive[\w.]*["']""")
    offenders: list[str] = []
    for p in _PKG.rglob("*.py"):
        if "__pycache__" in str(p) or _ARCHIVE_DIRNAME in p.parts:
            continue
        if pat.search(p.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(p.relative_to(_ROOT)))
    assert not offenders, (
        "以下活跃模块引用了 _archive（归档死代码不应被引用）:\n  " + "\n  ".join(offenders)
    )


# ── 子进程入口：跑全量导入并打印失败清单 ────────────────────────────────
if __name__ == "__main__":
    mods = _iter_live_modules()
    bad: list[tuple[str, str]] = []
    for m in mods:
        try:
            importlib.import_module(m)
        except BaseException as e:      # noqa: BLE001 — 任何导入期异常都算失败
            bad.append((m, "%s: %s" % (type(e).__name__, str(e)[:160])))
    print("活跃模块 %d，导入失败 %d" % (len(mods), len(bad)))
    for m, t in bad:
        print("  FAIL %s -> %s" % (m, t))
    raise SystemExit(1 if bad else 0)

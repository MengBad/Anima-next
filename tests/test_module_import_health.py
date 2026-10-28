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

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PKG = _ROOT / "anima"
_ARCHIVE_DIRNAME = "_archive"

#: 存在于主包内、但**只能在独立 venv 里导入**的模块（按路径分量判定）。
#:
#: 🔴 2026-10-28 新增：`anima/rainyun_runner/` 是**独立进程脚本目录**，
#: 由 `tools/builtin/rainyun_account.py::_runner_paths()` 用
#: `/opt/anima/rainyun/venv/bin/python` 以 subprocess 拉起 —— 它依赖
#: numpy==1.26 + opencv + onnxruntime + ddddocr + selenium（约 1.2G），
#: **刻意不装进 anima 主 venv**（`requirements.txt` 里也没有）。
#:
#: 实测：生产主 venv 里 `import cv2` / `import numpy` / `import selenium`
#: 全部 ModuleNotFoundError，而 `/opt/anima/rainyun/venv` 里
#: cv2 5.0.0 + numpy 1.26.4 齐备，每天签到成功 2/2。
#: ⇒ 把这里算作「导入必须成功」是**判据错误**，会让守卫恒红。
#:
#: 正确判据：主包内代码**不得**在 import 期依赖这些（必须 try/except 降级），
#: 而 runner 目录里的脚本本来就只在另一个解释器里跑。
_ALT_VENV_DIRNAME = "rainyun_runner"


def _iter_live_modules() -> list[str]:
    """返回所有**需要在本解释器里可导入**的 anima 模块全名（去重、排序）。

    排除两类，都按**路径分量**判定（不用子串 —— `anima.webui.life_archive`
    是合法活跃模块，子串判定会误伤）：

    · `_archive/` —— 归档死代码，零生产引用
    · `rainyun_runner/` —— 独立 venv 脚本目录，主 venv 刻意不装它的依赖
      （见 `_ALT_VENV_DIRNAME` 的说明）
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
        if _ALT_VENV_DIRNAME in parts:
            continue           # 独立 venv 脚本：主 venv 无其依赖（设计如此）
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


def test_rainyun_runner_excluded_but_present():
    """元守卫：`rainyun_runner/` 被排除，但**文件必须还在**。

    排除它的理由是「依赖只装在独立 venv」，不是「删了」。
    若有人真的删掉 runner，签到会静默失效 —— 本测试要抓住那种情况。

    同时反向自检：主包里引用 runner 的那个模块**仍须可导入**
    （`rainyun_account.py` 只用 subprocess 调它，本身不 import runner 的依赖）。
    """
    mods = _iter_live_modules()
    assert not [m for m in mods if _ALT_VENV_DIRNAME in m.split(".")], (
        "rainyun_runner 是独立 venv 脚本目录，不应进主解释器导入集"
    )
    runner_dir = _PKG / _ALT_VENV_DIRNAME
    assert runner_dir.is_dir(), "rainyun_runner 目录被删 ⇒ 雨云签到会失效"
    for name in ("sign_in.py", "icr.py", "common.py"):
        assert (runner_dir / name).exists(), "rainyun_runner/%s 缺失" % name
    # 反向：调用侧必须在扫描集里（它本身不依赖 cv2/selenium）
    assert "anima.tools.builtin.rainyun_account" in mods, (
        "调用 runner 的模块必须在导入守卫覆盖范围内"
    )


def test_main_venv_does_not_need_runner_deps():
    """runner 的重依赖**不得**出现在主 venv requirements 里。

    这是「独立 venv」这个设计的前提。若有人把 opencv/selenium 加进
    `requirements.txt`，部署体积会涨 ~1.2G，而收益为零。
    """
    req = (_ROOT / "requirements.txt")
    if not req.exists():
        pytest.skip("requirements.txt 不存在")
    text = req.read_text(encoding="utf-8", errors="replace").lower()
    for dep in ("opencv", "selenium", "onnxruntime", "ddddocr", "numpy"):
        assert dep not in text, (
            "%s 属于 rainyun_runner 独立 venv 的依赖，不应进主 requirements" % dep
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

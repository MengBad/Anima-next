"""孤儿导入的 CI 守卫（D-11 的仓库侧防线，2026-09-18）。

背景
----
生产上有 12 个「独有 · 零引用 · 内部 import 已悬空」的废弃模块。实测：

    $ cd /opt/anima && ./venv/bin/python -c "import anima.capability.runtime_evidence_integration"
    ModuleNotFoundError: No module named 'anima.capability_runtime_observation'

    $ ./venv/bin/python -m pytest tests/ --collect-only -q
    !!! Interrupted: 12 errors during collection !!!      ← 生产跑不了全量测试

危害不是"平时会炸"，而是**任何遍历式加载**（pkgutil 扫描、pytest 收集、打包、
反射工具）会当场 ImportError。所以仓库侧必须有一条守门：**活跃代码的每个内部
import 都必须解析得到**。

本组测试跑的就是 `deploy/check_orphan_imports.py`（纯 ast、零副作用），并把扫描器
本身也用"植入孤儿"的载荷测试证明有牙齿。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "check_orphan_imports", _ROOT / "deploy" / "check_orphan_imports.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_scanner = _load_scanner()


# --------------------------------------------------------------------------
# 1) 仓库本身必须干净
# --------------------------------------------------------------------------
def test_repo_has_no_active_orphan_imports():
    """活跃代码（排除 `_archive/`）不得有解析不到的内部 import。"""
    report = _scanner.scan(_ROOT)
    assert report["active"] == {}, (
        f"活跃代码出现悬空内部导入，遍历式加载会 ImportError：{sorted(report['active'])}"
    )
    assert report["files_scanned"] > 100, "扫描面异常，疑似根目录推断错误"


def test_all_repo_tests_are_collectible():
    """仓库 tests/ 下每个文件都必须能被 pytest 收集（否则 CI 在收集期就中断）。"""
    broken = _scanner.scan_tests(_ROOT)
    assert broken == [], f"这些测试文件无法被收集: {broken}"


def test_archived_orphans_are_reported_separately():
    """归档残留必须与活跃悬空分开统计（否则会淹没真问题）——并如实存在。"""
    report = _scanner.scan(_ROOT)
    # 仓库里 `anima/capability/_archive/` 的 shim 指向生产独有的模块，属已知残留
    assert report["archived"], "归档残留清单为空，分类逻辑可能失效"
    assert all("_archive" in ref[0] for refs in report["archived"].values() for ref in refs)


# --------------------------------------------------------------------------
# 2) 载荷证明：扫描器真的能发现问题（不是永远返回空）
# --------------------------------------------------------------------------
def _plant(tmp_path: Path, *, archived: bool = False) -> Path:
    pkg = tmp_path / "anima"
    sub = (pkg / "_archive") if archived else pkg
    sub.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "planted.py").write_text(
        "from __future__ import annotations\n"
        "from anima.does_not_exist import thing\n"     # 悬空
        "from anima.real_one import other\n",          # 存在
        encoding="utf-8",
    )
    (pkg / "real_one.py").write_text("other = 1\n", encoding="utf-8")
    return tmp_path


def test_scanner_detects_planted_active_orphan(tmp_path):
    root = _plant(tmp_path)
    report = _scanner.scan(root)
    assert "anima.does_not_exist" in report["active"], "扫描器漏掉植入的活跃孤儿"
    assert "anima.real_one" not in report["active"], "扫描器误报已存在的模块"
    assert report["active"]["anima.does_not_exist"] == [("anima/planted.py", 2)]


def test_scanner_classifies_archived_orphan_separately(tmp_path):
    root = _plant(tmp_path, archived=True)
    report = _scanner.scan(root)
    assert report["active"] == {}, "归档目录里的引用不应算活跃"
    assert "anima.does_not_exist" in report["archived"]


def test_scanner_emits_pytest_ignore_for_broken_tests(tmp_path):
    """`--pytest-ignore` 的数据源：无法收集的测试文件必须被列出。"""
    root = _plant(tmp_path)
    tdir = root / "tests"
    tdir.mkdir()
    (tdir / "test_broken.py").write_text(
        "from anima.does_not_exist import thing\n", encoding="utf-8"
    )
    (tdir / "test_fine.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    assert _scanner.scan_tests(root) == ["test_broken.py"]

"""测试公共 fixture。"""

import pytest

# 归档测试（tests/_archive/）不参与收集。
# 注意：pytest 默认会递归 `_` 前缀目录（9.x 实测如此），必须显式忽略。
collect_ignore_glob = ["_archive"]


@pytest.fixture
def crossgroup_tmp(monkeypatch, tmp_path):
    """隔离的 crossgroup 单例 + 临时数据目录。

    每个 test 自带干净的单例与 data_dir，互不污染；test 结束后还原。
    """
    import anima.crossgroup.manager_singleton as ms

    ms._instance = None
    d = tmp_path / "cg"
    monkeypatch.setenv("ANIMA_CROSSGROUP_DIR", str(d))
    mgr = ms.get_crossgroup_manager()
    yield mgr
    ms._instance = None


@pytest.fixture(autouse=True)
def _reset_data_dir_ignoring_singletons():
    """逐测重置**忽略 data_dir 的全局单例**，消除按文件顺序的隐性耦合。

    只收录**已证**「构造后忽略 data_dir」的单例；不做全量单例清理
    （另 39 处单例中多数被测试有意共享，盲清会破坏它们）。

    - `anima.cognitive.world_model._world_model`
      （`world_model.py:291-295`；本轮回归 1 failed 的直接成因，见模块 docstring）
    """
    import anima.cognitive.world_model as _wm

    saved = _wm._world_model
    _wm._world_model = None
    yield
    _wm._world_model = saved


@pytest.fixture(autouse=True)
def _isolate_x_browser_quota(tmp_path, monkeypatch):
    """把落在**仓库 ``data/``** 下的 X 额度计数文件重定向到 ``tmp_path``。

    D-20 起额度计数落盘（生产必需），而既有测试用仓库 ``data/x_browser`` 当 data_dir
    ⇒ 计数跨测试进程累积，会把 ``search_daily_limit=10`` 跑满并让后续整批测试
    静默快速失败（2026-09-18 实测：同一命令第二次运行 10 failed）。

    只重定向**落在仓库 data/ 下**的路径：用 ``tmp_path`` 构造的测试（如
    ``test_x_quota_persistence.py``）不受影响，仍能验真实落盘语义。
    """
    import pathlib

    import anima.cognitive.x_browser as xb

    repo_data = (pathlib.Path.cwd() / "data").resolve()
    original = xb.XBrowserSession._quota_path

    def _redirected(self):
        path = original(self)
        try:
            resolved = pathlib.Path(path).resolve()
        except OSError:
            return path
        if repo_data in resolved.parents:
            return tmp_path / "x_daily_quota.json"
        return path

    monkeypatch.setattr(xb.XBrowserSession, "_quota_path", _redirected)

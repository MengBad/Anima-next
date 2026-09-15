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

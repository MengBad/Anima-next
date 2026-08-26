# -*- coding: utf-8 -*-
"""M2.4/M2.5：relation 与 crossgroup 单例的默认数据目录归一到配置值，消除 split-brain。"""
import os

from anima.cognitive.relation import get_relation, set_relation_data_dir
from anima.crossgroup.manager_singleton import get_crossgroup_manager, configure_crossgroup_manager
import anima.crossgroup.manager_singleton as _ms


def test_relation_default_follows_configured_dir(tmp_path):
    cfg = str(tmp_path / "cfg")
    set_relation_data_dir(cfg)
    try:
        a = get_relation()
        b = get_relation(cfg)   # 显式传同一配置目录
        assert a is b, "get_relation() 默认与显式 data_dir 应同一实例（修复 split-brain）"
        other = get_relation(str(tmp_path / "another"))
        assert other is not a, "不同目录仍应隔离（真实多目录场景不允许混）"
    finally:
        set_relation_data_dir("data")


def test_crossgroup_follows_configured_base(tmp_path):
    _ms._instance = None
    _ms._base_dir = None
    old_env = os.environ.pop("ANIMA_CROSSGROUP_DIR", None)
    try:
        configure_crossgroup_manager(str(tmp_path))
        mgr = get_crossgroup_manager()
        assert mgr.data_dir == os.path.join(str(tmp_path), "crossgroup"), mgr.data_dir
        assert get_crossgroup_manager() is mgr, "单例应保持"
    finally:
        _ms._instance = None
        _ms._base_dir = None
        if old_env is not None:
            os.environ["ANIMA_CROSSGROUP_DIR"] = old_env


def test_crossgroup_env_still_overrides(tmp_path):
    _ms._instance = None
    _ms._base_dir = None
    os.environ["ANIMA_CROSSGROUP_DIR"] = os.path.join(str(tmp_path), "envcross")
    try:
        configure_crossgroup_manager(str(tmp_path))
        assert get_crossgroup_manager().data_dir.endswith("envcross")
    finally:
        _ms._instance = None
        _ms._base_dir = None
        os.environ.pop("ANIMA_CROSSGROUP_DIR", None)

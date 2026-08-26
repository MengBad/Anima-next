# -*- coding: utf-8 -*-
"""M4：approvals 加载容错——未知字段不整条丢弃；损坏项落警告而非静默丢审批。"""
import json
import time

from anima.core.persistence import PersistenceProvider
from anima.core.approvals import ApprovalStore


def _seed(tmp_path, rows):
    (tmp_path / "pending_approvals.json").write_text(json.dumps(rows), encoding="utf-8")


def _base(**over):
    r = {"tool": "run_cmd", "args": {"a": 1}, "context": {}, "permission_level": 3,
         "expires_at": time.time() + 300, "id": "veryshortid", "status": "pending"}
    r.update(over)
    return r


def test_load_tolerates_future_unknown_fields(tmp_path):
    # 未来 schema 多出的字段：此前 **raw 会 TypeError 静默丢整条
    _seed(tmp_path, [_base(future_new_field=123, another=object() if False else "x")])
    store = ApprovalStore(PersistenceProvider(str(tmp_path)))
    item = store.get_pending("veryshortid")
    assert item is not None, "未知字段不应导致审批被丢"
    assert item.status == "pending"


def test_load_skips_nonobject_and_corrupt_but_keeps_others(tmp_path):
    _seed(tmp_path, ["not-a-dict", _base(), {"tool": "x"}])  # 第三项缺必填 args/context
    store = ApprovalStore(PersistenceProvider(str(tmp_path)))
    assert store.get_pending("veryshortid") is not None
    # 非对象串与损坏项被跳过（不崩溃、不静默假装成功）

# -*- coding: utf-8 -*-
"""一生身份快照恢复 —— 服务级 fail-closed + 可逆门控测试。"""
import json
import pathlib

import pytest

from anima.webui.life_restore import (
    restore_snapshot, rollback_restore, list_staged, FORBIDDEN,
)


@pytest.fixture()
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "identity_2026-08-24.json").write_text(json.dumps({"identity": "orb"}), encoding="utf-8")
    (d / "chat_memory.json").write_text(json.dumps({"memory": "x"}), encoding="utf-8")
    (d / "identity_crisis.json").write_text(json.dumps({"crisis": True}), encoding="utf-8")
    return d


def test_deny_non_owner(data_dir):
    out = restore_snapshot(data_dir, "identity_2026-08-24.json", confirm=True, owner_ok=False)
    assert out["staged"] is False and out["code"] == 403


def test_require_confirm(data_dir):
    out = restore_snapshot(data_dir, "identity_2026-08-24.json", confirm=False, owner_ok=True)
    assert out["staged"] is False and out["code"] == 400


def test_path_traversal_rejected(data_dir):
    out = restore_snapshot(data_dir, "../etc/passwd", confirm=True, owner_ok=True)
    assert out["code"] == 400


def test_forbidden_chat_memory_rejected(data_dir):
    for n in FORBIDDEN:
        out = restore_snapshot(data_dir, n, confirm=True, owner_ok=True)
        assert out["code"] == 400, n


def test_non_snapshot_file_rejected(data_dir):
    # commitments.json 不在清点模式(identity_*.json/persona_*.bak/*snapshot*.json/*identity*.json)
    (data_dir / "commitments.json").write_text(json.dumps({"c": 1}), encoding="utf-8")
    out = restore_snapshot(data_dir, "commitments.json", confirm=True, owner_ok=True)
    assert out["code"] == 400


def test_invalid_json_rejected(data_dir):
    (data_dir / "identity_bad.json").write_text("not-json", encoding="utf-8")
    out = restore_snapshot(data_dir, "identity_bad.json", confirm=True, owner_ok=True)
    assert out["code"] == 400


def test_success_staged_with_backup_and_atomic(data_dir):
    out = restore_snapshot(data_dir, "identity_2026-08-24.json", confirm=True, owner_ok=True)
    assert out["staged"] is True
    restored = pathlib.Path(out["restored_path"])
    assert restored.is_file()
    assert json.loads(restored.read_text(encoding="utf-8")) == {"identity": "orb"}
    backup = pathlib.Path(out["backup_path"])
    assert backup.is_file()
    assert "identity_2026-08-24.json" in list_staged(data_dir)


def test_rollback_restores_backup(data_dir):
    restore_snapshot(data_dir, "identity_2026-08-24.json", confirm=True, owner_ok=True)
    # 篡改 restored 内容后回滚应还原唯一备份时的原文
    rb = rollback_restore(data_dir, "identity_2026-08-24.json", confirm=True, owner_ok=True)
    assert rb["rolled_back"] is True
    assert json.loads(pathlib.Path(rb["restored_path"]).read_text(encoding="utf-8")) == {"identity": "orb"}


def test_rollback_requires_owner_and_confirm(data_dir):
    assert rollback_restore(data_dir, "identity_2026-08-24.json", confirm=True, owner_ok=False)["code"] == 403
    assert rollback_restore(data_dir, "identity_2026-08-24.json", confirm=False, owner_ok=True)["code"] == 400

import json

import yaml
import pytest

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig, PermissionConfig, PersistenceConfig
from anima.core.config_recovery import arm, mark_healthy, prepare_start
from anima.core.config_service import ConfigService


def test_failed_candidate_start_restores_backup_and_audits(tmp_path):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    backup_path = data_dir / "config_backups" / "config.yaml.1.bak"
    config_path.write_text("llm:\n  model: candidate-model\n", encoding="utf-8")
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text("llm:\n  model: known-good-model\n", encoding="utf-8")
    (data_dir / "config_changes.json").write_text(
        json.dumps({"cfg_test": {"status": "applied", "created_at": 1}}),
        encoding="utf-8",
    )

    assert arm(data_dir, "cfg_test", config_path, backup_path) is True
    assert prepare_start(config_path) == "attempting"
    assert "candidate-model" in config_path.read_text(encoding="utf-8")

    assert prepare_start(config_path) == "rolled_back"
    assert "known-good-model" in config_path.read_text(encoding="utf-8")
    record = json.loads((data_dir / "config_changes.json").read_text(encoding="utf-8"))["cfg_test"]
    assert record["status"] == "rolled_back"
    assert record["rollback_reason"] == "startup_health_failed"


def test_healthy_candidate_is_not_rolled_back_on_next_restart(tmp_path):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    backup_path = data_dir / "config_backups" / "config.yaml.1.bak"
    config_path.write_text("llm:\n  model: candidate-model\n", encoding="utf-8")
    backup_path.parent.mkdir(parents=True)
    backup_path.write_text("llm:\n  model: known-good-model\n", encoding="utf-8")

    arm(data_dir, "cfg_healthy", config_path, backup_path)
    assert prepare_start(config_path) == "attempting"
    assert mark_healthy(data_dir) is True
    assert prepare_start(config_path) == "healthy_cleared"
    assert "candidate-model" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_config_service_arms_recovery_for_restart_change(tmp_path):
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("llm:\n  model: old-model\n", encoding="utf-8")
    config = AnimaConfig(
        persistence=PersistenceConfig(data_dir=str(data_dir)),
        permissions=PermissionConfig(owners=[42]),
    )
    core = AnimaCore(config)
    core.config_path = str(config_path)
    service = ConfigService(core)
    change = service.create_change_set({"llm": {"model": "new-model"}})

    applied = service.apply(change["id"])

    assert applied["status"] == "applied"
    marker = json.loads((data_dir / "config_recovery.json").read_text(encoding="utf-8"))
    assert marker["state"] == "awaiting_start"
    assert marker["change_id"] == change["id"]
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["llm"]["model"] == "new-model"

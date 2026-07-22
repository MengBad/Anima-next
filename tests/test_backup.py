import zipfile

from anima.core.anima_core import AnimaCore
from anima.core.config import AnimaConfig
from anima.core.evaluation import RuntimeEvaluator


def test_backup_excludes_chat_content_and_secrets(tmp_path):
    core = AnimaCore(AnimaConfig.model_validate({"persistence": {"data_dir": str(tmp_path)}}))
    core.persistence.write_json("intent_ledger.json", {"items": []})
    core.persistence.write_json("chat_memory.json", {"private": "must not be copied"})
    (tmp_path / "config.yaml").write_text("api_key: must-not-be-copied", encoding="utf-8")

    snapshot = core.backups.create("test")

    assert snapshot["files"] == ["intent_ledger.json"]
    with zipfile.ZipFile(tmp_path / "backups" / snapshot["name"]) as archive:
        names = archive.namelist()
        assert "intent_ledger.json" in names
        assert "chat_memory.json" not in names
        assert "config.yaml" not in names
        assert "manifest.json" in names


def test_evaluation_reports_observable_facts(tmp_path):
    core = AnimaCore(AnimaConfig.model_validate({"persistence": {"data_dir": str(tmp_path)}}))
    report = RuntimeEvaluator(core).evaluate()
    assert report["summary"]["attention"] >= 1
    assert {item["id"] for item in report["checks"]} >= {"runtime", "qq_channel", "llm", "backup"}
    assert all(item["status"] in {"ready", "attention", "unknown"} for item in report["checks"])


def test_backup_maintenance_creates_daily_snapshot_and_prunes(tmp_path):
    core = AnimaCore(AnimaConfig.model_validate({"persistence": {"data_dir": str(tmp_path)}}))
    core.backups.retention = 2
    first = core.backups.maintain(now=1_000)
    assert first["created"] is not None
    assert core.backups.maintain(now=1_001)["created"] is None
    core.backups.maintain(now=1_000 + 86_401)
    third = core.backups.maintain(now=1_000 + 2 * 86_401)
    assert third["removed"] == 1
    assert core.backups.health(now=1_000 + 2 * 86_401)["count"] == 2

import json
import tomllib
import zipfile
from pathlib import Path

import _deploy
import anima


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_and_license_are_aligned():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == anima.__version__ == _deploy._project_version()
    assert project["license"] == "AGPL-3.0-only"
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3, 19 November 2007" in license_text


def test_release_excludes_runtime_state_and_private_keys():
    excluded = _deploy._build_exclude(False)

    for path in (
        "config.yaml",
        ".env",
        "data/chat_memory.json",
        "secrets/api.pem",
        "secrets/id_ed25519",
        "public/anima-next-source.zip",
    ):
        assert _deploy._is_excluded(path, excluded), path
    assert not _deploy._is_excluded("config/default.yaml", excluded)
    assert not _deploy._is_excluded("anima/core/anima_core.py", excluded)


def test_corresponding_source_archive_has_identity_and_no_runtime_data():
    plan = [
        (str(ROOT / "LICENSE"), "/opt/anima/LICENSE"),
        (str(ROOT / "pyproject.toml"), "/opt/anima/pyproject.toml"),
        # A defensive second filter in the archive builder must reject this.
        (str(ROOT / "config.yaml"), "/opt/anima/config.yaml"),
    ]
    identity = {
        "commit": "deadbeef1234",
        "branch": "codex/release-0.3-rc",
        "dirty": False,
        "commit_epoch": 1_720_000_000,
    }
    temporary, archive, manifest_path = _deploy._build_source_archive(
        plan, anima.__version__, identity,
    )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["license"] == "AGPL-3.0-only"
        assert manifest["commit"] == "deadbeef1234"
        assert {item["path"] for item in manifest["files"]} == {
            "LICENSE", "pyproject.toml",
        }

        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
        assert any(name.endswith("/RELEASE-MANIFEST.json") for name in names)
        assert not any(name.endswith("/config.yaml") or "/data/" in name for name in names)
    finally:
        temporary.cleanup()

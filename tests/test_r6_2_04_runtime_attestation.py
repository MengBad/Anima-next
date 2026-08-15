"""R6.2-04 Runtime drift detection — 生产一致性测试。

验证：
  * running SHA == approved SHA → PASS。
  * running SHA != approved SHA → DRIFT DETECTED。
  * 缺失 approved 参考 → DRIFT DETECTED（fail-closed）。
  * config 哈希不匹配 → DRIFT DETECTED。
  * config 哈希匹配 → PASS。
"""
from __future__ import annotations

from anima.capability.governance.attestation import attest, config_hash


def test_attest_pass():
    r = attest("8eca02dfae47", "8eca02dfae47")
    assert r.ok is True
    assert r.status == "PASS"
    assert r.checks["commit_sha"][0] is True


def test_attest_sha_mismatch():
    r = attest("8eca02dfae47", "deadbeef")
    assert r.ok is False
    assert r.status == "DRIFT DETECTED"
    assert r.checks["commit_sha"][0] is False


def test_attest_missing_approved():
    r = attest("8eca02dfae47", None)
    assert r.ok is False
    assert "not provided" in r.checks["commit_sha"][1]


def test_attest_config_mismatch():
    r = attest("8eca02dfae47", "8eca02dfae47",
               running_config_hash="sha256:aaa", approved_config_hash="sha256:bbb")
    assert r.ok is False
    assert r.checks["config_hash"][0] is False


def test_attest_config_match():
    r = attest("8eca02dfae47", "8eca02dfae47",
               running_config_hash="sha256:same", approved_config_hash="sha256:same")
    assert r.ok is True
    assert "config_hash" in r.checks


def test_config_hash_stable():
    a = config_hash("a: 1\nb: 2\n")
    b = config_hash("b: 2\na: 1\n")  # 顺序不同但语义相同
    assert a == b
    assert a.startswith("sha256:")


def test_attest_runtime_reads_version(tmp_path):
    from anima.capability.governance.attestation import attest_runtime
    vp = tmp_path / "version.json"
    vp.write_text('{"commit": "8eca02dfae47", "branch": "x"}', encoding="utf-8")
    r = attest_runtime("8eca02dfae47", str(vp))
    assert r.ok is True

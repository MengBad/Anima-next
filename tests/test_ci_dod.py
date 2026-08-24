# -*- coding: utf-8 -*-
"""CI DoD 校验 (Stage 4 定义通过标准) tests。"""
from anima.evolution.ci_dod import check_ci_dod, candidate_reviewable


def test_all_green_passes():
    r = check_ci_dod(test_passed=True, frontend_ok=True, evidence_ok=True)
    assert r["pass"] is True
    assert r["reasons"] == []
    assert candidate_reviewable(r) is True


def test_backend_missing_fails():
    r = check_ci_dod(test_passed=None, frontend_ok=True, evidence_ok=True)
    assert r["pass"] is False


def test_frontend_missing_fails():
    r = check_ci_dod(test_passed=True, frontend_ok=None, evidence_ok=True)
    assert r["pass"] is False


def test_evidence_missing_fails_fail_closed():
    r = check_ci_dod(test_passed=True, frontend_ok=True, evidence_ok=False)
    assert r["pass"] is False
    assert any("evidence" in x for x in r["reasons"])


def test_core_requires_owner_ack():
    r = check_ci_dod(test_passed=True, frontend_ok=True, evidence_ok=True,
                     security_requires_owner_review=True)
    assert r["pass"] is False
    r2 = check_ci_dod(test_passed=True, frontend_ok=True, evidence_ok=True,
                      security_requires_owner_review=True, owner_acknowledged=True)
    assert r2["pass"] is True


def test_sections_present():
    r = check_ci_dod(test_passed=True, frontend_ok=True, evidence_ok=True)
    for sec in ("backend", "frontend", "evidence", "security_requires_owner_review"):
        assert sec in r["sections"]

import copy

from anima.operator_contract_tooling import validate_bundle


def auth(**overrides):
    value = {
        "authorization_id": "auth-1",
        "actor_id": "operator-1",
        "role": "operator",
        "action": "maintain",
        "scope": {
            "repository": "G:/Anima-next",
            "environment": "development",
            "paths": ["docs/"],
            "data_access": "none",
        },
        "target": "docs",
        "reason": "documentation review",
        "evidence_refs": ["evidence-1"],
        "approver_id": "human-1",
        "issued_at": "2099-01-01T00:00:00+00:00",
        "expires_at": "2099-01-02T00:00:00+00:00",
        "status": "approved",
        "constraints": ["project"],
    }
    value.update(overrides)
    return value


def base_bundle():
    return {
        "authorization": auth(),
        "declared_scope": {
            "repository": "G:/Anima-next",
            "environment": "development",
            "paths": ["docs/"],
            "mutation": "documentation",
        },
    }


def result(bundle, check):
    return validate_bundle(bundle, check)["results"][0]


def test_authorization_output_contract_and_all_checks_are_structured():
    output = validate_bundle(base_bundle())
    assert output["tool_version"] == 1
    assert {item["check_type"] for item in output["results"]} == {"authorization", "action", "report", "memory_access", "boundary"}
    assert result(base_bundle(), "authorization")["result"] == "pass"


def test_authorization_is_deny_by_default_for_unknown_role_action_and_wildcard():
    bundle = base_bundle()
    bundle["authorization"] = auth(role="observer", action="maintain", scope={**auth()["scope"], "paths": ["*"]})
    checked = result(bundle, "authorization")
    assert checked["result"] in {"fail", "stop"}
    assert {finding["code"] for finding in checked["findings"]} >= {"role_action_mismatch", "invalid_paths"}


def test_action_without_authorization_is_terminal_stop():
    bundle = base_bundle()
    bundle.pop("authorization")
    bundle["action"] = {"action_id": "action-1"}
    checked = result(bundle, "action")
    assert checked["result"] == "stop"
    assert checked["next_step"] == "stop"
    assert any(item["code"] == "missing_authorization" for item in checked["findings"])


def test_report_contradiction_without_required_evidence_stops():
    bundle = base_bundle()
    bundle["report"] = {
        "report_id": "report-1", "report_type": "evidence_analysis", "authorization_id": None,
        "action_id": None, "actor_id": "operator-1", "role": "operator",
        "created_at": "2099-01-01T00:00:00+00:00", "scope": {}, "baseline": {},
        "facts": [], "evidence": [], "judgment": [], "impact": [], "risks": [],
        "contradiction": {"observed": True, "evidence_refs": [], "why_current_contract_cannot_handle": ""},
        "next_step": "stop", "stop_reason": "review", "production_touched": False,
        "foundation_boundary_touched": False,
    }
    checked = result(bundle, "report")
    assert checked["result"] == "stop"
    assert any(item["code"] == "incomplete_contradiction" for item in checked["findings"])


def test_memory_content_read_is_deny_by_default_without_explicit_scope():
    bundle = base_bundle()
    bundle["memory_access"] = {
        "access_request_id": "memory-1", "actor_id": "operator-1", "role": "operator",
        "authorization_id": "auth-1", "memory_scope": "identity", "operation": "read_content",
        "target": "identity-1", "requested_at": "2099-01-01T00:00:00+00:00",
        "decision": "allowed", "decision_reason": "", "evidence_refs": [],
    }
    checked = result(bundle, "memory_access")
    assert checked["result"] == "stop"
    assert any(item["code"] == "memory_scope_not_explicit" for item in checked["findings"])


def test_boundary_audit_stops_documentation_authorization_for_code_mutation():
    bundle = base_bundle()
    bundle["declared_scope"]["mutation"] = "code"
    checked = result(bundle, "boundary")
    assert checked["result"] == "stop"
    assert any(item["code"] == "missing_action_record" for item in checked["findings"])


def test_boundary_audit_stops_foundation_boundary_touch():
    bundle = base_bundle()
    bundle["action"] = {"foundation_boundary_touched": True}
    checked = result(bundle, "boundary")
    assert checked["result"] == "stop"
    assert any(item["code"] == "foundation_boundary_touched" for item in checked["findings"])


def test_expired_authorization_stops_action():
    bundle = base_bundle()
    bundle["authorization"] = auth(issued_at="2020-01-01T00:00:00+00:00", expires_at="2020-01-02T00:00:00+00:00")
    bundle["action"] = {
        "action_id": "action-1", "authorization_id": "auth-1", "actor_id": "operator-1",
        "role": "operator", "action": "maintain", "started_at": "2020-01-01T00:00:00+00:00",
        "ended_at": "2020-01-01T00:01:00+00:00", "environment": "development", "target": "docs",
        "baseline": {"commit": "abc", "branch": "codex/test", "dirty": False},
        "changes": {"files": ["docs/"], "mutation": "documentation"}, "evidence_refs": [],
        "result": "completed", "stop_reason": None, "production_touched": False,
        "foundation_boundary_touched": False, "report_id": "report-1",
    }
    checked = result(bundle, "action")
    assert checked["result"] == "stop"
    assert any(item["code"] == "authorization_expired" for item in checked["findings"])


def test_input_bundle_is_not_mutated():
    bundle = base_bundle()
    before = copy.deepcopy(bundle)
    validate_bundle(bundle)
    assert bundle == before

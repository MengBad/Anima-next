"""Offline validators for the Operator Contract design.

This module is intentionally standalone.  It is not imported by the Anima
runtime, does not contact production, and does not grant authority.  It only
validates an explicit JSON verification bundle and returns structured findings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping


ROLES = {"observer", "operator", "evolution"}
AUTH_STATUSES = {"proposed", "approved", "consumed", "revoked", "expired", "rejected"}
AUTH_ACTIONS = {"observe", "diagnose", "maintain", "release", "rollback", "design_review", "evolve"}
ENVIRONMENTS = {"local", "development", "staging", "production"}
MUTATIONS = {"none", "documentation", "code", "config", "data", "deployment"}
REPORT_TYPES = {"runtime_status", "maintenance", "release", "rollback", "evidence_analysis", "design"}
REPORT_NEXT_STEPS = {"stop", "continue_observation", "request_authorization", "execute_approved_action"}
MEMORY_SCOPES = {"identity", "project", "experience", "user_collaboration"}
MEMORY_OPERATIONS = {"inspect_metadata", "read_content", "validate"}
MEMORY_DECISIONS = {"allowed", "denied", "expired", "out_of_scope"}

ROLE_ACTIONS = {
    "observer": {"observe"},
    "operator": {"observe", "diagnose", "maintain", "release", "rollback"},
    "evolution": {"design_review", "evolve"},
}


def _result(check_type: str, bundle: Mapping[str, Any], result: str, findings: list[dict[str, Any]], *, next_step: str) -> dict[str, Any]:
    fingerprint = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "verification_id": f"verify-{fingerprint}",
        "check_type": check_type,
        "result": result,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "input_refs": [],
        "findings": findings,
        "evidence_refs": [],
        "next_step": next_step,
    }


def _finding(code: str, severity: str, message: str, field: str | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "field": field, "message": message}


def _section(bundle: Mapping[str, Any], name: str, check_type: str) -> tuple[Mapping[str, Any] | None, dict[str, Any] | None]:
    value = bundle.get(name)
    if value is None:
        return None, _result(check_type, bundle, "not_applicable", [], next_step="continue_review")
    if not isinstance(value, Mapping):
        return None, _result(check_type, bundle, "fail", [_finding("invalid_section", "error", f"{name} must be an object", name)], next_step="stop")
    return value, None


def _finish(check_type: str, bundle: Mapping[str, Any], findings: list[dict[str, Any]], *, stop: bool = False) -> dict[str, Any]:
    result = "stop" if stop else ("fail" if findings else "pass")
    return _result(check_type, bundle, result, findings, next_step="stop" if (stop or findings) else "continue_review")


def _timestamp(value: Any, field: str, findings: list[dict[str, Any]]) -> datetime | None:
    if not isinstance(value, str):
        findings.append(_finding("invalid_timestamp", "error", f"{field} must be an ISO-8601 timestamp", field))
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        findings.append(_finding("invalid_timestamp", "error", f"{field} must be an ISO-8601 timestamp", field))
        return None
    if parsed.tzinfo is None:
        findings.append(_finding("timestamp_without_timezone", "error", f"{field} must include a timezone", field))
        return None
    return parsed


def _required(record: Mapping[str, Any], fields: set[str], findings: list[dict[str, Any]], label: str) -> None:
    missing = sorted(fields - set(record))
    if missing:
        findings.append(_finding("missing_fields", "error", f"{label} missing fields: {missing}", label))


def _valid_authorization(auth: Mapping[str, Any], *, require_approved: bool, findings: list[dict[str, Any]]) -> bool:
    required = {"authorization_id", "actor_id", "role", "action", "scope", "target", "reason", "evidence_refs", "approver_id", "issued_at", "expires_at", "status", "constraints"}
    _required(auth, required, findings, "authorization")
    if findings:
        return False
    if not all(isinstance(auth.get(key), str) and auth[key].strip() for key in ("authorization_id", "actor_id", "target", "reason", "approver_id")):
        findings.append(_finding("invalid_identity_field", "error", "authorization identity and reason fields must be non-empty strings", "authorization"))
    if auth["role"] not in ROLES:
        findings.append(_finding("invalid_role", "error", "authorization role is invalid", "role"))
    if auth["action"] not in AUTH_ACTIONS:
        findings.append(_finding("invalid_action", "error", "authorization action is invalid", "action"))
    if auth["status"] not in AUTH_STATUSES:
        findings.append(_finding("invalid_status", "error", "authorization status is invalid", "status"))
    if auth["role"] in ROLE_ACTIONS and auth["action"] not in ROLE_ACTIONS[auth["role"]]:
        findings.append(_finding("role_action_mismatch", "stop", "role is not permitted for this action", "action"))
    scope = auth["scope"]
    if not isinstance(scope, Mapping):
        findings.append(_finding("invalid_scope", "error", "authorization scope must be an object", "scope"))
    else:
        if scope.get("environment") not in ENVIRONMENTS:
            findings.append(_finding("invalid_environment", "error", "scope environment is invalid", "scope.environment"))
        if not isinstance(scope.get("repository"), str) or not scope["repository"].strip():
            findings.append(_finding("invalid_repository", "error", "scope repository must be explicit", "scope.repository"))
        if not isinstance(scope.get("paths"), list) or not scope["paths"] or not all(isinstance(path, str) and path.strip() and "*" not in path for path in scope["paths"]):
            findings.append(_finding("invalid_paths", "error", "scope paths must be a non-empty explicit allowlist", "scope.paths"))
        if scope.get("data_access") not in {"none", "metadata", "operational", "user_data"}:
            findings.append(_finding("invalid_data_access", "error", "scope data_access is invalid", "scope.data_access"))
    issued = _timestamp(auth["issued_at"], "issued_at", findings)
    expires = _timestamp(auth["expires_at"], "expires_at", findings)
    if issued and expires and issued > expires:
        findings.append(_finding("invalid_time_range", "error", "issued_at must not be later than expires_at", "expires_at"))
    if not isinstance(auth["evidence_refs"], list) or not all(isinstance(ref, str) and ref.strip() for ref in auth["evidence_refs"]):
        findings.append(_finding("invalid_evidence_refs", "error", "evidence_refs must be a list of non-empty strings", "evidence_refs"))
    if not isinstance(auth["constraints"], list) or not all(isinstance(item, str) for item in auth["constraints"]):
        findings.append(_finding("invalid_constraints", "error", "constraints must be a list of strings", "constraints"))
    if require_approved:
        if auth["status"] != "approved":
            findings.append(_finding("authorization_not_approved", "stop", "authorization is not approved", "status"))
        if auth["approver_id"] == auth["actor_id"]:
            findings.append(_finding("self_approval", "stop", "actor and approver must be distinct", "approver_id"))
        if expires and expires <= datetime.now(timezone.utc):
            findings.append(_finding("authorization_expired", "stop", "authorization has expired", "expires_at"))
    return not findings


def validate_authorization(bundle: Mapping[str, Any]) -> dict[str, Any]:
    auth, error = _section(bundle, "authorization", "authorization")
    if error:
        return error
    if auth is None:
        return _result("authorization", bundle, "not_applicable", [], next_step="continue_review")
    findings: list[dict[str, Any]] = []
    _valid_authorization(auth, require_approved=False, findings=findings)
    return _finish("authorization", bundle, findings, stop=any(item["severity"] == "stop" for item in findings))


def _authorization_scope_covers(auth: Mapping[str, Any], declared: Mapping[str, Any]) -> bool:
    scope = auth.get("scope", {})
    return (
        isinstance(scope, Mapping)
        and scope.get("environment") == declared.get("environment")
        and scope.get("repository") == declared.get("repository")
        and set(declared.get("paths", [])) <= set(scope.get("paths", []))
    )


def validate_action(bundle: Mapping[str, Any]) -> dict[str, Any]:
    action, error = _section(bundle, "action", "action")
    if error:
        return error
    if action is None:
        return _result("action", bundle, "not_applicable", [], next_step="continue_review")
    findings: list[dict[str, Any]] = []
    _required(action, {"action_id", "authorization_id", "actor_id", "role", "action", "started_at", "ended_at", "environment", "target", "baseline", "changes", "evidence_refs", "result", "stop_reason", "production_touched", "foundation_boundary_touched", "report_id"}, findings, "action")
    auth = bundle.get("authorization")
    if not isinstance(auth, Mapping):
        findings.append(_finding("missing_authorization", "stop", "action requires an authorization record", "authorization"))
        return _finish("action", bundle, findings, stop=True)
    _valid_authorization(auth, require_approved=True, findings=findings)
    if action.get("authorization_id") != auth.get("authorization_id"):
        findings.append(_finding("authorization_link_mismatch", "stop", "action does not link to the supplied authorization", "authorization_id"))
    for field in ("actor_id", "role", "target"):
        if action.get(field) != auth.get(field):
            findings.append(_finding("action_scope_mismatch", "stop", f"action {field} does not match authorization", field))
    started = _timestamp(action.get("started_at"), "started_at", findings)
    ended = _timestamp(action.get("ended_at"), "ended_at", findings)
    if started and ended and started > ended:
        findings.append(_finding("invalid_action_time_range", "error", "started_at must not be later than ended_at", "ended_at"))
    if action.get("environment") not in ENVIRONMENTS:
        findings.append(_finding("invalid_environment", "error", "action environment is invalid", "environment"))
    baseline = action.get("baseline")
    changes = action.get("changes")
    if not isinstance(baseline, Mapping) or not all(key in baseline for key in ("commit", "branch", "dirty")):
        findings.append(_finding("invalid_baseline", "error", "action baseline must include commit, branch, and dirty", "baseline"))
    if not isinstance(changes, Mapping) or changes.get("mutation") not in MUTATIONS or not isinstance(changes.get("files"), list):
        findings.append(_finding("invalid_changes", "error", "action changes must include files and a valid mutation", "changes"))
    if not _authorization_scope_covers(auth, bundle.get("declared_scope", {})):
        findings.append(_finding("action_out_of_scope", "stop", "action is outside the declared authorization scope", "declared_scope"))
    if action.get("foundation_boundary_touched") is True:
        findings.append(_finding("foundation_boundary_touched", "stop", "Foundation boundary touch requires governance review", "foundation_boundary_touched"))
    if action.get("result") in {"blocked", "stopped"} and not isinstance(action.get("stop_reason"), str):
        findings.append(_finding("missing_stop_reason", "error", "blocked or stopped actions require stop_reason", "stop_reason"))
    return _finish("action", bundle, findings, stop=any(item["severity"] == "stop" for item in findings))


def validate_report(bundle: Mapping[str, Any]) -> dict[str, Any]:
    report, error = _section(bundle, "report", "report")
    if error:
        return error
    if report is None:
        return _result("report", bundle, "not_applicable", [], next_step="continue_review")
    findings: list[dict[str, Any]] = []
    _required(report, {"report_id", "report_type", "authorization_id", "action_id", "actor_id", "role", "created_at", "scope", "baseline", "facts", "evidence", "judgment", "impact", "risks", "contradiction", "next_step", "stop_reason", "production_touched", "foundation_boundary_touched"}, findings, "report")
    if report.get("report_type") not in REPORT_TYPES:
        findings.append(_finding("invalid_report_type", "error", "report_type is invalid", "report_type"))
    _timestamp(report.get("created_at"), "created_at", findings)
    for field in ("facts", "judgment", "impact", "risks"):
        if not isinstance(report.get(field), list) or not all(isinstance(item, str) for item in report[field]):
            findings.append(_finding("invalid_report_list", "error", f"{field} must be a list of strings", field))
    contradiction = report.get("contradiction")
    if not isinstance(contradiction, Mapping) or not isinstance(contradiction.get("observed"), bool):
        findings.append(_finding("invalid_contradiction", "error", "contradiction must include observed boolean", "contradiction"))
    elif contradiction["observed"]:
        if not contradiction.get("evidence_refs") or not contradiction.get("why_current_contract_cannot_handle"):
            findings.append(_finding("incomplete_contradiction", "stop", "contradiction requires evidence_refs and why_current_contract_cannot_handle", "contradiction"))
    if report.get("next_step") not in REPORT_NEXT_STEPS:
        findings.append(_finding("invalid_next_step", "error", "next_step is invalid", "next_step"))
    if report.get("next_step") == "execute_approved_action":
        auth = bundle.get("authorization")
        if not isinstance(auth, Mapping):
            findings.append(_finding("missing_authorization", "stop", "execution next step requires authorization", "authorization"))
        else:
            _valid_authorization(auth, require_approved=True, findings=findings)
    if report.get("foundation_boundary_touched") is True:
        findings.append(_finding("foundation_boundary_touched", "stop", "Foundation boundary touch requires governance review", "foundation_boundary_touched"))
    if report.get("next_step") == "stop" and not report.get("stop_reason"):
        findings.append(_finding("missing_stop_reason", "error", "stop reports require stop_reason", "stop_reason"))
    return _finish("report", bundle, findings, stop=any(item["severity"] == "stop" for item in findings))


def validate_memory_access(bundle: Mapping[str, Any]) -> dict[str, Any]:
    request, error = _section(bundle, "memory_access", "memory_access")
    if error:
        return error
    if request is None:
        return _result("memory_access", bundle, "not_applicable", [], next_step="continue_review")
    findings: list[dict[str, Any]] = []
    _required(request, {"access_request_id", "actor_id", "role", "authorization_id", "memory_scope", "operation", "target", "requested_at", "decision", "decision_reason", "evidence_refs"}, findings, "memory_access")
    if request.get("role") not in ROLES:
        findings.append(_finding("invalid_role", "error", "memory access role is invalid", "role"))
    if request.get("memory_scope") not in MEMORY_SCOPES:
        findings.append(_finding("invalid_memory_scope", "error", "memory_scope is invalid", "memory_scope"))
    if request.get("operation") not in MEMORY_OPERATIONS:
        findings.append(_finding("invalid_memory_operation", "error", "operation is invalid", "operation"))
    if request.get("decision") not in MEMORY_DECISIONS:
        findings.append(_finding("invalid_memory_decision", "error", "decision is invalid", "decision"))
    _timestamp(request.get("requested_at"), "requested_at", findings)
    if request.get("decision") == "allowed":
        auth = bundle.get("authorization")
        if not isinstance(auth, Mapping):
            findings.append(_finding("missing_authorization", "stop", "allowed memory access requires explicit authorization", "authorization"))
        else:
            _valid_authorization(auth, require_approved=True, findings=findings)
            if auth.get("actor_id") != request.get("actor_id") or auth.get("role") != request.get("role"):
                findings.append(_finding("memory_actor_mismatch", "stop", "memory access actor and role do not match authorization", "authorization"))
            if request.get("memory_scope") not in auth.get("constraints", []) and auth.get("scope", {}).get("data_access") != "user_data":
                findings.append(_finding("memory_scope_not_explicit", "stop", "memory category is not explicitly authorized", "memory_scope"))
    if request.get("operation") == "read_content" and request.get("decision") != "allowed":
        findings.append(_finding("read_not_allowed", "stop", "content reads are deny-by-default", "decision"))
    return _finish("memory_access", bundle, findings, stop=any(item["severity"] == "stop" for item in findings))


def validate_boundary(bundle: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    declared = bundle.get("declared_scope")
    if not isinstance(declared, Mapping):
        return _result("boundary", bundle, "stop", [_finding("missing_declared_scope", "stop", "declared_scope is required", "declared_scope")], next_step="stop")
    if declared.get("environment") not in ENVIRONMENTS or declared.get("mutation") not in MUTATIONS:
        findings.append(_finding("invalid_declared_scope", "stop", "declared scope environment or mutation is invalid", "declared_scope"))
    if not isinstance(declared.get("paths"), list) or not all(isinstance(path, str) and path.strip() for path in declared["paths"]):
        findings.append(_finding("invalid_declared_paths", "stop", "declared paths must be explicit", "declared_scope.paths"))
    auth = bundle.get("authorization")
    if not isinstance(auth, Mapping):
        findings.append(_finding("missing_authorization", "stop", "boundary audit requires authorization", "authorization"))
    else:
        _valid_authorization(auth, require_approved=True, findings=findings)
        if not _authorization_scope_covers(auth, declared):
            findings.append(_finding("boundary_out_of_scope", "stop", "declared scope is outside authorization", "declared_scope"))
        if declared.get("environment") == "production":
            if auth.get("action") not in {"release", "rollback"} or auth.get("scope", {}).get("data_access") not in {"metadata", "operational"}:
                findings.append(_finding("production_boundary", "stop", "production scope requires explicit release or rollback authorization", "declared_scope.environment"))
    if declared.get("mutation") in {"code", "config", "data", "deployment"} and not isinstance(bundle.get("action"), Mapping):
        findings.append(_finding("missing_action_record", "stop", "mutating boundary requires an Action Record", "action"))
    if bundle.get("action", {}).get("foundation_boundary_touched") is True:
        findings.append(_finding("foundation_boundary_touched", "stop", "Foundation boundary touch is terminal STOP", "foundation_boundary_touched"))
    return _finish("boundary", bundle, findings, stop=any(item["severity"] == "stop" for item in findings))


VALIDATORS = {
    "authorization": validate_authorization,
    "action": validate_action,
    "report": validate_report,
    "memory_access": validate_memory_access,
    "boundary": validate_boundary,
}


def validate_bundle(bundle: Mapping[str, Any], check: str = "all") -> dict[str, Any]:
    if not isinstance(bundle, Mapping):
        raise TypeError("verification bundle must be an object")
    if check == "all":
        results = [validator(bundle) for validator in VALIDATORS.values()]
    elif check in VALIDATORS:
        results = [VALIDATORS[check](bundle)]
    else:
        raise ValueError(f"unknown check: {check}")
    return {"tool_version": 1, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline Anima Operator Contract validator")
    parser.add_argument("input", nargs="?", default="-", help="JSON bundle path, or - for stdin")
    parser.add_argument("--check", choices=["all", *VALIDATORS], default="all")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
        bundle = json.loads(raw)
        output = validate_bundle(bundle, args.check)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"tool_version": 1, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(item["result"] in {"pass", "not_applicable"} for item in output["results"]) else 3


if __name__ == "__main__":
    raise SystemExit(main())

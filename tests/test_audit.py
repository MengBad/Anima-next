import json
from anima.core.audit import AuditLogger
def test_audit_jsonl(tmp_path):
    a=AuditLogger(str(tmp_path)); a.emit("x", ok=True)
    row=json.loads(next((tmp_path/"audit").glob("*.jsonl")).read_text().strip()); assert row["type"]=="x" and row["ok"]
    assert a.recent(1)[0]["type"] == "x"

"""R6.5 —— Execution Evidence：完整行为证据系统（trace → artifact → audit → replay）。

## 目标（R6.5）

建立完整行为证据系统，使**任何 Agent 行为可以事后还原**：

    execution trace   —— 一次行为的完整行为轨迹（谁/什么/如何/结果/何时）
        ↓
    artifact          —— 行为产出的工件（输出、文件、副作用标记）
        ↓
    audit             —— 审计事件（决策/授权/隔离事实，与 R6.2/R6.3/R6.4 关联）
        ↓
    replay            —— 由 trace 还原一次行为（execution_id → 全过程）

## 本模块边界（加性新增，不动共享核心）

- **不修改** ``tools/sandbox.py``、``runtime/*``、``pipeline/*``、``capability/*``。
  本模块是**独立证据系统**：由未来接线点（owner 授权部署阶段）把每次执行的关键
  事实喂进来，或由运维脚本调用，形成可事后还原的证据链。单独测试时以构造的
  执行事实装配，证明 trace/artifact/audit/replay 闭环自洽。
- **记录不许说谎**：trace 只记录调用方给定的既定事实；artifact 只引用真实产生的
  工件（execution_id / 哈希 / 输出摘要），不伪造"可能发生过"的产物。
- **审计 fail-open**：证据存储写失败只计数，绝不阻断执行（与 R6.2 一致）。

## 模型

:class:`ExecutionTrace` —— 一次执行的行为轨迹：
- ``execution_id``：全局唯一执行标识（对账 ledger 的关键）
- ``principal`` / ``capability`` / ``decision`` / ``provider`` / ``sandbox`` / ``result``
- ``args_hash``：参数摘要哈希（不存原始敏感参数，只留指纹）
- ``resource_account``：资源使用（来自 R6.4）
- ``started_at`` / ``finished_at`` / ``latency``

:class:`ArtifactRecord` —— 行为产出工件：
- ``execution_id``：归属哪次执行
- ``name`` / ``kind``（stdout/file/url/state）/ ``sha256`` / ``size`` / ``created_at``

:class:`EvidenceStore` —— 持久化 trace + artifact（JSONL append-only，envelope）。
:class:`ReplayEngine` —— 由 execution_id 还原：trace + 关联 artifacts + 审计事件，
  产出人类可读的"行为还原报告"。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

#: 证据 JSONL 信封 schema 版本。
EVIDENCE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionTrace:
    """一次执行的行为轨迹（证据核心）。"""

    execution_id: str
    principal: str
    capability: str
    decision: str                       # allowed | denied
    provider: str
    sandbox: str
    result: str                         # success | denied | error | sandbox_failure
    args_hash: str                      # 参数摘要哈希（不存原始敏感参数）
    started_at: float
    finished_at: float
    resource_account: dict = field(default_factory=dict)  # R6.4 资源记账
    reason: str | None = None           # 决策理由
    denied_reason: str | None = None    # 拒绝原因
    role: str | None = None             # 生效角色（R6.3 RBAC）
    granted_scopes: tuple[str, ...] = ()  # 实际授予的 scope（R6.3）

    @property
    def latency(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)

    @property
    def success(self) -> bool:
        return self.result == "success"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactRecord:
    """行为产出工件（artifact）。"""

    execution_id: str
    name: str
    kind: str                           # stdout | file | url | state | error
    sha256: str
    size: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 指纹辅助
# ---------------------------------------------------------------------------
def args_fingerprint(args: dict[str, Any]) -> str:
    """对调用参数生成摘要哈希（不存原始敏感参数，只留指纹用于对账）。

    确定性序列化后 SHA256。敏感字段（code/command）只参与哈希、不落证据原文。
    """
    def _norm(v: Any):
        if isinstance(v, dict):
            return {str(k): _norm(x) for k, x in sorted(v.items())}
        if isinstance(v, (list, tuple)):
            return [_norm(x) for x in v]
        return str(v)

    canonical = json.dumps(_norm(args), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 持久化（JSONL append-only + envelope）
# ---------------------------------------------------------------------------
class EvidenceStore:
    """持久化 trace + artifact 的证据存储。

    信封形如 ``{"schema_version":1, "recorded_at":<float>, "kind":"trace"|"artifact",
    "payload":{...}}``。坏行计数跳过，不抛。append 不吞异常（fail-open 由 sink 层做）。
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self.parse_failures = 0
        self.unknown_versions = 0

    @property
    def primary_path(self) -> Path:
        return self._path

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        """追加一行信封（kind ∈ trace|artifact）。IO 错误如实上抛。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "recorded_at": time.time(),
            "kind": kind,
            "payload": payload,
        }
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")

    def append_trace(self, trace: ExecutionTrace) -> None:
        self.append("trace", trace.to_dict())

    def append_artifact(self, artifact: ArtifactRecord) -> None:
        self.append("artifact", artifact.to_dict())

    def read_all(self) -> list[dict[str, Any]]:
        """读全部记录：每个元素形如 ``{"kind":..., "payload":{...}}``。"""
        records: list[dict[str, Any]] = []
        if not self._path.exists():
            return records
        try:
            with self._path.open("r", encoding="utf-8") as file:
                lines = file.readlines()
        except OSError:
            self.parse_failures += 1
            return records
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                self.parse_failures += 1
                continue
            if row.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
                self.unknown_versions += 1
            records.append({"kind": row.get("kind"), "payload": row.get("payload")})
        return records


# ---------------------------------------------------------------------------
# Replay：由 execution_id 还原行为（事后还原）
# ---------------------------------------------------------------------------
class ReplayEngine:
    """由 execution_id 还原一次行为的完整证据。

    replay(execution_id) → 该执行的 trace + 关联 artifacts + 审计事件，产出
    人类可读"行为还原报告"。找不到 → 返回 None（不伪造"可能发生过"）。
    """

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def replay(self, execution_id: str) -> dict[str, Any] | None:
        trace = None
        artifacts: list[ArtifactRecord] = []
        for rec in self._store.read_all():
            kind, payload = rec.get("kind"), rec.get("payload")
            if kind == "trace" and payload.get("execution_id") == execution_id:
                trace = payload
            elif kind == "artifact" and payload.get("execution_id") == execution_id:
                artifacts.append(payload)
        if trace is None:
            return None
        return {
            "execution_id": execution_id,
            "trace": trace,
            "artifacts": artifacts,
            "audit": {
                "principal": trace.get("principal"),
                "capability": trace.get("capability"),
                "decision": trace.get("decision"),
                "provider": trace.get("provider"),
                "sandbox": trace.get("sandbox"),
                "result": trace.get("result"),
            },
            "reconstructable": True,
        }

    def replay_report(self, execution_id: str) -> str:
        """人类可读还原报告（"任何 Agent 行为可以事后还原"的可读证据）。"""
        result = self.replay(execution_id)
        if result is None:
            return f"[replay] execution {execution_id}: 无记录（无法还原）"
        t = result["trace"]
        lines = [
            f"[replay] execution {execution_id}",
            f"  principal : {t.get('principal')}",
            f"  capability: {t.get('capability')}",
            f"  decision  : {t.get('decision')} ({t.get('reason') or '-'})",
            f"  provider  : {t.get('provider')}",
            f"  sandbox   : {t.get('sandbox')}",
            f"  result    : {t.get('result')}",
            f"  latency   : {t.get('finished_at', 0) - t.get('started_at', 0):.3f}s",
            f"  artifacts : {len(result['artifacts'])}",
        ]
        return "\n".join(lines)


class EvidenceSink:
    """可注入的持久化证据 sink（写失败只计数，fail-open）。"""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store
        self.write_failures = 0

    @property
    def store(self) -> EvidenceStore:
        return self._store

    def record_trace(self, trace: ExecutionTrace) -> None:
        try:
            self._store.append_trace(trace)
        except Exception:  # noqa: BLE001 - 观测失灵不得让执行不可用（fail-open）
            self.write_failures += 1

    def record_artifact(self, artifact: ArtifactRecord) -> None:
        try:
            self._store.append_artifact(artifact)
        except Exception:  # noqa: BLE001
            self.write_failures += 1

    def __call__(self, event: dict) -> None:
        """把一条证据事件落盘（接受编排器/调用方传入的 flat dict）。fail-open。

        兼容可调用接口（``sink(event)``）：R7 编排器每阶段把证据 dict 投进来，
        本 sink 以信封 ``{kind:"event", payload:{...}}`` 落盘，供 R6.5 replay 还原。
        """
        try:
            self._store.append("event", dict(event))
        except Exception:  # noqa: BLE001 - 观测失灵不得让执行不可用（fail-open）
            self.write_failures += 1
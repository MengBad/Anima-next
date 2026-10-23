"""Typed constitution vocabulary — the state classes of the 0100 trilogy.

Grounded in:
- RFC-0100 §1.4 (Identity), §1.6 (Affect), §1.7 (Drive), §1.8 (Goal), §1.9 (Task)
- RFC-0100B §3 (state classes: Hybrid / Transient / Persistent), §4 (Ownership)

These dataclasses are *vocabulary*, not behavior. Derivation lives in the engine
modules (identity.py, need.py, ...). A state has NO setter method by design:
the only way to obtain a state is to derive it (or, for Goal, to have it adopted
by the Planning Authority). This is the single most important invariant the code
must not violate — it is what keeps the life a *life* and not an agent that edits
its own wants in place (RFC-0100 §1.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time


# --------------------------------------------------------------------------
# Memory — the canonical root store (RFC-0101: Identity = f(Memory))
# --------------------------------------------------------------------------
@dataclass
class MemoryRecord:
    """One experience / fact sedimented into Memory."""

    kind: str  # "episode" | "semantic" | "relation" | "skill"
    content: dict
    timestamp: float = field(default_factory=time.time)
    salience: float = 0.5  # how strongly it shapes Identity (0..1)


@dataclass
class Memory:
    """The canonical Memory store — root of the derivation chain.

    Owned by the **Memory Authority** (RFC-0100B). It is the ONLY direct writer
    besides the Planning Authority's Goal. Reflection writes HERE, never to Goal.
    """

    episodes: list[MemoryRecord] = field(default_factory=list)
    semantic: list[MemoryRecord] = field(default_factory=list)
    relations: list[MemoryRecord] = field(default_factory=list)

    def snapshot(self) -> "Memory":
        """Return a copy for derivation. f(Memory) must never mutate the source."""
        return Memory(
            episodes=list(self.episodes),
            semantic=list(self.semantic),
            relations=list(self.relations),
        )


# --------------------------------------------------------------------------
# Identity — canonical Hybrid, derived f(Memory) (RFC-0101)
# --------------------------------------------------------------------------
@dataclass
class Identity:
    """Who the life has become. Derived pure-function f(Memory); no setter.

    Cross-session persistence is *inherited* via Memory (RFC-0101): there is no
    separate Identity store. Re-derive each session from Memory.
    """

    traits: dict[str, float] = field(default_factory=dict)  # "curiosity": 0.8 ...
    values: list[str] = field(default_factory=list)  # what matters
    self_model: dict = field(default_factory=dict)  # narrative self
    #: 🔴 2026-10-23：**归一化前的真实峰值**（0..1）。
    #: `traits` 会被归一化成「峰值 =1.0」以便跨会话比较，但那样会**抹掉绝对强度**
    #: —— 一条 0.1 的微弱欲望和一条 1.0 的强烈欲望都变成 traits=1.0
    #: ⇒ `derive_need` 的 urgency 恒为 1.0 ⇒ Drive 永远激活、
    #: 「最想要什么」失去区分度（生产实测确认）。
    #: 保留原始峰值，让 urgency 能反映**真实强度**。
    peak_intensity: float = 0.0


# --------------------------------------------------------------------------
# Need — Transient-Derived, g(Identity) (RFC-0102)
# --------------------------------------------------------------------------
class WantVector(Enum):
    DESIRE = "desire"
    REQUIREMENT = "requirement"
    AVERSION = "aversion"
    CURIOSITY = "curiosity"
    SOCIAL = "social"


@dataclass
class Need:
    """The life's current want-vector. Derived g(Identity); no setter, no storage."""

    vectors: dict[WantVector, list[str]] = field(default_factory=dict)
    urgency: float = 0.0


# --------------------------------------------------------------------------
# Perception — instantaneous input to h(Need, Perception) (RFC-0103)
# --------------------------------------------------------------------------
@dataclass
class Perception:
    """The life's read of the current environment / utterance. Instantaneous input to
    ``h(Need, Perception)`` — NOT a stored state. Produced by the perception layer each
    tick; it is read-only for the derivation chain. Affect interprets it; it does not
    react to it (the ``Environment → Emotion → Action`` collapse is forbidden, RFC-0103
    Challenge C1).
    """

    signals: dict[str, float] = field(default_factory=dict)  # e.g. {"novelty": 0.7, "threat": 0.1}
    raw: str = ""


# --------------------------------------------------------------------------
# Affect — Transient-Derived, h(Need, Perception) (RFC-0103)
# --------------------------------------------------------------------------
@dataclass
class Affect:
    """Interpretation of how the life evaluates its need vs. current perception.

    Five dimensions (RFC-0100 §1.6). Derived h(Need, Perception); no setter; no
    persistence authority (long-term patterns sediment into Memory, not Affect).
    """

    valence: float = 0.0  # -1..1 pleasant/unpleasant
    arousal: float = 0.0  # 0..1 activation
    curiosity: float = 0.0  # 0..1
    confidence: float = 0.0  # 0..1
    urgency: float = 0.0  # 0..1 — feeds Drive activation


# --------------------------------------------------------------------------
# Drive — Transient-Derived, i(Affect) (RFC-0104)
# --------------------------------------------------------------------------
class OrientationAxis(Enum):
    APPROACH = "approach"
    AVOID = "avoid"
    INTENSITY = "intensity"
    FOCUS = "focus"


@dataclass
class Drive:
    """Operational orientation (not motivational content). Derived i(Affect).

    Exists only during one reasoning cycle (RFC-0104 D2); never restored.
    """

    orientation: dict[OrientationAxis, float] = field(default_factory=dict)
    activated: bool = False  # Need stays latent without Affect urgency


# --------------------------------------------------------------------------
# Goal — canonical Hybrid / Owned (RFC-0105)
# --------------------------------------------------------------------------
class GoalLifecycle(Enum):
    ADOPTED = "adopted"
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"


@dataclass
class Goal:
    """The life's current committed target. Owned + lifecycle-maintained by the
    Planning Authority (RFC-0105). NOT derived; NOT a Drive; NOT a Task.
    """

    target: str
    success_criterion: str
    horizon: str  # "weeks" / "months"
    priority: float = 0.0
    lifecycle: GoalLifecycle = GoalLifecycle.ADOPTED


# --------------------------------------------------------------------------
# PlannerContext — read-only situational snapshot for Plan construction (RFC-0106)
# --------------------------------------------------------------------------
@dataclass
class PlannerContext:
    """Read-only situational snapshot the Planning Authority reads when constructing Plan.

    Available capabilities, current environment, resources. Parallel to how Affect reads
    Perception (RFC-0103): it is an *input signal*, never a stored state. The Planning
    Authority reads it but never mutates it (RFC-0106 §5.1 C2).
    """

    capabilities: list[str] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Plan — Transient-Derived, p(Goal, Drive, Context) (RFC-0106)
# --------------------------------------------------------------------------
class PlanSource(Enum):
    """Origin of a Plan (RFC-0002 §5 `source`). Descriptive provenance only —
    NOT an authority grant. The Planning Authority remains the sole writer
    regardless of source (RFC-0002 §6 authority boundary)."""
    PLANNER = "planner"
    HUMAN = "human"
    MEMORY = "memory"
    EXTERNAL_API = "external_api"


class PlanStatus(Enum):
    """Runtime routing state of a Plan (RFC-0002 §5 `status` / §7).
    Data-only enum; transition *behavior* is owned by the Planning Interface
    (T2.1 / Slice 2) — not by this dataclass."""
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DEPRECATED = "deprecated"


# Plan contract version (RFC-0002 §5 `contract_version`, semver of the PLAN CONTRACT).
PLAN_CONTRACT_VERSION: str = "1.0.0"


# --------------------------------------------------------------------------
# Task — a single executable unit inside a Plan (RFC-0100 §1.9 / RFC-0106 §4 / RFC-0002 §5)
# --------------------------------------------------------------------------
@dataclass
class Task:
    """A single executable unit inside a Plan — the canonical *Task* of the §5 Plan contract.

    **Transient vocabulary, not a stored state** (RFC-0002 §8): a Task node has no setter and
    no storage authority; it is produced by the Planning Authority and consumed by Execution /
    Workflow (RFC-0003), never persisted. It carries only descriptors — it does NOT mutate any
    upstream state (Goal / Drive / Context) and does NOT execute capabilities (RFC-0001 owns
    that; ``capability_need`` here is metadata, not an action).

    This dataclass is the *representation layer* only. Population of ``Plan.task_graph`` with
    ``Task`` objects (in place of the current list-of-dicts emitted by ``derive_plan``) is the
    runtime-wiring slice (T3) — explicitly out of scope for T1.2. ``id`` is assigned at
    construction (T2/T3), consistent with ``Plan.id``: the default is empty so the pure-function
    ``derive_plan`` stays deterministic and no nondeterministic value is introduced here.
    """

    id: str = ""                       # unique per construction (assigned at build time, T2/T3); not a restore key
    title: str = ""                    # human-readable label
    intent: str = ""                   # what this Task contributes toward the Goal
    capability_need: dict = field(default_factory=dict)   # OPTIONAL declared need {type, args}; empty = none declared
    depends_on: list = field(default_factory=list)       # task ids this Task waits on (list[uuid])
    estimates: dict = field(default_factory=dict)        # optional planner hints (cost/latency/risk); not commitments


@dataclass
class Plan:
    """The life's operational structure for reaching its committed Goal (RFC-0106).

    ``Plan = p(Goal, Drive, Context)`` — **Transient-Derived**, *not* Owned (RFC-0106 §4 P1).
    Constructed by the Planning Authority for the duration of a Goal's pursuit; consumed by
    Execution; **never stored**. No setter, no storage authority (RFC-0106 P3). Plan is the
    *graph of Tasks* (RFC-0100 §1.15), not a single Task and not a Goal.
    """

    goal_target: str = ""          # the Goal this Plan pursues (content source, read-only)
    task_graph: list["Task"] = field(default_factory=list)   # decomposition: graph of Task nodes (RFC-0002 §5)
    ordering: list = field(default_factory=list)     # sequence / dependencies among Tasks
    branching: dict = field(default_factory=dict)    # conditional / fallback paths (Workflow shape)
    rollback: dict = field(default_factory=dict)     # unwind-on-failure (not a Plan lifecycle)
    context_ref: str = ""           # deterministic ref of the Context read (audit only)
    emphasis: str = ""              # "approach" | "avoid" — Drive orientation, read-only influence
    focus: float = 0.0              # Drive focus, read-only influence

    # --- RFC-0002 §5 canonical contract fields (Slice 1 / T1.1) ---
    id: str = ""                    # unique per construction (assigned at activation, T2/T3); not a restore key
    contract_version: str = PLAN_CONTRACT_VERSION  # semver of the PLAN CONTRACT (interface stability)
    goal_ref: str = ""              # ref to the Goal this Plan serves (read-only; set at wiring T1.2/T3)
    source: PlanSource = PlanSource.PLANNER  # origin-agnostic provenance (descriptive, not authority)
    status: PlanStatus = PlanStatus.DRAFT    # RUNTIME routing state (§7); transitions owned by Planning Interface (Slice 2)
    created_at: str = ""            # construction timestamp (assigned at construction, T2/T3); empty = not yet constructed
    drive_emphasis: str = ""        # RFC-0002 §5 `drive_emphasis` — "approach" | "avoid", Drive orientation, read-only
                                   #   (RFC-0002 §5 names the legacy `emphasis` field `drive_emphasis`; `emphasis` is
                                   #   retained under its current name in T1.1 to avoid touching the out-of-scope runtime
                                   #   consumer `execution.py`. Exact rename deferred — see slice report.)              # Drive focus, read-only influence


# --------------------------------------------------------------------------
# ExecutionContext — read-only runtime snapshot for carrying a Plan (RFC-0107)
# --------------------------------------------------------------------------
@dataclass
class ExecutionContext:
    """Read-only situational snapshot the Execution Runtime reads when carrying a Plan.

    Available capabilities, current environment, resources at execution time. Parallel to
    PlannerContext / Perception (RFC-0107 §5.1): it is an *input signal* to the carrying act,
    never a stored Constitution state. The Runtime reads it but never mutates it (RFC-0107 C2).
    """

    capabilities: list[str] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# ExecutionResult — Transient event output of carrying a Plan (RFC-0107)
# --------------------------------------------------------------------------
class ExecutionStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class ExecutionResult:
    """The event output of carrying a Plan into the Universe (RFC-0107).

    **Transient** — produced by Execution, consumed by Reflection → Memory, **never stored** as a
    Constitution state (RFC-0107 C3). It references the *Plan it consumed* (``plan_ref``), **not** the
    Goal that produced the Plan (RFC-0107 C1) — so it cannot reach back and mutate Goal ownership.
    No setter, no storage authority (RFC-0107 P1/V4). ``lessons`` is what sediments into Memory
    next cycle; ``status`` / ``outcome`` are the carrying record only.
    """

    plan_ref: str = ""             # deterministic ref of the consumed Plan (NOT a Goal reference)
    status: ExecutionStatus = ExecutionStatus.PENDING
    outcome: str = ""
    lessons: list = field(default_factory=list)   # what was learned — feeds Reflection → Memory
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# ReflectionResult — Transient output of Reflection's transform act (RFC-0108)
# --------------------------------------------------------------------------
@dataclass
class ReflectionResult:
    """The records Reflection decides to sediment after interpreting an ExecutionResult.

    **Transient** — produced by Reflection (Reflection Authority), consumed by the
    **Memory Authority** (the *only* legitimate sink), **never stored** as a Constitution
    state (RFC-0108 C3/V4). It references the *ExecutionResult it consumed* (``result_ref``),
    **not** the Goal that produced the Plan (RFC-0108 C1 — so Reflection cannot reach back and
    mutate Goal ownership), and **not** the Plan (RFC-0107 C1). No setter, no storage authority.
    ``records`` is the *learned* shape that enters Memory; the transform act itself is not persisted.
    """

    result_ref: str = ""           # deterministic ref of the consumed ExecutionResult (NOT Goal/Plan)
    records: list = field(default_factory=list)   # list of {"kind","content","salience"} candidates
    timestamp: float = field(default_factory=time.time)


# --------------------------------------------------------------------------
# Persona — Derived projection of Identity (RFC-0110)
# --------------------------------------------------------------------------
@dataclass
class Persona:
    """The presentation mask — a Derived projection of Identity (RFC-0110).

    Persona is the *expression face* of the self, NOT a second Identity root and NOT a
    store. Derived pure-function projection of Identity; no setter, no storage authority
    (RFC-0110 P1/C3). It references the Identity it projected (``identity_ref``), never
    holds a mutable handle to Identity, so it cannot reach back and rewrite the root
    (RFC-0100B single-Identity-root rule, line 162; RFC-0110 C1/C2). Re-projected each
    Recall (RFC-0100 §1.4); never stored, never restored. Deterministic: same Identity
    ⇒ same Persona (no ``timestamp`` / no hidden state) — it is a *reading*, like Need.
    """

    identity_ref: str = ""        # deterministic ref of the projected Identity (NOT an Identity handle)
    voice: str = ""               # how the self presents (derived from traits/values)
    presentation: dict = field(default_factory=dict)   # the expression projection (read-only view)


# --------------------------------------------------------------------------
# RelationshipProjection — Derived projection of (Memory.relations ∩ Identity) (RFC-0111)
# --------------------------------------------------------------------------
@dataclass
class RelationshipProjection:
    """The model the life holds of an external agent — a Derived projection (RFC-0111).

    Relationship is the *relational model*: the life's held view of *another* agent (human
    or bot), computed from ``Memory.relations`` (filtered by ``external_ref``) intersected
    with ``Identity``. It is **not** a second Memory, **not** a motivation source, **not** a
    second Identity system. Derived pure-function projection of Memory + Identity; no setter,
    no storage authority (RFC-0111 P1/C3). It references the sources only by immutable refs
    (never mutable handles), so it cannot reach back and rewrite the root (Identity) or the
    sink (Memory) — the three relationship risks the user named (Relationship → Memory bypass,
    Relationship → Goal mutation, Relationship → Identity rewrite) are mechanically closed
    (RFC-0100B single-Identity-root / single-Memory rules, lines 162/167; RFC-0111 C1/C2/C3).
    Re-projected each Recall (RFC-0100 §1.13); never stored, never restored. Deterministic:
    same inputs ⇒ same RelationshipProjection (no ``timestamp`` / no hidden state) — it is a
    *reading*, like Need / Persona.
    """

    external_ref: str = ""       # the modeled external agent (Environment object; not owned by Relationship)
    trust: float = 0.0           # modeled trust in the external agent (0..1), derived from relation records
    role: str = ""               # modeled role of the external agent (derived)
    history: list = field(default_factory=list)   # derived summary of relation records (read-only view)
    memory_ref: str = ""         # deterministic ref of the Memory.relations slice read (NOT a Memory handle)
    identity_ref: str = ""       # deterministic ref of the Identity read (NOT an Identity handle)


# --------------------------------------------------------------------------
# WorkflowRun — RFC-0003 §5 run-state vocabulary (Infrastructure Workflow Authority)
# --------------------------------------------------------------------------
class RunStatus(Enum):
    """Lifecycle routing state of a WorkflowRun (RFC-0003 §5 `status` / §7).

    Data-only enum; transition *behavior* is owned by the Workflow Interface (D2 slice) —
    not by this dataclass. Mirrors the ``PlanStatus`` / ``ExecutionStatus`` vocabulary pattern
    (RFC-0002 §5 / RFC-0107): the enumeration carries no writer.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRunStatus(Enum):
    """Per-task execution state inside a WorkflowRun (RFC-0003 §5 `task_states[*].status`).

    Data-only enum; resolution of the run-level F-04 completion/failure judgment is owned by
    the Workflow Interface (D2/D4 slices), not by this dataclass.
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# WorkflowRun contract version (RFC-0003 §5 `contract_version`, semver of the WORKFLOWRUN CONTRACT).
WORKFLOWRUN_CONTRACT_VERSION: str = "1.0.0"


@dataclass(frozen=True)
class PlanRef:
    """Opaque, immutable handle to a Plan — the *only* way a WorkflowRun references a Plan
    (RFC-0003 §5 `plan_ref`, F-02).

    It holds **no** Plan object, **no** task_graph, and grants **no** Plan storage or lifecycle
    authority. The Plan stays transient and owned solely by the Planning Authority (RFC-0002);
    the Workflow reads Plan content at ``start_run`` time via the Planning Interface (D3 slice)
    and never holds it. Frozen to enforce opacity — a handle cannot be mutated into a live Plan
    reference, and it cannot acquire a status writer.
    """

    plan_id: str = ""
    contract_version: str = PLAN_CONTRACT_VERSION  # matches the referenced Plan's contract semver


@dataclass
class TaskState:
    """The run-local state of a single Task within a WorkflowRun (RFC-0003 §5 `task_states[*]`).

    Pure vocabulary: it records an outcome snapshot, it does not execute and it carries no status
    writer. The Workflow Interface owns the *transition* of run/task state (D2/D4 slices); this
    class only holds the recorded value. ``result`` / ``error`` are transient recorded payloads,
    not stored Constitution state (RFC-0003 §4.1 G4).
    """

    task_id: str = ""                             # corresponds to a Task.id in the consumed Plan
    status: TaskRunStatus = TaskRunStatus.PENDING
    attempts: int = 0                             # retry count observed for this task
    result: dict = field(default_factory=dict)    # recorded outcome payload (transient)
    error: str = ""                               # recorded failure reason (transient)


@dataclass
class WorkflowRun:
    """The Infrastructure Workflow Authority's run-state record for executing one Plan
    (RFC-0003 §5). Vocabulary only — no setter, no storage authority, no Plan lifecycle writer.

    The Workflow consumes a Plan **read-only** (RFC-0003 §1.2 / §4.1): it references the Plan
    exclusively through the opaque ``plan_ref`` handle (F-02); it never stores the Plan object,
    never mutates ``Plan.status`` (owned solely by the Planning Interface), and never persists the
    Plan. ``task_states`` are derived snapshots of the consumed Plan's ``task_graph`` taken at
    ``start_run`` time (D3 slice), not live references back into the Plan.

    This dataclass is the *representation layer* only. The Workflow Interface (run-state single
    writer, O1) and the ``start_run`` / ``report_task_outcome`` behavior belong to the D2/D3/D4
    slices — explicitly out of scope for D1.
    """

    id: str = ""                                       # unique per construction; not a restore key
    contract_version: str = WORKFLOWRUN_CONTRACT_VERSION  # semver of the WORKFLOWRUN CONTRACT
    plan_ref: PlanRef = field(default_factory=PlanRef)    # opaque handle {plan_id, contract_version} (F-02)
    goal_ref: str = ""                                 # ref to the Goal the consumed Plan serves (read-only)
    status: RunStatus = RunStatus.PENDING              # RUNTIME routing state (§7); transitions owned by Workflow Interface (D2)
    task_states: list = field(default_factory=list)    # list[TaskState] — derived run-local snapshots (not live Plan refs)
    current_task_id: str = ""                          # TaskState in focus (empty iff status not in {running, suspended})
    checkpoint_ref: str = ""                           # opaque ref to the latest recovery checkpoint (F-05)
    retry_policy: dict = field(default_factory=dict)   # METADATA ONLY (policy hints; not an action)
    schedule_ref: str = ""                             # opaque metadata ref (RFC-0003 §5)
    budget_ref: str = ""                               # opaque metadata ref (RFC-0003 §5)
    context_ref: str = ""                              # opaque metadata ref (RFC-0003 §5)
    created_at: str = ""                               # construction timestamp (assigned at construction); empty = not yet constructed
    completed_at: str = ""                             # terminal timestamp (assigned by Workflow Interface at D2); empty = still running


# --------------------------------------------------------------------------
# WorkflowExecutionResult — the Workflow's run-terminal ledger (RFC-0003 §5, F2)
# --------------------------------------------------------------------------
@dataclass
class WorkflowExecutionResult:
    """The run's terminal execution ledger (RFC-0003 §5), reported by the Workflow Authority.

    **Transient** — the run-completion ledger: a snapshot of ``WorkflowRun.task_states`` taken
    at a terminal transition, handed to the Planning Authority / audit (RFC-0003 §5: the Workflow
    reports its run's terminal output; it does **not** write Memory). Deliberately distinct from
    ``ExecutionResult`` (RFC-0107): that is Execution's *observation envelope* for Reflection
    (``status``/``outcome``/``lessons``), whereas this is the Workflow's *lifecycle ledger*
    (``task_results``). Two authorities, two producers, two consumers — one shared name would
    have conflated them (F1 reconciliation, C-scheme).

    ``task_results`` are **snapshots**, never live references back into a ``WorkflowRun``: the
    run's own ``task_states`` may mutate no further after a terminal transition, but the ledger
    must not hold a mutable handle into run-state (mirrors the ``TaskState`` snapshot discipline
    of ``outcome_to_task_state`` / ``plan_to_run_tasks``). No setter, no storage authority; the
    Workflow Authority owns its construction (F2).
    """

    run_id: str = ""                                   # the WorkflowRun this ledger concludes
    plan_ref: str = ""                                 # opaque Plan ref of the concluded run (F-02)
    goal_ref: str = ""                                 # ref to the Goal the run served (read-only)
    status: RunStatus = RunStatus.PENDING              # terminal verdict of the concluded run
    task_results: list = field(default_factory=list)   # list[TaskState] — per-Task snapshot (copy)
    summary: str = ""                                  # short human-readable conclusion
    completed_at: str = ""                             # terminal timestamp (echo of run.completed_at)


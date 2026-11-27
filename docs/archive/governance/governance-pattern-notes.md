# Governance Pattern Observations (M0.5)

> **Status: Observation only — NOT a specification.**
> Language used: *Observed* / *Pattern* / *Hypothesis* / *Open*. No `SHALL` / `MUST` / "standard".
> This note does **not** freeze the process. It is a running field log to support a future abstraction — *if and only if* enough samples confirm the pattern.

---

## 1. Sample base (what has actually been completed)

| Sample | Track | Primary artifact | Cadence reached | HEAD commit |
|---|---|---|---|---|
| **S1** | M0.4.x Pilot Activation | `M0.4.8-*` governance set | Draft → Review → r3 → Decision Package → *(Human Approval pending)* | `18d6300` (frozen) |
| **S2** | M0.5 infra RFC | `RFC-0002-Planning` (+ Recon/Study/Decision-Input/Authority-Study) | pre-Draft analysis → Draft v0 → v0.1 → Review → r1 → Decision Package → *(Human Approval pending)* | `a7da73f` |

Both samples belong to the **same governance family** (M0.5 infra-track RFCs + a pilot RFC under the identical cadence). **Sample size = 2.** This is the central reason this note is observation-only (see §5).

---

## 2. Observed stable pattern (2 / 2 samples)

- **Pattern C1 — Fixed governance cadence.**
  `Draft → Independent Review → r-revision → Decision Package → Human Approval → (Implementation)`.
  Both S1 and S2 reached Decision Package via this sequence. The only variation is *how many pre-Draft analysis steps* preceded Draft (see §3).

- **Pattern C2 — Docs-only until Approval.**
  In both samples, zero `*.py` changes were made before the Human Approval gate. Code, flag-flip, and provider execution stayed outside the governance phase. The 3 `capability_contracts/*.md` specs remained untracked/unsigned throughout.

- **Pattern C3 — Wall-off settled governance from Review.**
  In S2, Canonical Plan Authority (A2) was an accepted ruling *before* Review; the Review brief stated "A2 is out of scope for review" and the reviewer verified *application*, not re-litigation. S1 had an analogous wall-off (RFC-0001 Capability substrate treated as fixed baseline).

- **Pattern C4 — 4-doc Decision Package shape.**
  S1 (`M0.4.8-GOVERNANCE-DECISION / -DECISION-MATRIX / -APPROVAL-RECORD-TEMPLATE / -FINAL-SCOPE-CONFIRMATION`) and S2 (`RFC-0002-*` equivalents) used the same 4-doc structure. 2 / 2.

---

## 3. Steps that appeared but are CONDITIONAL (sample-specific hypothesis)

S2 contained a pre-Draft analysis chain that S1 did **not**:
`Planning Recon → Plan-Unification Study → Decision Input → Plan-Authority Study`.

- **Hypothesis H1.** These pre-Draft steps trigger only when a **definitional collision / canonical-ownership ambiguity** exists. S2 had 3 competing Plan definitions across the 000x / 010x tracks; S1 (Pilot) had no such collision and skipped them.
- **Open O1.** Does RFC-0003 (Workflow) need a Recon / Study chain? Workflow has its own collision surface (production `AgentTask`/`Step` vs RFC-0106 reserved Workflow interface point). Likely yes — *unconfirmed*.
- **Open O2.** If 0003 / 0004 / 0005 confirm H1, the pre-Draft chain becomes a *conditional submodule*, not a universal step.

---

## 4. Observed human governance judgments (recurring candidate rulings)

These were human rulings during S2, **not** agent proposals. Listed as *observed*, not *codified*:

- **Judgment J1 — Frozen ≠ blocks RFC.** "Foundation Frozen" blocks *stealth modification* of Foundation, not *drafting / discussing an RFC that references it*. Correct flow: Draft → discover Foundation impact → label "Requires Foundation Evolution (F1/F2)" → await decision. (Agent initially mis-derived Frozen ⇒ cannot-start; human corrected.)
- **Judgment J2 — Origin ≠ Authority.** A Plan's `source` (provenance: human / memory / API) is distinct from the canonical owner (Planning Authority per A2). Reviewer confused the two; human downgraded F-02 from Major to Editorial with a one-line clarification, not a new governance flow.
- **Judgment J3 — Draft must describe, not conclude.** In-Draft proposals about *undecided classifications* (e.g. "AgentTask belongs to RFC-0003") are out-of-scope; defer to the owning RFC using governance language ("out of scope, deferred to RFC-0003"), never a premature conclusion.

---

## 5. Why this note is NOT a spec (yet)

- Sample size = 2, both in the same governance family. Risk of over-fitting "accidental steps" as "standard process" is real.
- Human-stated bar before abstraction: **RFC-0002, 0003, 0004, 0005 all complete**, with the cadence `Recon → Study → Draft → Review → Revision → Decision Package → Human Approval` stably recurring.
- At that point, a `governance-decision-package` skill could be extracted with confidence. Not before.

---

## 6. Open items for future samples

- **O1** — Does 0003 need pre-Draft Recon / Study? (tests H1)
- **O2** — Do 0004 / 0005 introduce cadence steps not seen in S1 / S2?
- **O3** — Is the 4-doc Decision Package shape stable across all four, or does 0005 (Multi-Agent) need a different shape?
- **O4** — Does the "wall-off settled governance" pattern hold when a later RFC *must* revisit an earlier ruling? (not yet observed)
- **O5** — Does the "docs-only until Approval" invariant survive the first real Implementation phase (post-Approval)?

---

*Last updated: 2026-08-07. Next revision expected after RFC-0003 completes.*

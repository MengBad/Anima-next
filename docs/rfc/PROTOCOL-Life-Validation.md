# PROTOCOL: Life Validation (Life-Architecture Validation Protocol)

> **Status**: Locked — 2026-08-04, after RFC-0104 Challenge. Consumed by every Life-layer RFC from
> RFC-0105 onward.
> **Role**: The *reusable content* of LAWP Phase C (Challenge). It standardizes **what** each
> Life-layer RFC must validate, so the pattern is no longer re-derived or copy-evolved across RFCs.
> **Layer**: Protocol (Document Taxonomy — empty of Ontology/RFC/ADR content; see §7).
> **Depends-on**: LAWP (`PROTOCOL-Architect-Loop.md`, Phase C), Foundation Evolution Protocol
> (`PROTOCOL-Foundation-Evolution.md`, F1–F4).

---

## 0. Why this Protocol exists

RFC-0101 → RFC-0102 → RFC-0103 → RFC-0104 each ran the same Challenge and each surfaced the *same
shape* of clarification:

| RFC | Upstream bypass | Downstream bypass | Authority Boundary | Anti-persistence | Cycle | Foundation Stable |
|-----|-----------------|-------------------|--------------------|------------------|-------|-------------------|
| 0101 Identity | `Memory→Identity` only | — | Derivation ≠ Derivation-of-Need | `identity.update()` banned | Transient-Derived | ✅ |
| 0102 Need | `Memory→Identity→Need` | no `Need→Goal` | Identity Auth governs Need, not Identity | `Need.save()` banned | Transient-Derived | ✅ |
| 0103 Affect | `Memory→…→Affect` | no `Affect→Goal` | Affect Auth ≠ Drive Auth | `Affect.save()` banned | Transient-Derived | ✅ |
| 0104 Drive | `Memory→…→Drive` | no `Drive→Goal` | Drive Auth ≠ Planning Auth | `Drive.save()` banned | one cycle | ✅ |

This is **not** the content of any single Life RFC — it is a **methodology**. Leaving it scattered
across four documents risks drift (each RFC re-invents the checklist slightly differently) and
duplication (each new RFC copy-pastes the prior one's callouts). This Protocol freezes the six checks
as the single source of truth. From RFC-0105, every Life RFC *references* this Protocol; it does not
re-derive it.

---

## 1. Scope — the six mandatory checks

Every Life-layer RFC, at LAWP Phase C, **must** walk all six checks against its target state `X`.
Each check is a binary PASS/FAIL; a FAIL routes to the Foundation Evolution Protocol (F1–F4), never to
an ad-hoc edit.

### V1 — Upstream bypass (anti-bypass)

> Does `X` read **only** its immediate upstream via the defined derivation (`f`/`g`/`h`/`i`/…), and
> **never** an earlier joint (Memory, Identity, Need) directly?

- PASS: `X = φ(immediate_upstream)`; any influence from further-upstream arrives *only* transitively
  through the chain (`Memory → Identity → Need → Affect → Drive → …`).
- FAIL: `X = φ'(Memory)` or `X = φ''(Need)` that reads around an intermediate authority.
- Established at: RFC-0102 §4.3 (Need), RFC-0103 §5.2 (Affect), RFC-0104 §5.2 (Drive).

### V2 — Downstream bypass

> Does `X` feed **only** its immediate downstream authority, and **never** skip to a non-adjacent
> joint?

- PASS: `X → next_authority` only.
- FAIL: `X → Goal`, `X → Plan`, or any edge that jumps over the adjacent downstream node.
- Established at: RFC-0103 Boundary (no `Affect→Goal`; only `Affect→Drive→Goal`), RFC-0104 Boundary B
  (no `Drive→Goal`; only `Drive→Planner→Goal`).

### V3 — Authority Boundary

> Does the owning Authority govern **derivation of `X`**, and **neither** mutate upstream **nor** adopt
> downstream?

The owning Authority of `X` owns the rule `X = φ(upstream)`. It may **read** upstream and **emit** to
downstream, but must **never** write them. Use the **symmetric fixed format**:

```
X Authority governs X derivation,
not <upstream> mutation,
not <downstream> adoption.
```

- PASS: the boundary is stated in this form; code shapes `xAuthority.derive_x()` are correct,
  `xAuthority.set_upstream()` / `xAuthority.choose_downstream()` are mis-assignments (collapse two
  authorities, violate O1).
- Established at: RFC-0103 §6.2 C2 (`Affect Authority governs Affect derivation, not Need mutation`),
  RFC-0104 §6.2 Boundary B (`Drive Authority governs drive derivation, not goal adoption`).

### V4 — Persistence (anti-persistence)

> Is `X` a **derived state with no storage authority**?

- PASS: no `X.save()` / `X.persist()` / `X.update()` / `X.restore()`; no `XMemory` / `XStore` /
  `XDatabase`. Any long-term pattern that *looks* like `X` belongs in **Memory** (the only legitimate
  long-term writer), where it re-enters the cycle as `f → … → X` next cycle.
- FAIL: provisioning an `X` store; a second writer that duplicates state owned by another authority
  (breaks O1).
- Established at: RFC-0101 §6 (anti-`identity.update()`), RFC-0102 §8, RFC-0103 §5.4, RFC-0104 §5.4 / §8.

### V5 — Cycle (transient lifetime)

> Does `X` exist **only within one reasoning cycle**, and get re-derived (not restored) each cycle?

- PASS: `X` is Transient-Derived (RFC-0100B §3.2); re-computed from its upstream at cycle start;
  discarded at cycle end; no `X.restore()` across cycles. Cross-cycle continuity is *inherited* from
  upstream (ultimately Memory), never stored in `X`.
- Established at: RFC-0102 §4.2, RFC-0103 §5.4, RFC-0104 §4.2 (D2).

### V6 — Foundation Stability (the Gate)

> Does any of V1–V5 require a **base edit** (new noun / new flow / new authority / new state class)?

- If **yes** for any: the RFC proposes a Foundation patch via the Foundation Evolution Protocol (F1 only
  a Foundation Validation RFC may propose; F2 source must be an *observed contradiction*; F3 must name
  the future inconsistency removed; F4 must *shrink* ambiguity, never expand theory). The RFC does
  **not** edit the Foundation inline.
- If **no** for all: the Gate verdict is **Foundation Stable → YES**, and the RFC may anchor.

---

## 2. Mapping to LAWP Phase C

This Protocol *is* the content of LAWP Phase C (Challenge). The four LAWP questions map onto the six
checks as:

| LAWP Phase C question | Life Validation checks |
|-----------------------|------------------------|
| C1 — Missing Foundation concept? | V6 (any new noun/flow/authority?) |
| C2 — Internal contradiction? | V1 (upstream), V2 (downstream), V6 |
| C3 — Missing Authority? | V3 (boundary), V4 (persistence), V5 (cycle) |
| C4 — Implied Stored State? | V4, V5 |

A Life RFC records its Phase C as: *"ran Life Validation Protocol V1–V6; results in §13.4."* It no
longer re-lists the checks prose-by-prose.

---

## 3. The symmetric Authority-boundary convention (canonical form)

V3 is the single most-repeated clarification. Freeze the wording so every joint reads identically:

> **`<State> Authority governs <state> derivation, not <upstream> mutation, not <downstream> adoption.`**

Examples already in the base:

- `Identity Derivation Authority governs Identity derivation, not Memory mutation.` (Memory is owned by Memory Authority)
- `Identity Authority governs Need derivation, not Identity mutation.` (RFC-0102 §6.2)
- `Affect Authority governs Affect derivation, not Need mutation, not Goal adoption.` (RFC-0103 §6.2 C2)
- `Drive Authority governs drive derivation, not Affect/Need mutation, not goal adoption.` (RFC-0104 §6.2 B)

Future Life RFCs (Goal, Planner, Execution, …) extend the same line: each state's Authority governs
*its* derivation and names its upstream/downstream it must **not** touch.

---

## 4. Worked template (copy into each Life RFC's §13.4)

```
### 13.4 Challenge (LAWP Phase C — Life Validation Protocol V1–V6)

| Check | Result | Note |
|-------|--------|------|
| V1 Upstream bypass | ✅ PASS | X = φ(immediate upstream) only |
| V2 Downstream bypass | ✅ PASS | X → next authority only; no skip |
| V3 Authority Boundary | ✅ PASS | "<X> Authority governs X derivation, not …" |
| V4 Persistence | ✅ PASS | no X.save/persist/update/restore |
| V5 Cycle | ✅ PASS | X re-derived per cycle; no restore |
| V6 Foundation Stability | ✅ PASS | no base edit → Stable |

Clarifications added (all F4, zero base edits): D1 … D2 … D3 …

Challenge verdict → Foundation Stable: YES.
```

---

## 5. Relationship to the other protocols

- **LAWP** defines the *workflow* (Ground → Draft → Challenge → Decision → Anchor). This Protocol is
  the *content* of the Challenge step.
- **Foundation Evolution Protocol** defines *when* a Foundation change is allowed (F1–F4). V6 routes
  here when a check fails.
- **This Protocol** defines *what* a Life RFC validates, so the loop is consistent and copy-free.

The three together close the governance model:

```
LAWP ─────────────► how to think (workflow)
  └─ Phase C ──────► Life Validation Protocol (what to check, V1–V6)
        └─ V6 FAIL ─► Foundation Evolution Protocol (when base may change, F1–F4)
```

---

## 6. Non-goals

- This Protocol does **not** define any Life-layer *content* (that is RFC-0100 + the Life RFCs).
- It does **not** replace LAWP or Foundation Evolution; it composes with them.
- It does **not** expand the Foundation. Every check is a *reading* of existing 0100/0100A/0100B; a
  check that needs a new base element is, by definition, a Foundation Evolution case (V6 → F1–F4).

---

## 7. Document Taxonomy

This file is **Protocol**-category. It contains no Ontology (`what`), no RFC (`a specific design`),
no ADR (`a decision record`). It is governance method, analogous to `PROTOCOL-Architect-Loop.md` and
`PROTOCOL-Foundation-Evolution.md`. The three Protocol files together form the Life-Architecture
working-method layer; the Ontology/RFC/ADR layers sit beneath them.

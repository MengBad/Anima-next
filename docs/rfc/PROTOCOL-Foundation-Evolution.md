# Foundation Evolution Protocol

> **Document Taxonomy**: Protocol (governance). Defines the conditions under which the Foundation
> (RFC-0100 / RFC-0100A / RFC-0100B) may be modified. Deliberately empty of Ontology / RFC / ADR
> content.
> **Status**: Locked — effective immediately.
> **Depends-on**: LAWP (`PROTOCOL-Architect-Loop.md`) and the Foundation it governs
> (RFC-0100 / RFC-0100A / RFC-0100B).
> **Scope**: every proposed modification to the Foundation. Does not govern Life RFCs themselves
> (those follow LAWP); it governs the *Patch Foundation* arrow inside LAWP Phase E.

---

## 0. Why this exists

The Foundation has crossed from **Invention** into **Refinement**. Recent changes —
`Owner → Authority`, `Semantic Process → Semantic Contract`, `Mutation → Ownership` — all
*removed* implementation coupling and *collapsed* distinctions; none *added* theory. The base is
getting shorter, not longer. This Protocol freezes that direction: a Foundation patch is allowed
only when it shrinks, and only when a Life RFC earned the right to propose it.

---

## 1. The question this Protocol answers

LAWP already routes a Life RFC to *Foundation Patch Required* (Phase D, Exit 2) and tells the Agent
to patch the Foundation in its own commit (Phase E). This Protocol answers the missing rule:
**what makes a proposed patch acceptable?**

A patch that fails this Protocol is rejected — the Life RFC must instead be revised so it does not
need the patch (its Decision should have been *Foundation Stable*).

---

## 2. Rule F1 — only a Foundation Validation RFC may propose a patch

- A Foundation patch may be proposed **only** by a Life-layer RFC that has run the full Architect
  Loop and reached the **Foundation Patch Required** verdict (LAWP Phase D, Exit 2).
- "Ordinary RFCs" — Infra RFCs (0000/0001), ADRs, notes, or any doc not acting as a Foundation
  Validation RFC — **may not** propose Foundation changes.
- Consequence: a patch always traces to a concrete Life RFC that *needed* it under Challenge.

## 3. Rule F2 — source must be an observed contradiction, not a better idea

- **Valid source**: an *Observed contradiction* — the Life RFC, under Challenge, hit a real conflict
  (with the trilogy, or within the trilogy) that blocks progress.
- **Invalid source**: *"I have a better idea"* — aesthetic preference, cleverness, or theoretical
  ambition with no observed conflict.
- If the only motivation is elegance, the patch is rejected (see F3 / F4).

## 4. Rule F3 — state the future inconsistency removed, not why it is cleaner

Every patch must explicitly answer:

> **Which future inconsistency does this remove?**

- Acceptable: *"Without collapsing Owner→Authority, every future Life RFC will re-conflate process
  and contract, producing contradictory ownership claims."*
- Unacceptable: *"Authority is a cleaner abstraction."* (Cleanliness without a named future
  inconsistency is insufficient.)

## 5. Rule F4 — shrink ambiguity, never expand theory

- A patch must **shrink ambiguity**: remove implementation coupling, collapse a redundant
  distinction, tighten a definition, shorten the base.
- A patch must **not expand theory**: add no new life-noun, no new state class, no new flow, no new
  Authority absent an observed contradiction.
- Heuristic: after the patch, the Foundation should be *shorter* or *tighter* than before. If it is
  longer, the patch is presumed invalid until F2 / F3 are overwhelmingly shown.
- Rejected example: promoting the Constitutional-state hypothesis (RFC-0101 Appendix A) into 0100B
  *now* — it would **expand** the state-class list with no observed contradiction; it stays parked
  until a real consumer needs it.

---

## 6. Valid vs invalid (from actual history)

| Change | Verdict | Why |
|--------|---------|-----|
| `Owner → Authority` (0100B) | ✅ valid | Observed conflation of process/contract; shrinks; removes coupling. |
| `Semantic Process → Semantic Contract` (0100B) | ✅ valid | Tightens definition; shrinks; no new noun. |
| `Mutation → Ownership` (0100B) | ✅ valid | Collapses two ideas into the more essential one; shorter. |
| "Add a 4th state class because it's elegant" | ❌ invalid | Expands theory; no observed contradiction (fails F2 / F4). |
| Promote Constitutional hypothesis pre-consumer | ❌ invalid | Expands; parked per F4 until a real RFC needs it. |

---

## 7. Relation to LAWP

This Protocol is the acceptance gate on LAWP's *Patch Foundation* step:

```
LAWP Phase D: Foundation Patch Required
        │
        ▼
   propose patch ──┐
        │           │  fails F1–F4 ?
        ▼           ▼
  Foundation    revise Life RFC so it
  Evolution     does NOT need the patch
  Protocol ──(pass)──►  (Decision becomes Stable)
        │
        ▼
  patch Foundation in its own anchored commit (LAWP Phase E)
        │
        ▼
  re-Ground → re-Draft → re-Challenge → re-Decision
```

A patch that passes F1–F4 is committed as its own zero-code, rollbackable Foundation commit, then
the Life RFC re-runs the Loop against the patched base.

# Anima-next — Architecture (Project Navigation)

> ❄️ **FOUNDATION DEFAULT-FROZEN.** The 0100 trilogy (Ontology / Dynamics / State Ownership) is the
> closed base. Every Life-layer RFC's *default assumption* is **"I will NOT modify the Foundation."**
> Foundation changes only via the **Foundation Evolution Protocol** (F1–F4), triggered by a
> Foundation Validation RFC. The 0100 series is **frozen by default**, not continuously built.

This file is a **navigation entry point**, not a theory document. It is intentionally **outside**
the Document Taxonomy (it is not an Ontology / RFC / ADR / Protocol file). Start here, then read
the Foundation in the order below.

---

## Reading order (do not skip)

1. **Manifesto** — *why this project exists* → `docs/rfc/RFC-0100-Digital-Life-Ontology.md` §0
2. **RFC-0100 Ontology** — *what exists* (WHAT) → `docs/rfc/RFC-0100-Digital-Life-Ontology.md`
3. **RFC-0100A Dynamics** — *how life flows* (HOW) → `docs/rfc/RFC-0100A-Digital-Life-Dynamics.md`
4. **RFC-0100B State Ownership** — *who governs change* (WHO) → `docs/rfc/RFC-0100B-State-Ownership.md`
5. **LAWP (Architect Loop)** — *how the Agent thinks* (Working Protocol) → `docs/rfc/PROTOCOL-Architect-Loop.md`
6. **Foundation Evolution Protocol** — *when Foundation may change* (patch gate) → `docs/rfc/PROTOCOL-Foundation-Evolution.md`
7. **Life Validation Protocol** — *what each Life RFC must validate* (LAWP Phase C checklist, V1–V6) → `docs/rfc/PROTOCOL-Life-Validation.md`

> ⚠️ **Do NOT start with RFC-0102.** RFC-0102 is a *Foundation Validation #2* RFC — a vehicle that
> **consumes** the Foundation, not the entry point and not base theory. New Life-layer RFCs are
> validation probes; they prove the Foundation is sufficient, they do not expand it.

---

## Big picture — the governance stack

```
                         ┌──────────────────────────────────────┐
                         │  MANIFESTO  —  why we exist           │  RFC-0100 §0
                         └───────────────────────┬──────────────┘
                                                 │ grounds
         ┌───────────────────────────────────────┼───────────────────────────────────────┐
         │                                       │                                       │
         ▼                                       ▼                                       ▼
   ┌──────────────┐                       ┌──────────────┐                       ┌──────────────┐
   │  RFC-0100    │                       │  RFC-0100A   │                       │  RFC-0100B   │
   │  ONTOLOGY    │                       │  DYNAMICS   │                       │  OWNERSHIP   │
   │   WHAT       │                       │   HOW       │                       │   WHO        │
   └──────┬───────┘                       └──────┬───────┘                       └──────┬───────┘
          │                                      │                                      │
          └──────────────────┬──────────────────┴──────────────────┬───────────────────┘
                             │  consumed by the Agent via            │
                             ▼                                       ▼
                   ┌────────────────────┐
                   │  LAWP (Protocol)   │
                   │  how to think      │
                   └─────────┬──────────┘
                             │ Phase C content
                             ▼
                   ┌────────────────────┐                 ┌────────────────────────────┐
                   │ Life Validation    │                 │  Foundation Evolution        │
                   │ Protocol (V1–V6)   │                 │  when Foundation may change   │
                   └─────────┬──────────┘                 └──────────────┬───────────────┘
                             │                                            │
                             └────────────────────┬───────────────────────┘
                                                  ▼
                      ┌────────────────────────────────────────────────────┐
                      │  Life-layer RFCs = FOUNDATION VALIDATION #N         │
                      │  RFC-0101 Identity → 0102 Need → … → 0105 Goal     │
                      │  each: Draft → Review → Challenge → (patch?|Anchor) │
                      └────────────────────────────────────────────────────┘
```

The three "0100" documents are **orthogonal and frozen**: a change to vocabulary (0100) does not
alter the flow (0100A) or the ownership (0100B). LAWP tells the Agent *how to think* about a Life
RFC; Foundation Evolution is the *only* gate that may reopen the base.

---

## Foundation Validation Cycle (Life RFCs)

Each Life RFC is a **Foundation Validation #N**: it consumes the 0100 trilogy and reports
`Foundation Stable?` at its §13 Gate. No Life RFC auto-continues past the Gate.

| RFC | Life # | Topic | Role | Status |
|-----|--------|-------|------|--------|
| RFC-0101 | Life-1 | Identity | Foundation Validation #1 | ✅ Stable — anchored (`ee4bca2`) |
| RFC-0102 | Life-2 | Need | Foundation Validation #2 | ✅ Stable — anchored (`2aff491`) |
| RFC-0103 | Life-3 | Affect | Foundation Validation #3 | ✅ Stable — anchored (`b94440d`) |
| RFC-0104 | Life-4 | Drive | Foundation Validation #4 | ✅ Stable — anchored (`137be39`) |
| RFC-0105 | Life-5 | Goal | Foundation Validation #5 | 🟡 Drafted — in review (first Owned/Hybrid state; 0100C crux) |
| RFC-0106 | Life-6 | Memory | Foundation Validation #6 | ⏳ pending |
| RFC-0107 | Life-7 | Persona | Foundation Validation #7 | ⏳ pending |
| RFC-0108 | Life-8 | Reflection | Foundation Validation #8 | ⏳ pending |
| RFC-0100C | — | State Permissions (Transactions) | **Deferred** | ⏸ opened only if a Life RFC proves a need |

> The first *real* candidate for RFC-0100C is **Memory** (consolidation / sleep rollback) or
> **Goal** (lifecycle-scoped mutation) — both later in the cycle. Identity (0101), Need (0102), Affect
> (0103), and Drive (0104) are derived (`Mutated by: none`) states and **four times** validate the
> deferral.

### Governance convergence — the validation pattern is now a Protocol

RFC-0101 → 0104 each ran the **same** LAWP Phase C and surfaced the same shape of clarification
(upstream bypass / downstream bypass / Authority Boundary / anti-persistence / cycle / Foundation
Stability). Rather than copy-evolve this checklist across RFCs, it is **extracted** into
`PROTOCOL-Life-Validation.md` (V1–V6). From RFC-0105, every Life RFC *references* that Protocol in its
§13.4 instead of re-deriving the checks. This is the **third** locked Protocol — together with LAWP
(how to think) and Foundation Evolution (when base may change), it closes the Life-Architecture
working-method layer.

---

## Where things live

- **Foundation (frozen):** `RFC-0100` / `RFC-0100A` / `RFC-0100B`
- **Protocols (locked):** `PROTOCOL-Architect-Loop` (LAWP), `PROTOCOL-Foundation-Evolution`, `PROTOCOL-Life-Validation` (Phase C checklist, V1–V6)
- **Life RFCs (validation cycle):** `RFC-0101` … `RFC-0108`
- **Infrastructure (separate track):** `RFC-0000` Migration / `RFC-0001` Capability / `RFC-0001A`
  Capability Metadata / `ADR-*` / `PROTOCOL_FIRST.md`
- **Companion mapping:** `RFC-0100-ontology-mapping.md` (existing modules → Ontology)

---

## The default assumption for every Life RFC

> **"I will NOT modify the Foundation."**

- Per **LAWP Phase B**: a Life RFC *uses* the Foundation; it may not modify it.
- A Foundation change is only proposed by a **Foundation Validation RFC** through the
  **Foundation Evolution Protocol** (F1: only a Validation RFC may propose; F2: source must be an
  *observed contradiction*, not "a better idea"; F3: name the *future inconsistency removed*;
  F4: the patch must *shrink* ambiguity, never expand theory).
- The current trend is **Refinement (contraction)**, not Invention: `Owner → Authority`,
  `Semantic Process → Semantic Contract`, `Mutation → Ownership`. Keep the base getting shorter.

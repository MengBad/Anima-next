# Architect Loop — Life Architecture Working Protocol (LAWP)

> **Document Taxonomy**: Protocol (governance). This document defines *how the Agent produces
> Life-layer RFCs*. It contains no Ontology (no life concepts), no RFC (no build proposal), no
> ADR (no one-time locked decision). It is the operating loop every Life RFC from RFC-0101 onward
> must follow.
> **Status**: Locked working protocol — effective immediately. Amend only via the same loop (a
> Protocol change is itself a Foundation-level change, reviewed and anchored before it takes effect).
> **Depends-on**: RFC-0100 (Ontology), RFC-0100A (Dynamics), RFC-0100B (State Ownership). The Gate
> rhythm was first recorded in RFC-0101 §12.1 / §13.
> **Scope**: Every Life-layer RFC (Life-1 … Life-N = RFC-0101 …). Does not govern Infrastructure
> RFCs (0000/0001) or ADRs.

---

## 0. Why this exists

Left as a habit, the Life-architecture workflow drifts: by ~RFC-0105 an RFC starts inventing nouns,
redefining Authorities, or brainstorming instead of validating. To keep the theory *converging*
under pressure rather than *diverging*, the loop already emergent in RFC-0101 is named and frozen
here.

The loop has **five phases, strictly ordered, with a hard stop and a binary decision**:

```
        ┌──────────────────────────────────────────────────────┐
        │                                                        │
        ▼                                                        │
   Phase A  Ground                                              │
        │  (any of 3 questions unanswerable → STOP, no Draft)    │
        ▼                                                        │
   Phase B  Draft  ──── USE Foundation only; never modify ──┐     │
        │                                                  │     │
        ▼                                                  │     │
   Phase C  Challenge  (Validation, NOT Brainstorm)        │     │
        │  four bounded questions only                     │     │
        ▼                                                  │     │
   Phase D  Decision ── exactly two exits ─────────────────┤     │
        │                                                  │     │
   ┌────┴──────────────┐                                  │     │
   │                   │                                  │     │
Foundation         Foundation Patch Required             │     │
   Stable               │                                 │     │
   │                 patch 0100/0100A/0100B               │     │
   │                 (own anchored commit)               │     │
   │                   ▼                                  │     │
   │             re-Ground (Phase A) ────────────────────┘     │
   ▼                                                          │
Phase E  Anchor  (Life RFC commit, zero code)                 │
        │                                                      │
        └──────────────────────────────────────────────────────┘
```

---

## 1. Phase A — Ground

**Intent**: prove the next Life RFC *stands on* the Foundation before writing a word.

**Required actions**
- Read RFC-0100, RFC-0100A, RFC-0100B.
- Produce a **Grounding note** that explicitly answers three questions, each with a section citation:
  1. **Ontology (0100)** — which life-noun(s) / concept(s) will this RFC consume? (e.g. Identity, §1.4)
  2. **Dynamics (0100A)** — which loop / transition / Principle will this RFC consume? (e.g.
     Memory-Recall→Identity, Principle 0)
  3. **Ownership (0100B)** — which Authority / matrix row / invariant will this RFC consume? (e.g.
     Identity Derivation Authority, O1–O4)

**Gate (hard stop)**: if any of the three cannot be answered with a citation, **STOP**. Do not enter
Phase B. The RFC is not ready; the missing Foundation must be resolved first (which is itself a
Foundation Patch, handled by the loop).

**Forbidden**: inventing nouns / flows / authorities inside the Grounding note. Grounding only *reads*.

---

## 2. Phase B — Draft

**Intent**: write the Life RFC *using* the Foundation.

**Allowed**
- Reference, consume, specialize, and constrain Foundation concepts.
- Introduce RFC-local structure (facets, examples, open questions) that does not contradict the trilogy.

**Forbidden (critical constraint)**
- **Modifying the Foundation.** No new life-noun, no new Dynamics loop/transition, no new Authority,
  no redefinition of an existing concept.
- If drafting reveals a *need* to modify the Foundation, that is a **Phase C finding**, not a Phase B
  action. Record it; do **not** edit 0100/0100A/0100B here.

**Exit**: a complete Life RFC draft.

---

## 3. Phase C — Challenge (Validation, not Brainstorm)

**Intent**: stress-test the Foundation *through* this RFC. This is the loop's validation step.

**Ask only these four questions** (no others — this bounds scope and prevents drift):

1. **Holes?** — does the RFC need a concept the Foundation lacks?
2. **Contradictions?** — does the RFC contradict the trilogy, or does the trilogy contradict itself?
3. **Missing words?** — is there a life-noun / Affect dimension / state absent from the Ontology that
   this RFC needs?
4. **Missing Authorities?** — does the RFC introduce or imply a state with no owner in the 0100B matrix?

**Output**: a **Challenge Report** — an explicit list where each item is tagged
`hole` / `contradiction` / `missing-word` / `missing-authority`, or the single line `none`.

**Forbidden**: open-ended ideation, "future work" brainstorming, expanding theory beyond the four
questions.

---

## 4. Phase D — Decision (two exits only)

**Intent**: collapse the Challenge Report to a binary verdict.

- **Exit 1 — Foundation Stable**: Challenge found zero required Foundation changes (`none`, or every
  finding is RFC-local and non-blocking).
- **Exit 2 — Foundation Patch Required**: Challenge found ≥1 required Foundation change.

**No third exit.** "Stable enough", "mostly stable", "stable except X we'll fix later" are not allowed
— if X is required, the verdict is *Patch Required*.

---

## 5. Phase E — Anchor

**If Decision = Foundation Stable**
- Anchor the Life RFC: commit as an independent, zero-code, rollbackable commit.
- Proceed to the next Life RFC (back to Phase A).

**If Decision = Foundation Patch Required**
- **Do NOT anchor the Life RFC.**
- Patch the Foundation (0100 / 0100A / 0100B) as its **own** anchored commit (zero code), citing the
  Challenge findings.
- Return to **Phase A** (re-Ground the Life RFC against the *patched* Foundation) → Phase B (re-Draft)
  → Phase C (re-Challenge) → Phase D.
- Only when the re-run yields **Foundation Stable** does Phase E anchor the Life RFC.

---

## 6. Anti-drift properties

- **Re-grounding**: every Life RFC re-cites the same three docs — the base stays the single source of truth.
- **Draft can't mutate base**: Phase B forbids Foundation edits; mutation is forced into the
  Challenge→Patch path, which is visible and anchored.
- **Bounded Challenge**: exactly four question-types, so validation cannot become scope creep.
- **Binary Decision**: no fuzzy "good enough" lets a hole slip through.
- **Gated Anchor**: a Life RFC is committed only when the Foundation it depends on is Stable.

---

## 7. Relationship to the Document Taxonomy

This Protocol is category **Protocol**. It is deliberately empty of:
- *Ontology* content (that lives in RFC-0100),
- *RFC* content (each Life RFC is its own RFC-category doc),
- *ADR* content (one-time locked decisions live in ADRs).

Life RFCs produced by this loop remain RFC-category documents; this Protocol only governs *how they
are produced*.

---

## 8. First consumer (already embodied by RFC-0101)

RFC-0101 Identity was written before this Protocol was named, but already followed it:

- **Ground** — §0 cites 0100 §1.4 / 0100A Principle 0 / 0100B Identity Derivation Authority.
- **Draft** — §1–§11 use the Foundation, propose no new noun / flow / authority.
- **Challenge** — §13.2 lists findings (`none requiring a Foundation change`).
- **Decision** — §13.3 = **Foundation Stable**.
- **Anchor** — committed `ee4bca2`.

The Constitutional-state candidate (Appendix A) was surfaced in Challenge, judged a hypothesis without
a real consumer, and **parked** rather than forced into a Foundation Patch — the correct Loop behavior.

This Protocol merely makes the loop explicit and mandatory for every subsequent Life RFC
(0102 Need, 0103 Affect, 0104 Goal, 0105 Reflection, …).

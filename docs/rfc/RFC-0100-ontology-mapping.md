# RFC-0100 Appendix: Ontology → Code Mapping

> **Status**: Companion to RFC-0100 (Digital Life Ontology)
> **Role**: Migration navigation map — the single table every Life-layer RFC must
> consult *before* designing, so it upgrades an existing module instead of re-inventing
> one.
> **Scope**: Map only. No interfaces, no code changes.

---

## Why this document exists

RFC-0100 defines the *vocabulary* of a digital life. This appendix maps that vocabulary
onto the *existing code*, so the Life-layer RFCs (RFC-0101 Identity, RFC-0102 Need, …)
extend what is already there rather than fork a second implementation. It is the bridge
between worldview and implementation — and the antidote to the `AnimaCore` god-object
pattern RFC-0100 §2 calls out.

---

## Mapping table

| Existing module | Ontology concept | Disposition | Note for the next owning RFC |
|-----------------|------------------|-------------|------------------------------|
| `soul` | Identity | **Upgrade** | formalize from prompt-mask → persisted self-model; primary input to RFC-0101 |
| `dream` | Reflection | **Keep** | async background reflection pass; wire into the §3 Reflection→Memory path |
| `chat_memory` | Episodic Memory | **Upgrade** | per-round records → feed the Reflection→Memory loop |
| `vector_memory` | Semantic Memory | **Keep** | generalized-fact store; unchanged |
| `tool_learning` | Skill Learning | **Upgrade** | this *is* the Learning loop; promote to a first-class Life-layer system |
| `crossgroup` | Relationship | **Upgrade** | cross-group user facts are Relationship, currently ad hoc; formalize in RFC-0101/0102 |
| `bot_social` | Social Behaviour (early Autonomy) | **Review** | emergent autonomy seed; owning RFC must close Keep/merge |

---

## How to read the Disposition column

- **Keep** — leave as-is; future RFCs reference it by its Ontology name, never re-wrap it.
- **Upgrade** — extend the module *toward* the Ontology definition; do **not** fork a
  parallel implementation. The upgrade is a refactor, not a rewrite.
- **Review** — status unresolved; the owning RFC (RFC-0101/0102) MUST decide Keep or
  merge and record the decision as an ADR.

## Rule

Any new Life-layer RFC MUST check this table first. If it would introduce a module that
already appears here under a different name, that is a **duplicate** and the RFC is
rejected until it reuses the existing asset. This is the same "import, don't invent"
discipline RFC-0100 §5 applies to vocabulary, applied to code.

---

## Non-goals

- Does **not** define interfaces — those live in the respective Life-layer RFC.
- Does **not** change code — this is a map, not a migration. Migrations are tracked by
  M0's `PENDING` debt register and the Life-layer RFCs.

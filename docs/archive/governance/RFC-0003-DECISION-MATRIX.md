# RFC-0003 Decision Matrix

Three governance choices for the approver. Companion to `GOVERNANCE-DECISION.md`.

| Dimension | **Option A — Approve & authorize Implementation** | **Option B — Accept design, defer Implementation** | **Option C — Reject & reopen** |
|---|---|---|---|
| **What happens** | r1 frozen as design baseline; Implementation may begin per Roadmap / M0.4.x discipline (no code yet). | RFC-0003 settled; Implementation held to a later gate. No change to the RFC. | RFC returned for revision; current r1 cancelled. |
| **Benefits** | Unblocks the Roadmap's next RFC (0004); design is review-complete and internally consistent (lifecycle / interface / owner / transition closed after F-01..F-05). | Safest conservative posture; keeps a settled design on the book without committing implementation effort. | Forces re-examination; a genuinely better design *could* emerge. |
| **Risks** | Downstream contract drift (mitigated by §11 `contract_version` pin); possible future Foundation Evolution (bounded path, NG8). | Roadmap momentum paused; 0004 waits. | Document churn with no new information (all findings resolved; no defect); likely reconverges on r1. |
| **Operational cost** | Low: one Approval Record signature; Implementation planning per existing discipline. | None. | Medium: another design + review cycle. |
| **Rollback complexity** | Low: Implementation is gated; if issues arise, the `Requires Foundation Evolution (F1/F2)` path handles contradictions. | N/A (nothing implemented). | N/A (nothing implemented). |
| **Governance impact** | Positive: validates the converged `Recon → Study → Study Review → Draft` cadence (no Decision Input) end-to-end for an Infrastructure RFC. | Neutral / conservative: safe, but defers the same decision. | Negative if avoidable: weakens the "review outcome is sufficient" signal. |

## Bottom line

- **A** is the lowest-cost way to advance the Roadmap with a review-complete, internally consistent design.
- **B** is the safest but pauses Roadmap momentum and only postpones this decision.
- **C** is not justified by the review record (no Reject / no blocking defect; all findings resolved).

See `GOVERNANCE-DECISION.md` §6 for the recommendation.

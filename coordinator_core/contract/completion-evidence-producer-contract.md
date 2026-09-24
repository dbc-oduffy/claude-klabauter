# Completion-evidence producer contract

> **What this is.** The interface `hnd-warm-dispatch-reconcile-e1ed90` builds its dispatch-ack
> record and poll op against. It states what a caller may rely on when it reads the terminal
> stamp of an acknowledged MUTATING op, the wire meaning of each evidence class, and which module
> is the ack's builder. This doc does not build the ack record, its storage, retention, or poll
> op — those are `hnd-warm-dispatch-reconcile-e1ed90`'s to build; that baton is `blocked_by` the
> plan that ratified this contract.
>
> **Ratified by:** `docs/decisions/DR-442-completion-evidence-is-the-engine-s-record-not-the-side-effect.md`,
> extending the 2026-09-09 ruling (DoE-claude `state/rulings/2026-09-09-staff-eng-rulings-run-c0d7111e.md`
> § Row M11, @ 62d63adfe).
>
> **Spec backlinks.**
> - Plan (source of truth): `docs/plans/2026-09-23-completion-evidence-contract.md` § C2/C3
> - Spike verdict: `docs/research/spike-verdicts/2026-09-23-completion-evidence-shape-under-the-brightline.md`
> - Registry module: `coordinator_core/authz/completion_evidence.py`
> - Consuming call site: `coordinator_core/warm/client.py :: _indeterminate_envelope`
> - Ack builder (not yet built): `hnd-warm-dispatch-reconcile-e1ed90`

---

## 1. What the terminal stamp may claim

The dispatch-ack record's terminal stamp (built by `hnd-warm-dispatch-reconcile-e1ed90`, against
this contract) asserts exactly one fact: **the handler returned, with this outcome.** It never
asserts that a write landed anywhere the handler cannot observe before returning, and no code path
under this contract reads a mutation's side effect (an outbox file, `git log`, a sibling repo's
inbox) to decide completion. Doing so is the racing read the 2026-09-09 ruling rejected.

## 2. Evidence classes

| Class | Meaning | Caller may rely on |
|---|---|---|
| `ack` (default, undeclared in the registry) | Every write the handler makes completes synchronously, on-box, before it returns. | The terminal stamp is complete evidence the mutation happened. No reconcile, no re-run. |
| `fire_and_forget` | The effect lands off-box or after the handler returns (a detached process, a remote peer). | The result is telemetry only. It may report an unobserved outcome. It may never be read as an instruction to reconcile, adjudicate, or re-run by hand. |
| `undeclared` (fail-closed, absence from the registry) | No declaration exists for this op. | The stamp asserts nothing beyond the box. The caller must not re-run; there is no further evidence to seek. |

`OP_COMPLETION_EVIDENCE` (`coordinator_core/authz/completion_evidence.py`) is a sparse
`MappingProxyType[str, EvidenceClass]` holding only non-`ack` entries. `evidence_class(op)` returns
the declared class or `UNDECLARED`. It performs a dict lookup only: no filesystem read, no git
spawn, no side-effect probe.

## 3. Transport-failure rule (binds brief halves, C5)

Exit code 3 means compute never ran. The producer emits nothing on stdout; the diagnostic goes to
stderr; the exit code is the only evidence. A synthetic envelope at exit 3 asserts a decision no
one computed and is refused under this contract.

## 4. Wiring

`coordinator_core/warm/client.py :: _indeterminate_envelope` selects its message text by
`evidence_class(msg.get("method"))`, imported lazily inside that function, on the failure path
only. `FIRE_AND_FORGET` renders fire-and-forget text (outcome not observed, not the caller's to
adjudicate, do not re-run by hand). `UNDECLARED`, and any import or lookup failure, render the
fail-closed message, matching `_op_may_mutate`'s fail-closed pattern.

## 5. What this contract does not cover

The dispatch-ack record's own storage shape, retention, request-id keying, and poll op are
`hnd-warm-dispatch-reconcile-e1ed90`'s to design and build against this contract. This doc pins
only what the terminal stamp may claim once that record exists.

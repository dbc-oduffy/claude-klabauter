# Queue admission — the five-question self-check

This is the full text `write_guards/nudge_improvement_queue_write.py` used to
inline on every improvement-queue write before the 2026-07-30 escape-mechanism
rework moved it here — see that module's docstring for why: the friction the
guard adds at decision time is now a short pointer plus one concrete action
(add a `justification:` line and re-run the write), not a 483-word wall. The
five questions themselves are unchanged; only their location moved.

## Two writes are hard-forbidden outright (2026-06-01 PM ruling)

A `justification:` line does NOT make either of these OK — if this write is
either case, do not add the field, go fix/decline/surface instead:

- **Work actionable THIS SESSION** (named fix-locus + bounded scope = an
  action, not a queue line — dispatch it or do it inline).
- **An INBOUND CROSS-REPO MEMO 'ask'.** Filing a picked-up memo-ask to the
  queue launders an inbox into a staging ground and silently makes a
  prioritization call (this ask is not-now) that belongs to the PM, not you.
  A memo-ask's only exits are Accept / Decline-with-architectural-rationale /
  Surface-to-PM (skills/pickup M3; `docs/wiki/cross-repo-communication.md` §
  Picking up a memo).

## Before writing `justification:`, answer these out loud

1. **CAN I FIX IT NOW?** A quick dispatch is almost always faster than
   queueing + triage + a future session re-loading context. If the fix-locus
   is named and the scope is bounded, the queue is the wrong tool — dispatch
   an executor or do it inline.

2. **SHOULD I FLAG IT TO THE PM?** If it's a real tradeoff, a product call,
   or something that changes user-visible behavior — that's a PM
   conversation, not a queue entry. Queueing it routes it to nobody.

3. **AM I BEING LAZY?** Honestly. "Annoying to fix right now" is not an
   architectural reason. If the answer is "I could, but I'd rather not" —
   fix it.

4. **AM I DECIDING SCOPE?** "Out of scope for this session" is sometimes a
   PM call dressed up as an EM call. If you're deferring something the PM
   might want done now, ask — don't queue around them.

5. **IS THIS AN INBOUND CROSS-REPO MEMO ASK?** If you picked up a memo and
   are about to queue its request, stop — that is one of the two
   hard-forbidden writes above. Adjudicate-and-own it: Accept (do the work),
   Decline (architectural rationale), or Surface-to-PM (it's competing for
   priority — that's the PM's call). Queuing it is laundering the inbox, not
   handling the memo.

If after these five questions queueing is still right (legitimate cases:
cross-cutting universal pattern noticed mid-other-work, genuinely needs its
own plan, depends on something not yet built) — write the `justification:`
line and land the entry. Then surface a one-line "Queuing X because Y" to the
PM in-session: the PM ruling is visibility-not-approval, so no sign-off is
needed, but nothing lands silently and the PM can veto.

The field requires a typed reason not because the guard reads it for
content-policing beyond triviality, but because YOU have to write the
sentence. If writing it feels harder than just fixing the thing, that's the
signal.

Spec backlink: `coordinator_core/write_guards/nudge_improvement_queue_write.py`

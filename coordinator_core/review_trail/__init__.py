"""
coordinator_core.review_trail

Purpose: the on-disk home for the reviewed-set STORE (see `reviewed_set.py`)
— the append-only, per-clone, resident-read materialization of "which
commits carry a review stamp" — AND, per DR-374 (C2b,
state/dispatch-briefs/2026-08-29-the-gravestoned-review-trail-surface-is-
deleted/C2b.md), the home for the live+archive review-trail CORPUS reader
(`records.py`, moved from `coordinator_core.ops.list_review_trail_records`).

AMENDS this docstring's own prior statement that the reader "stays in
`ops/`" and that "no later migration ... is implied or owed by this plan"
— true of the plan that wrote it (docs/plans/2026-08-27-the-reviewed-set-
is-a-file-not-a-computation.md § C1), superseded here: `records.py` is
read-side code over the same review-trail corpus in the same domain this
package already owns, and its writer counterpart
(`coordinator_core.ops.review_trail_write`) is gravestoned per DR-374 and
deleted by a follow-up chunk, which is what forced `records.py`'s three
live module-scope importers to a surviving home before that deletion
could land. The op handler and CLI `main(argv)` entry stay behind in
`coordinator_core.ops.list_review_trail_records` until that chunk deletes
them.

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1
Spec backlink: state/dispatch-briefs/2026-08-29-the-gravestoned-review-trail-surface-is-deleted/C2b.md
"""

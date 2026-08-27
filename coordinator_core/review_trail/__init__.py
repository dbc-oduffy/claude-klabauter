"""
coordinator_core.review_trail

Purpose: the on-disk home for the reviewed-set STORE (see `reviewed_set.py`)
— the append-only, per-clone, resident-read materialization of "which
commits carry a review stamp". This is the new home for the store only;
the existing review-trail writer (`coordinator_core.ops.review_trail_write`)
and reader (`coordinator_core.ops.list_review_trail_records`) stay in
`ops/` — this package is not an op, so it does not join them, and no
later migration of the writer/reader into this package is implied or
owed by this plan.

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1
"""

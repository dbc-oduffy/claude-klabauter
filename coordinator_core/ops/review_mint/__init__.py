"""coordinator_core.ops.review_mint — mints a gated review Workflow from a plan.

Consumes DoE-claude's ``review-roster-fragment.json`` (``schema_version: 3``)
through a single shared, stage-aware parser (``roster.parse_stages``) so
``review.mint_workflow`` and ``dispatch.emit`` never fork the reader. See
``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md``.
"""

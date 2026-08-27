"""
coordinator_core.ops.tracker — tracker.* MUTATING plan-chunk-tracker ops sub-package.

Purpose: home for ops that mutate a plan chunk directory's "tracker README" — the
markdown status table `coordinator/skills/enrich-and-review/SKILL.md` (Phase 2.5 /
4.5 / 6) and `coordinator/docs/wiki/delegate-execution.md` (§ Phase 1.5, Re-dispatch
budget) both read/write as their write-ahead-log record for a stub's enrichment/
review/execution lifecycle status.

Ops registered by handler modules (imported by coordinator_core/ops/__init__.py):
    tracker.advance_status — advance_status.py

GOVERNANCE NOTE (read before adding a second op here): `advance_status.py`'s own
module docstring carries the full account of why this sub-package's write target is
NOT yet covered by a ratified DR-level reserved-noun-write carve-out (cf. DR-211
fleet archival, DR-212 handoff frontmatter, DR-213 queue additive-create, DR-216
changelog/completion/review-trail) — the tracker README's location is a caller-
supplied directory path, not a single fixed noun, and its mutation is an in-place
REWRITE of existing table-cell content, not append-only. A second op in this
sub-package inherits the same open governance question; do not treat
`advance_status.py`'s provisional classification as settled precedent without
re-reading its docstring's "Provisional classification" section.

Mirrors: coordinator_core/ops/fleet/ sub-package shape (one shared __init__.py,
per-handler modules, co-located tests/).
"""

"""A lessons grind profile and open-row builder shared by the queue-emit mechanical-close tests."""

from __future__ import annotations

import json

RULE = "Never trust a plan-frozen file count; recompute the membership set live at execution time."


_PROFILE = """\
row_id_key: "@stem"
batch_key: [scope]
priority:
  field: created
  order: asc
verdicts: [discard, fix-it]
graph:
  triage:
    kind: triage
    edges:
      discard: refute_close
      fix-it: fix
  refute_close:
    kind: refute-close
    edges:
      confirmed: commit
      refuted: fix
    on_fail: needs-judgment
  fix:
    kind: fix
    edges:
      done: verify
    on_fail: baton
  verify:
    kind: verify
    verify:
      default: agent
    edges:
      pass: commit
    on_fail: verify-failed
  commit:
    kind: commit
    edges: {}
closure:
  status_field: status
  closed_values:
    fix: applied
    refute-close: discarded
  stamp_fields: [closed_at, closed_by]
archive_path: archive/lessons/
schema: lesson-entry.schema.json
triage_policy: |
  Triage each row.
appetite:
  standard:
    where: []
    limit: null
    concurrency: 2
    extra_verification: false
    batch_size: {default: 4}
    triage_depth: {default: standard}
    window: 1
    max_agent_calls: 40
"""


def _row_text(**fields) -> str:
    base = dict(
        created="2026-08-01",
        title="a lesson",
        body=RULE,
        status="open",
        scope="project",
        from_repo="claude-klabauter",
    )
    base.update(fields)
    return "\n".join(f"{k}: {json.dumps(v)}" for k, v in base.items()) + "\n"


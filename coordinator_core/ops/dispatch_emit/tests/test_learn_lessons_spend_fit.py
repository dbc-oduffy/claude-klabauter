"""Pins the default learn-lessons queue emit's receipt budget: its token cap is the admitted rows'
share of the whole-backlog bar (bar x admitted / open rows) and the projected spend fits it, with
no admitted row declined to make it fit.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.contract import run_budget
from coordinator_core.ops.dispatch_emit.queue_emit import emit_queue_script

from .conftest import REVIEW_KW

_OPEN = 50

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
    base = dict(created="2026-08-01", title="a lesson", status="open", scope="project", from_repo="claude-klabauter")
    base.update(fields)
    return "\n".join(f"{k}: {json.dumps(v)}" for k, v in base.items()) + "\n"


def _batched_row_ids(script: str) -> list:
    line = next(ln for ln in script.splitlines() if ln.startswith("const BATCHES = "))
    batches = json.loads(line[len("const BATCHES = "):-1])
    return [row if isinstance(row, str) else row["row_id"] for b in batches for row in b["rows"]]


def _repo(tmp_path: Path, batch_size: int) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    queue = repo / "state" / "lessons"
    queue.mkdir(parents=True)
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "lessons.yaml").write_text(
        _PROFILE.replace("batch_size: {default: 4}", f"batch_size: {{default: {batch_size}}}"),
        encoding="utf-8",
    )
    for i in range(_OPEN):
        body = f"A distinct rule number {i} that nobody has written down anywhere else."
        day = i % 28 + 1
        (queue / f"row{i:02d}.yaml").write_text(_row_text(created=f"2026-08-{day:02d}", body=body), encoding="utf-8")
    return repo, queue, pdir


def _emit(repo: Path, queue: Path, pdir: Path):
    run_dir = repo / "state" / "queue-grind" / "lessons" / "run-1"
    run_dir.mkdir(parents=True)
    return emit_queue_script(
        "lessons", "standard", None, queue=[queue], profile_dir=pdir, repo_root=repo,
        run_dir=run_dir, session_id="sess1", **REVIEW_KW,
    )


def test_receipt_cap_is_the_admitted_share_of_the_whole_backlog_bar_and_spend_fits_it(tmp_path):
    repo, queue, pdir = _repo(tmp_path, batch_size=8)

    emission = _emit(repo, queue, pdir)

    extras = emission.receipt_extras
    admitted = len(_batched_row_ids(emission.script))
    assert admitted == extras["admitted_count"] and 0 < admitted < _OPEN
    cap = extras["budget"]["cap"]
    assert cap["tokens"] == run_budget.whole_backlog_bar() * admitted // _OPEN
    assert cap["tokens"] != run_budget.load("learn-lessons").tokens
    assert extras["budget"]["projected"]["tokens"] <= cap["tokens"]
    assert all(extras["budget"]["within"].values())
    assert extras["deferred"] == _OPEN - admitted
    assert not [d for d in extras["declined"] if d]


def test_a_share_too_small_for_one_batch_still_admits_the_first_row_and_reports_over(tmp_path):
    repo, queue, pdir = _repo(tmp_path, batch_size=4)

    emission = _emit(repo, queue, pdir)

    extras = emission.receipt_extras
    assert extras["admitted_count"] == 1
    assert extras["budget"]["cap"]["tokens"] == run_budget.whole_backlog_bar() // _OPEN
    assert extras["budget"]["within"]["tokens"] is False

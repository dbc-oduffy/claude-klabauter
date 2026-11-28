"""dispatch.terminal_commit reconciles every open/partial row of the run's own sizing against the
run's judged verdicts: an earlier run's unclaimed open row the judge returns met is stamped met,
wired and `<plan>@<landing sha>`; a row the judge does not return met stays open and keeps the sizing live."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/2026-10-09-x.yaml"
_PLAN = "docs/plans/p.md"
_SID = "11111111-2222-3333-4444-555555555555"
_SOURCES = [{"doc": "brief.md", "ref": "r@abc"}]

_PLAN_TEXT = f"""---
title: p
status: executing
plan_id: pln-x-123456
sizing_object: {_SIZING}
prime_exit_criterion:
  text: a.py exists
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _row(rid, claimed):
    return {"id": rid, "source_text": f"t {rid}", "surface": "ui", "status": "open",
            "claimed_by": list(claimed), "wired": False}


def _sizing_text(rows):
    head = (
        "schema: sizing-object\nintent: x\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: e\n"
    )
    return head + yaml.safe_dump({"requirement_register": {"sources": _SOURCES, "rows": rows}}, sort_keys=False)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    rows = [_row("r1", [_PLAN]), _row("r2", []), _row("r3", [])]
    (root / _SIZING).write_text(_sizing_text(rows), encoding="utf-8")
    (root / _PLAN).write_text(_PLAN_TEXT, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _judged(rid, status="met", wired=True):
    return {"id": rid, "claim": "this-plan", "status": status, "wired": wired, "surface": "ui",
            "observed_ref": "x.py::f"}


def _fire(repo: Path, judged: list) -> dict:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=_PLAN)
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
        "criterion": {"status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md",
                      "register_rows": judged},
    }
    return terminal_commit._handler(
        {"inline_review": review, "script_path": "run.mjs", "incomplete_chunks": [], "session_id": _SID},
        repo_root=repo / ".git",
    )


def _rows(repo: Path) -> dict:
    doc = yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))
    return {r["id"]: r for r in doc["requirement_register"]["rows"]}


def test_an_earlier_runs_unclaimed_open_row_the_judge_meets_is_stamped_at_the_landing_sha(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2"), _judged("r3", status="partial", wired=False)])

    assert out["committed"] is True, out
    rows = _rows(repo)
    assert rows["r2"]["status"] == "met" and rows["r2"]["wired"] is True
    assert f"{_PLAN}@{out['sha']}" in rows["r2"]["met_by"]
    assert rows["r1"]["status"] == "met"


def test_a_row_the_judge_does_not_meet_stays_open_and_the_sizing_stays_live(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2"), _judged("r3", status="partial", wired=False)])

    rows = _rows(repo)
    assert rows["r3"]["status"] == "open" and not rows["r3"].get("met_by")
    assert "sizing_shipped" not in out
    assert (repo / _SIZING).is_file()
    assert "r3" in str(out.get("register_unreconciled") or out.get("closure") or out.get("open_rows"))


def test_a_row_with_no_verdict_is_left_untouched(repo):
    out = _fire(repo, [_judged("r1")])

    assert out["committed"] is True, out
    rows = _rows(repo)
    assert rows["r2"]["status"] == "open" and rows["r3"]["status"] == "open"
    assert not rows["r2"].get("met_by")

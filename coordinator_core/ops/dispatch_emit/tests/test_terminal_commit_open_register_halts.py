"""dispatch.terminal_commit never reports a green delivery while the cited sizing's register still owes rows:
the commit stands, the reply is `ok: False, halted: criterion_not_met` with the owed rows."""

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


def _row(rid):
    return {"id": rid, "source_text": f"t {rid}", "surface": "ui", "status": "open",
            "claimed_by": [_PLAN], "wired": False}


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
    (root / _SIZING).write_text(_sizing_text([_row("r1"), _row("r2")]), encoding="utf-8")
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


def test_an_open_register_row_commits_but_never_reads_green(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2", status="partial", wired=False)])

    assert out["committed"] is True and out["sha"], out
    assert "sizing_shipped" not in out and out["sizing_ship_skipped"]
    assert out["ok"] is False
    assert out["halted"] == "criterion_not_met"
    assert [r["id"] for r in out["open_rows"]] == ["r2"]
    assert "criterion_not_met" in out["error"] and "r2" in out["error"]
    assert "a.py" in _git(["show", "--no-renames", "--name-only", "--format=", out["sha"]], repo).split()


def test_a_fully_met_register_stays_green(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2")])

    assert out["committed"] is True and out["sizing_shipped"]["sizing"] == _SIZING, out
    assert "halted" not in out and "open_rows" not in out and out.get("ok") is not False


def test_a_register_free_sizing_stays_green(repo):
    (repo / _SIZING).write_text(_sizing_text([_row("r1")]).split("requirement_register:")[0], encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "register-free"], repo)

    out = _fire(repo, [])

    assert out["committed"] is True and "halted" not in out and out.get("ok") is not False, out


def test_the_helper_exempts_only_rows_the_ledger_gate_passed_open(repo):
    both = terminal_commit._open_register_halt(repo, _SIZING, [])
    assert both is not None and [r["id"] for r in both["open_rows"]] == ["r1", "r2"]

    one = terminal_commit._open_register_halt(repo, _SIZING, [{"id": "r1", "reason": "withheld_by C2"}])
    assert one is not None and [r["id"] for r in one["open_rows"]] == ["r2"]

    assert terminal_commit._open_register_halt(repo, _SIZING, [{"id": "r1"}, {"id": "r2"}]) is None
    assert terminal_commit._open_register_halt(repo, None, []) is None
    assert terminal_commit._open_register_halt(repo, "state/sizings/absent.yaml", []) is None


def test_the_halt_kind_is_the_one_the_chain_reads():
    from coordinator_core.ops.dispatch_emit.ask_compose import HALT_CRITERION_NOT_MET

    assert terminal_commit._HALT_CRITERION_NOT_MET == HALT_CRITERION_NOT_MET

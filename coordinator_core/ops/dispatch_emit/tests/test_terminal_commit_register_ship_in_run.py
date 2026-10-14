"""dispatch.terminal_commit ships a register-bearing sizing in-run: stamps it shipped when the
judged register is fully met, closes its claimed rows, and archives the sizing and consumed baton
in the run's own stamp commit; an unmet register leaves the sizing live and the commit standing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/2026-10-09-x.yaml"
_ARCHIVED_SIZING = "archive/sizings/2026-10/2026-10-09-x.yaml"
_BATON = "state/handoffs/2026-10-09-b.md"
_ARCHIVED_BATON = "archive/handoffs/2026-10/2026-10-09-b.md"
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


def _sizing_text(rows, baton=True):
    head = (
        "schema: sizing-object\nintent: x\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: e\n"
    )
    if baton:
        head += f"baton: {_BATON}\n"
    return head + yaml.safe_dump({"requirement_register": {"sources": _SOURCES, "rows": rows}}, sort_keys=False)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / _SIZING).write_text(_sizing_text([_row("r1"), _row("r2")]), encoding="utf-8")
    (root / _BATON).write_text("---\ntitle: b\nstatus: claimed\nkind: session-handoff\n---\n\n# b\n", encoding="utf-8")
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


def _files(repo: Path, rev: str) -> set:
    return set(_git(["show", "--no-renames", "--name-only", "--format=", rev], repo).split())


def test_a_met_register_ships_closes_its_rows_and_archives_in_the_run_commits(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2")])

    assert out["committed"] is True and "coded_stamp_error" not in out, out.get("coded_stamp_error")
    assert out["plan_status"] == "implemented", out.get("plan_status_refusal")
    assert out.get("sizing_shipped", {}).get("sizing") == _SIZING, (
        out.get("sizing_ship_skipped"), out.get("register_writeback_error"), out.get("register_failure")
    )
    assert out["archived"] == {_SIZING: _ARCHIVED_SIZING, _BATON: _ARCHIVED_BATON}
    assert not (repo / _SIZING).exists() and not (repo / _BATON).exists()

    archived = yaml.safe_load((repo / _ARCHIVED_SIZING).read_text(encoding="utf-8"))
    assert archived["status"] == "shipped" and archived["resolution"]["sha"] == out["sha"]
    rows = archived["requirement_register"]["rows"]
    assert [(r["status"], r["wired"]) for r in rows] == [("met", True), ("met", True)]
    assert rows[0]["met_by"] == [f"{_PLAN}@{out['sha']}"]

    stamp = _files(repo, "HEAD")
    assert {_SIZING, _ARCHIVED_SIZING, _BATON, _ARCHIVED_BATON, _PLAN} <= stamp
    tracked = set(_git(["ls-files"], repo).split())
    assert {_ARCHIVED_SIZING, _ARCHIVED_BATON} <= tracked and not ({_SIZING, _BATON} & tracked)
    assert _git(["status", "--porcelain", "--", "state", "archive", _PLAN], repo) == ""
    assert "Sizing-Shipped:" in _git(["log", "-1", "--format=%B", "HEAD"], repo)


def test_an_unmet_register_leaves_the_sizing_live_and_the_commit_standing(repo):
    out = _fire(repo, [_judged("r1"), _judged("r2", status="partial", wired=False)])

    assert out["committed"] is True, out
    assert "sizing_shipped" not in out and "archived" not in out
    assert "not met" in out["sizing_ship_skipped"] or "partial" in out["sizing_ship_skipped"]
    assert (repo / _SIZING).is_file() and (repo / _BATON).is_file()
    sizing = yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))
    assert sizing["status"] == "routed"
    rows = sizing["requirement_register"]["rows"]
    assert (rows[0]["status"], rows[1]["status"]) == ("met", "open")
    assert "a.py" in _files(repo, out["sha"])
    assert _git(["status", "--porcelain", "--", _SIZING], repo) == ""


def test_a_plan_that_does_not_flip_ships_the_sizing_unarchived(repo):
    (repo / _PLAN).write_text(
        _PLAN_TEXT.replace("  deferred: false\n```", "  deferred: false\n- id: C4\n  title: t4\n"
                           "  change_kind: code-edit\n  surface: b.py\n  disposition: open\n  deferred: false\n```"),
        encoding="utf-8",
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "second open row"], repo)

    out = _fire(repo, [_judged("r1"), _judged("r2")])

    assert out["committed"] is True, out
    assert out["plan_status"] == "refused"
    assert out["sizing_shipped"]["sizing"] == _SIZING and "archived" not in out
    assert (repo / _SIZING).is_file() and (repo / _BATON).is_file()
    assert yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))["status"] == "shipped"


def test_a_register_free_sizing_is_shipped_and_left_for_the_boot_sweep(repo):
    (repo / _SIZING).write_text(_sizing_text([_row("r1")]).split("requirement_register:")[0], encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "register-free"], repo)

    out = _fire(repo, [])

    assert out["committed"] is True and out["sizing_shipped"]["sizing"] == _SIZING, out
    assert "archived" not in out and (repo / _SIZING).is_file()


def test_the_baton_stays_live_when_it_is_not_terminal(repo):
    (repo / _BATON).write_text(
        "---\ntitle: b\nstatus: open\nkind: session-handoff\n---\n\n# b\n", encoding="utf-8"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "open baton"], repo)

    out = _fire(repo, [_judged("r1"), _judged("r2")])

    assert out["archived"] == {_SIZING: _ARCHIVED_SIZING}
    assert [s["id"] for s in out["archive_skipped"]] == [_BATON]
    assert (repo / _BATON).is_file()

"""The terminal commit writes judged register rows back to the cited sizing in the coded-stamp commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIZING = "state/sizings/2026-10-09-x.yaml"
_PLAN = "docs/plans/p.md"
_HEAD = """schema: sizing-object
intent: "x"
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: read
  evidence: e
"""
_TAIL = "# trailer outside the register\n"
_SOURCES = [{"doc": "brief.md", "ref": "r@abc"}]


def _row(rid, claimed=True, **kw):
    row = {"id": rid, "source_text": f"t {rid}", "surface": "ui", "status": "open",
           "claimed_by": [_PLAN] if claimed else [], "wired": False}
    row.update(kw)
    return row


def _sizing_text(rows):
    block = yaml.safe_dump({"requirement_register": {"sources": _SOURCES, "rows": rows}}, sort_keys=False)
    return _HEAD + block + _TAIL


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / _SIZING).write_text(_sizing_text([_row("r1"), _row("r2"), _row("r3", claimed=False)]), encoding="utf-8")
    (root / _PLAN).write_text(f"---\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _judged(rid, status="met", wired=True):
    return {"id": rid, "claim": "this-plan", "status": status, "wired": wired, "surface": "ui",
            "observed_ref": "x.py::f"}


def _review(rows, status="met"):
    return {"integration_stem": "s", "slices": 1, "fixes": 0,
            "criterion": {"status": status, "observation": "ok", "register_rows": rows}}


def _register(repo):
    return yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))["requirement_register"]


def test_writeback_met_rows_get_status_wired_met_by_progress_and_rollup(repo):
    out = terminal_commit._register_writeback(repo, _PLAN, _review([_judged("r1"), _judged("r2")]), "abc1234")

    assert out == {"register_sizing": _SIZING}
    reg = _register(repo)
    r1, r2, r3 = reg["rows"]
    assert (r1["status"], r1["wired"], r1["met_by"]) == ("met", True, [f"{_PLAN}@abc1234"])
    assert r1["last_progress_at"] and r2["status"] == "met"
    assert r3["status"] == "open" and "met_by" not in r3
    assert reg["rollup"]["coverage"] == {"met": 2, "total": 3}
    assert reg["rollup"]["met"] == 2 and reg["rollup"]["unclaimed"] == 1
    assert reg["sources"] == _SOURCES


def test_writeback_not_met_run_appends_no_met_by_and_advances_progress(repo):
    terminal_commit._register_writeback(
        repo, _PLAN, _review([_judged("r1", "partial", False)], status="not_met"), "abc1234"
    )

    r1 = _register(repo)["rows"][0]
    assert r1["status"] == "open" and "met_by" not in r1
    assert r1["last_progress_at"]


def test_writeback_leaves_bytes_outside_the_register_block_unchanged(repo):
    terminal_commit._register_writeback(repo, _PLAN, _review([_judged("r1")]), "abc1234")

    text = (repo / _SIZING).read_text(encoding="utf-8")
    assert text.startswith(_HEAD) and text.endswith(_TAIL)


def test_schema_invalid_result_is_refused_and_named_and_file_untouched(repo):
    bad = _sizing_text([_row("r1", surface="nope")])
    (repo / _SIZING).write_text(bad, encoding="utf-8")

    out = terminal_commit._register_writeback(repo, _PLAN, _review([_judged("r1")]), "abc1234")

    assert "register write-back refused" in out["register_writeback_error"]
    assert (repo / _SIZING).read_text(encoding="utf-8") == bad


def test_no_judged_rows_or_no_register_is_a_noop(repo):
    before = (repo / _SIZING).read_text(encoding="utf-8")
    assert terminal_commit._register_writeback(repo, _PLAN, _review([]), "abc1234") == {}
    assert (repo / _SIZING).read_text(encoding="utf-8") == before
    (repo / _SIZING).write_text(_HEAD, encoding="utf-8")
    assert terminal_commit._register_writeback(repo, _PLAN, _review([_judged("r1")]), "abc1234") == {}
    assert (repo / _SIZING).read_text(encoding="utf-8") == _HEAD


def test_terminal_commit_lands_writeback_in_coded_stamp_commit_with_product_sha(repo):
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=_PLAN)
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")

    out = terminal_commit._handler(
        {"inline_review": _review([_judged("r1"), _judged("r2")]), "script_path": "run.mjs",
         "incomplete_chunks": []},
        repo_root=repo / ".git",
    )

    assert out["committed"] is True and "register_writeback_error" not in out
    r1 = _register(repo)["rows"][0]
    assert r1["met_by"] == [f"{_PLAN}@{out['sha']}"]
    assert out["coded_sha"]
    stamp_files = _git(["show", "--name-only", "--format=", out["coded_sha"]], repo).split()
    assert stamp_files.count(_SIZING) == 1
    assert _SIZING not in _git(["show", "--name-only", "--format=", out["sha"]], repo)
    assert _git(["status", "--porcelain", "--", _SIZING], repo) == ""

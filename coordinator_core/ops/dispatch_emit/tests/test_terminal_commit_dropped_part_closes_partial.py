"""dispatch.terminal_commit closes a warp ask run as partial when its delivery note records a stated
part as not done with no ruling: the part becomes an open register row, the sizing is not shipped,
and the reply names the part."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    DELIVERY_NOTE_DIR,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}
_SIZING = "state/sizings/2026-10-09-x.yaml"
_PLAN = "docs/plans/p.md"
_RUN = "run1"
_NOTE = f"{DELIVERY_NOTE_DIR}/{_RUN}.md"
_DROPPED_NOTE = "# Delivery\n\n(a) reacher gating was not investigated. (b) remnant completion is done.\n"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _row(rid, text, status="met", **extra):
    return {"id": rid, "source_text": text, "surface": "ui", "status": status, "claimed_by": [], **extra}


def _sizing_text(rows):
    head = (
        "schema: sizing-object\nintent: x\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: e\n"
    )
    sources = [{"doc": "brief.md", "ref": "r@abc"}]
    return head + yaml.safe_dump({"requirement_register": {"sources": sources, "rows": rows}}, sort_keys=False)


def _met_rows():
    return [_row("R1", "ship b", wired=True, met_by=["x@1"]), _row("R2", "ship c", wired=True, met_by=["x@1"])]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for d in ("state/sizings", "docs/plans", DELIVERY_NOTE_DIR):
        (root / d).mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / _SIZING).write_text(_sizing_text(_met_rows()), encoding="utf-8")
    (root / _PLAN).write_text(f"---\ntitle: p\nsizing_object: {_SIZING}\n---\nbody\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _ask_fire(repo: Path, note: str | None) -> dict:
    run_dir = repo / RUN_DIR_ROOT / _RUN
    run_dir.mkdir(parents=True)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=_PLAN)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{_RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{_RUN}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{_RUN}/manifest.json\n", encoding="utf-8")
    if note is not None:
        (repo / _NOTE).write_text(note, encoding="utf-8")
    return terminal_commit._handler(
        {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []},
        repo_root=repo / ".git",
    )


def _register(repo: Path) -> dict:
    return yaml.safe_load((repo / _SIZING).read_text(encoding="utf-8"))


def _committed(repo: Path, rev: str) -> str:
    return _git(["show", "--no-renames", "--name-only", "--format=", rev], repo)


def test_an_unruled_dropped_part_closes_partial_with_an_open_row_and_the_sizing_unshipped(repo):
    out = _ask_fire(repo, _DROPPED_NOTE)

    assert out["committed"] is True, out
    assert out["closed_as"] == "partial"
    assert [(p["label"], p["row_id"] is not None) for p in out["dropped_parts"]] == [("a", True)]
    assert "sizing_shipped" not in out and "sizing_ship_skipped" in out, out
    assert out["close"]["outcome"] == "partial", out["close"]
    assert out["dropped_parts"][0]["row_id"] in out["close"]["open_parts"]
    sizing = _register(repo)
    assert sizing["status"] == "routed"
    minted = [r for r in sizing["requirement_register"]["rows"] if r["id"] == out["dropped_parts"][0]["row_id"]]
    assert len(minted) == 1 and minted[0]["status"] == "open" and minted[0]["anchor"] == "relayed"
    assert minted[0]["source_text"].startswith("(a) ")
    assert _SIZING in _committed(repo, out["sha"]) + _committed(repo, "HEAD")
    assert _git(["status", "--porcelain", "--", _SIZING], repo) == ""


def test_a_note_with_no_dropped_part_ships_the_sizing_as_before(repo):
    out = _ask_fire(repo, "# Delivery\n\n(a) gating is done. (b) remnant completion is done.\n")

    assert out["committed"] is True, out
    assert "closed_as" not in out and "dropped_parts" not in out
    assert out["sizing_shipped"]["sizing"] == _SIZING, out.get("sizing_ship_skipped")
    assert _register(repo)["status"] == "shipped"


def test_a_run_with_no_delivery_note_ships_the_sizing_as_before(repo):
    out = _ask_fire(repo, None)

    assert out["committed"] is True, out
    assert "closed_as" not in out and out["sizing_shipped"]["sizing"] == _SIZING


def test_a_part_ruled_out_by_a_revise_ruling_or_waived_is_not_a_dropped_part(repo):
    ruling = {"source": "pm", "quote": "drop (a)", "ref": "workflow.revise:run1:out_of_scope", "on": "2026-10-25"}
    rows = [
        _row("R1", "(a) reacher gating", status="deferred", ruling=ruling),
        _row("R2", "(b) remnant completion", wired=True, met_by=["x@1"]),
    ]
    (repo / _SIZING).write_text(_sizing_text(rows), encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "ruled"], repo)

    out = _ask_fire(repo, _DROPPED_NOTE)

    assert out["committed"] is True, out
    assert "closed_as" not in out and "dropped_parts" not in out


def test_a_waived_part_is_ruled(repo):
    rows = [
        _row("R1", "(a) reacher gating", status="waived"),
        _row("R2", "(b) remnant completion", wired=True, met_by=["x@1"]),
    ]
    (repo / _SIZING).write_text(_sizing_text(rows), encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "waived"], repo)

    out = _ask_fire(repo, _DROPPED_NOTE)

    assert "closed_as" not in out and "dropped_parts" not in out, out


def test_a_refire_over_the_minted_row_adds_no_duplicate(repo):
    first = _ask_fire(repo, _DROPPED_NOTE)
    rows_after_first = _register(repo)["requirement_register"]["rows"]
    text = first["dropped_parts"][0]["row_id"]
    assert text is not None

    again = terminal_commit._dropped_part_rows(repo, _SIZING, _DROPPED_NOTE)

    assert again["closed_as"] == "partial"
    assert _register(repo)["requirement_register"]["rows"] == rows_after_first
    assert again["dropped_parts"][0]["row_id"] == first["dropped_parts"][0]["row_id"]

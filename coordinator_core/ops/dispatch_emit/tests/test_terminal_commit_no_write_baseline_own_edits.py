"""A run whose write_baseline lacks a path it declared lands that path's edits; an undeclared one still refuses."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    ManifestRow,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000e1"
DECLARED = "pkg/declared.py"
UNDECLARED = "pkg/undeclared.py"
BASED = "pkg/based.py"
RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _build(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "pkg").mkdir()
    for rel in (DECLARED, UNDECLARED, BASED):
        (root / rel).write_text("a = 1\nb = 2\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    rdir = root / "state" / "warp" / RUN
    rdir.mkdir(parents=True)
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}",
        rows=(ManifestRow(
            id="C1", agent_type="coordinator:executor", model="sonnet", brief_path="b.md",
            writes=(DECLARED,), wave=0,
        ),),
        review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
        write_baseline=wb.capture(root, [BASED]),
    )
    (rdir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    run_token.mint(root, RUN, "run.mjs", session_id=SELF)
    return root


def _fire(root: Path, *paths: str) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=tuple(paths)),))
    marker = root / RUN_DIR_ROOT / RUN / "commit-request.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(render_marker(request) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text(
        f"{ASK_MANIFEST_MARKER}state/warp/{RUN}/manifest.json\n", encoding="utf-8"
    )
    params = {
        "inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": [],
        "session_id": SELF, "run_id": RUN,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_declared_path_without_baseline_lands(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    seed = _git(["rev-parse", "HEAD"], root)
    (root / DECLARED).write_text("a = 1\nb = 3\n", encoding="utf-8")

    out = _fire(root, DECLARED)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert _git(["rev-parse", "HEAD"], root) != seed
    assert _git(["show", f"HEAD:{DECLARED}"], root).splitlines()[1] == "b = 3"


def test_undeclared_path_without_baseline_is_excluded_as_residue(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    seed = _git(["rev-parse", "HEAD"], root)
    (root / DECLARED).write_text("a = 1\nb = 3\n", encoding="utf-8")
    (root / UNDECLARED).write_text("a = 1\nb = 9\n", encoding="utf-8")

    out = _fire(root, DECLARED, UNDECLARED)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert _git(["rev-parse", "HEAD"], root) != seed
    assert _git(["show", f"HEAD:{DECLARED}"], root).splitlines()[1] == "b = 3"
    assert _git(["show", f"HEAD:{UNDECLARED}"], root).splitlines()[1] == "b = 2"
    assert (root / UNDECLARED).read_text(encoding="utf-8").splitlines()[1] == "b = 9"
    assert UNDECLARED in out["foreign_excluded"], out
    assert [p for p in out["provenance"] if p["path"] == UNDECLARED][0]["reason"] == "foreign-path-no-baseline"


def test_new_undeclared_path_without_baseline_still_refuses(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    seed = _git(["rev-parse", "HEAD"], root)
    (root / "pkg" / "fresh.py").write_text("a = 1\n", encoding="utf-8")

    out = _fire(root, "pkg/fresh.py")

    assert out.get("committed") is False, out
    assert out["refused"] == "foreign-hunk-unproduced", out
    assert _git(["rev-parse", "HEAD"], root) == seed


def _measure_own_write(root: Path) -> str:
    """The run writes DECLARED, records the write and measures it; returns the measured blob sha."""
    from coordinator_core.ops.dispatch_emit import measured_rev
    from coordinator_core.session import touch_record

    (root / DECLARED).write_text("a = 1\nb = 3\n", encoding="utf-8")
    touch_record.append_event(
        touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF), session_id=SELF, agent_id=None,
        verb="T", path=DECLARED, content_hash=touch_record.compute_content_hash(root / DECLARED), kind="w",
    )
    return measured_rev.measured_rev(RUN, [DECLARED], root)["blobs"][DECLARED]


def test_peer_edit_after_measure_lands_the_measured_blob_and_keeps_the_peer_bytes(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    seed = _git(["rev-parse", "HEAD"], root)
    measured = _measure_own_write(root)
    (root / DECLARED).write_text("a = 1\nb = 3\nc = 4  # peer\n", encoding="utf-8")

    out = _fire(root, DECLARED)

    assert out["committed"] is True, out
    assert _git(["rev-parse", "HEAD"], root) != seed
    assert _git(["rev-parse", f"HEAD:{DECLARED}"], root) == measured
    assert out["measured_blob_landed"] == [DECLARED], out
    assert (root / DECLARED).read_text(encoding="utf-8").splitlines()[2] == "c = 4  # peer"
    assert DECLARED in _git(["status", "--porcelain"], root)


def test_peer_edit_after_measure_with_head_moved_on_the_path_does_not_land_the_measured_blob(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    measured = _measure_own_write(root)
    (root / DECLARED).write_text("a = 1\nb = 7\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "peer lands the path"], root)
    (root / DECLARED).write_text("a = 1\nb = 7\nc = 4  # peer\n", encoding="utf-8")

    out = _fire(root, DECLARED)

    assert "measured_blob_landed" not in out, out
    assert _git(["rev-parse", f"HEAD:{DECLARED}"], root) != measured
    assert "b = 3" not in _git(["show", f"HEAD:{DECLARED}"], root)


def test_peer_edit_after_measure_with_unreadable_record_does_not_land_a_measured_blob(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    _measure_own_write(root)
    from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

    measured_rev_record_path(root, RUN).write_text("{", encoding="utf-8")
    (root / DECLARED).write_text("a = 1\nb = 3\nc = 4  # peer\n", encoding="utf-8")

    out = _fire(root, DECLARED)

    assert "measured_blob_landed" not in out, out


def test_unreadable_provenance_on_declared_path_is_advisory(tmp_path, monkeypatch):
    root = _build(tmp_path, monkeypatch)
    monkeypatch.setattr(hp, "_unproduced", lambda r, run, p: (_ for _ in ()).throw(ValueError("x")))
    advisories: list = []
    found = hp.unproduced_hunks(root, RUN, [DECLARED, UNDECLARED], own_plan=None, advisories=advisories)
    assert [f.path for f in found] == [UNDECLARED]
    assert [a["path"] for a in advisories] == [DECLARED]

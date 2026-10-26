"""dispatch.terminal_commit still lands after an EM committed the run's record dir mid-run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
_REVIEWED = reviewed()
RUN = "ask-20261022T120000-cccccc"
SIZING = "state/sizings/2026-10-22-x.yaml"
PLAN = "docs/plans/2026-10-22-x.md"
SCRIPT = "run.mjs"
RECORD = f"{RECORD_DIR_ROOT}/{RUN}"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _write(root, SIZING, "status: draft\n")
    _write(root, PLAN, f"---\nsizing_object: {SIZING}\n---\nbody\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setattr(terminal_commit, "_ship_cited_sizing", lambda *a, **k: {})
    monkeypatch.setattr(terminal_commit, "_ledger_gate", lambda *a, **k: (None, None, []))
    return root


def _start_run(root: Path) -> str:
    """Mint the run's token, write its manifest and marker, and leave one product edit."""
    marker = f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=PLAN
    )
    _write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(), marker_path=marker,
    )
    _write(root, f"{RECORD}/manifest.json", json.dumps(manifest.to_json()))
    _write(root, f"{RECORD}/run.json", json.dumps({"sizing_rel": SIZING}))
    _write(root, SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD}/manifest.json\n")
    _write(root, "a.py", "a\n")
    return run_token.mint(root / ".git", RUN, SCRIPT)


def _em_checkpoint(root: Path) -> str:
    """A plain scoped commit of the record dir and sizing, no Checkpoint-* trailers."""
    _write(root, SIZING, "status: routed\n")
    _git(["add", "--", RECORD, SIZING], root)
    _git(["commit", "-q", "-m", "wip: em checkpoint of warp run state", "--", RECORD, SIZING], root)
    return _git(["rev-parse", "HEAD"], root).strip()


def _fire(root: Path, token: str) -> dict:
    return terminal_commit._handler(
        {
            "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": _REVIEWED,
            "run_id": RUN, "run_token": token,
        },
        repo_root=root / ".git",
    )


def test_terminal_commit_lands_post_checkpoint_record_files(repo):
    token = _start_run(repo)
    checkpoint = _em_checkpoint(repo)
    _write(repo, f"{RECORD}/late.md", "written after the checkpoint\n")

    out = _fire(repo, token)

    assert out["committed"] is True, out
    assert out.get("ok") is not False, out
    assert _git(["rev-parse", "HEAD"], repo).strip() != checkpoint
    carried = _git(["log", "--name-only", "--format=", f"{checkpoint}..HEAD"], repo).split()
    assert "a.py" in carried
    assert f"{RECORD}/late.md" in carried
    assert not run_token.is_live(repo / ".git", RUN)
    assert not run_token.held_records(repo / ".git")


def test_nothing_after_the_checkpoint_is_nothing_to_commit_and_releases_the_token(repo):
    token = _start_run(repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "wip: em checkpoint of everything"], repo)
    before = _git(["rev-parse", "HEAD"], repo).strip()

    out = _fire(repo, token)

    assert out.get("ok") is not False, out
    assert out["committed"] is False, out
    assert out.get("nothing_to_commit") is True, out
    assert _git(["rev-parse", "HEAD"], repo).strip() == before
    # A non-landing return releases the claim (unspent, redeemable again), never strands it.
    assert run_token.is_live(repo / ".git", RUN)
    assert not run_token.claim_pending(repo / ".git", RUN, token, SCRIPT)


def test_trailerless_em_commit_is_not_a_checkpoint(repo):
    token = _start_run(repo)
    _em_checkpoint(repo)
    message = _git(["log", "-1", "--format=%B"], repo)

    assert checkpoint_stamp.parse_checkpoint(message) is None

    out = _fire(repo, token)

    assert out["committed"] is True, out
    assert "error" not in out, out

"""The session that submitted a run is the run's own hand, not a peer: its touch claim and its sibling runs'
declared writes never withhold the run's paths; a different live session still holds them, and every hold is named."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_hub_path import session_dir_for

from coordinator_core.ops.dispatch_emit import ask_contract, land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import liveness
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FIRING = "5e1f0000-0000-4000-8000-0000000000c1"
SUBMITTER = "7a24cc98-d88c-4c5b-872c-5642b52b9ed7"
OTHER = "5e1f0000-0000-4000-8000-0000000000d3"
RUN = "ask-20261110T021537-083972"
SIBLING = "ask-20261110T021537-8a3aa8"
SCRIPT = "run-083972.mjs"
PLAN = "docs/plans/own-plan.md"
P = "coordinator_core/tui_status/fields.py"
Q = "coordinator_core/tui_status/grok.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _touch(root: Path, sid: str, rel: str) -> None:
    session_core.init(sid, cwd=str(root))
    session_scope.touch(sid, rel, cwd=str(root))
    touch_record.append_event(
        touch_record.sink_path(session_dir_for(root / ".git", sid)),
        session_id=sid, agent_id=None, verb="T", path=rel,
        content_hash=touch_record.compute_content_hash(root / rel), kind="w",
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "README.md", "seed\n")
    _write(root, PLAN, "# plan\n")
    _write(root, P, "x = 0\n")
    _write(root, Q, "y = 0\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", FIRING)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(FIRING, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd: False)
    return root


def _scenario(root: Path, *, touchers: dict, sibling_session: str = "") -> str:
    """A run whose landing caller and token are FIRING and whose run-stage record names SUBMITTER (083972's
    shape); ``touchers`` maps a path to the live session holding a touch claim on it; ``sibling_session``
    mints a sibling run under that session declaring P."""
    base = _git(["rev-parse", "HEAD"], root).strip()
    for rel in (P, Q):
        _write(root, rel, f"{Path(rel).stem} = 1\n")
    for rel, sid in touchers.items():
        _touch(root, sid, rel)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    ask_contract.write_run_stage_at(ask_contract.record_dir(root, RUN), "staged", session_id=SUBMITTER)
    if sibling_session:
        session_core.init(sibling_session, cwd=str(root))
        _write(root, f"state/warp/{SIBLING}/manifest.json", json.dumps({"rows": [{"id": "X1", "writes": [P]}]}))
        run_token.mint(root, SIBLING, "sibling.mjs", session_id=sibling_session)
    run_token.mint(root, RUN, SCRIPT, session_id=FIRING)
    request = CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=(P,)), ChunkCommit(id="C2", title="t", paths=(Q,))),
        plan_path=PLAN,
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return base


def _land(root: Path, base: str) -> dict:
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": FIRING, "run_id": RUN, "run_base_sha": base},
        repo_root=root / ".git",
    )


def _tree(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def test_083972_replay_submitter_touch_claim_does_not_withhold_the_run_paths(repo):
    base = _scenario(repo, touchers={P: SUBMITTER, Q: SUBMITTER})

    out = _land(repo, base)

    assert out["committed"] is True, out
    assert {P, Q} <= set(_tree(repo))
    assert not out.get("peer_held_left"), out
    assert not out.get("stranded"), out


def test_sibling_run_of_the_submitting_session_declaring_a_path_does_not_withhold_it(repo):
    base = _scenario(repo, touchers={}, sibling_session=SUBMITTER)

    out = _land(repo, base)

    assert out["committed"] is True, out
    assert {P, Q} <= set(_tree(repo))
    assert not out.get("peer_held_left"), out


def test_a_different_live_sessions_touch_claim_still_withholds_the_path(repo):
    base = _scenario(repo, touchers={P: OTHER})

    out = _land(repo, base)

    assert P not in _tree(repo)
    assert Q in _tree(repo)
    assert out["stranded"].get("C1") == [P]
    assert out["peer_held_left"] == [{"path": P, "sessions": [OTHER]}]


def test_a_sibling_run_of_a_different_session_declaring_a_path_still_withholds_it(repo):
    base = _scenario(repo, touchers={}, sibling_session=OTHER)

    out = _land(repo, base)

    assert P not in _tree(repo)
    assert out["stranded"].get("C1") == [P]
    assert {"path": P, "sessions": [OTHER]} in out["peer_held_left"]

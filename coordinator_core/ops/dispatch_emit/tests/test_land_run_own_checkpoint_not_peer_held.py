"""A path the run's own checkpoint landed is not held by a senior same-session run's bare declaration;
a live write claim of another session and an unchecked declaration still hold, and every hold is named."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import claim_index, liveness
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
OTHER = "5e1f0000-0000-4000-8000-0000000000b2"
RUN_A = "plan-20261021T100000-aaaaaa"
RUN_B = "run-b"
SCRIPT = "run-a.mjs"
PLAN = "docs/plans/own-plan.md"
P = "src/p.py"
Q = "src/q.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _record(root: Path, rel: str) -> None:
    sink = touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path=rel,
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
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root


def _scenario(root: Path, *, checkpointed: tuple, declared_by_senior: tuple) -> tuple:
    """Run base, then a same-plan checkpoint landing ``checkpointed``, a peer commit, a re-dirty of
    P and Q, and a senior same-session run declaring ``declared_by_senior``."""
    base = _git(["rev-parse", "HEAD"], root).strip()
    for rel in checkpointed:
        _write(root, rel, f"{Path(rel).stem} = 1\n")
    if checkpointed:
        _git(["add", "."], root)
        _git(
            ["commit", "-q", "-m", "checkpoint(wave 0): 1 row — C1", "-m", f"Checkpoint-Plan: {PLAN}"], root
        )
    _write(root, "peer.txt", "peer\n")
    _git(["add", "peer.txt"], root)
    _git(["commit", "-q", "-m", "peer commit"], root)
    for rel in (P, Q):
        _write(root, rel, f"{Path(rel).stem} = 2\n")
        session_scope.touch(SELF, rel, cwd=str(root))
        _record(root, rel)
    _write(root, "state/warp/run-b/manifest.json", json.dumps({"rows": [{"id": "C9", "writes": list(declared_by_senior)}]}))
    run_token.mint(root, RUN_B, "b.mjs", session_id=SELF)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN_A}/run.json", json.dumps({"run_id": RUN_A}))
    request = CommitRequest(
        chunks=(ChunkCommit(id="C4", title="t", paths=(P,)), ChunkCommit(id="C5", title="t", paths=(Q,))),
        plan_path=PLAN,
    )
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    return base, request


def _land(root: Path, base: str) -> dict:
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": SELF, "run_id": RUN_A},
        repo_root=root / ".git",
    )


def _tree(root: Path) -> list:
    return sorted(_git(["show", "--name-only", "--format=", "HEAD"], root).split())


def test_own_checkpointed_path_also_declared_by_an_unlanded_sibling_is_refused_co_declared(repo):
    base, _ = _scenario(repo, checkpointed=(P,), declared_by_senior=(P, Q))
    head = _git(["rev-parse", "HEAD"], repo)

    out = _land(repo, base)

    assert out["committed"] is False, out
    assert out["refused"] == "peer-hunks-co-declared"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_incident_replay_own_checkpointed_path_lands_and_unchecked_declaration_still_holds(repo):
    base, _ = _scenario(repo, checkpointed=(P,), declared_by_senior=(Q,))

    out = _land(repo, base)

    assert out["committed"] is True, out
    assert P in _tree(repo)
    assert Q not in _tree(repo)
    assert out["stranded"] == {"C5": [Q]}
    assert out["peer_held_left"] == [{"path": Q, "sessions": [SELF]}]
    message = _git(["log", "-1", "--format=%B"], repo)
    assert f"Row-Unlanded: C5 {Q} peer-held" in message
    assert "Row-Unlanded: C4" not in message


def test_without_a_checkpoint_the_same_declaration_holds_the_path(repo):
    base, _ = _scenario(repo, checkpointed=(), declared_by_senior=(P,))

    out = _land(repo, base)

    assert P not in _tree(repo)
    assert out["stranded"].get("C4") == [P]
    assert {"path": P, "sessions": [SELF]} in out["peer_held_left"]


def test_another_live_sessions_write_claim_still_holds_an_own_checkpointed_path(repo, monkeypatch):
    base, _ = _scenario(repo, checkpointed=(P,), declared_by_senior=())
    real = claim_index.lookup

    def fake(paths, sessions_dir=None, cwd=None):
        found = real(paths, sessions_dir=sessions_dir, cwd=cwd)
        found[P] = [*found.get(P, []), OTHER]
        found.recorded_kind = {**(found.recorded_kind or {}), P: {OTHER: "w"}}
        return found

    monkeypatch.setattr(claim_index, "lookup", fake)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd: False)

    out = _land(repo, base)

    assert P not in _tree(repo)
    assert out["stranded"].get("C4") == [P]
    assert {"path": P, "sessions": [OTHER]} in out["peer_held_left"]

"""A run's own subagent sessions (an exited Agy child included) and claimants with no verified pid never hold
the run's land; a foreign session with a live pid still does."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import psutil
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
CHILD = "d4c86285-5b99-43bf-bad8-4ad2d5331620"
GRANDCHILD = "d4c86285-5b99-43bf-bad8-4ad2d5331621"
OTHER = "5e1f0000-0000-4000-8000-0000000000d3"
RUN = "ask-20261128T113947-62ee52"
SCRIPT = "run-62ee52.mjs"
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


def _stamp_pid(root: Path, sid: str, *, live: bool) -> None:
    """Rewrite ``sid``'s meta so Layer 1 sees this process (``live``) or no stable pid at all."""
    path = Path(session_core.session_dir(sid, str(root))) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for key in ("stable_pid", "stable_pid_lstart", "stable_pid_start_epoch"):
        meta.pop(key, None)
    if live:
        epoch = str(int(psutil.Process(os.getpid()).create_time()))
        meta.update(stable_pid=str(os.getpid()), stable_pid_lstart=epoch, stable_pid_start_epoch=epoch)
    path.write_text(json.dumps(meta), encoding="utf-8")


def _register_child(home: Path, child: str, parent: str) -> None:
    """An Agy host-session row naming ``parent``; its recorded pid is dead, and it was never unregistered."""
    folder = home / "host-sessions" / "agy"
    folder.mkdir(parents=True, exist_ok=True)
    row = {"schema_version": 1, "session_id": child, "provider": "agy", "pid": 999999, "start_epoch": 1,
           "cwd": str(home), "parent_session_id": parent}
    (folder / f"{child}.json").write_text(json.dumps(row), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
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
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd: False)
    return root


def _scenario(root: Path, *, touchers: dict) -> str:
    """A run fired and token-claimed by FIRING; ``touchers`` maps ``(path, session)`` pairs to a ``live`` pid flag."""
    base = _git(["rev-parse", "HEAD"], root).strip()
    for rel in (P, Q):
        _write(root, rel, f"{Path(rel).stem} = 1\n")
    for (rel, sid), live in touchers.items():
        _touch(root, sid, rel)
        _stamp_pid(root, sid, live=live)
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    ask_contract.write_run_stage_at(ask_contract.record_dir(root, RUN), "staged", session_id=FIRING)
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


def _peer_held(root: Path) -> dict:
    return land_run._peer_held(root, [P], FIRING, RUN, None, self_sessions={FIRING})


def test_own_exited_agy_child_without_a_pid_does_not_withhold_the_run_path(repo, tmp_path):
    _register_child(tmp_path / "home", CHILD, FIRING)
    base = _scenario(repo, touchers={(P, CHILD): False})
    assert liveness.session_live(CHILD, str(repo)), "the child must read live on Layer-2 recency alone"

    assert _peer_held(repo) == {}
    out = _land(repo, base)

    assert out["committed"] is True, out
    assert {P, Q} <= set(_tree(repo))
    assert not out.get("peer_held_left"), out


def test_own_child_with_a_live_pid_is_still_the_runs_own_hand(repo, tmp_path):
    _register_child(tmp_path / "home", CHILD, FIRING)
    _scenario(repo, touchers={(P, CHILD): True})

    assert _peer_held(repo) == {}


def test_a_grandchild_of_the_firing_session_is_the_runs_own_hand(repo, tmp_path):
    _register_child(tmp_path / "home", CHILD, FIRING)
    _register_child(tmp_path / "home", GRANDCHILD, CHILD)
    _scenario(repo, touchers={(P, GRANDCHILD): True})

    assert _peer_held(repo) == {}


def test_a_child_of_a_different_session_with_a_live_pid_still_holds(repo, tmp_path):
    _register_child(tmp_path / "home", CHILD, OTHER)
    _scenario(repo, touchers={(P, CHILD): True})

    assert _peer_held(repo) == {P: [CHILD]}


def test_foreign_claimant_live_on_recency_alone_does_not_hold(repo):
    base = _scenario(repo, touchers={(P, OTHER): False})
    assert liveness.session_live(OTHER, str(repo)), "the claimant must read live on Layer-2 recency alone"

    assert _peer_held(repo) == {}
    out = _land(repo, base)

    assert out["committed"] is True, out
    assert P in _tree(repo)
    assert not out.get("peer_held_left"), out


def test_foreign_claimant_with_a_live_pid_still_withholds_the_path(repo):
    base = _scenario(repo, touchers={(P, OTHER): True})

    out = _land(repo, base)

    assert P not in _tree(repo)
    assert Q in _tree(repo)
    assert out["stranded"].get("C1") == [P]
    assert out["peer_held_left"] == [{"path": P, "sessions": [OTHER]}]


@pytest.mark.parametrize("which", ["fanout", "terminal"])
def test_sibling_peer_held_skips_the_run_s_own_subagent(which, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import land_run_fanout, terminal_commit_refusals
    from coordinator_core.session import claim_index

    parents = {CHILD: FIRING, GRANDCHILD: CHILD}
    monkeypatch.setattr(land_run, "_spawn_parent", lambda sid: parents.get(sid, ""))
    monkeypatch.setattr(land_run, "_without_landed_claimants", lambda root, held: held)
    monkeypatch.setattr(warp_run_deliverable, "live_run_write_claims", lambda *a, **k: [])
    peer = lambda *sids: SimpleNamespace(verdict=claim_index.OWNERSHIP_PEER, peers=list(sids))
    by_path = {P: peer(CHILD, GRANDCHILD), Q: peer(GRANDCHILD, OTHER)}
    monkeypatch.setattr(claim_index, "classify_paths", lambda *a, **k: SimpleNamespace(by_path=by_path))
    if which == "fanout":
        held = land_run_fanout._peer_held(tmp_path, FIRING, [P, Q], RUN)
    else:
        held = terminal_commit_refusals._peer_held(tmp_path, FIRING, [P, Q], run_id=RUN)
    assert held == {Q}

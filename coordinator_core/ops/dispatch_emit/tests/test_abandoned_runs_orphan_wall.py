"""`dispatch.reap_abandoned_runs` orphan wall and CAS-retried commit: a run started more than
`ORPHAN_WALL_SECONDS` ago with no live session, token or stage lands even when a record file was
touched recently; a lost ref CAS is re-called on the new HEAD, and an overlap or exhaustion is a
named refusal."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import ref_cas_retry
from coordinator_core.ops.dispatch_emit import abandoned_runs as ar
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.warm import entry_seam, live_runs
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

WALL_AGE = ar.ORPHAN_WALL_SECONDS + 600
INSIDE_WALL_AGE = ar.TOKENLESS_QUIET_SECONDS + 600
RUN = "warp-foo-20261001T000000-aaaaaa"
SIZING = "state/sizings/foo.yaml"
BATON = "state/handoffs/foo-baton.md"
PLAN = "docs/plans/foo.md"
LOST = "compare-and-swap failed -- refs/heads/main moved since abc was captured"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str = "x\n", *, age: float = 0.0) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age:
        os.utime(path, (time.time() - age,) * 2)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".coordinator-local/\n", encoding="utf-8")
    _write(root, "seed.txt", "seed\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _run(root: Path, *, started: float, touched_ago: float = 60.0, sizing: str = SIZING) -> None:
    """A run whose ask.md is `started` seconds old and whose newest file is `touched_ago` old, with
    a sizing, baton and plan naming each other."""
    _write(root, SIZING, f"baton: {BATON}\nplan: {PLAN}\n", age=started)
    _write(root, BATON, "---\ncarried_items: []\n---\nbaton\n", age=started)
    _write(root, PLAN, "plan\n", age=started)
    _write(root, f"state/warp/{RUN}/ask.md", "ask\n", age=started)
    _write(root, f"state/warp/{RUN}/run.json", json.dumps({"sizing_rel": sizing}), age=started)
    _write(root, f"state/warp/{RUN}/notes.md", "late\n", age=touched_ago)


def _reap(root: Path, **params) -> dict:
    return ar._handler(params, repo_root=root / ".git")


def _committed(root: Path, rev: str = "HEAD") -> set[str]:
    return set(_git(root, "show", "--name-only", "--format=", rev).split())


def test_past_wall_orphan_with_recent_mtime_lands_with_its_named_artifacts(repo):
    _run(repo, started=WALL_AGE)

    out = _reap(repo)

    assert out["ok"] is True, out.get("error")
    assert out["reaped"] == [RUN] and out["committed_sha"]
    assert _committed(repo) == {SIZING, BATON, PLAN, f"state/run-evidence/{RUN}/outcome.json"}
    assert set(out["artifacts"][RUN]) == {SIZING, BATON, PLAN}


def test_run_inside_the_wall_with_recent_mtime_waits(repo):
    _run(repo, started=INSIDE_WALL_AGE)

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None
    assert {"run_id": RUN, "reason": "live-or-recent"} in out["skipped"]


def test_one_copied_file_with_an_old_mtime_puts_a_tokenless_run_past_the_wall(repo):
    """The run's start is its oldest changed file, so a preserved old mtime on one copied file counts."""
    _write(repo, f"state/warp/{RUN}/run.json", json.dumps({}), age=60)
    _write(repo, f"state/warp/{RUN}/notes.md", "late\n", age=60)
    _write(repo, f"state/warp/{RUN}/ask.md", "copied\n", age=WALL_AGE)

    out = _reap(repo)

    assert out["reaped"] == [RUN] and out["committed_sha"]


def test_a_tokenless_run_whose_files_are_all_recent_waits(repo):
    for name in ("run.json", "notes.md", "ask.md"):
        _write(repo, f"state/warp/{RUN}/{name}", "{}\n", age=60)

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None


def test_past_wall_run_with_a_fresh_token_is_skipped(repo):
    _run(repo, started=WALL_AGE)
    run_token.mint(repo, RUN, "run.mjs", session_id="s")

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None


def _stale_token(repo: Path, session_id: str) -> None:
    run_token.mint(repo, RUN, "run.mjs", session_id=session_id)
    path = run_token._record_path(repo, RUN, claimed=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["minted_at"] = time.time() - INSIDE_WALL_AGE
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (time.time() - INSIDE_WALL_AGE,) * 2)


@pytest.mark.parametrize("owner_live, reaped", [(False, True), (True, False)])
def test_past_wall_run_with_an_idle_token_is_held_only_by_a_live_session(repo, monkeypatch, owner_live, reaped):
    _run(repo, started=WALL_AGE)
    _stale_token(repo, "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(ar, "_owner_live", lambda _sid: owner_live)

    out = _reap(repo)

    assert (out["reaped"] == [RUN]) is reaped
    assert (out["committed_sha"] is not None) is reaped


def test_past_wall_run_whose_stage_owner_is_live_is_skipped(repo, monkeypatch):
    _run(repo, started=WALL_AGE)
    _write(repo, f"state/warp/{RUN}/run-stage.json", json.dumps({"stage": "executing", "session_id": "s-1"}), age=60)
    monkeypatch.setattr(live_runs, "_owner_live", lambda _sid, memo=None: True)

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None
    assert {"run_id": RUN, "reason": "live-workflow"} in out["skipped"]


def test_past_wall_pure_rule_ignores_a_recent_newest_mtime():
    changed = {f"state/warp/{RUN}/ask.md"}
    kept = ar.abandoned_run_ids(changed, set(), "state/warp", {RUN: 1000.0}, 1060.0)
    landed = ar.abandoned_run_ids(changed, set(), "state/warp", {RUN: 1000.0}, 1060.0, past_wall={RUN})
    held = ar.abandoned_run_ids(changed, {RUN}, "state/warp", {RUN: 1000.0}, 1060.0, past_wall={RUN})
    assert (kept, landed, held) == ([], [RUN], [])


# --- lost ref CAS -------------------------------------------------------------------------


def _install_dispatch(monkeypatch, *, before_first: object | None, always_lost: bool = False) -> list[int]:
    """Wrap `reentrant_dispatch`: the first (or every) commit_v2 call returns a lost-CAS reply after
    `before_first()` moves HEAD, as a peer commit winning the race would."""
    real = entry_seam.reentrant_dispatch
    calls: list[int] = []

    def fake(name, params, **kw):
        calls.append(1)
        if name == "ceremony.commit_v2" and (always_lost or len(calls) == 1):
            if callable(before_first):
                before_first()
            return {"error": LOST}
        return real(name, params, **kw)

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", fake)
    return calls


def test_lost_ref_cas_on_a_disjoint_peer_commit_lands_on_the_new_head(repo, monkeypatch):
    _run(repo, started=WALL_AGE)

    def peer() -> None:
        _write(repo, "peer.txt", "peer\n")
        _git(repo, "add", "peer.txt")
        _git(repo, "commit", "-q", "-m", "peer")

    calls = _install_dispatch(monkeypatch, before_first=peer)

    out = _reap(repo)

    assert out["ok"] is True, out.get("error")
    assert len(calls) == 2
    assert out["committed_sha"] == _git(repo, "rev-parse", "HEAD").strip()
    assert _git(repo, "log", "-2", "--format=%s").splitlines()[1] == "peer"
    assert {SIZING, BATON, PLAN} <= _committed(repo)


def test_lost_ref_cas_on_a_peer_commit_of_a_named_path_is_a_named_refusal(repo, monkeypatch):
    _run(repo, started=WALL_AGE)

    def peer() -> None:
        _write(repo, PLAN, "peer wrote it\n")
        _git(repo, "add", PLAN)
        _git(repo, "commit", "-q", "-m", "peer")

    _install_dispatch(monkeypatch, before_first=peer)

    out = _reap(repo)

    assert out["ok"] is False and out["refused"] == ref_cas_retry.REFUSED_OVERLAP
    assert LOST in out["error"]
    assert out["committed_sha"] is None


def test_lost_ref_cas_every_attempt_is_a_named_exhaustion(repo, monkeypatch):
    _run(repo, started=WALL_AGE)
    calls = _install_dispatch(monkeypatch, before_first=None, always_lost=True)

    out = _reap(repo)

    assert out["ok"] is False and out["refused"] == ref_cas_retry.REFUSED_EXHAUSTED
    assert len(calls) == ref_cas_retry.REF_CAS_ATTEMPTS
    assert out["committed_sha"] is None

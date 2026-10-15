"""abandoned_runs: the pure predicate and message, and the reaper op over a tmp repo."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import abandoned_runs as ar
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags

ROOT = "state/warp"
NOW = 1_000_000_000.0
OLD = NOW - run_token.TTL_SECONDS - 10


def test_op_name_and_kind():
    assert ar.OP_NAME == "dispatch.reap_abandoned_runs"
    assert ar.HALTED_KIND == "abandoned"


def test_stale_unlive_run_is_abandoned_sorted():
    changed = {f"{ROOT}/b/manifest.json", f"{ROOT}/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": OLD, "b": OLD}, NOW) == ["a", "b"]


def test_live_run_excluded():
    changed = {f"{ROOT}/a/x.json", f"{ROOT}/b/x.json"}
    assert ar.abandoned_run_ids(changed, {"a"}, ROOT, {"a": OLD, "b": OLD}, NOW) == ["b"]


def test_nested_path_ignored():
    assert ar.abandoned_run_ids({f"{ROOT}/a/sub/x.json"}, set(), ROOT, {"a": OLD}, NOW) == []


def test_path_outside_record_root_ignored():
    changed = {"state/sizings/a/x.json", f"{ROOT}x/a/x.json", "other/state/warp/a/x.json"}
    assert ar.abandoned_run_ids(changed, set(), ROOT, {"a": OLD}, NOW) == []


def test_young_run_not_abandoned():
    young = NOW - run_token.TTL_SECONDS + 10
    assert ar.abandoned_run_ids({f"{ROOT}/a/x.json"}, set(), ROOT, {"a": young}, NOW) == []


def test_missing_mtime_not_abandoned():
    assert ar.abandoned_run_ids({f"{ROOT}/a/x.json"}, set(), ROOT, {}, NOW) == []


def test_empty_input():
    assert ar.abandoned_run_ids(set(), set(), ROOT, {}, NOW) == []


def test_message_carries_halted_by_abandoned():
    msg = ar.reap_message(["a", "b"])
    assert "Halted-By: abandoned" in msg
    assert "a, b" in msg.splitlines()[0]


# --- the op over a tmp repo -------------------------------------------------------------

LIMIT_WITH_CANDIDATES = 2

STALE = "ask-20261001T000000-aaaaaa"
STALE2 = "ask-20261001T000001-bbbbbb"
LIVE = "ask-20261001T000002-cccccc"
CLAIMED = "ask-20261001T000003-dddddd"
YOUNG = "ask-20261001T000004-eeeeee"
SCRIPT = "run.mjs"
SIZING = "state/sizings/orphan.yaml"
HELD_SIZING = "state/sizings/held.yaml"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _record(root: Path, run_id: str, *, age: float, sizing: str | None = None) -> None:
    rec = root / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing} if sizing else {}), encoding="utf-8")
    stamp = time.time() - age
    for f in rec.iterdir():
        os.utime(f, (stamp, stamp))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "state" / "sizings").mkdir(parents=True)
    for rel in (SIZING, HELD_SIZING):
        (root / rel).write_text("a: 1\n", encoding="utf-8")
    old = run_token.TTL_SECONDS + 3600
    _record(root, STALE, age=old, sizing=SIZING)
    _record(root, STALE2, age=old)
    _record(root, LIVE, age=old, sizing=HELD_SIZING)
    _record(root, CLAIMED, age=old)
    _record(root, YOUNG, age=60)
    run_token.mint(root, LIVE, SCRIPT, session_id="s", sizing_rel=HELD_SIZING)
    token = run_token.mint(root, CLAIMED, SCRIPT)
    assert run_token.redeem(root, CLAIMED, token, SCRIPT)
    return root


def _porcelain(root: Path) -> set[str]:
    return {l[3:] for l in _git(["status", "--porcelain", "-uall"], root).splitlines()}


def _reap(root: Path, **params) -> dict:
    return ar._handler(params, repo_root=root / ".git")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reaper_lands_abandoned_runs_and_orphaned_sizing_in_one_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo)

    assert out["ok"] is True
    assert out["reaped"] == [STALE, STALE2]
    assert out["sizings"] == [SIZING]
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == head
    names = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert names == {
        f"state/warp/{r}/{f}" for r in (STALE, STALE2) for f in ("ask.md", "run.json")
    } | {SIZING}
    assert "Halted-By: abandoned" in _git(["log", "-1", "--format=%B"], repo)
    assert out["committed_sha"] == _git(["rev-parse", "HEAD"], repo).strip()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_live_claimed_and_young_runs_stay_dirty(repo):
    _reap(repo)

    dirty = _porcelain(repo)
    for run_id in (LIVE, CLAIMED, YOUNG):
        assert f"state/warp/{run_id}/ask.md" in dirty
    assert HELD_SIZING in dirty
    assert not any(STALE in p or STALE2 in p for p in dirty)
    assert SIZING not in dirty


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_second_call_is_a_noop(repo):
    _reap(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo)

    assert out["reaped"] == [] and out["committed_sha"] is None
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dry_run_commits_nothing(repo):
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _reap(repo, dry_run=True)

    assert out["reaped"] == [STALE, STALE2] and out["committed_sha"] is None
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_spawn_count_with_and_without_candidates(repo, monkeypatch):
    spawns = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **k):
            spawns.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)

    _reap(repo)
    with_candidates = len(spawns)
    spawns.clear()
    _reap(repo)

    assert len(spawns) == 1
    assert with_candidates <= LIMIT_WITH_CANDIDATES, with_candidates

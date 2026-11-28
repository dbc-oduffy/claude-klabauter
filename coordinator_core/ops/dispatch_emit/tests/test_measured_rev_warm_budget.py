"""`dispatch.measured_rev` resolves peer owners once per call and stays under the 500ms process-time bar.

`hunk_provenance._peer_split` fed `co_declared_peer_holds` one path at a time, so each changed path rebuilt
the unlanded-run list over `state/warp` and re-walked `.git/index`: 3.1-3.6 s of process time on the
production tree, which the warm engine's io-stall restart killed as -32004.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_index
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, sibling_claims

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CHANGED = 12
PEER_RUNS = 400
BAR_S = 0.5


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def repo(tmp_path: Path) -> tuple:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    paths = [f"pkg/f{i}.py" for i in range(CHANGED)]
    (root / "pkg").mkdir()
    for p in paths:
        (root / p).write_bytes(b"x = 1\ny = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    for p in paths:
        (root / p).write_bytes(b"x = 2\ny = 1\n")
    for i in range(PEER_RUNS):
        (root / "state" / "warp" / f"peer-run-{i:04d}" / "prompts").mkdir(parents=True)
    return root, paths


def _call(root: Path, paths: list) -> tuple:
    started = time.process_time()
    out = mr._handler({"run_id": "probe-run", "paths": paths}, root)
    return out, time.process_time() - started


def test_many_changed_paths_scan_peers_once_and_stay_under_the_bar(
    repo: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, paths = repo
    unlanded, parses, holds = [], [], []
    real_unlanded = sibling_claims._unlanded_run_ids
    real_parse = git_index.parse_index_identity
    real_holds = peer_hunk_gate.co_declared_peer_holds
    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *a, **k: unlanded.append(1) or real_unlanded(*a, **k))
    monkeypatch.setattr(git_index, "parse_index_identity", lambda *a, **k: parses.append(1) or real_parse(*a, **k))
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: holds.append(len(list(a[2]))) or real_holds(*a, **k),
    )

    out, spent = _call(root, paths)

    assert "refused" not in (out or {}), out
    assert len(out["paths"]) == CHANGED
    assert holds == [CHANGED], f"co_declared_peer_holds must run once over every changed path, got {holds}"
    # One scan each for the co-declared holds and the halted withholds; never one per changed path.
    assert len(unlanded) <= 2, f"_unlanded_run_ids ran {len(unlanded)} times for {CHANGED} paths"
    assert len(parses) <= 1, f"index identity parsed {len(parses)} times"
    assert spent < BAR_S, f"measured_rev took {spent * 1000:.0f} ms process time (bar {BAR_S * 1000:.0f} ms)"


def test_batched_owner_still_names_the_holder_per_path(repo: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    root, paths = repo
    held = {paths[0]: "peer-a", paths[1]: "peer-b"}
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds",
        lambda _root, _run, wanted, _own, **k: [(p, held[p]) for p in wanted if p in held],
    )
    token = hp._OWNER_BATCH.set({"paths": frozenset(paths), "holds": None, "halted": None})
    try:
        assert hp._owners(root, "probe-run", paths[0], None) == ["peer-a"]
        assert hp._owners(root, "probe-run", paths[1], None) == ["peer-b"]
        assert hp._owners(root, "probe-run", paths[2], None) == []
    finally:
        hp._OWNER_BATCH.reset(token)


def test_owner_lookup_outside_a_batch_stays_per_path(repo: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    root, paths = repo
    asked = []
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda _r, _run, wanted, _o, **k: asked.append(list(wanted)) or [],
    )
    assert hp._owners(root, "probe-run", paths[0], None) == []
    assert asked == [[paths[0]]]

"""`own_hunk_candidate.candidate` and the halt commit's use of it: HEAD plus only the run's recorded hunks.

Replays case 5 (b1abb0 `_RECEIPT_PATH_JS` kept as own), case 7 (6ad434 `seed.unmanaged_extent` hunk in a file
1a6a49 also writes is excluded and named) and case 8 (61e845 own hunk at lines 189-190, also recorded by a peer,
is never `unknown`) and case 9 (b9c1a5-1 halt never takes live run c35e97-2's driver.py hunks)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import own_hunk_candidate as ohc, stranded_sibling
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

X = "pkg/shared.py"
OWN_RUN = "ask-20261114T100000-aaaaaa"
PEER_RUN = "ask-20261114T100001-bbbbbb"
LINES = 200


def _git(args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                          **no_console_creationflags()).stdout.strip()


def _body(**edits: str) -> bytes:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return ("\n".join(rows) + "\n").encode("utf-8")


def _blob(root: Path, data: bytes) -> str:
    return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=data, capture_output=True,
                          check=True, **no_console_creationflags()).stdout.decode().strip()


def _record(root: Path, run_id: str, path: str, sha: str) -> None:
    rdir = root / RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {path: sha}}}}), encoding="utf-8")


def _touched(root: Path, run_id: str, path: str, run_blob: str) -> None:
    stranded_sibling.write(root / RECORD_DIR_ROOT / run_id, {path: {"base": None, "run": run_blob}})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "pkg").mkdir()
    (root / X).write_bytes(_body())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _head_bytes(root: Path) -> bytes:
    return subprocess.run(["git", "show", f"HEAD:{X}"], cwd=str(root), capture_output=True, check=True,
                          **no_console_creationflags()).stdout


def _candidate(root: Path, run_id: str, tree: bytes, **kw) -> dict:
    return ohc.candidate(root, run_id, X, _head_bytes(root), tree, **kw)


def test_case5_a_recorded_hunk_is_kept_and_an_unrecorded_one_is_excluded_as_unknown(repo):
    receipt = _body(l50="x_50 = '_RECEIPT_PATH_JS'")
    _record(repo, OWN_RUN, X, _blob(repo, receipt))
    tree = _body(l50="x_50 = '_RECEIPT_PATH_JS'", l110="x_110 = 'STRAY'")

    out = _candidate(repo, OWN_RUN, tree)

    assert out["excluded"] == [{"lines": "110", "owner": ohc.UNKNOWN_OWNER}]
    assert out["bytes"] == receipt
    assert _candidate(repo, OWN_RUN, receipt) == {"bytes": receipt, "excluded": []}


def test_case7_a_sibling_runs_stranded_hunk_is_excluded_and_names_its_owner(repo):
    own = _body(l115="x_115 = 'OWN 1a6a49'")
    stranded = _body(l5="x_5 = 'seed.unmanaged_extent()'")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    _touched(repo, PEER_RUN, X, _blob(repo, stranded))
    tree = _body(l5="x_5 = 'seed.unmanaged_extent()'", l115="x_115 = 'OWN 1a6a49'")

    out = _candidate(repo, OWN_RUN, tree)

    assert out["excluded"] == [{"lines": "5", "owner": PEER_RUN}]
    assert out["bytes"] == own
    assert b"seed.unmanaged_extent" not in out["bytes"]


def test_case8_an_own_hunk_a_peer_also_recorded_is_never_unknown(repo):
    shared = _body(l189="x_189 = 'OWN'", l190="x_190 = 'OWN'")
    _touched(repo, OWN_RUN, X, _blob(repo, shared))
    _record(repo, PEER_RUN, X, _blob(repo, shared))

    out = _candidate(repo, OWN_RUN, shared, declared=False)

    assert out == {"bytes": shared, "excluded": []}


def test_a_path_with_no_evidence_of_the_run_is_refused_naming_lines_and_owner(repo):
    _record(repo, PEER_RUN, X, _blob(repo, _body(l5="x_5 = 'PEER'")))

    out = _candidate(repo, OWN_RUN, _body(l5="x_5 = 'PEER'"), declared=False)

    assert out["refused"] == ohc.NO_EVIDENCE
    assert out["lines"] == "5" and out["owner"] == PEER_RUN


def test_a_declared_path_owns_every_hunk_no_peer_recorded(repo):
    _record(repo, PEER_RUN, X, _blob(repo, _body(l5="x_5 = 'PEER'")))

    out = _candidate(repo, OWN_RUN, _body(l5="x_5 = 'PEER'", l115="x_115 = 'MINE'"), declared=True)

    assert out["excluded"] == [{"lines": "5", "owner": PEER_RUN}]
    assert out["bytes"] == _body(l115="x_115 = 'MINE'")


def test_halt_commit_clears_an_own_hunk_a_peer_also_recorded_case8(repo):
    shared = _body(l189="x_189 = 'OWN'")
    _record(repo, OWN_RUN, X, _blob(repo, shared))
    _record(repo, PEER_RUN, X, _blob(repo, shared))
    (repo / X).write_bytes(shared)
    finding = type("Finding", (), {"path": X})()

    kept, reply, supplied = hrc._own_recorded_cleared(repo, OWN_RUN, [finding])

    assert kept == [] and reply == {} and supplied == {}


def test_case9_a_halt_candidate_never_sweeps_in_a_live_sibling_runs_uncommitted_hunks(repo):
    """b9c1a5-1 halted at 7ed1ac1063: its diff swept in the uncommitted driver.py hunks of run c35e97-2,
    still in flight (example-game-repo tree, same own-hunk candidate rule)."""
    live_sibling = "ask-20261114T100002-c35e97"
    own = _body(l20="x_20 = 'OWN b9c1a5'")
    sibling = _body(l150="x_150 = 'LIVE c35e97'")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    _touched(repo, live_sibling, X, _blob(repo, sibling))
    tree = _body(l20="x_20 = 'OWN b9c1a5'", l150="x_150 = 'LIVE c35e97'")

    out = _candidate(repo, OWN_RUN, tree)
    (repo / X).write_bytes(tree)
    finding = type("Finding", (), {"path": X})()
    cleared = hrc._own_recorded_cleared(repo, OWN_RUN, [finding])

    assert out["excluded"] == [{"lines": "150", "owner": live_sibling}]
    assert out["bytes"] == own
    assert cleared[1] == {"foreign_hunks": {X: [{"lines": "150", "owner": live_sibling}]}}


def test_halt_commit_lands_only_the_own_hunk_and_names_the_peer_hunk_case7(repo):
    own = _body(l115="x_115 = 'OWN'")
    peer = _body(l5="x_5 = 'seed.unmanaged_extent()'")
    _record(repo, OWN_RUN, X, _blob(repo, own))
    _record(repo, PEER_RUN, X, _blob(repo, peer))
    (repo / X).write_bytes(_body(l5="x_5 = 'seed.unmanaged_extent()'", l115="x_115 = 'OWN'"))
    finding = type("Finding", (), {"path": X})()

    kept, reply, supplied = hrc._own_recorded_cleared(repo, OWN_RUN, [finding])

    assert kept == []
    assert reply == {"foreign_hunks": {X: [{"lines": "5", "owner": PEER_RUN}]}}
    stored = subprocess.run(["git", "cat-file", "blob", supplied[X]], cwd=str(repo), capture_output=True,
                            check=True, **no_console_creationflags()).stdout
    assert stored == own

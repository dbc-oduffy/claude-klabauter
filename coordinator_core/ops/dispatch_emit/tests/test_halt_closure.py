"""halt_closure.partial_chunks, and land_run's halted mode landing the record only, never product, against real throwaway git repos."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.halt_closure import partial_chunks
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/tracked.py", "def f():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_partial_chunk_names_missing_paths_and_whole_chunks_pass():
    owner = {"a1": "A", "a2": "A", "b1": "B", "run/x": "C"}
    got = partial_chunks(owner, ["a1", "a2", "b1"], ["a1", "b1"])
    assert got == {"A": ["a2"]}
    assert partial_chunks(owner, ["a1", "a2", "b1"], ["a1", "a2", "b1"]) == {}
    assert partial_chunks(owner, ["unowned"], []) == {}
    assert partial_chunks(owner, ["a1", "a2"], []) == {"A": ["a1", "a2"]}


def _halt_with_a_peer_held(repo, monkeypatch, chunk_paths: dict, held: str):
    from coordinator_core.ops.ceremony import commit_v2
    from coordinator_core.ops.dispatch_emit import land_run_halt as lrh

    for paths in chunk_paths.values():
        for p in paths:
            _write(repo, p, "x = 1\n")
    chunks = [type("C", (), {"id": cid, "paths": paths, "report": None})() for cid, paths in chunk_paths.items()]
    monkeypatch.setattr(lrh, "done_chunks", lambda *_a: chunks)
    monkeypatch.setattr(lrh, "_own_prefix_files", lambda *_a: [])

    def claims(root, paths, holds, own_sid=None):
        holds.append((held, "peer"))
        return []

    monkeypatch.setattr(commit_v2, "_peer_claim_warnings", claims)
    (repo / "state/warp/r1").mkdir(parents=True)
    return lrh._land(
        repo, repo, {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}},
        "r1", "state/warp/r1", {"halted": "ledger-check"},
    )


def test_halt_commit_lands_the_record_only_and_names_a_peer_held_write(repo, monkeypatch):
    reply = _halt_with_a_peer_held(
        repo, monkeypatch, {"A": ["pkg/a1.py", "pkg/a2.py"], "B": ["pkg/b1.py"]}, held="pkg/a2.py"
    )
    assert reply["committed"] is True, reply
    landed = set(_git(repo, "show", "--name-only", "--format=", reply["sha"]).split())
    assert landed and all(p.startswith("state/warp/r1/") for p in landed), landed
    assert "pkg/a2.py" not in landed
    assert reply["withheld"]["pkg/a2.py"] == "peer-held"
    assert reply["withheld"]["pkg/a1.py"] == "halt-record-only"
    assert reply["withheld"]["pkg/b1.py"] == "halt-record-only"
    assert "landed_chunks" not in reply
    json.dumps(reply)


def test_halt_commit_carries_no_product_even_beside_a_peer_held_definer(repo, monkeypatch):
    reply = _halt_with_a_peer_held(
        repo, monkeypatch, {"C1": ["pkg/li.py"], "C4": ["pkg/caller.py"], "C9": ["pkg/other.py"]}, held="pkg/li.py"
    )
    assert reply["committed"] is True, reply
    landed = set(_git(repo, "show", "--name-only", "--format=", reply["sha"]).split())
    assert "pkg/li.py" not in landed
    assert reply["withheld"]["pkg/li.py"] == "peer-held"
    assert reply["withheld"]["pkg/other.py"] == "halt-record-only"
    assert not any(p.startswith("pkg/") for p in landed), landed

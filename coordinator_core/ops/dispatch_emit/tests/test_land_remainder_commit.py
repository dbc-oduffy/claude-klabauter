"""``dispatch.land_verified_remainder`` op body: not-halted refusal, token check, write-set refusal,
verify refusal, and a halted fixture run landing with Run-Id and Lands-Chunks trailers."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import land_remainder as lane
from coordinator_core.ops.dispatch_emit import land_remainder_contract as contract
from coordinator_core.ops.dispatch_emit import land_remainder_writeset as writeset
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

RUN = "warp-test-run"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _row(rid, writes):
    return SimpleNamespace(id=rid, writes=list(writes), surface="", writes_under=[])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    run_dir = root / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (run_dir / "halted.json").write_text(json.dumps({"halted": "commit"}), encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (root / "state" / "sizings" / "s.yaml").write_text("plan: docs/plans/p.md\n", encoding="utf-8")
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "p.md").write_text("plan\n", encoding="utf-8")
    rows = [_row("C1", ["a/one.py"]), _row("C2", ["b/two.py", "b/three.py"])]
    monkeypatch.setattr(writeset, "read_spine", lambda plan: rows)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.spine_read.read_spine", lambda plan: rows)

    def fake_dirty(specs, r, **_kw):
        return sorted(s for s in specs if (r / s).is_file())

    monkeypatch.setattr(writeset, "_dirty_in_write_set", fake_dirty)
    return root


def _write(root, *rels):
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("VALUE = 1\n", encoding="utf-8")


def _head_message(root) -> str:
    return _git(["log", "-1", "--format=%B"], root)


def test_a_run_that_is_not_halted_is_refused(repo) -> None:
    (repo / "state" / "warp" / RUN / "halted.json").unlink()
    reply = lane.land(repo, {"run_id": RUN})
    assert reply["refused"] == contract.REFUSED_NOT_HALTED
    assert reply["landed"] is False


def test_a_run_token_that_does_not_match_is_refused_and_not_spent(repo) -> None:
    reply = lane.land(repo, {"run_id": RUN, "run_token": "nope", "script_path": "run.mjs"})
    assert reply["refused"] == "run-token"
    assert not (repo / "state" / "warp" / RUN / "run-token.json").exists()


def test_out_of_write_set_path_is_refused_naming_it_and_nothing_commits(repo) -> None:
    _write(repo, "a/one.py", "z/stray.py")
    before = _git(["rev-parse", "HEAD"], repo)
    reply = lane.land(repo, {"run_id": RUN, "paths": ["a/one.py", "z/stray.py"]})
    assert reply["refused"] == contract.REFUSED_OUT_OF_WRITE_SET
    assert "z/stray.py" in reply["error"]
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_verify_refusal_blocks_the_commit_and_is_relayed(repo, monkeypatch) -> None:
    _write(repo, "a/one.py")
    monkeypatch.setattr(
        lane.verify, "verify_remainder",
        lambda root, run_id, paths: {"refused": contract.REFUSED_IMPORT_CLOSURE_OPEN, "closure_open": ["a/one.py: x"]},
    )
    before = _git(["rev-parse", "HEAD"], repo)
    reply = lane.land(repo, {"run_id": RUN})
    assert reply["refused"] == contract.REFUSED_IMPORT_CLOSURE_OPEN
    assert reply["closure_open"] == ["a/one.py: x"]
    assert _git(["rev-parse", "HEAD"], repo) == before


def test_halted_fixture_run_lands_with_run_id_and_lands_chunks_trailers(repo) -> None:
    _write(repo, "a/one.py", "b/two.py", "b/three.py", "foreign.py")
    start = time.process_time()
    reply = lane.land(repo, {"run_id": RUN})
    inline = time.process_time() - start
    assert reply["landed"] is True and reply["refused"] is None
    assert set(contract.REPLY_KEYS) <= set(reply)
    assert reply["sha"] == _git(["rev-parse", "HEAD"], repo).strip()
    message = _head_message(repo)
    assert f"Run-Id: {RUN}" in message
    assert "Lands-Chunks: C1, C2" in message
    committed = set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert committed == {"a/one.py", "b/two.py", "b/three.py"}
    assert inline < 0.5


def test_declared_dirty_paths_left_out_are_named_and_their_chunk_is_not_credited(repo) -> None:
    _write(repo, "a/one.py", "b/two.py", "b/three.py")
    (repo / "coordinator.local.md").write_text("---\ngenerated_outputs: b/three.py\n---\n", encoding="utf-8")
    reply = lane.land(repo, {"run_id": RUN})
    assert reply["landed"] is True
    assert [o["path"] for o in reply["omitted"]] == ["b/three.py"]
    message = _head_message(repo)
    assert "Lands-Chunks: C1" in message and "C2" not in message.split("Lands-Chunks:")[1]


def test_requested_subset_commits_only_it_and_names_the_rest(repo) -> None:
    _write(repo, "a/one.py", "b/two.py")
    reply = lane.land(repo, {"run_id": RUN, "paths": ["a/one.py"]})
    assert reply["landed"] is True
    assert {"path": "b/two.py", "reason": "not-requested"} in reply["omitted"]
    assert set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split()) == {"a/one.py"}

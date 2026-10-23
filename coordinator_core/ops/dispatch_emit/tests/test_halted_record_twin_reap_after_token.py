"""A halted-record commit reaps the run's own staged twin only after its run token verifies; the TTL sweep is unconditional."""

from __future__ import annotations

import os
import time
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_record_commit, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

RUN = "ask-20261023T000000-aaaaaa"
OTHER = "ask-20261023T000000-bbbbbb"
SCRIPT = "scratch/warp/x.workflow.mjs"
SHA = "a" * 64


def _twin(root: Path, run: str) -> Path:
    path = root / "scratch" / "warp" / f"{run}.staged-{SHA}.workflow.mjs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// twin\n", encoding="utf-8")
    return path


def _age(path: Path) -> None:
    old = time.time() - run_token.TTL_SECONDS - 60
    os.utime(path, (old, old))


def _commit(root: Path, token: str) -> dict:
    return halted_record_commit.commit_halted_record(
        root,
        root / ".git",
        {"run_id": RUN, HALTED_RECORD_PARAM: {"halted": "refusal"}, "run_token": token, "script_path": SCRIPT},
    )


def _root(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        halted_record_commit, "_land", lambda *a, **k: {"committed": False, "nothing_to_commit": True}
    )
    return tmp_path


def test_unverified_token_keeps_own_twin_but_sweeps_aged(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    run_token.mint(root, RUN, SCRIPT)
    own = _twin(root, RUN)
    aged = _twin(root, OTHER)
    _age(aged)

    out = _commit(root, "wrong-token")

    assert out.get("refused") == "run-token", out
    assert own.exists()
    assert not aged.exists()


def test_verified_token_removes_own_twin_and_sweeps_aged(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    token = run_token.mint(root, RUN, SCRIPT)
    own = _twin(root, RUN)
    fresh_foreign = _twin(root, "ask-20261023T000000-cccccc")
    aged = _twin(root, OTHER)
    _age(aged)

    out = _commit(root, token)

    assert out.get("refused") is None, out
    assert not own.exists()
    assert not aged.exists()
    assert fresh_foreign.exists()


def test_tokenless_commit_keeps_own_twin_but_sweeps_aged(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    own = _twin(root, RUN)
    aged = _twin(root, OTHER)
    _age(aged)

    halted_record_commit.commit_halted_record(
        root, root / ".git", {"run_id": RUN, HALTED_RECORD_PARAM: {"halted": "refusal"}}
    )

    assert own.exists()
    assert not aged.exists()

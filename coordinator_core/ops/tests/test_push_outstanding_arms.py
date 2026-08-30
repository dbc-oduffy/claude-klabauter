"""Tier T tests for C2: `push_outstanding`'s two arms become separately
rankable in telemetry.

Live evidence (`.git/coordinator-sessions/logs/op-latency*.jsonl`, 842 `ok`
rows under the wire op `push.outstanding`: 479 under 50ms, 363 over 500ms)
showed two populations under one op name -- a census over that name
describes neither. The wire op name is NOT renamed (four DoE-owned surfaces
reach it by memo); instead each arm records its OWN telemetry identity as a
sibling row, via `_record_arm_latency`.

This file asserts the two arms emit DISTINGUISHABLE identities, and --
because the property that makes the no-op arm cheap is the one a telemetry
change could most easily cost -- that the no-op arm still spawns zero git
processes even with telemetry wired in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as push_outstanding_mod
from coordinator_core.ops.ceremony.push import PushOutcome
from coordinator_core.ops.push_outstanding import push_outstanding

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=no_window,
    )


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _make_repo_with_remote(tmp_path: Path, *, branch: str = "work/some-branch") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    bare = tmp_path / "bare.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)

    repo = _init_repo(tmp_path, "repo")
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", branch], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", branch], repo)
    return repo


def _patch_recorder(monkeypatch):
    calls = []

    def _fake_record_op_latency(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_latency",
        _fake_record_op_latency,
    )
    return calls


def test_noop_arm_and_network_arm_emit_distinguishable_identities(monkeypatch, tmp_path):
    """The no-op arm and the network arm must record DIFFERENT `op`
    identities -- the whole point of this chunk. Same wire op
    (`push.outstanding`), different telemetry rows."""
    calls = _patch_recorder(monkeypatch)

    noop_repo = _make_repo_with_remote(tmp_path / "noop", branch="work/noop")
    push_outstanding(noop_repo)

    network_repo = _make_repo_with_remote(tmp_path / "network", branch="work/network")
    _seed_file(network_repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], network_repo)
    _git(["commit", "-q", "-m", "second"], network_repo)
    monkeypatch.setattr(
        push_outstanding_mod,
        "push_with_retry",
        lambda root, **kwargs: PushOutcome(exit_code=0, acted=["push"]),
    )
    push_outstanding(network_repo)

    assert len(calls) == 2
    noop_op = calls[0]["op"]
    network_op = calls[1]["op"]
    assert noop_op != network_op
    assert "push.outstanding" in noop_op
    assert "push.outstanding" in network_op


def test_noop_arm_still_spawns_zero_git_processes_with_telemetry_wired(monkeypatch, tmp_path):
    """The property that makes the no-op arm cheap -- zero git spawns -- must
    survive the telemetry addition. Proven the same way the module docstring
    proves it elsewhere: fail loudly on any `git` invocation."""
    calls = _patch_recorder(monkeypatch)
    repo = _make_repo_with_remote(tmp_path, branch="work/zero-spawn")

    def _fail_if_spawned(args, cwd=None, **kwargs):
        raise AssertionError(f"unexpected git spawn during no-op arm: {args!r}")

    monkeypatch.setattr(subprocess, "run", _fail_if_spawned)

    outcome = push_outstanding(repo)

    assert outcome.skipped == ["push:nothing-outstanding"]
    assert len(calls) == 1
    assert calls[0]["op"] == push_outstanding_mod._ARM_NOOP


def test_network_arm_records_the_network_identity(monkeypatch, tmp_path):
    """An outstanding commit delegating to `push_with_retry` records the
    network-arm identity, not the no-op one."""
    calls = _patch_recorder(monkeypatch)
    repo = _make_repo_with_remote(tmp_path)
    _seed_file(repo, "second.txt", "more")
    _git(["add", "--", "second.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)

    monkeypatch.setattr(
        push_outstanding_mod,
        "push_with_retry",
        lambda root, **kwargs: PushOutcome(exit_code=0, acted=["push"]),
    )

    outcome = push_outstanding(repo)

    assert outcome.acted == ["push"]
    assert len(calls) == 1
    assert calls[0]["op"] == push_outstanding_mod._ARM_NETWORK


def test_arm_recording_never_breaks_dispatch_on_failure(monkeypatch, tmp_path):
    """A telemetry sink failure must not surface to the caller -- the
    outstanding-work decision is what matters, never the census row."""
    repo = _make_repo_with_remote(tmp_path, branch="work/telemetry-fails")

    def _raise(**kwargs):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_latency", _raise
    )

    outcome = push_outstanding(repo)

    assert outcome.skipped == ["push:nothing-outstanding"]

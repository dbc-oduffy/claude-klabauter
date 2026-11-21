"""Batch job: one call per model, flagged symbols only, sidecar-only commit, leg outcome amended."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.symbol_knowledge import agent_batches, sidecar

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ADJ = "scip-python python pkg 0.1 `pkg.m`/adj()."
NEW = "scip-python python pkg 0.1 `pkg.m`/new()."
SHA = "a" * 40


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.mark.spawns_process
def test_commit_names_only_the_sidecar_and_peer_dirt_stays_unstaged(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "peer.txt").write_text("v1", encoding="utf-8")
    _git(tmp_path, "add", "peer.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "peer.txt").write_text("dirty", encoding="utf-8")
    sidecar.write_sidecar(tmp_path, sidecar.merge_entries({}, {ADJ: ("d", SHA)}))
    agent_batches._commit_sidecar(tmp_path, SHA, 1)
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [sidecar.SIDECAR_REL]
    assert _git(tmp_path, "status", "--porcelain").split() == ["M", "peer.txt"]


SRC = 'import os\n\n\ndef alpha(x):\n    return x\n\n\ndef beta(y):\n    """Documented."""\n    return y\n'

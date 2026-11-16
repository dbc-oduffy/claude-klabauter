"""The stall dump's repo-drive probe is a number and names the root it probed, even when
the op envelope carries no `_origin_worktree`/`_caller_cwd`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.warm import server

pytestmark = pytest.mark.complete


def _header(record: Path) -> dict:
    head = record.read_text(encoding="utf-8").partition("--- threads ---")[0]
    return dict(line.split(": ", 1) for line in head.strip().split("\n"))


def _dump(tmp_path, monkeypatch, msg):
    stall_dir = tmp_path / "stall-dumps"
    monkeypatch.setattr(server, "_WORKER_STDERR_FILE", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", stall_dir)
    claim = tmp_path / "k1"
    claim.write_text("1")
    root, source = server._stall_probe_root(msg)
    disarm = server._arm_stall_dump(0.1, claim, root, source)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (stall_dir.is_dir() and any(stall_dir.glob("*.txt"))):
        time.sleep(0.05)
    disarm()
    (record,) = stall_dir.glob("*.txt")
    return _header(record)


def test_envelope_without_worktree_or_cwd_still_probes_a_number_at_the_engine_root(tmp_path, monkeypatch):
    header = _dump(tmp_path, monkeypatch, {"method": "review.prep", "params": {}})
    assert float(header["repo-drive-probe-ms"]) >= 0
    assert header["repo-drive-probe-root"].startswith("engine-root ")


def test_op_worktree_is_labelled_and_probed(tmp_path, monkeypatch):
    repo = tmp_path / "wt"
    repo.mkdir()
    header = _dump(tmp_path, monkeypatch, {"_origin_worktree": str(repo)})
    assert float(header["repo-drive-probe-ms"]) >= 0
    assert header["repo-drive-probe-root"] == f"op-worktree {repo}"


def test_raw_envelope_repo_field_is_the_second_choice(tmp_path, monkeypatch):
    repo = tmp_path / "envrepo"
    repo.mkdir()
    header = _dump(tmp_path, monkeypatch, {"params": {"repo_root": str(repo)}})
    assert float(header["repo-drive-probe-ms"]) >= 0
    assert header["repo-drive-probe-root"] == f"envelope-repo-field {repo}"


def test_stall_probe_root_never_raises_and_prefers_the_worktree(tmp_path):
    assert server._stall_probe_root(None) == (None, "engine-root")
    assert server._stall_probe_root({"params": "x"}) == (None, "engine-root")
    root, source = server._stall_probe_root({"_caller_cwd": str(tmp_path), "repo_root": "/other"})
    assert (root, source) == (tmp_path, "op-worktree")

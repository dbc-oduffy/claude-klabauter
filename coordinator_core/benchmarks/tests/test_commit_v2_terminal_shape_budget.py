"""`ceremony.commit_v2` on the terminal-commit shape: process time under the commit-route bar, the
stopped-run scan independent of the run count, and a stalled signing wait ending inside the
dispatcher's deadline.

The terminal-commit shape is product paths plus run-artifact paths landed by one call, in a session
that owns stopped runs. The cost lives in the stopped-run residue scan that runs before every
landing, so the fixture builds that corpus; a fixture without it passes at about 200ms regardless.
Process time only: wall clock measures peer load.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon, warp_live
from coordinator_core.benchmarks import commit_v2_terminal_shape_full_path as driver
from coordinator_core.benchmarks.commit_v2_terminal_shape_full_path import (
    LAUNCH_DIR_FILES,
    RUNS,
    SELF,
)
from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.git import sshsig
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

COMMIT_ROUTE_BAR_MS = 400.0
K_INVOCATIONS = 3
SAMPLES = 3
_DRIVER = "coordinator_core.benchmarks.commit_v2_terminal_shape_full_path"
_git = driver._git
_stopped_run = driver._stopped_run
_terminal_params = driver.terminal_params


@pytest.fixture
def launch_dirs(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    for i in range(LAUNCH_DIR_FILES):
        (legacy / f"unrelated-{i}.tmp").write_bytes(b"")
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    return managed, legacy


@pytest.fixture
def repo(tmp_path, monkeypatch, launch_dirs):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    (root / "product.py").write_text("a = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _launch_listings(monkeypatch, dirs: tuple) -> list:
    seen: list = []
    real = os.listdir
    watched = {os.path.normcase(os.path.normpath(str(d))) for d in dirs}

    def counting(path="."):
        if os.path.normcase(os.path.normpath(str(path))) in watched:
            seen.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "listdir", counting)
    return seen


def _min_sample(workdir, *extra: str) -> dict:
    samples = []
    for _ in range(SAMPLES):
        result = batched_process_time_ms(
            [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
        )
        assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
        samples.append(result)
    return min(samples, key=lambda r: r["process_time_ms"])


def test_terminal_shape_process_time_is_under_the_commit_route_bar(tmp_path):
    setup = _min_sample(tmp_path, "--setup-only")
    full = _min_sample(tmp_path)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    detail = (
        f"{net_ms}ms net tree process time over {RUNS} stopped runs "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    assert net_ms > 0, f"call cost not observed (full <= setup-only): {detail}"
    assert net_ms <= COMMIT_ROUTE_BAR_MS, detail


def test_launch_dir_listings_do_not_scale_with_the_stopped_run_count(
    repo, tmp_path, launch_dirs, monkeypatch
):
    seen = _launch_listings(monkeypatch, launch_dirs)
    counts = []
    made = 0
    for total in (3, 9):
        while made < total:
            _stopped_run(repo, tmp_path)
            made += 1
        seen.clear()
        result = _call(repo, _terminal_params(repo))
        assert result["committed"] is True, result
        counts.append(len(seen))

    assert counts[1] == counts[0], f"launch-dir listings grew with the run count: {counts}"
    assert counts[1] <= len(launch_dirs), counts


@pytest.fixture
def ssh_repo(repo, tmp_path, monkeypatch):
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not available on this host")
    key = tmp_path / "id_test"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(key) + ".pub")
    _git(repo, "config", "commit.gpgsign", "true")
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    monkeypatch.setattr(sshsig, "_UNREACHABLE", {})
    return repo


@pytest.mark.deliberate_wall_clock(reason="the reply must land inside the dispatcher deadline while signing stalls; a stalled wait burns no CPU")
def test_stalled_signing_wait_replies_inside_the_dispatcher_deadline(ssh_repo, monkeypatch):
    stall_s = 20.0
    budget_s = dispatch_abandon.REPLY_MARGIN_SECS + 3.0
    monkeypatch.setattr(sshsig, "_exchange", lambda *_a, **_k: time.sleep(stall_s))
    params = _terminal_params(ssh_repo)

    t0 = time.monotonic()
    token = dispatch_abandon.DEADLINE.set(t0 + budget_s)
    try:
        result = _call(ssh_repo, params)
    finally:
        dispatch_abandon.DEADLINE.reset(token)
    elapsed = time.monotonic() - t0

    assert isinstance(result.get("committed"), bool), result
    assert elapsed < budget_s, f"replied after {elapsed:.1f}s against a {budget_s:.1f}s deadline"
    if result["committed"]:
        assert "gpgsig" not in _git(ssh_repo, "cat-file", "commit", result["sha"])
    else:
        assert result["error"]

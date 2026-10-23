"""A halted dispatch.terminal_commit answers inside the dispatcher's deadline: signed-or-unsigned landing, or a named committed:false."""

from __future__ import annotations

import inspect
import shutil
import subprocess
import time

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.benchmarks import halted_record_commit_full_path as bench
from coordinator_core.git import commit as commit_mod
from coordinator_core.git import sshsig
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

_SIGN_ADDRESS_DEFAULT = inspect.signature(sshsig.sign_via_agent).parameters["address"].default

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

HALTED = {"halted": "ledger-check", "reason": "sizing refused: route is not spec-dispatch"}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _params() -> dict:
    return {"script_path": "run.mjs", "run_id": bench.RUN_ID, "halted_by": dict(HALTED)}


@pytest.fixture
def repo(tmp_path):
    return bench.build_fixture(tmp_path)


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


def test_incident_shaped_halted_call_lands_its_record(repo):
    out = terminal_commit._handler(_params(), repo_root=repo / ".git")

    assert out["committed"] is True
    assert _git(repo, "merge-base", "--is-ancestor", out["sha"], "HEAD") == ""


def test_short_deadline_before_commit_v2_replies_committed_false_naming_the_step(repo):
    head = _git(repo, "rev-parse", "HEAD")
    token = dispatch_abandon.DEADLINE.set(time.monotonic() + dispatch_abandon.REPLY_MARGIN_SECS / 2)
    try:
        out = terminal_commit._handler(_params(), repo_root=repo / ".git")
    finally:
        dispatch_abandon.DEADLINE.reset(token)

    assert out["committed"] is False and out["sha"] is None
    assert "ceremony.commit_v2" in out["error"] and "nothing committed" in out["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


def test_signing_stall_replies_inside_the_deadline_through_the_served_path(ssh_repo, monkeypatch):
    from coordinator_core.warm import dispatch_ack, server

    stall_s = 20.0
    monkeypatch.setattr(sshsig, "_exchange", lambda *_a, **_k: time.sleep(stall_s))
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "5")
    store = dispatch_ack.AckStore(boot_ns=0)
    monkeypatch.setattr(server, "_ack_store", store)
    key = "1-4242"
    store.admit(key, "dispatch.terminal_commit")
    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "dispatch.terminal_commit",
        "params": _params(), "_origin_worktree": str(ssh_repo),
    }

    t0 = time.monotonic()
    response = server._run_dispatch(msg, dispatch_key=key)
    elapsed = time.monotonic() - t0

    assert "error" not in response, response
    # Budget 5s: the in-process sign joins on 2 * (5 - REPLY_MARGIN_SECS) / 2 = 3s, then the git fallback is skipped.
    assert elapsed < 5.0, elapsed
    result = response["result"]
    if result.get("committed") is True:
        assert "gpgsig" not in _git(ssh_repo, "cat-file", "commit", result["sha"])
    else:
        assert result["error"]
    assert store.status(key)["state"] == dispatch_ack.STATE_FINISHED


def test_cold_path_signs_with_the_default_agent_timeout(ssh_repo, monkeypatch):
    seen: dict = {}

    def _spy(data, pub, env=None, timeout=sshsig._DEFAULT_TIMEOUT_S, *, address=_SIGN_ADDRESS_DEFAULT):
        seen["timeout"] = timeout
        raise sshsig.AgentUnavailable("spy")

    monkeypatch.setattr(sshsig, "sign_via_agent", _spy)
    assert dispatch_abandon.remaining() is None

    commit_mod._sign_commit_in_process(ssh_repo, "0" * 40, None, "t", "t@example.com", "0 +0000", "m")

    assert seen["timeout"] == sshsig._DEFAULT_TIMEOUT_S


def test_bound_deadline_halves_the_remaining_budget_for_the_agent_timeout(ssh_repo, monkeypatch):
    seen: dict = {}

    def _spy(data, pub, env=None, timeout=sshsig._DEFAULT_TIMEOUT_S, *, address=_SIGN_ADDRESS_DEFAULT):
        seen["timeout"] = timeout
        raise sshsig.AgentUnavailable("spy")

    monkeypatch.setattr(sshsig, "sign_via_agent", _spy)
    token = dispatch_abandon.DEADLINE.set(time.monotonic() + dispatch_abandon.REPLY_MARGIN_SECS + 4.0)
    try:
        commit_mod._sign_commit_in_process(ssh_repo, "0" * 40, None, "t", "t@example.com", "0 +0000", "m")
    finally:
        dispatch_abandon.DEADLINE.reset(token)

    assert 0 < seen["timeout"] <= 2.0


def test_signing_below_the_floor_is_skipped_and_the_commit_lands_unsigned(ssh_repo):
    token = dispatch_abandon.DEADLINE.set(time.monotonic() + dispatch_abandon.REPLY_MARGIN_SECS + 0.2)
    try:
        sha, warning = commit_mod._sign_commit_tree(
            ssh_repo, _git(ssh_repo, "rev-parse", "HEAD^{tree}"), None, "t", "t@example.com", "0 +0000", "m",
        )
    finally:
        dispatch_abandon.DEADLINE.reset(token)

    assert sha is None and "dispatch deadline" in warning

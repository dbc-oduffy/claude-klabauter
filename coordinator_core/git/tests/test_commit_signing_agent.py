"""`write_signed_commit_object` (the commit_v2 signing seam) through a FAKE ssh-agent, and the
machine-wide in-flight limiter. Nothing here contacts a real agent."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git import sshsig
from coordinator_core.git.commit_signing import write_signed_commit_object
from coordinator_core.git.tests.test_commit_sshsig_agent import (
    _HAS_UNIX,
    FakeAgent,
    PipeServer,
    UnixServer,
    _git,
    _isolated,  # noqa: F401 -- autouse fixture
    _repo,
    key,  # noqa: F401 -- fixture
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_STAMP = "1700000000 +0000"


def _inputs(repo):
    tree = _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return tree, head


def _popens(monkeypatch):
    seen = []
    real = subprocess.Popen

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, cmd, *a, **kw):
            seen.append(cmd)
            super().__init__(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return seen


def _serve(tmp_path, agent):
    if sys.platform == "win32":
        name = "\\\\.\\pipe\\claude-klabauter-test-agent-" + uuid.uuid4().hex
        return name, PipeServer(name, agent)
    if not _HAS_UNIX:
        pytest.skip("no AF_UNIX in this Python")
    sock = str(tmp_path / "a.sock")
    return sock, UnixServer(sock, agent)


def test_agent_signs_commit_v2_object_with_zero_spawns(tmp_path, key, monkeypatch):  # noqa: F811
    keyfile, private, pub_text, blob = key
    agent = FakeAgent(private, blob)
    sock, server = _serve(tmp_path, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text)
        tree, head = _inputs(repo)
        monkeypatch.setenv("SSH_AUTH_SOCK", sock)
        monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
        seen = _popens(monkeypatch)
        sha, warning = write_signed_commit_object(
            repo, tree, head, b"subject\n\nbody", "t", "t@local", _STAMP,
        )
        spawned = list(seen)
        assert warning is None and sha
        assert spawned == []
        assert agent.requests == [11, 13]
        verify = _git(repo, "verify-commit", sha, check=False)
        assert verify.returncode == 0, verify.stderr
        assert _git(repo, "cat-file", "-p", sha).stdout.endswith("body\n")
    finally:
        server.close()


def test_no_agent_falls_back_to_one_commit_tree_spawn(tmp_path, key, monkeypatch):  # noqa: F811
    keyfile, _, pub_text, _ = key
    repo = _repo(tmp_path, keyfile, pub_text)
    tree, head = _inputs(repo)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    seen = _popens(monkeypatch)
    sha, warning = write_signed_commit_object(repo, tree, head, "msg", "t", "t@local", _STAMP)
    spawned = list(seen)
    assert warning is None and sha
    assert len(spawned) == 1 and spawned[0][3:5] == ["commit-tree", "-S"]
    assert _git(repo, "verify-commit", sha, check=False).returncode == 0


def test_limiter_caps_in_flight_and_falls_back_when_full(tmp_path, monkeypatch):
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(sshsig, "MAX_IN_FLIGHT", 2)
    monkeypatch.setattr(sshsig, "_SLOT_WAIT_S", 0.05)
    with sshsig.agent_slot():
        with sshsig.agent_slot():
            with pytest.raises(sshsig.AgentUnavailable, match="slots busy"):
                with sshsig.agent_slot():
                    pytest.fail("third slot must not be granted")
        with sshsig.agent_slot():  # a released slot is reusable
            pass


def test_limiter_never_exceeds_cap_under_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(sshsig, "MAX_IN_FLIGHT", 3)
    monkeypatch.setattr(sshsig, "_SLOT_WAIT_S", 5.0)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def work():
        with sshsig.agent_slot():
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            threading.Event().wait(0.02)
            with lock:
                state["now"] -= 1

    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] <= 3 and state["now"] == 0


def test_busy_slots_make_sign_via_agent_unavailable_without_contacting_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(sshsig, "MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(sshsig, "_SLOT_WAIT_S", 0.05)
    monkeypatch.setattr(
        sshsig, "_exchange", lambda *a, **k: pytest.fail("agent contacted without a slot"),
    )
    blob = sshsig._s(b"ssh-ed25519") + sshsig._s(b"k")
    with sshsig.agent_slot():
        with pytest.raises(sshsig.AgentUnavailable):
            sshsig.sign_via_agent(b"x", blob, env={"SSH_AUTH_SOCK": str(tmp_path / "s")})


def test_timed_out_worker_keeps_its_slot_until_it_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(sshsig, "MAX_IN_FLIGHT", 1)
    monkeypatch.setattr(sshsig, "_SLOT_WAIT_S", 0.05)
    started, gate = threading.Event(), threading.Event()

    def hung():
        started.set()
        gate.wait(10)
        return "late"

    fd = sshsig._acquire_slot()
    with pytest.raises(sshsig.AgentUnavailable, match="did not answer"):
        sshsig._run_bounded(hung, 0.05, lambda: os.close(fd))
    assert started.is_set()
    with pytest.raises(sshsig.AgentUnavailable, match="slots busy"):
        sshsig._acquire_slot()
    gate.set()
    monkeypatch.setattr(sshsig, "_SLOT_WAIT_S", 5.0)
    with sshsig.agent_slot():
        pass


def test_tampered_agent_signature_falls_back_to_git(tmp_path, key, monkeypatch):  # noqa: F811
    keyfile, private, pub_text, blob = key

    class Forger(FakeAgent):
        def respond(self, msg):
            reply = super().respond(msg)
            if msg[0] == 13:
                sig = bytearray(reply)
                sig[-1] ^= 1
                return bytes(sig)
            return reply

    agent = Forger(private, blob)
    sock, server = _serve(tmp_path, agent)
    try:
        repo = _repo(tmp_path, keyfile, pub_text)
        tree, head = _inputs(repo)
        monkeypatch.setenv("SSH_AUTH_SOCK", sock)
        monkeypatch.setattr(sshsig, "_SLOT_DIR", tmp_path / "slots")
        seen = _popens(monkeypatch)
        sha, warning = write_signed_commit_object(repo, tree, head, "msg", "t", "t@local", _STAMP)
        spawned = list(seen)
        assert warning is None and sha
        assert agent.requests == [11, 13]
        assert len(spawned) == 1 and spawned[0][3:5] == ["commit-tree", "-S"]
        assert _git(repo, "verify-commit", sha, check=False).returncode == 0
    finally:
        server.close()

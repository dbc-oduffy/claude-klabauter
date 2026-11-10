"""dispatch.terminal_commit: its pre-commit object reads get one bounded retry, then a persisting stall halts `pack-read-stall`."""

from __future__ import annotations

import hashlib
import time

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_objects import PackReadDeadline, PackReadStalled
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.halt_remedy_class import PACK_READ_STALL_HALT_KIND

_BYTES = b"hello"
_SHA256 = hashlib.sha256(_BYTES).hexdigest()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("a wait loop"))


def _reader(monkeypatch, stalls):
    """`_read_object` that raises PackReadDeadline for its first `stalls` calls (None: always)."""
    calls = []

    def read(common_dir, sha):
        calls.append(sha)
        if stalls is None or len(calls) <= stalls:
            raise PackReadDeadline("stalled")
        return ("blob", _BYTES)

    monkeypatch.setattr(git_objects, "_read_object", read)
    return calls


def test_one_stall_is_absorbed_on_the_stale_blob_check(tmp_path, monkeypatch):
    calls = _reader(monkeypatch, 1)
    assert terminal_commit._stale_kept_blobs(tmp_path, {"a.py": "b" * 40}, {"a.py": _SHA256}) == []
    assert len(calls) == 2


def test_a_stale_blob_is_still_reported_after_a_retry(tmp_path, monkeypatch):
    _reader(monkeypatch, 1)
    assert terminal_commit._stale_kept_blobs(tmp_path, {"a.py": "b" * 40}, {"a.py": "0" * 64}) == [("a.py", "b" * 40)]


def test_a_persisting_stall_raises_pack_read_stalled(tmp_path, monkeypatch):
    calls = _reader(monkeypatch, None)
    with pytest.raises(PackReadStalled):
        terminal_commit._stale_kept_blobs(tmp_path, {"a.py": "b" * 40}, {"a.py": _SHA256})
    assert len(calls) == 2


def test_the_halt_reply_names_the_kind_and_a_refire_and_commits_nothing():
    reply = terminal_commit._pack_read_stall_reply(PackReadStalled("pack read exceeded 4.0s"))
    assert reply["committed"] is False and reply["sha"] is None
    assert reply["refused"] == PACK_READ_STALL_HALT_KIND == reply["halt"]["kind"]
    assert "refire" in reply["halt"]["next_action"].lower() and "Refire" in reply["error"]


def test_measured_blob_read_is_routed_through_the_retry():
    import inspect

    assert "retry_past_stall" in inspect.getsource(terminal_commit._measured_blob_supply)
    assert "retry_past_stall" in inspect.getsource(terminal_commit._stale_kept_blobs)

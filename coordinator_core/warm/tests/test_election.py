"""Tests for `coordinator_core.warm.election` -- Windows named-pipe
first-instance election.

Purpose: C14 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md.
Covers `pipe_name`'s shape and its sensitivity to each of its three
load-bearing components (SID, clone hash, engine token), `current_user_sid`'s
SDDL-string shape, and `elect`'s actual win/lose semantics against a live
pipe name -- the kernel's `FILE_FLAG_FIRST_PIPE_INSTANCE` atomicity is the
thing under test, not a mock of it.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C14
"""

from __future__ import annotations

import re
import sys
import uuid

import pytest

from coordinator_core.warm import election

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="election.elect is Windows-only")


def _unique_pipe_name() -> str:
    return election.pipe_name(uuid.uuid4().hex, user_sid="S-1-5-21-1-2-3-1001")


def test_pipe_name_shape():
    name = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert name.startswith(r"\\.\pipe\coordinator-core.")
    parts = name[len(r"\\.\pipe\coordinator-core."):].split(".")
    assert parts[0] == "S-1-5-21-1-2-3-1001"
    assert re.fullmatch(r"[0-9a-f]{16}", parts[1])
    assert parts[2] == "tok1"


def test_pipe_name_changes_with_engine_token():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok2", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert a != b


def test_pipe_name_changes_with_clone_path():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone="..", user_sid="S-1-5-21-1-2-3-1001")
    assert a != b


def test_pipe_name_changes_with_sid():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1002")
    assert a != b


def test_pipe_name_deterministic_for_same_inputs():
    a = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    b = election.pipe_name("tok1", engine_clone=".", user_sid="S-1-5-21-1-2-3-1001")
    assert a == b


def test_pipe_name_defaults_engine_clone_to_repo_root_and_resolves_own_sid():
    name = election.pipe_name("tok1")
    assert name.startswith(r"\\.\pipe\coordinator-core.")
    parts = name[len(r"\\.\pipe\coordinator-core."):].split(".")
    assert parts[0] == election.current_user_sid()


def test_current_user_sid_shape():
    sid = election.current_user_sid()
    assert re.fullmatch(r"S-1-5-21-\d+-\d+-\d+-\d+", sid)


def test_elect_wins_first_instance():
    name = _unique_pipe_name()
    handle = election.elect(name)
    try:
        assert isinstance(handle, int)
    finally:
        import _winapi

        _winapi.CloseHandle(handle)


def test_elect_loses_to_a_live_first_instance():
    import _winapi

    name = _unique_pipe_name()
    winner = election.elect(name)
    try:
        with pytest.raises(election.ElectionLost) as excinfo:
            election.elect(name)
        assert excinfo.value.pipe_name == name
    finally:
        _winapi.CloseHandle(winner)


def test_elect_reelectable_after_winner_closes_handle():
    import _winapi

    name = _unique_pipe_name()
    first = election.elect(name)
    _winapi.CloseHandle(first)

    second = election.elect(name)
    try:
        assert isinstance(second, int)
    finally:
        _winapi.CloseHandle(second)


def test_elect_and_current_user_sid_are_windows_gated():
    real_is_windows = election._is_windows
    election._is_windows = lambda: False
    try:
        with pytest.raises(RuntimeError):
            election.current_user_sid()
        with pytest.raises(RuntimeError):
            election.elect(r"\\.\pipe\does-not-matter")
    finally:
        election._is_windows = real_is_windows


def test_election_lost_is_an_election_error():
    assert issubclass(election.ElectionLost, election.ElectionError)

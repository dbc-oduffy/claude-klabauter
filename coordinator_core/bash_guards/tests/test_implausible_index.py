"""implausible_deletion_note: plausibility gate and inherited-env pinning (pure, no spawns)."""

from __future__ import annotations

from coordinator_core.bash_guards._implausible_index import (
    _IMPLAUSIBLE_PROBE_GATE,
    implausible_deletion_note,
)


def _fake_git(head_n: int, index_n: int, calls: list):
    def run(args, cwd, timeout=2.0):
        calls.append(args[0])
        n = head_n if args[0] == "ls-tree" else index_n
        return 0, "".join("f%d\n" % i for i in range(n))

    return run


def _deleted(n: int) -> list:
    return ["D\tf%d" % i for i in range(n)]


def test_below_gate_spawns_nothing():
    calls: list = []
    assert implausible_deletion_note(
        _deleted(_IMPLAUSIBLE_PROBE_GATE - 1), None, _fake_git(500, 0, calls), env={}
    ) is None
    assert calls == []


def test_empty_index_note_names_inherited_redirect():
    calls: list = []
    note = implausible_deletion_note(
        _deleted(250), None, _fake_git(500, 0, calls),
        env={"GIT_INDEX_FILE": "/x/no-such-index"},
    )
    assert "implausible index" in note and "not checked" in note
    assert "GIT_INDEX_FILE=/x/no-such-index" in note
    assert "GIT_DIR=unset" in note


def test_note_reports_unset_when_no_redirect():
    note = implausible_deletion_note(
        _deleted(250), None, _fake_git(250, 250, []), env={}
    )
    assert "GIT_INDEX_FILE=unset GIT_DIR=unset" in note


def test_genuine_partial_deletion_is_plausible():
    assert implausible_deletion_note(
        _deleted(250), None, _fake_git(500, 500, []), env={}
    ) is None

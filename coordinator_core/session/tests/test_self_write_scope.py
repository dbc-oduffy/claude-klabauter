"""project_self_write_scope: the self scope minus read-only holds."""

from __future__ import annotations

from coordinator_core.session import scope
from coordinator_core.session.touch_record import VERB_RELEASE, VERB_TOUCH, append_event

T, R = VERB_TOUCH, VERB_RELEASE


def _put(sink, verb, path, kind=None, ts=1.0):
    append_event(sink, session_id="s1", agent_id=None, verb=verb, path=path,
                 kind=kind, timestamp=ts, name=None)


def _sink(tmp_path):
    return tmp_path / "s1" / "touch-record.jsonl"


def test_read_only_hold_leaves_self_write_scope(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", "r")
    paths, degraded = scope.project_self_write_scope(sink)
    assert paths == set() and degraded is False


def test_write_then_read_keeps_path(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", "w", 1.0)
    _put(sink, T, "p.py", "r", 2.0)
    assert scope.project_self_write_scope(sink)[0] == {"p.py"}


def test_kindless_touch_keeps_path(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", None)
    assert scope.project_self_write_scope(sink)[0] == {"p.py"}


def test_released_path_absent(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", "w", 1.0)
    _put(sink, R, "p.py", None, 2.0)
    assert scope.project_self_write_scope(sink)[0] == set()


def test_kind_blind_readers_still_see_read_hold(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", "r")
    lines, _ = scope._read_touch_record_as_legacy_lines(sink)
    assert scope.project_self_scope(lines) == {"p.py"}
    assert scope.project_self_write_scope(sink)[0] <= scope.project_self_scope(lines)


def test_degraded_flag_surfaces(tmp_path):
    sink = _sink(tmp_path)
    _put(sink, T, "p.py", "w")
    with open(sink, "ab") as fh:
        fh.write(b"not json\n")
    paths, degraded = scope.project_self_write_scope(sink)
    assert degraded is True and paths == {"p.py"}

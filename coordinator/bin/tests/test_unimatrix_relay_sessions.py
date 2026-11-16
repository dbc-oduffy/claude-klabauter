"""Relay-session parsing, classification and reaping, all over injected inputs."""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "unimatrix_relay_sessions_under_test",
    Path(__file__).resolve().parents[1] / "lib" / "unimatrix_relay_sessions.py")
rs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rs)

PS = """  PID  PPID STARTED                      COMMAND
  101   100 Wed Nov 11 21:38:02 2026 /opt/homebrew/bin/python3 unimatrix/unimatrix-relay.py
  102     1 Thu Nov 12 04:43:10 2026 /opt/homebrew/bin/python3 unimatrix/unimatrix-relay.py
  103   100 Thu Nov 12  6:00:00 2026 /opt/homebrew/bin/python3 unimatrix/unimatrix-file-relay.py --peer b
  104   100 Thu Nov 12  6:00:00 2026 vim unimatrix-relay.py
  105   100 Thu Nov 12  6:00:00 2026 /usr/bin/python3 other.py
garbage line
"""


def _ts(s):
    return time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y"))


def test_list_picks_only_relay_argv_and_parses_padded_day():
    got = rs.list_relay_sessions(PS)
    assert [s.pid for s in got] == [101, 102, 103]
    assert got[2].script == "unimatrix-file-relay.py"
    assert got[2].started == _ts("Thu Nov 12 06:00:00 2026")


def test_classify_mismatched_stale_live():
    sessions = rs.list_relay_sessions(PS)
    script = _ts("Thu Nov 12 05:00:00 2026")
    out = rs.classify_relay_sessions(sessions, script, lambda ppid: ppid == 100)
    assert [s.pid for s in out[rs.MISMATCHED]] == [101]
    assert [s.pid for s in out[rs.STALE]] == [102]
    assert [s.pid for s in out[rs.LIVE]] == [103]


def test_dead_parent_is_stale_and_mapping_stamp_is_per_script():
    sessions = rs.list_relay_sessions(PS)
    out = rs.classify_relay_sessions(
        sessions, {"unimatrix-relay.py": 0.0}, lambda ppid: False)
    assert len(out[rs.STALE]) == 3
    out = rs.classify_relay_sessions(sessions, {"unimatrix-relay.py": 0.0}, lambda ppid: True)
    assert [s.pid for s in out[rs.LIVE]] == [101, 103]


def test_reap_kills_only_given_and_tolerates_gone_and_denied():
    sessions = rs.list_relay_sessions(PS)
    seen = []

    def kill(pid):
        seen.append(pid)
        if pid == 102:
            raise ProcessLookupError
        if pid == 103:
            raise PermissionError

    reaped, failed = rs.reap(sessions, kill)
    assert seen == [101, 102, 103]
    assert reaped == [101, 102] and failed == [103]
    assert rs.reap([], kill) == ([], [])


def test_same_key_successor_makes_the_older_session_stale():
    sessions = rs.list_relay_sessions(PS)
    out = rs.classify_relay_sessions(sessions, 0.0, lambda ppid: True,
                                     key=lambda s: s.ppid)
    assert [s.pid for s in out[rs.STALE]] == [101, 102]  # 102 is orphaned (ppid 1) regardless
    assert [s.pid for s in out[rs.LIVE]] == [103]
    # without a key nothing is superseded
    out = rs.classify_relay_sessions(sessions, 0.0, lambda ppid: True)
    assert out[rs.STALE] == [sessions[1]]


def test_distinct_or_none_keys_never_supersede():
    sessions = rs.list_relay_sessions(PS)
    out = rs.classify_relay_sessions(sessions, 0.0, lambda ppid: True, key=lambda s: s.pid)
    assert [s.pid for s in out[rs.STALE]] == [102]
    out = rs.classify_relay_sessions(sessions, 0.0, lambda ppid: True, key=lambda s: None)
    assert [s.pid for s in out[rs.STALE]] == [102]


def test_reap_releases_each_reaped_session_but_not_a_denied_one():
    sessions = rs.list_relay_sessions(PS)
    released = []

    def kill(pid):
        if pid == 103:
            raise PermissionError

    reaped, failed = rs.reap(sessions, kill, release=lambda s: released.append(s.pid))
    assert reaped == [101, 102] and failed == [103]
    assert released == [101, 102]

    def boom(s):
        raise OSError

    assert rs.reap(sessions[:1], kill, release=boom) == ([101], [])


def test_ps_snapshot_sets_locale_and_swallows_failure():
    calls = []

    class R:
        returncode = 0
        stdout = "out"

    def run(argv, **kw):
        calls.append((argv, kw))
        return R()

    assert rs.ps_snapshot(run) == "out"
    assert calls[0][0] == rs.PS_ARGV and calls[0][1]["env"]["LC_ALL"] == "C"

    def boom(argv, **kw):
        raise OSError

    assert rs.ps_snapshot(boom) == ""

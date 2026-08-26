"""
Tests for coordinator_core.ops.session.reap._reap_stale_agents (sub-reap (ii)):
the sweep that moves stale per-agent dirs under
``.git/coordinator-sessions/.agents/<aid>/`` into ``.archive/``.

WHY THIS MODULE EXISTS, and it is the whole point of it. Sub-reap (ii) had NO
unit coverage — ``test_reap.py``'s negative-spec says so in as many words
("does not exercise sub-reaps (i)/(ii)/(iii)"). It was keyed on a single
record filename, ``touched.txt``.

THE OBSERVED DEFECT: an agent dir holding ``em-session-id.txt`` but no
``touched.txt`` — a dispatched agent that touched no file — was unreapable
forever. Absent that one name the sweep fell to the empty-dir ``rmdir`` arm,
which raises ``OSError`` because the back-pointer is still in the dir, and
that error was swallowed best-effort. No archive, no removal, no log line, on
every pass. Measured on this box: 175 such dirs already past the staleness
window, 114 more not yet due, against 219 carrying ``touched.txt`` that reaped
normally.

The residue accumulates on ``scope.compute_scope``'s Step 3b scan, which pays
a file read plus several stats per agent dir on the COMMIT HOT PATH: 1893 dirs
observed, measured at 203-266ms of process time against the brightline's 200ms
rule. Reaping the orphans returned the same call to 94-109ms.

SECOND, PROSPECTIVE REASON for keying on mtime rather than on a WIDENED
FILENAME LIST: the touched-files record redesign renames this record to
``touch-record.jsonl`` plus rotated siblings. A name-keyed predicate would
acquire the identical silent stop at that cutover. Note the record has NOT
been renamed on disk yet — zero ``touch-record.jsonl`` files exist across 507
live agent dirs as of 2026-08-25 — so those two cases below are guards against
a future shape, not reproductions of a current one. An earlier version of this
docstring asserted that rename had already landed and caused the residue; it
had not, and that claim was withdrawn.

So the defect was invisible in three ways at once — no test, no surfaced
error, and the only symptom a slow unrelated caller.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops.session import reap

pytestmark = [pytest.mark.cadence]

_STALE = reap._AGENT_STALE_SECONDS + 3600
_FRESH = 60


def _agent_dir(sessions_dir: Path, aid: str, files: dict, age_sec: float) -> Path:
    """Plant one agent dir holding ``files`` (name -> contents), aged by
    setting every member's mtime ``age_sec`` into the past."""
    adir = sessions_dir / ".agents" / aid
    adir.mkdir(parents=True)
    when = time.time() - age_sec
    for name, body in files.items():
        member = adir / name
        member.write_text(body, encoding="utf-8")
        os.utime(member, (when, when))
    return adir


def test_backpointer_only_dir_is_reaped_when_stale(tmp_path):
    """THE OBSERVED DEFECT, and the reason this module exists. A stale agent
    dir holding only ``em-session-id.txt`` — a dispatched agent that touched
    no file — is archived.

    Pre-fix this returned reaped==[] and left the dir in place forever: the
    predicate looked for ``touched.txt``, did not find it, tried to rmdir a
    dir that still held the back-pointer, and swallowed the OSError. 175 dirs
    on this box were in exactly this state."""
    sessions = tmp_path / "coordinator-sessions"
    adir = _agent_dir(
        sessions, "agent-backptr-only", {"em-session-id.txt": "sid-dead\n"}, _STALE
    )

    reaped, deferred, failed = reap._reap_stale_agents(sessions)

    assert reaped == ["agent-backptr-only"], (reaped, deferred, failed)
    assert not adir.exists()
    assert (sessions / ".archive").is_dir()


def test_future_record_name_is_reaped_when_stale(tmp_path):
    """PROSPECTIVE, not a current shape: zero ``touch-record.jsonl`` files
    exist on disk today. This pins that the cutover to that name cannot
    silently disable the sweep the way the missing-``touched.txt`` case did."""
    sessions = tmp_path / "coordinator-sessions"
    adir = _agent_dir(
        sessions, "agent-future-name",
        {"touch-record.jsonl": '{"path":"a.py"}\n', "em-session-id.txt": "sid-dead\n"},
        _STALE,
    )

    reaped, deferred, failed = reap._reap_stale_agents(sessions)

    assert reaped == ["agent-future-name"], (reaped, deferred, failed)
    assert not adir.exists()


def test_rotated_only_record_is_reaped_when_stale(tmp_path):
    """Also prospective. A dir holding only ROTATED siblings still reaps — the
    rotation suffix is one more filename a name-keyed predicate would miss."""
    sessions = tmp_path / "coordinator-sessions"
    _agent_dir(
        sessions, "agent-rotated",
        {"touch-record.jsonl.rotated-1755000000000-4242.jsonl": '{"path":"b.py"}\n'},
        _STALE,
    )

    reaped, _deferred, failed = reap._reap_stale_agents(sessions)

    assert reaped == ["agent-rotated"], (reaped, failed)


def test_legacy_touched_txt_still_reaped(tmp_path):
    """The pre-C7 name keeps working. ~500 such dirs survive on this box, so
    the fix must widen the predicate, never swap one filename for another."""
    sessions = tmp_path / "coordinator-sessions"
    _agent_dir(sessions, "agent-legacy", {"touched.txt": "a.py\n"}, _STALE)

    reaped, _deferred, failed = reap._reap_stale_agents(sessions)

    assert reaped == ["agent-legacy"], (reaped, failed)


def test_fresh_agent_dir_is_kept(tmp_path):
    """A dir written inside the staleness window is left alone — the fix must
    not reap live sub-agents' bookkeeping out from under them."""
    sessions = tmp_path / "coordinator-sessions"
    adir = _agent_dir(
        sessions, "agent-fresh", {"touch-record.jsonl": '{"path":"c.py"}\n'}, _FRESH
    )

    reaped, _deferred, _failed = reap._reap_stale_agents(sessions)

    assert reaped == []
    assert adir.exists()


def test_newest_member_decides_not_oldest(tmp_path):
    """A dir whose record is ancient but which has RECENT activity in another
    file is kept. Staleness is max-over-files, so an unrecognised-but-fresh
    member defers the reap — the fail-closed-to-keep direction, and what makes
    the next record rename unable to silently disable this sweep again."""
    sessions = tmp_path / "coordinator-sessions"
    adir = _agent_dir(
        sessions, "agent-mixed", {"touch-record.jsonl": '{"path":"d.py"}\n'}, _STALE
    )
    recent = adir / "some-future-record-name.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    now = time.time()
    os.utime(recent, (now, now))

    reaped, _deferred, _failed = reap._reap_stale_agents(sessions)

    assert reaped == []
    assert adir.exists()


def test_genuinely_empty_dir_is_removed(tmp_path):
    """The empty-dir rmdir arm survives the rewrite — it is now reached only
    when the dir holds no file at all, rather than competing with a
    record-bearing dir whose record simply had an unfamiliar name."""
    sessions = tmp_path / "coordinator-sessions"
    adir = sessions / ".agents" / "agent-empty"
    adir.mkdir(parents=True)

    reaped, _deferred, _failed = reap._reap_stale_agents(sessions)

    assert reaped == []
    assert not adir.exists()


def test_missing_agents_base_is_a_noop(tmp_path):
    sessions = tmp_path / "coordinator-sessions"
    sessions.mkdir()

    assert reap._reap_stale_agents(sessions) == ([], [], [])

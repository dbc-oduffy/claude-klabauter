"""Tests for `coordinator_core.group_em.watch_spool` -- the wake spool's
path helper and its one shortening op, `clear` (sizing
`state/sizings/2026-09-01-the-group-em-wake-gets-the-spool-it-is-m.yaml`).

Covers: `clear` on an absent spool is not an error, `clear` truncates a
populated spool to empty regardless of what it holds (malformed lines,
unknown keys, a missing trailing newline included -- `clear` never opens the
file to inspect it). The debounce, the timestamp reader, and drain-point
retention this file used to cover were deleted
(`state/improvement-queue/2026-09-02-the-wake-spool-s-debounce-optimises-an-a-e480fd8bba2d.yaml`,
findings 2/5) -- there is no production reader of spool content left to
test, and `clear` replaced the old filtered `compact`.
"""

from __future__ import annotations

import json
import os

from coordinator_core.group_em import watch_spool


def _records(repo_root):
    """Every well-formed record on disk. The module deliberately exposes no
    reader of its own -- the test that wants to SEE the file parses it here."""
    path = watch_spool.spool_path(str(repo_root))
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as fh:
        for line in fh.read().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def _write_lines(repo_root, lines):
    os.makedirs(os.path.join(str(repo_root), "state"), exist_ok=True)
    with open(watch_spool.spool_path(str(repo_root)), "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")


def test_absent_spool_is_not_an_error(tmp_path):
    (tmp_path / "state").mkdir()
    assert _records(tmp_path) == []
    assert watch_spool.clear(str(tmp_path)) is True
    assert not os.path.exists(watch_spool.spool_path(str(tmp_path)))


def test_clear_empties_a_spool_holding_well_formed_records(tmp_path):
    good = json.dumps(
        {"session_id": "s1", "state": "PAUSED:idle", "at": "2026-09-02T10:00:00Z",
         "writer": "receiver-state-sensor"}
    )
    _write_lines(tmp_path, [good])
    assert len(_records(tmp_path)) == 1

    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []


def test_clear_empties_a_spool_holding_malformed_and_torn_lines(tmp_path):
    """`clear` never opens the file to inspect it -- a malformed or torn
    line is dropped exactly like a well-formed one, because nothing here
    reads a record's content before truncating."""
    good = json.dumps(
        {"session_id": "s1", "state": "PAUSED:idle", "at": "2026-09-02T10:00:00Z",
         "writer": "receiver-state-sensor"}
    )
    torn = '{"session_id": "s2", "state": "PAUSED:i'  # interleaved/truncated write
    scalar = "42"  # valid JSON, not an object
    _write_lines(tmp_path, [good, torn, scalar, "", "   "])

    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []


def test_clear_empties_a_spool_holding_unknown_keys(tmp_path):
    """The producer may add a key without a version bump -- `clear` never
    reads far enough into a record to reject or accept it for carrying one."""
    line = json.dumps(
        {"session_id": "s1", "state": "PAUSED:idle", "at": "2026-09-02T10:00:00Z",
         "writer": "receiver-state-sensor", "future_field": "whatever"}
    )
    _write_lines(tmp_path, [line])
    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []


def test_clear_empties_a_spool_missing_its_final_newline(tmp_path):
    """The producer writes one `write()` per record; a crash mid-append can
    leave the file without its final newline -- `clear` truncates it anyway,
    since it never parses lines at all."""
    os.makedirs(os.path.join(str(tmp_path), "state"), exist_ok=True)
    with open(watch_spool.spool_path(str(tmp_path)), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "p1", "state": "PAUSED:turn-ended",
                             "at": "2026-09-02T10:00:00Z", "writer": "s"}))
    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []


def test_clear_is_idempotent_on_an_already_empty_spool(tmp_path):
    _write_lines(tmp_path, [])
    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []
    assert watch_spool.clear(str(tmp_path)) is True
    assert _records(tmp_path) == []

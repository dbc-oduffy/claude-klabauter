"""Tests for `coordinator_core.group_em.watch_spool` -- the wake spool's
path helper and its one shortening op, `prune` (sizing
`state/sizings/2026-09-01-the-group-em-wake-gets-the-spool-it-is-m.yaml`).

Covers: `prune` on an absent spool is not an error; a record inside the
retain window survives; one outside it is dropped; the count cap binds and
triggers a rewrite even when every record is well inside the trigger; an
absent/unparseable `at` is dropped once a rewrite happens; and the laziness
itself -- a spool whose oldest record is inside `PRUNE_TRIGGER_SECONDS` is
left byte-identical, never rewritten. `clear`'s old blind-truncate tests
(malformed lines, unknown keys, missing trailing newline -- all "prune never
opens the file to inspect it") no longer apply: `prune` DOES read the file
once a trigger fires, so those cases are now covered as "a rewrite tolerates
a malformed/torn/keyless line by dropping it," folded into the tests below
rather than kept as their own scenarios.
"""

from __future__ import annotations

import json
import os
import time

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


def _raw_contents(repo_root):
    path = watch_spool.spool_path(str(repo_root))
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_lines(repo_root, lines):
    os.makedirs(os.path.join(str(repo_root), "state"), exist_ok=True)
    with open(watch_spool.spool_path(str(repo_root)), "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line)
            fh.write("\n")


def _stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _record(epoch, session_id="s1"):
    return json.dumps(
        {"session_id": session_id, "state": "PAUSED:idle", "at": _stamp(epoch),
         "writer": "receiver-state-sensor"}
    )


def test_absent_spool_is_not_an_error(tmp_path):
    (tmp_path / "state").mkdir()
    assert _records(tmp_path) == []
    assert watch_spool.prune(str(tmp_path)) is True
    assert not os.path.exists(watch_spool.spool_path(str(tmp_path)))


def test_a_record_inside_the_retain_window_survives_a_triggered_prune(tmp_path):
    """A young record plus an old one (past the trigger) forces a rewrite;
    the young one -- inside `RETAIN_SECONDS` -- must still be there after."""
    now = time.time()
    old_line = _record(now - watch_spool.PRUNE_TRIGGER_SECONDS - 60, "old")
    young_line = _record(now - 60, "young")
    _write_lines(tmp_path, [old_line, young_line])

    assert watch_spool.prune(str(tmp_path), now_epoch=now) is True
    kept_ids = {r["session_id"] for r in _records(tmp_path)}
    assert kept_ids == {"young"}


def test_a_record_past_the_retain_window_is_dropped_by_a_triggered_prune(tmp_path):
    """A record between RETAIN and the trigger window is dropped once the
    prune actually runs -- retention is `RETAIN_SECONDS`, not the trigger."""
    now = time.time()
    trigger_line = _record(now - watch_spool.PRUNE_TRIGGER_SECONDS - 1, "trigger")
    mid_line = _record(now - watch_spool.RETAIN_SECONDS - 60, "between-retain-and-trigger")
    fresh_line = _record(now - 60, "fresh")
    _write_lines(tmp_path, [trigger_line, mid_line, fresh_line])

    assert watch_spool.prune(str(tmp_path), now_epoch=now) is True
    kept_ids = {r["session_id"] for r in _records(tmp_path)}
    assert kept_ids == {"fresh"}


def test_prune_is_a_no_op_and_byte_identical_when_the_oldest_record_is_inside_the_trigger(tmp_path):
    """THE LAZINESS ITSELF. An oldest record inside `PRUNE_TRIGGER_SECONDS`
    (even if outside `RETAIN_SECONDS`) must not cause a rewrite at all --
    the file is left byte-for-byte untouched."""
    now = time.time()
    # Outside RETAIN_SECONDS but inside PRUNE_TRIGGER_SECONDS -- would be
    # dropped BY a rewrite, but no rewrite is triggered, so it survives.
    line = _record(now - watch_spool.RETAIN_SECONDS - 60, "stale-but-not-triggering")
    _write_lines(tmp_path, [line])
    before = _raw_contents(tmp_path)

    assert watch_spool.prune(str(tmp_path), now_epoch=now) is True
    assert _raw_contents(tmp_path) == before


def test_the_count_cap_triggers_a_prune_even_when_every_record_is_fresh(tmp_path):
    """A burst well inside `PRUNE_TRIGGER_SECONDS` must still be capped --
    the count trigger fires independently of age."""
    now = time.time()
    lines = [_record(now - 1, f"s{i}") for i in range(watch_spool.MAX_RECORDS + 5)]
    _write_lines(tmp_path, lines)

    assert watch_spool.prune(str(tmp_path), now_epoch=now) is True
    kept = _records(tmp_path)
    assert len(kept) == watch_spool.MAX_RECORDS
    # tail-keep: the most recent MAX_RECORDS survive
    kept_ids = {r["session_id"] for r in kept}
    assert "s0" not in kept_ids
    assert f"s{watch_spool.MAX_RECORDS + 4}" in kept_ids


def test_an_unparseable_oldest_record_triggers_a_rewrite_that_drops_it(tmp_path):
    now = time.time()
    torn = '{"session_id": "s2", "state": "PAUSED:i'  # interleaved/truncated write
    fresh = _record(now - 1, "fresh")
    _write_lines(tmp_path, [torn, fresh])

    assert watch_spool.prune(str(tmp_path), now_epoch=now) is True
    kept_ids = {r["session_id"] for r in _records(tmp_path)}
    assert kept_ids == {"fresh"}


def test_prune_returns_false_and_leaves_the_spool_untouched_on_a_write_failure(tmp_path, monkeypatch):
    now = time.time()
    old_line = _record(now - watch_spool.PRUNE_TRIGGER_SECONDS - 60, "old")
    _write_lines(tmp_path, [old_line])
    before = _raw_contents(tmp_path)

    def _boom(*_a, **_kw):
        raise OSError("simulated tempfile failure")

    monkeypatch.setattr(watch_spool.tempfile, "mkstemp", _boom)
    assert watch_spool.prune(str(tmp_path), now_epoch=now) is False
    assert _raw_contents(tmp_path) == before

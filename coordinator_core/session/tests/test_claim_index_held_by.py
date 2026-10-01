"""``_LookupResult.held_by`` — per-claimant write-held paths from lookup's own rebuild."""

import os
import subprocess

from coordinator_core.session import claim_index, touch_record

T0 = 1_780_000_000.0


def _ev(base, sid, verb, path, kind=None, ts=T0):
    touch_record.append_event(
        os.path.join(base, sid, "touch-record.jsonl"),
        session_id=sid,
        agent_id=None,
        verb=verb,
        path=path,
        timestamp=ts,
        kind=kind,
    )


def test_peer_write_held_paths_exclude_reads(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", touch_record.KIND_WRITE)
    _ev(base, "peer", "T", "t.py", touch_record.KIND_WRITE)
    _ev(base, "peer", "T", "r.py", touch_record.KIND_READ)
    _ev(base, "me", "T", "a.py", touch_record.KIND_WRITE)

    result = claim_index.lookup(["a.py"], sessions_dir=base)

    assert result.held_by["peer"] == ["a.py", "t.py"]
    assert "me" in result.held_by


def test_unreturned_claimant_absent(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", touch_record.KIND_WRITE)
    _ev(base, "other", "T", "z.py", touch_record.KIND_WRITE)

    result = claim_index.lookup(["a.py"], sessions_dir=base)

    assert set(result.held_by) == {"peer"}


def test_released_path_absent(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", touch_record.KIND_WRITE, ts=T0)
    _ev(base, "peer", "T", "t.py", touch_record.KIND_WRITE, ts=T0)
    _ev(base, "peer", "R", "t.py", None, ts=T0 + 1)

    result = claim_index.lookup(["a.py"], sessions_dir=base)

    assert result.held_by["peer"] == ["a.py"]


def test_absent_kind_counts_as_write(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", None)

    assert claim_index.lookup(["a.py"], sessions_dir=base).held_by == {"peer": ["a.py"]}


def test_fully_released_sid_contributes_nothing(tmp_path):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", touch_record.KIND_WRITE, ts=T0)
    _ev(base, "peer", "R", "a.py", None, ts=T0 + 1)

    assert claim_index.lookup(["a.py"], sessions_dir=base).held_by == {}


def test_empty_base_yields_empty_held_by(monkeypatch):
    monkeypatch.setattr(claim_index, "_resolve_base", lambda *a, **k: None)

    result = claim_index.lookup(["a.py"])

    assert result.held_by == {}


def test_no_subprocess(tmp_path, monkeypatch):
    base = str(tmp_path)
    _ev(base, "peer", "T", "a.py", touch_record.KIND_WRITE)

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)

    assert claim_index.lookup(["a.py"], sessions_dir=base).held_by == {"peer": ["a.py"]}

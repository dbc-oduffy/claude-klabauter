"""Tests for the day-keyed apply resume checkpoint."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ceremony_common import apply_checkpoint as ac


@pytest.fixture
def sess(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(ac, "sessions_dir", lambda _r: str(root))
    monkeypatch.setattr(ac, "local_day", lambda _r=None: "2026-10-17")

    def no_spawn(*a, **k):
        raise AssertionError("checkpoint must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    return root


def _open(sess, **kw):
    return ac.open_checkpoint(sess, "workday-complete", **kw)


def _files(sess):
    return sorted((sess / "workday-complete-apply").glob("*.jsonl"))


def test_round_trip(sess):
    cp = _open(sess, dirty_before={"a.md": "M"})
    cp.record_dispatching("d1")
    cp.record_landed("d1", {"x": 1}, stdout="out")
    cp.record_dispatching("d2")
    again = _open(sess)
    assert again.landed_ids() == {"d1"}
    assert again.interrupted_ids() == {"d2"}
    assert again.stdout_of("d1") == "out"
    assert again.stdout_of("d2") is None
    assert again.dirty_before() == {"a.md": "M"}
    assert not again.args_changed("d1", {"x": 1})
    assert again.args_changed("d1", {"x": 2})


def test_stdout_absent_when_not_passed(sess):
    cp = _open(sess)
    cp.record_dispatching("d1")
    cp.record_landed("d1", {})
    row = [json.loads(l) for l in _files(sess)[0].read_text().splitlines()][-1]
    assert "stdout" not in row
    assert _open(sess).stdout_of("d1") is None


def test_finished_resets_landed_set(sess):
    cp = _open(sess, dirty_before={"a": "M"})
    cp.record_dispatching("d1")
    cp.record_landed("d1", {})
    cp.finish()
    assert cp.landed_ids() == frozenset()
    fresh = _open(sess, dirty_before={"b": "M"})
    assert fresh.landed_ids() == frozenset()
    assert fresh.interrupted_ids() == frozenset()
    assert fresh.dirty_before() == {"b": "M"}


def test_other_day_finished_pruned_unfinished_kept(sess, monkeypatch):
    monkeypatch.setattr(ac, "local_day", lambda _r=None: "2026-10-16")
    done = _open(sess)
    done.record_dispatching("d1")
    done.record_landed("d1", {})
    done.finish()
    monkeypatch.setattr(ac, "local_day", lambda _r=None: "2026-10-15")
    dead = _open(sess, for_date="x")
    dead.record_dispatching("d1")
    monkeypatch.setattr(ac, "local_day", lambda _r=None: "2026-10-17")
    cp = _open(sess)
    names = [p.name for p in _files(sess)]
    assert not any(n.startswith("2026-10-16") for n in names)
    assert any(n.startswith("2026-10-15") for n in names)
    assert cp.stale_checkpoint is not None and "2026-10-15" in cp.stale_checkpoint
    assert cp.landed_ids() == frozenset()


def test_for_date_and_only_keys_are_isolated(sess):
    default = _open(sess)
    default.record_dispatching("d1")
    default.record_landed("d1", {})
    assert _open(sess, for_date="2026-10-01").landed_ids() == frozenset()
    assert _open(sess, only_mode=True).landed_ids() == frozenset()
    assert _open(sess).landed_ids() == {"d1"}
    assert len(_files(sess)) == 3


def test_malformed_line_tolerated(sess, capsys):
    cp = _open(sess)
    cp.record_dispatching("d1")
    cp.record_landed("d1", {})
    path = _files(sess)[0]
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{torn\n")
    again = _open(sess)
    assert again.landed_ids() == {"d1"}
    assert "malformed" in capsys.readouterr().err


def test_unavailable_sessions_dir_is_empty_and_noop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ac, "sessions_dir", lambda _r: "")
    cp = ac.open_checkpoint(tmp_path, "workday-complete")
    assert not cp.available
    cp.record_dispatching("d1")
    cp.record_landed("d1", {}, stdout="s")
    cp.finish()
    assert cp.landed_ids() == frozenset()
    assert "resume unavailable" in capsys.readouterr().err


def test_unreadable_file_fails_open(sess, capsys):
    d = sess / "workday-complete-apply"
    d.mkdir()
    (d / f"2026-10-17-{ac._run_key(None, False)}.jsonl").write_bytes(b"\xff\xfe\x00bad")
    cp = _open(sess)
    assert not cp.available
    assert cp.landed_ids() == frozenset()
    assert "resume unavailable" in capsys.readouterr().err


def test_failed_append_degrades(sess, monkeypatch, capsys):
    cp = _open(sess)
    monkeypatch.setattr(ac, "append_line", lambda *a: (_ for _ in ()).throw(OSError("full")))
    cp.record_dispatching("d1")
    assert not cp.available
    cp.record_landed("d1", {})
    assert "resume unavailable" in capsys.readouterr().err


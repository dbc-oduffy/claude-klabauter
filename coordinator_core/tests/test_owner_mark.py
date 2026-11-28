"""owner_mark: mark round-trip, owner liveness per kind (fail closed to keep), and the keep-set."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core import liveness, owner_mark
from coordinator_core.ask_spool import record
from coordinator_core.ops.dispatch_emit import abandoned_runs, run_scratch_reap


def _session_mark(sid: str = "sess-a") -> dict:
    return {"owner_kind": "session", "owner_id": sid, "repo": None, "created": "2026-01-01T00:00:00+00:00", "ttl_hours": 1}


def test_write_read_round_trip(tmp_path):
    written = owner_mark.write_mark(tmp_path / "d", "session", "sess-a", repo="R", ttl_hours=2)
    read = owner_mark.read_mark(tmp_path / "d")
    assert read == written
    assert read["owner_kind"] == "session" and read["owner_id"] == "sess-a"
    assert read["repo"] == "R" and read["ttl_hours"] == 2
    assert datetime.fromisoformat(read["created"]).tzinfo is not None
    assert not list((tmp_path / "d").glob("*.tmp"))


def test_write_mark_refuses_bad_input(tmp_path):
    with pytest.raises(ValueError):
        owner_mark.write_mark(tmp_path, "tenant", "x")
    with pytest.raises(ValueError):
        owner_mark.write_mark(tmp_path, "pid", "  ")


@pytest.mark.parametrize("body", ["", "[]", "{not json", '{"owner_kind": "bogus", "owner_id": "x"}', '{"owner_kind": "pid"}'])
def test_read_mark_rejects_malformed(tmp_path, body):
    (tmp_path / owner_mark.MARK_FILE).write_text(body, encoding="utf-8")
    assert owner_mark.read_mark(tmp_path) is None


def test_read_mark_absent(tmp_path):
    assert owner_mark.read_mark(tmp_path) is None


def test_session_alive_true_false_and_error(monkeypatch):
    monkeypatch.setattr(liveness, "resolve_live_session_ids", lambda: frozenset({"sess-a", "sess-b"}))
    assert owner_mark.owner_alive(_session_mark("sess-a")) is True
    assert owner_mark.owner_alive(_session_mark("sess-dead")) is False

    def boom():
        raise RuntimeError("resolver down")

    monkeypatch.setattr(liveness, "resolve_live_session_ids", boom)
    assert owner_mark.owner_alive(_session_mark("sess-a")) is None


def test_session_empty_live_set_is_unknown_not_dead(monkeypatch):
    monkeypatch.setattr(liveness, "resolve_live_session_ids", lambda: frozenset())
    assert owner_mark.owner_alive(_session_mark()) is None


def test_session_of_another_repo_is_unknown(monkeypatch, tmp_path):
    from coordinator_core.session import core as session_core

    monkeypatch.setattr(liveness, "resolve_live_session_ids", lambda: frozenset({"other"}))
    monkeypatch.setattr(session_core, "sessions_dir", lambda cwd=None: "A" if cwd is None else "B")
    mark = {**_session_mark("sess-a"), "repo": str(tmp_path)}
    assert owner_mark.owner_alive(mark) is None


def test_unknown_or_malformed_mark_is_none():
    assert owner_mark.owner_alive(None) is None
    assert owner_mark.owner_alive({}) is None
    assert owner_mark.owner_alive({"owner_kind": "session", "owner_id": ""}) is None
    assert owner_mark.owner_alive({"owner_kind": "tenant", "owner_id": "x"}) is None


def _run_mark(tmp_path) -> dict:
    return {"owner_kind": "run", "owner_id": "run-1", "repo": str(tmp_path), "created": "2026-01-01T00:00:00+00:00", "ttl_hours": None}


def test_run_alive_until_terminal_and_released(monkeypatch, tmp_path):
    mark = _run_mark(tmp_path)
    monkeypatch.setattr(abandoned_runs, "_terminal_state", lambda root, rid: None)
    assert owner_mark.owner_alive(mark) is True

    monkeypatch.setattr(abandoned_runs, "_terminal_state", lambda root, rid: "landed")
    monkeypatch.setattr(run_scratch_reap, "_own_run_gate", lambda root, rid: ("", False))
    assert owner_mark.owner_alive(mark) is False

    monkeypatch.setattr(run_scratch_reap, "_own_run_gate", lambda root, rid: ("landing retest is live", False))
    assert owner_mark.owner_alive(mark) is True

    monkeypatch.setattr(run_scratch_reap, "_own_run_gate", lambda root, rid: ("", True))
    assert owner_mark.owner_alive(mark) is True


def test_run_error_and_missing_repo_are_unknown(monkeypatch, tmp_path):
    def boom(root, rid):
        raise ValueError("unreadable")

    monkeypatch.setattr(abandoned_runs, "_terminal_state", boom)
    assert owner_mark.owner_alive(_run_mark(tmp_path)) is None
    assert owner_mark.owner_alive({**_run_mark(tmp_path), "repo": None}) is None


def _pid_mark(pid, created=None) -> dict:
    return {
        "owner_kind": "pid", "owner_id": str(pid), "repo": None,
        "created": created or datetime.now(timezone.utc).isoformat(), "ttl_hours": None,
    }


def test_pid_probe_states(monkeypatch):
    assert owner_mark.owner_alive(_pid_mark(os.getpid())) is True
    monkeypatch.setattr(record, "pid_state", lambda pid: False)
    assert owner_mark.owner_alive(_pid_mark(4242)) is False
    monkeypatch.setattr(record, "pid_state", lambda pid: None)
    assert owner_mark.owner_alive(_pid_mark(4242)) is None
    assert owner_mark.owner_alive(_pid_mark("not-a-pid")) is None
    assert owner_mark.owner_alive(_pid_mark(0)) is None


def test_pid_reused_after_mark_reads_dead(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(record, "pid_state", lambda pid: True)
    monkeypatch.setattr(record, "pid_create_time", lambda pid: now.timestamp())
    old = (now - timedelta(hours=1)).isoformat()
    assert owner_mark.owner_alive(_pid_mark(4242, created=old)) is False
    assert owner_mark.owner_alive(_pid_mark(4242, created=now.isoformat())) is True


def test_past_ttl():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    mark = {"created": "2026-01-01T00:00:00+00:00", "ttl_hours": 6}
    assert owner_mark.past_ttl(mark, now) is True
    assert owner_mark.past_ttl({**mark, "ttl_hours": 24}, now) is False
    assert owner_mark.past_ttl({**mark, "ttl_hours": None}, now) is None
    assert owner_mark.past_ttl({"ttl_hours": 1}, now) is None


def test_keep_set_names_the_three_classes():
    assert set(owner_mark.KEEP_SET) == {"publish-engine", "clean-mirror", "publish-clone"}


def _patch_keep_inputs(monkeypatch, tmp_path, flat):
    from coordinator_core import machine_resolver, temp_layout

    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: flat)
    monkeypatch.setattr(temp_layout, "fleet_temp_root", lambda: tmp_path / "_fleet")


def test_keep_paths_resolve_engine_mirrors_and_publish_clone(monkeypatch, tmp_path):
    repo = tmp_path / "work" / "proj"
    clone = tmp_path / "pub" / "proj-publish"
    repo.mkdir(parents=True)
    _patch_keep_inputs(monkeypatch, tmp_path, {"repos.proj": str(repo), "publish.mirrors.proj.path": str(clone)})
    paths = owner_mark.keep_paths()
    assert paths[owner_mark.KEEP_PUBLISH_ENGINE] == [tmp_path / "_fleet" / "claude-klabauter-publish-engine"]
    assert paths[owner_mark.KEEP_PUBLISH_CLONE] == [clone]
    assert len(paths[owner_mark.KEEP_CLEAN_MIRROR]) == 1
    assert paths[owner_mark.KEEP_CLEAN_MIRROR][0].parent.name == "clean-mirror"


def test_is_kept_member_inside_ancestor_and_unrelated(tmp_path):
    engine = tmp_path / "_fleet" / "claude-klabauter-publish-engine"
    keep = {"publish-engine": [engine], "publish-clone": [tmp_path / "clone"]}
    assert owner_mark.is_kept(engine, keep)
    assert owner_mark.is_kept(engine / "abc123", keep)
    assert owner_mark.is_kept(tmp_path / "_fleet", keep)
    assert owner_mark.is_kept(tmp_path / "clone", keep)
    assert not owner_mark.is_kept(tmp_path / "scratch" / "x", keep)
    assert not owner_mark.is_kept(tmp_path / "_fleet-other", keep)


def test_is_kept_fails_closed_when_keep_set_unresolvable(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(owner_mark, "keep_paths", boom)
    assert owner_mark.is_kept(tmp_path / "anything") is True


def test_may_reap_requires_dead_owner_past_ttl_and_not_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(owner_mark, "is_kept", lambda p, keep=None: False)
    monkeypatch.setattr(record, "pid_state", lambda pid: False)
    now = datetime.now(timezone.utc)
    d = tmp_path / "d"
    owner_mark.write_mark(d, "pid", 4242, ttl_hours=1)
    assert owner_mark.may_reap(d, now) is False  # inside its age floor
    assert owner_mark.may_reap(d, now + timedelta(hours=2)) is True
    owner_mark.write_mark(d, "pid", 4242)
    assert owner_mark.may_reap(d, now) is True  # no ttl: liveness alone decides
    monkeypatch.setattr(record, "pid_state", lambda pid: None)
    assert owner_mark.may_reap(d, now + timedelta(hours=2)) is False  # unknown keeps
    monkeypatch.setattr(record, "pid_state", lambda pid: False)
    monkeypatch.setattr(owner_mark, "is_kept", lambda p, keep=None: True)
    assert owner_mark.may_reap(d, now + timedelta(hours=2)) is False  # keep-set wins at any age
    assert owner_mark.may_reap(tmp_path / "unmarked") is False


@pytest.mark.parametrize("ttl", ["6", -1, True, "soon"])
def test_may_reap_keeps_a_dir_whose_ttl_is_present_but_unusable(monkeypatch, tmp_path, ttl):
    monkeypatch.setattr(owner_mark, "is_kept", lambda p, keep=None: False)
    monkeypatch.setattr(record, "pid_state", lambda pid: False)
    d = tmp_path / "d"
    owner_mark.write_mark(d, "pid", 4242)
    mark = owner_mark.read_mark(d)
    monkeypatch.setattr(owner_mark, "read_mark", lambda directory: {**mark, "ttl_hours": ttl})
    assert owner_mark.may_reap(d, datetime.now(timezone.utc) + timedelta(days=30)) is False

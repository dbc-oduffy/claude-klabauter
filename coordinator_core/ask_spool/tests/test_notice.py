"""Notice roundtrip, rename-before-read, and the drain trigger."""

from __future__ import annotations

import os
import time

from coordinator_core.ask_spool import notice


def _stale_minted_run(tmp_path, run_id="run-1"):
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE, record_dir

    d = record_dir(tmp_path, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_STAGE_FILE).write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    return d


def test_withdrawn_spool_entry_wins_over_stale_minted_stage(tmp_path):
    from coordinator_core.ask_spool import record

    _stale_minted_run(tmp_path)
    notice.append_notice(str(tmp_path), "s1", "abc", "run-1")
    open(record.entry_path(str(tmp_path), "abc", record.WITHDRAWN_SUFFIX), "w").close()
    text = notice.advisory_text(str(tmp_path), "s1")
    assert text == "Your spooled ask abc minted run run-1; it is now withdrawn, nothing to start."


def test_halted_record_wins_over_stale_minted_stage_and_a_live_minted_run_still_reads_minted(tmp_path):
    d = _stale_minted_run(tmp_path)
    notice.append_notice(str(tmp_path), "s1", "abc", "run-1")
    assert notice.advisory_text(str(tmp_path), "s1") == "Your spooled ask abc minted run run-1."
    (d / "halted.json").write_text('{"halted": "x"}', encoding="utf-8")
    notice.append_notice(str(tmp_path), "s1", "abc", "run-1")
    assert notice.advisory_text(str(tmp_path), "s1") == "Your spooled ask abc minted run run-1; it is now halted, nothing to start."


def test_roundtrip_text_and_consumed(tmp_path):
    notice.append_notice(str(tmp_path), "s1", "abc", "run-1")
    notice.append_notice(str(tmp_path), "s1", "def", "run-2")
    text = notice.advisory_text(str(tmp_path), "s1")
    assert text.splitlines() == [
        "Your spooled ask abc minted run run-1.",
        "Your spooled ask def minted run run-2.",
    ]
    assert notice.advisory_text(str(tmp_path), "s1") == ""
    assert os.listdir(notice.notices_dir(str(tmp_path))) == []


def test_other_session_untouched(tmp_path):
    notice.append_notice(str(tmp_path), "s2", "abc", "run-1")
    assert notice.advisory_text(str(tmp_path), "s1") == ""
    assert notice.advisory_text(str(tmp_path), "s2") != ""


def test_no_dir_or_root_is_empty(tmp_path):
    assert notice.advisory_text(str(tmp_path), "s1") == ""
    assert notice.advisory_text(None, "s1") == ""


def test_append_after_read_lands_in_fresh_file(tmp_path, monkeypatch):
    notice.append_notice(str(tmp_path), "s1", "a", "r1")
    real_open = open

    def open_then_append(path, *a, **k):
        if str(path).endswith(notice.READING_SUFFIX):
            notice.append_notice(str(tmp_path), "s1", "b", "r2")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", open_then_append)
    first = notice.consume_notices(str(tmp_path), "s1")
    monkeypatch.undo()
    assert [n["spool_id"] for n in first] == ["a"]
    assert [n["spool_id"] for n in notice.consume_notices(str(tmp_path), "s1")] == ["b"]


def test_malformed_line_skipped(tmp_path):
    notice.append_notice(str(tmp_path), "s1", "a", "r1")
    with open(notice.notice_path(str(tmp_path), "s1"), "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert len(notice.consume_notices(str(tmp_path), "s1")) == 1


def test_postuse_leg_surfaces_notice(tmp_path):
    from coordinator_core.hooks import postuse_advisory_dispatch as pad

    notice.append_notice(str(tmp_path), "s1", "abc", "run-1")
    assert pad._ask_spool_notice_sync("s1", tmp_path) == "Your spooled ask abc minted run run-1."
    assert pad._ask_spool_notice_sync("s1", tmp_path) == ""
    assert pad._ask_spool_notice_sync("s1", None) == ""


def _pending(tmp_path, name, mtime):
    path = os.path.join(str(tmp_path), ".coordinator-local", "ask-spool", name + ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{}")
    os.utime(path, (mtime, mtime))


def test_server_triggers_drain_once_per_repo(tmp_path, monkeypatch):
    from coordinator_core.warm import server

    calls = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: calls.append(root))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: fn(*a))
    state = server._DrainTrigger()
    state.maybe_drain(tmp_path)
    state.maybe_drain(tmp_path)
    assert calls == [tmp_path]


def test_server_redrains_when_entry_spooled_after_last_drain(tmp_path, monkeypatch):
    from coordinator_core.warm import server

    calls = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: calls.append(root))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: fn(*a))
    clock = [100.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    state = server._DrainTrigger()
    _pending(tmp_path, "old", 1.0)
    state.maybe_drain(tmp_path)
    state.maybe_drain(tmp_path)
    assert calls == [tmp_path]
    _pending(tmp_path, "late", 150.0)
    clock[0] = 200.0
    state.maybe_drain(tmp_path)
    assert calls == [tmp_path, tmp_path]
    state.maybe_drain(tmp_path)
    assert calls == [tmp_path, tmp_path]


def test_server_never_runs_two_drains_of_one_repo_concurrently(tmp_path, monkeypatch):
    from coordinator_core.warm import server

    calls = []
    held = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: calls.append(root))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: held.append((fn, a)))
    state = server._DrainTrigger()
    state.maybe_drain(tmp_path)
    _pending(tmp_path, "late", time.time() + 5)
    state.maybe_drain(tmp_path)
    assert len(held) == 1
    fn, args = held[0]
    fn(*args)
    assert calls == [tmp_path]
    state.maybe_drain(tmp_path)
    assert len(held) == 2


def test_coalesced_and_withdrawn_notices_render_their_lines(tmp_path):
    import json

    path = notice.notice_path(str(tmp_path), "s1")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"spool_id": "a", "status": "coalesced", "run_id": "r1"}) + "\n")
        fh.write(json.dumps({"spool_id": "b", "status": "withdrawn", "reason": "stale: sizing x is gone"}) + "\n")
    assert notice.advisory_text(str(tmp_path), "s1").splitlines() == [
        "Your spooled ask a coalesced into run r1.",
        "Your spooled ask b was withdrawn: stale: sizing x is gone",
    ]


def test_failed_notice_is_surfaced_not_dropped(tmp_path):
    import json

    path = notice.notice_path(str(tmp_path), "s1")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"spool_id": "abc", "status": "failed", "attempts": 1, "error": "boom"}) + "\n")
    assert notice.advisory_text(str(tmp_path), "s1") == "Your spooled ask abc could not be minted yet (attempt 1): boom"

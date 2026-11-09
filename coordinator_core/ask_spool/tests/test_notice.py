"""Notice roundtrip, rename-before-read, and the drain trigger."""

from __future__ import annotations

import os

from coordinator_core.ask_spool import notice


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


def test_server_triggers_drain_once_per_repo(tmp_path, monkeypatch):
    from coordinator_core.warm import server

    calls = []
    monkeypatch.setattr(server, "_run_ask_spool_drain", lambda root: calls.append(root))
    monkeypatch.setattr(server, "_spawn_background", lambda fn, *a: fn(*a))
    state = server._DrainTrigger()
    state.maybe_drain(tmp_path)
    state.maybe_drain(tmp_path)
    assert calls == [tmp_path]


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

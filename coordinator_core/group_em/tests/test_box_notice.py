"""Tests for coordinator_core.group_em.box_notice -- notice text and the spawn-free recipient reader."""

from __future__ import annotations

import json
import os

from coordinator_core.group_em import box_notice


def _row(directory, name, session_id, pid, label):
    (directory / f"{label}.json").write_text(
        json.dumps({"sessionId": session_id, "name": name, "pid": pid, "cwd": f"/fake/{label}"}),
        encoding="utf-8",
    )


def test_registry_recipients_exclude_holder_unnamed_and_dead(tmp_path, monkeypatch):
    monkeypatch.setattr(box_notice.session_registry, "pid_alive", lambda pid: pid != 999)
    _row(tmp_path, "holder", "sid-me", os.getpid(), "me")
    _row(tmp_path, "peer-a", "sid-a", os.getpid(), "a")
    _row(tmp_path, "", "sid-unnamed", os.getpid(), "unnamed")
    _row(tmp_path, "peer-dead", "sid-dead", 999, "dead")

    got = box_notice.recipients_from_registry("sid-me", directory=tmp_path)

    assert got == [{"name": "peer-a", "sessionId": "sid-a", "cwd": "/fake/a"}]


def test_registry_recipients_honour_skip_set(tmp_path):
    _row(tmp_path, "peer-a", "sid-a", os.getpid(), "a")

    assert box_notice.recipients_from_registry(
        "sid-me", frozenset({"sid-a"}), directory=tmp_path
    ) == []


def test_ended_notice_text_is_pinned():
    text = box_notice.render_notice(
        box_notice.NOTICE_ENDED, {"sessionId": "sid-me", "name": "coordinator-content-repo-9"}
    )

    assert text == (
        "Group EM notice, informational, no reply is wanted: coordinator-content-repo-9 (session sid-me) has "
        "stood down and the box has no Group EM now. Do not route to that address. The next "
        "/group-em entry announces its own."
    )


def test_unnamed_holder_notice_uses_session_id_and_asks_nothing():
    for kind in (box_notice.NOTICE_ENTERED, box_notice.NOTICE_ENDED):
        text = box_notice.render_notice(kind, {"sessionId": "sid-me", "name": None})
        assert "session sid-me" in text
        assert "?" not in text

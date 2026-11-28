"""box_send: verified append into a registered box inbox, and the per-session read."""

import json

from coordinator_core.group_em import box_registry, box_send
from pathlib import Path


def _setup(tmp_path):
    primary = tmp_path / "primary"
    second = tmp_path / "second"
    gdir = primary / "state" / "group-em"
    rec = box_registry.register(second, {"os": "linux"}, directory=gdir)
    return primary, second, gdir, rec["box_id"]


def test_send_lands_verified_line_and_reads_back(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    r = box_send.send(bid, "em-1", "hello", directory=gdir, now=5.0)
    assert r["sent"] is True
    lines = (second / "state/group-em/box/inbox.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec == {"ts": 5.0, "from_box": "local", "to_session": "em-1", "text": "hello"}
    assert box_send.read_inbox("em-1", home=second) == [rec]


def test_unknown_box_refused_and_nothing_written(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    r = box_send.send("box-nope", "em-1", "x", directory=gdir)
    assert r == {"sent": False, "reason": "unknown_box"}
    assert not (second / "state/group-em/box/inbox.jsonl").exists()


def test_empty_session_refused(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    assert box_send.send(bid, "", "x", directory=gdir)["reason"] == "bad_session"


def test_read_inbox_filters_by_session_and_skips_malformed(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    box_send.send(bid, "a", "1", directory=gdir)
    box_send.send(bid, "b", "2", directory=gdir)
    box_send.send(bid, "a", "3", directory=gdir)
    with open(box_send.inbox_path(second), "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert [m["text"] for m in box_send.read_inbox("a", home=second)] == ["1", "3"]
    assert [m["text"] for m in box_send.read_inbox("b", home=second)] == ["2"]
    assert box_send.read_inbox("c", home=second) == []


def test_from_box_is_the_sender_identity_and_text_survives_newlines(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    sender = box_registry.ensure_identity(primary)
    box_send.send(bid, "em-1", "a\nb", directory=gdir)
    (rec,) = box_send.read_inbox("em-1", home=second)
    assert rec["from_box"] == sender and rec["text"] == "a\nb"


def test_read_inbox_rewrites_remote_from_into_receiver_namespace(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    sender = box_registry.ensure_identity(primary)
    box_registry.ensure_identity(second)
    box_send.send(bid, "em-1", "hi", directory=gdir, from_session="s-9")
    (rec,) = box_send.read_inbox("em-1", home=second)
    assert rec["from_session"] == "s-9"
    assert rec["from"] == f"{sender}:s-9"


def test_read_inbox_local_sender_stays_bare(tmp_path: Path) -> None:
    home = tmp_path / "home"
    me = box_registry.ensure_identity(home)
    path = box_send.inbox_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"ts": 1, "from_box": me, "from_session": "s-1", "to_session": "t", "text": "x"})
        + "\n"
    )
    (rec,) = box_send.read_inbox("t", home=home)
    assert rec["from"] == "s-1"


def test_read_inbox_missing_file_is_empty(tmp_path: Path) -> None:
    assert box_send.read_inbox("a", home=tmp_path / "none") == []


def test_unwritable_inbox_reports_failure(tmp_path: Path) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    (second / "state/group-em/box").write_text("file blocks the dir")
    r = box_send.send(bid, "em-1", "x", directory=gdir)
    assert r["sent"] is False and r["reason"].startswith("write_failed")


def test_send_to_another_root_makes_no_session_handoff(tmp_path: Path, monkeypatch) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(primary))
    r = box_send.send(bid, "em-1", "x", directory=gdir)
    assert r["sent"] is True and "handoff" not in r
    assert not (second / "agent-pipe").exists() and not (primary / "agent-pipe").exists()


def test_send_to_this_box_also_appends_to_the_session_mailbox(tmp_path: Path, monkeypatch) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(second))
    r = box_send.send(bid, "em-1", "ROLL CALL ask", directory=gdir)
    assert r["sent"] is True and r["handoff"] == "mailbox"
    (line,) = (second / "agent-pipe" / "mailboxes" / "em-1.jsonl").read_text().splitlines()
    msg = json.loads(line)
    assert msg["to_session"] == "em-1" and msg["content"] == "ROLL CALL ask"
    assert len(box_send.read_inbox("em-1", home=second)) == 1


def test_send_to_this_box_routes_a_non_claude_binding_through_the_bridge(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.agent_pipe import bridge, roster

    primary, second, gdir, bid = _setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(second))
    monkeypatch.setattr(roster, "resolve", lambda name: {"kind": "codex"})
    routed = []

    def route(msg):
        routed.append(msg)
        return bridge.Delivery("handed-off", None, msg)

    monkeypatch.setattr(bridge, "route", route)
    r = box_send.send(bid, "cx-1", "ask", directory=gdir)
    assert r["handoff"] == "bridge:handed-off"
    assert [m.to_identity for m in routed] == ["cx-1"] and routed[0].content == "ask"
    assert not (second / "agent-pipe" / "mailboxes").exists()


def test_failed_handoff_is_reported_and_the_inbox_line_stays(tmp_path: Path, monkeypatch) -> None:
    primary, second, gdir, bid = _setup(tmp_path)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(second))
    r = box_send.send(bid, "a/b", "x", directory=gdir)
    assert r["sent"] is True and r["handoff"].startswith("failed:")
    assert len(box_send.read_inbox("a/b", home=second)) == 1

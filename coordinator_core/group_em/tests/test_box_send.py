"""box_send: verified append into a registered box inbox, and the per-session read."""

import json

from coordinator_core.group_em import box_registry, box_send


def _setup(tmp_path):
    primary = tmp_path / "primary"
    second = tmp_path / "second"
    gdir = primary / "state" / "group-em"
    rec = box_registry.register(second, {"os": "linux"}, directory=gdir)
    return primary, second, gdir, rec["box_id"]


def test_send_lands_verified_line_and_reads_back(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    r = box_send.send(bid, "em-1", "hello", directory=gdir, now=5.0)
    assert r["sent"] is True
    lines = (second / "state/group-em/box/inbox.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec == {"ts": 5.0, "from_box": "local", "to_session": "em-1", "text": "hello"}
    assert box_send.read_inbox("em-1", home=second) == [rec]


def test_unknown_box_refused_and_nothing_written(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    r = box_send.send("box-nope", "em-1", "x", directory=gdir)
    assert r == {"sent": False, "reason": "unknown_box"}
    assert not (second / "state/group-em/box/inbox.jsonl").exists()


def test_empty_session_refused(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    assert box_send.send(bid, "", "x", directory=gdir)["reason"] == "bad_session"


def test_read_inbox_filters_by_session_and_skips_malformed(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    box_send.send(bid, "a", "1", directory=gdir)
    box_send.send(bid, "b", "2", directory=gdir)
    box_send.send(bid, "a", "3", directory=gdir)
    with open(box_send.inbox_path(second), "a", encoding="utf-8") as fh:
        fh.write("not json\n")
    assert [m["text"] for m in box_send.read_inbox("a", home=second)] == ["1", "3"]
    assert [m["text"] for m in box_send.read_inbox("b", home=second)] == ["2"]
    assert box_send.read_inbox("c", home=second) == []


def test_from_box_is_the_sender_identity_and_text_survives_newlines(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    sender = box_registry.ensure_identity(primary)
    box_send.send(bid, "em-1", "a\nb", directory=gdir)
    (rec,) = box_send.read_inbox("em-1", home=second)
    assert rec["from_box"] == sender and rec["text"] == "a\nb"


def test_read_inbox_rewrites_remote_from_into_receiver_namespace(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    sender = box_registry.ensure_identity(primary)
    box_registry.ensure_identity(second)
    box_send.send(bid, "em-1", "hi", directory=gdir, from_session="s-9")
    (rec,) = box_send.read_inbox("em-1", home=second)
    assert rec["from_session"] == "s-9"
    assert rec["from"] == f"{sender}:s-9"


def test_read_inbox_local_sender_stays_bare(tmp_path):
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


def test_read_inbox_missing_file_is_empty(tmp_path):
    assert box_send.read_inbox("a", home=tmp_path / "none") == []


def test_unwritable_inbox_reports_failure(tmp_path):
    primary, second, gdir, bid = _setup(tmp_path)
    (second / "state/group-em/box").write_text("file blocks the dir")
    r = box_send.send(bid, "em-1", "x", directory=gdir)
    assert r["sent"] is False and r["reason"].startswith("write_failed")

import logging

from coordinator_core.ops.dispatch_emit import run_hold
from coordinator_core.ops.dispatch_emit.run_hold import HeldRun


def _hold():
    return HeldRun("sess-1", "pm turn 9", "example-retrieval-repo tool gap closes", "2026-11-23T23:00:00Z")


def test_round_trip(tmp_path):
    path = run_hold.write_hold(tmp_path, _hold())
    assert path == tmp_path / "held.json"
    assert run_hold.read_hold(tmp_path) == _hold()


def test_absent_reads_none(tmp_path):
    assert run_hold.read_hold(tmp_path) is None


def test_clear_removes_and_reports(tmp_path):
    run_hold.write_hold(tmp_path, _hold())
    assert run_hold.clear_hold(tmp_path) is True
    assert not (tmp_path / "held.json").exists()
    assert run_hold.read_hold(tmp_path) is None
    assert run_hold.clear_hold(tmp_path) is False


def test_malformed_reads_none_with_advisory(tmp_path, caplog):
    (tmp_path / "held.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert run_hold.read_hold(tmp_path) is None
    assert "malformed" in caplog.text


def test_wrong_shape_and_missing_holder_read_none(tmp_path):
    held = tmp_path / "held.json"
    held.write_text("[]", encoding="utf-8")
    assert run_hold.read_hold(tmp_path) is None
    held.write_text('{"holder_session": 3}', encoding="utf-8")
    assert run_hold.read_hold(tmp_path) is None
    held.write_text('{"pm_turn_ref": "x"}', encoding="utf-8")
    assert run_hold.read_hold(tmp_path) is None


def test_held_reply_names_holder_release_and_continue():
    text = run_hold.held_reply("ask-1", _hold())
    assert "sess-1" in text and "pm turn 9" in text
    assert "example-retrieval-repo tool gap closes" in text
    assert f'args.{run_hold.CONTINUE_KEY}: "ask-1"' in text
    assert run_hold.CONTINUE_KEY == "continue_held"

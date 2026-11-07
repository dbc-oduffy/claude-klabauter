"""run_write_prints record/read: merge, absent-as-None, latest-wins, unknown-as-None."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import kept_work, run_write_prints
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "runP"


def test_read_is_none_when_nothing_recorded(tmp_path):
    assert run_write_prints.read(tmp_path, RUN) is None


def test_record_prints_present_and_absent_paths(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")

    run_write_prints.record(tmp_path, RUN, ["a.txt", "gone.txt"])

    assert run_write_prints.read(tmp_path, RUN) == {
        "a.txt": kept_work.blob_sha(b"one\n"),
        "gone.txt": None,
    }
    assert (record_dir(tmp_path, RUN) / "write_prints.json").is_file()


def test_record_merges_and_latest_write_wins(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    (tmp_path / "b.txt").write_bytes(b"bee\n")
    run_write_prints.record(tmp_path, RUN, ["a.txt", "b.txt"])

    (tmp_path / "a.txt").write_bytes(b"two\n")
    run_write_prints.record(tmp_path, RUN, ["./a.txt"])

    assert run_write_prints.read(tmp_path, RUN) == {
        "a.txt": kept_work.blob_sha(b"two\n"),
        "b.txt": kept_work.blob_sha(b"bee\n"),
    }


def test_unreadable_record_reads_none_and_is_replaced(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    run_write_prints.record(tmp_path, RUN, ["a.txt"])
    (record_dir(tmp_path, RUN) / "write_prints.json").write_text("{not json", encoding="utf-8")
    assert run_write_prints.read(tmp_path, RUN) is None

    run_write_prints.record(tmp_path, RUN, ["a.txt"])

    assert run_write_prints.read(tmp_path, RUN) == {"a.txt": kept_work.blob_sha(b"one\n")}


def test_prints_are_per_run(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    run_write_prints.record(tmp_path, RUN, ["a.txt"])
    assert run_write_prints.read(tmp_path, "runQ") is None


def _live(monkeypatch, *records):
    from coordinator_core.ops.dispatch_emit import run_token

    monkeypatch.setattr(run_token, "live_records", lambda root: iter(records))


def test_record_write_prints_a_subagent_write_for_the_sessions_one_live_run(tmp_path, monkeypatch):
    _live(monkeypatch, {"run_id": RUN, "session_id": "S1"})
    (tmp_path / "a.txt").write_bytes(b"one\n")

    got = run_write_prints.record_write(tmp_path, "S1", "agent1", str(tmp_path / "a.txt"))

    assert got == RUN
    assert run_write_prints.read(tmp_path, RUN) == {"a.txt": kept_work.blob_sha(b"one\n")}


def test_record_write_records_nothing_without_a_resolvable_run(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"one\n")
    for records, session, agent in (
        ((), "S1", "agent1"),
        (({"run_id": RUN, "session_id": "S2"},), "S1", "agent1"),
        (({"run_id": RUN, "session_id": "S1"}, {"run_id": "runQ", "session_id": "S1"}), "S1", "agent1"),
        (({"run_id": RUN, "session_id": "S1"},), "S1", ""),
        (({"run_id": RUN, "session_id": "S1"},), "", "agent1"),
    ):
        _live(monkeypatch, *records)
        assert run_write_prints.record_write(tmp_path, session, agent, "a.txt") is None
    assert run_write_prints.read(tmp_path, RUN) is None
    assert run_write_prints.read(tmp_path, "runQ") is None


def test_record_write_refuses_a_path_outside_the_tree(tmp_path, monkeypatch):
    _live(monkeypatch, {"run_id": RUN, "session_id": "S1"})
    outside = tmp_path.parent / "elsewhere.txt"
    assert run_write_prints.record_write(tmp_path, "S1", "agent1", str(outside)) is None
    assert run_write_prints.read(tmp_path, RUN) is None


def _edit(old, new):
    return {"old_string": old, "new_string": new}


def test_edit_after_a_peer_edit_between_run_writes_drops_the_print(tmp_path, monkeypatch):
    _live(monkeypatch, {"run_id": RUN, "session_id": "S1"})
    target = tmp_path / "a.txt"
    target.write_bytes(b"one\n")
    run_write_prints.record_write(tmp_path, "S1", "agent1", "a.txt")
    target.write_bytes(b"one\npeer\n")  # peer edit
    target.write_bytes(b"ONE\npeer\n")  # run's second edit: one -> ONE

    run_write_prints.record_write(tmp_path, "S1", "agent1", "a.txt", _edit("one", "ONE"))

    assert "a.txt" not in (run_write_prints.read(tmp_path, RUN) or {})


def test_edit_continuing_from_the_last_print_updates_it(tmp_path, monkeypatch):
    _live(monkeypatch, {"run_id": RUN, "session_id": "S1"})
    target = tmp_path / "a.txt"
    target.write_bytes(b"one\n")
    run_write_prints.record_write(tmp_path, "S1", "agent1", "a.txt")
    target.write_bytes(b"ONE\n")

    run_write_prints.record_write(tmp_path, "S1", "agent1", "a.txt", _edit("one", "ONE"))

    assert run_write_prints.read(tmp_path, RUN) == {"a.txt": kept_work.blob_sha(b"ONE\n")}

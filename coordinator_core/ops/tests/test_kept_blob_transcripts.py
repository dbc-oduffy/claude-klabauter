"""collect_edits: successful Write/Edit/MultiEdit calls on repo paths across every transcript."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import kept_blob_transcripts as kbt
from coordinator_core.ops.kept_blob_transcripts import collect_edits
from coordinator_core.ops.tests._kept_blob_fixture import write_transcript

REPO = Path("D:/w/proj")  # abs-path-ok: synthetic root, never touched on disk
REL = "pkg/mod.py"


def _use(tid, name, fp, ts, **extra):
    return ("use", tid, name, {"file_path": fp, **extra}, ts)


def test_all_three_tools_collected(tmp_path):
    write_transcript(tmp_path / "s" / "a.jsonl", [
        _use("1", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:01Z", content="x"),
        _use("2", "Edit", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:02Z", old_string="x", new_string="y"),
        _use("3", "MultiEdit", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:03Z", edits=[]),
        _use("4", "Read", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:04Z"),
    ])
    got = collect_edits(REPO, [REL], [tmp_path])
    assert [e.name for e in got[REL]] == ["Write", "Edit", "MultiEdit"]
    assert got[REL][0].ts.utcoffset().total_seconds() == 0
    assert got[REL][0].src == "s/a.jsonl"


def test_errored_result_drops_call(tmp_path):
    write_transcript(tmp_path / "a.jsonl", [
        _use("bad", "Edit", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:01Z"),
        _use("ok", "Edit", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:02Z"),
        ("result", "bad", True),
        ("result", "ok", False),
    ])
    assert [e.tool_use_id for e in collect_edits(REPO, [REL], [tmp_path])[REL]] == ["ok"]


def test_duplicate_id_keeps_first_timestamped(tmp_path):
    write_transcript(tmp_path / "a.jsonl", [
        ("raw", {"message": {"content": [{"type": "tool_use", "id": "d", "name": "Write",
                                          "input": {"file_path": "D:/w/proj/pkg/mod.py", "content": "none"}}]}}),
        _use("d", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:05Z", content="first"),
        _use("d", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:09Z", content="late"),
        _use("u", "Write", "D:/w/proj/pkg/mod.py", "not-a-time", content="unorderable"),
    ])
    got = collect_edits(REPO, [REL], [tmp_path])[REL]
    assert len(got) == 1
    assert got[0].input["content"] == "first"


def test_ordered_by_timestamp_across_sibling_sessions(tmp_path):
    write_transcript(tmp_path / "s1" / "a.jsonl", [
        _use("a1", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:10Z"),
        _use("a2", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:30Z"),
    ])
    write_transcript(tmp_path / "s2" / "b.jsonl", [
        _use("b1", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:20Z"),
    ])
    got = collect_edits(REPO, [REL], [tmp_path])[REL]
    assert [e.tool_use_id for e in got] == ["a1", "b1", "a2"]
    assert [e.src for e in got] == ["s1/a.jsonl", "s2/b.jsonl", "s1/a.jsonl"]


def test_same_rel_under_other_repo_not_collected(tmp_path):
    write_transcript(tmp_path / "a.jsonl", [
        _use("other", "Write", "D:/w/other/pkg/mod.py", "2026-01-01T00:00:01Z"),
        _use("nested", "Write", "D:/w/proj/sub/pkg/mod.py", "2026-01-01T00:00:02Z"),
        _use("mine", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:03Z"),
    ])
    assert [e.tool_use_id for e in collect_edits(REPO, [REL], [tmp_path])[REL]] == ["mine"]


def test_path_forms_each_match_their_repo(tmp_path):
    write_transcript(tmp_path / "a.jsonl", [
        _use("win", "Write", "D:\\w\\proj\\pkg\\mod.py", "2026-01-01T00:00:01Z"),  # abs-path-ok: fixture
        _use("low", "Write", "d:/w/proj/pkg/mod.py", "2026-01-01T00:00:02Z"),
        _use("bash", "Write", "/d/w/proj/pkg/mod.py", "2026-01-01T00:00:03Z"),
    ])
    assert [e.tool_use_id for e in collect_edits(REPO, [REL], [tmp_path])[REL]] == ["win", "low", "bash"]
    write_transcript(tmp_path / "p" / "b.jsonl", [_use("posix", "Write", "/home/u/proj/pkg/mod.py", "2026-01-01T00:00:01Z")])  # abs-path-ok: fixture
    got = collect_edits(Path("/home/u/proj"), [REL], [tmp_path / "p"])
    assert [e.tool_use_id for e in got[REL]] == ["posix"]


def test_posix_root_matches_on_a_path_boundary_only(tmp_path):
    write_transcript(tmp_path / "b.jsonl", [
        _use("sibling", "Write", "/home/u/projX/pkg/mod.py", "2026-01-01T00:00:01Z"),  # abs-path-ok: fixture
        _use("mine", "Write", "/home/u/proj/pkg/mod.py", "2026-01-01T00:00:02Z"),  # abs-path-ok: fixture
    ])
    got = collect_edits(Path("/home/u/proj"), [REL], [tmp_path])  # abs-path-ok: fixture
    assert [e.tool_use_id for e in got[REL]] == ["mine"]


def test_file_without_needle_never_parsed(tmp_path, monkeypatch):
    write_transcript(tmp_path / "none.jsonl", [_use("n", "Write", "D:/w/proj/pkg/other.py", "2026-01-01T00:00:01Z")])
    write_transcript(tmp_path / "has.jsonl", [_use("h", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:01Z")])
    parsed = []
    real = json.loads
    monkeypatch.setattr(kbt.json, "loads", lambda s, *a, **k: (parsed.append(s), real(s, *a, **k))[1])
    got = collect_edits(REPO, [REL], [tmp_path / "none.jsonl", tmp_path / "has.jsonl"])
    assert [e.tool_use_id for e in got[REL]] == ["h"]
    assert parsed and all(b"mod.py" in p for p in parsed)


def test_every_rel_is_a_key_and_malformed_lines_ignored(tmp_path):
    p = write_transcript(tmp_path / "a.jsonl", [_use("1", "Write", "D:/w/proj/pkg/mod.py", "2026-01-01T00:00:01Z")])
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"tool_use" mod.py broken\n')
    got = collect_edits(REPO, [REL, "pkg/none.py"], [tmp_path])
    assert len(got[REL]) == 1 and got["pkg/none.py"] == []

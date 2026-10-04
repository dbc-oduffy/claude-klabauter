"""Contract of `coordinator_core.orient_brief`: argv/exit codes, envelope shape, read-only."""
from __future__ import annotations

import json
import os

import pytest

from coordinator_core import orient_brief
from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.contract.decision_object.reader_result import ReaderResult
from coordinator_core.orient_brief import CADENCES, _branch, _health, _work, brief, main


def _envelope(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out)


def _assert_envelope(obj: dict) -> None:
    assert set(obj) == set(ENVELOPE_KEYS)
    assert isinstance(obj["directives"], list)
    assert isinstance(obj["judgment_points"], list)
    assert isinstance(obj["narration"], str) and obj["narration"]
    assert isinstance(obj["next_move"], str) and obj["next_move"]


def test_cadences_are_the_closed_set():
    assert CADENCES == ("session", "day", "week")


@pytest.mark.parametrize("cadence", CADENCES)
def test_brief_returns_conformant_envelope(cadence, tmp_path):
    _assert_envelope(brief(cadence, repo_root=tmp_path))


def test_brief_rejects_unknown_cadence(tmp_path):
    with pytest.raises(ValueError):
        brief("bogus", repo_root=tmp_path)


@pytest.mark.parametrize("family", [_work, _branch, _health])
@pytest.mark.parametrize("cadence", CADENCES)
def test_family_protocol(family, cadence, tmp_path):
    assert isinstance(family.collect(cadence, repo_root=tmp_path), ReaderResult)


@pytest.mark.parametrize("cadence", CADENCES)
def test_main_success_exit_0(cadence, capsys, monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["brief", "--cadence", cadence]) == 0
    _assert_envelope(_envelope(capsys))


def test_main_equals_form(capsys, tmp_path):
    assert main(["brief", "--cadence=day", f"--target-root={tmp_path}"]) == 0
    _assert_envelope(_envelope(capsys))


@pytest.mark.parametrize("argv", [
    [],
    ["brief"],
    ["brief", "--cadence", "bogus"],
    ["brief", "--cadence"],
    ["brief", "--cadence", "day", "--extra"],
    ["nonsense"],
])
def test_usage_errors_exit_2_with_envelope(argv, capsys):
    assert main(argv) == 2
    captured = capsys.readouterr()
    _assert_envelope(json.loads(captured.out))
    assert "usage:" in captured.err


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["brief", "--help"]])
def test_help_exit_0_is_one_plain_usage_line(argv, capsys):
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: orient-assemble brief --cadence")
    assert out.count(chr(10)) == 1


def test_bad_target_root_exits_2_without_envelope(capsys, tmp_path):
    assert main(["brief", "--cadence", "day", "--target-root", str(tmp_path / "absent")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "is not a directory" in captured.err


def test_outside_a_git_tree_exits_2_without_envelope(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(orient_brief._repo_root_mod, "show_toplevel", lambda cwd: None)
    assert main(["brief", "--cadence", "day"]) == 2
    assert capsys.readouterr().out == ""


def test_target_root_scopes_the_brief(capsys, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(_work, "collect", lambda c, *, repo_root: seen.append(repo_root) or ReaderResult())
    assert main(["brief", "--cadence", "day", "--target-root", str(tmp_path)]) == 0
    assert seen == [tmp_path.resolve()]


def test_raising_family_is_dropped_and_named(capsys, monkeypatch, tmp_path):
    def boom(cadence, *, repo_root):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(_work, "collect", boom)
    assert main(["brief", "--cadence", "day", "--target-root", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    _assert_envelope(json.loads(captured.out))
    assert "reader work failed (RuntimeError: probe exploded)" in captured.err


def test_envelope_artifact_narration_and_next_move(tmp_path, monkeypatch):
    for fam in (_work, _health, orient_brief._branch):
        monkeypatch.setattr(fam, "collect", lambda c, *, repo_root: ReaderResult())
    env = brief("week", repo_root=tmp_path)
    assert env["artifact"] == {"cadence": "week"}
    assert env["narration"] == (
        "orient-assemble brief --cadence week: 0 directive(s), 0 judgment point(s) "
        "asked across 3 reader families (0 found total)."
    )
    assert env["next_move"] == "No findings from any reader family this pass."
    monkeypatch.setattr(_work, "collect", lambda c, *, repo_root: ReaderResult(directives=[{"id": "w"}]))
    assert brief("week", repo_root=tmp_path)["next_move"] == "Review directives[] and judgment_points[] below."


def test_family_entries_are_merged_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(_work, "collect", lambda c, *, repo_root: ReaderResult(directives=[{"id": "w"}]))
    monkeypatch.setattr(_health, "collect", lambda c, *, repo_root: ReaderResult(directives=[{"id": "h"}]))
    env = brief("day", repo_root=tmp_path)
    assert [d["id"] for d in env["directives"]] == ["w", "h"]


def _snapshot(root):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            snap[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size)
    return snap


@pytest.mark.parametrize("cadence", CADENCES)
def test_brief_is_read_only(cadence, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "h.md").write_text("---\nstatus: active\n---\n")
    (tmp_path / "untracked.txt").write_text("x")
    before = _snapshot(tmp_path)
    brief(cadence, repo_root=tmp_path)
    assert _snapshot(tmp_path) == before


def test_module_entry_is_the_same_main():
    from coordinator_core.orient_brief import __main__ as entry

    assert entry.main is orient_brief.main


def test_session_brief_stays_under_the_context_flood_bound():
    """REQ-C12: the live session brief serializes under 30000 bytes."""
    env = brief("session")
    assert len(json.dumps(env, indent=2, sort_keys=True).encode("utf-8")) < 30000

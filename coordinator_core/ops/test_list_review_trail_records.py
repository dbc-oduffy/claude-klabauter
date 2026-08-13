"""Tests for coordinator_core.ops.list_review_trail_records.

Golden oracle snapshotted 2026-07-16 against the live repo state
(Port of: list-review-trail-records.sh, coordinator-claude b5a4192c, 2026-07-20):

    no args (real repo state/archive)                         -> 469 lines / 0
    --date-prefix 2026-05-21 (real match)                      -> N lines, sorted / 0
    --date-prefix 1999-01-01 (real, no match)                  -> empty / 0
    --date-prefix bad-date (malformed)                         -> error / 1
    --foo (unknown option)                                     -> error / 1
    cwd outside any git repo, no COORDINATOR_ROOT               -> error / 1
    COORDINATOR_ROOT pointing at empty dir tree (absent dirs)   -> empty / 0
    COORDINATOR_ROOT="<root>/state" vs COORDINATOR_ROOT="<root>" -> same result / 0
    --print0                                                    -> NUL-separated
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from coordinator_core.ops import list_review_trail_records as lrtr


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# _resolve_state_root — COORDINATOR_ROOT override branching
# ---------------------------------------------------------------------------


def test_coordinator_root_state_suffix_used_verbatim(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("COORDINATOR_ROOT", str(state_dir))
    assert lrtr._resolve_state_root() == str(state_dir)


def test_coordinator_root_repo_root_gets_state_appended(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path))
    assert lrtr._resolve_state_root() == str(tmp_path) + "/state"


def test_no_override_not_a_git_repo_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.setattr(lrtr, "_git_root", lambda: None)
    assert lrtr._resolve_state_root() is None


# ---------------------------------------------------------------------------
# _collect — absent-dir-safe, recursive, symlink-following
# ---------------------------------------------------------------------------


def test_collect_absent_dir_returns_empty(tmp_path):
    assert lrtr._collect(str(tmp_path / "nope")) == []


def test_collect_finds_nested_json(tmp_path):
    _touch(tmp_path / "week-2026-05-25" / "2026-05-19-foo.json")
    _touch(tmp_path / "2026-05-20-bar.json")
    _touch(tmp_path / "not-json.txt")
    got = sorted(lrtr._collect(str(tmp_path)))
    assert got == [
        ("2026-05-19-foo.json", str(tmp_path / "week-2026-05-25" / "2026-05-19-foo.json")),
        ("2026-05-20-bar.json", str(tmp_path / "2026-05-20-bar.json")),
    ]


# ---------------------------------------------------------------------------
# main() — CLI behavior, byte-parity with oracle
# ---------------------------------------------------------------------------


def _run(monkeypatch, capsys, argv, coordinator_root):
    monkeypatch.setenv("COORDINATOR_ROOT", coordinator_root)
    rc = lrtr.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_empty_result_when_dirs_absent(monkeypatch, capsys, tmp_path):
    rc, out, err = _run(monkeypatch, capsys, [], str(tmp_path))
    assert rc == 0
    assert out == ""
    assert err == ""


def test_sorted_union_of_live_and_archive_by_basename(monkeypatch, capsys, tmp_path):
    # Oracle rationale (module docstring): sort by basename, not full path —
    # an archive record whose basename predates a live one must still sort
    # first even though its full path (nested under a week-subdir) would
    # otherwise sort later.
    live = tmp_path / "state" / "review-trail" / "2026-05-20-bbbbbbbb.json"
    archived = (
        tmp_path
        / "archive"
        / "review-trail"
        / "week-2026-05-25"
        / "2026-05-19-aaaaaaaa.json"
    )
    _touch(live)
    _touch(archived)
    rc, out, err = _run(monkeypatch, capsys, [], str(tmp_path / "state"))
    assert rc == 0
    assert err == ""
    lines = out.splitlines()
    assert lines == [str(archived), str(live)]


def test_date_prefix_filters_to_matching_basenames(monkeypatch, capsys, tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")
    _touch(tmp_path / "state" / "review-trail" / "2026-05-21-bbbb.json")
    rc, out, err = _run(
        monkeypatch, capsys, ["--date-prefix", "2026-05-21"], str(tmp_path / "state")
    )
    assert rc == 0
    assert out.splitlines() == [str(tmp_path / "state" / "review-trail" / "2026-05-21-bbbb.json")]


def test_date_prefix_no_match_is_empty_not_error(monkeypatch, capsys, tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")
    rc, out, err = _run(
        monkeypatch, capsys, ["--date-prefix", "1999-01-01"], str(tmp_path / "state")
    )
    assert rc == 0
    assert out == ""
    assert err == ""


def test_bad_date_prefix_is_error(monkeypatch, capsys, tmp_path):
    rc, out, err = _run(
        monkeypatch, capsys, ["--date-prefix", "bad-date"], str(tmp_path / "state")
    )
    assert rc == 1
    assert "must be YYYY-MM-DD" in err


def test_unknown_option_is_error(monkeypatch, capsys, tmp_path):
    rc, out, err = _run(monkeypatch, capsys, ["--foo"], str(tmp_path / "state"))
    assert rc == 1
    assert "unknown option: --foo" in err


def test_not_a_git_repo_no_override_is_error(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.setattr(lrtr, "_git_root", lambda: None)
    rc = lrtr.main([])
    out = capsys.readouterr()
    assert rc == 1
    assert "cannot resolve state/review-trail/" in out.err


def test_print0_null_separated(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json"
    _touch(p)
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path / "state"))
    rc = lrtr.main(["--print0"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == str(p) + "\0"


def test_coordinator_root_with_and_without_state_suffix_agree(monkeypatch, capsys, tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")

    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path / "state"))
    rc1 = lrtr.main([])
    out1 = capsys.readouterr().out

    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path))
    rc2 = lrtr.main([])
    out2 = capsys.readouterr().out

    assert rc1 == rc2 == 0
    assert out1 == out2

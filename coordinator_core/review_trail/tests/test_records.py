"""Tests for coordinator_core.review_trail.records.

Moved code arrives with its own pin (C2b, state/dispatch-briefs/2026-08-29-
the-gravestoned-review-trail-surface-is-deleted/C2b.md) — pins ``list_paths``'
state-root resolution and date-prefix filter, the same behavior
``coordinator_core.ops.test_list_review_trail_records`` already pins against
the doomed module's own ``main()`` CLI, over the module this content actually
lives in now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.review_trail import records


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


# ---------------------------------------------------------------------------
# _resolve_state_root — COORDINATOR_ROOT override branching
# ---------------------------------------------------------------------------


def test_coordinator_root_state_suffix_used_verbatim(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("COORDINATOR_ROOT", str(state_dir))
    assert records._resolve_state_root() == str(state_dir)


def test_coordinator_root_repo_root_gets_state_appended(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path))
    assert records._resolve_state_root() == str(tmp_path) + "/state"


def test_no_override_not_a_git_repo_returns_none(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.setattr(records, "_git_root", lambda: None)
    assert records._resolve_state_root() is None


def test_explicit_override_param_takes_precedence_over_env(monkeypatch, tmp_path):
    # Warm-server callers must pass state_root_override rather than staging
    # COORDINATOR_ROOT into os.environ (module docstring's precedence-1
    # rationale) — an explicit override wins even when the env var disagrees.
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path / "env-root"))
    explicit = str(tmp_path / "state")
    assert records._resolve_state_root(explicit) == explicit


# ---------------------------------------------------------------------------
# _collect — absent-dir-safe, recursive, symlink-following
# ---------------------------------------------------------------------------


def test_collect_absent_dir_returns_empty(tmp_path):
    assert records._collect(str(tmp_path / "nope")) == []


def test_collect_finds_nested_json(tmp_path):
    _touch(tmp_path / "week-2026-05-25" / "2026-05-19-foo.json")
    _touch(tmp_path / "2026-05-20-bar.json")
    _touch(tmp_path / "not-json.txt")
    got = sorted(records._collect(str(tmp_path)))
    assert got == [
        ("2026-05-19-foo.json", str(tmp_path / "week-2026-05-25" / "2026-05-19-foo.json")),
        ("2026-05-20-bar.json", str(tmp_path / "2026-05-20-bar.json")),
    ]


# ---------------------------------------------------------------------------
# list_paths — programmatic API, oracle-parity with the retiring CLI's
# non---print0 success path
# ---------------------------------------------------------------------------


def test_list_paths_sorted_union_of_live_and_archive_by_basename(tmp_path):
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
    got = records.list_paths(state_root_override=str(tmp_path / "state"))
    assert got == [str(archived), str(live)]


def test_list_paths_date_prefix_filters_to_matching_basenames(tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")
    _touch(tmp_path / "state" / "review-trail" / "2026-05-21-bbbb.json")
    got = records.list_paths(
        date_prefix="2026-05-21", state_root_override=str(tmp_path / "state")
    )
    assert got == [str(tmp_path / "state" / "review-trail" / "2026-05-21-bbbb.json")]


def test_list_paths_date_prefix_no_match_is_empty_not_error(tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")
    got = records.list_paths(
        date_prefix="1999-01-01", state_root_override=str(tmp_path / "state")
    )
    assert got == []


def test_list_paths_bad_date_prefix_raises(tmp_path):
    with pytest.raises(records.ReviewTrailListError, match="must be YYYY-MM-DD"):
        records.list_paths(
            date_prefix="bad-date", state_root_override=str(tmp_path / "state")
        )


def test_list_paths_unresolvable_state_root_raises(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.setattr(records, "_git_root", lambda: None)
    with pytest.raises(records.ReviewTrailListError, match="cannot resolve state/review-trail/"):
        records.list_paths()


def test_list_paths_with_and_without_state_suffix_agree(tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")

    got_with_suffix = records.list_paths(state_root_override=str(tmp_path / "state"))
    got_without_suffix = records.list_paths(state_root_override=str(tmp_path))

    assert got_with_suffix == got_without_suffix

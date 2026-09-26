
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.review_trail import records


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


# _resolve_state_root — COORDINATOR_ROOT override branching


def test_coordinator_root_state_suffix_used_verbatim(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("COORDINATOR_ROOT", str(state_dir))
    assert records._resolve_state_root() == str(state_dir)


def test_coordinator_root_repo_root_gets_state_appended(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path))
    assert records._resolve_state_root() == str(tmp_path) + "/state"


def test_no_override_at_all_returns_none(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    assert records._resolve_state_root() is None


def test_explicit_override_param_takes_precedence_over_env(monkeypatch, tmp_path):
    # COORDINATOR_ROOT into os.environ (module docstring's precedence-1
    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path / "env-root"))
    explicit = str(tmp_path / "state")
    assert records._resolve_state_root(explicit) == explicit


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


def test_list_paths_unresolvable_state_root_raises(monkeypatch):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    with pytest.raises(records.ReviewTrailListError, match="cannot resolve state/review-trail/"):
        records.list_paths()


def test_list_paths_with_and_without_state_suffix_agree(tmp_path):
    _touch(tmp_path / "state" / "review-trail" / "2026-05-20-aaaa.json")

    got_with_suffix = records.list_paths(state_root_override=str(tmp_path / "state"))
    got_without_suffix = records.list_paths(state_root_override=str(tmp_path))

    assert got_with_suffix == got_without_suffix

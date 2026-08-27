"""Tests for coordinator_core.bash_guards.scope_orphan_census.

Every row runs against a real, isolated `tmp_path` git repo -- never the
Claude-klabauter checkout, whose live `.git/coordinator-sessions/` logs are
exactly the moving-target corpus this module exists to make re-runnable
rather than snapshot. See scope_orphan_census.py's module docstring for the
cause taxonomy asserted here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import scope_orphan_census as census_mod

# Spawns a real external process (git, for repo fixture setup and tracked-at-
# HEAD checks); runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _write_log(repo: Path, session_id: str, lines: list) -> Path:
    session_dir = repo / ".git" / "coordinator-sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    log_path = session_dir / "scope-warnings.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _orphan_line(ts: str, session_id: str, path: str) -> str:
    return f"{ts} | {session_id} | foreign-staged | {path} | owner:orphan | pending-resolution"


def _named_owner_line(ts: str, session_id: str, path: str, owner_session: str) -> str:
    return (
        f"{ts} | {session_id} | foreign-staged | {path} | "
        f"owner:session {owner_session} | pending-resolution"
    )


# --- log parsing / iteration -------------------------------------------------


def test_iter_orphan_events_reads_only_owner_orphan_lines(repo: Path):
    _write_log(
        repo,
        "sess-1",
        [
            _orphan_line("2026-08-27T09:00:00Z", "sess-1", "foo.txt"),
            _named_owner_line("2026-08-27T09:01:00Z", "sess-1", "bar.txt", "sess-2"),
        ],
    )
    events = list(census_mod.iter_orphan_events(str(repo)))
    assert len(events) == 1
    assert events[0].path == "foo.txt"
    assert events[0].session_id == "sess-1"
    assert events[0].day == "2026-08-27"


def test_iter_orphan_events_filters_by_day(repo: Path):
    _write_log(
        repo,
        "sess-1",
        [
            _orphan_line("2026-08-26T09:00:00Z", "sess-1", "foo.txt"),
            _orphan_line("2026-08-27T09:00:00Z", "sess-1", "bar.txt"),
        ],
    )
    events = list(census_mod.iter_orphan_events(str(repo), day="2026-08-27"))
    assert [e.path for e in events] == ["bar.txt"]


def test_iter_orphan_events_across_multiple_session_dirs(repo: Path):
    _write_log(repo, "sess-1", [_orphan_line("2026-08-27T09:00:00Z", "sess-1", "a.txt")])
    _write_log(repo, "sess-2", [_orphan_line("2026-08-27T10:00:00Z", "sess-2", "b.txt")])
    events = list(census_mod.iter_orphan_events(str(repo)))
    assert sorted(e.path for e in events) == ["a.txt", "b.txt"]


def test_no_session_dirs_yields_no_events(repo: Path):
    assert list(census_mod.iter_orphan_events(str(repo))) == []


def test_malformed_line_is_skipped_not_raised(repo: Path):
    _write_log(
        repo,
        "sess-1",
        [
            "not a well formed line",
            _orphan_line("2026-08-27T09:00:00Z", "sess-1", "ok.txt"),
        ],
    )
    events = list(census_mod.iter_orphan_events(str(repo)))
    assert [e.path for e in events] == ["ok.txt"]


# --- cause classification -----------------------------------------------------


def test_classify_archival_sink(repo: Path):
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="archive/handoffs/foo.md",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "archival-sink"


def test_classify_deletion_when_untracked_and_absent(repo: Path):
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="never/existed.py",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "deletion"


def test_classify_unrecorded_write_when_file_exists_on_disk(repo: Path):
    (repo / "unclaimed.py").write_text("x = 1\n", encoding="utf-8")
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="unclaimed.py",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "unrecorded-write"


def test_classify_unrecorded_write_when_tracked_at_head(repo: Path):
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="README.md",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "unrecorded-write"


def test_classify_undeclared_op_output_derived_from_op_source(repo: Path, monkeypatch):
    ops_dir = repo / "coordinator_core" / "ops"
    ops_dir.mkdir(parents=True)
    (ops_dir / "some_op.py").write_text(
        'OUTPUT_DIR = "state/sizings"\n', encoding="utf-8"
    )
    census_mod._op_output_prefix_cache = None
    (repo / "state").mkdir()
    (repo / "state" / "sizings").mkdir()
    (repo / "state" / "sizings" / "example.yaml").write_text("x: 1\n", encoding="utf-8")
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="state/sizings/example.yaml",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "undeclared-op-output"
    census_mod._op_output_prefix_cache = None


def test_op_output_prefix_cache_is_populated_after_first_call(repo: Path):
    census_mod._op_output_prefix_cache = None
    census_mod._derive_op_output_prefixes(str(repo))
    assert census_mod._op_output_prefix_cache is not None
    census_mod._op_output_prefix_cache = None


# --- census aggregation --------------------------------------------------------


def test_run_census_buckets_counts_by_cause_path_session(repo: Path):
    _write_log(
        repo,
        "sess-1",
        [
            _orphan_line("2026-08-27T09:00:00Z", "sess-1", "archive/handoffs/a.md"),
            _orphan_line("2026-08-27T09:01:00Z", "sess-1", "never/existed.py"),
        ],
    )
    (repo / "unclaimed.py").write_text("x = 1\n", encoding="utf-8")
    _write_log(
        repo,
        "sess-2",
        [_orphan_line("2026-08-27T09:02:00Z", "sess-2", "unclaimed.py")],
    )

    result = census_mod.run_census(str(repo), day="2026-08-27")

    assert result.total_events == 3
    assert result.by_cause["archival-sink"] == 1
    assert result.by_cause["deletion"] == 1
    assert result.by_cause["unrecorded-write"] == 1
    assert result.by_cause["genuinely-unowned"] == 0
    assert result.by_path["archive/handoffs/a.md"] == 1
    assert result.by_session["sess-1"] == 2
    assert result.by_session["sess-2"] == 1
    assert len(result.members["archival-sink"]) == 1
    assert result.members["archival-sink"][0]["path"] == "archive/handoffs/a.md"


def test_run_census_every_cause_key_present_even_when_zero(repo: Path):
    result = census_mod.run_census(str(repo))
    for cause in (
        "archival-sink",
        "deletion",
        "undeclared-op-output",
        "unrecorded-write",
        "genuinely-unowned",
    ):
        assert cause in result.by_cause
        assert result.by_cause[cause] == 0
        assert result.members[cause] == []


def test_run_census_new_unexplained_cause_lands_in_genuinely_unowned(repo: Path, monkeypatch):
    # A path that is untracked, absent from op-output prefixes, AND absent
    # from disk cannot be a real "unrecorded write" -- it must fall to the
    # residue bucket rather than being silently absorbed elsewhere.
    monkeypatch.setattr(
        census_mod, "_git_tracked_at_head", lambda git_root, path: True
    )
    monkeypatch.setattr(
        census_mod, "_exists_on_disk_or_tracked", lambda git_root, path: False
    )
    event = census_mod.OrphanEvent(
        timestamp="2026-08-27T09:00:00Z",
        day="2026-08-27",
        session_id="sess-1",
        event_type="foreign-staged",
        path="weird/unexplained.bin",
        log_path="",
    )
    assert census_mod.classify_cause(str(repo), event) == "genuinely-unowned"


def test_to_dict_round_trips_all_fields(repo: Path):
    _write_log(
        repo,
        "sess-1",
        [_orphan_line("2026-08-27T09:00:00Z", "sess-1", "archive/handoffs/a.md")],
    )
    result = census_mod.run_census(str(repo), day="2026-08-27")
    payload = result.to_dict()
    assert payload["day"] == "2026-08-27"
    assert payload["total_events"] == 1
    assert payload["by_cause"]["archival-sink"] == 1
    assert payload["members"]["archival-sink"][0]["session_id"] == "sess-1"


# --- CLI ----------------------------------------------------------------------


def test_main_prints_json_for_day(repo: Path, capsys):
    _write_log(
        repo,
        "sess-1",
        [_orphan_line("2026-08-27T09:00:00Z", "sess-1", "archive/handoffs/a.md")],
    )
    rc = census_mod.main(["--git-root", str(repo), "--day", "2026-08-27"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"archival-sink": 1' in out

"""Tests for `coordinator_core.install.resolution_journal`.

Spec backlink: docs/research/2026-08-06-install-receipt-persistence-design.md,
chunk C1

Purpose: proves the append-only JSONL journal round-trips
`WriteSurfaceEntry`/`ClauseResolution` values through
`record_resolution`/`read_journal`, groups multi-writer/multi-clause rows
correctly, never clobbers a prior append, tolerates a truncated or
malformed row by leaving that writer/clause unreported (never crashing,
never partially applying a bad row), and honours
`COORDINATOR_DISABLE_MACHINE_MUTATION`.

Negative spec — this module does NOT:
  - exercise `maximalist.py`'s orchestrator wiring (env-var set / journal
    clear-at-run-start / receipt build-at-run-end) — that is C4, a
    separate chunk;
  - exercise `receipt.build_receipt` end-to-end — `read_journal`'s return
    shape is asserted to match `ClauseResolution`'s contract directly,
    without going through a real `WriteSurfaceDeclaration` + derivation.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.install import resolution_journal as target
from coordinator_core.install.receipt import ClauseResolution
from coordinator_core.install.write_surface import WriteSurfaceEntry


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    """Every test gets its own run-scoped journal path via the env var —
    never the settings-home default (which would touch a real machine
    location) — and `COORDINATOR_DISABLE_MACHINE_MUTATION` explicitly
    unset so the belt-and-braces suite-wide opt-out (armed elsewhere in
    this repo's conftest for OTHER modules' real-machine guards) does not
    silently no-op every append in this file."""
    journal_path = tmp_path / "journal-dir" / "resolution-journal.jsonl"
    monkeypatch.setenv(target.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_path


def test_absent_journal_reads_empty():
    assert target.read_journal() == {}


def test_round_trip_single_entry():
    entry = WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach")
    target.record_resolution("configure-git", 0, [entry])

    journal = target.read_journal()

    assert journal == {"configure-git": {0: ClauseResolution(entries=(entry,))}}


def test_multi_writer_and_multi_clause_grouping():
    entry_a = WriteSurfaceEntry(kind="git-config-key", key="gc.autoDetach")
    entry_b = WriteSurfaceEntry(kind="machine-local-key", key="repos.foo")
    entry_c = WriteSurfaceEntry(kind="file-path", path="/some/path")

    target.record_resolution("configure-git", 0, [entry_a])
    target.record_resolution("register-discovered-repos", 0, [entry_b])
    target.record_resolution("register-discovered-repos", 1, [entry_c])

    journal = target.read_journal()

    assert journal == {
        "configure-git": {0: ClauseResolution(entries=(entry_a,))},
        "register-discovered-repos": {
            0: ClauseResolution(entries=(entry_b,)),
            1: ClauseResolution(entries=(entry_c,)),
        },
    }


def test_append_not_overwrite_across_separate_calls():
    entry_a = WriteSurfaceEntry(kind="git-config-key", key="a")
    entry_b = WriteSurfaceEntry(kind="git-config-key", key="b")

    target.record_resolution("writer-a", 0, [entry_a])
    target.record_resolution("writer-b", 0, [entry_b])

    journal = target.read_journal()

    assert "writer-a" in journal and "writer-b" in journal
    assert journal["writer-a"][0] == ClauseResolution(entries=(entry_a,))
    assert journal["writer-b"][0] == ClauseResolution(entries=(entry_b,))


def test_rows_for_the_same_writer_and_clause_accumulate():
    """One clause is commonly resolved from several call sites — `dep_check`'s
    `visited_set_init` and `visited_set_crash_cleanup` both resolve clause 1.
    Each row is a partial contribution, so they union. Under the original
    last-write-wins reading the first site's entry silently vanished from the
    receipt, and uninstall never learned about a surface that was written."""
    entry_first = WriteSurfaceEntry(kind="git-config-key", key="first")
    entry_second = WriteSurfaceEntry(kind="git-config-key", key="second")

    target.record_resolution("writer-a", 0, [entry_first])
    target.record_resolution("writer-a", 0, [entry_second])

    journal = target.read_journal()

    assert journal == {
        "writer-a": {0: ClauseResolution(entries=(entry_first, entry_second))}
    }


def test_identical_entry_journalled_twice_is_not_doubled():
    """A call site that runs twice in one process, or re-journals after a
    retry, must not double its entry into the receipt."""
    entry = WriteSurfaceEntry(kind="git-config-key", key="same")

    target.record_resolution("writer-a", 0, [entry])
    target.record_resolution("writer-a", 0, [entry])

    assert target.read_journal() == {
        "writer-a": {0: ClauseResolution(entries=(entry,))}
    }


def test_empty_row_marks_the_clause_reported_without_contributing_entries():
    """The module's central invariant: "resolved to nothing on this machine"
    (a present, empty resolution) stays distinguishable from "never reported"
    (absent from the mapping entirely)."""
    target.record_resolution("writer-a", 0, [])

    journal = target.read_journal()

    assert journal == {"writer-a": {0: ClauseResolution(entries=())}}
    assert 0 in journal["writer-a"]


def test_truncated_final_line_is_skipped(_journal_env):
    entry = WriteSurfaceEntry(kind="git-config-key", key="ok")
    target.record_resolution("writer-a", 0, [entry])

    # Simulate a run that died mid-append: a second, well-formed row minus
    # its trailing newline (the truncated-tail shape a crash mid-os.write
    # would leave behind).
    with open(_journal_env, "a", encoding="utf-8") as f:
        f.write('{"writer_id": "writer-b", "clause_index": 0, "entries": []')

    journal = target.read_journal()

    assert journal == {"writer-a": {0: ClauseResolution(entries=(entry,))}}
    assert "writer-b" not in journal


def test_malformed_json_row_is_skipped(_journal_env):
    entry = WriteSurfaceEntry(kind="git-config-key", key="ok")
    target.record_resolution("writer-a", 0, [entry])

    with open(_journal_env, "a", encoding="utf-8") as f:
        f.write("not json at all\n")

    target.record_resolution("writer-c", 0, [entry])

    journal = target.read_journal()

    assert "writer-a" in journal
    assert "writer-c" in journal
    assert len(journal) == 2


def test_unparseable_entry_skips_whole_row_not_partial(_journal_env):
    good_entry = WriteSurfaceEntry(kind="git-config-key", key="ok")
    target.record_resolution("writer-a", 0, [good_entry])

    import json

    bad_row = json.dumps(
        {
            "writer_id": "writer-bad",
            "clause_index": 0,
            "entries": [
                {"kind": "git-config-key", "key": "fine"},
                {"kind": "git-config-key", "unexpected_field": "boom"},
            ],
        }
    )
    with open(_journal_env, "a", encoding="utf-8") as f:
        f.write(bad_row + "\n")

    journal = target.read_journal()

    assert "writer-a" in journal
    assert "writer-bad" not in journal


def test_row_missing_required_field_is_skipped(_journal_env):
    import json

    _journal_env.parent.mkdir(parents=True, exist_ok=True)
    with open(_journal_env, "a", encoding="utf-8") as f:
        f.write(json.dumps({"writer_id": "writer-a"}) + "\n")

    assert target.read_journal() == {}


def test_disabled_guard_refuses_append_and_clear(monkeypatch, _journal_env):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    entry = WriteSurfaceEntry(kind="git-config-key", key="x")
    target.record_resolution("writer-a", 0, [entry])

    assert not _journal_env.exists()
    assert target.read_journal() == {}

    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    target.record_resolution("writer-a", 0, [entry])
    assert _journal_env.exists()

    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    target.clear_journal()
    assert _journal_env.exists()  # refused — file untouched


def test_clear_journal_removes_file(_journal_env):
    entry = WriteSurfaceEntry(kind="git-config-key", key="x")
    target.record_resolution("writer-a", 0, [entry])
    assert _journal_env.exists()

    target.clear_journal()

    assert not _journal_env.exists()
    assert target.read_journal() == {}


def test_clear_journal_absent_file_is_noop():
    target.clear_journal()  # must not raise


def test_default_journal_path_uses_settings_home(monkeypatch, tmp_path):
    monkeypatch.delenv(target.RESOLUTION_JOURNAL_ENV_VAR, raising=False)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

    path = target._journal_path()

    assert path == fake_home / ".coordinator-claude-settings" / "install" / "resolution-journal.jsonl"


def test_write_surface_declares_journal_file():
    assert target.WRITE_SURFACE.writer_id == "resolution-journal"
    assert target.WRITE_SURFACE.source_module == "coordinator_core.install.resolution_journal"
    assert len(target.WRITE_SURFACE.clauses) == 1
    clause = target.WRITE_SURFACE.clauses[0]
    assert len(clause.entries) == 1
    assert clause.entries[0].kind == "file-path"
    assert clause.entries[0].path == os.path.join("install", "resolution-journal.jsonl")

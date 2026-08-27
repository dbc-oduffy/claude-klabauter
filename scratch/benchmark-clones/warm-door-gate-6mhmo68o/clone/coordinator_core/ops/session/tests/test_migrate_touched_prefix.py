"""
coordinator_core.ops.session.tests.test_migrate_touched_prefix — coverage for
the one-time `../`/absolute-path prefix migration over the legacy
`touched.txt` corpus (C6/C1/C1b/C7, pln-touched-txt-path-poisoning-nor-ecab01).

This test file did not exist at HEAD; created by C4 of
docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-its-writers.md.

Purpose: this module is a designed exception to the writer-repoint cutover
that plan performs elsewhere — it exists SOLELY to clean up the retired
`touched.txt` dialect and must keep reading/rewriting that dialect for as
long as legacy files survive on disk (see C9/AC8, and this module's own
docstring "Scope, precisely"). This suite pins that BY-DESIGN legacy-read
behavior (`_iter_touched_files` targets `touched.txt`, never the new
`touch-record.jsonl` sink) alongside AC1's operator-facing message-string
requirement: `main`'s argparse description must name the record as
retired/legacy rather than presenting `touched.txt` as the live record.

Negative spec: do not repoint `_iter_touched_files` at
`session.scope._read_touch_record_as_legacy_lines` (the seam) — that seam
is a READ-ONLY union of old+new dialects and cannot discriminate "this
line came from the legacy file, rewrite it there" from "this line is
already migrated", which is exactly the discrimination this module's
prefix-poisoning fix requires. See this chunk's own dispatch brief.

Spec backlink: docs/plans/2026-08-25-the-legacy-touch-record-is-retired-by-repointing-its-writers.md § C4
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.session.migrate_touched_prefix import (
    _iter_touched_files,
    main,
    plan_file,
    run_migration,
)

pytestmark = [pytest.mark.cadence]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _make_repo(tmp_path: Path) -> Path:
    # apply=True mode rewrites through `locked_write.locked_rmw`, which
    # resolves the git common dir against `repo_root` via
    # `coordinator_core.git.repo_root`'s pure-Python upward `.git` walk (no
    # subprocess spawn -- see that seam's own negative-spec). A bare `.git`
    # directory marker is sufficient for that walk to resolve; no real `git
    # init` spawn is needed to satisfy it.
    (tmp_path / ".git").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# AC1 — operator-facing message text names the record as retired/legacy,
# never presents touched.txt as the live/current dialect.
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
def test_main_argparse_description_names_the_record_as_retired_by_design(capsys):
    # Drive the REAL main() argparse builder via --help, which prints the
    # description and exits 0 -- proving the shipped description (not a
    # hand-copied string in this test) satisfies AC1.
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    # argparse hard-wraps the description across lines at terminal width, so
    # compare against the whitespace-collapsed text rather than the raw
    # (possibly line-broken) capture.
    out = " ".join(capsys.readouterr().out.split())
    assert "retired legacy touched.txt record" in out
    assert "touch-record.jsonl" in out
    assert "BY DESIGN" in out


# ---------------------------------------------------------------------------
# BY-DESIGN legacy read: `_iter_touched_files` targets touched.txt, not the
# new dialect's touch-record.jsonl sink.
# ---------------------------------------------------------------------------


def test_iter_touched_files_yields_legacy_touched_txt_only(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-1" / "touch-record.jsonl", "")  # new-dialect sibling
    _write(sessions_base / "sid-2" / "touched.txt", "c/d.py\n")

    found = list(_iter_touched_files(sessions_base))

    assert found == sorted(found)
    assert all(p.name == "touched.txt" for p in found)
    rels = {p.relative_to(sessions_base).as_posix() for p in found}
    assert rels == {"sid-1/touched.txt", "sid-2/touched.txt"}


def test_iter_touched_files_excludes_archive_component(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / ".archive" / "old" / "touched.txt", "a/b.py\n")
    _write(sessions_base / "sid-2" / "touched.txt", "c/d.py\n")

    found = list(_iter_touched_files(sessions_base))

    rels = {p.relative_to(sessions_base).as_posix() for p in found}
    assert rels == {"sid-2/touched.txt"}


def test_iter_touched_files_empty_when_sessions_base_absent(tmp_path):
    sessions_base = tmp_path / "does-not-exist"
    assert list(_iter_touched_files(sessions_base)) == []


# ---------------------------------------------------------------------------
# Core migration behavior — dry-run vs apply, escape classification.
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
def test_plan_file_classifies_clean_entry_unchanged(tmp_path):
    touched = tmp_path / "coordinator-sessions" / "sid-1" / "touched.txt"
    _write(touched, "a/b.py\n")

    outcome = plan_file(touched, tmp_path)

    assert outcome.changed is False
    assert outcome.outcomes[0].entry_class == "clean"


@pytest.mark.spawns_process
def test_plan_file_classifies_escaping_entry_as_dropped(tmp_path):
    touched = tmp_path / "coordinator-sessions" / "sid-1" / "touched.txt"
    _write(touched, "../escape.py\n")

    outcome = plan_file(touched, tmp_path)

    assert outcome.outcomes[0].entry_class == "dropped"
    assert outcome.outcomes[0].new_value is None


@pytest.mark.spawns_process
def test_dry_run_writes_nothing(tmp_path):
    sessions_base = tmp_path / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "../escape.py\na/b.py\n")
    original = (sessions_base / "sid-1" / "touched.txt").read_bytes()

    report = run_migration(sessions_base, tmp_path, apply=False)

    assert (sessions_base / "sid-1" / "touched.txt").read_bytes() == original
    assert report.backup_dir is None
    assert report.totals()["dropped"] == 1
    assert report.totals()["clean"] == 1


@pytest.mark.spawns_process
def test_apply_rewrites_file_and_writes_backup_and_manifest(tmp_path):
    repo = _make_repo(tmp_path)
    sessions_base = repo / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "../escape.py\na/b.py\n")
    git_common_dir = repo

    report = run_migration(
        sessions_base, repo, apply=True, git_common_dir=git_common_dir
    )

    rewritten = (sessions_base / "sid-1" / "touched.txt").read_text(encoding="utf-8")
    assert rewritten == "a/b.py\n"
    assert report.backup_dir is not None
    assert report.backup_dir.is_dir()
    backup_file = report.backup_dir / "sid-1" / "touched.txt"
    assert backup_file.read_text(encoding="utf-8") == "../escape.py\na/b.py\n"
    manifest_path = report.backup_dir / "drop-manifest.json"
    assert manifest_path.is_file()
    assert report.drop_manifest[0]["path"] == "../escape.py"


@pytest.mark.spawns_process
def test_apply_is_idempotent(tmp_path):
    repo = _make_repo(tmp_path)
    sessions_base = repo / "coordinator-sessions"
    _write(sessions_base / "sid-1" / "touched.txt", "../escape.py\na/b.py\n")

    run_migration(sessions_base, repo, apply=True, git_common_dir=repo)
    second = run_migration(sessions_base, repo, apply=True, git_common_dir=repo)

    assert second.files_changed_count() == 0

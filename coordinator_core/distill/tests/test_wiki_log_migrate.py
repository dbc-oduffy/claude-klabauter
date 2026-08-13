"""
coordinator_core.distill.tests.test_wiki_log_migrate

Unit tests for coordinator_core.distill.wiki_log_migrate — the one-time RETIREMENT of
Claude-klabauter's legacy wiki-side distill ledger (`docs/wiki/.distill-log.md`, C7). Folds every
row of the `<spec> → <wiki-target>` unicode-arrow ledger into the canonical
`state/distillation-log.md` schema, then tombstones the source file.

Coverage:
  - all three section shapes parse: Harvested-upstream (glob path -> DISTILLED,
    "reclaimed-upstream:" fate prefix), Harvested (spec -> DISTILLED, "harvested
    into <target>" fate), Skipped (spec -> SKIP, `#`-comment reason as fate)
  - run-id extraction: `pruned <date>` token, `(run <id>)` token, fallback constant
    for the Skipped section (no token in its header)
  - every migrated row round-trips through parse_distillation_log with the expected
    path/disposition/fate/run_id
  - rows_migrated counts every recognized data row across all three sections
  - backup created before write; original ledger content preserved verbatim at
    backup_path
  - source file replaced with the one-line tombstone after migration
  - re-run against an already-tombstoned file is REFUSED (AlreadyMigratedError),
    canonical log and backup left untouched
  - a second attempt against a stale `.legacy-backup` sibling is REFUSED
    (FileExistsError)
  - a malformed line inside a recognized section raises MalformedWikiLogError,
    nothing written
  - migrating into a canonical log that already has rows APPENDS rather than
    clobbering existing content

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C7.
"""

from __future__ import annotations

import pytest

from coordinator_core.distill._common import parse_distillation_log
from coordinator_core.distill.wiki_log_migrate import (
    FALLBACK_RUN_ID,
    AlreadyMigratedError,
    MalformedWikiLogError,
    is_already_tombstoned,
    migrate_wiki_log,
    render_tombstone,
)

WIKI_LOG_FIXTURE = """\
# Distill Harvest-Debt Ledger

Tracks which `archive/specs/**` plans have been harvested into `docs/wiki/` (or a DR).

> Some blockquote prose that must never be parsed as a data row.

## Harvested-upstream — pre-July example-doctrine-mirror-repo cohort (reclaimed to coordinator-claude, pruned 2026-07-08)

archive/specs/2026-03..06/** → harvested-upstream (distilled in coordinator-claude; see coordinator-claude `state/distillation-log.md`) — reclaimed to coordinator-claude, pruned from claude-klabauter per DR `2026-07-08-reclaim-pre-july-history-from-claude-klabauter`

## Harvested — 2026-07 claude-klabauter cohort (run 2026-07-08-pass1)

archive/specs/2026-07/2026-07-02-pcore-03-beachhead-coordinator-core.md → docs/wiki/coordinator-core-engine.md
archive/specs/2026-07/2026-07-05-coordinator-core-lifecycle-selfheal.md → docs/wiki/coordinator-core-engine.md

## Skipped — superseded (retained un-harvested, not extracted)

archive/specs/2026-07/2026-07-04-cross-repo-memo-claude-klabauter-ownership-and-standalone-service.md  # superseded by DR-210-claude-klabauter-native-tooling-ownership-strangler
archive/specs/2026-07/2026-07-05-single-writer-queue-mutating-dispatch.md  # superseded
"""


def test_migrate_wiki_log_parses_all_three_sections(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)

    assert result.rows_migrated == 5
    dispositions = [r.disposition for r in result.rows]
    assert dispositions == ["DISTILLED", "DISTILLED", "DISTILLED", "SKIP", "SKIP"]


def test_migrate_wiki_log_run_id_extraction(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)

    by_path = {r.path: r for r in result.rows}
    assert by_path["archive/specs/2026-03..06/**"].run_id == "2026-07-08"
    assert (
        by_path[
            "archive/specs/2026-07/2026-07-02-pcore-03-beachhead-coordinator-core.md"
        ].run_id
        == "2026-07-08-pass1"
    )
    assert (
        by_path[
            "archive/specs/2026-07/2026-07-04-cross-repo-memo-claude-klabauter-ownership-and-standalone-service.md"
        ].run_id
        == FALLBACK_RUN_ID
    )


def test_migrate_wiki_log_fate_text_shapes(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)
    by_path = {r.path: r for r in result.rows}

    upstream_fate = by_path["archive/specs/2026-03..06/**"].fate
    assert upstream_fate.startswith("reclaimed-upstream: harvested-upstream")
    assert "migrated from docs/wiki/.distill-log.md" in upstream_fate

    harvested_fate = by_path[
        "archive/specs/2026-07/2026-07-02-pcore-03-beachhead-coordinator-core.md"
    ].fate
    assert harvested_fate == (
        "harvested into docs/wiki/coordinator-core-engine.md "
        "(migrated from docs/wiki/.distill-log.md)"
    )

    skipped_fate = by_path[
        "archive/specs/2026-07/2026-07-05-single-writer-queue-mutating-dispatch.md"
    ].fate
    assert skipped_fate == "superseded (migrated from docs/wiki/.distill-log.md)"


def test_migrate_wiki_log_rows_round_trip_through_canonical_parser(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)

    parsed = parse_distillation_log(canonical_log_path.read_text(encoding="utf-8"))
    assert len(parsed) == result.rows_migrated
    parsed_by_path = {row.path: row for row in parsed}
    for row in result.rows:
        canon = parsed_by_path[row.path]
        assert canon.disposition == row.disposition
        assert canon.fate == row.fate
        assert canon.run_id == row.run_id


def test_migrate_wiki_log_backs_up_original_verbatim(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)

    backup_path = wiki_log_path.with_name(wiki_log_path.name + ".legacy-backup")
    assert str(backup_path) == result.backup_path
    assert backup_path.read_text(encoding="utf-8") == WIKI_LOG_FIXTURE


def test_migrate_wiki_log_writes_tombstone_over_source(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    migrate_wiki_log(wiki_log_path, canonical_log_path)

    final_text = wiki_log_path.read_text(encoding="utf-8")
    assert final_text == render_tombstone()
    assert is_already_tombstoned(final_text)
    assert not (tmp_path / ".distill-log.md.tmp").exists()


def test_migrate_wiki_log_refuses_rerun_against_tombstoned_file(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(render_tombstone(), encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    with pytest.raises(AlreadyMigratedError):
        migrate_wiki_log(wiki_log_path, canonical_log_path)

    assert not canonical_log_path.exists()


def test_migrate_wiki_log_refuses_stale_backup_sibling(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"
    backup_path = wiki_log_path.with_name(wiki_log_path.name + ".legacy-backup")
    backup_path.write_text("stale backup from a prior attempt", encoding="utf-8")

    with pytest.raises(FileExistsError):
        migrate_wiki_log(wiki_log_path, canonical_log_path)

    assert backup_path.read_text(encoding="utf-8") == "stale backup from a prior attempt"
    assert wiki_log_path.read_text(encoding="utf-8") == WIKI_LOG_FIXTURE
    assert not canonical_log_path.exists()


def test_migrate_wiki_log_malformed_row_raises_and_writes_nothing(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    malformed_fixture = (
        "## Harvested — 2026-07 claude-klabauter cohort (run 2026-07-08-pass1)\n\n"
        "this line has no arrow and no hash reason\n"
    )
    wiki_log_path.write_text(malformed_fixture, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"

    with pytest.raises(MalformedWikiLogError):
        migrate_wiki_log(wiki_log_path, canonical_log_path)

    assert not canonical_log_path.exists()
    backup_path = wiki_log_path.with_name(wiki_log_path.name + ".legacy-backup")
    assert not backup_path.exists()
    assert wiki_log_path.read_text(encoding="utf-8") == malformed_fixture


def test_migrate_wiki_log_appends_to_existing_canonical_log(tmp_path):
    wiki_log_path = tmp_path / ".distill-log.md"
    wiki_log_path.write_text(WIKI_LOG_FIXTURE, encoding="utf-8")
    canonical_log_path = tmp_path / "distillation-log.md"
    canonical_log_path.write_text(
        "# Distillation Log (canonical)\n"
        "# Columns: run | path | disposition | fate\n\n"
        "## Run 2026-05-06\n"
        "- some/preexisting/path.md -> DISTILLED, pre-existing row (run: 2026-05-06)\n",
        encoding="utf-8",
    )

    result = migrate_wiki_log(wiki_log_path, canonical_log_path)

    final_text = canonical_log_path.read_text(encoding="utf-8")
    assert "some/preexisting/path.md" in final_text
    parsed = parse_distillation_log(final_text)
    assert len(parsed) == 1 + result.rows_migrated

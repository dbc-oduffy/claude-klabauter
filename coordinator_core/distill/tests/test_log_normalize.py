"""
coordinator_core.distill.tests.test_log_normalize

Unit tests for coordinator_core.distill.log_normalize — the one-time legacy-log
NORMALIZER (C8). Migrates a legacy pipe-table distillation log (`date | action | path |
last_sha | belongs_to_spec | reason`) to the DoE C1 canonical schema, run exactly once
per repo.

Coverage:
  - disposition mapping (DR-053): ARCHIVED -> DISTILLED, DELETED -> EPHEMERAL,
    DELETE -> EPHEMERAL, distill-harvest -> DISTILLED (keyed on belongs_to_spec, NOT
    path — the wiki target folds into fate)
  - DR-053 recognized-and-intentionally-skipped actions (DELETE-GROUP, dr-create,
    wiki-update, judgment-create, distill-run) -> skipped with a distinct
    recognized-drop reason, never "unrecognized"
  - unrecognized action -> skipped with reason, never a silent default, never dropped
    from the row count
  - §7 accounting invariant: rows_migrated + rows_skipped == total legacy data-row
    count, across a fixture covering all 7 DR-053 action types
  - malformed row -> skipped with reason
  - rows_migrated / rows_skipped counts match the fixture exactly
  - backup created before canonical write; original legacy content preserved verbatim
    at backup_path
  - re-run against an already-canonical log is REFUSED (AlreadyCanonicalError), file
    untouched
  - every migrated row round-trips through parse_distillation_log with the expected
    path/disposition/fate/run_id; a row whose legacy path/date cell contains embedded
    whitespace cannot round-trip and is routed to `skipped`, never miscounted
  - a second attempt against a stale `.legacy-backup` sibling is REFUSED
    (FileExistsError), the prior backup and log_path both left untouched
  - the canonical write is atomic (temp file + os.replace) — no `.tmp` sibling left
    behind on success
  - a file that is neither canonical nor legacy-shaped raises NotLegacyShapedError
    rather than silently writing an empty canonical shell

Also covers `normalize_arrow_dialects_log` — the arrow-edged dialect migration added
2026-08-06 (see `log_normalize` module docstring § "Arrow-dialect migration"): three
non-canonical dialects (`-> DISTILLED (harvested; ...)`, `-> DELETED ...`,
`-> deleted ...`), already-canonical rows left unchanged, out-of-run and genuinely
unrecognized rows skipped-not-dropped, run-header trailing prose preserved verbatim, and
the accounting invariant across a mixed fixture.

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C8;
DoE-claude/docs/contracts/distill-engine-scripts.md § 7 (binding I/O contract).
"""

from __future__ import annotations

import pytest

from coordinator_core.distill._common import parse_distillation_log
from coordinator_core.distill.log_normalize import (
    AlreadyCanonicalError,
    NoArrowDialectRowsError,
    NotArrowShapedError,
    NotLegacyShapedError,
    RECOGNIZED_SKIP_ACTIONS,
    is_already_canonical,
    normalize_arrow_dialects_log,
    normalize_log,
)

LEGACY_FIXTURE = """\
# Distillation Log
# Append-only. Each row = one deleted scaffold OR one archived spec.
# Columns: date | action | path | last_sha | belongs_to_spec | reason

| date | action | path | last_sha | belongs_to_spec | reason |
|------|--------|------|----------|------------------|--------|
| 2026-05-06 | ARCHIVED | archive/specs/2026-03-08-agent-hierarchy-design.md | 029bcd32 | docs/wiki/agent-hierarchy.md | foundational agent-hierarchy design distilled into agent-hierarchy wiki guide |
| 2026-05-06 | DELETED | archive/specs/2026-03-08-scratch-notes.md | 11aa22bb | | ephemeral scratch notes with no lasting value |
| 2026-05-07 | RENAMED | archive/specs/2026-03-09-weird-row.md | ccddeeff | | this action value is not recognized by the mapping |
| 2026-05-07 | ARCHIVED | not-enough-columns | ddeeffaa |
| 2026-05-07 | ARCHIVED | archive/specs/2026-03-09-second-batch.md | ffeeddcc | docs/wiki/second-batch.md | second batch archived row in a different run date |
"""

ALREADY_CANONICAL_FIXTURE = """\
# Distillation Log (canonical)
# Columns: run | path | disposition | fate

## Run 2026-07-12
- archive/specs/foo.md -> DISTILLED, already migrated (run: 2026-07-12)
"""

# Review: code-reviewer (Finding 1) — a legacy `path` cell with embedded whitespace
# produces a rendered row that cannot round-trip through parse_distillation_log
# (\S+-anchored path grammar). Must be routed to `skipped`, never miscounted as
# migrated.
LEGACY_FIXTURE_WITH_WHITESPACE_PATH = """\
# Distillation Log
# Columns: date | action | path | last_sha | belongs_to_spec | reason

| date | action | path | last_sha | belongs_to_spec | reason |
|------|--------|------|----------|------------------|--------|
| 2026-05-06 | ARCHIVED | archive/specs/old file.md | 029bcd32 | docs/wiki/x.md | has a space in the path column |
| 2026-05-06 | ARCHIVED | archive/specs/clean-path.md | 11aa22bb | docs/wiki/y.md | this one is fine |
"""

NOT_LEGACY_SHAPED_FIXTURE = """\
This file has no pipe-delimited lines at all, and no canonical markers either.
It is neither legacy-shaped nor canonical-shaped.
"""

# DR-053 fixture: one row per each of the 7 recognized action tokens, plus one
# genuinely unrecognized action, so every branch of the mapping is exercised in one
# fixture and the §7 accounting invariant can be checked end-to-end.
DR053_FIXTURE = """\
# Distillation Log
# Columns: date | action | path | last_sha | belongs_to_spec | reason

| date | action | path | last_sha | belongs_to_spec | reason |
|------|--------|------|----------|------------------|--------|
| 2026-05-06 | ARCHIVED | archive/specs/2026-03-08-agent-hierarchy-design.md | 029bcd32 | | foundational agent-hierarchy design distilled into agent-hierarchy wiki guide |
| 2026-05-06 | DELETED | archive/specs/2026-03-08-scratch-notes.md | 11aa22bb | | ephemeral scratch notes with no lasting value |
| 2026-05-07 | DELETE | archive/specs/2026-03-09-old-stub.md | aa11bb22 | | superseded stub removed |
| 2026-05-07 | distill-harvest | docs/wiki/harvest-target.md | ffeeddcc | archive/specs/2026-03-09-harvest-source.md | harvested into the wiki guide |
| 2026-05-08 | DELETE-GROUP | archive/specs/2026-*-batch-glob.md | 00112233 | | bulk cleanup of a stale batch |
| 2026-05-08 | dr-create | docs/decisions/DR-999-example.md | 44556677 | | decision record created during this run |
| 2026-05-08 | wiki-update | docs/wiki/some-guide.md | 8899aabb | | wiki page touched during this run |
| 2026-05-08 | judgment-create | state/review-trail/findings/example.md | ccddeeff | | judgment recorded during this run |
| 2026-05-08 | distill-run | n/a | 01234567 | | the distill-run event row itself |
| 2026-05-09 | RENAMED | archive/specs/2026-03-09-weird-row.md | ccddeeff | | this action value is not recognized by the mapping |
"""


# ---------------------------------------------------------------------------
# is_already_canonical
# ---------------------------------------------------------------------------


def test_is_already_canonical_detects_header_marker():
    assert is_already_canonical(ALREADY_CANONICAL_FIXTURE) is True


def test_is_already_canonical_false_for_legacy_fixture():
    assert is_already_canonical(LEGACY_FIXTURE) is False


# ---------------------------------------------------------------------------
# normalize_log — happy path
# ---------------------------------------------------------------------------


def test_normalize_log_migrates_archived_and_deleted(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    assert result.rows_migrated == 3
    assert result.rows_skipped == 2  # unrecognized action + malformed line
    assert len(result.skipped) == 2


def test_normalize_log_disposition_mapping_exact(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}

    assert by_path["archive/specs/2026-03-08-agent-hierarchy-design.md"].disposition == "DISTILLED"
    assert by_path["archive/specs/2026-03-08-scratch-notes.md"].disposition == "EPHEMERAL"
    assert by_path["archive/specs/2026-03-09-second-batch.md"].disposition == "DISTILLED"


def test_normalize_log_unrecognized_action_skipped_with_reason(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    reasons = [s.reason for s in result.skipped]
    assert any("RENAMED" in r for r in reasons)
    # never silently dropped: the unrecognized-action row must not appear as a
    # canonical row anywhere in the output
    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert all(r.path != "archive/specs/2026-03-09-weird-row.md" for r in rows)


def test_normalize_log_malformed_row_skipped_with_reason(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    reasons = [s.reason for s in result.skipped]
    assert any("malformed" in r for r in reasons)


def test_normalize_log_skipped_never_dropped_from_count(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    # every skipped row carries a line number and a reason, none silently vanish
    for skip in result.skipped:
        assert isinstance(skip.line, int)
        assert skip.line > 0
        assert skip.reason


def test_normalize_log_run_id_grouped_by_legacy_date(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}

    assert by_path["archive/specs/2026-03-08-agent-hierarchy-design.md"].run_id == "2026-05-06"
    assert by_path["archive/specs/2026-03-08-scratch-notes.md"].run_id == "2026-05-06"
    assert by_path["archive/specs/2026-03-09-second-batch.md"].run_id == "2026-05-07"


def test_normalize_log_fate_from_legacy_reason(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}

    assert (
        by_path["archive/specs/2026-03-08-agent-hierarchy-design.md"].fate
        == "foundational agent-hierarchy design distilled into agent-hierarchy wiki guide"
    )
    assert (
        by_path["archive/specs/2026-03-08-scratch-notes.md"].fate
        == "ephemeral scratch notes with no lasting value"
    )


# ---------------------------------------------------------------------------
# backup / preservation
# ---------------------------------------------------------------------------


def test_normalize_log_creates_backup_before_write(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    from pathlib import Path as _Path

    backup_path = _Path(result.backup_path)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == LEGACY_FIXTURE


def test_normalize_log_original_content_preserved_verbatim(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    from pathlib import Path as _Path

    backup_text = _Path(result.backup_path).read_text(encoding="utf-8")
    assert backup_text == LEGACY_FIXTURE
    # the legacy pipe-table rows must be recoverable byte-for-byte from the backup
    assert "| ARCHIVED |" in backup_text
    assert "| DELETED |" in backup_text


def test_normalize_log_refuses_to_clobber_existing_backup(tmp_path):
    # Review: code-reviewer (Finding 2, P1) — a stale `.legacy-backup` sibling from a
    # prior attempt must never be silently overwritten by a second attempt; that
    # backup may be the only remaining copy of the true original. Fail loud instead.
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    backup_path = log_path.with_name(log_path.name + ".legacy-backup")
    prior_backup_content = "this is a DIFFERENT, earlier original — must survive untouched"
    backup_path.write_text(prior_backup_content, encoding="utf-8")

    with pytest.raises(FileExistsError):
        normalize_log(log_path)

    # the pre-existing backup must not have been clobbered
    assert backup_path.read_text(encoding="utf-8") == prior_backup_content
    # log_path itself must be untouched too — the refusal happens before any write
    assert log_path.read_text(encoding="utf-8") == LEGACY_FIXTURE


def test_normalize_log_write_is_atomic_no_tmp_leftover_on_success(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    tmp_sibling = log_path.with_name(log_path.name + ".tmp")
    assert not tmp_sibling.exists()


def test_normalize_log_raises_not_legacy_shaped_for_unrecognized_content(tmp_path):
    # Review: code-reviewer (Finding 4, P2) — a file that is neither canonical nor
    # legacy-pipe-table-shaped (no pipe-delimited lines at all) must not silently
    # produce an empty canonical shell; it must raise instead.
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(NOT_LEGACY_SHAPED_FIXTURE, encoding="utf-8")

    with pytest.raises(NotLegacyShapedError):
        normalize_log(log_path)

    assert log_path.read_text(encoding="utf-8") == NOT_LEGACY_SHAPED_FIXTURE
    assert not log_path.with_name(log_path.name + ".legacy-backup").exists()


def test_normalize_log_output_is_no_longer_legacy_shaped(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    canonical_text = log_path.read_text(encoding="utf-8")
    assert is_already_canonical(canonical_text) is True


# ---------------------------------------------------------------------------
# re-run refusal
# ---------------------------------------------------------------------------


def test_normalize_log_refuses_rerun_against_canonical_header(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ALREADY_CANONICAL_FIXTURE, encoding="utf-8")

    with pytest.raises(AlreadyCanonicalError):
        normalize_log(log_path)

    # file must be left untouched — no backup created, no rewrite
    assert log_path.read_text(encoding="utf-8") == ALREADY_CANONICAL_FIXTURE
    assert not log_path.with_name(log_path.name + ".legacy-backup").exists()


def test_normalize_log_refuses_rerun_against_parseable_canonical_without_marker(tmp_path):
    # A canonical-shaped file that happens to lack the literal marker line but still
    # parses via parse_distillation_log must also be refused (contract §7's second
    # detection signal).
    text = "## Run run-1\n- archive/specs/foo.md -> DISTILLED, already canonical (run: run-1)\n"
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(text, encoding="utf-8")

    with pytest.raises(AlreadyCanonicalError):
        normalize_log(log_path)

    assert log_path.read_text(encoding="utf-8") == text


def test_normalize_log_running_twice_does_not_corrupt(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result1 = normalize_log(log_path)
    rows_after_first = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert len(rows_after_first) == result1.rows_migrated

    with pytest.raises(AlreadyCanonicalError):
        normalize_log(log_path)

    rows_after_second_attempt = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert rows_after_second_attempt == rows_after_first  # no duplication, no corruption


# ---------------------------------------------------------------------------
# round-trip guarantee (via log_append.render_row reuse)
# ---------------------------------------------------------------------------


def test_normalize_log_every_migrated_row_round_trips(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert len(rows) == result.rows_migrated

    for row in rows:
        assert row.disposition in {"DISTILLED", "EPHEMERAL", "PROMOTE", "SKIP", "PRESERVE"}
        assert row.path
        assert row.fate
        assert row.run_id


def test_normalize_log_canonical_rows_use_ascii_arrow(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    canonical_text = log_path.read_text(encoding="utf-8")
    assert "->" in canonical_text
    assert "→" not in canonical_text


def test_normalize_log_embedded_whitespace_path_is_skipped_not_migrated(tmp_path):
    # Review: code-reviewer (Finding 1, P1) — a legacy row whose `path` cell contains
    # embedded whitespace cannot round-trip through parse_distillation_log's
    # \S+-anchored grammar. It must be skipped with a reason, never written-but-
    # miscounted as migrated.
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE_WITH_WHITESPACE_PATH, encoding="utf-8")

    result = normalize_log(log_path)

    assert result.rows_migrated == 1
    assert result.rows_skipped == 1

    reasons = [s.reason for s in result.skipped]
    assert any("round-trip" in r for r in reasons)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert len(rows) == result.rows_migrated
    # the whitespace-path row must never appear in the canonical output
    assert all("old file.md" not in r.path for r in rows)
    assert any(r.path == "archive/specs/clean-path.md" for r in rows)


# ---------------------------------------------------------------------------
# DR-053 mapping extension: distill-harvest, DELETE, recognized-skip actions
# ---------------------------------------------------------------------------


def test_normalize_log_delete_maps_to_ephemeral(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}
    assert by_path["archive/specs/2026-03-09-old-stub.md"].disposition == "EPHEMERAL"


def test_normalize_log_distill_harvest_keys_on_belongs_to_spec_not_path(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}

    # the canonical <path> is the belongs_to_spec column (archive/specs/... source),
    # never the legacy `path` column (the wiki target) — DR-053.
    assert "archive/specs/2026-03-09-harvest-source.md" in by_path
    assert "docs/wiki/harvest-target.md" not in by_path

    harvested_row = by_path["archive/specs/2026-03-09-harvest-source.md"]
    assert harvested_row.disposition == "DISTILLED"
    # the wiki target must not be lost — it is folded into fate.
    assert "docs/wiki/harvest-target.md" in harvested_row.fate


def test_normalize_log_distill_harvest_row_round_trips(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert any(
        r.path == "archive/specs/2026-03-09-harvest-source.md" for r in rows
    )
    # every row present in the canonical file must have been counted as migrated
    assert len(rows) == result.rows_migrated


@pytest.mark.parametrize(
    "path_fragment",
    [
        "archive/specs/2026-*-batch-glob.md",
        "docs/decisions/DR-999-example.md",
        "docs/wiki/some-guide.md",
        "state/review-trail/findings/example.md",
        "n/a",
    ],
)
def test_normalize_log_recognized_skip_actions_never_appear_as_rows(tmp_path, path_fragment):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    normalize_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    assert all(r.path != path_fragment for r in rows)


def test_normalize_log_recognized_skip_actions_get_distinct_reason_not_unrecognized(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    reasons_by_line = {s.line: s.reason for s in result.skipped}

    # DELETE-GROUP row (line 8 of the fixture body) and the 4 event-row actions must
    # each carry their DR-053 recognized-drop reason, never "unrecognized action".
    recognized_reasons = [
        s.reason
        for s in result.skipped
        if s.reason in RECOGNIZED_SKIP_ACTIONS.values()
    ]
    assert len(recognized_reasons) == 5  # DELETE-GROUP + 4 event-row actions
    for reason in recognized_reasons:
        assert "unrecognized" not in reason

    # the DELETE-GROUP reason specifically calls out bulk-deletion / no per-spec
    # disposition, and the event-row reasons specifically call out "event row".
    assert any("bulk-deletion" in r for r in recognized_reasons)
    assert sum("event row" in r for r in recognized_reasons) == 4


def test_normalize_log_truly_unknown_action_still_unrecognized(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    reasons = [s.reason for s in result.skipped]
    assert any("unrecognized action" in r and "RENAMED" in r for r in reasons)


def test_normalize_log_dr053_accounting_invariant_migrated_plus_skipped_equals_total(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(DR053_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    total_data_rows = 10  # 9 recognized-action rows + 1 RENAMED row in DR053_FIXTURE
    assert result.rows_migrated + result.rows_skipped == total_data_rows
    # explicit expected split, so a future accidental miscount is caught precisely:
    # migrated: ARCHIVED, DELETED, DELETE, distill-harvest = 4
    # skipped: DELETE-GROUP, dr-create, wiki-update, judgment-create, distill-run,
    #          RENAMED (unrecognized) = 6
    assert result.rows_migrated == 4
    assert result.rows_skipped == 6


def test_normalize_log_accounting_invariant_holds_on_original_legacy_fixture_too(tmp_path):
    # sanity check against the original (pre-DR-053) fixture, which mixes a
    # malformed row in with recognized/unrecognized actions.
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(LEGACY_FIXTURE, encoding="utf-8")

    result = normalize_log(log_path)

    total_data_rows = 5  # see LEGACY_FIXTURE: 5 pipe-delimited data rows
    assert result.rows_migrated + result.rows_skipped == total_data_rows


# ---------------------------------------------------------------------------
# absent file
# ---------------------------------------------------------------------------


def test_normalize_log_raises_on_missing_file(tmp_path):
    log_path = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError):
        normalize_log(log_path)


# ---------------------------------------------------------------------------
# normalize_arrow_dialects_log — arrow-edged dialect migration (2026-08-06)
# ---------------------------------------------------------------------------

# Fixture covering all three arrow dialects, an already-canonical row, a row before any
# run header, and a genuinely unrecognized arrow-shaped row, under a plain `## Run`
# header (no trailing prose) so migrated rows are readable via `parse_distillation_log`
# for assertion purposes.
ARROW_DIALECT_FIXTURE = """\
# Distillation Log (mixed dialects)

- orphan/before-any-run-header.md -> DISTILLED, orphan row (run: nowhere)
## Run 2026-05-06
- archive/specs/already-canonical.md -> DISTILLED, already canonical (run: 2026-05-06)
- archive/specs/harvested-one.md -> DISTILLED (harvested; folded into wiki guide)
- archive/specs/deleted-upper.md -> DELETED no longer needed, superseded
- archive/specs/deleted-lower.md -> deleted, ephemeral scratch notes
- archive/specs/weird-shape.md -> SOMETHING-ELSE unrecognized arrow shape entirely
"""

# Real-world-shaped fixture: a run header carrying trailing prose (one with a
# parenthesised date before an em-dash, per the sender's actual log). A header with
# trailing prose is NOT matched by `_common._RUN_HEADER_RE`, so rows under it are
# migrated by `normalize_arrow_dialects_log` (which uses its own permissive header
# regex to locate the enclosing run_id) but are NOT yet readable via
# `parse_distillation_log` until the header itself is fixed (tracked separately —
# "stop writing descriptions on `## Run` headers" is the sender's own remediation item,
# not this normalizer's job). This fixture verifies the header line survives verbatim.
ARROW_DIALECT_FIXTURE_WITH_HEADER_DESCRIPTION = """\
# Distillation Log (mixed dialects)

## Run 2026-05-06 (harvest sweep) — legacy hand-rolled append, predates bin/distill-log-append.py
- archive/specs/harvested-one.md -> DISTILLED (harvested; folded into wiki guide)
"""


def test_normalize_arrow_dialects_migrates_all_three_dialects(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    rows = parse_distillation_log(log_path.read_text(encoding="utf-8"))
    by_path = {r.path: r for r in rows}

    assert by_path["archive/specs/harvested-one.md"].disposition == "DISTILLED"
    assert "folded into wiki guide" in by_path["archive/specs/harvested-one.md"].fate

    assert by_path["archive/specs/deleted-upper.md"].disposition == "EPHEMERAL"
    assert by_path["archive/specs/deleted-lower.md"].disposition == "EPHEMERAL"

    # already-canonical row survives untouched
    assert by_path["archive/specs/already-canonical.md"].disposition == "DISTILLED"


def test_normalize_arrow_dialects_accounting_invariant(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    # candidate rows: orphan(1) + already-canonical(1) + 3 arrow dialects + 1 unrecognized = 6
    assert result.rows_migrated + result.rows_skipped == 6
    assert result.rows_migrated == 4  # already-canonical + 3 successfully-migrated arrows
    assert result.rows_skipped == 2  # orphan-before-header + unrecognized shape


def test_normalize_arrow_dialects_unrecognized_row_skipped_not_dropped(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    reasons = [s.reason for s in result.skipped]
    assert any("unrecognized arrow-shaped row" in r for r in reasons)
    assert any("before any" in r for r in reasons)

    text = log_path.read_text(encoding="utf-8")
    # the unrecognized row's original text must still be present verbatim, never dropped
    assert "-> SOMETHING-ELSE unrecognized arrow shape entirely" in text
    assert "orphan/before-any-run-header.md" in text


def test_normalize_arrow_dialects_preserves_run_header_description(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE_WITH_HEADER_DESCRIPTION, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    text = log_path.read_text(encoding="utf-8")
    assert (
        "## Run 2026-05-06 (harvest sweep) — legacy hand-rolled append, "
        "predates bin/distill-log-append.py"
    ) in text
    # the row itself is still migrated (recognized by this module's own permissive
    # header regex), even though the descriptioned header keeps it unreadable via
    # `_common.parse_distillation_log` until the header is separately fixed
    assert result.rows_migrated == 1
    assert result.rows_skipped == 0
    assert "-> DISTILLED, harvested; folded into wiki guide (run: 2026-05-06)" in text


def test_normalize_arrow_dialects_backup_preserves_original(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    from pathlib import Path as _Path

    backup_text = _Path(result.backup_path).read_text(encoding="utf-8")
    assert backup_text == ARROW_DIALECT_FIXTURE


def test_normalize_arrow_dialects_atomic_no_tmp_leftover(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    normalize_arrow_dialects_log(log_path)

    assert not log_path.with_name(log_path.name + ".tmp").exists()


def test_normalize_arrow_dialects_refuses_when_no_run_headers(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text("no run headers here at all, just prose\n", encoding="utf-8")

    with pytest.raises(NotArrowShapedError):
        normalize_arrow_dialects_log(log_path)


def test_normalize_arrow_dialects_refuses_when_nothing_to_migrate(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(
        "## Run 2026-05-06\n"
        "- archive/specs/already-canonical.md -> DISTILLED, already canonical (run: 2026-05-06)\n",
        encoding="utf-8",
    )

    with pytest.raises(NoArrowDialectRowsError):
        normalize_arrow_dialects_log(log_path)


def test_normalize_arrow_dialects_missing_file_raises(tmp_path):
    log_path = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError):
        normalize_arrow_dialects_log(log_path)


def test_normalize_arrow_dialects_split_rewritten_and_already_canonical_counts(tmp_path):
    # Review: review-integrator — rows_migrated conflates "rewritten from a
    # dialect" and "already canonical, left untouched" per the module docstring's
    # own admission. rows_rewritten / rows_already_canonical split the ledger so a
    # consumer need not read that disambiguation to get the true breakdown.
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    result = normalize_arrow_dialects_log(log_path)

    assert result.rows_already_canonical == 1  # archive/specs/already-canonical.md
    assert result.rows_rewritten == 3  # the 3 successfully-migrated arrow dialects
    assert result.rows_rewritten + result.rows_already_canonical == result.rows_migrated


def test_normalize_log_rows_rewritten_and_already_canonical_are_none():
    # normalize_log (the legacy pipe-table normalizer) has no "already canonical,
    # left untouched" case at all — both split fields must stay None there, never
    # coerced to 0 (which would falsely imply the split was computed).
    from coordinator_core.distill.log_normalize import NormalizeResult

    result = NormalizeResult(log_path="x", rows_migrated=3, rows_skipped=2, skipped=[])
    assert result.rows_rewritten is None
    assert result.rows_already_canonical is None
    assert "rows_rewritten" not in result.to_dict()
    assert "rows_already_canonical" not in result.to_dict()


# ---------------------------------------------------------------------------
# any_arrow_dialect_match flag — the BLOCKED-verdict fix
#
# Review: reviewer (Finding 1, P1/BLOCKED) — the flag was previously set on
# `_ARROW_DIALECT_ROW_RE` match alone, before the round-trip check could still
# route the row to `skipped`. A file with exactly one round-trip-failing
# arrow-dialect row and no already-canonical rows must still raise
# NoArrowDialectRowsError (nothing actually survived to be migrated) rather
# than writing a no-op backup+rewrite and permanently occupying the backup
# slot.
# ---------------------------------------------------------------------------

def test_normalize_arrow_dialects_all_rows_round_trip_failing_raises_no_arrow_dialect_rows(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    # a DELETED-dialect row whose `rest` is comma-only: `_arrow_dialect_fate`
    # strips the leading comma and any surrounding whitespace, leaving an empty
    # fate — routed to `skipped` with "missing required field(s)" rather than
    # ever being written. No row survives, so the flag must never flip True.
    text = "## Run 2026-05-06\n- archive/specs/weird.md -> DELETED ,\n"
    log_path.write_text(text, encoding="utf-8")

    with pytest.raises(NoArrowDialectRowsError):
        normalize_arrow_dialects_log(log_path)

    # refusing before any write means: no backup slot consumed, file untouched.
    assert log_path.read_text(encoding="utf-8") == text
    assert not log_path.with_name(log_path.name + ".arrow-dialect-backup").exists()


def test_normalize_arrow_dialects_does_not_poison_backup_slot_on_round_trip_failure(tmp_path):
    # The consequence chain named by the BLOCKED finding: refusing via
    # NoArrowDialectRowsError must leave the `.arrow-dialect-backup` slot free so
    # a later legitimate migration (once the offending row is fixed) can still
    # create it.
    log_path = tmp_path / "distillation-log.md"
    # a DELETED-dialect row whose `rest` is comma-only: `_arrow_dialect_fate`
    # strips the leading comma and any surrounding whitespace, leaving an empty
    # fate — routed to `skipped` with "missing required field(s)" rather than
    # ever being written. No row survives, so the flag must never flip True.
    text = "## Run 2026-05-06\n- archive/specs/weird.md -> DELETED ,\n"
    log_path.write_text(text, encoding="utf-8")

    with pytest.raises(NoArrowDialectRowsError):
        normalize_arrow_dialects_log(log_path)

    backup_path = log_path.with_name(log_path.name + ".arrow-dialect-backup")
    assert not backup_path.exists()

    # a legitimate follow-up migration must still be able to run once a real
    # arrow-dialect row is present.
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")
    result = normalize_arrow_dialects_log(log_path)
    assert result.rows_rewritten == 3
    assert backup_path.exists()


# ---------------------------------------------------------------------------
# _arrow_dialect_fate — DISTILLED-wrapped-paren fallback with trailing text
#
# Review: reviewer (P3 nit) — untested fallback: a DISTILLED row with trailing
# content after the closing paren (`-> DISTILLED (harvested; ...) extra note`)
# falls through to "return rest unmodified, parens included" instead of
# unwrapping. Not a data-loss bug per the function's own contract (nothing
# invented or dropped), but this pins the current, intentional behavior so a
# future change to it is deliberate, not accidental.
# ---------------------------------------------------------------------------


def test_normalize_arrow_dialects_distilled_trailing_text_after_paren_kept_unwrapped():
    from coordinator_core.distill.log_normalize import _arrow_dialect_fate

    fate = _arrow_dialect_fate("DISTILLED", "(harvested; folded into wiki guide) extra note")
    # the whole-string paren-wrap regex only matches when the paren group spans
    # the ENTIRE trimmed rest — trailing text after the closing paren breaks
    # that match, so the fallback returns rest verbatim, parens included.
    assert fate == "(harvested; folded into wiki guide) extra note"


def test_normalize_arrow_dialects_refuses_to_clobber_existing_backup(tmp_path):
    log_path = tmp_path / "distillation-log.md"
    log_path.write_text(ARROW_DIALECT_FIXTURE, encoding="utf-8")

    backup_path = log_path.with_name(log_path.name + ".arrow-dialect-backup")
    prior_backup_content = "an earlier original that must survive untouched"
    backup_path.write_text(prior_backup_content, encoding="utf-8")

    with pytest.raises(FileExistsError):
        normalize_arrow_dialects_log(log_path)

    assert backup_path.read_text(encoding="utf-8") == prior_backup_content
    assert log_path.read_text(encoding="utf-8") == ARROW_DIALECT_FIXTURE

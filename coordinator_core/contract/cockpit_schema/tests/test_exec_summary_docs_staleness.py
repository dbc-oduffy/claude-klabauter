"""
test_exec_summary_docs_staleness — parse/reject tests for `ExecSummary.
docs_staleness` and its leaf `DocStalenessEntry` (C6,
`entities/exec_summary.py`).

Spec backlink: DoE-claude:pln-human-facing-doc-staleness-det-d9c047 § C6
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.exec_summary import (
    DocStalenessEntry,
    ExecSummary,
)
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_dump,
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "local_fs",
    "repo": "fixture-owner/fixture-repo",
    "ref": None,
    "path": "docs/exec-summary.md",
    "observed_at": "2026-07-28T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

ONE_DOC = {
    "path": "README.md",
    "stale": True,
    "commits_since": 8123,
    "days_since": 27,
    "last_touch_sha": "56c1bf6cbdeadbeefdeadbeefdeadbeefdeadbeef",
    "last_touch_date": "2026-07-01T09:15:00Z",
    "changed_areas": ["coordinator", "docs", "state"],
    "threshold_commits": 8000,
    "threshold_days": 21,
}

EXEC_SUMMARY_VALID = {
    "repo": "fixture-owner/fixture-repo",
    "coordinator_root_path": ".",
    "id": "fixture-repo",
    "provenance": PROV,
    "project": "Fixture Project",
    "generated": "2026-07-28T10:00:00Z",
    "generator": "exec-summary-gen",
    "identity": "identity body",
    "progress": "progress body",
    "what_makes_it_special": "special body",
    "near_term_goals": "goals body",
    "docs_staleness": [ONE_DOC],
}


# ===========================================================================
# DocStalenessEntry — leaf shape
# ===========================================================================


def test_doc_staleness_entry_valid_parses():
    zod_parse(DocStalenessEntry, ONE_DOC)


def test_doc_staleness_entry_rejects_bare_last_touch_date():
    """HARD CONSTRAINT (cockpit reply, note (a)): a naive datetime with no
    offset must fail — their strict Zod codegen rejects it and quarantines
    the whole exec-summary row on ingest if it slips through here."""
    naive = {**ONE_DOC, "last_touch_date": "2026-07-01T09:15:00"}
    assert not zod_safe_parse_ok(DocStalenessEntry, naive)


def test_doc_staleness_entry_rejects_missing_threshold_commits():
    missing = {k: v for k, v in ONE_DOC.items() if k != "threshold_commits"}
    assert not zod_safe_parse_ok(DocStalenessEntry, missing)


def test_doc_staleness_entry_rejects_missing_threshold_days():
    missing = {k: v for k, v in ONE_DOC.items() if k != "threshold_days"}
    assert not zod_safe_parse_ok(DocStalenessEntry, missing)


def test_doc_staleness_entry_rejects_missing_changed_areas():
    missing = {k: v for k, v in ONE_DOC.items() if k != "changed_areas"}
    assert not zod_safe_parse_ok(DocStalenessEntry, missing)


def test_doc_staleness_entry_rejects_extra_field():
    extra = {**ONE_DOC, "unexpected_field": "nope"}
    assert not zod_safe_parse_ok(DocStalenessEntry, extra)


# ===========================================================================
# ExecSummary.docs_staleness — required-with-null (D9), not optional
# ===========================================================================


def test_exec_summary_with_docs_staleness_array_parses():
    zod_parse(ExecSummary, EXEC_SUMMARY_VALID)


def test_exec_summary_docs_staleness_null_parses():
    """null == "detector did not run for this repo" — a distinct, valid
    state from `[]` ("ran, nothing stale")."""
    nulled = {**EXEC_SUMMARY_VALID, "docs_staleness": None}
    zod_parse(ExecSummary, nulled)


def test_exec_summary_docs_staleness_empty_list_parses():
    """[] == "detector ran, nothing stale" — distinct from null; both are
    valid and must not collapse to the same wire shape."""
    empty = {**EXEC_SUMMARY_VALID, "docs_staleness": []}
    zod_parse(ExecSummary, empty)


def test_exec_summary_rejects_missing_docs_staleness_key():
    """REQUIRED-WITH-NULL, not optional (cockpit reply, note (b)): the key
    itself must always be present — omitting it entirely is invalid, unlike
    `content_hash` (the true optional-with-default sibling on this entity)."""
    missing = {k: v for k, v in EXEC_SUMMARY_VALID.items() if k != "docs_staleness"}
    assert not zod_safe_parse_ok(ExecSummary, missing)


def test_exec_summary_docs_staleness_round_trips_through_json_dump():
    dumped = zod_dump(ExecSummary, zod_parse(ExecSummary, EXEC_SUMMARY_VALID))
    assert dumped["docs_staleness"] == [ONE_DOC]

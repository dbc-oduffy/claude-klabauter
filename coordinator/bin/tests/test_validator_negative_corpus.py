"""
coordinator.bin.tests.test_validator_negative_corpus

Standing regression oracle for the validator-negative fixture corpus. Re-port of
testNegativeCorpus(fixturesDir, schemasDir) from DoE-claude coordinator/bin/lib/schema.js,
deleted (not ported) in commit 480ad8f8 ("D1: retire claude-klabauter oracle .js + bin/lib/*.js").
The corpus itself (fixtures/validator-negative/*.md + expected-rejections.json) survived
the port; its loader did not, so nothing exercised the corpus from 480ad8f8 until this file.

Negative-spec: this is the ONLY consumer of
coordinator/bin/tests/fixtures/validator-negative/ in this repo. If another test starts
reading these fixtures directly, fold it into this module instead — a second reader with
its own drift-blind spot is exactly how the corpus rotted the first time.

Scope: this module drives fixtures/validator-negative/expected-rejections.json (the
must-reject index for coordinator_core.frontmatter.schema_validate's handoff and
cross-repo-memo validation paths). It deliberately does NOT drive
fixtures/validator-negative/c2-expected-reachability.json — those c2-*.md fixtures are
shape-VALID per schema_validate and are rejected only by a filesystem/git-aware
reachability pass that lived in the JS PreToolUse hook
(coordinator/hooks/scripts/validate-frontmatter-schema.js); that hook has no Python port
in this tree, so c2 has no oracle to re-port here (see expected-rejections.json's own
_comment for the c2 scope split). r01-diff-loc-wrong-type.json is excluded from
expected-rejections.json by original design (lint-frontmatter never processes .json
files; see README.md § NOT covered) and is excluded here for the same reason.

Known gaps surfaced by this port (see module-level xfail blocks below):
  - completion-entry (c01-c05): the completion-entry schema was never vendored under
    coordinator_core/frontmatter/schemas/ — validate() raises ValueError("unknown
    schema") rather than returning a rejection. Confirmed via `git log` that
    coordinator/schemas/completion-entry.schema.json never existed anywhere in claude-klabauter's
    history; the only trace is the orphaned coordinator/bin/tests/test-schema-completion-entry.js
    (also never ported). This is an unported schema, not a drifted one.
  - c3-01/c3-02: added wholesale by a05cae48 (test-suite adoption) with no owning index
    or oracle anywhere in this tree or in schema.js's history — orphaned fixtures.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import (
    parse_frontmatter,
    validate,
    validate_memo_cross_fields,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "validator-negative"
_INDEX_PATH = _FIXTURES_DIR / "expected-rejections.json"
_C2_INDEX_PATH = _FIXTURES_DIR / "c2-expected-reachability.json"

_INDEX = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
_C2_INDEX = json.loads(_C2_INDEX_PATH.read_text(encoding="utf-8"))

# completion-entry has no vendored Python schema (see module docstring) — validate()
# always raises ValueError for these, never returns a rejection verdict.
_UNPORTED_SCHEMA_FIXTURES = {
    "c01-loe-wrong-type.md",
    "c02-loe-tshirt-invalid-enum.md",
    "c03-nature-invalid-enum.md",
    "c04-list-of-string-wrong-type.md",
    "c05-number-or-null-in-object-wrong-type.md",
}

# Fixtures present on disk that are documented as deliberately outside
# expected-rejections.json's scope, with the citation for why.
_DOCUMENTED_NON_INDEX_FIXTURES = {
    # README.md § NOT covered — lint-frontmatter.js skips .json files; validated
    # directly against the validator, never via the expected-rejections.json sweep.
    "r01-diff-loc-wrong-type.json",
}

# Non-fixture control files that live alongside the fixtures in the same directory.
_CONTROL_FILES = {"README.md", "expected-rejections.json", "c2-expected-reachability.json"}


def _load_fixture(fixture_name: str) -> dict:
    content = (_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
    parsed = parse_frontmatter(content)
    return parsed["frontmatter"]


def _validate(schema_name: str, fm: dict) -> dict:
    """Run fm through the schema_name-appropriate validator, normalized to {"ok", "errors"}.

    cross-repo-memo has no vendored schema.json (validate_memo_cross_fields runs
    cross-field rules only, by design — see its docstring); every other indexed schema
    name is a vendored JSON-Schema-backed schema and goes through validate() directly.
    """
    if schema_name == "cross-repo-memo":
        errors = validate_memo_cross_fields(fm)
        return {"ok": not errors, "errors": errors}
    return validate(schema_name, fm)


def _xfail_reason(fixture_name: str) -> str | None:
    if fixture_name in _UNPORTED_SCHEMA_FIXTURES:
        return (
            f"{fixture_name}: schema 'completion-entry' is not vendored under "
            "coordinator_core/frontmatter/schemas/ — validate() raises ValueError"
            "('unknown schema') rather than rejecting. Unported schema, not validator "
            "drift; see module docstring."
        )
    return None


def _make_param(entry: dict) -> pytest.param:
    fixture_name = entry["fixture"]
    reason = _xfail_reason(fixture_name)
    # Review: code-reviewer — narrowed to `match=r'unknown schema'` so this
    # xfail only absorbs the specific, currently-known unported-schema gap.
    # Matching on `raises=ValueError` alone (the prior form) would also
    # silently swallow a differently-shaped ValueError raised post-vendoring
    # (a JSON-decode error, a malformed $ref, etc.) — masking a real
    # validator regression on the five C-TYPE-* rejection assertions these
    # fixtures exist to pin, instead of failing loud.
    marks = (
        [pytest.mark.xfail(raises=ValueError, match=r"unknown schema", strict=True, reason=reason)]
        if reason
        else []
    )
    return pytest.param(entry, id=fixture_name, marks=marks)


@pytest.mark.parametrize("entry", [_make_param(e) for e in _INDEX["fixtures"]])
def test_fixture_is_rejected(entry: dict) -> None:
    """Every fixture indexed in expected-rejections.json must fail validation.

    This is the AC5b all-false invariant: expect_ok is false for every entry in the
    index (asserted below as a guard against a future entry silently flipping it), so
    the assertion collapses to "the validator rejects this fixture."
    """
    assert entry["expect_ok"] is False, (
        f"{entry['fixture']}: expected_rejections.json declares expect_ok=true — "
        "this index's invariant is 'every fixture is a must-reject case'; a true "
        "entry here means the fixture belongs somewhere else, not in this index."
    )
    fm = _load_fixture(entry["fixture"])
    result = _validate(entry["schema"], fm)
    assert result["ok"] is False, (
        f"{entry['fixture']} ({entry['rule_covered']}): validator ACCEPTED a known-bad "
        f"record. Errors returned: {result.get('errors')}"
    )


def test_index_entries_exist_on_disk() -> None:
    missing = [
        entry["fixture"]
        for entry in _INDEX["fixtures"]
        if not (_FIXTURES_DIR / entry["fixture"]).is_file()
    ]
    assert not missing, f"expected-rejections.json cites fixtures absent from disk: {missing}"


def test_c2_index_entries_exist_on_disk() -> None:
    missing = [
        entry["fixture"]
        for entry in _C2_INDEX["fixtures"]
        if not (_FIXTURES_DIR / entry["fixture"]).is_file()
    ]
    assert not missing, f"c2-expected-reachability.json cites fixtures absent from disk: {missing}"


# c3-01/c3-02 were added wholesale by a05cae48 (coordinator test-suite adoption) with no
# owning index anywhere in this tree, in DoE-claude's schema.js history, or in the
# retired PreToolUse hook. Named and carved out here (not silently swallowed) so a NEW
# unindexed fixture still fails this test loud — the whole point of this check — while
# this one pre-existing, out-of-scope gap doesn't permanently red the suite. Fixing it
# for real requires authoring a net-new C3 origin_handoff/predecessor cross-field rule;
# reported as a finding for the EM/PM, not fixed in this test.
_ORPHANED_UNINDEXED_FIXTURES = {
    "c3-01-non-spinoff-origin-predecessor-conflated.md",
    "c3-02-non-spinoff-origin-predecessor-distinct-positive.md",
}


def test_every_fixture_is_indexed() -> None:
    on_disk = {
        p.name
        for p in _FIXTURES_DIR.iterdir()
        if p.is_file() and p.name not in _CONTROL_FILES
    }
    indexed = (
        {e["fixture"] for e in _INDEX["fixtures"]}
        | {e["fixture"] for e in _C2_INDEX["fixtures"]}
        | _DOCUMENTED_NON_INDEX_FIXTURES
        | _ORPHANED_UNINDEXED_FIXTURES
    )
    unindexed = sorted(on_disk - indexed)
    assert not unindexed, (
        "Fixture(s) on disk with no owning index entry (expected-rejections.json, "
        f"c2-expected-reachability.json, or a documented exclusion): {unindexed}. "
        "An unindexed fixture provides no coverage — either index it or delete it."
    )

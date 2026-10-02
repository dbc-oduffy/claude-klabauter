"""Legacy flat ``scoped_to_*`` memo keys validate against the nested ``scoped_to`` rules."""

from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import (
    fold_flat_scoped_to,
    validate_memo_cross_fields,
)

_BASE = {"created": "2026-10-05", "status": "open", "kind": "ask", "summary": "s"}


def _scoped_errors(fm: dict) -> list[dict]:
    return [e for e in validate_memo_cross_fields(fm) if e["field"] == "scoped_to"]


def test_fold_builds_nested_mapping_without_mutating_input():
    fm = {"scoped_to_artifact": "a.py", "scoped_to_sha": "abc1234", "scoped_to_seam": "s"}
    folded = fold_flat_scoped_to(fm)
    assert folded["scoped_to"] == {"artifact": "a.py", "sha": "abc1234", "seam": "s"}
    assert "scoped_to" not in fm


def test_nested_scoped_to_wins_over_flat_keys():
    fm = {"scoped_to": {"artifact": "n"}, "scoped_to_artifact": "flat"}
    assert fold_flat_scoped_to(fm) is fm


def test_flat_only_memo_with_malformed_sha_fails():
    fm = {**_BASE, "scoped_to_artifact": "a.py", "scoped_to_sha": "not-hex!", "scoped_to_seam": "s"}
    assert _scoped_errors(fm)


def test_flat_only_memo_with_well_formed_triple_passes():
    fm = {**_BASE, "scoped_to_artifact": "a.py", "scoped_to_sha": "abc1234", "scoped_to_seam": "s"}
    assert not _scoped_errors(fm)


def test_nested_malformed_sha_fails_and_well_formed_passes():
    bad = {**_BASE, "scoped_to": {"artifact": "a", "sha": "zz", "seam": "s"}}
    good = {**_BASE, "scoped_to": {"artifact": "a", "version": "1.0", "seam": "s"}}
    assert _scoped_errors(bad)
    assert not _scoped_errors(good)


def test_memo_without_scoped_to_is_unaffected():
    assert not _scoped_errors(dict(_BASE))


def test_memo_created_before_cutover_is_exempt():
    fm = {**_BASE, "created": "2026-09-24", "scoped_to_sha": "HEAD"}
    assert not _scoped_errors(fm)

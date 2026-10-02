"""``validate_outbox_frontmatter`` accepts nested ``scoped_to`` and the flat authoring alias."""

from __future__ import annotations

from coordinator_core.ops.fleet._outbox_frontmatter_rules import (
    OUTBOX_REQUIRED_FIELDS,
    validate_outbox_frontmatter,
)


def _draft(**extra) -> dict:
    fm = {field: "x" for field in OUTBOX_REQUIRED_FIELDS}
    fm.update(status="draft", kind="ask", **extra)
    return fm


def test_nested_scoped_to_complete_triple_is_valid():
    fm = _draft(scoped_to={"artifact": "a", "sha": "abc1234", "seam": "s"})
    assert validate_outbox_frontmatter(fm) == []


def test_nested_scoped_to_malformed_sha_is_refused():
    fm = _draft(scoped_to={"artifact": "a", "sha": "nothex", "seam": "s"})
    assert validate_outbox_frontmatter(fm)


def test_flat_alias_still_validates():
    ok = _draft(scoped_to_artifact="a", scoped_to_sha="abc1234", scoped_to_seam="s")
    bad = _draft(scoped_to_artifact="a", scoped_to_sha="nothex", scoped_to_seam="s")
    assert validate_outbox_frontmatter(ok) == []
    assert validate_outbox_frontmatter(bad)


def test_incomplete_scoped_to_error_names_the_nested_keys_not_flat_ones():
    errors = validate_outbox_frontmatter(_draft(scoped_to={"artifact": "a"}))
    assert errors
    assert "scoped_to.artifact" in errors[0]
    assert "scoped_to_artifact" not in errors[0]

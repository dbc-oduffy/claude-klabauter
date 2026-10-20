"""Reply-flavoured memo kinds get a receiver-tolerance hint; other bad kinds do not."""
import pytest

from coordinator_core.frontmatter.schema_validate import validate_memo_cross_fields


def _kind_findings(kind):
    fm = {"created": "2026-10-08"}
    if kind is not None:
        fm["kind"] = kind
    return [e for e in validate_memo_cross_fields(fm) if e.get("field") == "kind"]


@pytest.mark.parametrize("kind", ["reply", "consult-reply"])
def test_reply_flavoured_kind_hint_names_in_reply_to(kind):
    found = _kind_findings(kind)
    assert len(found) == 1
    assert "in_reply_to" in found[0]["hint"]
    assert "ask" in found[0]["hint"]


def test_bogus_kind_hint_omits_in_reply_to():
    found = _kind_findings("bogus")
    assert len(found) == 1
    assert "in_reply_to" not in found[0]["hint"]


@pytest.mark.parametrize("kind", ["fyi", None])
def test_valid_or_absent_kind_yields_no_finding(kind):
    assert _kind_findings(kind) == []

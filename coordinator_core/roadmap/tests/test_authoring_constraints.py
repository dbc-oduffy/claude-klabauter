"""Pins the authoring-constraints registry, waiver reader and renderer."""

from pathlib import Path

import pytest

from coordinator_core.roadmap import authoring_constraints as ac

REPO = Path(__file__).resolve().parents[3]


def test_every_id_is_a_kind_the_gate_source_emits():
    for c in ac.CONSTRAINTS:
        path = c.source.split()[-1]
        text = (REPO / path).read_text(encoding="utf-8")
        assert f'"{c.id}' in text, f"{c.id} not a literal in {path}"


def test_required_ids_present_and_entries_complete():
    ids = set(ac.constraint_ids())
    assert {
        "register-rows-unclaimed", "register-claims-missing", "anchor-premises-undeclared",
        "capabilities-undeclared", "census-undeclared", "prime-exit-absent",
    } <= ids
    assert len(ids) == len(ac.CONSTRAINTS)
    for d in ac.constraints_list():
        assert all(d[k] for k in ("id", "gate", "rule", "source", "satisfy"))
        assert isinstance(d["waivable"], bool)


def test_declared_empty_forms():
    assert ac.get_constraint("anchor-premises-undeclared").declared_empty_form == "anchor_premises: []"
    assert ac.get_constraint("census-undeclared").declared_empty_form == "census: []"
    assert ac.get_constraint("capabilities-undeclared").declared_empty_form == "capabilities: []"


def test_absent_field_proceeds():
    r = ac.read_waivers({"title": "x"})
    assert r.waived == {} and r.problems == ()
    assert ac.read_waivers(None).waived == {}


def test_valid_waiver_honoured():
    r = ac.read_waivers({"constraint_waived": [{"id": "anchor-premises-undeclared", "reason": " why "}]})
    assert r.waived == {"anchor-premises-undeclared": "why"}
    assert r.problems == ()


@pytest.mark.parametrize(
    "entry,kind",
    [
        ("nope", "malformed"),
        ({"reason": "r"}, "malformed"),
        ({"id": "anchor-premises-undeclared"}, "reasonless"),
        ({"id": "anchor-premises-undeclared", "reason": "  "}, "reasonless"),
        ({"id": "made-up", "reason": "r"}, "unknown"),
        ({"id": "register-rows-unclaimed", "reason": "r"}, "non-waivable"),
    ],
)
def test_bad_entries_reported_not_honoured(entry, kind):
    r = ac.read_waivers({"constraint_waived": [entry]})
    assert r.waived == {}
    assert [p["kind"] for p in r.problems] == [kind]


def test_non_list_is_malformed():
    r = ac.read_waivers({"constraint_waived": {"id": "x"}})
    assert [p["kind"] for p in r.problems] == ["malformed"]


def test_render_names_every_id_and_waiver_form():
    text = ac.render_prompt()
    for cid in ac.constraint_ids():
        assert cid in text
    assert "constraint_waived" in text
    assert "anchor_premises: []" in text

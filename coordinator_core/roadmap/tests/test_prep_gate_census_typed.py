"""The prep gate's CENSUS class admits a typed census entry and refuses a malformed one by field.

Presence-and-shape only: whether the claimed number is still free is the census leg's question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap.prep_gate import _census, evaluate_plan


def _typed(**over) -> dict:
    entry = {
        "question": "next free migration number under supabase/migrations",
        "kind": "next-free-number",
        "dir": "supabase/migrations",
        "claims": 715,
        "result": "715",
    }
    entry.update(over)
    return entry


def _command() -> dict:
    return {"question": "how many files", "command": "ls docs | wc -l", "result": "3", "count": 3}


def test_well_formed_typed_entry_passes() -> None:
    assert _census({"census": [_typed()]})["status"] == "PASS"


def test_claims_zero_passes() -> None:
    assert _census({"census": [_typed(claims=0)]})["status"] == "PASS"


def test_mixed_command_and_typed_list_passes() -> None:
    assert _census({"census": [_command(), _typed()]}, Path("."))["status"] == "PASS"


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"kind": "bogus"}, "kind"),
        ({"dir": None}, "dir"),
        ({"claims": -1}, "claims"),
        ({"claims": True}, "claims"),
        ({"dir": "/abs/migrations"}, "dir"),
        ({"dir": "a/../b"}, "dir"),
        ({"command": "ls"}, "command"),
        ({"count": 1}, "count"),
    ],
)
def test_malformed_typed_entry_is_refused_by_field(over: dict, needle: str) -> None:
    result = _census({"census": [_typed(**over)]})
    assert result["status"] == "DEFECT"
    assert result["kind"] == "census-incomplete"
    assert needle in result["detail"]


def test_missing_dir_and_claims_are_named() -> None:
    entry = _typed()
    del entry["dir"]
    assert "dir" in _census({"census": [entry]})["detail"]
    entry = _typed()
    del entry["claims"]
    assert "claims" in _census({"census": [entry]})["detail"]


@pytest.mark.parametrize("key", ["question", "result"])
def test_typed_entry_missing_question_or_result_is_refused(key: str) -> None:
    result = _census({"census": [_typed(**{key: ""})]})
    assert result["kind"] == "census-incomplete"
    assert key in result["detail"]


def test_typed_entry_is_not_value_checked() -> None:
    """A taken number is not the gate's business; the census leg answers it."""
    assert _census({"census": [_typed(claims=1)]}, Path("."))["status"] == "PASS"


def test_whole_plan_has_no_census_or_schema_census_defect() -> None:
    text = "\n".join(
        [
            "---",
            "title: fixture",
            "status: draft",
            "kind: plan",
            "census:",
            "  - question: next free migration number under supabase/migrations",
            "    kind: next-free-number",
            "    dir: supabase/migrations",
            "    claims: 715",
            '    result: "715"',
            "---",
            "",
            "# fixture",
            "",
        ]
    )
    classes = evaluate_plan(
        Path("docs/plans/x.md"), text=text, root_names=frozenset(), siblings=()
    )["classes"]
    assert classes["CENSUS"]["status"] == "PASS"
    assert "census" not in str(classes["SCHEMA"].get("detail", "")).lower()

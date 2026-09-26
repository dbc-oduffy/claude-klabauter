
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_sizing_object_schema_violation as guard

pytestmark = [pytest.mark.cadence]

_VALID_SIZING = """\
schema: sizing-object
intent: "Do the thing, verbatim."
estimate:
  tshirt: S
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
"""


def _payload(tool_name, file_path, cwd, **tool_input_extra):
    tool_input = {"file_path": file_path}
    tool_input.update(tool_input_extra)
    return {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd}


def _assert_deny_shape(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    assert "additionalContext" not in hso
    return hso["permissionDecisionReason"]


def test_module_contract():
    assert guard.CLASS == "hard-deny"
    assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit"}
    assert isinstance(guard.PRIORITY, int)


def test_string_pm_resolution_refused(tmp_path):
    content = _VALID_SIZING + 'pm_resolution: "the PM said so"\n'
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing_path = tmp_path / "state" / "sizings" / "2026-09-25-x.yaml"
    result = guard.check(
        _payload("Write", str(sizing_path), str(tmp_path), content=content)
    )
    assert result is not None
    reason = _assert_deny_shape(result)
    assert "pm_resolution" in reason


def test_object_pm_resolution_missing_decided_on_refused(tmp_path):
    content = _VALID_SIZING + "pm_resolution:\n  wiki_home: \"here\"\n"
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing_path = tmp_path / "state" / "sizings" / "2026-09-25-y.yaml"
    result = guard.check(
        _payload("Write", str(sizing_path), str(tmp_path), content=content)
    )
    assert result is not None
    reason = _assert_deny_shape(result)
    assert "pm_resolution" in reason
    assert "decided_on" in reason


def test_valid_pm_resolution_passes(tmp_path):
    content = _VALID_SIZING + 'pm_resolution:\n  decided_on: "2026-09-25"\n  wiki_home: "here"\n'
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing_path = tmp_path / "state" / "sizings" / "2026-09-25-z.yaml"
    result = guard.check(
        _payload("Write", str(sizing_path), str(tmp_path), content=content)
    )
    assert result is None


def test_valid_sizing_object_no_pm_resolution_passes(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing_path = tmp_path / "state" / "sizings" / "2026-09-25-w.yaml"
    result = guard.check(
        _payload("Write", str(sizing_path), str(tmp_path), content=_VALID_SIZING)
    )
    assert result is None


def test_non_sizing_doc_unchanged(tmp_path):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    other_path = tmp_path / "state" / "handoffs" / "2026-09-25-x.md"
    result = guard.check(
        _payload(
            "Write",
            str(other_path),
            str(tmp_path),
            content='pm_resolution: "bad shape but irrelevant here"\n',
        )
    )
    assert result is None


def test_non_yaml_extension_under_sizings_unchanged(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    other_path = tmp_path / "state" / "sizings" / "README.md"
    result = guard.check(
        _payload("Write", str(other_path), str(tmp_path), content="not a sizing")
    )
    assert result is None


def test_non_write_tool_unchanged(tmp_path):
    result = guard.check({"tool_name": "Read", "tool_input": {}, "cwd": str(tmp_path)})
    assert result is None

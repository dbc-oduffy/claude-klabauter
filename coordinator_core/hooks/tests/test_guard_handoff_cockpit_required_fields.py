"""Tests for the handoff write guard's cockpit-required-field advisory."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import guard_handoff_summary_cap_on_write as g
from coordinator_core.ops.emit.sections._cockpit_required import REQUIRED_STRING_FIELDS

FULL = {"title": "T", "created": "2026-10-16", "status": "open", "deployment_state": "pending"}


def _content(**overrides) -> str:
    fm = {**FULL, **overrides}
    lines = [f"{k}: {v}" for k, v in fm.items() if v is not None]
    return "---\n" + "\n".join(lines) + "\n---\nbody\n"


def _write(tmp_path, rel, content):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    params = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    return g._handler(params)


def _context(result) -> str:
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.mark.parametrize("field", REQUIRED_STRING_FIELDS)
@pytest.mark.parametrize("rel", ["state/handoffs/2026-10-16-x.md", "archive/handoffs/2026-09/x.md"])
def test_missing_field_is_named_with_scaffold_command(tmp_path, rel, field):
    ctx = _context(_write(tmp_path, rel, _content(**{field: None})))
    assert field in ctx
    assert "coordinator-doc-new --type handoff" in ctx


def test_all_missing_fields_named(tmp_path):
    ctx = _context(_write(tmp_path, "state/handoffs/x.md", "---\nsummary: s\n---\nbody\n"))
    for field in REQUIRED_STRING_FIELDS:
        assert field in ctx


def test_conformant_record_is_silent(tmp_path):
    assert _write(tmp_path, "state/handoffs/x.md", _content()) == {}


def test_conformant_record_with_unquoted_yaml_date_is_silent(tmp_path):
    assert _write(tmp_path, "archive/handoffs/x.md", _content(created="2026-10-16")) == {}


def test_out_of_scope_path_is_silent(tmp_path):
    assert _write(tmp_path, "docs/other/x.md", "---\nsummary: s\n---\nbody\n") == {}


def test_over_cap_and_missing_both_reported(tmp_path):
    ctx = _context(_write(tmp_path, "state/handoffs/x.md", _content(deployment_state=None, summary="x" * 150)))
    assert "150 chars" in ctx
    assert "deployment_state" in ctx


def test_guard_holds_no_second_required_field_list():
    import inspect

    src = inspect.getsource(g)
    assert "deployment_state" not in src

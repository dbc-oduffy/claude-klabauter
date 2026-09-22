"""Behavioral tests for coordinator_core.write_guards.nudge_dangling_sizing_citation
-- the advisory guard for a hand-authored `docs/plans/*.md` frontmatter
`sizing_object` citation that does not resolve on disk.

Spec: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C4 (AC5).

Covers, in priority order:
  1. Write of a new `docs/plans/*.md` with a schema-shaped but non-resolving
     `sizing_object` -> fires (the primary AC).
  2. `sizing_object` that resolves on disk -> silent.
  3. Edit that changes an already-dangling `sizing_object` value -> fires.
  4. Edit whose pre-image already cited the same dangling value, unchanged
     by this edit -> silent (no re-nag).
  5. Value not shaped like `state/sizings/*.yaml` -> silent (schema's remit).
  6. Non-`docs/plans/` path (e.g. `docs/problems/`) -> silent regardless of
     content -- scope is frontmatter-only, `docs/plans/*.md` only.
  7. Malformed/absent frontmatter, stale Edit `old_string` -> silent, no
     exception.
  Plus: never emits a permissionDecision; module contract
  (CLASS/PRIORITY/MATCHERS).
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.write_guards import nudge_dangling_sizing_citation as guard


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


def _plan_text(sizing_object: str) -> str:
    return f"---\nstatus: open\nsizing_object: {sizing_object}\n---\n\nBody.\n"


def test_module_contract() -> None:
    assert guard.CLASS == "advisory"
    assert guard.MATCHERS == ["Write", "Edit", "MultiEdit"]
    assert isinstance(guard.PRIORITY, int)


def test_write_dangling_sizing_object_fires(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": _plan_text("state/sizings/does-not-exist.yaml"),
        },
        "cwd": str(tmp_path),
    }
    result = guard.check(payload)
    assert result is not None
    text = _advisory_text(result)
    assert "state/sizings/does-not-exist.yaml" in text
    assert "coordinator:sizing" in text


def test_write_resolving_sizing_object_silent(tmp_path: Path) -> None:
    sizing_dir = tmp_path / "state" / "sizings"
    sizing_dir.mkdir(parents=True)
    (sizing_dir / "real.yaml").write_text("x: 1\n", encoding="utf-8")
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": _plan_text("state/sizings/real.yaml"),
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_edit_changes_to_new_dangling_value_fires(tmp_path: Path) -> None:
    sizing_dir = tmp_path / "state" / "sizings"
    sizing_dir.mkdir(parents=True)
    (sizing_dir / "real.yaml").write_text("x: 1\n", encoding="utf-8")
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_plan_text("state/sizings/real.yaml"), encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "sizing_object: state/sizings/real.yaml",
            "new_string": "sizing_object: state/sizings/repointed-missing.yaml",
        },
        "cwd": str(tmp_path),
    }
    result = guard.check(payload)
    assert result is not None
    assert "repointed-missing.yaml" in _advisory_text(result)


def test_edit_unchanged_dangling_value_silent_no_renag(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_plan_text("state/sizings/missing.yaml"), encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "Body.",
            "new_string": "Body edited, unrelated to sizing_object.",
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_non_schema_shaped_value_silent(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": _plan_text("not-a-sizing-path.txt"),
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_non_plan_path_silent(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "problems" / "2026-09-19-example.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": _plan_text("state/sizings/missing.yaml"),
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_malformed_frontmatter_silent(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": "No frontmatter here at all.\n",
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_edit_stale_old_string_silent(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "plans" / "2026-09-19-example.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_plan_text("state/sizings/missing.yaml"), encoding="utf-8")

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(target),
            "old_string": "this text is not present in the file",
            "new_string": "replacement",
        },
        "cwd": str(tmp_path),
    }
    assert guard.check(payload) is None


def test_never_raises_on_unexpected_shape() -> None:
    assert guard.check({}) is None
    assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None

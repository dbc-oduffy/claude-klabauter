"""A `needs_slot` row is named in the emitted script for the Group EM."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import emit_script

from .conftest import REVIEW_KW


def _row(row_id: str, extra: str = "") -> str:
    return (
        f"- id: {row_id}\n  title: t\n  change_kind: script-edit\n"
        f"  surface: pkg/{row_id}.py\n  writes:\n    - pkg/{row_id}.py\n  disposition: open\n"
        f"{extra}  body: |\n    Do it.\n"
    )


def _plan(tmp_path, *rows):
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "p.md"
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n```yaml plan-tasks\n"
        + "".join(rows)
        + "```\n",
        encoding="utf-8",
    )
    return path


def test_slot_rows_are_named(tmp_path):
    plan = _plan(tmp_path, _row("A1", "  needs_slot: true\n"), _row("B1"))
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    line = next(l for l in script.splitlines() if "needs_slot" in l)
    assert "A1" in line and "B1" not in line


def test_no_slot_rows_no_narration(tmp_path):
    plan = _plan(tmp_path, _row("A1"), _row("B1", "  needs_slot: false\n"))
    assert "needs_slot" not in emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_a_slot_row_brief_is_author_only_and_drops_its_build_gate(tmp_path):
    plan = _plan(tmp_path, _row("A1", "  needs_slot: true\n"), _row("B1"))
    text = plan.read_text(encoding="utf-8").replace(
        "title: p\n", "title: p\nrow_build_gate:\n  - command: build-plugin.ps1\n"
    )
    plan.write_text(text, encoding="utf-8")
    script = emit_script(plan, repo_root=tmp_path, **REVIEW_KW)
    assert script.count("Slot row (needs_slot): author only") == 1
    assert script.count("Build gate (mandatory)") == 1  # B1 keeps its gate; A1's moves to the EM

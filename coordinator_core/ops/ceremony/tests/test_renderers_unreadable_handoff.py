"""
coordinator_core.ops.ceremony.tests.test_renderers_unreadable_handoff — failure-path
coverage for _collect_handoffs_with_parse_errors' unreadable-file handling.

BEHAVIOUR: diagnostic-only fix (2026-07-22) — _collect_handoffs_with_parse_errors'
own docstring promises "frontmatter=None rows surfaced, not dropped", but an
unreadable handoff file was silently dropped via a bare `continue`, contradicting
that promise. Now it appends the same {"path": ..., "frontmatter": None} stub the
parse-error path already uses, restoring the documented contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.renderers import _collect_handoffs_with_parse_errors


@pytest.mark.skipif(os.name == "nt", reason="chmod-based unreadable-file fixture is POSIX-only")
def test_unreadable_handoff_surfaced_as_parse_error_stub(tmp_path):
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    readable = handoffs_dir / "readable.md"
    readable.write_text("---\nsession_id: abc123\n---\nbody\n", encoding="utf-8")

    blocked = handoffs_dir / "blocked.md"
    blocked.write_text("---\nsession_id: def456\n---\nbody\n", encoding="utf-8")
    os.chmod(blocked, 0o000)

    try:
        results = _collect_handoffs_with_parse_errors(tmp_path)
    finally:
        os.chmod(blocked, 0o644)

    paths = {r["path"] for r in results}
    assert any("blocked.md" in p for p in paths)

    blocked_row = next(r for r in results if "blocked.md" in r["path"])
    assert blocked_row["frontmatter"] is None

    readable_row = next(r for r in results if "readable.md" in r["path"])
    assert readable_row["frontmatter"] is not None

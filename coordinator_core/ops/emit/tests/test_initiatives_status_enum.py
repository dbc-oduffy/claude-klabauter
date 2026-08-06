"""Unit tests for the 4-value canonical InitiativeStatus enum in the initiatives section porter.

Asserts:
  (a) Each canonical value (active | paused | shipped | abandoned) passes through un-coerced.
  (b) A non-canonical value (e.g. "archived", "complete") coerces to null at emit.

These tests exercise ``sections.initiatives.collect()`` directly — no full emit() call, no
vendor-pin requirement.

Spec backlink: cross-repo/inbox/2026-07-05-initiative-govern-sweep-shape-outcome.md § Ask 1 (canonical enum, PM-ratified 2026-07-04)
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.initiatives import collect


# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> EmitContext:
    """Return a minimal EmitContext pointing at tmp_path as the central state root."""
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path,
        git_branch="test-branch",
        git_sha="0000000000000000000000000000000000000000",
        git_sha_short="00000000",
        observed_at="2026-07-05T00:00:00Z",
        hostname="test-host",
        repo_name="test-repo",
    )


def _write_initiative(initiatives_dir: Path, stem: str, status: str | None) -> None:
    """Write a minimal valid initiative YAML with the given status value."""
    status_line = f"status: {status}" if status is not None else ""
    content = textwrap.dedent(f"""\
        id: {stem}
        label: "Test initiative {stem}"
        {status_line}
        owner: test-em
    """)
    (initiatives_dir / f"{stem}.yaml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("canonical_status", ["active", "paused", "shipped", "abandoned"])
def test_canonical_status_passes_through(tmp_path: Path, canonical_status: str) -> None:
    """Each canonical InitiativeStatus value is emitted un-coerced (not coerced to null)."""
    ini_dir = tmp_path / "initiatives"
    ini_dir.mkdir()
    _write_initiative(ini_dir, f"ini-{canonical_status}", canonical_status)

    ctx = _make_ctx(tmp_path)
    records, malformed = collect(ctx)

    assert malformed == [], f"Unexpected malformed records: {malformed}"
    assert len(records) == 1
    assert records[0]["status"] == canonical_status, (
        f"Expected status={canonical_status!r} to pass through un-coerced, "
        f"got {records[0]['status']!r}"
    )


@pytest.mark.parametrize("non_canonical_status", ["archived", "complete", "draft", ""])
def test_non_canonical_status_coerces_to_null(tmp_path: Path, non_canonical_status: str) -> None:
    """Non-canonical on-disk status values coerce to null at emit (D9 coercion semantics)."""
    ini_dir = tmp_path / "initiatives"
    ini_dir.mkdir()
    # Write the status value directly into the YAML; empty string is the null-sentinel case
    stem = "ini-noncanonn"
    if non_canonical_status == "":
        # Empty-string status — the YAML parser treats it as null, so status is absent
        content = textwrap.dedent(f"""\
            id: {stem}
            label: "Test initiative {stem}"
            status:
            owner: test-em
        """)
    else:
        content = textwrap.dedent(f"""\
            id: {stem}
            label: "Test initiative {stem}"
            status: {non_canonical_status}
            owner: test-em
        """)
    (ini_dir / f"{stem}.yaml").write_text(content, encoding="utf-8")

    ctx = _make_ctx(tmp_path)
    records, malformed = collect(ctx)

    assert malformed == [], f"Unexpected malformed records: {malformed}"
    assert len(records) == 1
    assert records[0]["status"] is None, (
        f"Expected non-canonical status={non_canonical_status!r} to coerce to null, "
        f"got {records[0]['status']!r}"
    )

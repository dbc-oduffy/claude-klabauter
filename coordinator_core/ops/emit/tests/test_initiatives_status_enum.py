
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections.initiatives import collect


def _make_ctx(tmp_path: Path) -> EmitContext:
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
    status_line = f"status: {status}" if status is not None else ""
    content = textwrap.dedent(f"""\
        id: {stem}
        label: "Test initiative {stem}"
        {status_line}
        owner: test-em
    """)
    (initiatives_dir / f"{stem}.yaml").write_text(content, encoding="utf-8")


@pytest.mark.parametrize("canonical_status", ["active", "paused", "shipped", "abandoned"])
def test_canonical_status_passes_through(tmp_path: Path, canonical_status: str) -> None:
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
    ini_dir = tmp_path / "initiatives"
    ini_dir.mkdir()
    stem = "ini-noncanonn"
    if non_canonical_status == "":
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

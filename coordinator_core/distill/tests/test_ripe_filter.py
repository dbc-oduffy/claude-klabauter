"""
coordinator_core.distill.tests.test_ripe_filter

Unit tests for coordinator_core.distill.ripe_filter -- the pure frontmatter-scan RIPE
partition script (C1).

Coverage:
  scan_spec_dir, mixed-status fixture:
    (a) status: implemented -> harvest
    (b) status: shipped -> harvest
    (c) status: superseded -> skip, reason names the status
    (d) status: abandoned -> skip, reason names the status
    (e) status: partial -> skip, reason names the status
    (f) status: draft (not ripe, not an explicit skip status) -> skip, "not yet ripe" reason
    (g) missing status field entirely -> skip, "no status field" reason
    (h) missing frontmatter block entirely -> skip, "no frontmatter block" reason
    (i) non-.md files in the dir are ignored
    (j) harvest and skip lists are both sorted (deterministic output)
  recursion (month-foldered archive layout):
    (n) month-nested layout (specs/2026-07/foo.md) is scanned in one invocation, with
        output paths relative to the scan root using `/` separators on every platform
    (o) mixed layout (flat .md files alongside YYYY-MM/ subdirs) partitions both levels
  to_dict shape:
    (k) RipeFilterResult.to_dict() matches the plan-specified
        {harvest: [...], skip: [{path, status, reason}], sidecars: [...]} shape exactly
  CLI smoke:
    (l) bin/distill-ripe-filter.py invoked as a subprocess against the fixture dir emits
        valid JSON on stdout with the expected harvest/skip partition
    (m) bin/distill-ripe-filter.py exits non-zero on a non-existent directory
  sidecar cohort (F5, C4):
    (p) a sidecar-suffixed filename (e.g. `foo.review.md`) with `status: implemented`
        frontmatter lands in `sidecars`, never in `harvest`
    (q) a sidecar-suffixed filename with no frontmatter at all also lands in `sidecars`,
        never in `skip`
    (r) sidecars are excluded from harvest/skip entirely (three-way disjoint partition)
    (s) sidecars list is sorted
    (t) sidecars survive month-nested recursion with rel-posix paths

Spec backlink: docs/plans/2026-07-12-distill-ceremony-mechanical-substrate-joint-design.md § C1
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.distill.ripe_filter import (
    RIPE_STATUSES,
    SKIP_STATUSES,
    RipeFilterResult,
    SkipRecord,
    scan_spec_dir,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "bin" / "distill-ripe-filter.py"


def _write_spec(spec_dir: Path, filename: str, *, status: str | None, include_fm: bool = True) -> None:
    if not include_fm:
        (spec_dir / filename).write_text(f"# {filename}\n\nNo frontmatter here.\n", encoding="utf-8")
        return
    fm_lines = ["---", f"title: {filename}"]
    if status is not None:
        fm_lines.append(f"status: {status}")
    fm_lines.append("---")
    text = "\n".join(fm_lines) + f"\n\n# {filename}\n\nBody text.\n"
    (spec_dir / filename).write_text(text, encoding="utf-8")


@pytest.fixture
def mixed_status_spec_dir(tmp_path: Path) -> Path:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()

    _write_spec(spec_dir, "a-implemented.md", status="implemented")
    _write_spec(spec_dir, "b-shipped.md", status="shipped")
    _write_spec(spec_dir, "c-superseded.md", status="superseded")
    _write_spec(spec_dir, "d-abandoned.md", status="abandoned")
    _write_spec(spec_dir, "e-partial.md", status="partial")
    _write_spec(spec_dir, "f-draft.md", status="draft")
    _write_spec(spec_dir, "g-no-status.md", status=None)
    _write_spec(spec_dir, "h-no-frontmatter.md", status=None, include_fm=False)
    # Non-.md file -- must be ignored entirely.
    (spec_dir / "i-not-markdown.txt").write_text("ignore me\n", encoding="utf-8")

    return spec_dir


def test_ripe_statuses_harvest(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    assert "a-implemented.md" in result.harvest
    assert "b-shipped.md" in result.harvest


def test_skip_statuses_carry_reason(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    skip_by_path = {r.path: r for r in result.skip}

    assert skip_by_path["c-superseded.md"].status == "superseded"
    assert "superseded" in skip_by_path["c-superseded.md"].reason

    assert skip_by_path["d-abandoned.md"].status == "abandoned"
    assert "abandoned" in skip_by_path["d-abandoned.md"].reason

    assert skip_by_path["e-partial.md"].status == "partial"
    assert "partial" in skip_by_path["e-partial.md"].reason


def test_non_ripe_non_skip_status_falls_to_skip(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    skip_by_path = {r.path: r for r in result.skip}

    assert "f-draft.md" not in result.harvest
    assert skip_by_path["f-draft.md"].status == "draft"
    assert "not yet ripe" in skip_by_path["f-draft.md"].reason


def test_missing_status_field(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    skip_by_path = {r.path: r for r in result.skip}

    assert skip_by_path["g-no-status.md"].status is None
    assert "no status field" in skip_by_path["g-no-status.md"].reason


def test_missing_frontmatter_block(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    skip_by_path = {r.path: r for r in result.skip}

    assert skip_by_path["h-no-frontmatter.md"].status is None
    assert "no frontmatter" in skip_by_path["h-no-frontmatter.md"].reason


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_spec_dir_fails_loud(tmp_path: Path, caplog) -> None:
    """A permission-denied spec_dir logs a WARNING and raises OSError, rather than
    silently returning a well-formed empty partition — glob("*.md")'s selector would
    otherwise swallow the PermissionError and make a blocked spec dir indistinguishable
    from a genuinely-empty one."""
    spec_dir = tmp_path / "blocked-specs"
    spec_dir.mkdir()
    (spec_dir / "unreachable.md").write_text(
        "---\nstatus: implemented\n---\nBody.\n", encoding="utf-8"
    )

    original_mode = spec_dir.stat().st_mode
    os.chmod(spec_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.distill.ripe_filter"):
            with pytest.raises(OSError):
                scan_spec_dir(spec_dir)
    finally:
        os.chmod(spec_dir, original_mode)

    dir_warnings = [
        r
        for r in caplog.records
        if str(spec_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable spec_dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_non_markdown_files_ignored(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    all_paths = set(result.harvest) | {r.path for r in result.skip}
    assert "i-not-markdown.txt" not in all_paths


def test_full_partition_is_exhaustive_and_disjoint(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    harvest_set = set(result.harvest)
    skip_set = {r.path for r in result.skip}

    assert harvest_set.isdisjoint(skip_set)
    # 8 .md fixture files total (a..h); the .txt is excluded.
    assert len(harvest_set) + len(skip_set) == 8


def test_harvest_and_skip_are_sorted(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    assert result.harvest == sorted(result.harvest)
    skip_paths = [r.path for r in result.skip]
    assert skip_paths == sorted(skip_paths)


def test_ripe_and_skip_status_sets_do_not_overlap() -> None:
    assert RIPE_STATUSES.isdisjoint(SKIP_STATUSES)


def test_to_dict_shape(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    d = result.to_dict()

    assert set(d.keys()) == {"harvest", "skip", "sidecars"}
    assert isinstance(d["harvest"], list)
    assert all(isinstance(p, str) for p in d["harvest"])
    assert isinstance(d["skip"], list)
    for row in d["skip"]:
        assert set(row.keys()) == {"path", "status", "reason"}
    assert isinstance(d["sidecars"], list)
    assert all(isinstance(p, str) for p in d["sidecars"])


def test_to_dict_json_serializable(mixed_status_spec_dir: Path) -> None:
    result = scan_spec_dir(mixed_status_spec_dir)
    # Must not raise.
    serialized = json.dumps(result.to_dict())
    reloaded = json.loads(serialized)
    assert reloaded["harvest"] == result.to_dict()["harvest"]


def test_empty_dir_yields_empty_partition(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = scan_spec_dir(empty_dir)
    assert result.harvest == []
    assert result.skip == []


def test_skip_record_is_frozen_dataclass() -> None:
    rec = SkipRecord(path="x.md", status="draft", reason="status: draft (not yet ripe)")
    with pytest.raises(Exception):
        rec.path = "y.md"  # type: ignore[misc]


def test_result_is_frozen_dataclass() -> None:
    result = RipeFilterResult(harvest=[], skip=[])
    with pytest.raises(Exception):
        result.harvest = ["x.md"]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Recursion -- month-foldered archive layout (archive/specs/YYYY-MM/*.md)
# ---------------------------------------------------------------------------

def test_month_nested_layout_scanned_in_one_invocation(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    month_dir = spec_dir / "2026-07"
    month_dir.mkdir(parents=True)

    _write_spec(month_dir, "foo.md", status="implemented")
    _write_spec(month_dir, "bar.md", status="superseded")

    result = scan_spec_dir(spec_dir)
    # Output paths are relative to the scan root with `/` separators, even on Windows.
    assert result.harvest == ["2026-07/foo.md"]
    assert [r.path for r in result.skip] == ["2026-07/bar.md"]
    assert result.skip[0].status == "superseded"


def test_mixed_flat_and_nested_layout(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    month_a = spec_dir / "2026-06"
    month_a.mkdir()
    month_b = spec_dir / "2026-07"
    month_b.mkdir()

    _write_spec(spec_dir, "flat-shipped.md", status="shipped")
    _write_spec(spec_dir, "flat-draft.md", status="draft")
    _write_spec(month_a, "june-implemented.md", status="implemented")
    _write_spec(month_b, "july-abandoned.md", status="abandoned")
    # Non-.md file inside a month dir -- ignored, same as at the top level.
    (month_b / "notes.txt").write_text("ignore me\n", encoding="utf-8")

    result = scan_spec_dir(spec_dir)
    assert result.harvest == ["2026-06/june-implemented.md", "flat-shipped.md"]
    skip_by_path = {r.path: r for r in result.skip}
    assert set(skip_by_path) == {"flat-draft.md", "2026-07/july-abandoned.md"}
    assert skip_by_path["2026-07/july-abandoned.md"].status == "abandoned"
    # Both output lists stay sorted across levels.
    assert result.harvest == sorted(result.harvest)
    assert [r.path for r in result.skip] == sorted(r.path for r in result.skip)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_month_subdir_fails_loud(tmp_path: Path, caplog) -> None:
    """An unreadable subdirectory is the same failure class as an unreadable spec_dir:
    fail loud, never a silently-shrunk partition."""
    spec_dir = tmp_path / "specs"
    month_dir = spec_dir / "2026-07"
    month_dir.mkdir(parents=True)
    _write_spec(spec_dir, "flat-ok.md", status="implemented")
    _write_spec(month_dir, "unreachable.md", status="implemented")

    original_mode = month_dir.stat().st_mode
    os.chmod(month_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.distill.ripe_filter"):
            with pytest.raises(OSError):
                scan_spec_dir(spec_dir)
    finally:
        os.chmod(month_dir, original_mode)


# ---------------------------------------------------------------------------
# Sidecar cohort (F5, C4) -- sidecars are checked BEFORE frontmatter classification
# and never leak into harvest/skip.
# ---------------------------------------------------------------------------

def test_sidecar_with_ripe_frontmatter_lands_in_sidecars_not_harvest(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    # A sidecar-suffixed file that happens to carry status: implemented -- must still
    # land in `sidecars`, never `harvest`.
    _write_spec(spec_dir, "foo.review.md", status="implemented")
    _write_spec(spec_dir, "real-spec.md", status="implemented")

    result = scan_spec_dir(spec_dir)
    assert result.sidecars == ["foo.review.md"]
    assert "foo.review.md" not in result.harvest
    assert result.harvest == ["real-spec.md"]


def test_sidecar_with_no_frontmatter_lands_in_sidecars_not_skip(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    _write_spec(spec_dir, "bar.v3-divergence-check.md", status=None, include_fm=False)

    result = scan_spec_dir(spec_dir)
    assert result.sidecars == ["bar.v3-divergence-check.md"]
    assert "bar.v3-divergence-check.md" not in {r.path for r in result.skip}
    assert result.skip == []


def test_sidecars_excluded_from_harvest_and_skip_three_way_disjoint(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    _write_spec(spec_dir, "a-implemented.md", status="implemented")
    _write_spec(spec_dir, "b-draft.md", status="draft")
    _write_spec(spec_dir, "c.prior-art-check.md", status="implemented")
    _write_spec(spec_dir, "d.node-map.md", status=None, include_fm=False)

    result = scan_spec_dir(spec_dir)
    harvest_set = set(result.harvest)
    skip_set = {r.path for r in result.skip}
    sidecar_set = set(result.sidecars)

    assert harvest_set.isdisjoint(skip_set)
    assert harvest_set.isdisjoint(sidecar_set)
    assert skip_set.isdisjoint(sidecar_set)
    assert harvest_set | skip_set | sidecar_set == {
        "a-implemented.md",
        "b-draft.md",
        "c.prior-art-check.md",
        "d.node-map.md",
    }


def test_sidecars_are_sorted(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    _write_spec(spec_dir, "z.review.md", status=None, include_fm=False)
    _write_spec(spec_dir, "a.review.md", status=None, include_fm=False)
    _write_spec(spec_dir, "m.v3-divergence-check.md", status=None, include_fm=False)

    result = scan_spec_dir(spec_dir)
    assert result.sidecars == sorted(result.sidecars)
    assert result.sidecars == ["a.review.md", "m.v3-divergence-check.md", "z.review.md"]


def test_sidecars_survive_month_nested_recursion(tmp_path: Path) -> None:
    spec_dir = tmp_path / "specs"
    month_dir = spec_dir / "2026-07"
    month_dir.mkdir(parents=True)

    _write_spec(month_dir, "foo.md", status="implemented")
    _write_spec(month_dir, "foo.docs-check.md", status=None, include_fm=False)

    result = scan_spec_dir(spec_dir)
    assert result.harvest == ["2026-07/foo.md"]
    assert result.sidecars == ["2026-07/foo.docs-check.md"]
    assert result.skip == []


# ---------------------------------------------------------------------------
# CLI smoke tests -- bin/distill-ripe-filter.py as a subprocess
# ---------------------------------------------------------------------------

def test_cli_emits_valid_json_partition(mixed_status_spec_dir: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), str(mixed_status_spec_dir)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "a-implemented.md" in payload["harvest"]
    assert "b-shipped.md" in payload["harvest"]
    skip_paths = {row["path"] for row in payload["skip"]}
    assert "c-superseded.md" in skip_paths


def test_cli_nonexistent_dir_exits_nonzero(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), str(missing)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode != 0

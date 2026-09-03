"""
Tests for `coordinator_core.quick_wrap_assemble._c3_ancestry`'s archive probe.

The defect under test: step 2 of the `/quick-wrap` checklist instructs the
SAME session to `ship-handoff <path> --archive` the finished chain-root
baton it just claimed, moving it to `archive/handoffs/<YYYY-MM>/`. Condition
3 then reads `pickup_kind["artifact_path"]`, which this session's own pickup
fact never re-resolved to the new location, and failed closed on a baton
that was in fact archived one step earlier in the SAME ceremony.
`_archive_handoffs_fallback` (mirroring `ops._sizing_citation`'s
`_archive_sizings_fallback`) repairs exactly that one case: a null
`artifact_path` with a `basename` that resolves to EXACTLY ONE record under
`archive/handoffs/**`.

Every other fail-closed case this condition was written for — `degraded`
classification, zero matches, an ambiguous multi-match, a traversal/absolute
basename, and an archived file that still cannot be read — must keep failing
exactly as before. That is most of what this file asserts.

Spec backlink: docs/plans/2026-09-03-close-verb-archival-stops-asking-for-wri.md § C1
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core.quick_wrap_assemble import _c3_ancestry, _closed_pickup_kind

_ROOT_BODY = (
    "---\n"
    "title: example baton\n"
    "predecessor: null\n"
    "forked_from: null\n"
    "---\n\n# Example baton\n"
)

_NON_ROOT_BODY = (
    "---\n"
    "title: example baton\n"
    'predecessor: "state/handoffs/2026-09-01-prior.md"\n'
    "forked_from: null\n"
    "---\n\n# Example baton\n"
)


def _write(root: Path, rel: str, content: str) -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _pickup(*, classification: str = "handoff", basename: str | None, artifact_path: str | None = None) -> dict[str, Any]:
    return {
        "classification": classification,
        "artifact_path": artifact_path,
        "basename": basename,
        "deliverable_id": None,
        "actioned_memos": [],
        "consumed_predecessor": False,
    }


# ---------------------------------------------------------------------------
# The repaired case: archived chain-root baton, same-basename match
# ---------------------------------------------------------------------------


def test_archived_chain_root_baton_now_passes(tmp_path: Path):
    _write(tmp_path, "archive/handoffs/2026-09/2026-09-01-baton.md", _ROOT_BODY)
    pickup = _pickup(basename="2026-09-01-baton.md")
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is True
    assert "chain root" in reason


def test_archived_non_root_baton_still_fails_condition_3(tmp_path: Path):
    _write(tmp_path, "archive/handoffs/2026-09/2026-09-01-baton.md", _NON_ROOT_BODY)
    pickup = _pickup(basename="2026-09-01-baton.md")
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False
    assert "carries ancestry" in reason


# ---------------------------------------------------------------------------
# The unrepaired cases: fail-closed survives unchanged
# ---------------------------------------------------------------------------


def test_degraded_classification_still_fails_closed(tmp_path: Path):
    _write(tmp_path, "archive/handoffs/2026-09/2026-09-01-baton.md", _ROOT_BODY)
    pickup = _closed_pickup_kind()
    assert pickup["classification"] == "degraded"
    assert pickup["basename"] is None
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False
    assert "degraded" in reason


def test_zero_archive_matches_fails_closed(tmp_path: Path):
    (tmp_path / "archive" / "handoffs" / "2026-09").mkdir(parents=True)
    pickup = _pickup(basename="2026-09-01-nowhere.md")
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False
    assert "no artifact_path resolved" in reason


def test_ambiguous_archive_match_fails_closed(tmp_path: Path):
    for month in ("2026-07", "2026-09"):
        _write(tmp_path, f"archive/handoffs/{month}/2026-09-01-baton.md", _ROOT_BODY)
    pickup = _pickup(basename="2026-09-01-baton.md")
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False
    assert "ambiguous" in reason


def test_traversal_basename_fails_closed_and_does_not_escape(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    _write(outside, "escape.md", _ROOT_BODY)
    pickup = _pickup(basename="../outside/escape.md")
    ok, reason = _c3_ancestry(pickup, root)
    assert ok is False
    assert "no artifact_path resolved" in reason


def test_no_basename_fails_closed(tmp_path: Path):
    pickup = _pickup(basename=None)
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False


def test_resolved_archived_file_that_cannot_be_read_fails_closed(tmp_path: Path):
    archived_dir = tmp_path / "archive" / "handoffs" / "2026-09"
    archived_dir.mkdir(parents=True)
    # A directory, not a file, sharing the basename that would otherwise
    # match — `_read_ancestry_fields` cannot read it, so this must still fail
    # closed once the probe hands the (unreadable) path onward.
    (archived_dir / "2026-09-01-baton.md").mkdir()
    pickup = _pickup(basename="2026-09-01-baton.md")
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is False


# ---------------------------------------------------------------------------
# Live path still wins outright — the probe never fires when artifact_path
# already resolved.
# ---------------------------------------------------------------------------


def test_live_unarchived_path_still_passes(tmp_path: Path):
    _write(tmp_path, "state/handoffs/2026-09-01-baton.md", _ROOT_BODY)
    pickup = _pickup(
        basename="2026-09-01-baton.md",
        artifact_path="state/handoffs/2026-09-01-baton.md",
    )
    ok, reason = _c3_ancestry(pickup, tmp_path)
    assert ok is True
    assert "chain root" in reason


# ---------------------------------------------------------------------------
# The root handed in is not always the root `contained_path` hands back.
# ---------------------------------------------------------------------------


def test_relative_worktree_root_resolves_instead_of_raising(tmp_path: Path, monkeypatch):
    """A relative `worktree_root` must still fail-or-pass, never raise.

    `_archive_handoffs_fallback` returns `contained_path`'s output, which is
    `.resolve()`d. Relativising that against an UNRESOLVED root raises
    ValueError — an uncaught exception standing where every other exit of
    `_c3_ancestry` returns a controlled `(False, reason)`, so the ceremony
    dies instead of refusing. A relative root reproduces it without needing
    a symlink, which is what makes this testable on Windows too; the
    symlinked-root shape (macOS `/tmp` -> `/private/tmp`, any symlinked dev
    mount) is the same bug reached by a different route.
    """
    _write(tmp_path, "archive/handoffs/2026-09/2026-09-01-baton.md", _ROOT_BODY)
    monkeypatch.chdir(tmp_path)
    pickup = _pickup(basename="2026-09-01-baton.md")
    ok, reason = _c3_ancestry(pickup, Path("."))
    assert ok is True
    assert "chain root" in reason

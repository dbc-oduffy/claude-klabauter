"""
coordinator_core.distill.tests.test_common

Unit tests for coordinator_core.distill._common — the shared barrier module for the
5 distill-ceremony scripts.

Coverage:
  parse_distillation_log:
    (a) round-trip: a well-formed canonical log with one Run header + multiple rows
    (b) multiple Run headers group rows under the correct run_id
    (c) a row with an invalid disposition is skipped, not raised
    (d) a row appearing before any Run header is skipped
    (e) ASCII "->" is required — a row using a unicode arrow does not match
    (f) fate text is captured verbatim (trimmed) between the comma and "(run: ...)"
    (f2) a fate whose own text ends in a "(run: ...)"-shaped substring pins the
        current, documented, narrow edge-case behavior (Finding 2, 2026-07-12
        code review) — not a fix, a regression pin
  SIDECAR_SUFFIXES / is_sidecar_filename:
    (g) matches a full-suffix-anchored sidecar name (<stem>.review.md)
    (h) rejects a bare substring hit ("review" appearing in the filename but not as
        the anchored suffix)
    (i) matches every suffix in SIDECAR_SUFFIXES at least once
    (j) matches a timestamped variant (already covered by the plain suffix
        endswith check — TIMESTAMPED_SIDECAR_RE was removed as dead code, see
        workflow-review P4 finding 2026-07-12)
    (k) rejects a non-timestamped-prefixed name that isn't in SIDECAR_SUFFIXES
  active_reference_guard:
    (l) returns True when ripgrep finds the needle under an existing scope dir
    (m) returns False when ripgrep finds no match
    (n) returns False (not an error) when none of the scope dirs exist
  frontmatter re-export:
    (o) split_frontmatter / read_fm_field are importable from _common and behave
        identically to the primitives module (single read-only surface, no
        divergent re-implementation)

Spec backlink: pln-distill-ceremony-mechanical-su-1bcb38 § C0
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")

from coordinator_core.distill import _common
from coordinator_core.distill._common import (
    DISPOSITIONS,
    SIDECAR_SUFFIXES,
    active_reference_guard,
    is_sidecar_filename,
    parse_distillation_log,
    read_fm_field,
    split_frontmatter,
)


# ---------------------------------------------------------------------------
# parse_distillation_log
# ---------------------------------------------------------------------------

def test_parse_single_run_multiple_rows():
    text = (
        "## Run r-001\n"
        "- docs/plans/foo.md -> DISTILLED, folded into wiki/foo.md (run: r-001)\n"
        "- docs/plans/bar.md -> SKIP, superseded by later plan (run: r-001)\n"
    )
    rows = parse_distillation_log(text)
    assert len(rows) == 2
    assert rows[0].run_id == "r-001"
    assert rows[0].path == "docs/plans/foo.md"
    assert rows[0].disposition == "DISTILLED"
    assert rows[0].fate == "folded into wiki/foo.md"
    assert rows[1].disposition == "SKIP"


def test_parse_multiple_run_headers_group_correctly():
    text = (
        "## Run r-001\n"
        "- a.md -> PROMOTE, promoted to decision (run: r-001)\n"
        "## Run r-002\n"
        "- b.md -> EPHEMERAL, scratch only (run: r-002)\n"
    )
    rows = parse_distillation_log(text)
    assert len(rows) == 2
    assert rows[0].run_id == "r-001"
    assert rows[0].path == "a.md"
    assert rows[1].run_id == "r-002"
    assert rows[1].path == "b.md"


def test_parse_invalid_disposition_skipped_not_raised():
    text = (
        "## Run r-001\n"
        "- a.md -> BOGUS, not a real disposition (run: r-001)\n"
        "- b.md -> PRESERVE, kept as-is for now (run: r-001)\n"
    )
    rows = parse_distillation_log(text)
    assert len(rows) == 1
    assert rows[0].path == "b.md"
    assert rows[0].disposition == "PRESERVE"


def test_parse_row_before_any_run_header_skipped():
    text = (
        "- orphan.md -> DISTILLED, no header above me (run: r-000)\n"
        "## Run r-001\n"
        "- a.md -> DISTILLED, has a header (run: r-001)\n"
    )
    rows = parse_distillation_log(text)
    assert len(rows) == 1
    assert rows[0].path == "a.md"


def test_parse_requires_ascii_arrow_not_unicode():
    ascii_text = "## Run r-001\n- a.md -> DISTILLED, fine (run: r-001)\n"
    unicode_text = "## Run r-001\n- a.md → DISTILLED, fine (run: r-001)\n"
    assert len(parse_distillation_log(ascii_text)) == 1
    assert len(parse_distillation_log(unicode_text)) == 0


def test_parse_fate_captured_verbatim_trimmed():
    text = "## Run r-001\n- a.md -> DISTILLED,   spaced fate text   (run: r-001)\n"
    rows = parse_distillation_log(text)
    assert rows[0].fate == "spaced fate text"


def test_dispositions_constant_matches_plan_spec():
    assert DISPOSITIONS == {"DISTILLED", "PROMOTE", "EPHEMERAL", "SKIP", "PRESERVE"}


def test_parse_fate_ending_in_run_shaped_substring_pins_documented_edge_case():
    # Review: code-reviewer (Finding 2, 2026-07-12) — pins the CURRENT (imperfect
    # but documented) behavior for a fate whose own free text ends in a literal
    # "(run: ...)"-shaped substring with no separate trailing run-id group in the
    # source row. _ROW_RE's lazy-fate + end-anchored "(run: ...)" suffix cannot
    # distinguish this from a real trailing run-id group, so the fate's own
    # parenthetical is misread as row_run_id and fate is truncated. This is a
    # known, narrow, and deliberately-not-closed edge case (see _common.py's
    # _ROW_RE comment) — this test exists so a future _ROW_RE change can't
    # silently move this boundary without a test failing.
    text = "## Run r-001\n- a.md -> DISTILLED, superseded (run: r-999)\n"
    rows = parse_distillation_log(text)
    assert len(rows) == 1
    assert rows[0].fate == "superseded"
    assert rows[0].run_id == "r-001"  # header run_id is correct — unaffected


# ---------------------------------------------------------------------------
# SIDECAR_SUFFIXES / is_sidecar_filename
# ---------------------------------------------------------------------------

def test_sidecar_full_suffix_anchored_match():
    assert is_sidecar_filename("2026-07-12-some-plan.review.md") is True


def test_sidecar_rejects_bare_substring_hit():
    # "review" appears in the filename but not as the anchored ".review.md" suffix.
    assert is_sidecar_filename("my-review-notes.md") is False
    assert is_sidecar_filename("review.md") is False


@pytest.mark.parametrize("suffix", SIDECAR_SUFFIXES)
def test_every_declared_suffix_matches(suffix):
    assert is_sidecar_filename(f"some-stem{suffix}") is True


def test_sidecar_timestamped_variant_matches():
    # Covered by the plain SIDECAR_SUFFIXES endswith check alone — a former
    # dedicated TIMESTAMPED_SIDECAR_RE regex was confirmed dead code (every
    # string it matched already satisfied endswith(SIDECAR_SUFFIXES)) and
    # removed; see workflow-review P4 finding 2026-07-12.
    assert is_sidecar_filename("2026-07-12_143022-slug.review.md") is True
    assert is_sidecar_filename("2026-07-12T14-slug.c0-findings.md") is True


def test_sidecar_rejects_non_matching_name():
    assert is_sidecar_filename("plain-design-doc.md") is False
    assert is_sidecar_filename("2026-07-12-plan.md") is False


def test_sidecar_bare_dash_check_suffix_matches():
    # C1 (2026-07-23 claude-klabauter-driven-ceremony-redesign) — confirmed real, on-disk
    # class: archive/specs/2026-07/2026-07-10-percolation-engine-claude-klabauter.v3-divergence-check.md
    # is dash-separated before ".md" ("v3-divergence-check.md"), matched via the
    # closed, named ".v3-divergence-check.md" entry (narrowed from an open-ended
    # bare "-check.md" trailing-segment match, 2026-07-23 code review).
    assert is_sidecar_filename("2026-07-10-percolation-engine-claude-klabauter.v3-divergence-check.md") is True


def test_sidecar_bare_dash_review_suffix_matches():
    # C1 — confirmed real, on-disk class: reviewer-named dash-separated review
    # sidecars (the Staff Engineer-review.md, sonnet-review.md, eng-director-review.md,
    # OVERVIEW.the Director of Engineering-review.md) live under docs/plans/ and state/review-trail/,
    # matched via the closed, named per-reviewer dotted entries (narrowed from
    # an open-ended bare "-review.md" trailing-segment match, 2026-07-23 code
    # review) rather than the generic dotted ".review.md" entry.
    assert is_sidecar_filename("2026-07-19-coverage-gate-single-graph-walk.the Staff Engineer-review.md") is True
    assert is_sidecar_filename("2026-07-19-coverage-gate-single-graph-walk.sonnet-review.md") is True
    assert is_sidecar_filename("2026-07-06-claude-klabauter-native-op-central-subject-delegation.eng-director-review.md") is True
    assert is_sidecar_filename("OVERVIEW.the Director of Engineering-review.md") is True


def test_sidecar_narrowed_suffixes_reject_plausible_non_reviewer_plan_filename():
    # 2026-07-23 code review WARN: the bare "-check.md"/"-review.md" trailing-
    # segment forms this replaced would have silently reclassified a
    # legitimately-named plan/spec as a sidecar. These are plausible real plan
    # slugs (this repo's own domain vocabulary is full of "check"/"review"/
    # "gate" nouns) that must NOT match now that the suffixes are a closed,
    # named enum of known reviewer/process tokens.
    assert is_sidecar_filename(
        "2026-07-23-hook-repoint-validation-doctrine-hooks-check.md"
    ) is False
    assert is_sidecar_filename("2026-07-10-percolation-engine-v3-delta-review.md") is False
    assert is_sidecar_filename("2026-07-01-some-plan.pre-commit-hook-check.md") is False


def test_sidecar_corpus_archive_specs_all_match_no_plan_body_matches():
    # Corpus-driven check (C1 brief) over the real on-disk archive/specs/2026-07
    # directory: every sidecar-shaped filename in it matches, and no plan-body
    # filename in it (a bare "<date>-<slug>.md" with no sidecar suffix) matches.
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    corpus_dir = repo_root / "archive" / "specs" / "2026-07"
    if not corpus_dir.is_dir():
        pytest.skip("archive/specs/2026-07 corpus dir not present")

    plan_body_seen = False
    sidecar_seen = False
    for entry in corpus_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        looks_like_sidecar = any(
            name.endswith(suffix) for suffix in SIDECAR_SUFFIXES
        )
        if looks_like_sidecar:
            sidecar_seen = True
            assert is_sidecar_filename(name) is True, name
        else:
            plan_body_seen = True
            assert is_sidecar_filename(name) is False, name

    # Sanity: the fixture directory actually exercises both branches.
    assert plan_body_seen
    assert sidecar_seen


# ---------------------------------------------------------------------------
# active_reference_guard
# ---------------------------------------------------------------------------

@_requires_rg
def test_active_reference_guard_finds_match(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "some-doc.md").write_text("references archive/specs/old-thing.md here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is True


@_requires_rg
def test_active_reference_guard_no_match(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "some-doc.md").write_text("nothing relevant here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is False


def test_active_reference_guard_absent_scope_returns_false(tmp_path):
    # No docs/, tasks/, or archive/ dirs exist under tmp_path at all.
    assert active_reference_guard("anything", tmp_path) is False


def test_active_reference_guard_raises_on_ripgrep_error(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()

    # `shutil.which` is pinned, not left ambient: since the rg-absent fallback
    # landed, the rg branch this test exercises is reachable only when ripgrep
    # is on PATH. Without the pin, the test silently takes the fallback on any
    # machine lacking rg, never calls the patched `subprocess.run`, and fails
    # with DID NOT RAISE — reporting a defect in the guard when the only thing
    # wrong is the host's PATH. Pinning makes the branch under test the branch
    # that runs, on every machine.
    monkeypatch.setattr(_common.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(_common.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        active_reference_guard("needle", tmp_path)


# ---------------------------------------------------------------------------
# active_reference_guard — rg-absent fallback (Findings 1 & 5, code-reviewer
# 2026-07-28: undecodable files must be SKIPPED, not treated as a universal
# match, and well-known VCS/build dirs must not be walked)
# ---------------------------------------------------------------------------

def _force_rg_absent(monkeypatch):
    monkeypatch.setattr(_common.shutil, "which", lambda _name: None)


def test_active_reference_guard_fallback_finds_match(monkeypatch, tmp_path):
    _force_rg_absent(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "some-doc.md").write_text("references archive/specs/old-thing.md here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is True


def test_active_reference_guard_fallback_no_match(monkeypatch, tmp_path):
    _force_rg_absent(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "some-doc.md").write_text("nothing relevant here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is False


def test_active_reference_guard_fallback_skips_undecodable_file_does_not_universally_block(
    monkeypatch, tmp_path
):
    # Regression pin for Finding 1: a binary/non-UTF-8 file anywhere in scope must NOT
    # make the guard return True for every needle. Only a genuine content match blocks.
    _force_rg_absent(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "binary.bin").write_bytes(b"\xff\xfe\x00\x01not-utf8\x80")
    (docs / "irrelevant.md").write_text("nothing relevant here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is False


def test_active_reference_guard_fallback_still_finds_match_alongside_undecodable_file(
    monkeypatch, tmp_path
):
    _force_rg_absent(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "binary.bin").write_bytes(b"\xff\xfe\x00\x01not-utf8\x80")
    (docs / "cites-it.md").write_text("references archive/specs/old-thing.md here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is True


def test_active_reference_guard_fallback_skips_vcs_and_build_dirs(monkeypatch, tmp_path):
    # Regression pin for Finding 5: well-known ignore dirs are not walked/read at all.
    _force_rg_absent(monkeypatch)
    docs = tmp_path / "docs"
    docs.mkdir()
    git_dir = docs / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("references archive/specs/old-thing.md here\n")
    pycache_dir = docs / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "mod.pyc").write_text("references archive/specs/old-thing.md here\n")
    assert active_reference_guard("archive/specs/old-thing.md", tmp_path) is False


# ---------------------------------------------------------------------------
# active_reference_guard — provenance-marker-block exclusion
#
# 2026-07-23 cross-repo proposal (claude-central-em ->
# cross-repo/inbox/2026-07-23-claude-central-em-distill-active-reference-provenance-exclusion.md):
# a harvest-provenance block (PROVENANCE_MARKER_KEYS) records a harvested artifact's own
# repo-relative path as a tombstone, which previously tripped this guard against itself and
# made the artifact permanently undeletable. Marker key set per
# coordinator/docs/wiki/provenance-markers.md § "The marker key set (the contract)".
# ---------------------------------------------------------------------------

@_requires_rg
def test_active_reference_guard_excludes_provenance_only_citation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "harvested.md").write_text(
        "---\n"
        "archived_handoff:\n"
        "  - path: archive/handoffs/old-thing.md\n"
        "    workstream: foo\n"
        "---\n"
        "body text with nothing else\n"
    )
    assert active_reference_guard("archive/handoffs/old-thing.md", tmp_path) is False


@_requires_rg
def test_active_reference_guard_blocks_citation_outside_provenance_block(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "still-cited.md").write_text(
        "---\n"
        "status: active\n"
        "---\n"
        "see archive/handoffs/old-thing.md for context\n"
    )
    assert active_reference_guard("archive/handoffs/old-thing.md", tmp_path) is True


@_requires_rg
def test_active_reference_guard_blocks_when_cited_both_inside_and_outside_provenance_block(tmp_path):
    # Conservative-by-construction: an outside-block citation is sufficient to block, even
    # when the SAME candidate is also tombstoned inside a provenance block in the same
    # corpus. Getting this backwards (letting the tombstone "cancel out" the live citation)
    # would green-light deleting an artifact something still genuinely depends on.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "harvested.md").write_text(
        "---\n"
        "archived_handoff:\n"
        "  - path: archive/handoffs/old-thing.md\n"
        "---\n"
        "and also mentioned again here: archive/handoffs/old-thing.md\n"
    )
    assert active_reference_guard("archive/handoffs/old-thing.md", tmp_path) is True


@_requires_rg
def test_active_reference_guard_blocks_on_unknown_marker_key(tmp_path):
    # A citation inside a frontmatter block under a key that is NOT in
    # PROVENANCE_MARKER_KEYS is not recognized as a tombstone -> blocks, exactly like a
    # plain prose citation. Ambiguity blocks; only the named, coordinator-claude-ratified key set is
    # excluded — a lookalike key must not be inferred as a marker.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "custom-provenance.md").write_text(
        "---\n"
        "some_other_provenance:\n"
        "  - path: archive/handoffs/old-thing.md\n"
        "---\n"
        "body text\n"
    )
    assert active_reference_guard("archive/handoffs/old-thing.md", tmp_path) is True


# ---------------------------------------------------------------------------
# frontmatter re-export
# ---------------------------------------------------------------------------

def test_frontmatter_reexports_are_the_primitives_functions():
    from coordinator_core.frontmatter import primitives

    assert split_frontmatter is primitives.split_frontmatter
    assert read_fm_field is primitives.read_fm_field


def test_frontmatter_reexport_round_trip():
    text = "---\nstatus: implemented\n---\nbody text\n"
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "status") == "implemented"


def test_common_does_not_export_mutation_helpers():
    # Read-only invariant: _common must not expose insert/replace/remove/rebuild.
    for name in ("insert_fm_field", "replace_fm_field", "remove_fm_field", "rebuild"):
        assert not hasattr(_common, name) or name not in _common.__all__

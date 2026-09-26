"""
coordinator_core/roadmap/tests/test_prep_gate_four_legs.py — the four legs ported
from DoE-claude's ``coordinator/bin/mise-prep-gate.py`` at sha
``fbc7bf2bb9f58ef84a11254ef71f0c9391b220f6`` (resolved through
``coordinator_core.testing.doe_root.resolve_doe_root`` for any session that
needs the read-side twin itself, rather than this restatement of it).

Each leg gets exactly one fixture, built to trip that leg and no other —
``test_prep_gate.py`` already exercises three of these (``_created_roots``,
the archive-write SPINE check, refusal collapsing) from other angles; this
module is the dedicated pin for all four together, plus the one leg
(STALE-PREFIX) that had no fixture anywhere before this row.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg

# ---------------------------------------------------------------------------
# Fixture helpers (mirrors test_prep_gate.py's, kept local and minimal so this
# module has no import-order dependency on that one)
# ---------------------------------------------------------------------------


def _write_plan(
    root: Path,
    slug: str = "2026-09-26-four-legs-fixture.md",
    *,
    frontmatter: str = "",
    spine: str | None = None,
) -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    fm = [
        "title: fixture",
        "status: draft",
        "created: 2026-09-26",
        "author: fixture-session",
    ]
    if frontmatter.strip():
        fm.append(frontmatter.strip())
    body = ["", "# Fixture", "", "Prose that names nothing.", ""]
    if spine is not None:
        body += ["## Tasks", "", "```yaml plan-tasks", spine.strip(), "```", ""]
    path = plans / slug
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body), encoding="utf-8"
    )
    return path


_CLEAN_FM = """census: []
prime_exit_criterion:
  statement: the four legs land and validate
  derived_from: state/sizings/2026-09-26-four-legs-fixture.yaml
"""


def _gate(root: Path, path: Path) -> dict:
    return pg.gate_plan(root, path)


# ---------------------------------------------------------------------------
# Leg 1: _created_roots — a first segment the plan's own spine declares
# creating is not read as a cross-repo write.
# ---------------------------------------------------------------------------


def test_leg_created_roots_exempts_a_root_this_plan_creates(tmp_path):
    spine = """- id: C1
  title: Create the workspace root
  change_kind: code-edit
  surface: ide
  writes: [ide/shell/main.py]
  writes_under: [ide/]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"
    # Same value with no writes_under: declaration trips the ordinary
    # ROOT-EXISTENCE leg instead — confirming the exemption is read off the
    # declaration, not off the value's own shape.
    spine_undeclared = spine.replace("  writes_under: [ide/]\n", "")
    report2 = _gate(
        tmp_path,
        _write_plan(
            tmp_path,
            slug="2026-09-26-four-legs-fixture-b.md",
            frontmatter=_CLEAN_FM,
            spine=spine_undeclared,
        ),
    )
    assert report2["classes"]["EXTERNAL_DEPS"]["status"] == "DEFECT"


# ---------------------------------------------------------------------------
# Leg 2: STALE-PREFIX candidate — a first segment absent from the repo root
# but present one level down names a stale path prefix, not a cross-repo
# write, and the repair the detail line offers is to move the prefix.
# ---------------------------------------------------------------------------


def test_leg_stale_prefix_candidate_names_the_real_location(tmp_path):
    # `state/cross-repo/` exists; `cross-repo/` at the root does not. A row
    # naming `cross-repo/inbox/x.md` is spelled against a remembered layout.
    (tmp_path / "state" / "cross-repo").mkdir(parents=True)
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = """- id: C1
  title: Land a cross-repo memo
  change_kind: doc-edit
  surface: cross-repo/inbox
  writes: [cross-repo/inbox/x.md]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    detail = report["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "stale path prefix, not a cross-repo write" in detail
    assert "state/cross-repo/" in detail
    # A segment absent everywhere still reports the plain ROOT-EXISTENCE
    # message, with no fabricated candidate.
    spine_no_match = spine.replace(
        "writes: [cross-repo/inbox/x.md]", "writes: [nowhere-at-all/x.md]"
    )
    report2 = _gate(
        tmp_path,
        _write_plan(
            tmp_path,
            slug="2026-09-26-four-legs-fixture-b.md",
            frontmatter=_CLEAN_FM,
            spine=spine_no_match,
        ),
    )
    detail2 = report2["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "does not exist in this repo" in detail2
    assert "stale path prefix" not in detail2


def test_leg_stale_prefix_candidate_offers_every_matching_parent(tmp_path):
    """A name nested under more than one root entry is ambiguous and carries
    every parent — the candidate is a list of options, never a guess at one."""
    (tmp_path / "state" / "shared").mkdir(parents=True)
    (tmp_path / "docs" / "shared").mkdir(parents=True)
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = """- id: C1
  title: Ambiguous nested prefix
  change_kind: doc-edit
  surface: shared/notes
  writes: [shared/notes/x.md]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    detail = report["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "state/shared/" in detail
    assert "docs/shared/" in detail
    assert " or " in detail


# ---------------------------------------------------------------------------
# Leg 3: archive-write SPINE check — a row writing under archive/ outside the
# guard's carve-outs is BLOCKED in-wave and must be caught at authoring time.
# ---------------------------------------------------------------------------


def test_leg_archive_write_refused_in_wave(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = """- id: C1
  title: Archive a spec by hand
  body: Move the resolved spec into archive/specs/ by hand.
  change_kind: doc-edit
  surface: archive/specs/fixture-spec.md
  writes: [archive/specs/fixture-spec.md]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "writes-archive-refused-in-wave"
    assert "C1 (archive/specs/fixture-spec.md)" in report["classes"]["SPINE"]["detail"]


# ---------------------------------------------------------------------------
# Leg 4: refusal collapsing — one distinct fact reported once with a count,
# rather than once per row/value that shares it.
# ---------------------------------------------------------------------------


def test_leg_refusal_collapsing_reports_one_line_with_a_count(tmp_path):
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = """- id: C1
  title: Writes deep into a tree that is not here
  change_kind: code-edit
  surface: somewhere
  writes: [not_a_directory_here/deep/a.py, not_a_directory_here/deep/b.py, not_a_directory_here/deep/c.py]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    detail = report["classes"]["EXTERNAL_DEPS"]["detail"]
    assert detail.count("does not exist in this repo") == 1, (
        "three byte-identical facts must collapse to one line, not three"
    )
    assert "(x3)" in detail

"""
coordinator_core.ops.test_render_posture_overlay — behavior-parity tests for
the naked-Python port of the example-doctrine-repo-owned bash script.

Port of: render-posture-overlay.sh (example-doctrine-repo a1a568d2, 2026-07-22).

Tests assert:
  - insert path (no managed block yet) appends the block, leaving the rest
    of the file untouched.
  - swap path (managed block present) replaces only the marker span.
  - anchor == "default" is treated uniformly (no special-case skip).
  - --check-only computes the action and leaves the target byte-unchanged
    (including: does NOT create a target that does not yet exist).
  - collision detection fires on BOTH the insert path and the swap path for
    a markerless '## Posture' / '## Working Style' heading outside the span.
  - the module never gates on a CLAUDE.md byte budget for this target (a
    write far past the old 40000-byte HARD figure still succeeds), and
    never reads or scrapes a sibling repo's `check-claude-md-size.py` — see
    `test_never_scrapes_sibling_size_guard_source` for the regression guard
    against a reintroduced cross-repo scrape.
  - a target that does not yet exist is CREATED (including parent
    directories) on the write path rather than dying.
  - a template body containing a line byte-identical to a marker is rejected.
  - a malformed managed block (start marker, no end marker) is rejected.
  - unknown anchor / missing template / usage errors all exit 1 with the
    oracle's exit-code contract (0 success, 1 business-fail).
  - the atomic-replace faithfully mirrors the oracle's permission-loss
    behavior (mktemp-mode temp file replaces the target, NOT preserving the
    target's prior mode bits) — CLAUDE.md is non-executable, so this is a
    faithful-oracle-repro, not a regression (see module negative-spec).

Spec backlink: docs/plans/2026-07-30-posture-overlay-em-channel-retarget.md
"""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from coordinator_core.ops import render_posture_overlay
from coordinator_core.ops.render_posture_overlay import MARKER_END, MARKER_START, main, run


def _make_coordinator_root(tmp_path: Path) -> Path:
    root = tmp_path / "coordinator"
    (root / "templates" / "postures").mkdir(parents=True)
    for anchor, body in (
        ("precision", "## Posture\n\nprecision body"),
        ("default", "## Posture\n\ndefault body"),
        ("substrate-free", "## Posture\n\nsubstrate-free body"),
    ):
        (root / "templates" / "postures" / f"{anchor}.md").write_text(body, encoding="utf-8")
    return root


def _make_target(tmp_path: Path, content: str) -> Path:
    target = tmp_path / "CLAUDE.md"
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# insert path
# ---------------------------------------------------------------------------


def test_insert_appends_managed_block(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# My CLAUDE.md\n\nexisting content\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0
    out = target.read_text(encoding="utf-8")
    assert "existing content" in out
    assert MARKER_START in out
    assert "precision body" in out
    assert MARKER_END in out
    assert out.index(MARKER_START) > out.index("existing content")


def test_insert_on_empty_file(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "")

    rc = main(["default", str(target)], coordinator_root=str(root))

    assert rc == 0
    out = target.read_text(encoding="utf-8")
    assert out.startswith(MARKER_START)


# ---------------------------------------------------------------------------
# swap path
# ---------------------------------------------------------------------------


def test_swap_replaces_only_marker_span(tmp_path):
    root = _make_coordinator_root(tmp_path)
    original = (
        "# My CLAUDE.md\n\n"
        "before\n"
        f"{MARKER_START}\nold block content\n{MARKER_END}\n"
        "after\n"
    )
    target = _make_target(tmp_path, original)

    rc = main(["substrate-free", str(target)], coordinator_root=str(root))

    assert rc == 0
    out = target.read_text(encoding="utf-8")
    assert "before" in out
    assert "after" in out
    assert "old block content" not in out
    assert "substrate-free body" in out
    # exactly one managed span
    assert out.count(MARKER_START) == 1
    assert out.count(MARKER_END) == 1


def test_anchor_default_treated_uniformly(tmp_path):
    """AC6-equivalent: anchor=='default' injects like any other anchor, no skip-branch."""
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["default", str(target)], coordinator_root=str(root))

    assert rc == 0
    out = target.read_text(encoding="utf-8")
    assert MARKER_START in out and "default body" in out


# ---------------------------------------------------------------------------
# --check-only
# ---------------------------------------------------------------------------


def test_check_only_leaves_target_byte_unchanged(tmp_path):
    root = _make_coordinator_root(tmp_path)
    original = "# CLAUDE.md\n\nuntouched\n"
    target = _make_target(tmp_path, original)
    before_bytes = target.read_bytes()

    rc = main(["precision", str(target), "--check-only"], coordinator_root=str(root))

    assert rc == 0
    assert target.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# collision detection
# ---------------------------------------------------------------------------


def test_collision_markerless_heading_on_insert_path(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n\n## Posture\n\nhand-authored\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 1
    assert target.read_text(encoding="utf-8") == "# CLAUDE.md\n\n## Posture\n\nhand-authored\n"


def test_collision_markerless_heading_outside_span_on_swap_path(tmp_path):
    root = _make_coordinator_root(tmp_path)
    original = (
        "# CLAUDE.md\n\n"
        "## Working Style\n\nstray hand-authored section\n\n"
        f"{MARKER_START}\nold\n{MARKER_END}\n"
    )
    target = _make_target(tmp_path, original)

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 1
    # Fail loud, no mutation.
    assert target.read_text(encoding="utf-8") == original


def test_no_collision_for_heading_inside_managed_span(tmp_path):
    """A '## Posture' heading INSIDE the markers (expected template content) must not false-positive."""
    root = _make_coordinator_root(tmp_path)
    original = f"# CLAUDE.md\n\n{MARKER_START}\n## Posture\n\nold body\n{MARKER_END}\n"
    target = _make_target(tmp_path, original)

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0


# ---------------------------------------------------------------------------
# no CLAUDE.md byte-budget gate on this target (AC anti-scope item 3)
# ---------------------------------------------------------------------------


def test_no_size_gate_write_past_old_hard_threshold_succeeds(tmp_path):
    """A merge that would exceed the retired 40000-byte HARD figure must
    still succeed — this target (a per-repo em-context.md) is not gated
    against the global CLAUDE.md byte budget at all (see module docstring
    § Size guard). The repo-slot soft cap that DOES apply
    (`assert-em-role.py`'s `_REPO_SNIPPET_SOFT_CAP_BYTES`) is enforced on
    the consuming side, not here.
    """
    root = _make_coordinator_root(tmp_path)
    oversized_body = "## Posture\n\n" + ("x" * 50000)
    (root / "templates" / "postures" / "precision.md").write_text(
        oversized_body, encoding="utf-8"
    )
    target = _make_target(tmp_path, "# em-context.md\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0
    out = target.read_text(encoding="utf-8")
    assert len(out.encode("utf-8")) > 40000
    assert "x" * 50000 in out


def test_never_scrapes_sibling_size_guard_source(tmp_path):
    """Regression guard: the op must never read/scrape a sibling repo's
    check-claude-md-size.py, or reference a `HARD = ` line pattern, or a
    `hard_limit`-shaped byte-budget concept at all. A live-broken parse of
    that file previously sailed through 21 tests because the fixture
    fabricated its own copy of the scraped file (see module history / this
    test file's own docstring) — this test fails if that scrape (or any
    equivalent budget-gate) is reintroduced, WITHOUT needing a
    check-claude-md-size.py fixture to exist on disk at all.
    """
    # Structural checks against the executable code, not the module
    # docstring — the docstring legitimately narrates this history in prose
    # (why the scrape was removed), so a bare substring scan of the whole
    # source would false-positive on its own explanation. These checks
    # instead pin the absence of the actual scrape machinery.
    source = inspect.getsource(render_posture_overlay)

    assert "size_guard_src" not in source
    assert "hard_limit" not in source
    assert not hasattr(render_posture_overlay, "_parse_hard_limit")
    assert not hasattr(render_posture_overlay, "_HARD_RE")

    # No check-claude-md-size.py fixture exists in this coordinator_root at
    # all — if the op still tried to scrape it, it would die on a missing
    # file, not merely on a missing gate.
    root = _make_coordinator_root(tmp_path)
    assert not (root / "hooks").exists()
    target = _make_target(tmp_path, "# em-context.md\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0


# ---------------------------------------------------------------------------
# create-when-absent (AC4)
# ---------------------------------------------------------------------------


def test_creates_target_when_absent(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = tmp_path / "consumer-repo" / ".claude" / "em-context.md"
    assert not target.exists()

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0
    assert target.is_file()
    out = target.read_text(encoding="utf-8")
    assert out.startswith(MARKER_START)
    assert "precision body" in out
    assert MARKER_END in out


def test_check_only_does_not_create_absent_target(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = tmp_path / "consumer-repo" / ".claude" / "em-context.md"
    assert not target.exists()

    rc = main(["precision", str(target), "--check-only"], coordinator_root=str(root))

    assert rc == 0
    assert not target.exists()
    assert not target.parent.exists()


# ---------------------------------------------------------------------------
# marker-in-template guard
# ---------------------------------------------------------------------------


def test_template_containing_marker_line_is_rejected(tmp_path):
    root = _make_coordinator_root(tmp_path)
    (root / "templates" / "postures" / "precision.md").write_text(
        f"## Posture\n\n{MARKER_START}\nquoted illustratively\n", encoding="utf-8"
    )
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 1


# ---------------------------------------------------------------------------
# malformed managed block
# ---------------------------------------------------------------------------


def test_start_marker_without_end_marker_is_rejected(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, f"# CLAUDE.md\n\n{MARKER_START}\norphaned start\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 1


# ---------------------------------------------------------------------------
# usage / validation errors
# ---------------------------------------------------------------------------


def test_unknown_anchor_exits_1(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["bogus-anchor", str(target)], coordinator_root=str(root))

    assert rc == 1


def test_missing_template_exits_1(tmp_path):
    root = _make_coordinator_root(tmp_path)
    (root / "templates" / "postures" / "precision.md").unlink()
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 1


def test_absent_target_is_created_not_a_usage_error(tmp_path):
    """AC4: absence is the normal first-run case, not an error — superseded
    'missing target exits 1' expectation (pre-retarget behavior). See
    test_creates_target_when_absent for the fuller assertion of this path.
    """
    root = _make_coordinator_root(tmp_path)

    rc = main(["precision", str(tmp_path / "nope.md")], coordinator_root=str(root))

    assert rc == 0
    assert (tmp_path / "nope.md").is_file()


def test_too_few_args_exits_1(tmp_path):
    rc = main(["precision"], coordinator_root=str(tmp_path))
    assert rc == 1


def test_help_after_required_args_exits_0(tmp_path, capsys):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["precision", str(target), "-h"], coordinator_root=str(root))

    assert rc == 0
    assert target.read_text(encoding="utf-8") == "# CLAUDE.md\n"


def test_bare_help_flag_alone_is_usage_error_not_help(tmp_path):
    """Oracle parity: `-h` alone is 1 arg -> `$# -lt 2` -> usage+exit 1, NOT usage+exit 0."""
    rc = main(["-h"], coordinator_root=str(tmp_path))
    assert rc == 1


def test_unknown_flag_exits_1(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc = main(["precision", str(target), "--bogus"], coordinator_root=str(root))

    assert rc == 1


# ---------------------------------------------------------------------------
# run() direct — coordinator_root required
# ---------------------------------------------------------------------------


def test_main_without_coordinator_root_is_internal_usage_error(tmp_path):
    target = _make_target(tmp_path, "# CLAUDE.md\n")
    rc = main(["precision", str(target)])
    assert rc == 1


# ---------------------------------------------------------------------------
# permission-loss faithful-oracle-repro (negative-spec regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only invariant: this asserts a specific POSIX mode-bit "
    "distinction (tempfile.mkstemp's umask-independent 0600 vs. the "
    "original target's 0644) that Windows cannot express -- os.stat() "
    "always reports 0o666/0o444 there regardless of which file produced "
    "it, so the negative-spec this guards (mktemp mode NOT matching the "
    "original) is unobservable on this platform.",
)
def test_atomic_replace_does_not_preserve_prior_mode_bits(tmp_path):
    """Faithful port of the oracle's mktemp+mv behavior: the rewritten
    target's mode comes from the temp file (0600-ish, masked by umask), NOT
    the original target's mode — this mirrors the bash oracle's `mv` over a
    mktemp'd file exactly. CLAUDE.md is non-executable so this is not a
    permission-preservation defect under addendum rule 5 (scoped to
    executable files); asserted here so a future "helpful" chmod-preserve
    fix doesn't silently change behavior from the oracle.
    """
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")
    os.chmod(target, 0o644)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644

    rc = main(["precision", str(target)], coordinator_root=str(root))

    assert rc == 0
    new_mode = stat.S_IMODE(os.stat(target).st_mode)
    # tempfile.mkstemp creates 0600 files (umask-independent on POSIX);
    # os.replace carries that mode onto the target, same as oracle's mv.
    assert new_mode != 0o644
    assert new_mode == 0o600


# ---------------------------------------------------------------------------
# run() return shape
# ---------------------------------------------------------------------------


def test_run_returns_tuple_of_exit_code_and_message(tmp_path):
    root = _make_coordinator_root(tmp_path)
    target = _make_target(tmp_path, "# CLAUDE.md\n")

    rc, msg = run("precision", str(target), False, str(root))

    assert rc == 0
    assert "insert" in msg
    assert "precision" in msg

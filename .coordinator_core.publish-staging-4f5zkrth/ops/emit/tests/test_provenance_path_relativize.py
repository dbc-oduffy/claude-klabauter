"""Unit tests — provenance-path relativization helpers (DR-060 follow-up).

Direct, non-golden coverage for two helpers previously exercised only transitively via
``test_emit_parity``'s section-parity comparison, which never constructed a file outside
the resolved root nor a fixture path missing the anchor: ``review_trail.py::_relativize_path``'s
``ValueError`` fallback branch, and ``normalizers.py::_relativize_abs_fixture_path``'s three
canonicalization forms (absolute, anchor-relative, bare-relative) plus its non-string
passthrough.

Spec backlink: state/review-trail/findings/2026-07-21-codereview-slicemakima-cockpit-emitter-
provenance-relativize-coordinator-core-ops-emit-sections-revie.md — Finding 3.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.normalizers import _relativize_abs_fixture_path
from coordinator_core.ops.emit.sections.review_trail import _relativize_path


def _make_ctx(tmp_path: Path, subprocess_root: Path | None = None) -> EmitContext:
    return EmitContext(
        repo_root=tmp_path,
        coordinator_root=tmp_path,
        central_state_root=tmp_path / "state",
        git_branch="work/fixture/2026-07-01",
        git_sha="aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee",
        git_sha_short="aaaaaaaa",
        observed_at="2026-07-21T00:00:00Z",
        hostname="fixture.local",
        repo_name="fixture-owner/fixture-repo",
        subprocess_root=subprocess_root,
    )


class TestRelativizePathFallback:
    """``_relativize_path`` (review_trail.py) — outside-root fallback branch."""

    def test_filepath_outside_root_returns_original_absolute_path(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "state" / "review-trail" / "x.json"
        outside.parent.mkdir(parents=True)
        outside.write_text("{}")

        ctx = _make_ctx(root, subprocess_root=root)
        result = _relativize_path(ctx, str(outside))

        assert result == str(outside)

    def test_filepath_inside_root_is_relativized(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "state" / "review-trail").mkdir(parents=True)
        inside = root / "state" / "review-trail" / "y.json"
        inside.write_text("{}")

        ctx = _make_ctx(root, subprocess_root=root)
        result = _relativize_path(ctx, str(inside))

        assert result == "state/review-trail/y.json"


class TestRelativizeAbsFixturePath:
    """``_relativize_abs_fixture_path`` (normalizers.py) — three canonicalization forms."""

    def test_absolute_path_canonicalizes_to_state_relative(self) -> None:
        value = "/Users/x/project-makima/coordinator_core/ops/emit/tests/fixtures/root/state/review-trail/y.json"
        assert _relativize_abs_fixture_path(value) == "state/review-trail/y.json"

    def test_anchor_relative_path_canonicalizes_to_state_relative(self) -> None:
        value = "coordinator_core/ops/emit/tests/fixtures/root/state/lessons/z.yaml"
        assert _relativize_abs_fixture_path(value) == "state/lessons/z.yaml"

    def test_bare_relative_path_passes_through_unchanged(self) -> None:
        value = "state/foo.json"
        assert _relativize_abs_fixture_path(value) == "state/foo.json"

    def test_windows_backslash_path_canonicalizes_to_state_relative(self) -> None:
        value = r"C:\Users\x\project-makima\coordinator_core\ops\emit\tests\fixtures\root\state\review-trail\y.json"
        assert _relativize_abs_fixture_path(value) == "state/review-trail/y.json"

    def test_non_string_passes_through_unchanged(self) -> None:
        assert _relativize_abs_fixture_path(None) is None  # type: ignore[arg-type]
        assert _relativize_abs_fixture_path(42) == 42  # type: ignore[arg-type]

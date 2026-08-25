"""
Tests for coordinator_core.commit_ledger.classify -- the weighting layer
C4 adds on top of review_brightline_gate's shared classify_surface /
_is_noise_path primitives.

Uses a self-contained coordinator.local.md fixture per test (tmp_path) --
does NOT depend on, or assert against, this repo's own committed
coordinator.local.md, so these tests stay green independent of what
config values land there.
"""

from pathlib import Path

from coordinator_core.commit_ledger.classify import (
    _DEFAULT_BASELINE_WEIGHT,
    _ELEVATED_SURFACE_WEIGHT,
    weight_for_path,
)
from coordinator_core.ops.review_brightline_gate import classify_surface


def _write_local_md(repo_root: Path, frontmatter_body: str) -> None:
    (repo_root / "coordinator.local.md").write_text(
        f"---\n{frontmatter_body}\n---\n", encoding="utf-8"
    )


def test_baseline_weight_from_mapping(tmp_path):
    _write_local_md(
        tmp_path,
        "commit_ledger_baseline_weight:\n  python: 1.0\n  doctrine: 0.3\n",
    )
    assert weight_for_path(str(tmp_path), "coordinator_core/foo.py") == 1.0
    assert weight_for_path(str(tmp_path), "docs/notes.md") == 0.3


def test_unlisted_bucket_lands_on_default_never_zero(tmp_path):
    # AC5: a bucket with no configured weight, and a repo with no mapping
    # at all, both fall back to a non-zero default -- never a blind spot.
    _write_local_md(tmp_path, "commit_ledger_baseline_weight:\n  python: 1.0\n")
    assert classify_surface("README.md") == "doctrine"
    weight = weight_for_path(str(tmp_path), "README.md")
    assert weight == _DEFAULT_BASELINE_WEIGHT
    assert weight > 0.0


def test_no_config_at_all_falls_back_to_default(tmp_path):
    _write_local_md(tmp_path, "project_type: general\n")
    weight = weight_for_path(str(tmp_path), "coordinator_core/foo.py")
    assert weight == _DEFAULT_BASELINE_WEIGHT
    assert weight > 0.0


def test_elevated_surface_scales_baseline(tmp_path):
    _write_local_md(
        tmp_path,
        (
            "commit_ledger_baseline_weight:\n"
            "  python: 1.0\n"
            'commit_ledger_elevated_surfaces: ["coordinator_core/ops/**"]\n'
        ),
    )
    plain = weight_for_path(str(tmp_path), "coordinator_core/foo.py")
    elevated = weight_for_path(str(tmp_path), "coordinator_core/ops/bar.py")
    assert plain == 1.0
    assert elevated == 1.0 * _ELEVATED_SURFACE_WEIGHT


def test_noise_path_is_zero_weight(tmp_path):
    _write_local_md(tmp_path, "commit_ledger_baseline_weight:\n  python: 1.0\n")
    # __pycache__ artifacts are noise per review_brightline_gate's shared
    # _is_noise_path predicate.
    assert weight_for_path(str(tmp_path), "coordinator_core/__pycache__/foo.pyc") == 0.0


def test_malformed_weight_value_falls_back_to_default(tmp_path):
    _write_local_md(
        tmp_path,
        "commit_ledger_baseline_weight:\n  python: not-a-number\n",
    )
    weight = weight_for_path(str(tmp_path), "coordinator_core/foo.py")
    assert weight == _DEFAULT_BASELINE_WEIGHT

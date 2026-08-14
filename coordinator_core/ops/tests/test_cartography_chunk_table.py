"""
coordinator_core.ops.tests.test_cartography_chunk_table

Unit tests for the `oversized_threshold` param added to
coordinator_core.ops.cartography_chunk_table (chunk C1 of
docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md).

Coverage:
  (a) absent-param byte identity — no threshold, schema_version unchanged,
      emitted JSON bytes identical to a pre-param build
  (b) threshold boundary — at/above included, below excluded
  (c) None-loc exclusion — an undecodable (binary) file is never "oversized"
  (d) sort determinism — `oversized` is sorted regardless of discovery order
  (e) version-bump-fires-only-when-threshold-supplied
  (f) key-order — schema_version stays the first key in both cases

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-cartography-chunk-table-producer-seam.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_chunk_table  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_chunk_table import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_OVERSIZED,
    build_chunk_table_artifact,
)
from coordinator_core.cartography.chunk_table import compute_chunk_table

_OP_NAME = "cartography.chunk_table"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_chunk_table @register_op did not fire"
)

# build_chunk_table_artifact walks the git-tracked file set of the `git_repo`
# fixture — the byte-identity and sort-determinism assertions depend on real
# git-tracked-file discovery order, which a mock cannot reproduce faithfully.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

    # "big.py" is 5 newline-terminated lines (loc=5); "small.py" is 2 (loc=2);
    # "binary.py" is undecodable as UTF-8 (loc=None) despite a source
    # extension, so it must never appear in "oversized" regardless of
    # threshold.
    (root / "systemA").mkdir()
    (root / "systemA" / "big.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    (root / "systemA" / "small.py").write_text("a\nb\n", encoding="utf-8")
    (root / "systemA" / "binary.py").write_bytes(b"\xff\xfe\x00\x01not-utf8\n")

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


_SYSTEMS = {"systemA": ["systemA"]}


# ---------------------------------------------------------------------------
# (a) absent-param byte identity
# ---------------------------------------------------------------------------


def test_absent_threshold_no_file_opened_and_matches_pre_param_shape(git_repo, monkeypatch):
    opened: list[Path] = []
    import coordinator_core.ops.cartography_chunk_table as mod

    original = mod._loc_for

    def spy(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(mod, "_loc_for", spy)

    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )

    assert opened == []
    assert "oversized" not in artifact
    assert artifact["schema_version"] == SCHEMA_VERSION


def test_absent_threshold_byte_identical_to_no_oversized_support(git_repo):
    with_default = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    explicit_none = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=None
    )
    assert json.dumps(with_default, indent=2) == json.dumps(explicit_none, indent=2)


# ---------------------------------------------------------------------------
# (b) threshold boundary — at, above, below
# ---------------------------------------------------------------------------


def test_threshold_at_boundary_included(git_repo):
    # big.py has loc=5; threshold=5 is "at" the boundary ("at or above").
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert "systemA/big.py" in artifact["oversized"]


def test_loc_strictly_above_threshold_included(git_repo):
    # big.py has loc=5, threshold=3 -> 5 >= 3, included.
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=3
    )
    assert "systemA/big.py" in artifact["oversized"]


def test_loc_below_threshold_excluded(git_repo):
    # small.py has loc=2, threshold=5 -> 2 < 5, excluded.
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert "systemA/small.py" not in artifact["oversized"]


def test_threshold_one_above_max_loc_excludes_everything(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=6
    )
    assert artifact["oversized"] == []


# ---------------------------------------------------------------------------
# (c) None-loc exclusion
# ---------------------------------------------------------------------------


def test_none_loc_file_never_oversized(git_repo):
    # A threshold of 1 would catch every file with a known loc — the binary
    # file's loc is None, not a coerced 0, so it must stay excluded even here.
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=1
    )
    assert "systemA/binary.py" not in artifact["oversized"]
    assert "systemA/big.py" in artifact["oversized"]
    assert "systemA/small.py" in artifact["oversized"]


# ---------------------------------------------------------------------------
# (d) sort determinism
# ---------------------------------------------------------------------------


def test_oversized_is_sorted(git_repo):
    (git_repo / "systemA" / "zzz_big.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    (git_repo / "systemA" / "aaa_big.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "more"], cwd=git_repo, check=True)

    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert artifact["oversized"] == sorted(artifact["oversized"])
    assert "systemA/aaa_big.py" in artifact["oversized"]
    assert "systemA/zzz_big.py" in artifact["oversized"]


# ---------------------------------------------------------------------------
# (e) version-bump-fires-only-when-threshold-supplied
# ---------------------------------------------------------------------------


def test_schema_version_unchanged_when_threshold_absent(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert artifact["schema_version"] == SCHEMA_VERSION


def test_schema_version_bumps_when_threshold_supplied(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert artifact["schema_version"] == SCHEMA_VERSION_OVERSIZED
    assert SCHEMA_VERSION_OVERSIZED != SCHEMA_VERSION


# ---------------------------------------------------------------------------
# (f) key-order assertion retained
# ---------------------------------------------------------------------------


def test_schema_version_stays_first_key_absent_threshold(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert next(iter(artifact)) == "schema_version"


def test_schema_version_stays_first_key_with_threshold(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10, oversized_threshold=5
    )
    assert next(iter(artifact)) == "schema_version"


# ---------------------------------------------------------------------------
# (g) AC1 byte-identity against an independently constructed pre-param
# baseline — Review: coordinator:code-reviewer, 2026-08-06. The (a) test
# above compares two post-diff call shapes (absent kwarg vs
# oversized_threshold=None), both of which pass through the SAME dict
# literal in build_chunk_table_artifact; it cannot detect a regression in
# that literal itself. This test reconstructs the pre-`oversized_threshold`
# artifact shape independently (calling compute_chunk_table directly and
# assembling the dict by hand, mirroring what build_chunk_table_artifact's
# unconditional base construction looked like before this param existed)
# so a drift in the base dict's keys/values/ordering is actually caught.
# ---------------------------------------------------------------------------


def test_absent_threshold_byte_identical_to_independently_constructed_baseline(git_repo):
    result = compute_chunk_table(git_repo, _SYSTEMS, 10)
    pre_param_baseline = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "run1",
        "target_root": str(git_repo),
        "systems": dict(_SYSTEMS),
        "chunk_size": 10,
        "buckets": result.buckets,
        "unbucketed": result.unbucketed,
        "counts": result.counts,
    }
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert json.dumps(artifact, indent=2) == json.dumps(pre_param_baseline, indent=2)

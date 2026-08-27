"""
coordinator_core.cartography.tests.test_chunk_table

Unit tests for coordinator_core.cartography.chunk_table (pure functions) and
the thin cartography.chunk_table op wrapper (coordinator_core/ops/
cartography_chunk_table.py).

Coverage:
  (a) is_source_file — source-lang extensions True, doc/config/unknown False
  (b) is_build_or_test_artifact — SKIP_DIR_NAMES/TEST_DIR_NAMES path components,
      test-filename patterns, and the negative case (ordinary source file)
  (c) bucket_by_boundaries — caller-supplied boundaries actually govern bucketing
      (the regression DoE's memo describes), longest-prefix tie-break, no-match ->
      None ("unbucketed")
  (d) chunk_list — deterministic fixed-size slicing
  (e) compute_chunk_table — end-to-end reduction over a tmp_path git fixture:
      build/vendor/test artifacts excluded, caller boundaries govern bucketing,
      chunk slicing deterministic across repeated runs
  (f) op wrapper — schema_version-pinned atomic write, unknown-forward-version
      read fails loud, matching-version silent; import-guard + registry

Spec backlink: cross-repo/inbox/2026-08-06-doe-claude-em-cartography-chunk-table-producer-seam.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# Real git repo is load-bearing: compute_chunk_table pipes through
# cartography.tree.list_tracked_files, which shells `git ls-files` -- the
# tracked-vs-untracked distinction under test (build/vendor/test-artifact
# exclusion) has no filesystem-only stand-in.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_chunk_table  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_chunk_table import (
    ChunkTableSchemaError,
    SCHEMA_VERSION,
    SCHEMA_VERSION_OVERSIZED,
    _cartography_chunk_table,
    build_chunk_table_artifact,
    check_schema_version,
    write_chunk_table,
)
from coordinator_core.cartography.chunk_table import (
    bucket_by_boundaries,
    chunk_list,
    compute_chunk_table,
    is_build_or_test_artifact,
    is_source_file,
)

_OP_NAME = "cartography.chunk_table"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_chunk_table @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

    files = {
        "systemA/foo.py": "print(1)\n",
        "systemA/bar.py": "print(2)\n",
        "systemA/tests/test_foo.py": "def test_x(): pass\n",
        "systemA/baz_test.py": "def test_y(): pass\n",
        "systemB/lib.ts": "export const x = 1;\n",
        "systemB/lib.test.ts": "test('x', () => {});\n",
        "systemB/node_modules/pkg/index.js": "module.exports = 1;\n",
        "docs/readme.md": "# hi\n",
        "unbucketed/orphan.py": "print(3)\n",
        "config.json": "{}\n",
    }
    for relpath, content in files.items():
        full = root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)
    return root


_SYSTEMS = {"systemA": ["systemA"], "systemB": ["systemB"]}


# ---------------------------------------------------------------------------
# (a) is_source_file
# ---------------------------------------------------------------------------


def test_is_source_file_true_for_code_extensions():
    assert is_source_file("a/b.py")
    assert is_source_file("a/b.ts")
    assert is_source_file("a/b.rs")


def test_is_source_file_false_for_doc_config_unknown():
    assert not is_source_file("README.md")
    assert not is_source_file("config.json")
    assert not is_source_file("data.bin")


# ---------------------------------------------------------------------------
# (b) is_build_or_test_artifact
# ---------------------------------------------------------------------------


def test_is_build_or_test_artifact_skip_dir():
    assert is_build_or_test_artifact("systemB/node_modules/pkg/index.js")


def test_is_build_or_test_artifact_test_dir():
    assert is_build_or_test_artifact("systemA/tests/test_foo.py")


def test_is_build_or_test_artifact_test_filename_pattern():
    assert is_build_or_test_artifact("systemA/baz_test.py")
    assert is_build_or_test_artifact("systemB/lib.test.ts")


def test_is_build_or_test_artifact_false_for_ordinary_source():
    assert not is_build_or_test_artifact("systemA/foo.py")


# ---------------------------------------------------------------------------
# (c) bucket_by_boundaries
# ---------------------------------------------------------------------------


def test_bucket_by_boundaries_governs_bucketing_not_top_level_directory():
    # Regression: DoE's memo defect was bucketing by top-level directory
    # name (cartography.file_index.system_for_path shape) instead of
    # caller-supplied boundaries. Here the caller names "svc" to cover a
    # nested path unrelated to its own top-level directory component.
    systems = {"svc": ["systemA/nested"]}
    assert bucket_by_boundaries("systemA/nested/deep/file.py", systems) == "svc"
    assert bucket_by_boundaries("systemA/other/file.py", systems) is None


def test_bucket_by_boundaries_longest_prefix_wins():
    systems = {"outer": ["coordinator_core"], "inner": ["coordinator_core/ops"]}
    assert bucket_by_boundaries("coordinator_core/ops/foo.py", systems) == "inner"
    assert bucket_by_boundaries("coordinator_core/cartography/tree.py", systems) == "outer"


def test_bucket_by_boundaries_no_match_is_none():
    assert bucket_by_boundaries("elsewhere/file.py", {"a": ["systemA"]}) is None


def test_bucket_by_boundaries_empty_systems_is_none():
    assert bucket_by_boundaries("systemA/foo.py", {}) is None


# ---------------------------------------------------------------------------
# (d) chunk_list
# ---------------------------------------------------------------------------


def test_chunk_list_fixed_size_slicing():
    items = [f"f{i}" for i in range(7)]
    chunks = chunk_list(items, 3)
    assert chunks == [["f0", "f1", "f2"], ["f3", "f4", "f5"], ["f6"]]


def test_chunk_list_never_divides_by_zero():
    assert chunk_list(["a", "b"], 0) == [["a"], ["b"]]


# ---------------------------------------------------------------------------
# (e) compute_chunk_table — end-to-end
# ---------------------------------------------------------------------------


def test_compute_chunk_table_excludes_build_vendor_test_and_docs(git_repo):
    result = compute_chunk_table(git_repo, _SYSTEMS, chunk_size=10)
    all_bucketed = {f for bucket in result.buckets.values() for f in bucket["files"]}
    all_files = all_bucketed | set(result.unbucketed)

    assert "systemA/foo.py" in all_files
    assert "systemA/bar.py" in all_files
    assert "systemB/lib.ts" in all_files

    excluded = {
        "systemA/tests/test_foo.py",
        "systemA/baz_test.py",
        "systemB/lib.test.ts",
        "systemB/node_modules/pkg/index.js",
        "docs/readme.md",
        "config.json",
    }
    assert not (excluded & all_files)


def test_compute_chunk_table_caller_boundaries_govern_bucketing(git_repo):
    result = compute_chunk_table(git_repo, _SYSTEMS, chunk_size=10)
    assert result.buckets["systemA"]["files"] == ["systemA/bar.py", "systemA/foo.py"]
    assert result.buckets["systemB"]["files"] == ["systemB/lib.ts"]
    assert result.unbucketed == ["unbucketed/orphan.py"]


def test_compute_chunk_table_deterministic_across_runs(git_repo):
    first = compute_chunk_table(git_repo, _SYSTEMS, chunk_size=10)
    second = compute_chunk_table(git_repo, _SYSTEMS, chunk_size=10)
    assert first.buckets == second.buckets
    assert first.unbucketed == second.unbucketed
    assert first.counts == second.counts


def test_compute_chunk_table_chunk_slicing(git_repo):
    result = compute_chunk_table(git_repo, _SYSTEMS, chunk_size=1)
    assert result.buckets["systemA"]["chunks"] == [["systemA/bar.py"], ["systemA/foo.py"]]


# ---------------------------------------------------------------------------
# (f) op wrapper — schema_version, atomic write, registry
# ---------------------------------------------------------------------------


def test_build_chunk_table_artifact_schema_version_first_key(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    assert next(iter(artifact)) == "schema_version"
    assert artifact["schema_version"] == SCHEMA_VERSION


def test_check_schema_version_silent_on_matching():
    check_schema_version({"schema_version": SCHEMA_VERSION})


def test_check_schema_version_fails_loud_on_unknown_forward_version():
    with pytest.raises(ChunkTableSchemaError):
        check_schema_version(
            {"schema_version": max(SCHEMA_VERSION, SCHEMA_VERSION_OVERSIZED) + 1}
        )


def test_write_chunk_table_atomic_and_write_confined(git_repo):
    artifact = build_chunk_table_artifact(
        git_repo, run_id="run1", systems=_SYSTEMS, chunk_size=10
    )
    written = write_chunk_table(git_repo, "run1", artifact)
    assert written == git_repo / "state" / "scratch" / "cartography-chunk-table" / "run1" / "chunk-table.json"
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk == artifact

    # Full-rewrite on second write (not partial mutation) — different systems
    # entirely replace, not merge, the prior content.
    artifact2 = build_chunk_table_artifact(
        git_repo, run_id="run1", systems={}, chunk_size=10
    )
    write_chunk_table(git_repo, "run1", artifact2)
    on_disk2 = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk2["systems"] == {}


def test_op_handler_missing_target_root_raises():
    with pytest.raises(ValueError):
        _cartography_chunk_table({"run_id": "r1"}, None)


def test_op_handler_missing_run_id_raises(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table({"target_root": str(git_repo)}, None)


def test_op_handler_unsafe_run_id_raises(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table(
            {"target_root": str(git_repo), "run_id": "../escape"}, None
        )


def test_op_handler_happy_path_no_emit(git_repo):
    result = _cartography_chunk_table(
        {"target_root": str(git_repo), "run_id": "r1", "systems": _SYSTEMS},
        None,
    )
    assert "chunk_table_path" not in result
    assert result["buckets"]["systemA"]["files"] == ["systemA/bar.py", "systemA/foo.py"]


def test_op_handler_emit_writes_file(git_repo):
    result = _cartography_chunk_table(
        {
            "target_root": str(git_repo),
            "run_id": "r1",
            "systems": _SYSTEMS,
            "emit": True,
        },
        None,
    )
    assert result["chunk_table_path"] == "state/scratch/cartography-chunk-table/r1/chunk-table.json"
    written = git_repo / result["chunk_table_path"]
    assert written.is_file()
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == SCHEMA_VERSION


def test_op_handler_emit_reply_omits_the_bulk_payload(git_repo):
    """An emitting call must NOT echo buckets/unbucketed back in the reply.

    This is the whole point of the producer seam: DoE's pipeline broke when a
    ~515KB reply was offloaded to a pointer object by their harness. Emitting
    the artifact and ALSO returning it would leave that failure mode intact.
    The reduction is read back from chunk_table_path; the reply stays small.
    """
    result = _cartography_chunk_table(
        {
            "target_root": str(git_repo),
            "run_id": "r1",
            "systems": _SYSTEMS,
            "emit": True,
        },
        None,
    )
    assert "buckets" not in result
    assert "unbucketed" not in result
    assert result["counts"]["source_total"] > 0

    on_disk = json.loads((git_repo / result["chunk_table_path"]).read_text(encoding="utf-8"))
    assert on_disk["buckets"]["systemA"]["files"] == ["systemA/bar.py", "systemA/foo.py"]


# ---------------------------------------------------------------------------
# (g) op-handler wire-level validation of oversized_threshold — Review:
# coordinator:code-reviewer, 2026-08-06. The `oversized_threshold` test file
# (coordinator_core/ops/tests/test_cartography_chunk_table.py) only calls
# build_chunk_table_artifact directly, never through the registered op, so
# _cartography_chunk_table's isinstance/bool/positive-int guard
# (cartography_chunk_table.py:316-325) had no coverage. Extends this file's
# existing op-handler pattern.
# ---------------------------------------------------------------------------


def test_op_handler_oversized_threshold_bool_rejected(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table(
            {
                "target_root": str(git_repo),
                "run_id": "r1",
                "oversized_threshold": True,
            },
            None,
        )


def test_op_handler_oversized_threshold_negative_rejected(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table(
            {
                "target_root": str(git_repo),
                "run_id": "r1",
                "oversized_threshold": -1,
            },
            None,
        )


def test_op_handler_oversized_threshold_zero_rejected(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table(
            {
                "target_root": str(git_repo),
                "run_id": "r1",
                "oversized_threshold": 0,
            },
            None,
        )


def test_op_handler_oversized_threshold_non_int_rejected(git_repo):
    with pytest.raises(ValueError):
        _cartography_chunk_table(
            {
                "target_root": str(git_repo),
                "run_id": "r1",
                "oversized_threshold": "5",
            },
            None,
        )


def test_op_handler_oversized_threshold_valid_bumps_version_and_retained_on_emit(git_repo):
    """Confirms the module docstring's claim that `oversized` is never
    stripped from an emitting call's reply (unlike buckets/unbucketed)."""
    result = _cartography_chunk_table(
        {
            "target_root": str(git_repo),
            "run_id": "r1",
            "systems": _SYSTEMS,
            "emit": True,
            "oversized_threshold": 1,
        },
        None,
    )
    assert result["schema_version"] == SCHEMA_VERSION_OVERSIZED
    assert "oversized" in result
    assert "buckets" not in result
    assert "unbucketed" not in result

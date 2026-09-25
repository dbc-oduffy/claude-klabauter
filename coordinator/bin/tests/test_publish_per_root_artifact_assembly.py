"""coordinator/bin/tests/test_publish_per_root_artifact_assembly.py — P079-C3
(`docs/plans/2026-09-11-publish-build-verify-swap-one-staging-pa.md` chunk C3).

Pins `assemble_per_root_artifact_view`/`assemble_per_root_artifact_views`: the
ONE materialised per-destination-root artifact view the plan's 7 wired
end-of-run legs (C4) will run against, built by hardlink-else-copy from the
rows' OWN staging trees (`_create_publish_staging_dir`) plus the untouched
remainder of the destination repo root — never a read-time overlay resolver
(rejected in the plan body; census row 7: zero of the 7 legs can accept a
path-lookup callable).

Two fixture rows sharing one dest repo root, at different rel_roots
(`""` — toplevel — and `"sub/nested"`), exercise the dominant case named in
the plan body (census row 2: all 10 real rows resolve to ONE root). This file
never touches `process_target`/the percolate engine — the assembly is a pure
filesystem operation over already-built staging trees, so its own inputs are
hand-built fixture directories, not a driven publish row.

Run: python -m pytest coordinator/bin/tests/test_publish_per_root_artifact_assembly.py -x -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_per_root_artifact_assembly_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _enumerate_files(root: Path) -> "set[str]":
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_two_rows_sharing_one_root_assemble_into_union_view(tmp_path: Path) -> None:
    dest_repo_root = tmp_path / "dest-repo"
    # The untouched remainder of the dest repo root — content no row in this
    # round's staging touches at all.
    _write(dest_repo_root / "README.md", "pre-existing\n")
    _write(dest_repo_root / "other" / "leftover.txt", "leftover\n")

    # Row A: toplevel row (rel_root == ""). Its staging tree carries the
    # post-transform bytes for the dest repo root's own top level.
    row_a_staging = tmp_path / "row-a-staging"
    _write(row_a_staging / "README.md", "row-a-transformed\n")
    _write(row_a_staging / "top.txt", "row-a-top\n")

    # Row B: subdir row (rel_root == "sub/nested").
    row_b_staging = tmp_path / "row-b-staging"
    _write(row_b_staging / "nested_a.txt", "row-b-a\n")
    _write(row_b_staging / "deeper" / "nested_b.txt", "row-b-b\n")

    view_root = publish.assemble_per_root_artifact_view(
        dest_repo_root,
        [(row_a_staging, ""), (row_b_staging, "sub/nested")],
    )
    try:
        assert view_root != dest_repo_root
        assert dest_repo_root not in view_root.parents
        assert view_root.parent == dest_repo_root.parent

        expected = {
            "README.md",  # overlaid by row A's staging tree
            "top.txt",
            "other/leftover.txt",  # untouched remainder
            "sub/nested/nested_a.txt",
            "sub/nested/deeper/nested_b.txt",
        }
        assert _enumerate_files(view_root) == expected
        assert (view_root / "README.md").read_text(encoding="utf-8") == "row-a-transformed\n"
    finally:
        publish.discard_per_root_artifact_view(view_root)
    assert not view_root.exists()


def test_live_sibling_staging_tree_excluded_from_view(tmp_path: Path) -> None:
    dest_repo_root = tmp_path / "dest-repo"
    _write(dest_repo_root / "README.md", "pre-existing\n")
    # A live sibling row's staging tree, planted directly inside the dest
    # repo root's own tree — the shape `_create_publish_staging_dir` mints
    # (dot-prefixed, `.{dest_dir.name}.publish-staging-<suffix>`).
    live_sibling = dest_repo_root / ".claude-klabauter.publish-staging-abc123"
    _write(live_sibling / "sibling_payload.txt", "sibling in-flight\n")

    row_a_staging = tmp_path / "row-a-staging"
    _write(row_a_staging / "README.md", "row-a-transformed\n")

    view_root = publish.assemble_per_root_artifact_view(
        dest_repo_root,
        [(row_a_staging, "")],
    )
    try:
        assert _enumerate_files(view_root) == {"README.md"}
        assert not (view_root / ".claude-klabauter.publish-staging-abc123").exists()
    finally:
        publish.discard_per_root_artifact_view(view_root)


def test_assemble_per_root_artifact_views_groups_by_repo_root(tmp_path: Path) -> None:
    dest_repo_root = tmp_path / "dest-repo"
    _write(dest_repo_root / "README.md", "pre-existing\n")
    # `_dest_prefix_for` walks up from `dest_dir` for the nearest `.git`
    # ancestor (`_dest_repo_root`) — a bare directory entry is enough for
    # that walk; this fixture is never a real git worktree.
    (dest_repo_root / ".git").mkdir(parents=True, exist_ok=True)

    row_a_staging = tmp_path / "row-a-staging"
    _write(row_a_staging / "README.md", "row-a-transformed\n")
    row_b_staging = tmp_path / "row-b-staging"
    _write(row_b_staging / "nested.txt", "row-b\n")

    target_a = publish.ResolvedTarget(
        name="row-a",
        mode="subdir",
        source_dir=tmp_path / "source-a",
        dest_dir=dest_repo_root,
    )
    target_b = publish.ResolvedTarget(
        name="row-b",
        mode="subdir",
        source_dir=tmp_path / "source-b",
        dest_dir=dest_repo_root / "sub" / "nested",
    )

    views = publish.assemble_per_root_artifact_views(
        {dest_repo_root: [target_a, target_b]},
        {"row-a": row_a_staging, "row-b": row_b_staging},
    )
    try:
        assert set(views.keys()) == {dest_repo_root}
        view_root = views[dest_repo_root]
        assert _enumerate_files(view_root) == {
            "README.md",
            "sub/nested/nested.txt",
        }
    finally:
        publish.discard_per_root_artifact_views(views)
    for view_root in views.values():
        assert not view_root.exists()

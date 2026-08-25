"""
coordinator_core.warm.tests.test_engine_root_definition

Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C2

Pins the single definition this plan collapses seven duplicated
`Path(__file__).resolve().parents[2]` computations onto: a directory is an
engine root iff it carries a valid `coordinator_core/_engine_stamp`. C2 is
definition only -- no fail-closed raise here (that is C4) and no rewired
caller (that is C3), so these tests exercise `engine_root` in isolation.
"""

from pathlib import Path

from coordinator_core.warm import engine_root
from coordinator_core.warm.skew import write_engine_stamp


def _bare_tree(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    (root / "coordinator_core").mkdir(parents=True)
    return root


def test_unstamped_tree_is_not_an_engine_root(tmp_path):
    root = _bare_tree(tmp_path)
    assert engine_root.is_engine_root(root) is False
    assert engine_root.resolve_engine_root(root) is None


def test_stamped_tree_is_an_engine_root(tmp_path):
    root = _bare_tree(tmp_path)
    write_engine_stamp(root, "deadbeef")
    assert engine_root.is_engine_root(root) is True
    assert engine_root.resolve_engine_root(root) == root


def test_empty_stamp_file_is_not_valid(tmp_path):
    root = _bare_tree(tmp_path)
    stamp = root / "coordinator_core" / engine_root.ENGINE_STAMP_FILENAME
    stamp.write_bytes(b"")
    assert engine_root.is_engine_root(root) is False
    assert engine_root.resolve_engine_root(root) is None


def test_nonexistent_root_is_not_an_engine_root(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert engine_root.is_engine_root(missing) is False
    assert engine_root.resolve_engine_root(missing) is None


def test_stamp_filename_matches_skew_module():
    from coordinator_core.warm import skew

    assert engine_root.ENGINE_STAMP_FILENAME == skew.ENGINE_STAMP_FILENAME

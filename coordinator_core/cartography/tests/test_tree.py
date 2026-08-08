"""
coordinator_core.cartography.tests.test_tree

Unit tests for coordinator_core.cartography.tree (pure functions) and the
import-guard + registry assertions for the thin cartography.tree op wrapper.

Coverage:
  (a) _lang_for  — known + unknown extension mapping
  (b) _loc_for   — newline count (with/without trailing newline), binary file -> None
  (c) list_tracked_files — tmp_path git fixture, tracked files only (untracked excluded)
  (d) build_tree — {path: {lang, loc}} shape over a tmp_path git fixture
  (e) AC5 regression — cartography.tree over THIS repo reproduces
      coordinator_core/authz/classification.py == 932 loc (not the atlas's
      inflated 1,462)
  (f) import-guard + registry — "cartography.tree" registered after import
  (g) op handler — missing target_root raises ValueError; happy path delegates
      to build_tree via the path_guard-contained root

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_tree  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_tree import _cartography_tree
from coordinator_core.cartography.tree import (
    _lang_for,
    _loc_for,
    build_tree,
    list_tracked_files,
)

_OP_NAME = "cartography.tree"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_tree @register_op did not fire"
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "cartography-test@claude-klabauter.test")
    _git("config", "user.name", "Cartography Test")
    _git("config", "commit.gpgsign", "false")

    (root / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (root / "b.md").write_text("# heading\nno trailing newline", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "c.js").write_text("console.log(1);\n", encoding="utf-8")
    (root / "unknown_ext.xyz").write_text("mystery\n", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "seed")

    # Untracked file — must NOT appear in list_tracked_files/build_tree.
    (root / "untracked.py").write_text("ghost\n", encoding="utf-8")

    return root


# ---------------------------------------------------------------------------
# _lang_for
# ---------------------------------------------------------------------------


def test_lang_for_known_extension():
    assert _lang_for(Path("foo.py")) == "python"
    assert _lang_for(Path("foo.md")) == "markdown"
    assert _lang_for(Path("foo.JS")) == "javascript"


def test_lang_for_unknown_extension():
    assert _lang_for(Path("foo.xyz")) == "unknown"
    assert _lang_for(Path("foo")) == "unknown"


# ---------------------------------------------------------------------------
# _loc_for
# ---------------------------------------------------------------------------


def test_loc_for_trailing_newline(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert _loc_for(f) == 3


def test_loc_for_no_trailing_newline(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("line1\nline2", encoding="utf-8")
    assert _loc_for(f) == 2


def test_loc_for_empty_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("", encoding="utf-8")
    assert _loc_for(f) == 0


def test_loc_for_binary_file_returns_none(tmp_path):
    f = tmp_path / "f.bin"
    f.write_bytes(b"\xff\xfe\x00\x01binary\x80\x81")
    assert _loc_for(f) is None


def test_loc_for_missing_file_returns_none(tmp_path):
    assert _loc_for(tmp_path / "does-not-exist.txt") is None


# ---------------------------------------------------------------------------
# list_tracked_files / build_tree
# ---------------------------------------------------------------------------


def test_list_tracked_files_excludes_untracked(git_repo):
    tracked = list_tracked_files(git_repo)
    assert "a.py" in tracked
    assert "b.md" in tracked
    assert "sub/c.js" in tracked
    assert "unknown_ext.xyz" in tracked
    assert "untracked.py" not in tracked


def test_build_tree_shape(git_repo):
    result = build_tree(git_repo)
    assert result["file_count"] == 4
    assert set(result["files"]) == {"a.py", "b.md", "sub/c.js", "unknown_ext.xyz"}
    assert result["files"]["a.py"] == {"lang": "python", "loc": 3}
    assert result["files"]["b.md"]["lang"] == "markdown"
    assert result["files"]["b.md"]["loc"] == 2  # no trailing newline
    assert result["files"]["sub/c.js"]["lang"] == "javascript"
    assert result["files"]["unknown_ext.xyz"]["lang"] == "unknown"


def test_build_tree_raises_outside_git_worktree(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(RuntimeError):
        build_tree(not_a_repo)


def test_list_tracked_files_scope_narrows_enumeration(git_repo):
    scoped = list_tracked_files(git_repo, scope=["sub"])
    assert scoped == ["sub/c.js"]


def test_build_tree_scope_narrows_result(git_repo):
    result = build_tree(git_repo, scope=["sub"])
    assert result["file_count"] == 1
    assert set(result["files"]) == {"sub/c.js"}


def test_build_tree_absent_scope_unchanged(git_repo):
    unscoped = build_tree(git_repo)
    explicit_none = build_tree(git_repo, scope=None)
    assert unscoped == explicit_none


# ---------------------------------------------------------------------------
# AC5 — regression: classification.py's reported loc must match ground truth
# (a plain newline count of the file on disk), not the atlas's inflated 1,462
# figure. classification.py is the shared authz seam that every op-
# registration chunk appends affirmation blocks to, so its true line count
# grows over time (932 at original plan-authoring, 1,063+ as of this op's own
# C2/C3/C4 registrations) — pinning a frozen literal here would make this
# regression test fail on every future op registration for a reason that has
# nothing to do with tree.py's correctness. Assert against freshly-computed
# ground truth instead, so the test locks in "matches wc -l", not a snapshot.
# ---------------------------------------------------------------------------


def test_ac5_classification_py_loc_matches_ground_truth_not_atlas_1462():
    result = build_tree(_REPO_ROOT)
    key = "coordinator_core/authz/classification.py"
    assert key in result["files"], (
        f"{key} not found in cartography.tree output over {_REPO_ROOT} — "
        "is this test running from the claude-klabauter repo root?"
    )
    loc = result["files"][key]["loc"]

    target_path = Path(_REPO_ROOT) / key
    ground_truth_loc = len(target_path.read_text(encoding="utf-8").splitlines())

    assert loc == ground_truth_loc, (
        f"AC5 regression: cartography.tree reports classification.py loc={loc}, "
        f"but a direct newline count of the file is {ground_truth_loc}."
    )
    assert loc != 1462, (
        "AC5 regression: cartography.tree reproduces the atlas's inflated "
        "1,462 figure for classification.py — this primitive exists to fix "
        "exactly that miscount."
    )
    assert loc != 1462


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


def test_op_missing_target_root_raises_value_error():
    with pytest.raises(ValueError):
        _cartography_tree({})


def test_op_happy_path(git_repo):
    result = _cartography_tree({"target_root": str(git_repo)})
    assert result["file_count"] == 4
    assert "a.py" in result["files"]


def test_op_scope_narrows_result(git_repo):
    result = _cartography_tree({"target_root": str(git_repo), "scope": ["sub"]})

    assert result["file_count"] == 1
    assert set(result["files"]) == {"sub/c.js"}

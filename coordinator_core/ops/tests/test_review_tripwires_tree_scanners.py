"""tree_scanners selects tracked tests that walk the own directory of a created path."""

from __future__ import annotations

from coordinator_core.ops import review_tripwires as rt
from coordinator_core.ops.tests.test_review_tripwires import _repo

WALKER = 'from pathlib import Path\nfor p in Path("pkg_core").rglob("*.py"):\n    pass\n'
CREATED = "pkg_core/sub/new.py"


def test_walker_of_own_directory_is_selected(tmp_path):
    body = WALKER.replace("pkg_core", "pkg_core/sub")
    repo = _repo(tmp_path, {"tests/test_walk.py": body})
    assert rt.tree_scanners(repo, [CREATED]) == ["tests/test_walk.py"]


def test_walker_of_shallower_ancestor_is_not_selected(tmp_path):
    repo = _repo(tmp_path, {"tests/test_walk.py": WALKER})
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_walker_of_unrelated_dir_is_not_selected(tmp_path):
    body = WALKER.replace("pkg_core", "other_dir")
    repo = _repo(tmp_path, {"tests/test_walk.py": body})
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_literal_without_walk_primitive_is_not_selected(tmp_path):
    repo = _repo(tmp_path, {"tests/test_lit.py": 'X = "pkg_core"\n'})
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_dot_and_empty_literals_never_count(tmp_path):
    body = 'import os\nos.walk(".")\nX = ""\n'
    repo = _repo(tmp_path, {"tests/test_dot.py": body})
    assert rt.tree_scanners(repo, ["new.py", CREATED]) == []


def test_literal_equal_to_created_path_itself_is_not_an_ancestor(tmp_path):
    body = 'import os\nos.walk("x")\nX = "pkg_core/sub/new.py"\n'
    repo = _repo(tmp_path, {"tests/test_self.py": body})
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_many_shallow_walkers_excluded_and_output_sorted(tmp_path):
    own = WALKER.replace("pkg_core", "pkg_core/sub")
    files = {f"tests/test_shallow_{i}.py": WALKER for i in range(20)}
    files.update({"tests/test_b.py": own, "tests/test_a.py": own})
    repo = _repo(tmp_path, files)
    assert rt.tree_scanners(repo, [CREATED]) == ["tests/test_a.py", "tests/test_b.py"]


def test_untracked_module_is_not_opened(tmp_path):
    repo = _repo(tmp_path, {"tests/test_a.py": "X = 1\n"})
    (repo / "tests" / "test_b.py").write_text(WALKER, encoding="utf-8")
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_no_created_paths_is_empty(tmp_path):
    repo = _repo(tmp_path, {"tests/test_walk.py": WALKER})
    assert rt.tree_scanners(repo, []) == []

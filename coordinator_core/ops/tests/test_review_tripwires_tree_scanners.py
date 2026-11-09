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


def test_separator_variants_of_own_directory_are_selected(tmp_path):
    files = {
        "tests/test_back.py": 'import os\nos.walk("pkg_core\\\\sub")\n',
        "tests/test_dot.py": 'import os\nos.walk("./pkg_core//sub/")\n',
    }
    repo = _repo(tmp_path, files)
    assert rt.tree_scanners(repo, [CREATED]) == ["tests/test_back.py", "tests/test_dot.py"]


def test_component_text_alone_is_not_the_own_directory(tmp_path):
    body = 'import os\nos.walk("pkg_core")\nY = "sub"\nZ = "pkg_core_sub"\n'
    repo = _repo(tmp_path, {"tests/test_parts.py": body})
    assert rt.tree_scanners(repo, [CREATED]) == []


def test_non_ascii_directory_is_still_matched(tmp_path):
    body = 'import os\nos.walk("pkg_é/sub")\n'
    repo = _repo(tmp_path, {"tests/test_u.py": body})
    assert rt.tree_scanners(repo, ["pkg_é/sub/new.py"]) == ["tests/test_u.py"]


def test_probe_agrees_with_literal_definition(tmp_path):
    from coordinator_core.ops.review_tripwires import _LITERAL_RE, _norm, walked_created

    for body in (b'p.glob("pkg_core/sub")', b'"x"p.glob("pkg_core/sub" ', b"it's p.rglob(\"pkg_core/sub\")"):
        lits = {_norm(m.group(2).decode()).rstrip("/") for m in _LITERAL_RE.finditer(body)}
        assert bool(walked_created(body, [CREATED])) == ("pkg_core/sub" in lits)


def test_no_created_paths_is_empty(tmp_path):
    repo = _repo(tmp_path, {"tests/test_walk.py": WALKER})
    assert rt.tree_scanners(repo, []) == []

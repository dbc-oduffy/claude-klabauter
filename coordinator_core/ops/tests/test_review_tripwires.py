"""review_tripwires: literal-name scan of test modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.review_tripwires import literal_namers, names_in

pytestmark = [pytest.mark.cadence]

EDITED = "ops/coverage-oracles/run_pass.py"


def _repo(tmp_path: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _selected(tmp_path, literal, edited=EDITED, **kw):
    repo = _repo(tmp_path, {"tests/test_t.py": f"X = [{literal}]\n"})
    return list(literal_namers(repo, [edited], **kw))


def test_bare_basename_literal_selected(tmp_path):
    assert _selected(tmp_path, '"run_pass.py"') == ["tests/test_t.py"]


def test_component_suffix_and_full_path_selected(tmp_path):
    assert _selected(tmp_path, '"coverage-oracles/run_pass.py"') == ["tests/test_t.py"]


def test_full_path_selected(tmp_path):
    assert _selected(tmp_path, f'"{EDITED}"') == ["tests/test_t.py"]


def test_longer_basename_not_selected(tmp_path):
    assert _selected(tmp_path, '"test_run_pass.py"') == []


def test_other_directory_not_selected(tmp_path):
    assert _selected(tmp_path, '"other/run_pass.py"') == []


def test_backslash_literal_selected(tmp_path):
    assert _selected(tmp_path, '"ops\\\\coverage-oracles\\\\run_pass.py"') == [
        "tests/test_t.py"
    ]


def test_bare_generic_basename_not_selected(tmp_path):
    assert _selected(tmp_path, '"__init__.py"', edited="x/indexer_arm/__init__.py") == []


def test_qualified_generic_basename_selected(tmp_path):
    assert _selected(
        tmp_path, '"indexer_arm/__init__.py"', edited="x/indexer_arm/__init__.py"
    ) == ["tests/test_t.py"]


def test_non_py_edited_path_selected(tmp_path):
    assert _selected(tmp_path, '"data.json"', edited="fixtures/data.json") == [
        "tests/test_t.py"
    ]


def test_exclude_honoured(tmp_path):
    assert _selected(tmp_path, '"run_pass.py"', exclude={"tests/test_t.py"}) == []


def test_non_test_module_not_scanned(tmp_path):
    repo = _repo(tmp_path, {"lib/helper.py": 'X = "run_pass.py"\n'})
    assert literal_namers(repo, [EDITED]) == {}


def test_suffix_named_test_module_scanned(tmp_path):
    repo = _repo(tmp_path, {"t/run_test.py": 'X = "run_pass.py"\n'})
    assert list(literal_namers(repo, [EDITED])) == ["t/run_test.py"]


def test_empty_named_returns_empty(tmp_path):
    repo = _repo(tmp_path, {"tests/test_t.py": '"run_pass.py"\n'})
    assert literal_namers(repo, []) == {}


def test_maps_to_sorted_named_paths(tmp_path):
    repo = _repo(tmp_path, {"tests/test_t.py": '["b.py", "a.py"]\n'})
    assert literal_namers(repo, ["z/b.py", "z/a.py", "z/c.py"]) == {
        "tests/test_t.py": ["z/a.py", "z/b.py"]
    }


def test_names_in_single_file():
    assert names_in(b"x = 'run_pass.py'", [EDITED, "q/other.py"]) == [EDITED]
    assert names_in(b"x = 1", [EDITED]) == []


def test_testpaths_bound_the_walk_tripwire(tmp_path):
    body = 'X = "run_pass.py"\n'
    repo = _repo(
        tmp_path,
        {
            "pyproject.toml": (
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                'norecursedirs = ["_archived"]\n'
            ),
            "tests/test_a.py": body,
            "other/test_b.py": body,
            "tests/_archived/test_c.py": body,
            "tests/.hidden/test_d.py": body,
        },
    )
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_a.py"]


def test_stray_apostrophe_does_not_hide_literal_tripwire(tmp_path):
    repo = _repo(
        tmp_path,
        {"tests/test_t.py": "# don't\nA = 1\nFILES = [\"run_pass.py\"]\n"},
    )
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_t.py"]


def test_scan_writes_nothing_and_reads_no_index_tripwire(tmp_path):
    from coordinator_core.ops import review_tripwires as rt

    for attr in ("git_index", "machinery_paths", "json", "hashlib"):
        assert not hasattr(rt, attr)
    repo = _repo(tmp_path, {"tests/test_t.py": 'X = "run_pass.py"\n'})
    before = sorted(str(p) for p in repo.rglob("*"))
    assert list(literal_namers(repo, [EDITED])) == ["tests/test_t.py"]
    assert sorted(str(p) for p in repo.rglob("*")) == before

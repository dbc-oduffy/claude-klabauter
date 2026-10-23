"""review_tripwires: literal-name scan of tracked test modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_tripwires import literal_namers, names_in

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EDITED = "ops/coverage-oracles/run_pass.py"


def _repo(tmp_path: Path, files: dict, untracked: dict | None = None) -> Path:
    for rel, text in {**files, **(untracked or {})}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=_NOWIN)
    subprocess.run(
        ["git", "add", "--", *files], cwd=tmp_path, check=True, creationflags=_NOWIN
    )
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


def test_untracked_module_not_scanned(tmp_path):
    repo = _repo(
        tmp_path,
        {"tests/test_a.py": "pass\n"},
        untracked={"tests/test_b.py": 'X = "run_pass.py"\n'},
    )
    assert literal_namers(repo, [EDITED]) == {}


def test_non_test_module_not_scanned(tmp_path):
    repo = _repo(tmp_path, {"lib/helper.py": 'X = "run_pass.py"\n'})
    assert literal_namers(repo, [EDITED]) == {}


def test_suffix_named_test_module_scanned(tmp_path):
    repo = _repo(tmp_path, {"t/run_test.py": 'X = "run_pass.py"\n'})
    assert list(literal_namers(repo, [EDITED])) == ["t/run_test.py"]


def test_no_git_dir_returns_empty(tmp_path):
    (tmp_path / "test_x.py").write_text('"run_pass.py"', encoding="utf-8")
    assert literal_namers(tmp_path, [EDITED]) == {}


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

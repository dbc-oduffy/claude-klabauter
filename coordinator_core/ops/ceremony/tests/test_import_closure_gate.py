"""`import_closure_gate`: a changed `.py` whose intra-repo import is absent from the landed tree is refused.

Fixtures reproduce the shapes of three example-game-repo commits (7379135bc0, d5ff5e54b5, 9693dd80d8) as
in-memory trees; the gate reads only through `read_source`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Sequence

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony.commit_gates import import_closure_gate


def _gate(tree: Dict[str, str], changed: Sequence[str], root: Path = Path("unused")):
    def read(rel: str):
        text = tree.get(rel)
        return None if text is None else text.encode("utf-8")

    return import_closure_gate(root, list(changed), read)


def test_missing_symbol_in_existing_module_is_refused():
    tree = {
        "pkg/__init__.py": "",
        "pkg/create_stage.py": "ARM = 1\n",
        "pkg/user.py": "from pkg.create_stage import ARM_BYPASS\n",
    }
    out = _gate(tree, ["pkg/user.py"])
    assert not out.passed
    assert "pkg/user.py:1" in out.diagnostics[0]
    assert "ARM_BYPASS" in out.diagnostics[0] and "pkg/create_stage.py" in out.diagnostics[0]


def test_missing_symbol_resolves_once_defined():
    tree = {
        "pkg/__init__.py": "",
        "pkg/create_stage.py": "ARM_BYPASS = 1\n",
        "pkg/user.py": "from pkg.create_stage import ARM_BYPASS\n",
    }
    assert _gate(tree, ["pkg/user.py"]).passed


def test_missing_exception_name_is_refused():
    tree = {
        "pkg/__init__.py": "",
        "pkg/noun_lift.py": "class Other(Exception):\n    pass\n",
        "pkg/user.py": "from pkg.noun_lift import NounMapRefused, Other\n",
    }
    out = _gate(tree, ["pkg/user.py"])
    assert not out.passed
    assert len(out.diagnostics) == 1 and "NounMapRefused" in out.diagnostics[0]


def test_test_importing_absent_module_is_refused_with_path_to_add():
    tree = {
        "pkg/__init__.py": "",
        "pkg/tests/test_mapped_compare.py": "from pkg.mapped_compare import compare\n",
    }
    out = _gate(tree, ["pkg/tests/test_mapped_compare.py"])
    assert not out.passed
    assert "pkg/mapped_compare.py" in out.diagnostics[0]


def test_plain_import_of_absent_module_is_refused():
    tree = {"pkg/__init__.py": "", "pkg/user.py": "import pkg.gone\n"}
    out = _gate(tree, ["pkg/user.py"])
    assert not out.passed and "pkg/gone.py" in out.diagnostics[0]


def test_package_reexport_submodule_relative_and_getattr_pass():
    tree = {
        "pkg/__init__.py": "from pkg.impl import Thing\ntry:\n    from pkg.impl import Fallback\nexcept C:\n    Fallback = None\n",
        "pkg/impl.py": "class Thing: ...\n",
        "pkg/sub/__init__.py": "def __getattr__(name):\n    return 1\n",
        "pkg/star.py": "from pkg.impl import *\n",
        "pkg/a.py": (
            "from pkg import Thing, impl\n"
            "from . import impl as i2\n"
            "from .impl import Thing as T\n"
            "from .sub import anything_dynamic\n"
            "from pkg.star import Thing\n"
        ),
    }
    out = _gate(tree, ["pkg/a.py"])
    assert out.passed, out.diagnostics


def test_relative_import_of_missing_name_is_refused():
    tree = {"pkg/__init__.py": "", "pkg/impl.py": "x = 1\n", "pkg/a.py": "from .impl import y\n"}
    out = _gate(tree, ["pkg/a.py"])
    assert not out.passed and "`y`" in out.diagnostics[0]


def test_stdlib_third_party_and_guarded_imports_are_skipped():
    tree = {
        "pkg/__init__.py": "",
        "pkg/a.py": (
            "import os\nfrom collections import nope\nimport requests\n"
            "try:\n    import pkg.optional\nexcept ImportError:\n    pass\n"
        ),
    }
    assert _gate(tree, ["pkg/a.py"]).passed


def test_non_python_commit_skips_and_unchanged_importer_is_not_checked():
    tree = {"pkg/__init__.py": "", "pkg/bad.py": "from pkg.gone import x\n"}
    out = _gate(tree, ["README.md"])
    assert out.passed and out.skipped
    out = _gate(tree, ["pkg/__init__.py"])
    assert out.passed and not out.skipped


def test_deleted_target_in_landed_tree_is_refused():
    tree = {"pkg/__init__.py": "", "pkg/user.py": "from pkg.old import x\n"}
    assert not _gate(tree, ["pkg/user.py"]).passed


def _git(args, cwd):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_pre_commit_gates_refuses_import_of_symbol_bound_nowhere(tmp_path: Path):
    # A symbol bound nowhere (not even in the worktree) is outside the `leak_gate` import_closure
    # detector, which reports only definers that exist uncommitted.
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "pkg/__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg/stage.py").write_text("OLD = 1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout

    (repo / "pkg/user.py").write_text("from pkg.stage import ARM_BYPASS\n", encoding="utf-8")
    result = commit_v2._handler({"paths": ["pkg/user.py"], "message": "land user\n"}, repo_root=repo / ".git")
    assert result["committed"] is False, result
    assert "import_closure_gate" in result["error"] and "ARM_BYPASS" in result["error"]
    assert "pkg/stage.py" in result["error"]
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout == head

    (repo / "pkg/stage.py").write_text("OLD = 1\nARM_BYPASS = 2\n", encoding="utf-8")
    result = commit_v2._handler(
        {"paths": ["pkg/user.py", "pkg/stage.py"], "message": "land both\n"}, repo_root=repo / ".git"
    )
    assert result["committed"] is True, result


def test_namespace_package_directory_is_not_refused_but_absent_one_is(tmp_path: Path):
    (tmp_path / "pkg" / "ns").mkdir(parents=True)
    tree = {
        "pkg/__init__.py": "",
        "pkg/user.py": "from pkg import ns\nimport pkg.ns\nfrom pkg import gone\n",
    }
    out = _gate(tree, ["pkg/user.py"], tmp_path)
    assert not out.passed
    assert len(out.diagnostics) == 1 and "`gone`" in out.diagnostics[0]

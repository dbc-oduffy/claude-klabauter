"""`import_closure_gate`: a changed `.py` whose intra-repo import is absent from the landed tree is refused.

Fixtures reproduce the shapes of three example-game-repo commits (7379135bc0, d5ff5e54b5, 9693dd80d8) as
in-memory trees; the gate reads only through `read_source`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Sequence

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


def test_namespace_package_directory_is_not_refused_but_absent_one_is(tmp_path: Path):
    (tmp_path / "pkg" / "ns").mkdir(parents=True)
    tree = {
        "pkg/__init__.py": "",
        "pkg/user.py": "from pkg import ns\nimport pkg.ns\nfrom pkg import gone\n",
    }
    out = _gate(tree, ["pkg/user.py"], tmp_path)
    assert not out.passed
    assert len(out.diagnostics) == 1 and "`gone`" in out.diagnostics[0]


def _commit_op_gates(tmp_path: Path, tree: Dict[str, str], worktree: Dict[str, str], changed, advisories=None):
    from coordinator_core.ops.ceremony import commit_v2

    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t.example", "-c", "user.name=t", *args], cwd=str(tmp_path),
            check=True, capture_output=True, **no_console_creationflags(),
        )

    for files in (tree, worktree):
        for rel, text in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(text, encoding="utf-8")
        if files is tree:
            git("init", "-q")
            git("add", "--", *tree)
            git("commit", "-q", "-m", "seed")

    def read(rel: str):
        text = tree.get(rel)
        return None if text is None else text.encode("utf-8")

    return commit_v2._pre_commit_gates(tmp_path, list(changed), [], read, advisories=advisories)


_PLANT_TREE = {
    "pkg/__init__.py": "",
    "pkg/mod.py": "x = 1\n",
    "pkg/user.py": "from pkg import mod\n\nmod.record_claim()\n",
}
_PLANT_WORKTREE = {**_PLANT_TREE, "pkg/mod.py": "x = 1\n\n\ndef record_claim():\n    return 1\n"}


def test_commit_op_refuses_an_attribute_call_to_a_name_only_the_worktree_has(tmp_path: Path):
    refusal = _commit_op_gates(tmp_path, _PLANT_TREE, _PLANT_WORKTREE, ["pkg/user.py"])
    assert refusal is not None and refusal.startswith("landed_names_gate:")
    assert "pkg/user.py:3" in refusal and "pkg.mod.record_claim" in refusal


def test_commit_op_lands_the_call_once_the_name_is_committed(tmp_path: Path):
    tree = {**_PLANT_TREE, "pkg/mod.py": _PLANT_WORKTREE["pkg/mod.py"]}
    assert _commit_op_gates(tmp_path, tree, _PLANT_WORKTREE, ["pkg/user.py"]) is None


def test_landed_names_gate_with_a_checker_that_cannot_run_passes_and_says_so(tmp_path: Path, monkeypatch):
    from coordinator_core.ops.ceremony import landed_names

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(landed_names, "_Checker", boom)
    advisories: list = []
    assert _commit_op_gates(tmp_path, _PLANT_TREE, _PLANT_WORKTREE, ["pkg/user.py"], advisories) is None
    assert any("landed_names_gate: could not run" in a for a in advisories)

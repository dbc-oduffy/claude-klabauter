"""Unit tests for the import_closure detector against real throwaway git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import HeadView, import_closure, leak_gate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]



def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", "def old_fn():\n    pass\n")
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _gate(root, paths):
    return leak_gate(root, paths, detectors=[import_closure])


def test_falsifier_import_of_uncommitted_name_refuses(repo):
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/a.py", "from pkg.b import new_fn\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "imports new_fn from pkg.b; pkg/b.py is uncommitted in the worktree" in out.diagnostics[0]
    assert "pkg/a.py:1" in out.diagnostics[0]


def test_definer_in_commit_passes(repo):
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/a.py", "from pkg.b import new_fn\n")
    assert _gate(repo, ["pkg/a.py", "pkg/b.py"]).passed


def test_existing_name_at_head_passes(repo):
    _write(repo, "pkg/a.py", "from pkg.b import old_fn\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_new_untracked_module_refuses(repo):
    _write(repo, "pkg/c.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.c\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "pkg/c.py is uncommitted in the worktree" in out.diagnostics[0]


def test_new_untracked_module_in_commit_passes(repo):
    _write(repo, "pkg/c.py", "y = 1\n")
    _write(repo, "pkg/a.py", "import pkg.c\n")
    assert _gate(repo, ["pkg/a.py", "pkg/c.py"]).passed


def test_submodule_from_import(repo):
    _write(repo, "pkg/c.py", "y = 1\n")
    _write(repo, "pkg/a.py", "from pkg import c\n")
    assert not _gate(repo, ["pkg/a.py"]).passed
    assert _gate(repo, ["pkg/a.py", "pkg/c.py"]).passed
    _write(repo, "pkg/a.py", "from pkg import b\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_deleted_target_refuses(repo):
    (repo / "pkg/b.py").unlink()
    _write(repo, "pkg/a.py", "import pkg.b\n")
    out = _gate(repo, ["pkg/a.py", "pkg/b.py"])
    assert not out.passed
    assert "deleted by this commit" in out.diagnostics[0]


def test_relative_import(repo):
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/a.py", "from .b import new_fn\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed and "imports new_fn from pkg.b" in out.diagnostics[0]


def test_binding_forms_satisfy(repo):
    _write(
        repo, "pkg/b.py",
        "import os\nfrom typing import Any as T\nA = 1\nB: int = 2\nclass K: ...\n"
        "if A:\n    C = 3\ntry:\n    D = 4\nexcept Exception:\n    D = 5\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "b")
    _write(repo, "pkg/a.py", "from pkg.b import os, T, A, B, K, C, D\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_star_import_and_getattr_satisfy(repo):
    _write(repo, "pkg/b.py", "from os.path import *\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "star")
    _write(repo, "pkg/a.py", "from pkg.b import anything\n")
    assert _gate(repo, ["pkg/a.py"]).passed
    _write(repo, "pkg/b.py", "def __getattr__(n):\n    return n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ga")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_name_missing_everywhere_is_absent_from_head(repo):
    # b.py is edited in the worktree only, so the target is parsed and the name is nowhere.
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\n# edited\n")
    _write(repo, "pkg/a.py", "from pkg.b import nowhere\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "pkg/b.py is absent from HEAD" in out.diagnostics[0]


def test_third_party_and_stdlib_ignored(repo):
    _write(repo, "pkg/a.py", "import os\nimport yaml\nfrom collections import nope\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_type_checking_branch_skipped(repo):
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/a.py", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg.b import new_fn\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_syntax_error_skipped(repo):
    _write(repo, "pkg/a.py", "from pkg.b import (\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_no_added_import_line_or_dirty_alias_read_skips_parse(repo, monkeypatch):
    _write(repo, "pkg/a.py", "x = 2\n")

    def _boom(*a, **k):
        raise AssertionError("parsed")

    monkeypatch.setattr(import_closure.ast, "parse", _boom)
    assert _gate(repo, ["pkg/a.py"]).passed


def _falsifier_repo(repo, caller_body="    return li.old_fn() + li.new_fn()\n", head_body="    return li.old_fn()\n", imp="from pkg import li"):
    _write(repo, "pkg/caller.py", f"def run():\n    {imp}\n{head_body}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "caller")
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/caller.py", f"def run():\n    {imp}\n{caller_body}")


def test_attribute_read_of_uncommitted_name_refuses(repo):
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "li")
    _falsifier_repo(repo)
    out = _gate(repo, ["pkg/caller.py"])
    assert not out.passed
    assert sum("import_closure --" in d for d in out.diagnostics) == 1
    assert "references new_fn from pkg.li; pkg/li.py is uncommitted in the worktree" in out.diagnostics[0]
    assert "pkg/caller.py:3" in out.diagnostics[0]


def test_attribute_read_with_definer_in_gate_passes(repo):
    _falsifier_repo(repo)
    assert _gate(repo, ["pkg/caller.py", "pkg/li.py"]).passed


@pytest.mark.parametrize(
    "imp,body",
    [
        ("import pkg.li as x", "    return x.new_fn()\n"),
        ("import pkg.li", "    return pkg.li.new_fn()\n"),
        ("from . import li", "    return li.new_fn()\n"),
    ],
)
def test_attribute_read_import_forms_refuse(repo, imp, body):
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "li")
    _falsifier_repo(repo, caller_body=body, head_body="    return None\n", imp=imp)
    out = _gate(repo, ["pkg/caller.py"])
    assert not out.passed
    assert "references new_fn from pkg.li" in out.diagnostics[0]


def test_attribute_read_of_unchanged_target_does_not_parse(repo, monkeypatch):
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n")
    _write(repo, "pkg/caller.py", "def run():\n    from pkg import li\n    return li.old_fn()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "li")
    _write(repo, "pkg/caller.py", "def run():\n    from pkg import li\n    return li.old_fn() + 1\n")

    def _boom(*a, **k):
        raise AssertionError("parsed")

    monkeypatch.setattr(import_closure.ast, "parse", _boom)
    assert _gate(repo, ["pkg/caller.py"]).passed


def test_attribute_name_absent_everywhere_is_not_reported(repo):
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "li")
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n# edited\n")
    _write(repo, "pkg/caller.py", "def run():\n    from pkg import li\n    return li.ghost()\n")
    assert _gate(repo, ["pkg/caller.py"]).passed


def test_shadowed_alias_is_not_reported(repo):
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "li")
    _write(repo, "pkg/li.py", "def old_fn():\n    pass\n\ndef new_fn():\n    pass\n")
    _write(repo, "pkg/caller.py", "def run():\n    from pkg import li\n    li = object()\n    return li.new_fn()\n")
    assert _gate(repo, ["pkg/caller.py"]).passed


def test_package_attribute_that_is_a_committed_submodule_passes(repo):
    _write(repo, "pkg/__init__.py", "# edited\n")
    _write(repo, "pkg/caller.py", "import pkg\n\ndef run():\n    return pkg.b.old_fn()\n")
    assert _gate(repo, ["pkg/caller.py"]).passed


def test_non_py_and_missing_worktree_paths_ignored(repo):
    _write(repo, "notes.md", "from pkg.b import new_fn\n")
    assert _gate(repo, ["notes.md", "pkg/gone.py"]).passed


def test_parenthesised_import_continuation_triggers(repo):
    _write(repo, "pkg/a.py", "from pkg.b import (\n    old_fn,\n)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "paren")
    _write(repo, "pkg/b.py", "def old_fn():\n    pass\n\n# edited\n")
    _write(repo, "pkg/a.py", "from pkg.b import (\n    old_fn,\n    ghost,\n)\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed and "imports ghost from pkg.b" in out.diagnostics[0]


_LAZY = "def f():\n    from coordinator_core.guard_messages.alternative_record import X\n    return X\n"


@pytest.fixture
def cc_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "coordinator_core/__init__.py", "")
    _write(tmp_path, "coordinator_core/guard.py", "def f():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(tmp_path, "coordinator_core/guard_messages/__init__.py", "")
    _write(tmp_path, "coordinator_core/guard_messages/alternative_record.py", "X = 1\n")
    _write(tmp_path, "coordinator_core/guard.py", _LAZY)
    return tmp_path


def _detect(root, paths, **kw):
    return import_closure.detect(root, paths, HeadView(root, import_closure.candidates(paths)), **kw)


def test_function_local_import_of_untracked_package_is_found(cc_repo):
    out = _detect(cc_repo, ["coordinator_core/guard.py"])
    assert len(out) == 1 and "guard_messages" in out[0].detail


def test_landed_text_without_the_import_yields_none(cc_repo):
    assert _detect(cc_repo, ["coordinator_core/guard.py"], landed_text=lambda rel: "def f():\n    return 1\n") == []


def test_landed_text_with_the_import_is_judged_over_the_worktree(cc_repo):
    _write(cc_repo, "coordinator_core/guard.py", "def f():\n    return 1\n")
    assert _detect(cc_repo, ["coordinator_core/guard.py"]) == []
    assert len(_detect(cc_repo, ["coordinator_core/guard.py"], landed_text=lambda rel: _LAZY)) == 1


def test_publish_gate_reports_the_same_module_over_the_committed_tree(cc_repo, tmp_path_factory):
    import importlib.util

    gate_path = Path(__file__).resolve().parents[3] / "coordinator" / "lib" / "percolate" / "import_closure.py"
    spec = importlib.util.spec_from_file_location("_percolate_import_closure", gate_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tree = tmp_path_factory.mktemp("committed")
    _write(tree, "coordinator_core/__init__.py", "")
    _write(tree, "coordinator_core/guard.py", _LAZY)
    _, violations = mod.find_import_closure_violations(tree)
    assert any("guard_messages" in entry for _, entry in violations)
    assert any("guard_messages" in f.detail for f in _detect(cc_repo, ["coordinator_core/guard.py"]))

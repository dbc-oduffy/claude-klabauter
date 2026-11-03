"""Unit tests for the call_signature detector against real throwaway git repos."""

from __future__ import annotations

import ast

import pytest

from coordinator_core.authoring_leaks import HeadView, call_signature, import_closure, leak_gate
from coordinator_core.authoring_leaks.tests._git_fixture import git as _git, write as _write

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_HEAD_B = "def f(a):\n    return a\n"
_DIRTY_B = "def f(a, withheld=None):\n    return a\n"


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", _HEAD_B)
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _gate(root, paths, **kw):
    return leak_gate(root, paths, detectors=[call_signature], **kw)


def _dirty(repo):
    _write(repo, "pkg/b.py", _DIRTY_B)


def test_keyword_leak_refuses_and_names_callee(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, withheld=2)\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "call_signature" in out.diagnostics[0]
    assert "unexpected keyword argument 'withheld'" in out.diagnostics[0]
    assert "add pkg/b.py to this commit" in out.diagnostics[0]
    assert "pkg/a.py:3" in out.diagnostics[0]


def test_arity_leak_refuses(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, 2)\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "too many positional arguments" in out.diagnostics[0]


def test_callee_in_gate_paths_gives_no_finding(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, withheld=2)\n")
    texts = {"pkg/a.py": "import pkg.b as b\n\nb.f(1, withheld=2)\n", "pkg/b.py": _DIRTY_B}
    assert _gate(repo, ["pkg/a.py", "pkg/b.py"], landed_text=texts.get).passed
    assert _gate(repo, ["pkg/a.py", "pkg/b.py"]).passed


def test_landed_text_replaces_the_worktree_read(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "x = 1\n")
    texts = {"pkg/a.py": "import pkg.b as b\n\nb.f(1, withheld=2)\n"}
    assert not _gate(repo, ["pkg/a.py"], landed_text=texts.get).passed


def test_call_failing_against_both_texts_gives_no_finding(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, nope=2)\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_clean_callee_never_parses_the_landing_file(repo, monkeypatch):
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, withheld=2)\n")
    parses = []
    real = ast.parse
    monkeypatch.setattr(ast, "parse", lambda *a, **k: parses.append(a) or real(*a, **k))
    assert _gate(repo, ["pkg/a.py"]).passed
    assert parses == []


def test_relative_import_is_resolved(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "from . import b\n\nb.f(1, withheld=2)\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "add pkg/b.py" in out.diagnostics[0]


def test_parenthesised_bare_name_import_is_refused(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "from pkg.b import (\n    f,\n)\n\n\ndef g():\n    return f(1, withheld=2)\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "pkg.b.f" in out.diagnostics[0]
    assert "pkg/a.py:7" in out.diagnostics[0]


def test_call_accepted_at_head_gives_no_finding(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1)\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_splat_typeerror_decorated_and_new_function_abstain(repo):
    _write(repo, "pkg/b.py", "import functools\n\n\ndef f(a):\n    return a\n\n\n@functools.cache\ndef d(a):\n    return a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")
    _write(
        repo,
        "pkg/b.py",
        "import functools\n\n\ndef f(a, withheld=None):\n    return a\n\n\n@functools.cache\ndef d(a, w=None):\n    return a\n\n\ndef n(a):\n    return a\n",
    )
    _write(
        repo,
        "pkg/a.py",
        "import pytest\nimport pkg.b as b\n\nb.f(1, **{})\nb.f(*[1], withheld=2)\nb.d(1, w=2)\nb.n(1, 2)\n"
        "with pytest.raises(TypeError):\n    b.f(1, withheld=2)\n",
    )
    assert _gate(repo, ["pkg/a.py"]).passed


def test_aliased_from_import_and_multiline_call_leak(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "from pkg.b import f as g\n\n\ndef run():\n    return g(\n        1,\n        withheld=(2),\n    )\n")
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "pkg/a.py:5" in out.diagnostics[0]


def test_calls_that_bind_at_head_pass_with_a_changed_signature(repo):
    _dirty(repo)
    _write(repo, "pkg/a.py", "from pkg.b import f\n\n\ndef run():\n    return f(1) + f(a=2)\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_default_leak_gate_path_sees_the_dirty_callee(repo):
    """The shared HeadView holds no callee path, so detect must build its own."""
    _dirty(repo)
    _write(repo, "pkg/a.py", "import pkg.b as b\n\nb.f(1, withheld=2)\n")
    out = leak_gate(repo, ["pkg/a.py"], detectors=[import_closure, call_signature])
    assert not out.passed
    assert any("call_signature" in d for d in out.diagnostics)


def test_keyword_line_added_to_an_existing_multiline_call_leaks(repo):
    head_a = "from pkg.b import f\n\n\ndef run():\n    return f(\n        1,\n    )\n"
    _write(repo, "pkg/a.py", head_a)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "landing")
    _dirty(repo)
    _write(repo, "pkg/a.py", head_a.replace("        1,\n", "        1,\n        withheld=2,\n"))
    out = _gate(repo, ["pkg/a.py"])
    assert not out.passed
    assert "add pkg/b.py to this commit" in out.diagnostics[0]


def test_landing_lines_the_commit_does_not_touch_are_not_judged(repo):
    head_a = "from pkg.b import f\n\n\ndef run():\n    return f(1, withheld=2)\n"
    _write(repo, "pkg/a.py", head_a)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "landing already calls it")
    _dirty(repo)
    assert _gate(repo, ["pkg/a.py"]).passed
    _write(repo, "pkg/a.py", head_a + "\n\nY = 1\n")
    assert _gate(repo, ["pkg/a.py"]).passed


def test_crlf_checkout_of_an_unedited_callee_is_not_dirty_but_an_edit_is(repo):
    view = HeadView(repo, ["pkg/b.py"])
    _write(repo, "pkg/b.py", _HEAD_B.replace("\n", "\r\n"))
    assert call_signature._dirty_text(view, "pkg/b.py") is None
    _write(repo, "pkg/b.py", _DIRTY_B.replace("\n", "\r\n"))
    assert call_signature._dirty_text(view, "pkg/b.py") == _DIRTY_B

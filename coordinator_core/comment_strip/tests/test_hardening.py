"""Tests for dispatch-brief hardening items 1-4: tooling docstring refs, source maps,
docstring-only-body syntax validity, and other tooling-comment KEEP patterns."""
from __future__ import annotations

import ast
from pathlib import Path

from coordinator_core.comment_strip import engine, keep_rules
from coordinator_core.comment_strip.engine import _plan_python, plan_file


def test_self_referencing_doc_keeps_all_docstrings():
    text = (
        '"""Module docstring used as CLI description."""\n'
        "import argparse\n"
        "p = argparse.ArgumentParser(description=__doc__)\n"
        "\n"
        "def f():\n"
        '    """Function docstring."""\n'
        "    return 1\n"
    )
    doc_keep = engine._self_references_own_doc(text)
    assert doc_keep is True
    res = _plan_python("mod.py", text, set(), False, doc_keep=doc_keep)
    assert res.changed is False
    assert res.new_text == text


def test_cross_file_module_doc_reference_keeps_docstring(tmp_path: Path):
    repo = tmp_path
    (repo / "target.py").write_text('"""Target module docstring."""\n\ndef g():\n    return 1\n', encoding="utf-8")
    (repo / "reader.py").write_text("import target\nprint(target.__doc__)\n", encoding="utf-8")
    referenced = engine._collect_docstring_referenced_modules(repo, ["target.py", "reader.py"])
    assert "target" in referenced
    res = plan_file(repo, "target.py", set(), frozenset(referenced))
    assert res.changed is False


def test_source_map_and_source_url_kept():
    assert keep_rules.is_tooling_comment("//# sourceMappingURL=app.js.map")
    assert keep_rules.is_tooling_comment("//# sourceURL=app.js")


def test_shellcheck_powershell_help_and_sql_hint_kept():
    assert keep_rules.is_tooling_comment("# shellcheck disable=SC2086")
    assert keep_rules.is_tooling_comment("#Requires -Version 5.1")
    assert keep_rules.is_tooling_comment("<#\n.SYNOPSIS\n  Does a thing.\n#>")
    assert keep_rules.is_tooling_comment("/*+ INDEX(t idx) */")
    assert keep_rules.is_tooling_comment("/*! preserved banner */")
    assert keep_rules.is_tooling_comment("// @preserve keep this")


def test_sole_body_docstring_removal_inserts_pass():
    text = (
        "class C:\n"
        '    """Only statement."""\n'
        "\n"
        "def f():\n"
        '    """Also only statement."""\n'
    )
    res = _plan_python("mod.py", text, set(), False)
    assert res.changed is True
    assert res.new_text is not None
    ast.parse(res.new_text)
    assert "pass" in res.new_text


def test_module_docstring_removal_does_not_need_pass():
    text = '"""Just a module docstring."""\n\nx = 1\n'
    res = _plan_python("mod.py", text, set(), False)
    assert res.changed is True
    ast.parse(res.new_text)
    assert "pass" not in res.new_text

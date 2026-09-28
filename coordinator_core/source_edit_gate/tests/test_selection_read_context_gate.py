"""Tests for the comment-only/doc-only literal read-context gate in
`coordinator_core.source_edit_gate.selection` -- a path/basename/dir literal
hit on such an edit counts only when the literal is used in a read context
(`open(`, `.read_text(`, `hashlib.`, a directory reader, etc.), never a bare
mention or an execution-only use (`subprocess`/`runpy`/`importlib`/argv).
`"string"`/`"code"` edits keep the pre-existing unconditional literal hit.
"""

from __future__ import annotations

from coordinator_core.source_edit_gate.selection import select_test_files


def _doc_only_repo(tmp_path, test_source: str):
    """A repo with one edited module (`package/mod.py`, module-docstring-only
    edit -- classifies `"doc-only"`) and one test file whose source is the
    caller-supplied `test_source`, mentioning the edited module's full
    repo-relative path (or its directory) as a string literal. `"package"`
    (not `"pkg"`) so the directory literal gate (>= 6 chars) is satisfied."""
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    pkg_dir = tmp_path / "package"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    mod = pkg_dir / "mod.py"
    original = '"""original docstring"""\n\ndef f():\n    return 1\n'
    edited = '"""a different docstring"""\n\ndef f():\n    return 1\n'
    mod.write_text(edited, encoding="utf-8")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_x.py"
    test_file.write_text(test_source, encoding="utf-8")

    all_files = ["conftest.py", "package/__init__.py", "package/mod.py", "tests/test_x.py"]
    return all_files, original, edited


def _select(tmp_path, test_source, classification="doc-only"):
    all_files, original, edited = _doc_only_repo(tmp_path, test_source)
    if classification == "code":
        # Force a "code" classification instead of "doc-only" -- some other
        # token besides the docstring also changed.
        original = '"""original docstring"""\n\ndef f():\n    return 1\n'
        edited = '"""a different docstring"""\n\ndef f():\n    return 2\n'
        (tmp_path / "package" / "mod.py").write_text(edited, encoding="utf-8")
    return select_test_files(
        str(tmp_path), ["package/mod.py"], all_files,
        file_texts={"package/mod.py": (original, edited)},
    )


def test_exec_only_literal_not_selected_for_doc_only_edit(tmp_path):
    test_source = (
        "import subprocess\n\n"
        "def test_it():\n"
        "    subprocess.run(['python', 'package/mod.py'])\n"
    )
    assert _select(tmp_path, test_source) == []


def test_read_text_literal_selected_for_doc_only_edit(tmp_path):
    test_source = (
        "from pathlib import Path\n\n"
        "def test_it():\n"
        "    assert Path('package/mod.py').read_text()\n"
    )
    assert _select(tmp_path, test_source) == ["tests/test_x.py"]


def test_hashlib_of_literal_selected_for_doc_only_edit(tmp_path):
    test_source = (
        "import hashlib\n\n"
        "def test_it():\n"
        "    assert hashlib.sha256('package/mod.py'.encode())\n"
    )
    assert _select(tmp_path, test_source) == ["tests/test_x.py"]


def test_glob_over_dir_selected_for_doc_only_edit(tmp_path):
    test_source = (
        "import glob\n\n"
        "def test_it():\n"
        "    assert glob.glob('package/*.py')\n"
    )
    assert _select(tmp_path, test_source) == ["tests/test_x.py"]


def test_exec_only_literal_still_selected_for_code_edit(tmp_path):
    test_source = (
        "import subprocess\n\n"
        "def test_it():\n"
        "    subprocess.run(['python', 'package/mod.py'])\n"
    )
    assert _select(tmp_path, test_source, classification="code") == ["tests/test_x.py"]

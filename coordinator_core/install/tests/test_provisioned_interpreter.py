"""resolve_provisioned_interpreter: pin read via example-retrieval-repo's reader, never PATH."""
from __future__ import annotations

import sys

from coordinator_core.install.provisioned_interpreter import resolve_provisioned_interpreter


def _fake_rag(tmp_path, body):
    reader = tmp_path / "project_rag_scripts" / "lib" / "read_claude_pin.py"
    reader.parent.mkdir(parents=True)
    reader.write_text(body)
    return str(tmp_path)


def test_resolves_pin_printed_by_reader(tmp_path):
    root = _fake_rag(tmp_path, f"print({sys.executable!r})\n")
    got = resolve_provisioned_interpreter(root)
    assert got.path == sys.executable


def test_reader_gets_consumer_root_and_one_line(tmp_path):
    from coordinator_core.install import provisioned_interpreter as pi

    root = _fake_rag(tmp_path, f"import sys; assert sys.argv[1] == {str(pi._CLAUDE_KLABAUTER_ROOT)!r}; print({sys.executable!r})\n")
    assert resolve_provisioned_interpreter(root).path == sys.executable
    two = _fake_rag(tmp_path / "two", f"print({sys.executable!r}); print({sys.executable!r})\n")
    assert resolve_provisioned_interpreter(two).path is None


def test_reader_failure_yields_no_path(tmp_path):
    root = _fake_rag(tmp_path, "import sys; sys.exit(3)\n")
    got = resolve_provisioned_interpreter(root)
    assert got.path is None and "exit 3" in got.reason


def test_missing_binary_yields_no_path(tmp_path):
    root = _fake_rag(tmp_path, "print('/nonexistent/python')\n")
    assert resolve_provisioned_interpreter(root).path is None


def test_missing_root_yields_no_path(tmp_path):
    assert resolve_provisioned_interpreter(str(tmp_path / "nope")).path is None

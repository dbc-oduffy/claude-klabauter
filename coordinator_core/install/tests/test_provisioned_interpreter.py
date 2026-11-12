"""resolve_provisioned_interpreter: pin read via example-retrieval-repo's reader module in-process, never PATH."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.install.provisioned_interpreter import resolve_provisioned_interpreter

pytestmark = [pytest.mark.cadence]


def _fake_rag(tmp_path, body):
    reader = tmp_path / "project_rag_scripts" / "lib" / "read_claude_pin.py"
    reader.parent.mkdir(parents=True)
    reader.write_text(body)
    return str(tmp_path)


def test_resolves_pin_returned_by_reader(tmp_path):
    root = _fake_rag(tmp_path, f"def read_pin(root):\n    return {sys.executable!r}\n")
    got = resolve_provisioned_interpreter(root)
    assert got.path == sys.executable


def test_reader_gets_consumer_root(tmp_path):
    from coordinator_core.install import provisioned_interpreter as pi

    root = _fake_rag(tmp_path, f"def read_pin(root):\n    assert root == {str(pi._CLAUDE_KLABAUTER_ROOT)!r}\n    return {sys.executable!r}\n")
    assert resolve_provisioned_interpreter(root).path == sys.executable


def test_pin_read_spawns_no_process(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("pin read spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    root = _fake_rag(tmp_path, f"def read_pin(root):\n    return {sys.executable!r}\n")
    assert resolve_provisioned_interpreter(root).path == sys.executable


def test_reader_failure_yields_no_path(tmp_path):
    root = _fake_rag(tmp_path, "def read_pin(root):\n    raise SystemExit(3)\n")
    got = resolve_provisioned_interpreter(root)
    assert got.path is None and "exit 3" in got.reason


def test_no_pin_exit_names_the_installer_and_stale_pin_does_not(tmp_path):
    root = _fake_rag(tmp_path / "a", "def read_pin(root):\n    raise SystemExit(3)\n")
    never = resolve_provisioned_interpreter(root)
    assert "install_example_retrieval_repo_plugin.py" in never.reason
    assert "install_example_retrieval_repo_plugin.py" in never.install_command
    stale = _fake_rag(tmp_path / "b", "def read_pin(root):\n    raise SystemExit(2)\n")
    assert "install_example_retrieval_repo_plugin.py" not in resolve_provisioned_interpreter(stale).reason


def test_never_installed_flag_only_on_exit_3(tmp_path):
    never = _fake_rag(tmp_path / "a", "def read_pin(root):\n    raise SystemExit(3)\n")
    assert resolve_provisioned_interpreter(never).never_installed is True
    stale = _fake_rag(tmp_path / "b", "def read_pin(root):\n    raise SystemExit(2)\n")
    assert resolve_provisioned_interpreter(stale).never_installed is False


def test_missing_binary_yields_no_path(tmp_path):
    root = _fake_rag(tmp_path, "def read_pin(root):\n    return '/nonexistent/python'\n")
    assert resolve_provisioned_interpreter(root).path is None


def test_missing_root_yields_no_path(tmp_path):
    assert resolve_provisioned_interpreter(str(tmp_path / "nope")).path is None


def test_set_but_missing_root_advisory_names_the_set_value_not_unset(tmp_path):
    gone = str(tmp_path / "nope")
    got = resolve_provisioned_interpreter(gone)
    assert got.path is None
    assert "unset" not in got.reason and gone in got.reason


def test_unset_key_advisory_says_unset(monkeypatch):
    import coordinator_core.machine_resolver as resolver

    monkeypatch.delenv("MACHINE_LOCAL_REPOS_EXAMPLE_RETRIEVAL_REPO", raising=False)
    monkeypatch.setattr(resolver, "registry_get", lambda key: None)
    got = resolve_provisioned_interpreter()
    assert got.path is None
    assert "unset" in got.reason


def test_set_key_is_read_in_process_without_cli(monkeypatch, tmp_path):
    gone = str(tmp_path / "nope")
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_EXAMPLE_RETRIEVAL_REPO", gone)
    monkeypatch.setenv("PATH", "")
    got = resolve_provisioned_interpreter()
    assert "unset" not in got.reason and gone in got.reason


def test_pin_read_leaves_sys_path_and_modules_untouched(tmp_path):
    root = _fake_rag(
        tmp_path,
        "import sys\nsys.path.insert(0, 'rag-sentinel-path')\nimport example_retrieval_repo_probe_mod\n"
        f"def read_pin(root):\n    return {sys.executable!r}\n",
    )
    (tmp_path / "example_retrieval_repo_probe_mod.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    before = list(sys.path)
    try:
        assert resolve_provisioned_interpreter(root).path == sys.executable
        assert sys.path == before and "example_retrieval_repo_probe_mod" not in sys.modules
    finally:
        sys.path.remove(str(tmp_path))

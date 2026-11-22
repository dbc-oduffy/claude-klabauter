"""A scoped clean mirror holds the root file a conftest loads by path, so pytest collects in it.

Witness ask-20261121T215044-2241ea: a root conftest loaded drive_root_write_guard.py by file path, the mirror
lacked it, pytest exited 3 (INTERNALERROR FileNotFoundError) and landing refused verify-overlay-unverified."""
from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TEST = "tests/test_mod.py"
CONFTEST = (
    "import importlib.util\n"
    "from pathlib import Path\n\n\n"
    "def pytest_configure(config):\n"
    "    path = Path(__file__).parent / '{name}'\n"
    "    spec = importlib.util.spec_from_file_location('{stem}', path)\n"
    "    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
)


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _repo(tmp_path, sibling):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "conftest.py", CONFTEST.format(name=sibling, stem=sibling[:-3]))
    _write(root, sibling, "LOADED = True\n")
    _write(root, "unrelated_root.py", "X = 1\n")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", "VALUE = 1\n")
    _write(root, TEST, "from pkg import mod\n\n\ndef test_mod():\n    assert mod.VALUE == 1\n")
    _commit(root, "init")
    return root


def _pytest_in(dest):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", TEST],
        cwd=dest, capture_output=True, text=True, **no_console_creationflags(),
    )


def test_a_scoped_mirror_holds_the_root_file_a_conftest_loads_by_path_and_pytest_collects(tmp_path):
    root = _repo(tmp_path, "drive_root_write_guard.py")

    with cov.clean_export(root, "run1", [], argv=[TEST]) as dest:
        assert (dest / "drive_root_write_guard.py").is_file()
        assert not (dest / "unrelated_root.py").exists()
        ran = _pytest_in(dest)

    assert ran.returncode == 0, ran.stdout + ran.stderr


def test_a_warm_scoped_mirror_gains_a_root_file_a_later_conftest_starts_loading(tmp_path):
    root = _repo(tmp_path, "first_guard.py")
    with cov.clean_export(root, "run1", [], argv=[TEST]):
        pass
    _write(root, "second_guard.py", "LOADED = True\n")
    _write(root, "conftest.py", CONFTEST.format(name="second_guard.py", stem="second_guard"))
    _commit(root, "conftest loads another root file")

    with cov.clean_export(root, "run2", [], argv=[TEST]) as dest:
        assert (dest / "second_guard.py").is_file()
        ran = _pytest_in(dest)

    assert ran.returncode == 0, ran.stdout + ran.stderr

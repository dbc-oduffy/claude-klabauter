"""The repo-root conftest fails a pytest session that moved HEAD or the index of its rootdir checkout.

Each case runs a real child pytest session inside a throwaway git repo whose conftest
re-exports the root conftest's session hooks, so the guard is driven through its
production hooks rather than a hand-built stand-in.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"

_CONFTEST = '''
import importlib.util
spec = importlib.util.spec_from_file_location("root_conftest_under_test", {root!r})
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
pytest_sessionstart = mod.pytest_sessionstart
pytest_sessionfinish = mod.pytest_sessionfinish
_attribute_checkout_move = mod._attribute_checkout_move
'''

_COMMITTING_TEST = '''
import subprocess

def test_moves_head():
    subprocess.run(["git", "commit", "--allow-empty", "-m", "leak"], check=True)
'''

_STAGING_TEST = '''
import subprocess

def test_moves_index():
    open("leak.txt", "w").write("x")
    subprocess.run(["git", "add", "leak.txt"], check=True)
'''

_CLEAN_TEST = '''
def test_noop():
    assert True
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _session(tmp_path: Path, test_body: str, *, init_git: bool = True):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "conftest.py").write_text(_CONFTEST.format(root=str(ROOT_CONFTEST)), encoding="utf-8")
    (repo / "test_case.py").write_text(test_body, encoding="utf-8")
    if init_git:
        _git(repo, "init", "-q")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(repo), str(repo)],
        cwd=repo, capture_output=True, text=True, timeout=300,
    )


def test_clean_session_passes(tmp_path):
    proc = _session(tmp_path, _CLEAN_TEST)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[checkout-guard]" not in proc.stderr


def test_session_that_commits_fails_naming_head(tmp_path):
    proc = _session(tmp_path, _COMMITTING_TEST)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "[checkout-guard]" in proc.stderr and "HEAD " in proc.stderr


def test_session_that_stages_fails_naming_index(tmp_path):
    proc = _session(tmp_path, _STAGING_TEST)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "[checkout-guard]" in proc.stderr and "index " in proc.stderr
    assert "HEAD " not in proc.stderr.split("[checkout-guard]", 1)[1]


_TWO_TESTS = '''
import subprocess

def test_innocent():
    assert True

def test_the_leaker():
    subprocess.run(["git", "commit", "--allow-empty", "-m", "leak"], check=True)

def test_also_innocent():
    assert True
'''


def test_the_moving_test_is_named_and_innocent_ones_are_not(tmp_path):
    proc = _session(tmp_path, _TWO_TESTS)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "test_case.py::test_the_leaker" in out.split("suspect", 1)[0].rsplit("[checkout-guard]", 1)[-1]
    assert "test_case.py::test_innocent " not in out and "test_case.py::test_also_innocent " not in out


def test_clean_session_names_no_suspect(tmp_path):
    proc = _session(tmp_path, _TWO_TESTS.replace('"commit", "--allow-empty", "-m", "leak"', '"status"'))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "suspect" not in proc.stdout + proc.stderr


def test_non_git_rootdir_skips_cleanly(tmp_path):
    proc = _session(tmp_path, _CLEAN_TEST, init_git=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[checkout-guard] skipped" in proc.stderr
    assert "moved the checkout" not in proc.stderr


def test_opt_out_keeps_the_move_advisory_only(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ALLOW_CHECKOUT_MOVE", "1")
    proc = _session(tmp_path, _COMMITTING_TEST)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "advisory only" in proc.stderr

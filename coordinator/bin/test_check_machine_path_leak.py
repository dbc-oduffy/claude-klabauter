"""test_check_machine_path_leak.py — pytest suite for check-machine-path-leak.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into collectable top-level test_* functions; assertion intent
preserved 1:1.

Review: code-reviewer — F5 (P2): check-machine-path-leak.py landed with ~340 lines of
non-trivial JSON/YAML structural tree-walk logic backing a HARD commit-block gate and
zero automated test coverage. This suite closes that gap: hard-block on a machine-path
leaf in settings.json, no-op on a clean file, --staged vs explicit-file-arg modes, the
PyYAML-absent linescan fallback path for working-repos.yaml, and the F4 $HOME-vs-
expanduser fallback (current-machine soft-warn firing when $HOME is unset, matching
Windows USERPROFILE-only environments).

Spec backlink: docs/plans/2026-06-23-machine-path-leak-guard.md
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(SCRIPT_DIR, "check-machine-path-leak.py")
PYTHON = sys.executable
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def run_helper(args, cwd=None, extra_env=None):
    env = dict(os.environ)
    if extra_env is not None:
        env = extra_env
    r = subprocess.run(
        [PYTHON, HELPER, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        creationflags=_NO_WINDOW,
    )
    return r.returncode, r.stdout, r.stderr


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    return str(tmp_path_factory.mktemp("check-machine-path-leak-test"))


@pytest.fixture(scope="module")
def fake_home(root):
    d = os.path.join(root, "fakehome")
    os.makedirs(d, exist_ok=True)
    return d


def test_settings_json_hard_block(root):
    bad_settings = os.path.join(root, "settings.json")
    write(bad_settings, json.dumps({
        "mcpServers": {
            "foo": {"command": "/Users/dev1/bin/foo"}
        }
    }))
    code, so, se = run_helper([bad_settings])
    assert code == 1, "hard violation must exit 1"
    assert "VIOLATION" in se
    assert "BLOCKED" in se


def test_settings_json_clean_file(root):
    clean_dir = os.path.join(root, "clean")
    os.makedirs(clean_dir, exist_ok=True)
    clean_settings = os.path.join(clean_dir, "settings.json")
    write(clean_settings, json.dumps({
        "mcpServers": {
            "foo": {"command": "npx"}
        }
    }))
    code, so, se = run_helper([clean_settings])
    assert code == 0, "no violation must exit 0"
    assert "OK" in so


def test_working_repos_yaml_soft_warn(root, fake_home):
    # Basename MUST be exactly "working-repos.yaml" — collect() classifies by
    # canonical basename, so each variant needs its own subdirectory.
    wr_dir_c = os.path.join(root, "wr-c")
    os.makedirs(wr_dir_c, exist_ok=True)
    wr_file = os.path.join(wr_dir_c, "working-repos.yaml")
    write(wr_file, f"repos:\n  - {fake_home}/my-project\n  - /Users/othermachine/their-project\n")
    env_c = dict(os.environ)
    env_c["HOME"] = fake_home
    code, so, se = run_helper([wr_file], extra_env=env_c)
    assert code == 0, "soft warn must not fail the commit"
    assert "WARN" in se
    assert fake_home in se
    assert "othermachine" not in se


def test_home_unset_falls_back_to_expanduser(root):
    # F4 — $HOME unset falls back to os.path.expanduser("~")
    wr_dir_d = os.path.join(root, "wr-d")
    os.makedirs(wr_dir_d, exist_ok=True)
    real_home = os.path.expanduser("~")
    wr_file_d = os.path.join(wr_dir_d, "working-repos.yaml")
    write(wr_file_d, f"repos:\n  - {real_home}/some-repo\n")
    env_d = dict(os.environ)
    env_d.pop("HOME", None)
    code, so, se = run_helper([wr_file_d], extra_env=env_d)
    assert code == 0, "HOME-unset run still exits 0 (soft warn)"
    assert "WARN" in se, "HOME-unset must still WARN on the real home-rooted path (expanduser fallback fired)"


def test_pyyaml_absent_linescan_fallback(root, fake_home):
    wr_dir_e = os.path.join(root, "wr-e")
    os.makedirs(wr_dir_e, exist_ok=True)
    wr_file_e = os.path.join(wr_dir_e, "working-repos.yaml")
    write(wr_file_e, f"repos:\n  - {fake_home}/linescan-project\n")
    probe_path = os.path.join(root, "yaml_blocked_probe.py")
    write(probe_path, (
        "import builtins, runpy, sys\n"
        "_real_import = builtins.__import__\n"
        "def _blocked_import(name, *a, **kw):\n"
        "    if name == 'yaml':\n"
        "        raise ImportError('yaml blocked for test')\n"
        "    return _real_import(name, *a, **kw)\n"
        "builtins.__import__ = _blocked_import\n"
        f"sys.argv = [{HELPER!r}, {wr_file_e!r}]\n"
        f"runpy.run_path({HELPER!r}, run_name='__main__')\n"
    ))
    env_e = dict(os.environ)
    env_e["HOME"] = fake_home
    r = subprocess.run(
        [PYTHON, probe_path],
        capture_output=True,
        text=True,
        env=env_e,
        creationflags=_NO_WINDOW,
    )
    assert r.returncode == 0, "PyYAML-absent run still exits 0 (soft warn)"
    assert "PyYAML absent" in r.stderr
    assert "WARN" in r.stderr, "linescan must still find the current-machine path"


def test_staged_outside_git_repo(root):
    nogit_dir = os.path.join(root, "nogit")
    os.makedirs(nogit_dir, exist_ok=True)
    code, so, se = run_helper(["--staged"], cwd=nogit_dir)
    assert code == 2, "not a git repo must exit 2"
    assert "ERROR" in se


def test_nothing_in_scope(root):
    unrelated = os.path.join(root, "unrelated.txt")
    write(unrelated, "nothing to see here\n")
    code, so, se = run_helper([unrelated])
    assert code == 0, "nothing in scope must exit 0"
    assert "nothing to check" in so

"""test_sh_suffix_polyglot.py — self-contained test suite for
check-sh-suffix-polyglot.py (SH-SUFFIX-POLYGLOT-RATCHET).

Tests run inside a throwaway `git init`-ed scratch repo (never against the
real repo, so the suite is independent of the live baseline's exact entry
count and can't be broken by future renames or the domain widening). A real
git repo is required because the guard enumerates via `git ls-files '*.sh'`
(widened 2026-07-20 to scan repo-wide, not just coordinator/bin/ non-recursive)
— every fixture file must be `git add`-ed (staged is enough; `ls-files` sees
the index) for the guard to see it.

  A — a brand-new `.sh` file carrying the polyglot trampoline, NOT in the
      baseline → VIOLATION, exit 1.
  B — a baselined `.sh` polyglot trampoline file → OK, exit 0 (ratchet
      does not re-flag known backlog).
  C — a genuine bash `.sh` file (no trampoline) → OK, exit 0 (not our concern).
  D — missing baseline file → ERROR, exit 2 (fail loud, never silently treat
      every offender as new).
  E — --list-baseline-candidates emits exactly the trampoline-bearing files
      (repo-relative paths).
  F — clean repo (no .sh files at all) → OK, exit 0.
  G — SAME-BASENAME HAZARD (2026-07-20 widening): two files sharing a
      basename in different directories — one baselined by its full
      repo-relative path, one NOT — the un-baselined one must still be a
      VIOLATION. This is the regression class a basename-keyed baseline
      would silently miss once the scan domain is repo-wide.

Converted from a hand-rolled PASS/FAIL harness to top-level pytest functions,
one per fixture scenario, each building its own throwaway git repo.

Spec backlink: state/audits/2026-07-20-sh-suffixed-python-trampolines.md
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
GUARD = os.path.join(_BIN_DIR, "check-sh-suffix-polyglot.py")
PYTHON = sys.executable
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TRAMPOLINE_BODY = (
    "#!/bin/sh\n"
    "''''exec \"$(command -v python3 || command -v python || command -v py)\" "
    "\"$0\" \"$@\" #'''\n"
    "import sys\n"
    "print('hello from python body')\n"
)

PLAIN_BASH_BODY = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "echo 'genuine bash, no trampoline here'\n"
)


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
        creationflags=_NO_WINDOW, check=True,
    )


def _fresh_repo(root, name):
    """Build a throwaway git repo containing a copy of the real guard at
    coordinator/bin/check-sh-suffix-polyglot.py (mirroring the real layout,
    since the guard resolves BASELINE_FILE relative to its own location and
    the repo root via `git rev-parse --show-toplevel` from there).
    """
    d = os.path.join(root, name)
    guard_dir = os.path.join(d, "coordinator", "bin")
    os.makedirs(guard_dir, exist_ok=True)
    shutil.copy(GUARD, os.path.join(guard_dir, "check-sh-suffix-polyglot.py"))
    _git(["init", "-q"], cwd=d)
    _git(["config", "user.email", "test@example.invalid"], cwd=d)
    _git(["config", "user.name", "test"], cwd=d)
    return d, guard_dir


def _commit_all(repo_dir):
    _git(["add", "-A"], cwd=repo_dir)
    _git(["commit", "-q", "-m", "fixture"], cwd=repo_dir)


def _run_guard(guard_dir, args=None):
    guard = os.path.join(guard_dir, "check-sh-suffix-polyglot.py")
    r = subprocess.run(
        [PYTHON, guard] + (args or []),
        cwd=guard_dir, capture_output=True, text=True, creationflags=_NO_WINDOW,
    )
    return r


def test_new_unbaselined_polyglot_sh_file_is_violation(tmp_path):
    root = str(tmp_path)
    d_a, gd_a = _fresh_repo(root, "a")
    write(os.path.join(gd_a, "sh-suffix-polyglot-baseline.txt"), "# empty baseline\n")
    write(os.path.join(d_a, "coordinator", "lib", "new-tool.sh"), TRAMPOLINE_BODY)
    _commit_all(d_a)
    r = _run_guard(gd_a)
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}"
    assert "VIOLATION" in r.stderr
    assert "coordinator/lib/new-tool.sh" in r.stderr


def test_baselined_polyglot_sh_file_is_ok(tmp_path):
    root = str(tmp_path)
    d_b, gd_b = _fresh_repo(root, "b")
    write(
        os.path.join(gd_b, "sh-suffix-polyglot-baseline.txt"),
        "coordinator/lib/known-tool.sh\n",
    )
    write(os.path.join(d_b, "coordinator", "lib", "known-tool.sh"), TRAMPOLINE_BODY)
    _commit_all(d_b)
    r = _run_guard(gd_b)
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}"
    assert "OK" in r.stdout


def test_genuine_bash_sh_file_is_not_flagged(tmp_path):
    root = str(tmp_path)
    d_c, gd_c = _fresh_repo(root, "c")
    write(os.path.join(gd_c, "sh-suffix-polyglot-baseline.txt"), "# empty baseline\n")
    write(os.path.join(d_c, "coordinator", "tests", "real-bash-tool.sh"), PLAIN_BASH_BODY)
    _commit_all(d_c)
    r = _run_guard(gd_c)
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}"


def test_missing_baseline_file_fails_loud(tmp_path):
    root = str(tmp_path)
    d_d, gd_d = _fresh_repo(root, "d")
    write(os.path.join(d_d, "coordinator", "lib", "some-tool.sh"), TRAMPOLINE_BODY)
    _commit_all(d_d)  # no baseline file ever written in this repo
    r = _run_guard(gd_d)
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}"
    assert "ERROR" in r.stderr


def test_list_baseline_candidates_emits_repo_relative_trampoline_paths(tmp_path):
    root = str(tmp_path)
    d_e, gd_e = _fresh_repo(root, "e")
    write(os.path.join(gd_e, "sh-suffix-polyglot-baseline.txt"), "# empty baseline\n")
    write(os.path.join(d_e, "coordinator", "lib", "poly-1.sh"), TRAMPOLINE_BODY)
    write(os.path.join(d_e, "coordinator", "tests", "poly-2.sh"), TRAMPOLINE_BODY)
    write(os.path.join(d_e, "coordinator", "bin", "real-bash.sh"), PLAIN_BASH_BODY)
    _commit_all(d_e)
    r = _run_guard(gd_e, ["--list-baseline-candidates"])
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}"
    listed = set(r.stdout.split())
    expected = {"coordinator/lib/poly-1.sh", "coordinator/tests/poly-2.sh"}
    assert listed == expected, f"expected {expected} got {listed}"


def test_clean_repo_with_no_sh_files_is_ok(tmp_path):
    root = str(tmp_path)
    d_f, gd_f = _fresh_repo(root, "f")
    write(os.path.join(gd_f, "sh-suffix-polyglot-baseline.txt"), "# empty baseline\n")
    _commit_all(d_f)
    r = _run_guard(gd_f)
    assert r.returncode == 0, f"expected exit 0, got {r.returncode}"
    assert "OK" in r.stdout


def test_same_basename_different_dir_hazard(tmp_path):
    # Two files named identically ("dup-tool.sh") in different directories.
    # coordinator/lib/dup-tool.sh IS baselined (by full repo-relative path);
    # coordinator/tests/dup-tool.sh is NOT. A basename-keyed baseline would
    # wrongly treat the tests/ one as baselined too (basename match) — the
    # repo-relative-path keying must keep them distinct.
    root = str(tmp_path)
    d_g, gd_g = _fresh_repo(root, "g")
    write(
        os.path.join(gd_g, "sh-suffix-polyglot-baseline.txt"),
        "coordinator/lib/dup-tool.sh\n",
    )
    write(os.path.join(d_g, "coordinator", "lib", "dup-tool.sh"), TRAMPOLINE_BODY)
    write(os.path.join(d_g, "coordinator", "tests", "dup-tool.sh"), TRAMPOLINE_BODY)
    _commit_all(d_g)
    r = _run_guard(gd_g)
    assert r.returncode == 1, "un-baselined same-basename sibling must still VIOLATE"
    assert "coordinator/tests/dup-tool.sh" in r.stderr
    assert "coordinator/lib/dup-tool.sh" not in r.stderr, (
        f"baselined lib/ copy was wrongly flagged: {r.stderr[:400]}"
    )

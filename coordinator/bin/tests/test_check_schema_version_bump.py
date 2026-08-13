# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/tests/test_check_schema_version_bump.py

Purpose: Unit tests for check-schema-version-bump.py (the bump-tripwire gate
wired into validate-commit as Check 9).

This test exercises check-schema-version-bump.py directly against scratch
git repos.

Test coverage:
  T1  violation: canonical-structure.yaml changed, coordinator-schema-version
      NOT bumped, --commit mode -> exit 1
  T2  clean: both files bumped together, --commit mode -> exit 0
  T3  no-op: canonical-structure.yaml untouched, --commit mode -> exit 0
  T4  NESTED-LAYOUT + --staged violation (regression guard)
  T5  NESTED-LAYOUT + --staged proper bump -> exit 0
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

from coordinator_core.win_portability import no_console_creationflags

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BUMP_TRIPWIRE = os.path.join(_SCRIPT_DIR, "..", "check-schema-version-bump.py")

# check-schema-version-bump.py's tripwire logic is content-agnostic (it only
# diffs whether canonical-structure.yaml/coordinator-schema-version changed
# between two git commits, never their contents) -- these tests only need
# SOME realistic seed bytes, not the real production files. Both files are
# example-doctrine-repo-owned and NOT vendored into claude-klabauter (D5 decision:
# coordinator_core/install/scaffold_structure.py module docstring, "Does NOT
# vendor a copy of canonical-structure.yaml"), so a hardcoded
# `../../canonical-structure.yaml` path here previously resolved to a file
# that never exists in this repo's tree -- a stale monorepo-era assumption,
# not an env-dependent gap (no sibling-repo resolution would fix it; the
# file is deliberately absent from every claude-klabauter checkout, not merely this
# machine's). Synthetic content sidesteps the dependency entirely.
_SYNTHETIC_CANONICAL_STRUCTURE = "# synthetic canonical-structure.yaml fixture\nkey: value\n"
_SYNTHETIC_SCHEMA_VERSION = "1\n"


def _seed_canonical_structure(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_SYNTHETIC_CANONICAL_STRUCTURE)


def _seed_schema_version(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_SYNTHETIC_SCHEMA_VERSION)


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", ".")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")


def _run_tripwire(cwd_env_root: str, *args: str) -> int:
    env = dict(os.environ)
    env["COORDINATOR_PLUGIN_ROOT"] = cwd_env_root
    proc = subprocess.run(
        [sys.executable, _BUMP_TRIPWIRE, *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    return proc.returncode


class CheckSchemaVersionBumpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_exists(self):
        self.assertTrue(os.path.isfile(_BUMP_TRIPWIRE))

    def test_t1_violation_canonical_changed_no_bump(self):
        repo = os.path.join(self.tmp.name, "tripwire_repo")
        _init_repo(repo)

        _seed_canonical_structure(os.path.join(repo, "canonical-structure.yaml"))
        _seed_schema_version(os.path.join(repo, "coordinator-schema-version"))
        _git(repo, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(repo, "commit", "-q", "-m", "initial")

        with open(os.path.join(repo, "canonical-structure.yaml"), "a", encoding="utf-8") as fh:
            fh.write("# test modification — no version bump\n")
        _git(repo, "add", "canonical-structure.yaml")
        _git(repo, "commit", "-q", "-m", "modify canonical-structure without bump")

        rc = _run_tripwire(repo, "--commit=HEAD")
        self.assertEqual(rc, 1)

    def test_t2_clean_both_bumped(self):
        repo = os.path.join(self.tmp.name, "tripwire_repo2")
        _init_repo(repo)

        _seed_canonical_structure(os.path.join(repo, "canonical-structure.yaml"))
        with open(os.path.join(repo, "coordinator-schema-version"), "w", encoding="utf-8") as fh:
            fh.write("1\n")
        _git(repo, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(repo, "commit", "-q", "-m", "initial")

        with open(os.path.join(repo, "canonical-structure.yaml"), "a", encoding="utf-8") as fh:
            fh.write("# modification with version bump\n")
        with open(os.path.join(repo, "coordinator-schema-version"), "w", encoding="utf-8") as fh:
            fh.write("2\n")
        _git(repo, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(repo, "commit", "-q", "-m", "bump schema version with canonical-structure change")

        rc = _run_tripwire(repo, "--commit=HEAD")
        self.assertEqual(rc, 0)

    def test_t3_noop_canonical_not_changed(self):
        repo = os.path.join(self.tmp.name, "tripwire_repo3")
        _init_repo(repo)

        _seed_canonical_structure(os.path.join(repo, "canonical-structure.yaml"))
        _seed_schema_version(os.path.join(repo, "coordinator-schema-version"))
        _git(repo, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(repo, "commit", "-q", "-m", "initial")

        with open(os.path.join(repo, "unrelated.txt"), "w", encoding="utf-8") as fh:
            fh.write("# unrelated change\n")
        _git(repo, "add", "unrelated.txt")
        _git(repo, "commit", "-q", "-m", "unrelated change, no schema files touched")

        rc = _run_tripwire(repo, "--commit=HEAD")
        self.assertEqual(rc, 0)

    def test_t4_nested_layout_staged_violation(self):
        # T4/T5: NESTED-LAYOUT + --staged — regression guard for the manual
        # ${ABS#$GIT_ROOT/} path-strip bug (Windows/Git-Bash path-format
        # divergence between --show-toplevel and pwd).
        repo = os.path.join(self.tmp.name, "tripwire_repo4")
        nested = os.path.join(repo, "plugins", "coordinator-claude", "coordinator")
        os.makedirs(nested, exist_ok=True)
        _init_repo(repo)

        _seed_canonical_structure(os.path.join(nested, "canonical-structure.yaml"))
        _seed_schema_version(os.path.join(nested, "coordinator-schema-version"))
        _git(
            repo,
            "add",
            "--",
            "plugins/coordinator-claude/coordinator/canonical-structure.yaml",
            "plugins/coordinator-claude/coordinator/coordinator-schema-version",
        )
        _git(repo, "commit", "-q", "-m", "initial nested")

        with open(os.path.join(nested, "canonical-structure.yaml"), "a", encoding="utf-8") as fh:
            fh.write("# nested test modification — no version bump\n")
        _git(
            repo,
            "add",
            "--",
            "plugins/coordinator-claude/coordinator/canonical-structure.yaml",
        )

        rc = _run_tripwire(nested, "--staged")
        self.assertEqual(rc, 1)

    def test_t5_nested_layout_staged_proper_bump(self):
        repo = os.path.join(self.tmp.name, "tripwire_repo5")
        nested = os.path.join(repo, "plugins", "coordinator-claude", "coordinator")
        os.makedirs(nested, exist_ok=True)
        _init_repo(repo)

        _seed_canonical_structure(os.path.join(nested, "canonical-structure.yaml"))
        with open(os.path.join(nested, "coordinator-schema-version"), "w", encoding="utf-8") as fh:
            fh.write("1\n")
        _git(
            repo,
            "add",
            "--",
            "plugins/coordinator-claude/coordinator/canonical-structure.yaml",
            "plugins/coordinator-claude/coordinator/coordinator-schema-version",
        )
        _git(repo, "commit", "-q", "-m", "initial nested")

        with open(os.path.join(nested, "canonical-structure.yaml"), "a", encoding="utf-8") as fh:
            fh.write("# nested modification with bump\n")
        with open(os.path.join(nested, "coordinator-schema-version"), "w", encoding="utf-8") as fh:
            fh.write("2\n")
        _git(
            repo,
            "add",
            "--",
            "plugins/coordinator-claude/coordinator/canonical-structure.yaml",
            "plugins/coordinator-claude/coordinator/coordinator-schema-version",
        )

        rc = _run_tripwire(nested, "--staged")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

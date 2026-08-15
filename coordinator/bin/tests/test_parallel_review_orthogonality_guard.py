"""bin/tests/test_parallel_review_orthogonality_guard.py

Purpose: Unit tests for parallel-review-orthogonality-guard.py, the fail-fast
lens-orthogonality guard + weekly-slice diff-freeze CLI ported from the bash
fences in coordinator/skills/parallel-code-review/SKILL.md (DoE-claude) §
Pre-Flight Orthogonality Assertion and § Snapshot.

Test coverage:
  T1  guard (static, no --chunk-manifest) against the real repo -> exit 0,
      "OK (static)" on stdout (the real lens-domain manifest has no collision)
  T2  guard --chunk-manifest <missing-file> -> exit 1, chunk-mode refusal
      message ("Chunk partitions are not disjoint...") on stderr, distinct
      from the static-mode refusal
  T3  guard --chunk-manifest <disjoint TSV fixture> -> exit 0
  T4  guard --chunk-manifest <overlapping TSV fixture> -> exit 1, chunk-mode
      refusal message
  T5  snapshot writes findings_dir + prints a JSON object whose
      head_sha_path is the diff_path's ".diff" suffix substituted with
      ".head.sha" (the load-bearing derivation this CLI carries over from
      the skill's `${DIFF_PATH%.diff}.head.sha` shell expansion)
  T6  snapshot --ts is honored verbatim (deterministic slice id), letting
      the test assert exact paths rather than a UTC-now pattern match
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

from coordinator_core.win_portability import no_console_creationflags

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_SCRIPT_DIR)
_CLI = os.path.join(_BIN_DIR, "parallel-review-orthogonality-guard.py")


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _init_repo_with_commit(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q", ".")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    with open(os.path.join(path, "seed.txt"), "w", encoding="utf-8") as f:
        f.write("seed\n")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-q", "-m", "seed")


def _run_cli(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, _CLI, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


class TestGuardStatic(unittest.TestCase):
    def test_static_ok_against_real_repo(self):
        # T1 — the real lens-domain manifest in THIS repo's skill file has no
        # collision and all four agent files exist; the guard runs from
        # coordinator/bin/ regardless of cwd (Path(__file__)-relative).
        proc = _run_cli(["guard"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK (static)", proc.stdout)


class TestGuardChunkManifest(unittest.TestCase):
    def test_missing_manifest_is_chunk_mode_refusal(self):
        # T2 — a manifest path that doesn't exist still fails via the
        # underlying verify CLI, but the guard's own refusal line must be
        # the chunk-mode message, not the static one.
        proc = _run_cli(["guard", "--chunk-manifest", "/nonexistent-manifest.tsv"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "Chunk partitions are not disjoint by file-scope; refusing to dispatch.",
            proc.stderr,
        )
        self.assertNotIn("Lens-orthogonality assertion failed", proc.stderr)

    def test_disjoint_manifest_passes(self):
        # T3
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "chunk-manifest.tsv")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("chunk-1\tsrc/a.py\n")
                f.write("chunk-2\tsrc/b.py\n")
            proc = _run_cli(["guard", "--chunk-manifest", manifest])
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_overlapping_manifest_fails(self):
        # T4 — same file in two chunks must trip the refusal.
        with tempfile.TemporaryDirectory() as tmp:
            manifest = os.path.join(tmp, "chunk-manifest.tsv")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("chunk-1\tsrc/a.py\n")
                f.write("chunk-2\tsrc/a.py\n")
            proc = _run_cli(["guard", "--chunk-manifest", manifest])
            self.assertEqual(proc.returncode, 1)
            self.assertIn(
                "Chunk partitions are not disjoint by file-scope; refusing to dispatch.",
                proc.stderr,
            )


class TestSnapshot(unittest.TestCase):
    def test_snapshot_derives_head_sha_path_and_writes_findings_dir(self):
        # T5 + T6
        with tempfile.TemporaryDirectory() as tmp:
            _init_repo_with_commit(tmp)
            with open(os.path.join(tmp, "seed.txt"), "a", encoding="utf-8") as f:
                f.write("more\n")
            _git(tmp, "add", "seed.txt")
            _git(tmp, "commit", "-q", "-m", "second")

            proc = _run_cli(
                [
                    "snapshot",
                    "--range",
                    "HEAD~1...HEAD",
                    "--slice-prefix",
                    "weekly",
                    "--ts",
                    "20260101T000000Z",
                    "--repo-root",
                    tmp,
                ]
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            payload = json.loads(proc.stdout.strip())
            expected_findings_dir = os.path.join(
                tmp, "state", "review-findings", "20260101T000000Z"
            )
            self.assertEqual(payload["findings_dir"], expected_findings_dir)
            self.assertTrue(os.path.isdir(expected_findings_dir))

            self.assertEqual(payload["weekly_slice_id"], "weekly-20260101T000000Z")

            self.assertTrue(payload["diff_path"].endswith(".diff"))
            self.assertTrue(os.path.isfile(payload["diff_path"]))

            expected_sha_path = payload["diff_path"][: -len(".diff")] + ".head.sha"
            self.assertEqual(payload["head_sha_path"], expected_sha_path)
            self.assertTrue(os.path.isfile(payload["head_sha_path"]))


if __name__ == "__main__":
    unittest.main()

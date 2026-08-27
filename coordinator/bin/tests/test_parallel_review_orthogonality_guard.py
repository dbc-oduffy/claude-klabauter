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

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator_core.ops.verify_parallel_review_lens_orthogonality import static_check
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


def _run_cli(
    args: list[str], cwd: str | None = None, doe_root: str | None = None
) -> subprocess.CompletedProcess:
    env = None
    if doe_root is not None:
        env = dict(os.environ)
        env["REPO_DOE_CLAUDE"] = doe_root
    return subprocess.run(
        [sys.executable, _CLI, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


_GOOD_SKILL_MD = """\
# parallel-code-review

## Lens-Domain Manifest

| Reviewer | Lens domain | Rationale |
|---|---|---|
| the Staff Engineer (`agents/staff-eng.md`) | code-semantics | ... |
| security-audit-worker (`agents/security-audit-worker.md`) | security | ... |
| dep-cve-auditor (`agents/dep-cve-auditor.md`) | deps | ... |
| test-evidence-parser (`agents/test-evidence-parser.md`) | tests | ... |

---

## Next section
"""


def _make_doe_fixture(tmp: str) -> str:
    """Build a minimal DoE-claude-shaped tree and return it as a REPO_DOE_CLAUDE root.

    Same fixture shape as the op's own characterization suite
    (`coordinator_core/ops/test_verify_parallel_review_lens_orthogonality.py ::
    _make_repo`), reached here through rung 1 of `coordinator_doe_root()` — the
    operator override — because the guard CLI exposes no `--doe-root` argv.

    Only the CHUNK-mode cases use it. The static check runs FIRST and
    short-circuits, so a chunk case pointed at the live sibling repo is not
    testing chunk disjointness at all: it passes or fails on whatever that
    repo's manifest table happens to hold. T1 is deliberately left on the live
    tree — a real cross-repo drift canary is its whole subject, and pinning it
    to a fixture would delete it.
    """
    skills_dir = os.path.join(tmp, "coordinator", "skills", "parallel-code-review")
    os.makedirs(skills_dir)
    with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(_GOOD_SKILL_MD)

    agents_dir = os.path.join(tmp, "coordinator", "agents")
    os.makedirs(agents_dir)
    for name in (
        "staff-eng.md",
        "security-audit-worker.md",
        "dep-cve-auditor.md",
        "test-evidence-parser.md",
    ):
        with open(os.path.join(agents_dir, name), "w", encoding="utf-8") as f:
            f.write("# agent\n")

    return tmp


def _load_guard_module():
    """Load parallel-review-orthogonality-guard.py by path (hyphenated
    filename, not import-name-shaped) so its module-level
    `_STATIC_FAILURE_MARKER` constant is reachable for the drift pin below.
    Mirrors `test_bin_module_scope_carriers_importable.py::_load_by_path`.
    """
    module_name = "parallel_review_orthogonality_guard_under_test"
    spec = importlib.util.spec_from_file_location(module_name, _CLI)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.modules.pop(module_name, None)
    return module


class TestStaticFailureMarkerDrift(unittest.TestCase):
    def test_guard_marker_matches_verify_op_static_failure_line(self):
        # Finding 1 (code-reviewer, this slice): the guard's
        # `_STATIC_FAILURE_MARKER` is a hand-duplicated copy of the verify
        # op's own terminal line for a failing static check, with no shared
        # source of truth. An importable-constant or distinct-exit-code fix
        # was weighed and rejected (see dispatch report — engine-import cost
        # on a deliberately thin bin wrapper, and the verify CLI's exit code
        # is documented parity-critical against the retired bash oracle).
        # This test is the substitute: it fails loudly, at the contract
        # layer, the moment the two strings drift, instead of surfacing as a
        # silently-crossed refusal message the way the bug this commit fixed
        # did.
        guard = _load_guard_module()

        with tempfile.TemporaryDirectory() as tmp:
            skill_file = Path(tmp) / "SKILL.md"
            skill_file.write_text(
                "# parallel-code-review\n\n"
                "## Lens-Domain Manifest\n\n"
                "| Reviewer | Lens domain | Rationale |\n"
                "|---|---|---|\n"
                "| the Staff Engineer (`agents/missing-agent.md`) | code-semantics | ... |\n\n"
                "---\n",
                encoding="utf-8",
            )
            agents_dir = Path(tmp) / "agents"  # deliberately not created -> FAIL
            out_lines, passed = static_check(skill_file, agents_dir)

        self.assertFalse(passed)
        self.assertIn(
            guard._STATIC_FAILURE_MARKER,
            "\n".join(out_lines),
            "verify_parallel_review_lens_orthogonality.static_check()'s failure "
            "line no longer contains the guard's _STATIC_FAILURE_MARKER — the "
            "two are hand-duplicated copies of the same prose and have drifted. "
            "Update parallel-review-orthogonality-guard.py's "
            "_STATIC_FAILURE_MARKER to match.",
        )


class TestGuardStatic(unittest.TestCase):
    def test_static_ok_against_real_repo(self):
        # T1 — deliberately LIVE, not fixtured: the lens-domain manifest is
        # DoE-claude's (coordinator/skills/parallel-code-review/SKILL.md,
        # resolved by coordinator_doe_root()), and asserting the real sibling
        # tree is this case's whole subject. A red here is a cross-repo drift
        # report addressed to that repo, not a defect in this one — pinning it
        # to a fixture would delete the canary. The guard runs from
        # coordinator/bin/ regardless of cwd (Path(__file__)-relative).
        proc = _run_cli(["guard"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("OK (static)", proc.stdout)


class TestGuardChunkManifest(unittest.TestCase):
    """Chunk-mode cases, each on a fixture DoE root (`_make_doe_fixture`).

    The static check runs first and short-circuits, so on the live sibling
    tree these three assert the sibling's manifest table, not chunk
    disjointness — T3 read as "partitions overlap" over a two-line manifest
    that plainly does not overlap.
    """

    def test_missing_manifest_is_chunk_mode_refusal(self):
        # T2 — a manifest path that doesn't exist fails in the CHUNK leg, so
        # the guard's refusal line must be the chunk-mode message. Reaching
        # that leg at all requires the static check to pass first.
        with tempfile.TemporaryDirectory() as tmp:
            doe = _make_doe_fixture(tmp)
            proc = _run_cli(
                ["guard", "--chunk-manifest", "/nonexistent-manifest.tsv"], doe_root=doe
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn(
                "Chunk partitions are not disjoint by file-scope; refusing to dispatch.",
                proc.stderr,
            )
            self.assertNotIn("Lens-orthogonality assertion failed", proc.stderr)

    def test_static_failure_under_chunk_mode_names_the_static_check(self):
        # T2b — the crossed-message case: --chunk-manifest given, but the
        # STATIC check is what refused. Naming the mode instead of the failing
        # check told the operator their partitions overlapped when the
        # manifest table was missing, and the manifest was never opened.
        with tempfile.TemporaryDirectory() as tmp:
            doe = _make_doe_fixture(tmp)
            skill = os.path.join(
                doe, "coordinator", "skills", "parallel-code-review", "SKILL.md"
            )
            with open(skill, "w", encoding="utf-8") as f:
                f.write("# parallel-code-review\n\nno manifest table here\n")
            manifest = os.path.join(tmp, "chunk-manifest.tsv")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("chunk-1\tsrc/a.py\n")
                f.write("chunk-2\tsrc/b.py\n")
            proc = _run_cli(["guard", "--chunk-manifest", manifest], doe_root=doe)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Lens-orthogonality assertion failed", proc.stderr)
            self.assertNotIn("Chunk partitions are not disjoint", proc.stderr)

    def test_disjoint_manifest_passes(self):
        # T3
        with tempfile.TemporaryDirectory() as tmp:
            doe = _make_doe_fixture(tmp)
            manifest = os.path.join(tmp, "chunk-manifest.tsv")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("chunk-1\tsrc/a.py\n")
                f.write("chunk-2\tsrc/b.py\n")
            proc = _run_cli(["guard", "--chunk-manifest", manifest], doe_root=doe)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_overlapping_manifest_fails(self):
        # T4 — same file in two chunks must trip the refusal.
        with tempfile.TemporaryDirectory() as tmp:
            doe = _make_doe_fixture(tmp)
            manifest = os.path.join(tmp, "chunk-manifest.tsv")
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("chunk-1\tsrc/a.py\n")
                f.write("chunk-2\tsrc/a.py\n")
            proc = _run_cli(["guard", "--chunk-manifest", manifest], doe_root=doe)
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

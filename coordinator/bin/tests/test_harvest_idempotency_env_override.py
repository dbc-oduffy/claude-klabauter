from __future__ import annotations
"""
test_harvest_idempotency_env_override.py — regression test for the
scan-root/write-root env-precedence mismatch in coordinator-harvest-deferrals.

Spec backlink: DoE-claude:pln-full-coverage-planning-posture-bca96f § C7
(idempotency-guarantee fix, dispatched as a same-day follow-up fix chunk)

Defect (confirmed, reproduced): coordinator-harvest-deferrals'
_candidate_search_dirs() resolved its project-scope improvement-queue
candidate dir via _repo_root() (a bare `git rev-parse --show-toplevel` against
the PROCESS CWD), while the write seam it must stay in parity with —
coordinator-queue-append's _output_path() — resolves its output root with
QUEUE_APPEND_OUTPUT_ROOT taking precedence FIRST over any git-root/cwd logic.
Likewise coordinator-lesson-promote's _outbox_root() honours
LESSON_PROMOTE_OUTBOX_ROOT first. When a caller sets these env overrides
WITHOUT ALSO chdir-ing into the same directory (the isolated-test-invocation
shape, not the production in-repo shape), the write seams wrote to the
override dir while the harvest's scan looked in the cwd's git-root dir — the
harvest-key marker was never found, and a second run double-wrote a
"-2.yaml"/duplicate entry, silently breaking the documented idempotency
guarantee.

This test deliberately does NOT use the existing suite's
_run_harvest_in_isolated_repo-style workaround (cwd-into-a-git-init'ed temp
dir matching the env override) — that workaround masks the bug by making
scan-root and write-root coincide anyway. Instead this test runs the harvest
CLI from a cwd that is DELIBERATELY DIFFERENT from (and unrelated to) the
QUEUE_APPEND_OUTPUT_ROOT / LESSON_PROMOTE_OUTBOX_ROOT targets, exercising the
env-override path directly. Pre-fix, this reproduces the double-write; post-fix
it must be fully idempotent (zero new files on the second run).

Run with: python3 -m pytest test_harvest_idempotency_env_override.py
"""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)  # coordinator/

_HARVEST_CLI = os.path.join(_BIN_DIR, "coordinator-harvest-deferrals.py")
_FIXTURES_DIR = os.path.join(_THIS_DIR, "fixtures", "plan-tasks-spine")
_FIXTURE_VALID = os.path.join(_FIXTURES_DIR, "valid-spine-with-deferrals.md")

_SUBPROCESS_TIMEOUT_SECS = 30


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _yaml_files_in(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".yaml"))


def test_second_run_idempotent_with_env_override_and_mismatched_cwd(stamped_engine_env: str) -> None:
    """Reproduces the confirmed defect directly: QUEUE_APPEND_OUTPUT_ROOT and
    LESSON_PROMOTE_OUTBOX_ROOT point at an isolated output root, but the harvest
    CLI is invoked from a SEPARATE cwd (this repo's coordinator/bin/tests dir,
    which is itself inside a git repo unrelated to the output root) — the exact
    shape that pre-fix caused the scan to look in the wrong directory.
    """
    name = "test_second_run_idempotent_with_env_override_and_mismatched_cwd"

    output_root = tempfile.mkdtemp(prefix="harvest-envoverride-output-")
    plan_dir = tempfile.mkdtemp(prefix="harvest-envoverride-plandir-")
    try:
        plan_path = os.path.join(plan_dir, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write(_read(_FIXTURE_VALID))

        env = dict(os.environ)
        env["QUEUE_APPEND_OUTPUT_ROOT"] = output_root
        env["LESSON_PROMOTE_OUTBOX_ROOT"] = os.path.join(output_root, "state", "lessons-outbox")
        env.pop("DOE_ROOT", None)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)

        cmd = ["python3", os.path.abspath(_HARVEST_CLI), "--plan", plan_path]

        # Deliberately mismatched cwd: NOT plan_dir, NOT output_root — this
        # repo's tests dir (a real git repo, distinct git-root from either).
        mismatched_cwd = _THIS_DIR

        r1 = subprocess.run(
            cmd, cwd=mismatched_cwd, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS
        )
        if r1.returncode != 0:
            raise AssertionError(f"{name}: first run: expected exit 0, got {r1.returncode}. stderr={r1.stderr}")
        if "Queued 2 deferred items" not in r1.stdout:
            raise AssertionError(f"{name}: first run: expected 'Queued 2 deferred items', got: {r1.stdout!r}")

        r2 = subprocess.run(
            cmd, cwd=mismatched_cwd, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS
        )
        if r2.returncode != 0:
            raise AssertionError(f"{name}: second run: expected exit 0, got {r2.returncode}. stderr={r2.stderr}")
        if "Queued 0 deferred items" not in r2.stdout:
            raise AssertionError(
                f"{name}: second run: expected 'Queued 0 deferred items' (fully deduped) with a "
                f"mismatched-cwd env-override invocation, got: {r2.stdout!r} "
                f"(a non-zero count here means the scan-root/write-root env-precedence "
                f"parity regressed — the harvest is double-writing)"
            )
        if "already-harvested" not in r2.stdout:
            raise AssertionError(f"{name}: second run: expected an 'already-harvested' dedup note, got: {r2.stdout!r}")

        qdir = os.path.join(output_root, "state", "improvement-queue")
        ldir = os.path.join(output_root, "state", "lessons-outbox")
        q_files = _yaml_files_in(qdir)
        l_files = _yaml_files_in(ldir)
        if len(q_files) != 1:
            raise AssertionError(
                f"{name}: expected exactly 1 improvement-queue file in the env-override output root "
                f"after two runs (idempotent), found {q_files}"
            )
        if len(l_files) != 1:
            raise AssertionError(
                f"{name}: expected exactly 1 lessons-outbox file in the env-override output root "
                f"after two runs (idempotent), found {l_files}"
            )
    finally:
        shutil.rmtree(output_root, ignore_errors=True)
        shutil.rmtree(plan_dir, ignore_errors=True)


def main() -> int:
    print("test_harvest_idempotency_env_override.py")
    print("=" * 50)

    if not os.path.isfile(_HARVEST_CLI):
        print(f"ERROR: harvest CLI not found at {_HARVEST_CLI}", file=sys.stderr)
        return 1
    if not os.path.isfile(_FIXTURE_VALID):
        print(f"ERROR: fixture not found at {_FIXTURE_VALID}", file=sys.stderr)
        return 1

    # Guard: never allow this test to touch the real state dirs even if a
    # future edit accidentally drops an env override or cwd redirect.
    real_queue_dir = os.path.join(_COORDINATOR_DIR, "..", "state", "improvement-queue")
    real_queue_dir = os.path.normpath(real_queue_dir)
    before = set(os.listdir(real_queue_dir)) if os.path.isdir(real_queue_dir) else set()

    print("\n-- env-override idempotency (mismatched cwd, direct defect reproduction) --")
    failures: list[str] = []
    try:
        test_second_run_idempotent_with_env_override_and_mismatched_cwd()
        print("  PASS: test_second_run_idempotent_with_env_override_and_mismatched_cwd")
    except AssertionError as exc:
        print(f"  FAIL: {exc}")
        failures.append(str(exc))

    after = set(os.listdir(real_queue_dir)) if os.path.isdir(real_queue_dir) else set()
    if after - before:
        msg = (
            f"real_state_dir_untouched_guard: real state/improvement-queue/ gained "
            f"unexpected file(s): {after - before} — this test must never write to "
            f"the real repo state dirs"
        )
        print(f"  FAIL: {msg}")
        failures.append(msg)

    print()
    print(f"Results: {len(failures)} failed")
    if failures:
        print()
        print("Failures:")
        for msg in failures:
            print(f"  FAIL: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

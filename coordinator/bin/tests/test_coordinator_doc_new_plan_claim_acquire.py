"""test_coordinator_doc_new_plan_claim_acquire.py -- coverage for the
author-side plan claim acquisition wired into `coordinator-doc-new --type
plan`'s `_scaffold_plan` write path.

Purpose: nothing on the plan-authoring path previously acquired
`coordinator_core.session.claims.claim_plan` -- it was reachable only from
`/pickup` and workstream-complete's `d-claim-plan-execution-lock` -- while
`/handoff`'s d5 directive already emits `session-claim-cli release-artifact
plan <slug>` on every handoff, releasing a claim nothing on the authoring
path took. This suite pins the acquire half:

1. A plan scaffold takes the claim under the plan's bare stem (no path
   separator, no `.md` suffix) -- `coordinator_core.session.claims.claim_plan`
   rejects a path-shaped slug loud and non-zero, so a passing claim proves
   the bare-stem contract was honoured.
2. A claim failure (the slug already held by a live peer session) does NOT
   fail the scaffold -- the plan file still lands and the CLI still exits 0
   (non-fatal, mirroring `claim_plan`'s own session-shape.json write).
3. A non-plan doc_type (e.g. `memo`) takes no plan claim at all.

Negative-spec: does not re-cover the sizing reverse-edge transaction
(test_coordinator_doc_new_sizing_reverse_edge.py's surface) or claim_plan's
own slug-validation unit coverage (coordinator_core/session/tests, if any) --
only the NEW call site wired here.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as the sizing reverse-edge suite.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_claim_acquire.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

import pytest

# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_claim_acquire_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_claim_acquire_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, **_NO_CONSOLE)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.email=test@test", "-c", "user.name=Test",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        capture_output=True,
        **_NO_CONSOLE,
    )


@contextlib.contextmanager
def _tmp_git_repo():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "testrepo"
        repo.mkdir()
        _init_git_repo(repo)
        out_path = repo / "custom-out.md"
        yield repo, out_path


def _run_cli(repo: Path, out_path: Path, title: str, doc_type: str = "plan",
             session_id: str = "test-session-abc", extra_env: dict | None = None,
             extra_args: list | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_SESSION_ID"] = session_id
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, str(_CLI_PATH), "--type", doc_type, "--title", title, "--out", str(out_path)]
    if doc_type == "plan":
        args.append("--no-sizing-object")
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(
        args,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        **_NO_CONSOLE,
    )


class PlanClaimAcquiredOnScaffoldTest(unittest.TestCase):
    """AC1: a plan scaffold takes the claim under the bare stem."""

    def test_plan_claim_lands_under_bare_stem(self):
        with _tmp_git_repo() as (repo, out_path):
            result = _run_cli(repo, out_path, "Plan claim acquire happy path")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())

            stem = out_path.stem
            claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / stem
            self.assertTrue(claim_dir.is_dir(), f"expected claim dir at {claim_dir}")
            self.assertEqual(
                (claim_dir / "session_id").read_text().strip(), "test-session-abc"
            )


class PlanClaimFailureNonFatalTest(unittest.TestCase):
    """AC2: a claim failure does not fail the scaffold.

    Exercised in-process (not via subprocess) so ``claim_plan`` can be
    forced to fail deterministically -- reproducing a genuine live-peer
    collision via ``liveness.claim_holder_live`` across a subprocess
    boundary needs a real live PID under a registered session, which this
    suite has no sanctioned way to fabricate; forcing the failure directly
    at the one new call site under test is the narrower, deterministic
    equivalent.
    """

    def test_claim_plan_failure_does_not_block_scaffold(self):
        with _tmp_git_repo() as (repo, out_path):
            import unittest.mock as mock

            from coordinator_core.session import claims as claims_mod

            argv = [
                "coordinator-doc-new", "--type", "plan",
                "--title", "Plan Claim Acquire Non Fatal Failure",
                "--out", str(out_path), "--no-sizing-object",
            ]
            env_patch = {"COORDINATOR_SESSION_ID": "test-session-nonfatal"}
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(claims_mod, "claim_plan", return_value=False), \
                 mock.patch.dict(os.environ, env_patch), \
                 mock.patch("os.getcwd", return_value=str(repo)):
                old_cwd = os.getcwd()
                os.chdir(repo)
                try:
                    _cli.main()
                except SystemExit as exc:
                    self.assertIn(exc.code, (0, None))
                finally:
                    os.chdir(old_cwd)

            self.assertTrue(out_path.exists())


class NonPlanDocTypeTakesNoPlanClaimTest(unittest.TestCase):
    """AC3: a non-plan doc_type takes no plan claim."""

    def test_memo_scaffold_takes_no_plan_claim(self):
        with _tmp_git_repo() as (repo, out_path):
            memo_out = repo / "custom-memo.md"
            result = _run_cli(
                repo, memo_out, "Plan claim non-plan doc type", doc_type="memo",
                extra_args=["--to", "peer-em", "--topic", "test"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(memo_out.exists())

            plan_claims_dir = repo / ".git" / "coordinator-sessions" / "plan-claims"
            self.assertFalse(plan_claims_dir.exists())


class AuthorThenExecuteReclaimIsNoOpTest(unittest.TestCase):
    """AC4: the same session authoring a plan and then executing it
    re-claims cleanly rather than contending with itself.

    This seam did not exist until the authorship acquire landed: before
    it, `/execute-plan`'s Step 0 `claim-plan` was the FIRST acquire any
    session made against a given plan, so the re-entrant branch was only
    ever reached on a genuine re-entry (compaction, a second Phase 1.5
    pass). Author-then-execute in one session now reaches it on the
    first execute, a path with no prior coverage anywhere -- raised by
    doe-claude-em in cross-repo/inbox/2026-08-12-doe-claude-em-claim-at-
    authorship-widened.md, which correctly noted it had never run.

    Guards `claim_artifact`'s plan-class-only re-entrant self-claim
    branch: remove it and this test contends and fails, which is exactly
    what /execute-plan's Step 0 would do to a session that authored its
    own plan.
    """

    def test_execute_plan_step0_reclaim_after_authorship_returns_true(self):
        with _tmp_git_repo() as (repo, out_path):
            result = _run_cli(repo, out_path, "Author then execute same session")
            self.assertEqual(result.returncode, 0, result.stderr)
            stem = out_path.stem

            env = dict(os.environ)
            env["COORDINATOR_SESSION_ID"] = "test-session-abc"
            reclaim = subprocess.run(
                [sys.executable, str(_CLI_PATH.parent / "session-claim-cli.py"), "claim-plan", stem],
                cwd=str(repo), capture_output=True, text=True, timeout=30,
                env=env, **_NO_CONSOLE,
            )

            self.assertEqual(
                reclaim.returncode, 0,
                "same-session re-claim after authorship must no-op to success, "
                f"not contend: {reclaim.stdout}{reclaim.stderr}",
            )
            self.assertNotIn("concurrent /pickup detected", reclaim.stderr)
            claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / stem
            self.assertEqual(
                (claim_dir / "session_id").read_text().strip(), "test-session-abc",
                "the re-claim must leave the original holder in place, not take over",
            )

    def test_different_session_still_contends(self):
        """The negative half: the carve-out is scoped to the SAME session.
        A different live session must still be refused -- otherwise the
        re-entrant branch would have widened into a claim bypass."""
        with _tmp_git_repo() as (repo, out_path):
            result = _run_cli(repo, out_path, "Author then foreign execute")
            self.assertEqual(result.returncode, 0, result.stderr)
            stem = out_path.stem

            env = dict(os.environ)
            env["COORDINATOR_SESSION_ID"] = "test-session-different"
            reclaim = subprocess.run(
                [sys.executable, str(_CLI_PATH.parent / "session-claim-cli.py"), "claim-plan", stem],
                cwd=str(repo), capture_output=True, text=True, timeout=30,
                env=env, **_NO_CONSOLE,
            )

            claim_dir = repo / ".git" / "coordinator-sessions" / "plan-claims" / stem
            self.assertEqual(
                (claim_dir / "session_id").read_text().strip(), "test-session-abc",
                "a foreign session must not silently take over the author's claim",
            )


if __name__ == "__main__":
    unittest.main()

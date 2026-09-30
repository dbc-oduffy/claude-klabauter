"""test_coordinator_doc_new_plan_claim_acquire.py -- pins that plan authorship
takes NO plan claim.

Purpose: `coordinator-doc-new --type plan` scaffolds a draft; a draft that is
never executed must not hold an execution claim. The acquirers are
`session-claim-cli claim-plan --for-execution` (execution) and `/pickup`.

1. A plan scaffold exits 0 and leaves no `plan-claims/<stem>/` directory.
2. An abandoned draft is claimable by a different session (session B takes it
   after session A authored it).
3. Several scaffolds under one session id leave that session holding no plan
   claims (`list_held_plan_claims` returns `[]`).
4. A non-plan doc_type takes no plan claim either.

Negative-spec: does not cover the sizing reverse-edge transaction or the
re-entrant same-session branch (covered by
coordinator_core/session/tests/test_claims.py::test_plan_class_reentrant_same_session_accepted).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint.

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


_SESSION_A = "test-session-author"
_SESSION_B = "test-session-picker"


def _plan_claims_dir(repo: Path) -> Path:
    return repo / ".git" / "coordinator-sessions" / "plan-claims"


class PlanScaffoldTakesNoClaimTest(unittest.TestCase):
    """A plan scaffold leaves no claim directory."""

    def test_plan_scaffold_creates_no_claim_dir(self):
        with _tmp_git_repo() as (repo, out_path):
            result = _run_cli(repo, out_path, "Plan authorship takes no claim",
                              session_id=_SESSION_A)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            self.assertFalse(
                (_plan_claims_dir(repo) / out_path.stem).exists(),
                "plan authorship must not take a claim",
            )


class AbandonedDraftIsClaimableTest(unittest.TestCase):
    """A draft authored under session A is claimable by session B."""

    def test_other_session_claims_abandoned_draft(self):
        with _tmp_git_repo() as (repo, out_path):
            result = _run_cli(repo, out_path, "Abandoned draft seed", session_id=_SESSION_A)
            self.assertEqual(result.returncode, 0, result.stderr)
            stem = out_path.stem

            env = dict(os.environ)
            env["COORDINATOR_SESSION_ID"] = _SESSION_B
            claim = subprocess.run(
                [sys.executable, str(_BIN_DIR / "session-claim-cli.py"), "claim-plan", stem],
                cwd=str(repo), capture_output=True, text=True, timeout=30,
                env=env, **_NO_CONSOLE,
            )
            self.assertEqual(claim.returncode, 0, f"{claim.stdout}{claim.stderr}")
            self.assertEqual(
                (_plan_claims_dir(repo) / stem / "session_id").read_text().strip(),
                _SESSION_B,
            )


class BlitzShapeHoldsNoClaimsTest(unittest.TestCase):
    """Three scaffolds under one session id leave no held plan claims."""

    def test_three_scaffolds_hold_no_claims(self):
        from coordinator_core.session.claimed_plan import list_held_plan_claims

        with _tmp_git_repo() as (repo, _unused):
            for n in range(3):
                out = repo / f"blitz-plan-{n}.md"
                result = _run_cli(repo, out, f"Blitz plan {n}", session_id=_SESSION_A)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(out.exists())

            saved = os.environ.get("COORDINATOR_SESSION_ID")
            os.environ["COORDINATOR_SESSION_ID"] = _SESSION_A
            try:
                held = list_held_plan_claims(repo)
            finally:
                if saved is None:
                    os.environ.pop("COORDINATOR_SESSION_ID", None)
                else:
                    os.environ["COORDINATOR_SESSION_ID"] = saved
            self.assertEqual(held, [])


class NonPlanDocTypeTakesNoPlanClaimTest(unittest.TestCase):
    """A non-plan doc_type takes no plan claim."""

    def test_memo_scaffold_takes_no_plan_claim(self):
        with _tmp_git_repo() as (repo, out_path):
            memo_out = repo / "custom-memo.md"
            result = _run_cli(
                repo, memo_out, "Plan claim non-plan doc type", doc_type="memo",
                extra_args=["--to", "peer-em", "--topic", "test", "--kind", "fyi"],
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(memo_out.exists())
            self.assertFalse(_plan_claims_dir(repo).exists())


if __name__ == "__main__":
    unittest.main()

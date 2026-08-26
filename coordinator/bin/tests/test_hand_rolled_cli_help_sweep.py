"""test_hand_rolled_cli_help_sweep.py — `--help`/`-h` must exit 0 on every
hand-rolled (non-argparse) `coordinator/bin` trampoline this suite names.

Defect this closes: the 2026-08-14 end-of-run ENTRYPOINT gate (`publish.py`
§ chunk C5) failed a klabauter publish round -- 20 of 66 shipped entrypoints
exited non-zero on `--help` (their hand-rolled argv parsers treated it as an
unrecognized argument/subcommand, even though every one of them already had
the usage text to print). Fixed by recognizing `--help`/`-h` as a first-class
request in each parser, printing the SAME usage text, exiting 0 -- never
converted to argparse (deliberate in this codebase), and no other flag or
exit code changed.

Full failing-entrypoint capture: state/audits/2026-08-14-klabauter-publish-
round-final4.txt (grep `(rc=`).

`claude-doe` is covered separately below (`TestClaudeDoeHelp`), not via the
`_SWEPT_HELP_ENTRYPOINTS` sweep -- its 2026-08-14 gate failure was a
publish-mirror registry-resolution failure (`repos.example_doctrine_repo`
unset in that sandbox, resolving to nothing), not an unrecognized-`--help`
failure: it never got a chance to answer `--help` before DoE-clone
resolution ran unconditionally first and failed. Fixed by answering
`--help`/`-h` directly, ahead of any clone/registry resolution -- see
`claude-doe.py`'s `_USAGE` and the module header. The dedicated test class
below exercises the registry-absent condition explicitly (`REPO_DOE_CLAUDE`
unset, `CLAUDE_DOE_MACHINE_LOCAL_BIN` pointed at a nonexistent path), since
that is the exact publish-sandbox shape the gate failure reproduced.

`coordinator-safe-commit` is exercised through its exported `main`/`usage`
(not a bare subprocess spawn) since a real invocation touches the commit
guard; every other entry here is a subprocess sweep asserting the literal
process exit code the ENTRYPOINT gate itself observes.

Run:
    pytest coordinator/bin/tests/test_hand_rolled_cli_help_sweep.py -v
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _BIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from cc_invoke import _no_console_kw  # noqa: E402

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# Every hand-rolled trampoline fixed by the 2026-08-14 publish-round chunk,
# relative to coordinator/bin/, invoked as `python3 <file> --help`.
_SWEPT_HELP_ENTRYPOINTS = (
    "backlog-grind-assemble.py",
    "consolidate-assemble.py",
    "coordinator-assemble.py",
    "coordinator-fold-execution-record.py",
    "coordinator-gate.py",
    "coordinator-safe-name.py",
    "handoff-carry-gate.py",
    "handoff-gate-aging.py",
    "learn-lessons-reconcile-candidates.py",
    "merge-assemble.py",
    "orient-assemble.py",
    "pickup-assemble.py",
    "remove-claude-klabauter-precommit-hook.py",
    "review-assemble.py",
    "review-exec-auth-stamp.py",
    "roadmap-number-stubs.py",
    "schema-drift-gate.py",
    "sizing-assemble.py",
    "snippet-registry.py",
    "staff-session-assemble.py",
    "workstream-complete-assemble.py",
)


# Review: code-reviewer (a1e94259) -- `_SWEPT_HELP_ENTRYPOINTS` above is a
# closed list that does not track new hand-rolled entrypoints added to
# coordinator/bin. A full behavioral --help sweep over the whole population
# is genuinely infeasible here: the population reused by the actual
# publish-time ENTRYPOINT gate (`coordinator_core.percolate.engine.
# enumerate_gate_entrypoints`) enumerates BARE post-publish names, not the
# dev-tree `.py` sources this test invokes directly, so it cannot be reused
# 1:1 as a source of truth for this file's population without re-deriving the
# publish-time bare-name transform (out of scope here, and `publish.py` is
# off-limits to this dispatch). Executing `--help` against the ~250 files
# below to VERIFY they behave correctly is also out of scope -- that is a
# multi-file remediation effort, not a review-integration fix.
#
# What IS in scope and applied: a static DIVERGENCE guard. `_candidate_hand_
# rolled_entrypoints` recomputes, via the same has-`__main__`-guard /
# lacks-`argparse`-import heuristic the 19 originally-fixed files share, every
# coordinator/bin/*.py file that looks like a hand-rolled CLI entrypoint. Any
# name in that computed set must appear in `_SWEPT_HELP_ENTRYPOINTS` (verified
# correct), `_SEPARATELY_TESTED_ENTRYPOINTS` (covered by a dedicated test
# class below), or `_LEGACY_UNVERIFIED_ENTRYPOINTS` (pre-existing as of this
# guard's introduction, --help behavior NOT verified by this test). A NEW
# hand-rolled entrypoint added later that matches none of the three is
# caught by `test_candidate_population_has_no_untracked_entrypoints` below,
# forcing an explicit disposition instead of silent non-coverage.
_SEPARATELY_TESTED_ENTRYPOINTS = frozenset({
    "claude-doe.py",  # TestClaudeDoeHelp, whole-argv scan, its own env staging
    "coordinator-safe-commit.py",  # exercised via exported main/usage, not a bare subprocess spawn
})

# Pre-existing hand-rolled entrypoints as of this guard's introduction
# (2026-08-14). NOT verified by this test to answer `--help` cleanly --
# tracked here only so a genuinely NEW entrypoint is distinguishable from
# this already-large legacy population. Verifying/fixing any one of these is
# a separate, scoped follow-up, not this guard's job.
_LEGACY_UNVERIFIED_ENTRYPOINTS = frozenset({
    "advance-tracker-status.py",
    "agent-worktree-sweep.py",
    "aggregate-chain-loe.py",
    "append-goal-event.py",
    "append-integrator-dispositions.py",
    "append-plan-session.py",
    "archive-paper-trail.py",
    "archive-stamp-cli.py",
    "assert-cwd.py",
    "assert-no-dangling-plan-backlinks.py",
    "assert-no-terminal-plans-in-live.py",
    "assert-plan-sizing-citation.py",
    "audit-enabled-plugins.py",
    # Restored 2026-08-16: pruned from this list alongside C2's 14 genuinely
    # caller-less migrate-*/backfill-* deletions, but this entry point was
    # NOT deleted -- workday-complete-step9 and workweek_complete/brief.py
    # both reach it, so it kept its .py and its .cmd rung. The prune matched
    # the deletion set's shape rather than the deletion set.
    "backfill-week-changelog-gaps.py",
    "audit-roadmap.py",
    "autonomous-verb.py",
    "baton-assemble.py",
    "baton-drift-sweep.py",
    "blocked.py",
    "capture-fan-out-threshold.py",
    "central-run-due.py",
    "check-arch-audit-staleness.py",
    "check-atlas-watch-drift.py",
    "check-auto-memory-drained.py",
    "check-auto-reconcile.py",
    "check-bin-sh-polyglot.py",
    "check-competitor-positioning-nudge.py",
    "check-deferral-orphan-memo.py",
    "check-deferral-partial-strangle.py",
    "check-description-length.py",
    "check-em-environment.py",
    "check-engine-drift.py",
    "check-forwarder-drift.py",
    "check-global-doctrine-mirror.py",
    "check-harvest-debt.py",
    "check-machine-local-regeneratability.py",
    "check-machine-path-leak.py",
    "check-claude-klabauter-doctor-sentinel.sh",
    "check-mcp-versions.py",
    "check-multi-event-hook-hardcoded-event.py",
    "check-no-illegal-paths.py",
    "check-no-monolith-completion-append.py",
    "check-pcli-drift-gate.py",
    "check-persona-slug-leak.py",
    "check-plugin-drift.py",
    "check-posix-exec-assumptions.py",
    "check-rag-state.py",
    "check-registry-codename-leak.py",
    "check-sh-suffix-polyglot.py",
    "check-shipped-on-main.py",
    "check-surface-inline-budget.py",
    "check-version-consistency.py",
    "check-weekly-staleness.py",
    "check-workstream-complete-deletion-blocks.py",
    "check-wsc-inline-budget.py",
    "claims-emit.py",
    "classify-dispatch-shape.py",
    "claude-ue-bootstrap.py",
    "close-out-and-stamp.py",
    "cmd-autorun-guard.py",
    "coordinator-auto-push.py",
    "coordinator-ceremony-hook.py",
    "coordinator-complete-entry.py",
    "coordinator-compute-layer-scaffold.py",
    "coordinator-configure-git.py",
    "coordinator-current-branch.py",
    "coordinator-doctor-sentinel.py",
    "coordinator-ensure-post-commit-hook.py",
    "coordinator-ensure-prepare-commit-msg-hook.py",
    "coordinator-initiative.py",
    "coordinator-prepare-commit-msg.py",
    "coordinator-publish.py",
    "coordinator-reap-stale-locks.py",
    "coordinator-render-rollup.py",
    "coordinator-renormalize-index.py",
    "coordinator-resolve-validation-cmd.py",
    "coordinator-session-loe.py",
    "coordinator-setup-state.py",
    "coordinator-tasks-mirror.py",
    "coordinator-uninstall.py",
    "coordinator-workflow-scaffold.py",
    "count-distill-backlog.py",
    "cruft-sweep.py",
    "cutover-cli.py",
    "day-coverage-sweep.py",
    "decode-claude-projects-dir.py",
    "derive-session-hierarchy.py",
    "detect-initiative-candidates.py",
    "detect-project-runtime.py",
    "dirty-tree-gate.py",
    "doctor-catalog-gen.py",
    "doctor-probe-select.py",
    "doctor.py",
    "draft-plan-aging.py",
    "edit-live-hook.py",
    "emit-artifact-shape-contract.py",
    "emit-cadence.py",
    "emit-cockpit-snapshot.py",
    "emit-goal-from-artifact.py",
    "ensure-doe-clone.py",
    "ensure-vscode-readonly.py",
    "extract-scope-paths.py",
    "fan-out-dispatch.py",
    "fan-out-integrator.py",
    "find-polluter.py",
    "fix-concrete-path-citations.py",
    "gen-claude-doe-launcher.py",
    "gen-claude-doe-shim.py",
    "gen-doe-root-pointer.py",
    "gen-settings-hooks.py",
    "generate-exec-summary.py",
    "generate-repomap.py",
    "goal-close-day.py",
    "handoff-loe-summary.py",
    "harvest-exit-interviews.py",
    "identity-cli.py",
    "install-claude-doe-wrapper.py",
    "install-doe-claude-precommit-hook.py",
    "install-health-run.py",
    "install-meta-repo-precommit-hook.py",
    "install-publish-repo-precommit-hook.py",
    "install-sandbox-check.py",
    "install-shell-init-guard-seam.py",
    "learn-lessons-config-update.py",
    "learn-lessons-roots.py",
    "lint-frontmatter.py",
    "list-orphaned-plans.py",
    "list-reverse-drift-cmds.py",
    "list-review-trail-records.py",
    "list-week-changelog.py",
    "mint-deliverable-id.py",
    "misc-session-and-guards.py",
    "new-project-scaffold.py",
    "normalize-consumed-frontmatter.py",
    "normalize-snippet.py",
    "orphan-branch-sweep.py",
    "parse-completeness-item.py",
    "parse-resolves-trailer.py",
    "percolate-preflight-scratch-publish.py",
    "plan-assemble.py",
    "plan-capture-persist.py",
    "plan-tasks-grouping-digest.py",
    "priority-set.py",
    "probe-cwd-example-retrieval-repo-relevance.py",
    "probe-memory-headroom.py",
    "probe-onboarding-currency.py",
    "promote-shipped-in-flight-stubs.py",
    "prune-closed-bugs.py",
    "prune-closed-improvements.py",
    "prune-resolved-queue-entries.py",
    "publish-resolve-target.py",
    "publish-time-transform-py.py",
    "query-completions.py",
    "query-handoff-columns.py",
    "query-session-hierarchy.py",
    "read-frontmatter-field.py",
    "reap-sessions.py",
    "reaper-resting-batons.py",
    "reassess-goal-krs.py",
    "refresh-queries.py",
    "refresh-roadmap-callout.py",
    # render-handoff-tracker.py: Review: code-reviewer (690dd6f9) -- deleted by
    # this diff (state/handoff-tracker.md render path retired); dangling sweep
    # row of the same shape as the earlier wsc_tail.py defect.
    "render-posture-overlay.py",
    "render-template-tree.py",
    "render-template.py",
    "resolve-repo-path.py",
    "review-brightline-gate.py",
    "review-coverage-gate.py",
    "rollup-derive.py",
    "run-platform-localize.py",
    "safe-commit-offer.py",
    "scan_unresolved_ubt_records.py",
    "scan-addon-health.py",
    "session-claim-cli.py",
    "session-liveness-cli.py",
    "session-reachability-cli.py",
    "set-goal-kr-status.py",
    "standup.py",
    "stitch-observer-sidecar.py",
    "sweep-actioned-memos.py",
    "sweep-boot.py",
    "sweep-shipped-handoffs.py",
    # sweep-terminal-plans.py: deleted -- fleet.archive_completed_plans killed
    # and rebuilt from scratch, 2026-08-23 PM ruling; dangling sweep row of the
    # same shape as the render-handoff-tracker.py note above.
    "sync-cockpit-contract.py",
    "sync-main.py",
    "sync-plugin-wiki.py",
    "tier-u-grant-cli.py",
    "untested-platform-advisory.py",
    "validate-install-contract.py",
    "verify-arch-audit-atlas-refresh.py",
    "verify-coverage.py",
    "verify-dist-publish-repo-sync.py",
    "verify-doe-root-seam-sync.py",
    "verify-no-console-flash.py",
    "verify-no-powershell-flash.py",
    "verify-orientation-cache-sync.py",
    "verify-parallel-review-lens-orthogonality.py",
    "verify-ps51-clean.py",
    "verify-publish-targets-portable-sync.py",
    "verify-schema-registry-sync.py",
    "verify-skill-anchor-links.py",
    "verify-snippet-registry-consistency.py",
    "verify-snippet-sync.py",
    "verify-subagent-sandbox-preamble-sync.py",
    "verify-templates-bin-sync.py",
    "verify-templates-setup-sync.py",
    "verify-ue-overrides.py",
    "whats-next.py",
    "workday-complete-args-and-validate.py",
    "workday-complete-assemble.py",
    "workday-complete-backfill-inject-anchor.py",
    "workday-complete-backfill-scan.py",
    "workday-complete-step1-validate.py",
    "workday-complete-step2_5-dirty-tree.py",
    "workday-complete-step3-consolidate.py",
    "workday-complete-step9-append-changelog.py",
    "workday-start-cross-repo-memo-outbox-surface.py",
    "workday-start-cross-repo-memo-surface.py",
    "workday-start-health-probes.py",
    "workday-start-inbox-blitz-assemble.py",
    "workday-start-step0-reconcile.py",
    "workday-start-step0.py",
    "workweek-complete-brief.py",
    "workweek-complete-doc-verify.py",
    "workweek-complete-reverse-drift-gate.py",
    "workweek-trail-scope.py",
    "write-workday-start-marker.py",
})


def _candidate_hand_rolled_entrypoints() -> "set[str]":
    """Every `coordinator/bin/*.py` file that looks like a hand-rolled (non-
    argparse) CLI entrypoint: has a `__main__` guard, has no `import argparse`.
    Purely static/textual -- no subprocess spawn, safe to run unconditionally.
    """
    candidates: "set[str]" = set()
    for path in _BIN_DIR.glob("*.py"):
        name = path.name
        if name.startswith("test_") or name == "conftest.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if '__name__' not in text or "__main__" not in text:
            continue
        if "import argparse" in text:
            continue
        candidates.add(name)
    return candidates


def _run_help(rel_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_BIN_DIR / rel_path), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        **_no_console_kw(str(_BIN_DIR)),
    )


class TestCandidatePopulationTracksSweep(unittest.TestCase):
    """Review: code-reviewer (a1e94259) -- the closed `_SWEPT_HELP_ENTRYPOINTS`
    tuple silently stops covering a NEW hand-rolled entrypoint. This asserts
    the static candidate population never diverges from the union of the
    three tracked buckets without an explicit disposition.
    """

    def test_candidate_population_has_no_untracked_entrypoints(self):
        tracked = (
            set(_SWEPT_HELP_ENTRYPOINTS)
            | set(_SEPARATELY_TESTED_ENTRYPOINTS)
            | set(_LEGACY_UNVERIFIED_ENTRYPOINTS)
        )
        untracked = _candidate_hand_rolled_entrypoints() - tracked
        self.assertEqual(
            untracked,
            set(),
            f"{len(untracked)} new hand-rolled entrypoint(s) with no --help "
            f"disposition: {sorted(untracked)}. Add to _SWEPT_HELP_ENTRYPOINTS "
            "once verified, or to _LEGACY_UNVERIFIED_ENTRYPOINTS with a note.",
        )


class TestHandRolledCliHelpSweep(unittest.TestCase):
    def test_every_swept_entrypoint_exits_zero_on_help(self):
        failures = []
        for rel_path in _SWEPT_HELP_ENTRYPOINTS:
            result = _run_help(rel_path)
            if result.returncode != 0:
                failures.append((rel_path, result.returncode, result.stderr[-500:]))
        self.assertEqual(
            failures, [], f"{len(failures)} entrypoint(s) still fail on --help: {failures}"
        )

    def test_every_swept_entrypoint_writes_help_to_stdout(self):
        # Review: code-reviewer (a1e94259) -- returncode alone doesn't catch a
        # `--help` path that exits 0 but prints usage to stderr instead of
        # stdout (three entrypoints did exactly this via a shared `_usage()`
        # helper defaulting to stderr). A caller capturing only stdout must
        # see non-empty usage text.
        failures = []
        for rel_path in _SWEPT_HELP_ENTRYPOINTS:
            result = _run_help(rel_path)
            if not result.stdout.strip():
                failures.append((rel_path, result.returncode, result.stderr[-500:]))
        self.assertEqual(
            failures,
            [],
            f"{len(failures)} entrypoint(s) wrote no --help text to stdout: {failures}",
        )

    def test_every_swept_entrypoint_exits_zero_on_short_help(self):
        failures = []
        for rel_path in _SWEPT_HELP_ENTRYPOINTS:
            result = subprocess.run(
                [sys.executable, str(_BIN_DIR / rel_path), "-h"],
                capture_output=True,
                text=True,
                timeout=30,
                **_no_console_kw(str(_BIN_DIR)),
            )
            if result.returncode != 0:
                failures.append((rel_path, result.returncode, result.stderr[-500:]))
        self.assertEqual(
            failures, [], f"{len(failures)} entrypoint(s) still fail on -h: {failures}"
        )

    def test_coordinator_safe_commit_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, str(_BIN_DIR / "coordinator-safe-commit.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            **_no_console_kw(str(_BIN_DIR)),
        )
        self.assertEqual(result.returncode, 0, result.stderr[-500:])

    def test_a_genuinely_unknown_flag_still_refuses_nonzero(self):
        # Negative case: adding --help/-h recognition must not have made
        # unrelated unknown flags lenient.
        for rel_path in ("sizing-assemble.py", "roadmap-number-stubs.py"):
            result = subprocess.run(
                [sys.executable, str(_BIN_DIR / rel_path), "--definitely-not-a-flag"],
                capture_output=True,
                text=True,
                timeout=30,
                **_no_console_kw(str(_BIN_DIR)),
            )
            self.assertNotEqual(
                result.returncode, 0, f"{rel_path} should still refuse a bogus flag"
            )


class TestClaudeDoeHelp(unittest.TestCase):
    """`claude-doe --help`/`-h` must exit 0 WITHOUT touching DoE-clone
    registry resolution -- see claude-doe.py's `_USAGE` / module header.
    """

    def _run(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_BIN_DIR / "claude-doe.py"), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            **_no_console_kw(str(_BIN_DIR)),
        )

    def _registry_absent_env(self) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env.pop("REPO_DOE_CLAUDE", None)
        # Simulate the publish sandbox: machine-local unresolvable, so any
        # rung that actually reaches the registry would fail loud rather
        # than silently succeeding via a real local registry on this box.
        env["CLAUDE_DOE_MACHINE_LOCAL_BIN"] = str(
            _BIN_DIR / "tests" / "__nonexistent_machine_local_for_help_test__"
        )
        return env

    def test_help_exits_zero_with_registry_absent(self):
        result = self._run(["--help"], self._registry_absent_env())
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertIn("claude-doe", result.stdout)

    def test_short_help_exits_zero_with_registry_absent(self):
        result = self._run(["-h"], self._registry_absent_env())
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertIn("claude-doe", result.stdout)

    def test_help_does_not_invoke_machine_local(self):
        env = self._registry_absent_env()
        result = self._run(["--help"], env)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertNotIn("machine-local", result.stderr)


if __name__ == "__main__":
    unittest.main()

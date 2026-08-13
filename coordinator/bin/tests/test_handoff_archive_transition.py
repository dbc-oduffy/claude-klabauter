"""test_handoff_archive_transition.py — unit test for
`coordinator/bin/handoff-archive-transition.py`.

Same idiom as test_archive_stamp_cli_ship_handoff.py /
test_archive_stamp_cli_chain_supersede_archive.py: monkeypatches the module's
own seams (`_guard_exit_code`, `_resolve_repo_root`, `cc_invoke.route_mutation`)
so this suite asserts ONLY the CLI's own dispatch logic — guard-result ->
mode selection, opportunistic-vs-hard-fail error handling, argv parsing — not
the engine behind `handoff.archive_transition` or `handoff.has_live_children`
(those are coordinator_core's own test surfaces).

Review: code-reviewer (P3) — exception: `ClosedTargetSupersedeChokePointTest`
below deliberately does the opposite, calling the real op handler
(`coordinator_core.ops.handoff_archive_transition._handler`) directly against
a real on-disk worktree, because the `cc_invoke.route_mutation` seam every
other class here patches is precisely the op choke point that test needs to
exercise (see that class's own docstring).

Loaded by file path (`importlib.machinery.SourceFileLoader`) since the CLI
module has a `.py` extension but is not on `sys.path` as an importable
package member — same load idiom used across coordinator/bin/tests/.

Spec backlink: M3/HO-3 extirpation-plan port of coordinator-claude
coordinator/skills/handoff/SKILL.md §§ "Chain archival — presume the sweep
wins" / "Park-with-links on supersession".

Run:
    pytest coordinator/bin/tests/test_handoff_archive_transition.py -v
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "handoff_archive_transition_test",
        str(_BIN_DIR / "handoff-archive-transition.py"),
    )
    spec = importlib.util.spec_from_loader("handoff_archive_transition_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _RecordingRouteMutation:
    """Stand-in for cc_invoke.route_mutation — records params, returns a
    canned result or raises a canned exception."""

    def __init__(self):
        self.calls: list[dict] = []
        self.result: dict = {}
        self.exc: Exception | None = None

    def __call__(self, op, params, repo_root, legacy_fn):
        self.calls.append({"op": op, "params": params, "repo_root": repo_root})
        if self.exc is not None:
            raise self.exc
        return self.result


class _StubHarness(unittest.TestCase):
    def setUp(self):
        self._orig_route_mutation = _cli.cc_invoke.route_mutation
        self._orig_guard = _cli._guard_exit_code
        self._orig_repo_root = _cli._resolve_repo_root
        self.addCleanup(self._restore)

        self.route_mutation = _RecordingRouteMutation()
        _cli.cc_invoke.route_mutation = self.route_mutation
        _cli._resolve_repo_root = lambda handoff_path: "/fake/repo/root"

        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        self._tmp.close()
        self.handoff_path = self._tmp.name

    def _restore(self):
        _cli.cc_invoke.route_mutation = self._orig_route_mutation
        _cli._guard_exit_code = self._orig_guard
        _cli._resolve_repo_root = self._orig_repo_root
        try:
            os.unlink(self.handoff_path)
        except OSError:
            pass

    def _seed_claimed_frontmatter(self):
        """Write minimal frontmatter satisfying DR-242's `claimed_or_shipped`
        predicate (coordinator_core/archival.py) onto self.handoff_path, so a
        supersede-path test exercises a legitimate successor rather than the
        bare-child shape DR-242 exists to refuse."""
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "claimed_at: 2026-07-20T10:00:00Z\n"
                "claimed_by: test-session\n"
                "---\n"
                "body\n"
            )

    def _seed_landed_chain_frontmatter(self, shipped_in: str):
        """The on-disk shape AFTER a real `handoff.archive_transition`
        mode='stamp_shipped' call has landed — `deployment_state: shipped`
        with a populated `shipped_in`. Used by `cmd_chain`'s self-
        verification tests to simulate the transition landing on disk
        despite a falsy envelope `moved` flag (defect 1)."""
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "deployment_state: shipped\n"
                f"shipped_in: {shipped_in}\n"
                "---\n"
                "body\n"
            )

    def _seed_landed_supersede_frontmatter(self, continued_into: str):
        """Extends `_seed_claimed_frontmatter`'s shape (still satisfies DR-242)
        with `deployment_state: continued` + `continued_into: <continued_into>`
        already present — the on-disk shape AFTER a real
        `handoff.archive_transition` supersede op has landed.

        `route_mutation` is stubbed throughout this harness (§ module
        docstring — it asserts only `cmd_supersede`'s own dispatch logic, not
        the real op), so it never performs an actual write. A test that
        exercises `cmd_supersede`'s AC3 post-write re-read must seed this
        landed shape itself to simulate a working op; a test that wants to
        simulate the reported silent-no-op bug (op claims `superseded: True`
        but the frontmatter never moved) uses `_seed_claimed_frontmatter`
        alone instead, deliberately withholding this shape."""
        with open(self.handoff_path, "w", encoding="utf-8") as fh:
            fh.write(
                "---\n"
                "claimed_at: 2026-07-20T10:00:00Z\n"
                "claimed_by: test-session\n"
                "deployment_state: continued\n"
                f"continued_into: {continued_into}\n"
                "---\n"
                "body\n"
            )


class ChainSubcommandTest(_StubHarness):
    def test_missing_predecessor_fails_loud(self):
        """C1/AC1: a missing predecessor file is NOT a verified prior
        archival — it is indistinguishable on-disk from a bad path or a
        race, so cmd_chain refuses (rc 1) rather than silently claiming the
        async sweep's outcome on its behalf (plan § S5, site 1)."""
        os.unlink(self.handoff_path)
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])

    def test_childless_guard_selects_stamp_shipped(self):
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.route_mutation.calls), 1)
        self.assertEqual(self.route_mutation.calls[0]["params"]["mode"], "stamp_shipped")
        self.assertEqual(self.route_mutation.calls[0]["op"], "handoff.archive_transition")

    def test_has_children_guard_selects_stamp_only(self):
        _cli._guard_exit_code = lambda path, exclude: 0
        self.route_mutation.result = {}
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 0)
        self.assertEqual(self.route_mutation.calls[0]["params"]["mode"], "stamp_only")

    def test_indeterminate_guard_selects_stamp_only(self):
        _cli._guard_exit_code = lambda path, exclude: 2
        self.route_mutation.result = {}
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 0)
        self.assertEqual(self.route_mutation.calls[0]["params"]["mode"], "stamp_only")

    def test_exclude_forwarded(self):
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.cmd_chain(self.handoff_path, ["state/handoffs/x.md", "state/handoffs/y.md"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["exclude"],
            ["state/handoffs/x.md", "state/handoffs/y.md"],
        )

    def test_archive_transition_refusal_returns_1(self):
        """C1/AC1: an op-level refusal (RouteMutationError) is now a hard
        failure, not opportunistic-swallowed success — the mutation this
        call promised did not land, so rc must say so (plan § S5, site 4)."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError("refused", {"exit_code": 1})
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)

    # Review: code-reviewer — Defect 1's actual new behavior (bare
    # RuntimeError transport/engine failure -> rc 1) had zero test coverage;
    # a RouteMutationError is a RuntimeError subclass, so the sibling test
    # above alone did not exercise the new `except RuntimeError` branch.
    def test_archive_transition_transport_failure_returns_1(self):
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.exc = RuntimeError("transport down")
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)

    def test_unresolved_repo_root_fails_loud(self):
        """C1/AC1: an unresolvable repo root means no mutation was
        attempted, not a clean pass-through — cmd_chain refuses (rc 1)
        rather than reporting the same 0 a genuine archive would (plan
        § S5, site 2)."""
        _cli._guard_exit_code = lambda path, exclude: 1
        _cli._resolve_repo_root = lambda path: None
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])

    def test_guard_spawn_failure_returns_1(self):
        def _raise(path, exclude):
            raise OSError("no interpreter")

        _cli._guard_exit_code = _raise
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])

    def test_archive_deferred_ambiguous_fails_loud(self):
        """C1 negative-spec (Finding 3): a childless-guard 'stamp_shipped'
        dispatch whose op envelope reports no landing (`moved` falsy) AND
        whose on-disk re-read also shows no landing (the untouched empty
        tmp file this harness seeds by default) is genuinely ambiguous —
        neither the envelope nor the disk can distinguish a deliberate
        deferral from a failed move attempt — so this must fail loud (rc 1).
        Mirrors the shape of test_archive_transition_transport_failure_returns_1."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {
            "moved": False,
            "message": "deferred by policy",
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)
        self.assertIn(
            "cannot distinguish a deliberate deferral from a failed attempt",
            stderr.getvalue(),
        )

    def test_moved_false_but_disk_confirms_landing_returns_0(self):
        """Defect 1 regression (`cross-repo/inbox/2026-08-04-market-
        intelligence-em-baton-lifecycle-three-defects.md`): a 'stamp_shipped'
        dispatch whose op envelope reports no landing (`moved` falsy — the
        envelope has in fact never populated the `archived` key this branch
        used to check) but whose target frontmatter shows the transition
        genuinely landed on disk (`deployment_state: shipped` + a populated
        `shipped_in`) must return 0 with NO fail-loud message — the
        production incident this fix closes was exactly this shape."""
        self._seed_landed_chain_frontmatter("abc12345")
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": False}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 0)
        self.assertNotIn(
            "cannot distinguish a deliberate deferral from a failed attempt",
            stderr.getvalue(),
        )
        self.assertIn("confirmed landed via on-disk re-read", stderr.getvalue())

    def test_true_failure_still_fails_loud_after_reread(self):
        """The re-read is a rescue for a misreported success, never a
        blanket amnesty — a genuinely failed/deferred transition (envelope
        AND disk both show no landing) still fails loud, unchanged from
        `test_archive_deferred_ambiguous_fails_loud` but pinned separately
        so a future edit cannot quietly widen the rescue into swallowing
        real failures."""
        self._seed_claimed_frontmatter()
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": False, "message": "git mv failed"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 1)
        self.assertIn(
            "cannot distinguish a deliberate deferral from a failed attempt",
            stderr.getvalue(),
        )

    def test_sha_forwarded_when_supplied(self):
        """C3/AC5: `--sha` (threaded here as cmd_chain's `sha` param) must
        land in params["sha"] verbatim."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.cmd_chain(self.handoff_path, [], sha="deadbeef")
        self.assertEqual(rc, 0)
        self.assertEqual(self.route_mutation.calls[0]["params"]["sha"], "deadbeef")

    def test_force_forwarded_when_supplied(self):
        """C3/AC5: `--force` (threaded here as cmd_chain's `force` param)
        must land in params["force"] truthy."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.cmd_chain(self.handoff_path, [], sha="deadbeef", force=True)
        self.assertEqual(rc, 0)
        self.assertIs(self.route_mutation.calls[0]["params"]["force"], True)

    def test_omitting_sha_and_force_adds_neither_key(self):
        """C3/AC5 negative-spec: omitting both `--sha` and `--force` must
        leave `params` byte-identical to pre-chunk resolution — neither key
        is added at all, not merely falsy-valued. Pins the docstring's
        explicit 'byte-identical to before this chunk' claim (Position A)."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.cmd_chain(self.handoff_path, [])
        self.assertEqual(rc, 0)
        params = self.route_mutation.calls[0]["params"]
        self.assertNotIn("sha", params)
        self.assertNotIn("force", params)


class SupersedeSubcommandTest(_StubHarness):
    def test_missing_continued_into_is_usage_error(self):
        rc = _cli.cmd_supersede(self.handoff_path, None, [])
        self.assertEqual(rc, 2)
        self.assertEqual(self.route_mutation.calls, [])

    def test_blank_continued_into_is_usage_error(self):
        rc = _cli.cmd_supersede(self.handoff_path, "   ", [])
        self.assertEqual(rc, 2)
        self.assertEqual(self.route_mutation.calls, [])

    def test_success_dispatches_supersede_mode(self):
        self._seed_landed_supersede_frontmatter("state/handoffs/successor.md")
        self.route_mutation.result = {"superseded": True}
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.route_mutation.calls), 1)
        params = self.route_mutation.calls[0]["params"]
        self.assertEqual(params["mode"], "supersede")
        self.assertEqual(params["continued_into"], "state/handoffs/successor.md")
        self.assertEqual(params["handoff_path"], self.handoff_path)

    def test_exclude_forwarded(self):
        self._seed_landed_supersede_frontmatter("state/handoffs/successor.md")
        self.route_mutation.result = {"superseded": True}
        rc = _cli.cmd_supersede(
            self.handoff_path, "state/handoffs/successor.md", ["state/handoffs/x.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["exclude"], ["state/handoffs/x.md"]
        )

    def test_landed_frontmatter_matching_successor_returns_0(self):
        """AC3/AC4 positive case: a successful supersede that ACTUALLY left
        `deployment_state: continued` + `continued_into: <successor>` on disk
        returns 0 — the re-read confirms what the op claimed."""
        self._seed_landed_supersede_frontmatter("state/handoffs/successor.md")
        self.route_mutation.result = {"superseded": True}
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 0)
        landed_fields = _cli._reread_frontmatter_fields(
            self.handoff_path, ["deployment_state", "continued_into"]
        )
        self.assertEqual(landed_fields["deployment_state"], "continued")
        self.assertEqual(landed_fields["continued_into"], "state/handoffs/successor.md")

    def test_op_reports_success_but_frontmatter_unchanged_fails_loud(self):
        """AC4 regression guard for the reported incident
        (state/improvement-queue/2026-07-25-archive-stamp-cli-supersede-
        archive-hand-6b71b547b70a.yaml): the op reports `superseded: True`
        but the predecessor's on-disk frontmatter never actually flipped to
        `deployment_state: continued` (simulated here by seeding ONLY the
        DR-242-satisfying claimed shape, withholding the landed shape) —
        `cmd_supersede` must not trust the op's self-report and must return
        non-zero instead of silently exiting 0."""
        self._seed_claimed_frontmatter()
        self.route_mutation.result = {"superseded": True}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)
        self.assertIn("status flip did not land", stderr.getvalue())

    def test_landed_but_mismatched_continued_into_fails_loud(self):
        """AC3: a landed `deployment_state: continued` paired with a
        DIFFERENT `continued_into` than the one this call supplied is also
        not a match — the re-read compares both fields, not just the
        deployment_state enum value."""
        self._seed_landed_supersede_frontmatter("state/handoffs/someone-else.md")
        self.route_mutation.result = {"superseded": True}
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)

    def test_guard_retained_supersede_with_landed_flip_still_returns_0(self):
        """AC3 negative-spec (retention is NOT failure): a guard-retained
        supersede (`retained: True` — live children found, or indeterminate/
        fail-closed) still returns 0 PROVIDED the status flip landed on
        disk — the status flip is unconditional and not gated on the
        archival-move guard, so a legitimate retention must not be conflated
        with the silent-failure shape this chunk exists to catch."""
        self._seed_landed_supersede_frontmatter("state/handoffs/successor.md")
        self.route_mutation.result = {
            "superseded": True,
            "retained": True,
            "retain_reason": "live children found",
        }
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 0)

    def test_route_mutation_error_is_hard_failure(self):
        self._seed_claimed_frontmatter()
        self.route_mutation.exc = _cli.cc_invoke.RouteMutationError("refused", {"exit_code": 1})
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)

    def test_superseded_false_is_not_reported_as_success(self):
        """Regression for the silent-no-op bug (2026-07-27): route_mutation
        only raises on a non-zero op-level exit_code, so an exit_code:0
        result whose `superseded` key is falsy/absent must still be treated
        as a decline, not a success — this is the second defect's fix."""
        self._seed_claimed_frontmatter()
        self.route_mutation.result = {}
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)

    def test_unresolved_repo_root_is_hard_failure(self):
        _cli._resolve_repo_root = lambda path: None
        rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])

    def test_never_claimed_parent_is_refused_dr242(self):
        """DR-242 (docs/decisions/DR-242-successor-named-child-is-not-
        evidence-of-succ.md): a bare, never-claimed-or-shipped parent is
        refused outright — a successor-named child is not evidence of
        succession on its own. self.handoff_path here is the harness's
        untouched empty tmp file, i.e. exactly the bare shape the gate
        exists to catch; no dispatch may reach route_mutation."""
        self.route_mutation.result = {"superseded": True}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = _cli.cmd_supersede(self.handoff_path, "state/handoffs/successor.md", [])
        self.assertEqual(rc, 1)
        self.assertEqual(self.route_mutation.calls, [])
        self.assertIn("DR-242", stderr.getvalue())


class MainArgvTest(_StubHarness):
    def test_chain_argv_dispatch(self):
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.main(["chain", self.handoff_path])
        self.assertEqual(rc, 0)

    def test_supersede_argv_dispatch(self):
        self._seed_landed_supersede_frontmatter("state/handoffs/s.md")
        self.route_mutation.result = {"superseded": True}
        rc = _cli.main(
            ["supersede", self.handoff_path, "--continued-into", "state/handoffs/s.md"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            self.route_mutation.calls[0]["params"]["continued_into"], "state/handoffs/s.md"
        )

    def test_no_subcommand_is_usage_error(self):
        rc = _cli.main([])
        self.assertEqual(rc, 2)

    def test_chain_argv_sha_and_force_wired_through(self):
        """C3/AC5: drives argv parsing (not just cmd_chain's signature) to
        confirm --sha/--force actually wire from the CLI through to the
        dispatched params — the argparse layer is untested by the
        cmd_chain-direct passthrough tests above."""
        _cli._guard_exit_code = lambda path, exclude: 1
        self.route_mutation.result = {"moved": True}
        rc = _cli.main(["chain", self.handoff_path, "--sha", "cafebabe", "--force"])
        self.assertEqual(rc, 0)
        params = self.route_mutation.calls[0]["params"]
        self.assertEqual(params["sha"], "cafebabe")
        self.assertIs(params["force"], True)


class ClosedTargetSupersedeChokePointTest(unittest.TestCase):
    """AC4 (docs/plans/2026-08-13-closed-baton-is-terminal-d6-declines-per-
    predecessor.md, chunk C2): exercises the REAL op handler
    (`coordinator_core.ops.handoff_archive_transition._handler`), not the
    CLI-stub harness above — `_StubHarness` monkeypatches
    `cc_invoke.route_mutation`, which is precisely the seam that stands in
    for the op under test everywhere else in this file, so it cannot assert
    anything about the op's own choke point. This class builds a minimal
    on-disk worktree (`.git` marker + `state/handoffs/`) and calls `_handler`
    directly instead."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        (self.worktree / "state" / "handoffs").mkdir(parents=True)
        self.handoff_path = self.worktree / "state" / "handoffs" / "closed-target.md"

    def _run(self, coro):
        return asyncio.run(coro)

    def _seed_closed_frontmatter(self, closed_reason: str):
        self.handoff_path.write_text(
            "---\n"
            "status: claimed\n"
            "claimed_at: 2026-07-20T10:00:00Z\n"
            "claimed_by: test-session\n"
            "deployment_state: closed\n"
            f"closed_reason: {closed_reason}\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

    def test_supersede_against_closed_target_refuses(self):
        """AC4: mode='supersede' against a `deployment_state: closed` target
        returns `superseded: False` with the closed-target refusal, and the
        target file is byte-unchanged."""
        from coordinator_core.ops.handoff_archive_transition import _handler

        self._seed_closed_frontmatter("displaced")
        before = self.handoff_path.read_text(encoding="utf-8")

        result = self._run(
            _handler(
                {
                    "handoff_path": str(self.handoff_path),
                    "mode": "supersede",
                    "continued_into": "state/handoffs/successor.md",
                },
                self.worktree / ".git",
            )
        )

        self.assertIs(result.get("superseded"), False)
        self.assertIs(result.get("retained"), False)
        self.assertEqual(result.get("exit_code"), 1)
        error = result.get("error") or ""
        self.assertIn("closed", error)
        self.assertIn("displaced", error)
        after = self.handoff_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


class RoadmapBatonGateOrderingTest(unittest.TestCase):
    """Pins the intentional gate ORDER inside the `if mode == "supersede":`
    block when a target is simultaneously `kind: roadmap-baton` AND
    `deployment_state: closed`: the roadmap-baton `blocked_by` gate (module
    docstring § roadmap-baton blocked_by gate) runs BEFORE the closed-baton
    gate (§ closed-baton-is-terminal gate) and wins unconditionally, per
    DR-126 — the roadmap-baton refusal fires on `kind` alone, with no
    dependents condition, so it is unconditional relative to
    `deployment_state` and the closed-baton gate never gets evaluated for
    this target. Until now this ordering was intent-only (asserted in prose
    in both gates' comments, never in a test); this pins it so a future
    reorder of the two gates is caught, not silently accepted — same defect
    class (unstated precedence between two gates) closed repeatedly in
    `baton_assemble` this session."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.worktree = Path(self._tmp.name).resolve()
        (self.worktree / ".git").mkdir()
        (self.worktree / "state" / "handoffs").mkdir(parents=True)
        self.handoff_path = self.worktree / "state" / "handoffs" / "roadmap-baton-closed.md"
        self.handoff_path.write_text(
            "---\n"
            "status: claimed\n"
            "claimed_at: 2026-07-20T10:00:00Z\n"
            "claimed_by: test-session\n"
            "kind: roadmap-baton\n"
            "deployment_state: closed\n"
            "closed_reason: displaced\n"
            "---\n"
            "body\n",
            encoding="utf-8",
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_roadmap_baton_gate_wins_over_closed_baton_gate(self):
        from coordinator_core.ops.handoff_archive_transition import _handler

        before = self.handoff_path.read_text(encoding="utf-8")

        result = self._run(
            _handler(
                {
                    "handoff_path": str(self.handoff_path),
                    "mode": "supersede",
                    "continued_into": "state/handoffs/successor.md",
                },
                self.worktree / ".git",
            )
        )

        self.assertIs(result.get("superseded"), False)
        self.assertEqual(result.get("exit_code"), 1)
        error = result.get("error") or ""
        # The roadmap-baton gate's message, not the closed-baton gate's.
        self.assertIn("roadmap-baton", error)
        self.assertNotIn("closed_reason", error)
        after = self.handoff_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

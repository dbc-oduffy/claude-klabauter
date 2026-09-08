"""test_coordinator_doc_new_sizing_reverse_edge.py -- coverage for the
plan->sizing reverse-edge writer in `coordinator-doc-new --type plan
--sizing-object <path>` (C4, docs/plans/2026-08-10-sizing-objects-join-the-
deliverable-spine.md § C4, AC7).

Purpose: the plan skill's own doctrine (coordinator/skills/plan/SKILL.md §
Exit) claims `--sizing-object` "also writes the reverse edge back onto the
cited sizing -- the plan FK plus the status flip, in the same transaction".
C4 (disposition_ref 2c4a022e8) implements that writer. This suite pins:

1. Happy path: a resolving `--sizing-object` lands `plan:` (repo-relative,
   POSIX-normalized) and `status: routed` on the cited sizing record, in the
   SAME invocation that writes the plan file (AC7's write-order +
   revert-on-failure mechanism, exercised end to end).
2. A sizing already carrying a DIFFERENT non-null `plan:` value is a
   re-route, not a first routing -- the CLI fails loud (exit 1, names the
   existing plan) and clobbers NEITHER the sizing's `plan:` value NOR writes
   the new plan file (no half-written pair).
3. A sizing carrying `plan:` byte-identical to the path this invocation
   would write is idempotent, not a clobber -- re-running the same scaffold
   against the same sizing must not fail loud.

Negative-spec: does NOT re-cover the pre-existing dangling-`--sizing-object`
or mutually-exclusive-flags cases -- those are
test_coordinator_doc_new_sizing_object_gate.py's surface and are unaffected
by this chunk's addition.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_sizing_object_gate.py.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sizing_reverse_edge.py -v
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

import yaml

#: A schema-VALID minimal sizing record. `sizing-object.schema.json` is
#: `additionalProperties: false` with nine required keys, so the old
#: `id: example` fixture could never survive the reverse edge's own
#: post-mutation validation: `id` is not a property of the schema at all,
#: and eight of the nine required keys were absent. The reverse edge writes
#: `plan` and `status`; every other key here exists only to be valid.
_MINIMAL_SIZING_KEYS = (
    "schema: sizing-object\n"
    "intent: example\n"
    "estimate:\n"
    "  tshirt: S\n"
    "  provisional: true\n"
    "route: dispatch\n"
    "detents: []\n"
    "fork: null\n"
    "xl_exit: null\n"
    "premise:\n"
    "  provenance: not-applicable\n"
    "  evidence: example\n"
)


def _sizing_yaml(status: str, plan: str) -> str:
    """A valid sizing record at `status`, pointing at `plan`."""
    return _MINIMAL_SIZING_KEYS + "status: %s\nplan: %s\n" % (status, plan)


from coordinator_core.win_portability import no_console_creationflags

import pytest

# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_reverse_edge_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sizing_reverse_edge_test", loader
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
        # Must satisfy the sizing schema's own `plan:` pattern (^docs/plans/.+\.md$):
        # the reverse edge writes this path INTO the sizing object and then validates
        # it, so a path outside docs/plans/ fails the post-mutation schema check
        # rather than exercising the branch under test.
        out_path = repo / "docs" / "plans" / "2026-08-10-reverse-edge-fixture.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        yield repo, out_path


def _run_cli(
    repo: Path, out_path: Path, title: str, sizing_rel_path: str,
    *extra_args: str,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, str(_CLI_PATH), "--type", "plan",
            "--title", title,
            "--sizing-object", sizing_rel_path,
            "--out", str(out_path),
            *extra_args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        **_NO_CONSOLE,
    )


class MutateSizingReverseEdgeHelperTest(unittest.TestCase):
    """Unit coverage of the pure mutation helper, independent of the CLI/locked_rmw path."""

    def test_first_routing_sets_plan_and_status(self):
        old_text = _sizing_yaml("sized", "null")
        new_text = _cli._mutate_sizing_reverse_edge(old_text, "docs/plans/2026-08-10-example.md")
        self.assertIn("plan: \"docs/plans/2026-08-10-example.md\"", new_text)
        self.assertIn("status: routed", new_text)

    def test_reroute_to_different_plan_raises_mutate_abort(self):
        from coordinator_core.locked_write import MutateAbort

        old_text = _sizing_yaml("routed", '"docs/plans/2026-08-01-other.md"')
        with self.assertRaises(MutateAbort) as ctx:
            _cli._mutate_sizing_reverse_edge(old_text, "docs/plans/2026-08-10-example.md")
        self.assertIn("docs/plans/2026-08-01-other.md", str(ctx.exception))

    def test_idempotent_rerun_same_plan_does_not_raise(self):
        old_text = _sizing_yaml("routed", '"docs/plans/2026-08-10-example.md"')
        new_text = _cli._mutate_sizing_reverse_edge(old_text, "docs/plans/2026-08-10-example.md")
        self.assertIn("plan: \"docs/plans/2026-08-10-example.md\"", new_text)
        self.assertIn("status: routed", new_text)

    def test_declined_sizing_raises_mutate_abort(self):
        """Regression for the mirror-drift hole: `declined` is terminal too
        (docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md §
        C2), and a re-route against a declined sizing must refuse exactly
        like an already-shipped one, not silently resurrect it as `routed`.
        """
        from coordinator_core.locked_write import MutateAbort

        old_text = _sizing_yaml("declined", "null")
        with self.assertRaises(MutateAbort) as ctx:
            _cli._mutate_sizing_reverse_edge(old_text, "docs/plans/2026-08-10-example.md")
        self.assertIn("declined", str(ctx.exception))

    def _write_existing_plan(self, repo: Path, deliverable_id: str | None) -> str:
        """Write a minimal plan file with (or without) `deliverable_id:` and
        return its repo-relative POSIX path, matching what
        `_existing_plan_value` on a sizing record names."""
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        existing_plan = plans_dir / "2026-08-01-other.md"
        body = "---\ntitle: Other plan\n"
        if deliverable_id is not None:
            body += f'deliverable_id: "{deliverable_id}"\n'
        body += "---\n\n# Other plan\n"
        existing_plan.write_text(body)
        return "docs/plans/2026-08-01-other.md"

    def test_fan_out_asserted_is_permitted_and_plan_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            existing_plan_rel = self._write_existing_plan(repo, "dlv-existing-aaaaaa")
            old_text = _sizing_yaml("routed", f'"{existing_plan_rel}"')
            new_text = _cli._mutate_sizing_reverse_edge(
                old_text,
                "docs/plans/2026-08-10-example.md",
                str(repo),
                True,
            )
            # `plan:` is left naming the FIRST plan, unchanged — the fan-out
            # relaxes the refusal, never the clobber guard.
            self.assertIn(f'plan: "{existing_plan_rel}"', new_text)
            self.assertNotIn("docs/plans/2026-08-10-example.md", new_text)
            self.assertIn("status: routed", new_text)

    def test_same_disk_situation_without_fan_out_flag_is_refused_and_writes_nothing(self):
        """The identical on-disk situation as the permitted case above --
        without `fan_out=True` this refuses loudly. Pins that the flag, not
        any inferred property of the disk state, is the discriminator."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            existing_plan_rel = self._write_existing_plan(repo, "dlv-existing-aaaaaa")
            old_text = _sizing_yaml("routed", f'"{existing_plan_rel}"')
            from coordinator_core.locked_write import MutateAbort

            with self.assertRaises(MutateAbort) as ctx:
                _cli._mutate_sizing_reverse_edge(
                    old_text,
                    "docs/plans/2026-08-10-example.md",
                    str(repo),
                    False,
                )
            self.assertIn(existing_plan_rel, str(ctx.exception))
            self.assertIn("--fan-out", str(ctx.exception))

    def test_default_fan_out_false_refuses_regardless_of_existing_plans_id(self):
        """`fan_out` defaults False, and the refusal fires whether or not the
        existing plan's own `deliverable_id` happens to be
        present/absent/malformed on disk -- that field is no longer read by
        this discriminator at all."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            existing_plan_rel = self._write_existing_plan(repo, None)
            old_text = _sizing_yaml("routed", f'"{existing_plan_rel}"')
            from coordinator_core.locked_write import MutateAbort

            with self.assertRaises(MutateAbort) as ctx:
                _cli._mutate_sizing_reverse_edge(
                    old_text,
                    "docs/plans/2026-08-10-example.md",
                    str(repo),
                )
            self.assertIn(existing_plan_rel, str(ctx.exception))


class SizingTerminalStatusMirrorSyncTest(unittest.TestCase):
    """Pins the CLI's local terminal-status mirror to the engine's own set.

    Purpose: `coordinator-doc-new::_SIZING_TERMINAL_STATUSES` is a
    necessary local copy (importing `coordinator_core.ops.deliverable_cascade`
    directly would trigger that module's import-time `register_op` side
    effect against the JSON-RPC registry) of
    `coordinator_core.ops.deliverable_cascade._SIZING_TERMINAL_STATUS`. A
    value drift between the two reopens exactly the guard hole this suite's
    `test_declined_sizing_raises_mutate_abort` regression-tests -- this pins
    the two sets equal so the next drift fails a test instead of opening a
    silent hole.
    """

    def test_mirror_matches_engine_constant(self):
        from coordinator_core.ops.deliverable_cascade import _SIZING_TERMINAL_STATUS

        self.assertEqual(_cli._SIZING_TERMINAL_STATUSES, _SIZING_TERMINAL_STATUS)


class FullCliReverseEdgeHappyPathTest(unittest.TestCase):
    """AC7 happy path: the reverse edge lands in the same CLI invocation that writes the plan."""

    def test_reverse_edge_lands_on_cited_sizing(self):
        with _tmp_git_repo() as (repo, out_path):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            sizing_file.write_text(_sizing_yaml("sized", "null"))
            result = _run_cli(
                repo, out_path, "Reverse edge happy path plan",
                "state/sizings/2026-08-10-example.yaml",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())

            sizing_after = yaml.safe_load(sizing_file.read_text())
            self.assertEqual(sizing_after.get("status"), "routed")
            plan_rel = out_path.relative_to(repo).as_posix()
            self.assertEqual(sizing_after.get("plan"), plan_rel)


class FullCliReverseEdgeClobberGuardTest(unittest.TestCase):
    """A sizing already routed to a DIFFERENT plan fails loud rather than being overwritten."""

    def test_already_routed_sizing_fails_loud_and_writes_nothing(self):
        with _tmp_git_repo() as (repo, out_path):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            original_text = _sizing_yaml("routed", '"docs/plans/2026-08-01-existing-owner.md"')
            sizing_file.write_text(original_text)

            result = _run_cli(
                repo, out_path, "Reverse edge clobber guard plan",
                "state/sizings/2026-08-10-example.yaml",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("docs/plans/2026-08-01-existing-owner.md", result.stderr)
            self.assertFalse(out_path.exists())
            self.assertEqual(sizing_file.read_text(), original_text)


class FullCliReverseEdgeFanOutTest(unittest.TestCase):
    """A second plan citing one sizing object, run with an asserted
    `--fan-out`, is permitted -- the sizing's `plan:` still names the FIRST
    plan afterwards -- covers the call-site wiring of `args.fan_out` into
    `_write_sizing_reverse_edge`, not just the helper in isolation. The
    IDENTICAL invocation without `--fan-out` refuses loudly and writes
    nothing."""

    def test_second_plan_with_fan_out_asserted_is_permitted_and_first_plan_kept(self):
        with _tmp_git_repo() as (repo, _unused_out):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            sizing_file.write_text(_sizing_yaml("sized", "null"))
            sizing_rel = "state/sizings/2026-08-10-example.yaml"

            (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
            first_out = repo / "docs" / "plans" / "2026-08-10-first-baton.md"
            second_out = repo / "docs" / "plans" / "2026-08-10-second-baton.md"

            first_result = _run_cli(repo, first_out, "First baton plan", sizing_rel)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertTrue(first_out.exists())

            sizing_after_first = yaml.safe_load(sizing_file.read_text())
            first_plan_rel = first_out.relative_to(repo).as_posix()
            self.assertEqual(sizing_after_first.get("plan"), first_plan_rel)
            self.assertEqual(sizing_after_first.get("status"), "routed")

            second_result = _run_cli(
                repo, second_out, "Second baton plan, a distinct deliverable", sizing_rel,
                "--fan-out",
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertTrue(second_out.exists())

            sizing_after_second = yaml.safe_load(sizing_file.read_text())
            # `plan:` is untouched by the fan-out -- still the FIRST plan.
            self.assertEqual(sizing_after_second.get("plan"), first_plan_rel)
            self.assertEqual(sizing_after_second.get("status"), "routed")

    def test_second_plan_without_fan_out_flag_refuses_and_writes_nothing(self):
        """The identical second citation, WITHOUT `--fan-out`, must refuse
        loudly rather than silently permitting a re-route."""
        with _tmp_git_repo() as (repo, _unused_out):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            sizing_file.write_text(_sizing_yaml("sized", "null"))
            sizing_rel = "state/sizings/2026-08-10-example.yaml"

            (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
            first_out = repo / "docs" / "plans" / "2026-08-10-first-baton.md"
            second_out = repo / "docs" / "plans" / "2026-08-10-second-baton.md"

            first_result = _run_cli(repo, first_out, "First baton plan", sizing_rel)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)

            sizing_after_first = yaml.safe_load(sizing_file.read_text())
            first_plan_rel = first_out.relative_to(repo).as_posix()

            second_result = _run_cli(
                repo, second_out, "Second baton plan, no flag asserted", sizing_rel,
            )
            self.assertNotEqual(second_result.returncode, 0)
            self.assertIn("--fan-out", second_result.stderr)
            self.assertFalse(second_out.exists())

            sizing_after_second = yaml.safe_load(sizing_file.read_text())
            self.assertEqual(sizing_after_second.get("plan"), first_plan_rel)


class FullCliReverseEdgeFanOutWithSizingCarriedDeliverableIdTest(unittest.TestCase):
    """Finding 1/3 experiment (review-integrator, coordinatorcode-reviewer.ab257aeb77bab9269):
    the realistic case where the CITED SIZING ITSELF carries a `deliverable_id`
    (the normal case -- a `--type sizing-object` scaffold mints one by
    default), and two plans cite it back-to-back with no
    `--deliverable-id`/`DELIVERABLE_ID` and no session-held roadmap-stub
    claim. `_MINIMAL_SIZING_KEYS`-based fixtures never exercise this: they
    have no `deliverable_id:` key, so both plans mint distinct ids from
    their own titles and never reach the cited-sizing carry tier at all.

    This is the ordinary shape->roadmap fan-out path, absent a roadmap-baton
    session claim (the session-state-parent tier that would otherwise
    resolve a DIFFERENT id ahead of the cited-sizing tier and never reach
    it). Verdict: fan-out vs re-route is asserted via `--fan-out`, never
    inferred by comparing `deliverable_id`s -- an inferred discrimination is
    defeated on this exact scenario, since the ordinary cited-sizing carry
    tier gives every citing plan the SAME verbatim id absent the flag.
    """

    def test_second_plan_citing_a_sizing_with_its_own_deliverable_id_is_permitted_with_fan_out(self):
        with _tmp_git_repo() as (repo, _unused_out):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            # A real sizing carries its own minted `deliverable_id` on disk
            # (`_scaffold_sizing`'s default path) -- not the bare
            # `_MINIMAL_SIZING_KEYS` fixture, which omits the key entirely.
            sizing_file.write_text(
                _MINIMAL_SIZING_KEYS
                + 'deliverable_id: "dlv-sizing-carried-aaaaaa"\n'
                + "status: sized\nplan: null\n"
            )
            sizing_rel = "state/sizings/2026-08-10-example.yaml"

            (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
            first_out = repo / "docs" / "plans" / "2026-08-10-first-baton.md"
            second_out = repo / "docs" / "plans" / "2026-08-10-second-baton.md"

            first_result = _run_cli(repo, first_out, "First baton plan", sizing_rel)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            self.assertTrue(first_out.exists())

            sizing_after_first = yaml.safe_load(sizing_file.read_text())
            first_plan_rel = first_out.relative_to(repo).as_posix()
            self.assertEqual(sizing_after_first.get("plan"), first_plan_rel)
            self.assertEqual(sizing_after_first.get("status"), "routed")
            # First plan carried the sizing-minted id verbatim -- no explicit
            # --deliverable-id, no session-held roadmap-stub claim, and the
            # sizing is not yet routed, so the cited-sizing carry tier fires.
            first_plan_text = first_out.read_text()
            self.assertIn('deliverable_id: "dlv-sizing-carried-aaaaaa"', first_plan_text)

            # Without --fan-out, the second citation refuses -- the sizing
            # already citing a different plan, exactly the re-route shape.
            second_refused = _run_cli(
                repo, second_out, "Second baton plan, no flag asserted", sizing_rel,
            )
            self.assertNotEqual(second_refused.returncode, 0)
            self.assertFalse(second_out.exists())

            # With --fan-out asserted, it proceeds -- and this plan mints
            # its OWN deliverable_id rather than carrying the cited sizing's
            # (the `--fan-out` tier skips the cited-sizing carry entirely),
            # so the two plans' ids do not fork one deliverable into two.
            second_result = _run_cli(
                repo, second_out, "Second baton plan, a distinct deliverable", sizing_rel,
                "--fan-out",
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertTrue(second_out.exists())

            second_plan_text = second_out.read_text()
            self.assertNotIn('deliverable_id: "dlv-sizing-carried-aaaaaa"', second_plan_text)

            sizing_after_second = yaml.safe_load(sizing_file.read_text())
            # `plan:` and `status:` are untouched by the fan-out -- still
            # naming the FIRST plan, same as the no-sizing-id fan-out test.
            self.assertEqual(sizing_after_second.get("plan"), first_plan_rel)
            self.assertEqual(sizing_after_second.get("status"), "routed")


class FullCliReverseEdgeFanOutExplicitDeliverableIdWinsTest(unittest.TestCase):
    """An explicit `--deliverable-id` still wins over both the cited-sizing
    carry AND the `--fan-out` mint-own-id behaviour -- it is checked ahead
    of the cited-sizing carry tier in `main()` regardless of `--fan-out`."""

    def test_explicit_deliverable_id_is_carried_verbatim_under_fan_out(self):
        with _tmp_git_repo() as (repo, _unused_out):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-example.yaml"
            sizing_file.write_text(
                _MINIMAL_SIZING_KEYS
                + 'deliverable_id: "dlv-sizing-carried-aaaaaa"\n'
                + "status: sized\nplan: null\n"
            )
            sizing_rel = "state/sizings/2026-08-10-example.yaml"

            (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
            first_out = repo / "docs" / "plans" / "2026-08-10-first-baton.md"
            second_out = repo / "docs" / "plans" / "2026-08-10-second-baton.md"

            first_result = _run_cli(repo, first_out, "First baton plan", sizing_rel)
            self.assertEqual(first_result.returncode, 0, first_result.stderr)

            second_result = _run_cli(
                repo, second_out, "Second baton plan, explicit id", sizing_rel,
                "--fan-out", "--deliverable-id", "dlv-explicit-cccccc",
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertTrue(second_out.exists())
            second_plan_text = second_out.read_text()
            self.assertIn('deliverable_id: "dlv-explicit-cccccc"', second_plan_text)


if __name__ == "__main__":
    unittest.main()


class SizingReverseEdgeIsClaimedByTheInvokingSessionTest(unittest.TestCase):
    """`state/bug-backlog/2026-08-20-safe-commit-offer-silently-drops-cli-wri-83abe919148c.yaml`.

    The reverse edge is a SECOND file this CLI writes, outside the
    Edit/Write hot path that fires `hooks.track_touched_files`. Undeclared,
    it carried no `touched.txt` claim at all, so
    `session.scope.compute_scope` saw it only through the Step-2 mtime
    fallback, routed it to `mtime_only`, and Step 4(c) withheld it from
    `my_scope` -- `safe-commit-offer` then reported "nothing to commit" over
    a file this invocation had just dirtied, and the next peer's blanket
    commit swept it.

    Pins BOTH writes of the transaction into the DR-276 declaration, not
    just the plan file: an assertion on the plan file alone passed
    throughout the defect's life.
    """

    def _touched_lines(self, repo: Path, sid: str) -> list[str]:
        touched = repo / ".git" / "coordinator-sessions" / sid / "touched.txt"
        if not touched.is_file():
            return []
        return [ln for ln in touched.read_text(encoding="utf-8").splitlines() if ln]

    def _scaffold_sizing(self, repo: Path, sizing_rel: str) -> None:
        """Produce the cited sizing through the CLI's own `--type
        sizing-object` emitter rather than a hand-written stub.

        The reverse-edge writer schema-validates its own post-mutation text,
        so a minimal hand-written record fails on missing required fields for
        a reason unrelated to what this test asserts — and would need editing
        again on every schema addition. Scaffolding it keeps the fixture
        valid by construction.
        """
        result = subprocess.run(
            [
                sys.executable, str(_CLI_PATH), "--type", "sizing-object",
                "--title", "Reverse edge claim sizing",
                "--out", str(repo / sizing_rel),
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
            **_NO_CONSOLE,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_both_the_plan_and_the_cited_sizing_are_claimed(self):
        sid = "reverse-edge-claim-test"
        with _tmp_git_repo() as (repo, _unused_out):
            sizing_rel = "state/sizings/2026-08-10-example.yaml"
            (repo / "state" / "sizings").mkdir(parents=True)
            self._scaffold_sizing(repo, sizing_rel)

            # The schema pins `plan:` to `^docs/plans/.+\.md$`, so the plan
            # this routes to must live there — `_tmp_git_repo`'s bare
            # `custom-out.md` fails validation before the declaration under
            # test is ever reached.
            out_path = repo / "docs" / "plans" / "2026-08-10-reverse-edge-claim.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)

            env = dict(os.environ)
            env["COORDINATOR_SESSION_ID"] = sid
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Reverse edge claim plan",
                    "--sizing-object", sizing_rel,
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            claimed = " ".join(self._touched_lines(repo, sid))
            plan_rel = out_path.relative_to(repo).as_posix()
            self.assertIn(
                plan_rel, claimed,
                "fixture precondition: the plan file's own claim (pre-existing "
                "DR-276 adoption) must still be recorded",
            )
            self.assertIn(
                sizing_rel, claimed,
                "the cited sizing was mutated by this invocation and must carry "
                "this session's claim -- otherwise safe-commit-offer reports "
                "'nothing to commit' over it while git status shows it dirty",
            )

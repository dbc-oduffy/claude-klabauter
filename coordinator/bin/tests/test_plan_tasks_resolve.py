"""test_plan_tasks_resolve.py — CLI-layer proving tests for
`coordinator/bin/plan-tasks-resolve`, the verb-shaped trampoline over
`plan.tasks.mutate`'s `resolve` verb (C5,
docs/plans/2026-08-05-make-the-five-exit-plan-tasks-resolver-r.md).

Scope: this suite exercises ONLY the trampoline's own CLI-layer behavior —
argument parsing, params-dict construction per exit flag, `--disposition-
detail` requirement enforcement, and the `--moved-to` add-task/resolve
orchestration (including its "record the id add-task actually wrote, never
the caller's proposed id" defensive-forward behavior). It does NOT exercise
`plan.tasks.mutate`'s own domain logic (pm_approved gating, D5 ordering,
disposition_ref shape validation, ...) — that is
`coordinator_core/ops/tests/test_plan_tasks_mutate.py`'s job. Every test here
monkeypatches `cc_invoke.route_mutation` (and, for --moved-to,
`_read_source_row`) rather than invoking the real op, so no CLAUDE_KLABAUTER_ROOT
resolution or coordinator_core.invoke subprocess spawn ever happens in this
suite.

Loader pattern mirrors coordinator/bin/tests/test_archive_stamp_cli_
subcommand_help.py: `importlib.machinery.SourceFileLoader` against the bare
extensionless entrypoint (no `.py` file to import by dotted name).

Run with: python3 -m pytest coordinator/bin/tests/test_plan_tasks_resolve.py
"""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import io
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "plan_tasks_resolve_test_subject", str(_BIN_DIR / "plan-tasks-resolve")
    )
    spec = importlib.util.spec_from_loader("plan_tasks_resolve_test_subject", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class TestHelp(unittest.TestCase):
    def test_help_lists_five_exits_and_marks_pm_gated_ones(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                _cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = buf.getvalue()
        for flag in ("--coded", "--spun-off", "--moved-to", "--backlogged", "--wont-do"):
            self.assertIn(flag, out)
        # The two PM-gated exits must be marked as needing a PM word.
        self.assertIn("PM WORD REQUIRED", out)
        # coded/spun_off/moved-to must NOT be marked as PM-gated.
        self.assertEqual(out.count("PM WORD REQUIRED"), 2)


class TestExitDispatch(unittest.TestCase):
    def _capture_route_mutation(self, result=None):
        calls = []

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            calls.append((op, dict(params), repo_root))
            return result if result is not None else {"applied": True, "message": "ok"}

        return calls, fake_route_mutation

    def test_coded_dispatches_disposition_coded_with_sha_as_ref(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1", "--plan", "docs/plans/foo.md",
                        "--coded", "a1b2c3d",
                        "--disposition-detail", "shipped the thing",
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        op, params, repo_root = calls[0]
        self.assertEqual(op, "plan.tasks.mutate")
        self.assertEqual(params["verb"], "resolve")
        self.assertEqual(params["id"], "C1")
        self.assertEqual(params["disposition"], "coded")
        self.assertEqual(params["disposition_ref"], "a1b2c3d")
        self.assertEqual(params["disposition_detail"], "shipped the thing")

    def test_coded_without_detail_is_refused_locally(self):
        """`coded` needs a detail too — the shared disposition-shape check
        refuses ANY non-open row with an empty detail, so a CLI that let
        --coded through only bought the EM a server round-trip to find out.
        Regression for the --help text that claimed detail was optional here."""
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with self.assertRaises(SystemExit):
                    _cli.main(
                        ["--id", "C1", "--plan", "docs/plans/foo.md", "--coded", "a1b2c3d"]
                    )
        self.assertEqual(calls, [], "refusal must be local — nothing dispatched")

    def test_spun_off_requires_disposition_detail(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            with self.assertRaises(SystemExit) as ctx:
                _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--spun-off", "docs/spinoffs/x.md",
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--disposition-detail is required", buf.getvalue())

    def test_spun_off_dispatches_disposition_spun_off(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--spun-off", "docs/spinoffs/x.md",
                        "--disposition-detail", "split out",
                    ]
                )
        self.assertEqual(rc, 0)
        _, params, _ = calls[0]
        self.assertEqual(params["disposition"], "spun_off")
        self.assertEqual(params["disposition_ref"], "docs/spinoffs/x.md")
        self.assertEqual(params["disposition_detail"], "split out")

    def test_backlogged_requires_disposition_detail(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                _cli.main(["--id", "C1", "--plan", "docs/plans/foo.md", "--backlogged"])
        self.assertEqual(ctx.exception.code, 2)

    def test_backlogged_requires_case_against(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(buf):
                _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--backlogged",
                        "--disposition-detail", "PM: defer to next quarter",
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--case-against is required", buf.getvalue())

    def test_backlogged_whitespace_only_case_against_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--backlogged",
                        "--disposition-detail", "PM: defer to next quarter",
                        "--case-against", "   ",
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)

    def test_backlogged_dispatches_disposition_backlogged(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--backlogged",
                        "--disposition-detail", "PM: defer to next quarter",
                        "--case-against", "nothing blocks on it, but it fixes a real papercut",
                    ]
                )
        self.assertEqual(rc, 0)
        _, params, _ = calls[0]
        self.assertEqual(params["disposition"], "backlogged")
        self.assertNotIn("disposition_ref", params)
        self.assertEqual(params["disposition_detail"], "PM: defer to next quarter")
        self.assertEqual(
            params["case_against"], "nothing blocks on it, but it fixes a real papercut"
        )

    def test_wont_do_requires_disposition_detail(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                _cli.main(["--id", "C1", "--plan", "docs/plans/foo.md", "--wont-do"])
        self.assertEqual(ctx.exception.code, 2)

    def test_wont_do_requires_case_against(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(buf):
                _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--wont-do",
                        "--disposition-detail", "PM: superseded",
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--case-against is required", buf.getvalue())

    def test_coded_does_not_require_case_against(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--coded", "a1b2c3d",
                        "--disposition-detail", "shipped",
                    ]
                )
        self.assertEqual(rc, 0)
        _, params, _ = calls[0]
        self.assertNotIn("case_against", params)

    def test_spun_off_does_not_require_case_against(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--spun-off", "docs/spinoffs/foo.md",
                        "--disposition-detail", "split out",
                    ]
                )
        self.assertEqual(rc, 0)
        _, params, _ = calls[0]
        self.assertNotIn("case_against", params)

    def test_wont_do_dispatches_disposition_wont_do(self):
        calls, fake = self._capture_route_mutation()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--wont-do",
                        "--disposition-detail", "PM: superseded",
                        "--case-against", "closest thing to a safety net if bar regresses",
                    ]
                )
        self.assertEqual(rc, 0)
        _, params, _ = calls[0]
        self.assertEqual(params["disposition"], "wont_do")
        self.assertNotIn("disposition_ref", params)


class TestMultiIdCoded(unittest.TestCase):
    def test_multi_id_coded_dispatches_one_batch_call_with_resolves(self):
        calls, fake = self._make_capture()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1", "--id", "C2", "--id", "C3",
                        "--plan", "docs/plans/foo.md",
                        "--coded", "a1b2c3d",
                        "--disposition-detail", "shipped the whole batch",
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1, "must be one atomic op call, not N")
        op, params, repo_root = calls[0]
        self.assertEqual(op, "plan.tasks.mutate")
        self.assertEqual(params["verb"], "resolve")
        self.assertNotIn("id", params)
        resolves = params["resolves"]
        self.assertEqual([r["id"] for r in resolves], ["C1", "C2", "C3"])
        for r in resolves:
            self.assertEqual(r["disposition"], "coded")
            self.assertEqual(r["disposition_ref"], "a1b2c3d")
            self.assertEqual(r["disposition_detail"], "shipped the whole batch")

    def test_multi_id_backlogged_is_refused_locally_and_writes_nothing(self):
        calls, fake = self._make_capture()
        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main(
                            [
                                "--id", "C1", "--id", "C2",
                                "--plan", "docs/plans/foo.md",
                                "--backlogged",
                                "--disposition-detail", "PM: defer",
                                "--case-against", "fixes a real papercut",
                            ]
                        )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(calls, [], "refusal must be local — nothing dispatched")
        self.assertIn("repeatable --id is only offered for --coded", buf.getvalue())
        self.assertIn("'backlogged'", buf.getvalue())
        self.assertIn("Make one call per row instead", buf.getvalue())

    def test_multi_id_wont_do_is_refused_locally(self):
        calls, fake = self._make_capture()
        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main(
                            [
                                "--id", "C1", "--id", "C2",
                                "--plan", "docs/plans/foo.md",
                                "--wont-do",
                                "--disposition-detail", "PM: superseded",
                                "--case-against", "safety net",
                            ]
                        )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(calls, [])
        self.assertIn("repeatable --id is only offered for --coded", buf.getvalue())

    def test_multi_id_spun_off_is_refused_locally(self):
        calls, fake = self._make_capture()
        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main(
                            [
                                "--id", "C1", "--id", "C2",
                                "--plan", "docs/plans/foo.md",
                                "--spun-off", "docs/spinoffs/x.md",
                                "--disposition-detail", "split out",
                            ]
                        )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(calls, [])
        self.assertIn("repeatable --id is only offered for --coded", buf.getvalue())

    def test_multi_id_moved_to_is_refused_locally(self):
        calls, fake = self._make_capture()
        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with redirect_stderr(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main(
                            [
                                "--id", "C1", "--id", "C2",
                                "--plan", "docs/plans/foo.md",
                                "--moved-to", "docs/plans/bar.md",
                                "--disposition-detail", "folded into bar",
                            ]
                        )
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(calls, [])
        self.assertIn("repeatable --id is only offered for --coded", buf.getvalue())

    def test_single_id_coded_behaviour_unchanged(self):
        calls, fake = self._make_capture()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                rc = _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--coded", "a1b2c3d",
                        "--disposition-detail", "shipped the thing",
                    ]
                )
        self.assertEqual(rc, 0)
        op, params, _ = calls[0]
        self.assertEqual(params["verb"], "resolve")
        self.assertEqual(params["id"], "C1")
        self.assertNotIn("resolves", params)

    @staticmethod
    def _make_capture(result=None):
        calls = []

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            calls.append((op, dict(params), repo_root))
            return result if result is not None else {"applied": True, "message": "ok"}

        return calls, fake_route_mutation


class TestPmGatedRefusalMessage(unittest.TestCase):
    def test_pm_gated_refusal_surfaces_op_message_and_nonzero_exit(self):
        refusal_text = (
            "resolve: disposition 'backlogged' for task 'C1' is a scope decision "
            "and needs PM ratification before it can be recorded (D3/D4). Ask the "
            "PM to ratify this disposition. Which work gets closed out is the "
            "PM's call, not the authoring agent's — there is deliberately no "
            "command that satisfies this from inside the session."
        )

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            raise _cli.cc_invoke.RouteMutationError(
                f"route_mutation: op={op!r} refused (exit_code=1, error={refusal_text!r})",
                {"exit_code": 1, "applied": False, "error": refusal_text},
            )

        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake_route_mutation):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with redirect_stderr(buf):
                    rc = _cli.main(
                        [
                            "--id", "C1",
                            "--plan", "docs/plans/foo.md",
                            "--backlogged",
                            "--disposition-detail", "PM: defer",
                            "--case-against", "nothing blocks on it, but it fixes a real papercut",
                        ]
                    )
        self.assertEqual(rc, 1)
        self.assertIn("Ask the PM to ratify this disposition", buf.getvalue())
        self.assertIn("PM's call, not the authoring agent's", buf.getvalue())


class TestMovedTo(unittest.TestCase):
    _SOURCE_ROW = {
        "id": "C1",
        "title": "example row",
        "change_kind": "code-edit",
        "surface": "foo.py",
        "queue_scope": "project",
        "deferred": False,
        "body": "do the thing",
    }

    def test_moved_to_add_tasks_then_resolves_spun_off_to_target_plan(self):
        calls = []

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            calls.append((op, dict(params), repo_root))
            if params["verb"] == "add-task":
                return {"applied": True, "message": "add-task: added task 'C1'"}
            return {"applied": True, "message": "resolve: 'C1' resolved to 'spun_off'"}

        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake_route_mutation):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with unittest.mock.patch.object(
                    _cli, "_read_source_row", lambda plan, tid, root: dict(self._SOURCE_ROW)
                ):
                    rc = _cli.main(
                        [
                            "--id", "C1",
                            "--plan", "docs/plans/foo.md",
                            "--moved-to", "docs/plans/bar.md",
                            "--disposition-detail", "folded into bar",
                        ]
                    )

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 2)

        add_op, add_params, _ = calls[0]
        self.assertEqual(add_op, "plan.tasks.mutate")
        self.assertEqual(add_params["verb"], "add-task")
        self.assertEqual(add_params["plan_path"], "docs/plans/bar.md")
        # The moved task must not carry the reserved disposition fields onto
        # the target plan — it starts OPEN there.
        for reserved in ("disposition", "disposition_ref", "disposition_detail"):
            self.assertNotIn(reserved, add_params["task"])
        self.assertEqual(add_params["task"]["id"], "C1")

        resolve_op, resolve_params, _ = calls[1]
        self.assertEqual(resolve_params["verb"], "resolve")
        self.assertEqual(resolve_params["plan_path"], "docs/plans/foo.md")
        self.assertEqual(resolve_params["id"], "C1")
        self.assertEqual(resolve_params["disposition"], "spun_off")
        # disposition_ref is the TARGET PLAN's own path — a single
        # repo-relative path, matching every other spun_off ref's shape.
        self.assertEqual(resolve_params["disposition_ref"], "docs/plans/bar.md")
        # Written id matched the proposed id — detail passes through verbatim.
        self.assertEqual(resolve_params["disposition_detail"], "folded into bar")

    def test_moved_to_records_the_written_id_not_the_proposed_one(self):
        """Defends the example-doctrine-repo ruling's explicit instruction: `resolve` must
        record the id add-task ACTUALLY wrote, never the caller's proposed
        id, because add-task may legitimately mint a non-colliding one.
        """
        calls = []

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            calls.append((op, dict(params), repo_root))
            if params["verb"] == "add-task":
                # Simulate add-task minting a DIFFERENT id than proposed.
                return {"applied": True, "message": "add-task: added task 'C1-2'"}
            return {"applied": True, "message": "resolve: 'C1' resolved to 'spun_off'"}

        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake_route_mutation):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with unittest.mock.patch.object(
                    _cli, "_read_source_row", lambda plan, tid, root: dict(self._SOURCE_ROW)
                ):
                    rc = _cli.main(
                        [
                            "--id", "C1",
                            "--plan", "docs/plans/foo.md",
                            "--moved-to", "docs/plans/bar.md",
                            "--disposition-detail", "folded into bar",
                        ]
                    )

        self.assertEqual(rc, 0)
        _, resolve_params, _ = calls[1]
        # The minted id ("C1-2"), NOT the proposed id ("C1"), must be
        # reflected in the recorded outcome.
        self.assertIn("C1-2", resolve_params["disposition_detail"])
        self.assertIn("folded into bar", resolve_params["disposition_detail"])

    def test_moved_to_resolve_failure_after_add_task_reports_partial_mutation(self):
        """Finding 1 regression: if add-task succeeds but the subsequent
        resolve on the source row raises RouteMutationError, the operator
        must be told the target plan already received the new row — not
        just handed the bare resolve refusal."""
        calls = []

        def fake_route_mutation(op, params, repo_root, legacy_fn):
            calls.append((op, dict(params), repo_root))
            if params["verb"] == "add-task":
                return {"applied": True, "message": "add-task: added task 'C1'"}
            raise _cli.cc_invoke.RouteMutationError(
                "route_mutation: op='plan.tasks.mutate' refused "
                "(exit_code=1, error='D5 ordering violation')",
                {"exit_code": 1, "applied": False, "error": "D5 ordering violation"},
            )

        buf = io.StringIO()
        with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake_route_mutation):
            with unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"):
                with unittest.mock.patch.object(
                    _cli, "_read_source_row", lambda plan, tid, root: dict(self._SOURCE_ROW)
                ):
                    with redirect_stderr(buf):
                        rc = _cli.main(
                            [
                                "--id", "C1",
                                "--plan", "docs/plans/foo.md",
                                "--moved-to", "docs/plans/bar.md",
                                "--disposition-detail", "folded into bar",
                            ]
                        )

        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2, "both add-task and resolve must have been attempted")
        stderr = buf.getvalue()
        # The operator must be told the target plan already received the row.
        self.assertIn("docs/plans/bar.md", stderr)
        self.assertIn("already applied", stderr)
        self.assertIn("'C1'", stderr)
        self.assertIn("D5 ordering violation", stderr)

    def test_moved_to_requires_disposition_detail(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                _cli.main(
                    [
                        "--id", "C1",
                        "--plan", "docs/plans/foo.md",
                        "--moved-to", "docs/plans/bar.md",
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)


class TestParseWrittenIdMatchesRealAddTaskMessage(unittest.TestCase):
    """Finding 3 regression: `_parse_written_id` decodes the literal message
    format `_add_task` (coordinator_core/ops/plan_tasks_mutate.py) actually
    emits, invoked for real rather than via a hand-crafted fake string — so a
    future edit to `_add_task`'s message wording breaks THIS test instead of
    silently degrading `_parse_written_id`'s fallback to "no divergence"."""

    @staticmethod
    def _make_git_repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()

        def _git(*args: str) -> None:
            subprocess.run(["git"] + list(args), cwd=str(repo), capture_output=True, check=True)

        _git("init", "-b", "main")
        _git("config", "user.email", "plan-tasks-resolve-test@claude-klabauter.test")
        _git("config", "user.name", "Plan Tasks Resolve Test")
        _git("config", "commit.gpgsign", "false")
        (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "plans" / ".gitkeep").write_text("", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-m", "chore: initial skeleton")
        return repo

    _PLAN_WITH_TASKS = """\
---
title: "Test Plan"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
```
"""

    def test_add_task_success_message_still_parses_to_the_written_id(self):
        import sys

        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        import coordinator_core.ops.plan_tasks_mutate as plan_tasks_mutate

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = self._make_git_repo(tmp_path)
            plan = repo / "docs" / "plans" / "moved.md"
            plan.write_text(self._PLAN_WITH_TASKS, encoding="utf-8")

            task = {
                "id": "C2",
                "title": "Second chunk",
                "change_kind": "script-edit",
                "surface": "some/other.py",
                "queue_scope": "project",
                "deferred": False,
                "body": "Do the second thing.\n",
            }
            result = asyncio.run(
                plan_tasks_mutate._handler(
                    {"verb": "add-task", "plan_path": str(plan), "task": task},
                    repo_root=repo / ".git",
                )
            )

        self.assertEqual(result["exit_code"], 0, result)
        message = result["message"]
        # Real message format, not a hand-crafted fake — this is the whole
        # point of the regression: it breaks here if `_add_task` ever changes
        # its wording, rather than silently degrading `_parse_written_id`.
        written_id = _cli._parse_written_id(message, "some-other-proposed-id")
        self.assertEqual(
            written_id, "C2",
            f"_parse_written_id failed to extract the real id from _add_task's "
            f"actual message: {message!r}",
        )


if __name__ == "__main__":
    unittest.main()

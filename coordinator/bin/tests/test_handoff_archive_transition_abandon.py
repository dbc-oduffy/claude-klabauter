"""test_handoff_archive_transition_abandon.py — unit tests for the `abandon`
subcommand of `coordinator/bin/handoff-archive-transition.py` (plan
docs/plans/2026-09-23-plan-blocked-state.md chunk C3).

`PredicateTest` (`_is_unworked_placeholder`) is pure and spawn-free — AC9.

The end-to-end classes build a real tmp git repo, because `cmd_abandon`
routes through `_resolve_repo_root`, which spawns a real
`git rev-parse --show-toplevel` (same technique
`test_handoff_archive_transition.py`'s `_StubHarness` avoids for the CHAIN
half by monkeypatching `_resolve_repo_root` directly — this suite keeps it
real so `cmd_abandon`'s own path-containment check runs against a real
worktree root). `cs_close_handoff` writes in-process (no subprocess — see
`coordinator_core.archive_stamp._call_handoff_transition`), while the
CHAIN half's own engine seams (`_guard_exit_code`, `cc_invoke.route_mutation`)
are monkeypatched exactly as `test_handoff_archive_transition.py`'s
`_StubHarness` does — this suite asserts `cmd_abandon`'s own composition
and refusal logic, not the engine behind either call it composes. Marked
cadence + spawns_process for the real git spawn.

Run:
    pytest coordinator/bin/tests/test_handoff_archive_transition_abandon.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _load_module(name: str, filename: str):
    loader = importlib.machinery.SourceFileLoader(name, str(_BIN_DIR / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_module(
    "handoff_archive_transition_abandon_test", "handoff-archive-transition.py"
)
# Triggers the module's own lazy cc_invoke bootstrap (PEP 562 __getattr__),
# which puts coordinator_core on sys.path for every test below.
_cli.cc_invoke.ensure_engine_on_path(_cli.__file__)

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_git_repo(tmp_path: Path) -> None:
    """`git init` + one initial commit, same shape
    `test_plan_status_transition.py::_ensure_git_repo` uses — a resolvable
    HEAD is the realistic fixture, and `_resolve_repo_root` only needs a
    resolvable toplevel, not a tracked handoff file."""
    env = {**os.environ, **_GIT_ENV_KEYS}
    for cmd in (
        ["git", "init"],
        ["git", "config", "commit.gpgsign", "false"],
    ):
        subprocess.run(
            cmd, cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
            **no_console_creationflags(),
        )
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitkeep"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(tmp_path), capture_output=True, env=env, timeout=15,
        **no_console_creationflags(),
    )


def _scaffolded_placeholder_text() -> str:
    """A real, schema-valid unworked placeholder handoff — the actual
    `_scaffold_handoff` output (AC9's own predicate fixture), so
    `cs_close_handoff`'s frontmatter validation passes."""
    doc_new = _load_module("coordinator_doc_new_fixture", "coordinator-doc-new.py")
    return doc_new._scaffold_handoff("Test title", "branch-x")


def _placeholder_text() -> str:
    return _scaffolded_placeholder_text()


class PredicateTest(unittest.TestCase):
    """AC9 — `_is_unworked_placeholder` is pure, no I/O, spawn-free."""

    def test_scaffold_output_with_no_edits_is_a_placeholder(self):
        doc_new = _load_module("coordinator_doc_new_abandon_test", "coordinator-doc-new.py")
        text = doc_new._scaffold_handoff("Test title", "branch-x")
        self.assertTrue(_cli._is_unworked_placeholder(text))

    def test_crlf_copy_of_scaffold_output_is_still_a_placeholder(self):
        doc_new = _load_module("coordinator_doc_new_abandon_crlf_test", "coordinator-doc-new.py")
        text = doc_new._scaffold_handoff("Test title", "branch-x")
        self.assertTrue(_cli._is_unworked_placeholder(text.replace("\n", "\r\n")))

    def test_one_sentence_added_is_no_longer_a_placeholder(self):
        doc_new = _load_module("coordinator_doc_new_abandon_edit_test", "coordinator-doc-new.py")
        text = doc_new._scaffold_handoff("Test title", "branch-x")
        edited = text.replace(
            "<!-- Replace with what was built, fixed, or shipped this session. -->",
            "<!-- Replace with what was built, fixed, or shipped this session. -->\n"
            "Shipped the thing.",
            1,
        )
        self.assertFalse(_cli._is_unworked_placeholder(edited))

    def test_placeholder_fixture_used_by_the_e2e_tests_is_itself_a_placeholder(self):
        self.assertTrue(_cli._is_unworked_placeholder(_placeholder_text()))


class _AbandonHarness(unittest.TestCase):
    """Real tmp git repo + a placeholder handoff under state/handoffs/.
    Monkeypatches only the CHAIN half's engine seams
    (`_guard_exit_code`, `cc_invoke.route_mutation`) — `cs_close_handoff`
    and `_resolve_repo_root` stay real."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name).resolve()
        _init_git_repo(self.repo_root)
        (self.repo_root / "state" / "handoffs").mkdir(parents=True)
        self.handoff_path = self.repo_root / "state" / "handoffs" / "placeholder.md"
        self.handoff_path.write_text(_placeholder_text(), encoding="utf-8")

        self._orig_guard = _cli._guard_exit_code
        self._orig_route_mutation = _cli.cc_invoke.route_mutation
        self.addCleanup(self._restore)
        self.route_calls: list[dict] = []
        self.route_result: dict = {"moved": True}

        def _stub_route_mutation(op, params, repo_root, legacy_fn):
            self.route_calls.append({"op": op, "params": params, "repo_root": repo_root})
            return self.route_result

        _cli.cc_invoke.route_mutation = _stub_route_mutation

    def _restore(self):
        _cli._guard_exit_code = self._orig_guard
        _cli.cc_invoke.route_mutation = self._orig_route_mutation


class AbandonSuccessTest(_AbandonHarness):
    def test_placeholder_baton_closes_then_chains(self):
        """AC7: an all-scaffold, carried_items-empty baton is closed
        (deployment_state: closed, closed_reason: cancelled — via the real,
        in-process `cs_close_handoff`) and then chain-dispatched (guard
        childless -> mode stamp_shipped), all rc 0. AC11: process time is
        recorded, not asserted."""
        _cli._guard_exit_code = lambda path, exclude: 1

        started = time.process_time()
        rc = _cli.cmd_abandon(str(self.handoff_path), "cancelled")
        elapsed_ms = (time.process_time() - started) * 1000
        print(f"abandon process_time: {elapsed_ms:.2f}ms")

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.route_calls), 1)
        self.assertEqual(self.route_calls[0]["params"]["mode"], "stamp_shipped")
        self.assertEqual(self.route_calls[0]["op"], "handoff.archive_transition")

        text = self.handoff_path.read_text(encoding="utf-8")
        self.assertIn("deployment_state: closed", text)
        self.assertIn("closed_reason: cancelled", text)
        self.assertNotIn("continued_into:", text)
        self.assertNotIn("shipped_in:", text)

    def test_live_holder_retains_stays_closed_but_unarchived_rc0(self):
        """AC8: a live claim holder makes `chain` retain (mode stamp_only) —
        that is `chain`'s own retention contract, matching abandon's promise:
        the record stays closed-but-unarchived (terminal) with rc 0."""
        _cli._guard_exit_code = lambda path, exclude: 0  # has-children

        rc = _cli.cmd_abandon(str(self.handoff_path), "displaced")

        self.assertEqual(rc, 0)
        self.assertEqual(self.route_calls[0]["params"]["mode"], "stamp_only")
        text = self.handoff_path.read_text(encoding="utf-8")
        self.assertIn("deployment_state: closed", text)
        self.assertIn("closed_reason: displaced", text)


class AbandonRefusalTest(_AbandonHarness):
    def test_bad_reason_refuses_and_writes_nothing(self):
        before = self.handoff_path.read_text(encoding="utf-8")
        rc = _cli.cmd_abandon(str(self.handoff_path), "nope")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.route_calls, [])
        self.assertEqual(self.handoff_path.read_text(encoding="utf-8"), before)

    def test_missing_reason_argument_refuses(self):
        rc = _cli.cmd_abandon(str(self.handoff_path), "")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.route_calls, [])

    def test_worked_body_refuses_and_names_close_handoff(self):
        """AC8: a body carrying anything beyond HTML comments / headings /
        whitespace refuses, and the message names
        `archive-stamp-cli close-handoff` as the deliberate door for a
        worked baton."""
        worked_text = _placeholder_text().replace(
            "<!-- Replace with what was built, fixed, or shipped this session. -->",
            "<!-- Replace with what was built, fixed, or shipped this session. -->\n"
            "Shipped the actual feature.",
            1,
        )
        self.handoff_path.write_text(worked_text, encoding="utf-8")

        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = _cli.cmd_abandon(str(self.handoff_path), "cancelled")

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.route_calls, [])
        self.assertIn("archive-stamp-cli close-handoff", buf.getvalue())
        self.assertEqual(self.handoff_path.read_text(encoding="utf-8"), worked_text)

    def test_non_empty_carried_items_refuses(self):
        """AC8: a non-empty carried_items refuses — those obligations would
        be dropped."""
        base = _placeholder_text()
        text = base.replace(
            "---\n\n",
            "carried_items:\n"
            "  - carry_id: cid-1\n"
            "    description: unfinished thing\n"
            "    disposition: open\n"
            "---\n\n",
            1,
        )
        # Sanity: the injected key landed INSIDE the frontmatter block (before
        # the closing delimiter), not appended after it.
        self.assertLess(text.index("carried_items:"), text.rindex("---\n"))
        self.handoff_path.write_text(text, encoding="utf-8")

        rc = _cli.cmd_abandon(str(self.handoff_path), "cancelled")

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.route_calls, [])
        self.assertEqual(self.handoff_path.read_text(encoding="utf-8"), text)

    def test_path_outside_state_handoffs_refuses(self):
        outside = self.repo_root / "docs" / "not-a-handoff.md"
        outside.parent.mkdir(parents=True)
        outside.write_text(_placeholder_text(), encoding="utf-8")

        rc = _cli.cmd_abandon(str(outside), "cancelled")

        self.assertNotEqual(rc, 0)
        self.assertEqual(self.route_calls, [])
        self.assertEqual(outside.read_text(encoding="utf-8"), _placeholder_text())


if __name__ == "__main__":
    unittest.main()

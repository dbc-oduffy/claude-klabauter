"""test_coordinator_doc_new_plan_inherits_exit_criterion.py -- coverage for
C3 of docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md:
`_scaffold_plan` inheriting an accepted sizing `exit_criterion` (Design §
Inheritance).

Purpose: the exit criterion moved from plan time to sizing time (target-
design.md § 11). Before this chunk a plan citing `--sizing-object` always
re-authored `prime_exit_criterion` from the `<REPLACE: ...>` placeholder
pair, even when the cited sizing already carried a PM-accepted criterion.
This suite pins the three-way branch:

1. A cited sizing whose `exit_criterion.accepted` is non-null carries
   `statement` verbatim into `prime_exit_criterion.statement`, with
   `derived_from` naming the cited sizing path, and NO `<REPLACE:` marker
   anywhere inside the block.
2. A cited sizing carrying a PROPOSED (`accepted: null`) criterion is
   byte-identical to today's placeholder block, plus exactly one stderr
   line naming the sizing path.
3. A cited sizing with no `exit_criterion` field at all -- the whole
   existing corpus, since there is no backfill -- is byte-identical to
   today's placeholder block with NO stderr output.
4. An unreadable/malformed cited sizing degrades to the placeholder block
   and never raises.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_sizing_object_gate.py.

Spec backlink: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C3, Design § Inheritance

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_inherits_exit_criterion.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_inherits_exit_criterion_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_inherits_exit_criterion_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_ACCEPTED_SIZING_PATH = "state/sizings/accepted.yaml"
_PROPOSED_SIZING_PATH = "state/sizings/proposed.yaml"
_NO_CRITERION_SIZING_PATH = "state/sizings/no-criterion.yaml"
_UNREADABLE_SIZING_PATH = "state/sizings/unreadable.yaml"

_BASE_SIZING = (
    "schema: sizing-object\n"
    'intent: "example intent"\n'
    "estimate:\n"
    "  tshirt: M\n"
    "  provisional: true\n"
    "route: plan\n"
    "detents: []\n"
    "fork: null\n"
    "xl_exit: null\n"
    "status: sized\n"
    "premise:\n"
    "  provenance: executed\n"
    '  evidence: "exercised for the plan-inherits-exit-criterion test"\n'
)


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
def _tmp_repo_with_sizings():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "testrepo"
        repo.mkdir()
        _init_git_repo(repo)
        sizing_dir = repo / "state" / "sizings"
        sizing_dir.mkdir(parents=True)
        (sizing_dir / "accepted.yaml").write_text(
            _BASE_SIZING
            + "exit_criterion:\n"
            + '  statement: "the tree does X"\n'
            + "  accepted:\n"
            + '    pm_quote: "yes, that is right"\n'
            + "    \"on\": '2026-09-27'\n"
            + "    mode: pm\n"
        )
        (sizing_dir / "proposed.yaml").write_text(
            _BASE_SIZING
            + "exit_criterion:\n"
            + '  statement: "a proposed but unaccepted criterion"\n'
            + "  accepted: null\n"
        )
        (sizing_dir / "no-criterion.yaml").write_text(_BASE_SIZING)
        (sizing_dir / "unreadable.yaml").write_text("this: [is, not, : valid yaml")
        yield repo


def _prime_exit_block(content: str) -> str:
    """Extract the prime_exit_criterion block text for assertions.

    The block is every line indented under `prime_exit_criterion:` (including
    the nested, commented `falsifier:` sub-block — F23, klabauter#71) up to
    the first line that starts at column 0 (the next top-level key).
    """
    _, _, rest = content.partition("prime_exit_criterion:\n")
    lines = rest.splitlines(keepends=True)
    block_lines: list[str] = []
    for ln in lines:
        if ln.strip() and not ln[0].isspace():
            break
        block_lines.append(ln)
    return "".join(block_lines)


class ScaffoldPlanInheritsAcceptedCriterionTest(unittest.TestCase):
    def test_accepted_criterion_carried_verbatim(self):
        with _tmp_repo_with_sizings() as repo:
            content = _cli._scaffold_plan(
                title="t",
                branch="b",
                author="test-author",
                sizing_object=_ACCEPTED_SIZING_PATH,
                sizing_repo_root=str(repo),
            )
            block = _prime_exit_block(content)
            self.assertNotIn("<REPLACE:", block)
            fm_text = content.split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(
                fields["prime_exit_criterion"]["statement"], "the tree does X"
            )
            self.assertEqual(
                fields["prime_exit_criterion"]["derived_from"], _ACCEPTED_SIZING_PATH
            )


class ScaffoldPlanProposedCriterionTest(unittest.TestCase):
    def test_proposed_criterion_leaves_placeholder_and_warns(self, capsys=None):
        import io
        import contextlib as _ctx

        with _tmp_repo_with_sizings() as repo:
            stderr_capture = io.StringIO()
            with _ctx.redirect_stderr(stderr_capture):
                content = _cli._scaffold_plan(
                    title="t",
                    branch="b",
                    author="test-author",
                    sizing_object=_PROPOSED_SIZING_PATH,
                    sizing_repo_root=str(repo),
                )
            stderr_text = stderr_capture.getvalue()
            block = _prime_exit_block(content)
            self.assertIn("<REPLACE:", block)
            self.assertIn(_PROPOSED_SIZING_PATH, stderr_text)
            self.assertIn("not accepted", stderr_text)
            self.assertEqual(stderr_text.count("\n"), 1)


class ScaffoldPlanNoCriterionTest(unittest.TestCase):
    def test_no_criterion_is_byte_identical_and_silent(self):
        import io
        import contextlib as _ctx

        with _tmp_repo_with_sizings() as repo:
            baseline = _cli._scaffold_plan(
                title="t", branch="b", author="test-author",
                sizing_object=_NO_CRITERION_SIZING_PATH,
            )
            stderr_capture = io.StringIO()
            with _ctx.redirect_stderr(stderr_capture):
                content = _cli._scaffold_plan(
                    title="t",
                    branch="b",
                    author="test-author",
                    sizing_object=_NO_CRITERION_SIZING_PATH,
                    sizing_repo_root=str(repo),
                )
            self.assertEqual(content, baseline)
            self.assertEqual(stderr_capture.getvalue(), "")


class ScaffoldPlanUnreadableSizingTest(unittest.TestCase):
    def test_unreadable_sizing_degrades_to_placeholder_never_raises(self):
        with _tmp_repo_with_sizings() as repo:
            content = _cli._scaffold_plan(
                title="t",
                branch="b",
                author="test-author",
                sizing_object=_UNREADABLE_SIZING_PATH,
                sizing_repo_root=str(repo),
            )
            block = _prime_exit_block(content)
            self.assertIn("<REPLACE:", block)


class ScaffoldPlanNoSizingRepoRootTest(unittest.TestCase):
    def test_no_sizing_repo_root_reproduces_todays_placeholder(self):
        """Omitting `sizing_repo_root` (the caller declining resolution, e.g.
        --no-sizing-object callers) is a pure no-op on the placeholder block --
        it must never attempt a read."""
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            sizing_object=_ACCEPTED_SIZING_PATH,
        )
        block = _prime_exit_block(content)
        self.assertIn("<REPLACE:", block)


class FullCliPlanInheritsAcceptedCriterionTest(unittest.TestCase):
    """AC: the real CLI surface end-to-end."""

    def test_full_cli_carries_accepted_criterion(self):
        with _tmp_repo_with_sizings() as repo:
            out_path = repo / "docs" / "plans" / "p.md"
            out_path.parent.mkdir(parents=True)
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "t",
                    "--sizing-object", _ACCEPTED_SIZING_PATH,
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(
                fields["prime_exit_criterion"]["statement"], "the tree does X"
            )
            self.assertEqual(
                fields["prime_exit_criterion"]["derived_from"], _ACCEPTED_SIZING_PATH
            )


if __name__ == "__main__":
    unittest.main()

"""test_coordinator_doc_new_sizing_object_gate.py -- coverage for C2 of the
plan sizing-citation gate (2026-08-06): threading `sizing_object` through
`_scaffold_plan` and `coordinator-doc-new --type plan --sizing-object PATH`.

Purpose: a plan's `sizing_object:` frontmatter key was hand-written on 17
plans and declared by nothing on the write path -- `_scaffold_plan` took no
sizing parameter, so the scaffolder could not emit it, and nothing checked
that a supplied path actually resolved before the file landed on disk. This
suite pins the scaffolder half of the fix (C1 declares the schema field
separately):

1. `--sizing-object PATH` resolving on disk emits a real `sizing_object:`
   frontmatter key (AC2).
2. Omitting BOTH `--sizing-object` and `--no-sizing-object` for `--type plan`
   exits 1 and writes no file -- INVERTED by
   docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md § C1: the
   commented-optional-key-skeleton-unchanged behaviour this docstring
   originally described no longer holds. An explicit sizing answer is now
   REQUIRED for `--type plan`; the old negative-spec case (bare omission
   passing silently) is exactly the failure mode
   `assert_plan_sizing_citation`'s absence leg exists to close, so it is
   refused here at write time instead.
3. `--sizing-object PATH` that does NOT resolve on disk fails loud (exit 1,
   naming the unresolvable path and `coordinator:sizing` as the route) and
   writes no file -- the write-time half of the gate, and the reason this
   chunk exists at all (AC3).
4. `--no-sizing-object` emits a literal unquoted `sizing_object: null` --
   parses via `yaml.safe_load` to real Python `None`, not the string
   `"null"`, since a quoted null would silently become a dangling citation
   under the absence-plan's sweep (AC2 of the absence plan).
5. `--sizing-object` and `--no-sizing-object` together exit 1 as mutually
   exclusive, writing no file (AC3 of the absence plan).

Spec backlink: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C2, AC2, AC3
Spec backlink: docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md § C1, AC1-AC4

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.
`_tmp_git_repo`/`_init_git_repo` mirror
test_coordinator_doc_new_roadmap_baton_self_validation.py's fixture exactly.

Negative-spec: does NOT cover AC1 (schema declaration -- C1's surface) or
AC4/AC5 (the corpus-wide assert op and the write-guard nudge -- C3/C4's
surfaces). Scoped to the scaffolder only.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sizing_object_gate.py -v
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

import yaml

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new"

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_object_gate_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sizing_object_gate_test", loader
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


class ScaffoldPlanEmitsSizingObjectTest(unittest.TestCase):
    """AC2: `_scaffold_plan(sizing_object=...)` emits a real frontmatter key."""

    def test_supplied_sizing_object_is_emitted_as_real_key(self):
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            sizing_object="state/sizings/2026-08-06-example.yaml",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(
            fields.get("sizing_object"), "state/sizings/2026-08-06-example.yaml"
        )

    def test_omitted_sizing_object_leaves_commented_skeleton_unchanged(self):
        """`_scaffold_plan` itself is unchanged for the no-sizing-object
        case -- the helper still emits the commented-optional-key skeleton
        when called with no `sizing_object` kwarg. C1 moved the REQUIREMENT
        that `--type plan` supply an explicit sizing answer into main()'s
        CLI-level validation (see FullCliSizingObjectRequiredTest below),
        not into this helper -- `_scaffold_plan` on its own still permits
        omission; the CLI is what now refuses it."""
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        self.assertNotIn("sizing_object:", content)
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("sizing_object", fields)
        # The pre-existing commented-optional-key block is untouched.
        self.assertIn(
            "# Optional keys — uncomment and fill as needed (promoted de-facto keys, D1):",
            content,
        )

    def test_no_sizing_object_emits_unquoted_null(self):
        """AC2 of the absence plan: `sizing_object="null"` (the sentinel
        threaded from `--no-sizing-object`) emits a literal unquoted
        `sizing_object: null` -- parses to real Python None, not the string
        "null"."""
        content = _cli._scaffold_plan(
            title="t", branch="b", author="test-author", sizing_object="null"
        )
        self.assertIn("sizing_object: null", content)
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertIsNone(fields.get("sizing_object"))
        self.assertNotEqual(fields.get("sizing_object"), "null")


class FullCliSizingObjectResolvesTest(unittest.TestCase):
    """AC2/AC3 end-to-end: the real CLI surface, not just the scaffolder helper."""

    def test_resolving_path_emits_key_and_writes_file(self):
        with _tmp_git_repo() as (repo, out_path):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-06-example.yaml"
            sizing_file.write_text("id: example\n")
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan",
                    "--sizing-object", "state/sizings/2026-08-06-example.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(
                fields.get("sizing_object"), "state/sizings/2026-08-06-example.yaml"
            )


class FullCliSizingObjectDanglingTest(unittest.TestCase):
    """AC3: a supplied path that does not resolve fails loud, no file written."""

    def test_dangling_path_exits_1_and_writes_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (dangling)",
                    "--sizing-object", "state/sizings/2026-08-06-does-not-exist.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state/sizings/2026-08-06-does-not-exist.yaml", result.stderr)
            self.assertIn("coordinator:sizing", result.stderr)
            self.assertFalse(out_path.exists())


class FullCliSizingObjectRequiredTest(unittest.TestCase):
    """AC1: neither flag supplied -> exit 1, no file, coordinator:sizing named."""

    def test_neither_flag_exits_1_and_writes_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (neither flag)",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("coordinator:sizing", result.stderr)
            self.assertFalse(out_path.exists())


class FullCliNoSizingObjectFlagTest(unittest.TestCase):
    """AC2 end-to-end: --no-sizing-object writes the plan with a real None."""

    def test_no_sizing_object_flag_writes_file_with_real_none(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (no-sizing-object)",
                    "--no-sizing-object",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertIn("sizing_object", fields)
            self.assertIsNone(fields.get("sizing_object"))
            self.assertNotEqual(fields.get("sizing_object"), "null")


class FullCliBothFlagsMutuallyExclusiveTest(unittest.TestCase):
    """AC3: both flags together -> exit 1, no file."""

    def test_both_flags_exit_1_and_write_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-06-example.yaml"
            sizing_file.write_text("id: example\n")
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (both flags)",
                    "--sizing-object", "state/sizings/2026-08-06-example.yaml",
                    "--no-sizing-object",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(out_path.exists())


if __name__ == "__main__":
    unittest.main()

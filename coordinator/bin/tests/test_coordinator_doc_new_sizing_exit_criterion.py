"""test_coordinator_doc_new_sizing_exit_criterion.py -- coverage for C3 of
docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md:
`--exit-criterion`/`--interaction-mode` on `--type sizing-object`.

Purpose: `_scaffold_sizing` had no way to emit the 1.23.0 `exit_criterion`/
`interaction_mode` fields (Design § Contract, C1) at scaffold time -- a PM's
exit criterion, confirmed at the sizing touchpoint, had to be hand-authored
onto the record afterward. This suite pins:

1. `_scaffold_sizing(exit_criterion=..., interaction_mode=...)` emits
   `exit_criterion: {statement, accepted: null}` and `interaction_mode: <mode>`,
   and the record validates against the vendored sizing-object schema (AC).
2. Omitting both leaves the record byte-identical to today's scaffold (no
   behavior change for every existing caller).
3. `--exit-criterion`/`--interaction-mode` are refused (exit 1, no file
   written) for every --type other than sizing-object -- the same type-
   scoped-flag posture `--sizing-object`/`--summary` already use.
4. The full CLI surface: `--type sizing-object --exit-criterion "S"
   --interaction-mode pm` emits a record carrying
   `exit_criterion: {statement: "S", accepted: null}` and
   `interaction_mode: pm`, and it validates.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_sizing_object_gate.py.

Spec backlink: docs/plans/2026-09-27-sizing-carries-exit-criterion-and-interaction-mode.md § C1, C3

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sizing_exit_criterion.py -v
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

from coordinator_core.frontmatter import schema_validate as sv
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_exit_criterion_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sizing_exit_criterion_test", loader
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
        yield repo


class ScaffoldSizingExitCriterionTest(unittest.TestCase):
    """AC: `_scaffold_sizing` emits the new fields and validates."""

    def test_exit_criterion_and_interaction_mode_emitted_and_validate(self):
        content = _cli._scaffold_sizing(
            title="an example PM ask",
            exit_criterion="the tree does X",
            interaction_mode="pm",
        )
        fields = yaml.safe_load(content)
        self.assertEqual(
            fields.get("exit_criterion"),
            {"statement": "the tree does X", "accepted": None},
        )
        self.assertEqual(fields.get("interaction_mode"), "pm")
        result = sv.validate("sizing-object", fields)
        self.assertTrue(result.get("ok"), result.get("errors"))

    def test_omitted_fields_are_byte_identical_to_today(self):
        content = _cli._scaffold_sizing(title="an example PM ask")
        self.assertNotIn("exit_criterion", content)
        self.assertNotIn("interaction_mode:", content)
        fields = yaml.safe_load(content)
        self.assertNotIn("exit_criterion", fields)
        self.assertNotIn("interaction_mode", fields)

    def test_only_exit_criterion_supplied(self):
        content = _cli._scaffold_sizing(
            title="an example PM ask", exit_criterion="only the criterion",
        )
        fields = yaml.safe_load(content)
        self.assertEqual(
            fields.get("exit_criterion"),
            {"statement": "only the criterion", "accepted": None},
        )
        self.assertNotIn("interaction_mode", fields)
        result = sv.validate("sizing-object", fields)
        self.assertTrue(result.get("ok"), result.get("errors"))


class CliFlagTypeScopeTest(unittest.TestCase):
    """AC: refused (exit 1, no file) for every --type but sizing-object."""

    def test_exit_criterion_refused_for_plan(self):
        with _tmp_git_repo() as repo:
            out_path = repo / "docs" / "plans" / "p.md"
            out_path.parent.mkdir(parents=True)
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "t", "--no-sizing-object",
                    "--exit-criterion", "S",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out_path.exists())
            self.assertIn("--exit-criterion", result.stderr)

    def test_interaction_mode_refused_for_plan(self):
        with _tmp_git_repo() as repo:
            out_path = repo / "docs" / "plans" / "p.md"
            out_path.parent.mkdir(parents=True)
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "t", "--no-sizing-object",
                    "--interaction-mode", "pm",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(out_path.exists())
            self.assertIn("--interaction-mode", result.stderr)


class FullCliSizingExitCriterionTest(unittest.TestCase):
    """AC: the real CLI surface end-to-end."""

    def test_full_cli_emits_and_validates(self):
        with _tmp_git_repo() as repo:
            out_path = repo / "state" / "sizings" / "2026-09-27-example.yaml"
            out_path.parent.mkdir(parents=True)
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "sizing-object", "--premise", "read", "--premise-evidence", "tests: premise read",
                    "--title", "an example PM ask",
                    "--exit-criterion", "S",
                    "--interaction-mode", "pm",
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
            fields = yaml.safe_load(out_path.read_text())
            self.assertEqual(
                fields.get("exit_criterion"), {"statement": "S", "accepted": None}
            )
            self.assertEqual(fields.get("interaction_mode"), "pm")
            result_v = sv.validate("sizing-object", fields)
            self.assertTrue(result_v.get("ok"), result_v.get("errors"))


if __name__ == "__main__":
    unittest.main()

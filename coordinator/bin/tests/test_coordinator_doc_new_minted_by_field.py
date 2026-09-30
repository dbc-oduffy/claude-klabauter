"""test_coordinator_doc_new_minted_by_field.py -- standing gate: every scaffold
arm that annotates ``authoring_session:`` with a ``# minted by <session name>``
comment also stamps the machine-readable ``minted_by:`` field (the operating
person's github alias, the value ``handoff.normalize`` stamps at the other
creation doors).

A YAML comment is invisible to every frontmatter reader, so a comment-only
baton looked attributed to a human and was unattributed to every tool. The
session display name must never leak into the field: it is a different axis
(harness session) from the person alias.

FAST TIER: no subprocess spawn -- session and identity resolvers are mocked at
the point of use.

Run:
    python3 -m pytest coordinator/bin/tests/test_coordinator_doc_new_minted_by_field.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_A_UUID = "bc1ca482-6b06-4943-ab49-92c9b35482ad"
_SESSION_NAME = "claude-klabauter-51"


def _load_cli_module():
    name = "coordinator_doc_new_minted_by_field_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _arms():
    return {
        "handoff": lambda: _cli._scaffold_handoff(title="t", branch="b"),
        "spinoff": lambda: _cli._scaffold_spinoff(title="t", branch="b"),
        "goal-seed": lambda: _cli._scaffold_goal_seed(title="t", branch="b"),
        "roadmap-seed": lambda: _cli._scaffold_roadmap_seed(title="t", branch="b"),
    }


def _scaffold(arm: str, alias: str | None = "dbc-example-operator") -> str:
    with mock.patch.object(
        _cli, "_resolve_session_id", return_value=_A_UUID
    ), mock.patch.object(
        _cli, "_resolve_session_display_name", return_value=_SESSION_NAME
    ), mock.patch.object(
        _cli, "_resolve_spinoff_workstream", return_value=None
    ), mock.patch.object(
        _cli,
        "_resolve_minted_by_line",
        return_value=f"minted_by: {alias}" if alias else None,
    ):
        return _arms()[arm]()


def _frontmatter_lines(content: str) -> list[str]:
    return content.split("---", 2)[1].splitlines()


class MintedByFieldTest(unittest.TestCase):
    def test_every_commenting_arm_stamps_the_field(self):
        for arm in _arms():
            with self.subTest(arm=arm):
                fm = _frontmatter_lines(_scaffold(arm))
                self.assertIn("minted_by: dbc-example-operator", fm)
                self.assertIn(f"# minted by {_SESSION_NAME}", fm)

    def test_field_is_a_single_top_level_line_never_the_session_name(self):
        for arm in _arms():
            with self.subTest(arm=arm):
                fm = _frontmatter_lines(_scaffold(arm))
                fields = [ln for ln in fm if ln.startswith("minted_by:")]
                self.assertEqual(fields, ["minted_by: dbc-example-operator"])

    def test_unresolvable_person_omits_the_key_entirely(self):
        for arm in _arms():
            with self.subTest(arm=arm):
                fm = _frontmatter_lines(_scaffold(arm, alias=None))
                self.assertFalse([ln for ln in fm if ln.startswith("minted_by:")])


class ResolveMintedByLineTest(unittest.TestCase):
    def _resolve(self, bundle):
        with mock.patch(
            "coordinator_core.person_resolver.resolve_operating_person",
            return_value=bundle,
        ):
            return _cli._resolve_minted_by_line()

    def test_github_alias_is_written_bare(self):
        self.assertEqual(self._resolve({"github": "dbc-example-operator"}), "minted_by: dbc-example-operator")

    def test_absent_alias_returns_none(self):
        self.assertIsNone(self._resolve({}))

    def test_unsafe_alias_is_quoted(self):
        self.assertEqual(self._resolve({"github": "a: b"}), 'minted_by: "a: b"')

    def test_resolver_failure_degrades_to_none(self):
        with mock.patch(
            "coordinator_core.person_resolver.resolve_operating_person",
            side_effect=OSError("boom"),
        ):
            self.assertIsNone(_cli._resolve_minted_by_line())


if __name__ == "__main__":
    unittest.main()

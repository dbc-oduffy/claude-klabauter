"""test_coordinator_doc_new_predecessor.py -- unit coverage for `_scaffold_handoff`'s
`predecessor` (path) parameter and its fail-loud contradiction guard (2026-07-29).

Purpose: before this fix, `--predecessor-id` (the C2 ID-companion) threaded
through unconditionally while `predecessor:` (the path field it companions)
was UNCONDITIONALLY hardcoded to the literal `none` -- a scaffold could
legally emit `predecessor: none` alongside a real `predecessor_id`, internally
contradictory frontmatter `schema_validate.py`'s own referential-integrity
check does not catch (it explicitly treats "id present, path absent" as
nothing-to-compare, by design, for the different in-flight-authoring case).
This suite covers the scaffolder's OWN correction: `predecessor` now threads
verbatim like `predecessor_id` already did, defaults to the byte-identical
`none` shape when omitted, and the scaffolder refuses (fail-loud) the
`predecessor_id`-without-`predecessor` combination rather than emitting it.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_archive_stamp_cli_ship_handoff.py /
test_session_claim_cli.py. Calls `_scaffold_handoff` directly (no subprocess,
no CLI argv parsing) -- `main()`'s own `--predecessor`/`--predecessor-id`
plumbing into that call is covered separately by
`coordinator_core/test_baton_assemble.py`'s d1-args assertions (this suite
does not re-derive that wiring).

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_predecessor.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

import yaml

from coordinator_core.frontmatter import schema_validate

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_predecessor_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_predecessor_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class ScaffoldHandoffPredecessorTest(unittest.TestCase):
    def test_predecessor_omitted_scaffolds_the_byte_identical_none_default(self):
        content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn("predecessor: none", content)
        self.assertNotIn("predecessor_id:", content)

    def test_predecessor_path_alone_threads_verbatim(self):
        content = _cli._scaffold_handoff(
            title="t", branch="b", predecessor="state/handoffs/2026-07-20-earlier.md"
        )
        self.assertIn('predecessor: "state/handoffs/2026-07-20-earlier.md"', content)
        self.assertNotIn("predecessor_id:", content)

    def test_predecessor_and_predecessor_id_both_thread_together(self):
        content = _cli._scaffold_handoff(
            title="t",
            branch="b",
            predecessor="archive/handoffs/2026-07/2026-07-20-earlier.md",
            predecessor_id="hnd-earlier-abc123",
        )
        self.assertIn('predecessor: "archive/handoffs/2026-07/2026-07-20-earlier.md"', content)
        self.assertIn('predecessor_id: "hnd-earlier-abc123"', content)

    def test_predecessor_id_without_predecessor_is_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(title="t", branch="b", predecessor_id="hnd-earlier-abc123")

    def test_predecessor_id_with_bare_none_predecessor_is_also_refused(self):
        with self.assertRaises(SystemExit):
            _cli._scaffold_handoff(
                title="t", branch="b", predecessor="none", predecessor_id="hnd-earlier-abc123"
            )

    def test_existing_recovery_scaffolder_unaffected(self):
        """Non-regression: `_scaffold_recovery`'s own `predecessor:` field
        (a crashed commit SHA, or null -- NOT a baton path) must be
        completely untouched by this fix -- it takes no new `predecessor`
        path parameter and its own hardcoded/conditional shape is unchanged."""
        content = _cli._scaffold_recovery(
            title="t", branch="b", recovers_session="sid-1"
        )
        self.assertIn("predecessor: null", content)


class ScaffoldHandoffAuthoringSessionTest(unittest.TestCase):
    """`authoring_session` stamping (2026-07-30, cross-authorship adoption
    gap) -- `_scaffold_handoff` is the ONLY scaffolder this change touches;
    the other kinds' `PLACEHOLDER` emission is asserted unchanged elsewhere
    and is out of scope here."""

    _ENV_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

    def test_emits_stamped_line_when_env_var_is_set(self):
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_SESSION_ID": "sid-abc123"},
        ):
            for var in ("COORDINATOR_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
                os.environ.pop(var, None)
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn('authoring_session: "sid-abc123"', content)

    def test_omits_the_key_entirely_when_no_env_var_is_set(self):
        with mock.patch.dict(os.environ, {}):
            for var in self._ENV_VARS:
                os.environ.pop(var, None)
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertNotIn("authoring_session:", content)

    def test_stamped_scaffold_still_validates_against_the_handoff_schema(self):
        with mock.patch.dict(os.environ, {"CLAUDE_SESSION_ID": "sid-abc123"}):
            for var in ("COORDINATOR_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
                os.environ.pop(var, None)
            content = _cli._scaffold_handoff(title="t", branch="b")
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("authoring_session"), "sid-abc123")
        result = schema_validate.validate("handoff", fields)
        self.assertTrue(result["ok"], result.get("errors"))

    def test_precedence_chain_matches_resolve_session_id(self):
        """COORDINATOR_SESSION_ID must win over CLAUDE_SESSION_ID -- the same
        precedence `_resolve_session_id` already documents and this
        scaffolder reuses verbatim, not a second resolver."""
        with mock.patch.dict(
            os.environ,
            {"COORDINATOR_SESSION_ID": "sid-primary", "CLAUDE_SESSION_ID": "sid-secondary"},
        ):
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn('authoring_session: "sid-primary"', content)


if __name__ == "__main__":
    unittest.main()

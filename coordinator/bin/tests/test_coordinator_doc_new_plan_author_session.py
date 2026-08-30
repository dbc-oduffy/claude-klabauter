"""test_coordinator_doc_new_plan_author_session.py -- unit coverage for the
session-identity stamp on plan `author:` and handoff/spinoff
`authoring_session:` (2026-08-20).

Purpose: `author:` on a scaffolded plan used to be a hardcoded, then a
repo-level EM-role string (`claude-klabauter-em`) -- traceable to the repo, not
to WHICH session minted the plan. `_resolve_plan_author` now stamps the
minting session's own resolvable name (e.g. `claude-klabauter-76`) off
`coordinator_core.session.harness_registry.self_record()`, falling back to the
prior repo-level identity (`_resolve_from_repo()`) when the registry seam
can't resolve one.

Extension (same dispatch): `_scaffold_handoff`'s `authoring_session:` field
was already machine-stamped, but as a raw session UUID with no human-readable
name attached; `_scaffold_spinoff` hand-typed a literal `PLACEHOLDER` an EM
had to fill in via Edit. Both now call the shared `_resolve_session_display_
name()` -- handoff appends it as a YAML trailing COMMENT beside the existing
UUID (no schema change, no new field); spinoff uses it to compute the same
UUID + comment `_scaffold_handoff` already emits, falling back to the literal
`PLACEHOLDER` (this function's own pre-existing unresolved convention, not
handoff's omit-the-key convention) only when unresolvable.

Stubs `harness_registry.self_record` and `_resolve_session_id` directly (no
live registry file, no subprocess) -- same injection idiom this file's
sibling suites use for session-state seams (see
test_coordinator_doc_new_predecessor.py's docstring). Never asserts against a
real running session's identity, which would be un-reproducible on CI/another
box.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_author_session.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_author_session_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_author_session_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _FakeRecord:
    def __init__(self, name: str | None):
        self.name = name


class ResolvePlanAuthorTest(unittest.TestCase):
    def test_uses_self_record_name_when_resolvable(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ):
            with mock.patch.object(_cli, "_resolve_session_id", return_value="sid-123"):
                self.assertEqual(
                    _cli._resolve_plan_author(), "claude-klabauter-76 (sid-123)"
                )

    def test_falls_back_to_repo_identity_when_self_record_is_none(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=None,
        ), mock.patch.object(
            _cli, "_resolve_from_repo", return_value="claude-klabauter-em"
        ):
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-em")

    def test_falls_back_to_repo_identity_when_record_name_is_empty(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord(None)),
        ), mock.patch.object(
            _cli, "_resolve_from_repo", return_value="claude-klabauter-em"
        ):
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-em")

    def test_falls_back_to_repo_identity_when_registry_read_raises(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            side_effect=RuntimeError("registry unreadable"),
        ), mock.patch.object(
            _cli, "_resolve_from_repo", return_value="claude-klabauter-em"
        ):
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-em")

    def test_scaffold_plan_threads_the_resolved_author(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ):
            with mock.patch.object(_cli, "_resolve_session_id", return_value="sid-123"):
                author = _cli._resolve_plan_author()
        content = _cli._scaffold_plan(title="t", branch="b", author=author)
        self.assertIn("author: claude-klabauter-76 (sid-123)", content)
        self.assertNotIn("replace with actual author", content)


class ResolvePlanAuthorUuidTest(unittest.TestCase):
    """The uuid half is the point: a display name collides between live
    sessions and nothing recovers it afterwards. See `_resolve_plan_author`."""

    def test_name_alone_when_session_id_is_the_unknown_sentinel(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ), mock.patch.object(_cli, "_resolve_session_id", return_value="em-unknown"):
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-76")

    def test_name_alone_when_session_id_resolver_raises(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ), mock.patch.object(
            _cli, "_resolve_session_id", side_effect=RuntimeError("seam down")
        ):
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-76")

    def test_author_line_is_resolvable_to_a_session(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ), mock.patch.object(
            _cli, "_resolve_session_id", return_value="aac212bc-ea6b-4172-bd9f-b885f156c033"
        ):
            author = _cli._resolve_plan_author()
        self.assertIn("aac212bc-ea6b-4172-bd9f-b885f156c033", author)


class ResolveSessionDisplayNameTest(unittest.TestCase):
    def test_returns_record_name_when_resolvable(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord("claude-klabauter-76")),
        ):
            self.assertEqual(_cli._resolve_session_display_name(), "claude-klabauter-76")

    def test_returns_none_when_self_record_is_none(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=None,
        ):
            self.assertIsNone(_cli._resolve_session_display_name())

    def test_returns_none_when_record_name_is_empty(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            return_value=("sid-123", _FakeRecord(None)),
        ):
            self.assertIsNone(_cli._resolve_session_display_name())

    def test_returns_none_when_registry_read_raises(self):
        with mock.patch(
            "coordinator_core.session.harness_registry.self_record",
            side_effect=RuntimeError("registry unreadable"),
        ):
            self.assertIsNone(_cli._resolve_session_display_name())


class ScaffoldHandoffAuthoringSessionDisplayNameTest(unittest.TestCase):
    def test_uuid_gets_a_readable_name_on_its_own_comment_line_when_resolvable(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="bc1ca482-6b06-4943-ab49-92c9b35482ad"
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value="claude-klabauter-51"
        ):
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn(
            '# minted by claude-klabauter-51\n'
            'authoring_session: "bc1ca482-6b06-4943-ab49-92c9b35482ad"',
            content,
        )

    def test_uuid_alone_when_display_name_unresolvable(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="bc1ca482-6b06-4943-ab49-92c9b35482ad"
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value=None
        ):
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertIn(
            'authoring_session: "bc1ca482-6b06-4943-ab49-92c9b35482ad"',
            content,
        )
        for line in content.splitlines():
            if line.startswith("authoring_session:"):
                self.assertNotIn("#", line)

    def test_field_omitted_entirely_when_session_id_unresolvable(self):
        with mock.patch.object(_cli, "_resolve_session_id", return_value="em-unknown"):
            content = _cli._scaffold_handoff(title="t", branch="b")
        self.assertNotIn("authoring_session:", content)


class ScaffoldSpinoffAuthoringSessionTest(unittest.TestCase):
    def test_uuid_and_display_name_replace_the_hand_typed_placeholder(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="bc1ca482-6b06-4943-ab49-92c9b35482ad"
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value="claude-klabauter-51"
        ), mock.patch.object(
            _cli, "_resolve_spinoff_workstream", return_value=None
        ):
            content = _cli._scaffold_spinoff(title="t", branch="b")
        self.assertIn(
            '# minted by claude-klabauter-51\n'
            'authoring_session: "bc1ca482-6b06-4943-ab49-92c9b35482ad"',
            content,
        )
        self.assertNotIn("authoring_session: PLACEHOLDER", content)

    def test_unresolvable_session_id_fails_loud_not_placeholder(self):
        """2026-08-21: the 'em-unknown' arm now refuses the scaffold outright
        (sys.exit 1) instead of degrading to a hand-typed 'PLACEHOLDER' --
        see test_coordinator_doc_new_spinoff_resolvable_fields.py for the
        dedicated coverage of this contract."""
        with mock.patch.object(_cli, "_resolve_session_id", return_value="em-unknown"):
            with self.assertRaises(SystemExit) as ctx:
                _cli._scaffold_spinoff(title="t", branch="b")
        self.assertEqual(ctx.exception.code, 1)

    def test_workstream_resolves_off_the_held_baton(self):
        """2026-08-21: `workstream` is no longer a hand-typed placeholder --
        it resolves via `_resolve_spinoff_workstream` (dedicated coverage in
        test_coordinator_doc_new_spinoff_resolvable_fields.py)."""
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="bc1ca482-6b06-4943-ab49-92c9b35482ad"
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value="claude-klabauter-51"
        ), mock.patch.object(
            _cli, "_resolve_spinoff_workstream", return_value="my-workstream"
        ):
            content = _cli._scaffold_spinoff(title="t", branch="b")
        self.assertIn('workstream: "my-workstream"', content)
        self.assertNotIn("workstream: PLACEHOLDER", content)


if __name__ == "__main__":
    unittest.main()


class AuthoringSessionStaysMachineReadableTest(unittest.TestCase):
    """The readable session name must never land on the `authoring_session:`
    line itself.

    Regression: a trailing `# name` comment on that line was returned AS PART OF
    THE VALUE by every line-based frontmatter reader that does not strip
    comments — including `session_ledger.aggregate_chain_loe ::
    extract_frontmatter_field`, whose caller `_resolve_fallback_session_id`
    then attributed chain LoE to a session id that does not exist. Two other
    readers happened to be comment-safe, which is what made the break look
    absent. Keeping the name on its OWN line removes the whole class: a reader
    keyed on `^authoring_session:` cannot match a comment-only line.
    """

    def _emit(self):
        with mock.patch.object(_cli, "_resolve_session_id", return_value="bc1ca482-6b06-4943-ab49-92c9b35482ad"), \
             mock.patch.object(_cli, "_resolve_session_display_name", return_value="claude-klabauter-51"), \
             mock.patch.object(_cli, "_resolve_spinoff_workstream", return_value=None):
            return _cli._scaffold_spinoff(title="t", branch="b")

    def test_authoring_session_line_carries_no_inline_comment(self):
        for line in self._emit().splitlines():
            if line.startswith("authoring_session:"):
                self.assertNotIn("#", line)
                return
        self.fail("no authoring_session: line emitted")

    def test_line_based_reader_recovers_the_bare_uuid(self):
        content = self._emit()
        for line in content.splitlines():
            if line.startswith("authoring_session:"):
                value = line.split(":", 1)[1].strip().strip('"').strip("'")
                self.assertEqual(value, "bc1ca482-6b06-4943-ab49-92c9b35482ad")
                return
        self.fail("no authoring_session: line emitted")

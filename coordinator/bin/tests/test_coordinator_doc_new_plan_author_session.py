"""test_coordinator_doc_new_plan_author_session.py -- unit coverage for
`_resolve_plan_author`'s session-identity stamp (2026-08-20).

Purpose: `author:` on a scaffolded plan used to be a hardcoded, then a
repo-level EM-role string (`claude-klabauter-em`) -- traceable to the repo, not
to WHICH session minted the plan. `_resolve_plan_author` now stamps the
minting session's own resolvable name (e.g. `claude-klabauter-76`) off
`coordinator_core.session.harness_registry.self_record()`, falling back to the
prior repo-level identity (`_resolve_from_repo()`) when the registry seam
can't resolve one.

Stubs `harness_registry.self_record` directly (no live registry file, no
subprocess) -- same injection idiom this file's sibling suites use for
session-state seams (see test_coordinator_doc_new_predecessor.py's docstring).
Never asserts against a real running session's identity, which would be
un-reproducible on CI/another box.

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
            self.assertEqual(_cli._resolve_plan_author(), "claude-klabauter-76")

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
            author = _cli._resolve_plan_author()
        content = _cli._scaffold_plan(title="t", branch="b", author=author)
        self.assertIn("author: claude-klabauter-76", content)
        self.assertNotIn("replace with actual author", content)


if __name__ == "__main__":
    unittest.main()

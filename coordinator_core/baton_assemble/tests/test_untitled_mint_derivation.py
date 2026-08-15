"""Coverage for the 2026-08-10 PM ruling on `_compute_fresh_output_path`'s
standalone-mint slug derivation (baton_assemble's naming defect fix):
plan/predecessor artifact -> this session's own sizing object ->
caller-supplied title -> a non-colliding shortid, NEVER the literal
`"untitled"`.

Spec backlink: this session's dispatch brief (baton-assemble naming defect,
2026-08-10) plus the PM's mid-task amendment adding the sizing-object
derivation tier.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from coordinator_core.baton_assemble import (
    _compute_fresh_output_path,
    _mint_last_resort_slug,
    _resolve_session_sizing_slug,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _run_git(args: list[str], cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        check=True,
        **no_console_creationflags(),
    )


class _GitRepoFixture(unittest.TestCase):
    _SESSION_ENV_VARS = (
        "COORDINATOR_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env = dict(os.environ)
        self.env["GIT_AUTHOR_NAME"] = "Test"
        self.env["GIT_AUTHOR_EMAIL"] = "test@example.com"
        self.env["GIT_COMMITTER_NAME"] = "Test"
        self.env["GIT_COMMITTER_EMAIL"] = "test@example.com"
        _run_git(["init", "-q"], self.root, self.env)
        (self.root / "state" / "sizings").mkdir(parents=True)
        (self.root / "state" / "handoffs").mkdir(parents=True)
        self._saved_session_env = {
            key: os.environ.get(key) for key in self._SESSION_ENV_VARS
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._saved_session_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set_session_env(self, **overrides: str) -> None:
        for key in self._SESSION_ENV_VARS:
            os.environ.pop(key, None)
        for key, value in overrides.items():
            os.environ[key] = value

    def _commit_sizing(self, name: str, session_id: str) -> None:
        path = self.root / "state" / "sizings" / name
        path.write_text("schema: sizing-object\nintent: test\n", encoding="utf-8")
        _run_git(["add", "--", f"state/sizings/{name}"], self.root, self.env)
        _run_git(
            [
                "commit",
                "-q",
                "-m",
                f"add {name}\n\nSession-Id: {session_id}\n",
            ],
            self.root,
            self.env,
        )


class TestStandaloneNoTitleNoSizing(_GitRepoFixture):
    def test_no_source_yields_non_untitled_shortid_slug(self) -> None:
        self._set_session_env()
        path = _compute_fresh_output_path("", root=self.root, title=None)
        basename = Path(path).stem
        self.assertNotEqual(basename.split("-", 3)[-1], "untitled")
        self.assertIn("untitled-", basename)
        # date-<8charhex>
        suffix = basename.rsplit("untitled-", 1)[-1]
        self.assertEqual(len(suffix), 8)
        int(suffix, 16)  # raises if not hex


class TestStandaloneWithTitle(_GitRepoFixture):
    def test_title_supplied_derives_slug(self) -> None:
        path = _compute_fresh_output_path("", root=self.root, title="My Great Title")
        self.assertIn("my-great-title", Path(path).stem)


class TestPlanArtifactDerivation(_GitRepoFixture):
    def test_plan_path_derives_slug(self) -> None:
        path = _compute_fresh_output_path(
            "docs/plans/2026-08-01-some-plan.md", root=self.root, title=None
        )
        self.assertIn("some-plan", Path(path).stem)


class TestSessionSizingDerivation(_GitRepoFixture):
    def test_own_session_sizing_picked(self) -> None:
        session_id = "11111111-1111-1111-1111-111111111111"
        self._commit_sizing("2026-08-10-config-files-are-not-changelogs-strip-an.yaml", session_id)
        self._set_session_env(COORDINATOR_SESSION_ID=session_id)
        slug = _resolve_session_sizing_slug(self.root)
        self.assertEqual(slug, "config-files-are-not-changelogs-strip-an")

    def test_different_session_sizing_not_picked(self) -> None:
        # negative: a sizing authored by a DIFFERENT session must not
        # be picked up by this session's derivation.
        other_session = "22222222-2222-2222-2222-222222222222"
        self._commit_sizing("2026-08-10-someone-elses-sizing.yaml", other_session)
        self._set_session_env(
            COORDINATOR_SESSION_ID="33333333-3333-3333-3333-333333333333"
        )
        slug = _resolve_session_sizing_slug(self.root)
        self.assertIsNone(slug)

    def test_sizing_precedes_title_in_standalone_derivation(self) -> None:
        session_id = "44444444-4444-4444-4444-444444444444"
        self._commit_sizing("2026-08-10-the-sizing-slug-wins.yaml", session_id)
        self._set_session_env(COORDINATOR_SESSION_ID=session_id)
        path = _compute_fresh_output_path(
            "", root=self.root, title="A totally different title"
        )
        self.assertIn("the-sizing-slug-wins", Path(path).stem)

    def test_multiple_sizings_same_session_picks_most_recent(self) -> None:
        session_id = "55555555-5555-5555-5555-555555555555"
        self._commit_sizing("2026-08-09-older-sizing.yaml", session_id)
        self._commit_sizing("2026-08-10-newer-sizing.yaml", session_id)
        self._set_session_env(COORDINATOR_SESSION_ID=session_id)
        slug = _resolve_session_sizing_slug(self.root)
        self.assertEqual(slug, "newer-sizing")


class TestOutputPathDeterminism(_GitRepoFixture):
    def test_repeated_derivation_is_stable(self) -> None:
        # `apply()` recomputes `brief()` in-process from the same
        # `(kind, artifact_path)` -- brief/apply path agreement rests on
        # `_compute_fresh_output_path` returning the SAME candidate for
        # identical inputs on repeated invocation within one day. Exercised
        # directly (not through `brief()`) so this test does not also
        # depend on `resolve_operator_config()`'s machine-local settings
        # resolution, which is unrelated to this derivation.
        session_id = "66666666-6666-6666-6666-666666666666"
        self._commit_sizing("2026-08-10-path-agreement-check.yaml", session_id)
        self._set_session_env(COORDINATOR_SESSION_ID=session_id)
        path1 = _compute_fresh_output_path("", root=self.root, title=None)
        path2 = _compute_fresh_output_path("", root=self.root, title=None)
        self.assertEqual(path1, path2)
        self.assertIn("path-agreement-check", path1)


if __name__ == "__main__":
    unittest.main()

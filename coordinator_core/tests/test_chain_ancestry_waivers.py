"""
coordinator_core.tests.test_chain_ancestry_waivers

Coverage for `chain_ancestry_waivers.chain_reached_terminal_close` — the
retention predicate W2's (next wave) reaper is built on: a chain's minted
waivers are reapable only once THAT chain_id has itself reached a terminal
`closed` disposition, per the ratified DR-084 vocabulary
(`open`/`claimed`/`continued`/`closed`).

This predicate is a thin, deliberate reuse of
`coordinator_core.ops.session.resolve_chain_terminal_disposition`'s
classification core (`classify_chain_terminal_disposition`, the public
wrapper this chunk added) via its `param_sid` tier — NOT a re-derivation of
the archived-handoff `deployment_state` read. See that module's own
docstring for the full dual-detector contract this predicate rides on top
of.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md § W1

Negative-spec:
  - Does NOT exercise the reaper (W2) — that op does not exist yet and this
    chunk does not write it.
  - Does NOT duplicate the archive-frontmatter read logic — every fixture
    here exists solely to drive `classify_chain_terminal_disposition`'s
    existing dual-detector classification, never a hand-rolled parse.
  - Does NOT delete any waiver file under state/review-trail/ — this module
    reads a predicate only.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.chain_ancestry_waivers import chain_reached_terminal_close

# _make_repo spawns real git per test (init/config/add/commit) — declared to
# the spawn ratchet rather than grandfathered in its frozen baseline. See
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _archive_handoff(repo, chain_id, deployment_state):
    archive_dir = repo / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{chain_id}.md").write_text(
        "---\n"
        "predecessor: none\n"
        f"claimed_by: {chain_id}\n"
        f"deployment_state: {deployment_state}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"archive handoff for {chain_id}"],
        cwd=repo,
        check=True,
    )


class TestChainReachedTerminalClose:
    def test_closed_disposition_is_terminal(self, tmp_path):
        repo = _make_repo(tmp_path)
        chain_id = "closed-chain-id"
        _archive_handoff(repo, chain_id, "closed")

        assert chain_reached_terminal_close(str(repo), chain_id) is True

    def test_continued_disposition_is_not_terminal(self, tmp_path):
        """The chain handed off to a successor under a DIFFERENT chain_id —
        a later close under the successor's own id does not license reaping
        THIS chain_id's waivers."""
        repo = _make_repo(tmp_path)
        chain_id = "continued-chain-id"
        _archive_handoff(repo, chain_id, "continued")

        assert chain_reached_terminal_close(str(repo), chain_id) is False

    def test_no_archived_record_is_not_terminal(self, tmp_path):
        """No claimed/archived handoff at all for this chain_id — the
        classification core's own 'open'/not-terminal branch. Must fail
        closed (never reapable), not raise."""
        repo = _make_repo(tmp_path)

        assert chain_reached_terminal_close(str(repo), "never-seen-chain-id") is False

    def test_classification_error_fails_closed_not_terminal(self, tmp_path):
        """A CC-7 structured-error classification (banned/unknown
        deployment_state token) must read as NON-terminal, never as
        'safe to reap' — the requirement W2's reaper is built on."""
        repo = _make_repo(tmp_path)
        chain_id = "abandoned-chain-id"
        _archive_handoff(repo, chain_id, "abandoned")

        assert chain_reached_terminal_close(str(repo), chain_id) is False

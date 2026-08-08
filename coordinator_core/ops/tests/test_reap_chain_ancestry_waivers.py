"""
coordinator_core.ops.tests.test_reap_chain_ancestry_waivers

Coverage for `coordinator_core.ops.reap_chain_ancestry_waivers.
reap_chain_ancestry_waivers` — the fail-closed, idempotent, remove-only,
single-chain-scoped reaper built on W1's `chain_reached_terminal_close`
predicate.

Fixtures reuse the same `_make_repo`/`_archive_handoff` recipe as
`coordinator_core/tests/test_chain_ancestry_waivers.py` (W1's own test), so
`chain_reached_terminal_close` classifies each fixture chain identically here
and there.

Spec backlink: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md § W2

Negative-spec:
  - Does NOT exercise `chain_ancestry_waivers.py`'s own predicate/mint logic
    beyond driving it as a black box — no re-derivation of its classification.
  - Does NOT touch `state/review-trail/chain-ancestry-waivers/` in THIS repo
    — every fixture lives under a fresh `tmp_path` git repo.
  - Does NOT assert on `record_chain_ancestry_waiver` — waiver files are
    written directly as fixture setup, never minted through the real writer,
    since this reaper must work on any file present under a chain
    subdirectory, not only gate-minted ones.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.reap_chain_ancestry_waivers import (
    reap_chain_ancestry_waivers,
)

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


def _write_waiver(repo, chain_id, sha):
    chain_dir = repo / "state" / "review-trail" / "chain-ancestry-waivers" / chain_id
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / f"{sha}.json").write_text('{"sha": "%s"}\n' % sha, encoding="utf-8")


class TestReapChainAncestryWaivers:
    def test_terminal_closed_chain_is_reaped(self, tmp_path):
        repo = _make_repo(tmp_path)
        chain_id = "cafe1111-dead-beef-0000-000000000001"
        _archive_handoff(repo, chain_id, "closed")
        _write_waiver(repo, chain_id, "a" * 40)
        _write_waiver(repo, chain_id, "b" * 40)

        result = reap_chain_ancestry_waivers(str(repo))

        assert result["removed"] == [{"chain_id": chain_id, "file_count": 2}]
        assert result["skipped"] == []
        assert result["errors"] == []
        chain_dir = repo / "state" / "review-trail" / "chain-ancestry-waivers" / chain_id
        assert not chain_dir.exists()

    def test_continued_chain_is_kept(self, tmp_path):
        repo = _make_repo(tmp_path)
        chain_id = "cafe1111-dead-beef-0000-000000000002"
        _archive_handoff(repo, chain_id, "continued")
        _write_waiver(repo, chain_id, "c" * 40)

        result = reap_chain_ancestry_waivers(str(repo))

        assert result["removed"] == []
        assert {"chain_id": chain_id, "reason": "not_terminal"} in result["skipped"]
        assert result["errors"] == []
        chain_dir = repo / "state" / "review-trail" / "chain-ancestry-waivers" / chain_id
        assert chain_dir.is_dir()
        assert (chain_dir / ("c" * 40 + ".json")).exists()

    def test_no_archived_record_is_kept(self, tmp_path):
        repo = _make_repo(tmp_path)
        chain_id = "cafe1111-dead-beef-0000-000000000003"
        _write_waiver(repo, chain_id, "d" * 40)

        result = reap_chain_ancestry_waivers(str(repo))

        assert result["removed"] == []
        assert {"chain_id": chain_id, "reason": "not_terminal"} in result["skipped"]
        assert result["errors"] == []
        chain_dir = repo / "state" / "review-trail" / "chain-ancestry-waivers" / chain_id
        assert chain_dir.is_dir()

    def test_missing_root_is_a_noop(self, tmp_path):
        repo = _make_repo(tmp_path)

        result = reap_chain_ancestry_waivers(str(repo))

        assert result == {"removed": [], "skipped": [], "errors": []}

    def test_rerun_after_reap_is_idempotent(self, tmp_path):
        repo = _make_repo(tmp_path)
        closed_id = "cafe1111-dead-beef-0000-000000000004"
        continued_id = "cafe1111-dead-beef-0000-000000000005"
        _archive_handoff(repo, closed_id, "closed")
        _archive_handoff(repo, continued_id, "continued")
        _write_waiver(repo, closed_id, "e" * 40)
        _write_waiver(repo, continued_id, "f" * 40)

        first = reap_chain_ancestry_waivers(str(repo))
        assert first["removed"] == [{"chain_id": closed_id, "file_count": 1}]

        second = reap_chain_ancestry_waivers(str(repo))

        assert second["removed"] == []
        assert second["errors"] == []
        assert {"chain_id": continued_id, "reason": "not_terminal"} in second["skipped"]
        # The reaped chain's directory is simply gone now — nothing left to
        # report about it, terminal or otherwise.
        assert all(entry["chain_id"] != closed_id for entry in second["skipped"])

    def test_invalid_chain_id_is_never_touched(self, tmp_path):
        repo = _make_repo(tmp_path)
        bad_dir = repo / "state" / "review-trail" / "chain-ancestry-waivers" / "not valid!!"
        bad_dir.mkdir(parents=True)
        (bad_dir / "x.json").write_text("{}", encoding="utf-8")

        result = reap_chain_ancestry_waivers(str(repo))

        assert result["removed"] == []
        assert {"chain_id": "not valid!!", "reason": "invalid_chain_id"} in result["skipped"]
        assert bad_dir.is_dir()
        assert (bad_dir / "x.json").exists()

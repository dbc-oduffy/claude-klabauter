"""
coordinator_core.ops.tests.test_completion_ops_reconcile — REOPENED session_id
git-resolution path parity coverage for completion.reconcile_commits.

Byte-parity oracle — Port of: reconcile-completion-commits.sh (DoE 432e3285, 2026-07-22), Zone A (pre-append-mode-fork) —
merge-base resolution, chain-slug expansion (Axis 1 multi-session widening),
multi-session Session-Id: trailer collection, SHA canonicalization, and the
id-provenance-mismatch probe. Fixture pattern mirrors
coordinator_core/reconcile/tests/test_commit_reality.py (tmp git repo, real
subprocess git — not mocked).

Covers:
  - backward compat: pre-computed commits list (session_id omitted) — unaffected.
  - chain-widening (Axis 1): a session-id's chain pulls in a sibling session's
    commits via authored_by (archive/completed/) and claimed_by (handoffs).
  - id-provenance-mismatch probe: zero matching commits in range, but the range
    DOES contain a foreign Session-Id: trailer — provenance_warning fires.
  - SHA canonicalization parity: a short stored SHA correctly dedupes against
    the full SHA a matching commit's log line reports (no short/ambiguous leak).
  - merge-base-unresolved non-blocking no-op.
  - session_id allowlist / "null" guard (ValueError, oracle exit-1 parity).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core.ops.completion_ops import (
    _reconcile_commits_handler,
    reconcile_completion_commits,
    resolve_chain_commits,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tmp git repo with identity configured and origin/main seeded."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: seed repo")
    # Anchor origin/main to the seed commit (no real remote needed — merge-base
    # only needs the ref to exist).
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def _commit(root: Path, rel_path: str, content: str, subject: str, trailer: str = "") -> str:
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    _git(root, "add", rel_path)
    msg = subject if not trailer else f"{subject}\n\n{trailer}"
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def _full_sha(root: Path, short: str) -> str:
    return _git(root, "rev-parse", short).stdout.strip()


def _write_entry(
    root: Path,
    rel_path: str,
    *,
    chain: str = "",
    commits: List[str] = (),
) -> Path:
    commits_block = (
        "[]"
        if not commits
        else "\n" + "\n".join(f'  - "{c}"' for c in commits)
    )
    chain_line = f"chain: {chain}\n" if chain else ""
    content = (
        "---\n"
        "status: pending-release\n"
        f"{chain_line}"
        f"commits: {commits_block}\n"
        "---\n"
        "body\n"
    )
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestBackwardCompat:
    """session_id omitted: behaves exactly as the pre-existing pre-computed path."""

    def test_precomputed_commits_path_unaffected(self, repo: Path, tmp_path: Path) -> None:
        entry = _write_entry(tmp_path, "entry.md", commits=[])
        result = reconcile_completion_commits(str(entry), commits=["abc1234"])
        assert result["appended"] == 1
        assert result["no_op"] is False
        assert "merge_base_unresolved" not in result
        assert "chain_session_ids" not in result

    def test_empty_commits_is_noop(self, tmp_path: Path) -> None:
        entry = _write_entry(tmp_path, "entry.md", commits=[])
        result = reconcile_completion_commits(str(entry), commits=[])
        assert result == {
            "plan_path": str(entry),
            "appended": 0,
            "skipped": 0,
            "no_op": True,
            "dry_run": False,
            "delta_shorts": [],
        }


class TestChainWidening:
    """Axis 1: session-id widens across the chain via authored_by/claimed_by."""

    def test_multi_session_chain_pulls_sibling_commits(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")

        # Sibling completion entry authored by sess-b, sharing chain: mychain.
        _write_entry(
            repo,
            "archive/completed/sibling.md",
            chain="mychain",
        )
        (repo / "archive/completed/sibling.md").write_text(
            (repo / "archive/completed/sibling.md").read_text(encoding="utf-8").replace(
                "commits: []", "authored_by: sess-b\ncommits: []"
            ),
            encoding="utf-8",
        )

        # This entry is the one under reconcile — chain: mychain, authored by sess-a
        # (implicitly the passed session_id), no commits stored yet.
        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-b"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_b}
        assert resolved["delta_count"] == 2
        assert resolved["merge_base_unresolved"] is False
        assert resolved["provenance_warning"] is None

    def test_chain_widening_via_handoff_claimed_by(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_c = _commit(repo, "c.txt", "c\n", "feat: c", "Session-Id: sess-c")

        handoff = repo / "state" / "handoffs" / "h1.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            "---\nworkstream: mychain\nclaimed_by: sess-c\n---\nbody\n",
            encoding="utf-8",
        )

        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-c"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_c}

    def test_chain_widening_tolerates_legacy_consumed_by(self, repo: Path) -> None:
        """DR-084: consumed_by is a deliberately-kept legacy fallback for
        pre-migration handoffs (completion_ops._read_frontmatter_field checks
        claimed_by first, consumed_by second) — old-vocabulary input stays here."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_c = _commit(repo, "c.txt", "c\n", "feat: c", "Session-Id: sess-c")

        handoff = repo / "state" / "handoffs" / "h1.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            "---\nworkstream: mychain\nconsumed_by: sess-c\n---\nbody\n",
            encoding="utf-8",
        )

        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-c"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_c}

    def test_no_chain_slug_scopes_to_single_session(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")

        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])  # no chain:

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["chain_session_ids"] == ["sess-a"]
        assert resolved["delta_shorts"] == [sha_a]

    def test_cosmetic_drift_in_chain_line_still_widens(self, repo: Path) -> None:
        """Finding 2 regression: a sibling entry's chain: line carries a trailing
        inline comment (cosmetic drift the old literal-line match would have missed
        since _read_frontmatter_field strips it but raw string-equality didn't)."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")

        sibling = _write_entry(repo, "archive/completed/sibling.md", chain="mychain")
        sibling.write_text(
            sibling.read_text(encoding="utf-8")
            .replace("chain: mychain", "chain: mychain  # cosmetic note")
            .replace("commits: []", "authored_by: sess-b\ncommits: []"),
            encoding="utf-8",
        )

        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-b"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_b}

    def test_explicit_chain_slug_override(self, repo: Path) -> None:
        """Finding 7: explicit chain_slug param overrides the entry's own frontmatter."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")

        sibling = _write_entry(repo, "archive/completed/sibling.md", chain="otherchain")
        sibling.write_text(
            sibling.read_text(encoding="utf-8").replace(
                "commits: []", "authored_by: sess-b\ncommits: []"
            ),
            encoding="utf-8",
        )

        # Entry's own frontmatter says chain: mychain, but the explicit chain_slug
        # param overrides it to "otherchain", which should pull in sess-b instead.
        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a", chain_slug="otherchain")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-b"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_b}

    def test_explicit_invalid_chain_slug_warns_and_skips(self, repo: Path) -> None:
        """Finding 7: an explicit chain_slug failing the allowlist hits the
        WARN-and-skip-to-'' branch, scoping resolution to just the seed session."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")

        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        resolved = resolve_chain_commits(
            repo, str(entry), "sess-a", chain_slug="bad;slug"
        )

        assert resolved["chain_session_ids"] == ["sess-a"]
        assert resolved["delta_shorts"] == [sha_a]

    def test_meta_repo_widening_resolves_central_root(
        self, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 6: meta-repo branch of _resolve_handoff_dirs (is_meta_repo True)
        resolves the central claude-klabauter root rather than the per-repo state dir."""
        central_root = tmp_path / "central"
        central_root.mkdir()
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_c = _commit(repo, "c.txt", "c\n", "feat: c", "Session-Id: sess-c")

        handoff = central_root / "state" / "handoffs" / "h1.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            "---\nworkstream: mychain\nclaimed_by: sess-c\n---\nbody\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            "coordinator_core.meta_repo_identity.is_meta_repo", lambda _root: True
        )
        monkeypatch.setattr(
            "coordinator_core.engine_root.coordinator_engine_root",
            lambda: str(central_root),
        )

        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert set(resolved["chain_session_ids"]) == {"sess-a", "sess-c"}
        assert set(resolved["delta_shorts"]) == {sha_a, sha_c}

    def test_meta_repo_resolution_exception_falls_back_and_warns(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 1 + Finding 6: is_meta_repo raising falls back to the per-repo
        state dir AND surfaces a diagnostic in the returned warnings list."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")

        monkeypatch.setattr(
            "coordinator_core.meta_repo_identity.is_meta_repo",
            lambda _root: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        # A per-repo handoff exists — should still be found via the fallback dir.
        handoff = repo / "state" / "handoffs" / "h1.md"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text(
            "---\nworkstream: mychain\nclaimed_by: sess-c\n---\nbody\n",
            encoding="utf-8",
        )
        sha_c = _commit(repo, "c.txt", "c\n", "feat: c", "Session-Id: sess-c")

        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["chain_session_ids"] == ["sess-a", "sess-c"]
        assert any("meta-repo" in w for w in resolved["warnings"])
        assert resolved["delta_shorts"] == [sha_a, sha_c]


class TestIdProvenanceMismatch:
    """delta_count==0 AND session_log empty, but range DOES carry a foreign trailer."""

    def test_provenance_mismatch_warns_without_blocking(self, repo: Path) -> None:
        _commit(repo, "x.txt", "x\n", "feat: x", "Session-Id: some-other-session")

        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["delta_count"] == 0
        assert resolved["delta_shorts"] == []
        assert resolved["provenance_warning"] is not None
        assert "id-provenance mismatch" in resolved["provenance_warning"]
        assert "sess-a" in resolved["provenance_warning"]

        # Full reconcile_completion_commits() call: no_op, no write, warning surfaced.
        result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo)
        )
        assert result["no_op"] is True
        assert result["appended"] == 0
        assert result["provenance_warning"] is not None
        assert "id-provenance mismatch" in result["provenance_warning"]
        assert "sess-a" in result["provenance_warning"]

    def test_zero_delta_no_foreign_trailer_no_warning(self, repo: Path) -> None:
        # No commits at all in range beyond the seed (no Session-Id: trailer anywhere).
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["delta_count"] == 0
        assert resolved["provenance_warning"] is None


class TestShaCanonicalization:
    """A short stored SHA must dedupe against the log's full SHA — no leakage."""

    def test_short_stored_sha_dedupes_against_full_log_sha(self, repo: Path) -> None:
        sha_a_short = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_a_full = _full_sha(repo, sha_a_short)

        # Entry already stores the SHORT sha; the op must canonicalize before diffing
        # against the full-SHA log line, or it will double-count this commit as delta.
        entry = _write_entry(
            repo, "archive/completed/entry.md", commits=[sha_a_short]
        )

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["delta_shorts"] == []
        assert resolved["delta_count"] == 0
        # Sanity: the full SHA really did resolve and differs textually from the short form.
        assert sha_a_full != sha_a_short
        assert sha_a_full.startswith(sha_a_short)

    def test_unstored_commit_still_appears_in_delta(self, repo: Path) -> None:
        sha_a_short = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b_short = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-a")

        entry = _write_entry(repo, "archive/completed/entry.md", commits=[sha_a_short])

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["delta_shorts"] == [sha_b_short]

    def test_bogus_stored_sha_warns_and_is_treated_unmatched(self, repo: Path) -> None:
        sha_a_short = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        entry = _write_entry(
            repo, "archive/completed/entry.md", commits=["deadbeef"]
        )

        resolved = resolve_chain_commits(repo, str(entry), "sess-a")

        assert resolved["delta_shorts"] == [sha_a_short]
        assert any("rev-parse failed" in w for w in resolved["warnings"])


class TestMergeBaseUnresolved:
    def test_merge_base_unresolved_is_nonblocking_noop(self, tmp_path: Path) -> None:
        # A repo with no origin/main ref at all.
        root = tmp_path / "bare_repo"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-q", "-m", "chore: seed")

        entry = _write_entry(root, "archive/completed/entry.md", commits=[])

        resolved = resolve_chain_commits(root, str(entry), "sess-a")
        assert resolved["merge_base_unresolved"] is True
        assert resolved["delta_shorts"] == []

        result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(root)
        )
        assert result["no_op"] is True
        assert result["merge_base_unresolved"] is True


class TestSessionIdValidation:
    def test_empty_session_id_raises(self, repo: Path) -> None:
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])
        with pytest.raises(ValueError):
            resolve_chain_commits(repo, str(entry), "")

    def test_null_literal_session_id_raises(self, repo: Path) -> None:
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])
        with pytest.raises(ValueError):
            resolve_chain_commits(repo, str(entry), "null")

    def test_non_allowlisted_session_id_raises(self, repo: Path) -> None:
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])
        with pytest.raises(ValueError):
            resolve_chain_commits(repo, str(entry), "sess;rm -rf /")

    def test_session_id_without_worktree_root_raises(self, tmp_path: Path) -> None:
        entry = _write_entry(tmp_path, "entry.md", commits=[])
        with pytest.raises(ValueError):
            reconcile_completion_commits(str(entry), session_id="sess-a")


class TestHandlerEndToEnd:
    """JSON-RPC handler wiring: session_id/chain_slug params threaded through,
    worktree_root derived from socket-authoritative repo_root (common_dir), and
    the invalid-session_id ValueError surfaces as the structured error shape."""

    def test_handler_resolves_chain_and_folds(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")
        (repo / "archive/completed").mkdir(parents=True, exist_ok=True)
        sibling = _write_entry(repo, "archive/completed/sibling.md", chain="mychain")
        sibling.write_text(
            sibling.read_text(encoding="utf-8").replace(
                "commits: []", "authored_by: sess-b\ncommits: []"
            ),
            encoding="utf-8",
        )
        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "sess-a"},
                repo_root=repo / ".git",
            )
        )

        assert "error" not in result
        assert result["appended"] == 2
        assert set(result["chain_session_ids"]) == {"sess-a", "sess-b"}
        assert entry.read_text(encoding="utf-8").count(sha_a) == 1
        assert entry.read_text(encoding="utf-8").count(sha_b) == 1

    def test_handler_invalid_session_id_returns_error_shape(self, repo: Path) -> None:
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "null"},
                repo_root=repo / ".git",
            )
        )

        assert "error" in result
        assert result["no_op"] is True

    def test_handler_empty_string_session_id_fails_loud(self, repo: Path) -> None:
        """Finding 5: an explicitly-empty "session_id" must NOT be silently
        reinterpreted as "not given" (which would misroute onto the backward-compat
        pre-computed-commits path) — it must raise the same fail-loud error shape as
        an omitted-but-required session_id on the REOPENED path."""
        entry = _write_entry(repo, "archive/completed/entry.md", commits=["abc1234"])

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "", "commits": ["deadbeef"]},
                repo_root=repo / ".git",
            )
        )

        assert "error" in result
        assert result["no_op"] is True
        # The pre-computed-commits path must NOT have been silently taken.
        assert "deadbeef" not in entry.read_text(encoding="utf-8")


class TestDryRun:
    """dry_run mode: computes the delta, writes NOTHING. Mirrors the write-case
    tests above but asserts the plan file's content AND mtime are unchanged, and
    that the returned delta matches what the apply path would have appended."""

    def test_precomputed_commits_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        entry = _write_entry(tmp_path, "entry.md", commits=[])
        before_content = entry.read_text(encoding="utf-8")
        before_mtime = entry.stat().st_mtime_ns

        result = reconcile_completion_commits(
            str(entry), commits=["abc1234"], dry_run=True
        )

        assert result["appended"] == 0
        assert result["no_op"] is False
        assert result["dry_run"] is True
        assert result["delta_shorts"] == ["abc1234"]
        assert entry.read_text(encoding="utf-8") == before_content
        assert entry.stat().st_mtime_ns == before_mtime

        # Apply path with the same input actually appends 1 — confirms the
        # dry-run delta matches what the apply path would have folded in.
        applied = reconcile_completion_commits(str(entry), commits=["abc1234"])
        assert applied["appended"] == 1

    def test_session_id_chain_widening_dry_run_matches_apply_delta(
        self, repo: Path
    ) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        sha_b = _commit(repo, "b.txt", "b\n", "feat: b", "Session-Id: sess-b")
        sibling = _write_entry(repo, "archive/completed/sibling.md", chain="mychain")
        sibling.write_text(
            sibling.read_text(encoding="utf-8").replace(
                "commits: []", "authored_by: sess-b\ncommits: []"
            ),
            encoding="utf-8",
        )
        entry = _write_entry(repo, "archive/completed/entry.md", chain="mychain", commits=[])
        before_content = entry.read_text(encoding="utf-8")
        before_mtime = entry.stat().st_mtime_ns

        dry = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo), dry_run=True
        )

        assert dry["appended"] == 0
        assert dry["no_op"] is False
        assert dry["dry_run"] is True
        assert set(dry["delta_shorts"]) == {sha_a, sha_b}
        assert entry.read_text(encoding="utf-8") == before_content
        assert entry.stat().st_mtime_ns == before_mtime

        applied = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo)
        )
        assert applied["appended"] == 2
        assert set(applied["delta_shorts"]) == set(dry["delta_shorts"])

    def test_already_present_commits_dry_run_is_noop(self, tmp_path: Path) -> None:
        entry = _write_entry(tmp_path, "entry.md", commits=["aaa1111"])
        before_content = entry.read_text(encoding="utf-8")
        before_mtime = entry.stat().st_mtime_ns

        result = reconcile_completion_commits(
            str(entry), commits=["aaa1111"], dry_run=True
        )

        assert result["appended"] == 0
        assert result["no_op"] is True
        assert result["dry_run"] is True
        assert result["delta_shorts"] == []
        assert entry.read_text(encoding="utf-8") == before_content
        assert entry.stat().st_mtime_ns == before_mtime


class TestDryRunReturnShapeInvariant:
    """The two early-return no-ops (merge_base_unresolved, delta_count == 0) MUST
    return the SAME payload shape under dry_run True and False — DoE's facade
    extracts .no_op/.appended/.merge_base_unresolved via jq; a shape desync
    between modes would break it (both branches precede the dry_run gate, so
    this is really an assertion that the gate placement didn't leak)."""

    def test_merge_base_unresolved_shape_matches_across_dry_run(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "bare_repo"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-q", "-m", "chore: seed")

        entry = _write_entry(root, "archive/completed/entry.md", commits=[])

        apply_result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(root), dry_run=False
        )
        dry_result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(root), dry_run=True
        )

        assert apply_result["merge_base_unresolved"] is True
        assert dry_result["merge_base_unresolved"] is True
        apply_keys = set(apply_result.keys()) - {"dry_run"}
        dry_keys = set(dry_result.keys()) - {"dry_run"}
        assert apply_keys == dry_keys
        for key in apply_keys:
            assert apply_result[key] == dry_result[key], (
                f"key {key!r} diverged between dry_run modes: "
                f"apply={apply_result[key]!r} dry={dry_result[key]!r}"
            )
        assert apply_result["dry_run"] is False
        assert dry_result["dry_run"] is True

    def test_zero_delta_shape_matches_across_dry_run(self, repo: Path) -> None:
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        apply_result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo), dry_run=False
        )
        dry_result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo), dry_run=True
        )

        assert apply_result["no_op"] is True
        assert dry_result["no_op"] is True
        apply_keys = set(apply_result.keys()) - {"dry_run"}
        dry_keys = set(dry_result.keys()) - {"dry_run"}
        assert apply_keys == dry_keys
        for key in apply_keys:
            assert apply_result[key] == dry_result[key], (
                f"key {key!r} diverged between dry_run modes: "
                f"apply={apply_result[key]!r} dry={dry_result[key]!r}"
            )
        assert apply_result["dry_run"] is False
        assert dry_result["dry_run"] is True


class TestHandlerDryRun:
    """Handler-level dry_run threading: param coercion + no-write guarantee."""

    def test_handler_dry_run_true_writes_nothing(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])
        before_content = entry.read_text(encoding="utf-8")

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "sess-a", "dry_run": True},
                repo_root=repo / ".git",
            )
        )

        assert "error" not in result
        assert result["appended"] == 0
        assert result["dry_run"] is True
        assert result["delta_shorts"] == [sha_a]
        assert entry.read_text(encoding="utf-8") == before_content

    def test_handler_dry_run_default_false_still_writes(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "sess-a"},
                repo_root=repo / ".git",
            )
        )

        assert "error" not in result
        assert result["appended"] == 1
        assert result["dry_run"] is False
        assert entry.read_text(encoding="utf-8").count(sha_a) == 1

    def test_handler_dry_run_non_bool_coerces_to_true(self, repo: Path) -> None:
        """A non-bool dry_run value fails CONSERVATIVE — coerces to True (no
        mutation), never to False (which would silently authorize a write)."""
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])
        before_content = entry.read_text(encoding="utf-8")

        result = _run(
            _reconcile_commits_handler(
                {"plan_path": str(entry), "session_id": "sess-a", "dry_run": "yes"},
                repo_root=repo / ".git",
            )
        )

        assert "error" not in result
        assert result["dry_run"] is True
        assert result["appended"] == 0
        assert entry.read_text(encoding="utf-8") == before_content


class TestLockedRmwAdoption:
    """D2b (DR-216 § D2(vi), AMENDED 2026-08-06): the write pass now runs under
    ``locked_write.locked_rmw``, keyed to the target path — asserts the sidecar
    lock file is actually created under the resolved repo's git common dir."""

    def test_session_id_apply_creates_lock_sidecar(self, repo: Path) -> None:
        sha_a = _commit(repo, "a.txt", "a\n", "feat: a", "Session-Id: sess-a")
        entry = _write_entry(repo, "archive/completed/entry.md", commits=[])

        lock_dir = repo / ".git" / "coordinator-locks"
        assert not lock_dir.exists()

        result = reconcile_completion_commits(
            str(entry), session_id="sess-a", worktree_root=str(repo)
        )

        assert result["appended"] == 1
        assert lock_dir.is_dir()
        assert list(lock_dir.glob("*.lock")), (
            "expected a sidecar .lock file under the git common dir after a "
            "real (non-no-op) locked_rmw write"
        )

    def test_no_repo_root_falls_back_without_lock_sidecar(self, tmp_path: Path) -> None:
        """The backward-compat pre-computed-commits path, given a plan file with
        no resolvable git repo, must still write correctly via the documented
        no-repo fallback (no lock sidecar can exist — there is no repo to key one
        off of)."""
        entry = _write_entry(tmp_path, "entry.md", commits=[])

        result = reconcile_completion_commits(str(entry), commits=["abc1234"])

        assert result["appended"] == 1
        assert not (tmp_path / ".git").exists()

"""
coordinator_core.ops.tests.test_record_history — file-set resolution (C1a)
and single-pass git log derivation (C1b) tests.

Purpose: prove `record_history`'s file-set resolution — `type_directory_pathspec`,
`resolve_record_files`, `partition_known_files`, `untracked_record_paths` — is
exactly equal to `records_query`'s own collected set, and that the AC5a
git-`*`-crosses-`/` hazard cannot silently widen it. Also proves C1b's
`derive_type_history` parser holds under the four measured hazards it names:
wipe/restore pairs, hunk-position, inline comments, and renames — each
fixture below is a throwaway git repo, not a mock, matching a real event
this corpus measured.

Spec backlink: docs/plans/2026-08-20-a-time-axis-for-any-record-type.md
  § C1a (AC5, AC5a, AC5b), § C1b (AC1, AC3, AC4)

Coverage (C1a):
  (a) type_directory_pathspec — short fixed-prefix pathspec for a wildcard-filename
      glob (`decision`), a `**`-wildcard-dir glob (`handoff-archived`), and a
      no-wildcard glob (`tracker`)
  (b) resolve_record_files == records_query._collect_files, both directions
      (AC5 set-equality)
  (c) AC5a — a record file nested one level deeper than the glob's wildcard
      filename segment is EXCLUDED from the resolved set, pinning the git
      pathspec `*`-crosses-`/` hazard the module docstring names
  (d) unsupported type (synthetic + unknown) raises UnsupportedRecordTypeError
      naming the supported set
  (e) partition_known_files / untracked_record_paths — pure-Python post-filter
      and AC5b untracked-marker set arithmetic, no git/I-O involved

Coverage (C1b), each against a real throwaway git repo:
  (f) wipe/restore pair — no phantom transition, `created_at` pins the
      original add, not the restore (F2)
  (g) rename mid-history — the rename-chain's history is keyed on the
      current path and still carries the pre-rename `created_at` (F6)
  (h) a body-level field line past the frontmatter bound is excluded (F4)
  (i) a comment-only edit compares equal after stripping and is dropped (F5)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops import record_history, records_query
from coordinator_core.ops.record_history import (
    UnsupportedRecordTypeError,
    derive_across_roots,
    derive_type_history,
    partition_known_files,
    resolve_record_files,
    supported_record_types,
    type_directory_pathspec,
    untracked_record_paths,
)
from coordinator_core.win_portability import no_console_creationflags


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, env=run_env,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test Author")
    _git(repo, "config", "user.email", "test@example.com")


def _commit(repo: Path, message: str, date: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_COMMITTER_DATE": date,
    }
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(path: Path, body: str = "status: draft\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestTypeDirectoryPathspec:
    def test_wildcard_filename_glob(self):
        assert type_directory_pathspec("decision") == "docs/decisions"

    def test_double_star_wildcard_dir_glob(self):
        assert type_directory_pathspec("handoff-archived") == "archive/handoffs"

    def test_single_star_wildcard_dir_glob(self):
        assert type_directory_pathspec("roadmap") == "state/roadmap"

    def test_no_wildcard_glob_drops_filename(self):
        assert type_directory_pathspec("tracker") == "docs"

    def test_unsupported_type_raises(self):
        with pytest.raises(UnsupportedRecordTypeError):
            type_directory_pathspec("handoff-ledger")


class TestResolveRecordFiles:
    def test_matches_records_query_collect_files_exactly(self, tmp_path: Path):
        worktree = tmp_path
        _write(worktree / "docs" / "decisions" / "dr-1.md")
        _write(worktree / "docs" / "decisions" / "dr-2.md")
        _write(worktree / "docs" / "decisions" / "not-md.txt")

        resolved = resolve_record_files(worktree, "decision")
        oracle = frozenset(
            p.relative_to(worktree).as_posix()
            for p in records_query._collect_files(worktree, "decision")
        )

        assert resolved == oracle
        assert resolved == {"docs/decisions/dr-1.md", "docs/decisions/dr-2.md"}

    def test_wildcard_dir_type_matches_records_query(self, tmp_path: Path):
        worktree = tmp_path
        _write(worktree / "archive" / "handoffs" / "2026-01" / "h1.md")
        _write(worktree / "archive" / "handoffs" / "2026-02" / "h2.md")

        resolved = resolve_record_files(worktree, "handoff-archived")
        oracle = frozenset(
            p.relative_to(worktree).as_posix()
            for p in records_query._collect_files(worktree, "handoff-archived")
        )

        assert resolved == oracle
        assert resolved == {
            "archive/handoffs/2026-01/h1.md",
            "archive/handoffs/2026-02/h2.md",
        }

    def test_ac5a_nested_path_excluded(self, tmp_path: Path):
        """Git pathspec `*` crosses `/`; Python `glob` `*` does not (AC5a).

        A file one directory level deeper than `decision`'s glob
        (`docs/decisions/*.md`) would be matched by a naive `git log --
        docs/decisions/*.md` pathspec but must NOT appear in the resolved
        set, since the set is derived through the same walker
        `records_query` uses, not through git pathspec semantics.
        """
        worktree = tmp_path
        _write(worktree / "docs" / "decisions" / "dr-1.md")
        _write(worktree / "docs" / "decisions" / "nested" / "dr-2.md")

        resolved = resolve_record_files(worktree, "decision")

        assert resolved == {"docs/decisions/dr-1.md"}
        assert "docs/decisions/nested/dr-2.md" not in resolved


class TestUnsupportedType:
    def test_synthetic_type_rejected(self):
        with pytest.raises(UnsupportedRecordTypeError) as exc_info:
            resolve_record_files(Path("."), "research-claim")
        assert "research-claim" in str(exc_info.value)
        assert "decision" in exc_info.value.supported

    def test_unknown_type_rejected(self):
        with pytest.raises(UnsupportedRecordTypeError):
            resolve_record_files(Path("."), "not-a-real-type")

    def test_supported_set_excludes_synthetic_types(self):
        supported = supported_record_types()
        assert "handoff-ledger" not in supported
        assert "research-claim" not in supported
        assert "decision" in supported
        assert "sizing-object" in supported


class TestPostFilter:
    def test_partition_known_files(self):
        known = frozenset({"docs/decisions/dr-1.md", "docs/decisions/dr-2.md"})
        candidates = [
            "docs/decisions/dr-1.md",
            "docs/decisions/nested/dr-2.md",
            "docs/decisions/dr-2.md",
        ]

        found, unknown = partition_known_files(candidates, known)

        assert found == ["docs/decisions/dr-1.md", "docs/decisions/dr-2.md"]
        assert unknown == ["docs/decisions/nested/dr-2.md"]

    def test_untracked_record_paths(self):
        """AC5b: an on-disk record git has never tracked reports as an
        explicit untracked marker set, distinct from a tracked-but-eventless
        record — this function supplies the set; the marker text itself is
        applied by the C1b history-assembly caller."""
        known = frozenset({"docs/decisions/dr-1.md", "docs/decisions/dr-untracked.md"})
        tracked = frozenset({"docs/decisions/dr-1.md"})

        untracked = untracked_record_paths(known, tracked)

        assert untracked == {"docs/decisions/dr-untracked.md"}

    def test_untracked_record_paths_empty_when_fully_tracked(self):
        known = frozenset({"docs/decisions/dr-1.md"})
        tracked = frozenset({"docs/decisions/dr-1.md"})

        assert untracked_record_paths(known, tracked) == frozenset()


def _decision_body(status: str, extra_lines: int = 0, decoy_status: str | None = None) -> str:
    """A minimal decision-record body: short frontmatter (`status:` inside
    the ≤60-line bound) plus optional padding and a decoy body-level
    `status:` line placed well past line 60 — used by the hunk-position
    fixture (F4)."""
    lines = [
        "---",
        f"status: {status}",
        "---",
        "",
        "# Decision",
        "",
    ]
    lines.extend(f"padding line {i}" for i in range(extra_lines))
    if decoy_status is not None:
        lines.append(f"status: {decoy_status}")
        lines.append("")
    return "\n".join(lines) + "\n"


class TestDeriveTypeHistoryWipeRestore:
    """F2: a whole-tree wipe/restore pair must not read as a phantom
    transition, and `created_at` must pin the original add, not the
    restore."""

    def test_wipe_restore_pair_yields_no_phantom_transition(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "docs" / "decisions" / "dr-1.md"
        record.parent.mkdir(parents=True)

        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, "add dr-1", "2026-01-01T00:00:00+00:00")

        record.unlink()
        _commit(repo, "wipe", "2026-01-02T00:00:00+00:00")

        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, "restore", "2026-01-03T00:00:00+00:00")

        history = derive_type_history(repo, "decision")
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-1.md")

        assert entry["events"] == []
        assert entry["created_at"].startswith("2026-01-01")


class TestDeriveTypeHistoryRename:
    """F6: a rename mid-history keeps the file's history keyed on the
    current path, carrying the pre-rename `created_at` and any transition
    made in the same commit as the rename."""

    def test_rename_keeps_created_at_and_records_same_commit_transition(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        original = repo / "docs" / "decisions" / "dr-250-old-name.md"
        renamed = repo / "docs" / "decisions" / "dr-273-new-name.md"
        original.parent.mkdir(parents=True)

        original.write_text(_decision_body("proposed"), encoding="utf-8")
        _commit(repo, "add dr-250", "2026-01-01T00:00:00+00:00")

        _git(repo, "mv", str(original.relative_to(repo)), str(renamed.relative_to(repo)))
        renamed.write_text(_decision_body("accepted"), encoding="utf-8")
        _commit(repo, "rename and accept", "2026-01-05T00:00:00+00:00")

        history = derive_type_history(repo, "decision")
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-273-new-name.md")

        assert entry["created_at"].startswith("2026-01-01")
        assert len(entry["events"]) == 1
        assert entry["events"][0]["changes"]["status"] == {"from": "proposed", "to": "accepted"}


class TestDeriveTypeHistoryHunkPosition:
    """F4: a `status:` line below the frontmatter bound (measured: some land
    below file line 30 in this corpus) must not read as a transition."""

    def test_body_level_status_line_excluded(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "docs" / "decisions" / "dr-1.md"
        record.parent.mkdir(parents=True)

        record.write_text(
            _decision_body("draft", extra_lines=80, decoy_status="example-a"), encoding="utf-8",
        )
        _commit(repo, "add dr-1 with decoy body status", "2026-01-01T00:00:00+00:00")

        record.write_text(
            _decision_body("draft", extra_lines=80, decoy_status="example-b"), encoding="utf-8",
        )
        _commit(repo, "edit only the decoy body status", "2026-01-02T00:00:00+00:00")

        history = derive_type_history(repo, "decision")
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-1.md")

        assert entry["events"] == []


class TestDeriveTypeHistoryCommentOnlyEdit:
    """F5: a trailing-comment-only edit compares equal after stripping and
    must not read as a transition."""

    def test_comment_only_edit_dropped(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "state" / "sizings" / "sz-1.yaml"
        record.parent.mkdir(parents=True)

        record.write_text(
            "status: sized  # draft | sized | routed | shipped\n", encoding="utf-8",
        )
        _commit(repo, "add sz-1", "2026-01-01T00:00:00+00:00")

        record.write_text(
            "status: sized  # draft | sized | routed | shipped | declined\n", encoding="utf-8",
        )
        _commit(repo, "edit comment only", "2026-01-02T00:00:00+00:00")

        history = derive_type_history(repo, "sizing-object")
        entry = next(e for e in history if e["path"] == "state/sizings/sz-1.yaml")

        assert entry["events"] == []

    def test_real_transition_still_recorded_alongside_comment_edit(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "state" / "sizings" / "sz-1.yaml"
        record.parent.mkdir(parents=True)

        record.write_text(
            "status: draft  # draft | sized | routed\n", encoding="utf-8",
        )
        _commit(repo, "add sz-1", "2026-01-01T00:00:00+00:00")

        record.write_text(
            "status: routed  # draft | sized | routed | shipped\n", encoding="utf-8",
        )
        _commit(repo, "route it", "2026-01-02T00:00:00+00:00")

        history = derive_type_history(repo, "sizing-object")
        entry = next(e for e in history if e["path"] == "state/sizings/sz-1.yaml")

        assert len(entry["events"]) == 1
        assert entry["events"][0]["changes"]["status"] == {"from": "draft", "to": "routed"}


class TestDeriveTypeHistorySpawnCount:
    """A single git invocation per call (AC2's per-call half; C4 owns the
    O(pathspecs)-not-O(records) cross-corpus proof)."""

    def test_single_git_spawn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "docs" / "decisions" / "dr-1.md"
        record.parent.mkdir(parents=True)
        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, "add dr-1", "2026-01-01T00:00:00+00:00")

        spawn_count = 0
        real_run = subprocess.run

        def _counting_run(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _counting_run)

        derive_type_history(repo, "decision")

        assert spawn_count == 1


def _build_decision_corpus(repo: Path, n_files: int) -> None:
    """A throwaway repo with `n_files` committed `decision` records, one
    commit per file so the git history isn't trivially collapsed to a
    single commit regardless of corpus size."""
    _init_repo(repo)
    for i in range(n_files):
        record = repo / "docs" / "decisions" / f"dr-{i}.md"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, f"add dr-{i}", f"2026-01-01T00:{i % 60:02d}:00+00:00")


class TestDeriveTypeHistorySpawnCountIsCorpusInvariant:
    """C4 leg (b): the actual O(1)-spawn proof (AC2 as restated by F8).

    A constant bound alone ("spawn count is small") does not prove O(1) --
    only an EQUAL spawn count across two materially different corpus sizes
    does, since a bound could still scale sub-linearly-but-not-constant and
    still look small on both ends. `subprocess.Popen` is wrapped in the
    `subprocess` module itself (not a `record_history`-local attribute), so
    a spawn routed through `os.popen`/`os.system`/a re-import would still be
    caught rather than silently missed by a narrower stub.

    Budget: 1 spawn per pathspec (`derive_type_history` issues exactly one
    `git log` pass per call, per AC2) -- no unexplained slack. No
    `rev-parse`/`show-toplevel` preamble exists in this call path today; if
    one is ever added, the budget below must be named explicitly rather
    than grown silently.
    """

    def _spawn_count_for_corpus(
        self,
        tmp_path: Path,
        n_files: int,
        monkeypatch: pytest.MonkeyPatch,
        real_popen_init,
    ) -> int:
        repo = tmp_path / f"repo-{n_files}"
        _build_decision_corpus(repo, n_files)

        spawn_count = 0

        def _counting_init(self, *args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return real_popen_init(self, *args, **kwargs)

        monkeypatch.setattr(subprocess.Popen, "__init__", _counting_init)
        try:
            derive_type_history(repo, "decision")
        finally:
            monkeypatch.undo()

        return spawn_count

    def test_spawn_count_equal_across_differently_sized_corpora(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Captured once, ahead of either patch, so the second call's
        # "original" is the real `Popen.__init__` -- never the first call's
        # already-wrapped counting shim (which would double-count).
        real_popen_init = subprocess.Popen.__init__

        small_count = self._spawn_count_for_corpus(tmp_path, 2, monkeypatch, real_popen_init)
        large_count = self._spawn_count_for_corpus(tmp_path, 200, monkeypatch, real_popen_init)

        assert small_count == 1
        assert large_count == 1
        assert small_count == large_count


class TestDeriveAcrossRoots:
    """C5 (AC9/AC10): multi-root labelling, and the non-worktree-root SKIP
    path staff-eng F11 named -- a registered root that IS a directory but is
    NOT a git worktree must be counted SKIPPED, never as an empty walked
    repo (which would make `queried_root_count` over-report)."""

    def test_queried_root_count_equals_roots_actually_walked(self, tmp_path: Path):
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        for repo in (repo_a, repo_b):
            _init_repo(repo)
            record = repo / "docs" / "decisions" / "dr-1.md"
            record.parent.mkdir(parents=True)
            record.write_text(_decision_body("draft"), encoding="utf-8")
            _commit(repo, "add dr-1", "2026-01-01T00:00:00+00:00")

        non_worktree = tmp_path / "not-a-repo"
        non_worktree.mkdir()
        (non_worktree / "docs").mkdir()

        result = derive_across_roots([repo_a, repo_b, non_worktree], "decision")

        assert result["queried_root_count"] == 2
        assert result["queried_root_count"] == len(result["roots_walked"])
        assert set(result["roots_walked"]) == {repo_a.as_posix(), repo_b.as_posix()}
        assert len(result["roots_skipped"]) == 1
        assert result["roots_skipped"][0]["root"] == non_worktree.as_posix()
        assert repo_a.as_posix() in result["repos"]
        assert non_worktree.as_posix() not in result["repos"]

    def test_all_roots_skipped_reports_zero_queried(self, tmp_path: Path):
        non_worktree_a = tmp_path / "plain-a"
        non_worktree_b = tmp_path / "plain-b"
        non_worktree_a.mkdir()
        non_worktree_b.mkdir()

        result = derive_across_roots([non_worktree_a, non_worktree_b], "decision")

        assert result["queried_root_count"] == 0
        assert result["roots_walked"] == []
        assert len(result["roots_skipped"]) == 2
        assert result["repos"] == {}


def _build_two_type_corpus(repo: Path) -> None:
    """One `decision` and one `sizing-object` record, each with a real
    `status` transition, for the multi-type widening tests (P083-C4)."""
    _init_repo(repo)
    decision = repo / "docs" / "decisions" / "dr-1.md"
    decision.parent.mkdir(parents=True)
    decision.write_text(_decision_body("draft"), encoding="utf-8")
    sizing = repo / "state" / "sizings" / "sz-1.yaml"
    sizing.parent.mkdir(parents=True)
    sizing.write_text("status: draft\n", encoding="utf-8")
    _commit(repo, "add dr-1 and sz-1", "2026-01-01T00:00:00+00:00")

    decision.write_text(_decision_body("accepted"), encoding="utf-8")
    sizing.write_text("status: sized\n", encoding="utf-8")
    _commit(repo, "advance both", "2026-01-02T00:00:00+00:00")


class TestDeriveTypeHistoryMultiType:
    """P083-C4: one `records.history`-shaped call for several types."""

    def test_single_type_call_keeps_existing_shape_byte_for_byte(self, tmp_path: Path):
        """R3 backward compatibility: a bare string keeps the singular shape
        -- no `record_type` key on each record."""
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)

        history = derive_type_history(repo, "decision")

        assert all("record_type" not in entry for entry in history)
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-1.md")
        assert entry["events"][0]["changes"]["status"] == {"from": "draft", "to": "accepted"}

    def test_sequence_call_returns_both_types_with_per_record_attribution(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)

        history = derive_type_history(repo, ["decision", "sizing-object"])
        by_path = {e["path"]: e for e in history}

        assert by_path["docs/decisions/dr-1.md"]["record_type"] == "decision"
        assert by_path["state/sizings/sz-1.yaml"]["record_type"] == "sizing-object"
        assert by_path["docs/decisions/dr-1.md"]["events"][0]["changes"]["status"] == {
            "from": "draft", "to": "accepted",
        }
        assert by_path["state/sizings/sz-1.yaml"]["events"][0]["changes"]["status"] == {
            "from": "draft", "to": "sized",
        }

    def test_field_policy_applied_per_owning_type_not_once_for_the_call(self, tmp_path: Path):
        """`_FIELD_POLICY` gates on the file's OWN type, never the call's
        first type -- a `decision`-only field change must not be dropped by
        `sizing-object`'s policy tuple or vice versa (both share `status`
        here, so this proves attribution stays correct even where the
        tracked field name collides)."""
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)

        history = derive_type_history(repo, ["decision", "sizing-object"])
        by_path = {e["path"]: e for e in history}

        assert by_path["docs/decisions/dr-1.md"]["events"]
        assert by_path["state/sizings/sz-1.yaml"]["events"]

    def test_unsupported_member_names_supported_set(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        with pytest.raises(UnsupportedRecordTypeError) as exc_info:
            derive_type_history(repo, ["decision", "not-a-real-type"])
        assert "not-a-real-type" in str(exc_info.value)
        assert "decision" in exc_info.value.supported

    def test_single_git_spawn_for_two_types(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The point of the ask (C4 body): a two-type call costs ONE walk,
        not one per type -- measured, not asserted, against the baseline of
        two single-type calls."""
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)

        spawn_count = 0
        real_run = subprocess.run

        def _counting_run(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _counting_run)

        derive_type_history(repo, ["decision", "sizing-object"])

        assert spawn_count == 1

        # Baseline: two single-type calls cost two spawns -- the widened
        # call must be strictly cheaper, never merely "also small".
        spawn_count = 0
        derive_type_history(repo, "decision")
        derive_type_history(repo, "sizing-object")
        assert spawn_count == 2


class TestDeriveTypeHistorySince:
    """P083-C4 R2: `since` bounds EVENTS only, never the walk or
    `created_at`/`untracked` classification."""

    def test_since_excludes_pre_window_event_but_keeps_created_at(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "docs" / "decisions" / "dr-1.md"
        record.parent.mkdir(parents=True)

        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, "add dr-1", "2026-01-01T00:00:00+00:00")

        record.write_text(_decision_body("accepted"), encoding="utf-8")
        _commit(repo, "advance", "2026-01-02T00:00:00+00:00")

        history = derive_type_history(repo, "decision", since="2026-01-05")
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-1.md")

        # The event predates the window -- excluded.
        assert entry["events"] == []
        # But created_at is a whole-history fact -- NOT bounded by `since`,
        # and NOT nulled the way a truncated walk would null it.
        assert entry["created_at"].startswith("2026-01-01")

    def test_since_keeps_in_window_event(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        record = repo / "docs" / "decisions" / "dr-1.md"
        record.parent.mkdir(parents=True)

        record.write_text(_decision_body("draft"), encoding="utf-8")
        _commit(repo, "add dr-1", "2026-01-01T00:00:00+00:00")

        record.write_text(_decision_body("accepted"), encoding="utf-8")
        _commit(repo, "advance", "2026-01-10T00:00:00+00:00")

        history = derive_type_history(repo, "decision", since="2026-01-05")
        entry = next(e for e in history if e["path"] == "docs/decisions/dr-1.md")

        assert len(entry["events"]) == 1
        assert entry["events"][0]["changes"]["status"] == {"from": "draft", "to": "accepted"}


class TestRecordsHistoryOp:
    """`records.history`'s envelope shape, both arms (P083-C4)."""

    def test_single_type_envelope_unchanged(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)

        result = record_history._records_history({"record_type": "decision", "root": str(repo)})

        assert result["record_type"] == "decision"
        assert isinstance(result["untracked"], list)
        assert all("record_type" not in r for r in result["records"])

    def test_multi_type_envelope_groups_untracked_per_type(self, tmp_path: Path):
        repo = tmp_path / "repo"
        _build_two_type_corpus(repo)
        # An untracked (uncommitted) sizing-object record.
        untracked_file = repo / "state" / "sizings" / "sz-untracked.yaml"
        untracked_file.write_text("status: draft\n", encoding="utf-8")

        result = record_history._records_history(
            {"record_type": ["decision", "sizing-object"], "root": str(repo)},
        )

        assert result["record_type"] == ["decision", "sizing-object"]
        assert set(result["untracked"]) == {"decision", "sizing-object"}
        assert result["untracked"]["decision"] == []
        assert "state/sizings/sz-untracked.yaml" in result["untracked"]["sizing-object"]
        assert {r["record_type"] for r in result["records"]} == {"decision", "sizing-object"}

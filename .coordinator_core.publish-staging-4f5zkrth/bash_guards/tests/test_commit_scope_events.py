"""Forward instrumentation for commit-scope sweeps — the per-commit-attempt
JSONL emitted from Check 5 of ``check_validate_commit``.

Spec backlink: state/sizings/2026-08-04-forward-instrumentation-for-commit-scope.yaml

Every row runs against a real, isolated ``tmp_path`` git repo — never the
project-makima checkout, whose index carries whatever the live session
happens to have staged (see test_git_commit_safe_commit_deny_escalation.py's
header for the same constraint stated at length).

Negative-spec: this module asserts the OBSERVATION surface only — that a
record is written, that its shape distinguishes a scoped commit from an
unscoped one, that the write is bounded, and above all that a broken write
path cannot change a verdict. It does NOT re-assert Check 5's warn/deny
table (test_check_validate_commit.py owns that), and it never reads
``scope-warnings.log``.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import commit_scope_events

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SID = "sess-instr"


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _session_dir(repo, sid=SID):
    return repo / ".git" / "coordinator-sessions" / sid


def _init_session(repo, sid=SID):
    """A session dir whose ``started_at`` is an hour in the FUTURE, so
    compute_scope's mtime fallback does not auto-adopt a freshly-staged file
    into this session's own scope (the pattern
    test_check_validate_commit.py's `_push_started_at_to_future` establishes)."""
    from coordinator_core.session import core

    assert core.init(sid, cwd=str(repo))
    sdir = _session_dir(repo, sid)
    future = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + 3600, tz=timezone.utc
    )
    (sdir / "started_at").write_text(
        future.strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )
    return sdir


def _stage(repo, name, content="x"):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def _verdict(cmd, repo):
    out = dispatch_checks.check_validate_commit(cmd, SID, str(repo))
    if out is None:
        return "none"
    decision = out["hookSpecificOutput"]["permissionDecision"]
    return "deny" if decision == "deny" else "advisory"


def _events(repo, sid=SID):
    path = commit_scope_events.events_path(_session_dir(repo, sid))
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _commit(flags='-m "x"'):
    return "git commit %s" % flags


class TestEventIsWritten:
    def test_unscoped_attempt_writes_one_record_of_expected_shape(self, tmp_path):
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")

        assert _verdict(_commit(), repo) == "advisory"

        records = _events(repo)
        assert len(records) == 1
        rec = records[0]
        assert rec["schema"] == commit_scope_events.SCHEMA
        assert rec["session_id"] == SID
        assert rec["staged"] == ["foreign.txt"]
        assert rec["warned_paths"] == ["foreign.txt"]
        assert rec["pathspec_scoped"] is False
        assert rec["sweep_all"] is False
        assert rec["verdict"] == "advisory"
        assert isinstance(rec["attribution"], dict)
        assert rec["timestamp"].endswith("+00:00")

    def test_exactly_one_record_per_attempt(self, tmp_path):
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")
        for _ in range(3):
            _verdict(_commit(), repo)
        assert len(_events(repo)) == 3

    def test_no_record_without_a_session_dir(self, tmp_path):
        """The JSONL lives inside the session dir; with no session dir there is
        nowhere to write and Check 5 does not run at all."""
        repo = _init_repo(tmp_path)
        _stage(repo, "foreign.txt")
        _verdict(_commit(), repo)
        assert _events(repo) == []


class TestScopedVsUnscopedAreDistinguishable:
    """The whole point of the record: a warning on a pathspec-scoped commit is
    not a sweep, and the two must be separable after the fact."""

    def test_scoped_and_unscoped_records_differ_on_pathspec_scoped(self, tmp_path):
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")

        _verdict(_commit(), repo)
        _verdict(_commit('-m "x" -- foreign.txt'), repo)

        unscoped, scoped = _events(repo)
        assert unscoped["pathspec_scoped"] is False
        assert scoped["pathspec_scoped"] is True
        # Both warned — pathspec presence never suppresses the warning; only
        # the sweep-candidate classification differs.
        assert unscoped["warned_paths"] == ["foreign.txt"]
        assert scoped["warned_paths"] == ["foreign.txt"]

    def test_sweep_all_flag_is_recorded(self, tmp_path):
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")
        _verdict(_commit('-am "x"'), repo)
        assert _events(repo)[0]["sweep_all"] is True


class TestFailOpen:
    """The most important row in this module: instrumentation must never be
    able to block, deny, or alter a commit decision."""

    def test_unwritable_events_path_does_not_change_verdict_or_raise(self, tmp_path):
        repo = _init_repo(tmp_path)
        sdir = _init_session(repo)
        _stage(repo, "foreign.txt")

        baseline = _verdict(_commit(), repo)
        commit_scope_events.events_path(sdir).unlink()

        # A DIRECTORY where the JSONL belongs: every read/write attempt raises
        # IsADirectoryError/PermissionError from inside the write path.
        (sdir / commit_scope_events.EVENTS_FILENAME).mkdir()

        assert _verdict(_commit(), repo) == baseline == "advisory"

    def test_record_commit_attempt_swallows_every_failure(self, tmp_path):
        """Direct-call fail-open: a bogus session dir, a bogus repo root, and
        an unserializable attribution value must each return None quietly."""
        assert (
            commit_scope_events.record_commit_attempt(
                session_id=SID,
                session_dir=tmp_path / "does" / "not" / "exist",
                repo_root=tmp_path / "not-a-repo",
                staged=["a"],
                warned_paths=["a"],
                attribution={},
                pathspec_scoped=False,
                sweep_all=False,
                verdict="advisory",
            )
            is None
        )

    def test_raising_writer_does_not_change_verdict(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")

        def _boom(**_kw):
            raise RuntimeError("instrumentation exploded")

        monkeypatch.setattr(commit_scope_events, "record_commit_attempt", _boom)
        assert _verdict(_commit(), repo) == "advisory"


class TestBound:
    def test_bounded_drops_oldest_past_max_records(self):
        text = "".join("line%d\n" % i for i in range(commit_scope_events.MAX_RECORDS + 50))
        out = commit_scope_events._bounded(text, "newest")
        lines = out.splitlines()
        assert len(lines) == commit_scope_events.MAX_RECORDS
        assert lines[-1] == "newest"
        assert "line0" not in lines

    def test_bounded_drops_oldest_past_max_bytes(self):
        big = "y" * 40000
        text = "".join(big + "\n" for _ in range(20))  # 800 KB, over MAX_BYTES
        out = commit_scope_events._bounded(text, "newest")
        assert len(out) <= commit_scope_events.MAX_BYTES
        assert out.splitlines()[-1] == "newest"

    def test_newest_record_survives_even_if_alone_it_exceeds_max_bytes(self):
        out = commit_scope_events._bounded("old\n", "z" * (commit_scope_events.MAX_BYTES + 10))
        assert out.splitlines() == ["z" * (commit_scope_events.MAX_BYTES + 10)]

    def test_file_stays_bounded_across_many_appends(self, tmp_path):
        repo = _init_repo(tmp_path)
        sdir = _session_dir(repo)
        for i in range(commit_scope_events.MAX_RECORDS + 25):
            commit_scope_events.record_commit_attempt(
                session_id=SID,
                session_dir=sdir,
                repo_root=repo,
                staged=["f%d" % i],
                warned_paths=["f%d" % i],
                attribution={},
                pathspec_scoped=False,
                sweep_all=False,
                verdict="advisory",
            )
        records = _events(repo)
        assert len(records) == commit_scope_events.MAX_RECORDS
        assert records[-1]["staged"] == ["f%d" % (commit_scope_events.MAX_RECORDS + 24)]

    def test_staged_list_is_clipped_per_record(self):
        rec = commit_scope_events.build_record(
            session_id=SID,
            staged=["p%d" % i for i in range(commit_scope_events.MAX_STAGED_PATHS + 10)],
            warned_paths=[],
            attribution={},
            pathspec_scoped=False,
            sweep_all=False,
            verdict="none",
        )
        assert len(rec["staged"]) == commit_scope_events.MAX_STAGED_PATHS
        assert rec["staged_truncated"] is True


class TestRecordShapeInvariants:
    def test_attribution_is_projected_only_for_staged_paths(self):
        from coordinator_core.session.scope import OwnerFact

        rec = commit_scope_events.build_record(
            session_id=SID,
            staged=["kept.txt"],
            warned_paths=["kept.txt", "not-staged.txt"],
            attribution={
                "kept.txt": OwnerFact(owner="peer", liveness="live", claim_source="session"),
                "dropped.txt": OwnerFact(
                    owner="other", liveness="dead", claim_source="agent"
                ),
            },
            pathspec_scoped=True,
            sweep_all=False,
            verdict="advisory",
        )
        assert set(rec["attribution"]) == {"kept.txt"}
        assert rec["attribution"]["kept.txt"] == {
            "owner": "peer",
            "liveness": "live",
            "claim_source": "session",
        }
        assert rec["warned_paths"] == ["kept.txt"]

    def test_unknown_verdict_degrades_to_none(self):
        rec = commit_scope_events.build_record(
            session_id=SID,
            staged=[],
            warned_paths=[],
            attribution={},
            pathspec_scoped=False,
            sweep_all=False,
            verdict="banana",
        )
        assert rec["verdict"] == "none"


class TestAggregator:
    """The read-only reporter over the JSONL (coordinator/bin/
    commit-scope-report.py) — loaded by path since bin/ is not a package."""

    @staticmethod
    def _load():
        import importlib.util

        here = Path(__file__).resolve()
        repo_root = here.parents[3]
        path = repo_root / "coordinator" / "bin" / "commit-scope-report.py"
        spec = importlib.util.spec_from_file_location("commit_scope_report", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_separates_sweep_candidates_from_scoped_warnings(self, tmp_path):
        mod = self._load()
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")
        _verdict(_commit(), repo)                              # sweep candidate
        _verdict(_commit('-m "x" -- foreign.txt'), repo)     # FP proxy

        summary = mod.summarize(iter(_events(repo)))
        assert summary["total_attempts"] == 2
        assert summary["attempts_with_foreign_staged"] == 2
        assert summary["sweep_candidates"] == 1
        assert summary["warned_but_scoped"] == 1
        assert summary["sweep_rate"] == 0.5
        assert summary["fp_proxy_rate"] == 0.5
        assert mod.render(summary)

    def test_reads_repo_and_is_read_only(self, tmp_path):
        mod = self._load()
        repo = _init_repo(tmp_path)
        _init_session(repo)
        _stage(repo, "foreign.txt")
        _verdict(_commit(), repo)

        before = commit_scope_events.events_path(_session_dir(repo)).read_text(
            encoding="utf-8"
        )
        assert mod.main(["--repo", str(repo), "--json"]) == 0
        after = commit_scope_events.events_path(_session_dir(repo)).read_text(
            encoding="utf-8"
        )
        assert before == after

    def test_malformed_lines_are_counted_not_fatal(self):
        mod = self._load()
        summary = mod.summarize(iter([{"__malformed__": True}, {"nonsense": 1}]))
        assert summary["malformed_records"] == 2
        assert summary["total_attempts"] == 0

    def test_git_common_dir_resolves_linked_worktree_gitdir_file(self, tmp_path):
        """`_git_common_dir` reimplements git's `.git`-file/linked-worktree
        walk without a subprocess; construct the fixture directly rather than
        running `git worktree add` (banned at this fleet's tool seam)."""
        mod = self._load()

        common_dir = tmp_path / "main-repo" / ".git"
        common_dir.mkdir(parents=True)
        worktree_gitdir = common_dir / "worktrees" / "feature-branch"
        worktree_gitdir.mkdir(parents=True)

        linked_worktree = tmp_path / "feature-checkout"
        linked_worktree.mkdir()
        (linked_worktree / ".git").write_text(
            "gitdir: %s\n" % worktree_gitdir, encoding="utf-8"
        )

        resolved = mod._git_common_dir(linked_worktree)
        assert resolved == common_dir

    def test_git_common_dir_resolves_relative_gitdir_file(self, tmp_path):
        """The `gitdir:` pointer can be relative to the `.git`-file's own
        directory, per real git's `.git`-file format."""
        mod = self._load()

        common_dir = tmp_path / "main-repo" / ".git"
        common_dir.mkdir(parents=True)
        worktree_gitdir = common_dir / "worktrees" / "feature-branch"
        worktree_gitdir.mkdir(parents=True)

        linked_worktree = tmp_path / "main-repo" / "feature-checkout"
        linked_worktree.mkdir()
        rel = os.path.relpath(worktree_gitdir, linked_worktree)
        (linked_worktree / ".git").write_text("gitdir: %s\n" % rel, encoding="utf-8")

        resolved = mod._git_common_dir(linked_worktree)
        assert resolved == common_dir

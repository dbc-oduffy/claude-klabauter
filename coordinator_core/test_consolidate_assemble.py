"""Tests for coordinator_core.consolidate_assemble + coordinator_core.
consolidate_assemble.apply — the `/consolidate-git` computed skill (B4
chunk C8).

Scope: `brief()`'s compute-only outputs (branch/worktree ownership
categorization, unique-commit judgment evidence, absorb/delete directive
shape) and `apply()`'s composition of `apply_base` (closed dispatch table
resolution, session-id gating). Does NOT invoke a real git subprocess for a
directive handler — those are monkeypatched; `coordinator_core.contract.
test_apply_base` already covers the generic directive-execution engine this
module composes.

Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md, chunk C8
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble
from coordinator_core.consolidate_assemble import apply as consolidate_apply


def _git(returncode=0, stdout="", stderr=""):
    return lambda args, cwd: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _dispatch(rules):
    """Builds a `run_git` stub keyed on `args[0]` (the git subcommand)."""

    def _run(args, cwd):
        for prefix, result in rules.items():
            if args[: len(prefix)] == list(prefix):
                return result
        raise AssertionError(f"unexpected git call: {args}")

    return _run


# ---------------------------------------------------------------------------
# categorize_branch
# ---------------------------------------------------------------------------

class TestCategorizeBranch:
    def test_current_branch(self):
        assert consolidate_assemble.categorize_branch("a", "a", "main", "x@y", "x@y") == "current"

    def test_main_branch(self):
        assert consolidate_assemble.categorize_branch("main", "cur", "main", "x@y", "x@y") == "main"

    def test_mine_stale_when_owned(self):
        assert (
            consolidate_assemble.categorize_branch("b", "cur", "main", "me@x", "me@x") == "mine-stale"
        )

    def test_others_when_not_owned(self):
        assert (
            consolidate_assemble.categorize_branch("b", "cur", "main", "other@x", "me@x") == "others"
        )


# ---------------------------------------------------------------------------
# list_branches / list_worktrees parsing
# ---------------------------------------------------------------------------

class TestListBranches:
    def test_parses_local_and_remote_skips_head_alias(self):
        run_git = _git(
            stdout=(
                "* work/a\n"
                "  main\n"
                "  remotes/origin/main\n"
                "  remotes/origin/HEAD -> origin/main\n"
                "  remotes/origin/stale-remote\n"
            )
        )
        out = consolidate_assemble.list_branches(run_git, Path("/repo"))
        names = {b["name"] for b in out}
        assert names == {"work/a", "main", "stale-remote"}
        stale = next(b for b in out if b["name"] == "stale-remote")
        assert stale["is_local"] is False
        assert stale["is_remote"] is True
        assert stale["ref"] == "origin/stale-remote"


class TestListWorktrees:
    def test_parses_porcelain_blocks(self):
        stdout = (
            "worktree /repo\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo-wt\n"
            "HEAD def456\n"
            "branch refs/heads/feature/x\n"
            "locked\n"
        )
        run_git = _git(stdout=stdout)
        out = consolidate_assemble.list_worktrees(run_git, Path("/repo"))
        assert len(out) == 2
        assert out[0] == {"path": "/repo", "branch": "main", "head": "abc123", "locked": False}
        assert out[1]["path"] == "/repo-wt"
        assert out[1]["branch"] == "feature/x"
        assert out[1]["locked"] is True


# ---------------------------------------------------------------------------
# brief()
# ---------------------------------------------------------------------------

class TestBrief:
    def _stub(self, monkeypatch, *, branch_lines, worktree_stdout, unique_commits=None, tip_author="me@x"):
        unique_commits = unique_commits or []

        def run_git(args, cwd):
            if args[:2] == ["config", "user.email"]:
                return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
            if args[:2] == ["rev-parse", "--verify"]:
                ok = args[2] == "main"
                return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
            if args[0] == "branch":
                return SimpleNamespace(returncode=0, stdout=branch_lines, stderr="")
            if args[0] == "log" and args[1] == "-1":
                return SimpleNamespace(returncode=0, stdout=f"{tip_author}\n", stderr="")
            if args[0] == "log" and args[1] == "--oneline":
                return SimpleNamespace(
                    returncode=0, stdout="\n".join(unique_commits) + ("\n" if unique_commits else ""), stderr=""
                )
            if args[0] == "show":
                return SimpleNamespace(returncode=0, stdout="1 file changed\n", stderr="")
            if args[0] == "worktree" and args[1] == "list":
                return SimpleNamespace(returncode=0, stdout=worktree_stdout, stderr="")
            if args[0] == "merge-base":
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[0] == "status":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        monkeypatch.setattr(consolidate_assemble, "default_run_git", run_git)
        return run_git

    def test_decision_object_shape(self, monkeypatch, tmp_path):
        run_git = self._stub(
            monkeypatch,
            branch_lines="* current\n  main\n",
            worktree_stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n",
        )
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        assert set(do.keys()) == {
            "artifact",
            "preflight",
            "gates",
            "directives",
            "judgment_points",
            "decisions",
            "narration",
            "next_move",
        }

    def test_zero_unique_commits_yields_delete_only_directive_no_judgment(self, monkeypatch, tmp_path):
        run_git = self._stub(
            monkeypatch,
            branch_lines="* current\n  main\n  stale\n",
            worktree_stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n",
            unique_commits=[],
        )
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        assert {d["cli"] for d in do["directives"]} >= {"delete-only", "fetch-prune"}
        assert not any(jp["id"].startswith("j-absorb-") for jp in do["judgment_points"])

    def test_unique_commits_yield_absorb_judgment_point_recommendation_none(self, monkeypatch, tmp_path):
        run_git = self._stub(
            monkeypatch,
            branch_lines="* current\n  main\n  stale\n",
            worktree_stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n",
            unique_commits=["abc123 a commit"],
        )
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        absorb_jps = [jp for jp in do["judgment_points"] if jp["id"] == "j-absorb-stale"]
        assert len(absorb_jps) == 1
        assert absorb_jps[0]["recommendation"] is None
        cherry_pick_directives = [d for d in do["directives"] if d["id"] == "d-absorb-stale"]
        assert cherry_pick_directives[0]["cli"] == "cherry-pick-and-delete"
        assert cherry_pick_directives[0]["depends_on"] == "j-absorb-stale"

    def test_others_branch_never_appears_in_directives(self, monkeypatch, tmp_path):
        run_git = self._stub(
            monkeypatch,
            branch_lines="* current\n  main\n  someone-elses\n",
            worktree_stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n",
            tip_author="other@x",
        )
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        assert not any("someone-elses" in str(d) for d in do["directives"])
        branch_report = next(b for b in do["gates"]["branches"] if b["name"] == "someone-elses")
        assert branch_report["category"] == "others"

    def test_directives_are_well_formed_for_apply_base_ordering(self, monkeypatch, tmp_path):
        run_git = self._stub(
            monkeypatch,
            branch_lines="* current\n  main\n  stale\n",
            worktree_stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n",
            unique_commits=["abc123 a commit"],
        )
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        directive_ids = {d["id"] for d in do["directives"]}
        for d in do["directives"]:
            dep = d.get("depends_on")
            if isinstance(dep, str) and dep.startswith("d-"):
                assert dep in directive_ids


# ---------------------------------------------------------------------------
# apply() — dispatch-table composition (no real subprocess)
# ---------------------------------------------------------------------------

class TestApplyDispatchTable:
    def test_every_brief_directive_cli_resolves_in_the_closed_table(self, monkeypatch, tmp_path):
        run_git = _dispatch(
            {
                ("config", "user.email"): SimpleNamespace(returncode=0, stdout="me@x\n", stderr=""),
                ("rev-parse", "--abbrev-ref"): SimpleNamespace(returncode=0, stdout="current\n", stderr=""),
                ("rev-parse", "--verify"): SimpleNamespace(returncode=0, stdout="", stderr=""),
                ("branch",): SimpleNamespace(returncode=0, stdout="* current\n  main\n  stale\n", stderr=""),
                ("log", "-1"): SimpleNamespace(returncode=0, stdout="me@x\n", stderr=""),
                ("log", "--oneline"): SimpleNamespace(returncode=0, stdout="abc123 a commit\n", stderr=""),
                ("show",): SimpleNamespace(returncode=0, stdout="1 file changed\n", stderr=""),
                ("worktree", "list"): SimpleNamespace(
                    returncode=0, stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n", stderr=""
                ),
                ("merge-base",): SimpleNamespace(returncode=1, stdout="", stderr=""),
                ("status",): SimpleNamespace(returncode=0, stdout="", stderr=""),
            }
        )
        monkeypatch.setattr(consolidate_assemble, "default_run_git", run_git)
        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)
        for d in do["directives"]:
            assert d["cli"] in consolidate_apply._CLI_DISPATCH

    def test_no_session_id_is_transport_fail(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        exit_code, report = consolidate_apply.apply(repo_root=tmp_path)
        assert exit_code == consolidate_apply.APPLY_EXIT_TRANSPORT_FAIL
        assert "error" in report

    def test_apply_runs_end_to_end_with_stubbed_handlers(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_SESSION_ID", "test-session")

        run_git = _dispatch(
            {
                ("config", "user.email"): SimpleNamespace(returncode=0, stdout="me@x\n", stderr=""),
                ("rev-parse", "--abbrev-ref"): SimpleNamespace(returncode=0, stdout="current\n", stderr=""),
                ("rev-parse", "--verify"): SimpleNamespace(returncode=1, stdout="", stderr=""),
                ("branch",): SimpleNamespace(returncode=0, stdout="* current\n  stale\n", stderr=""),
                ("log", "-1"): SimpleNamespace(returncode=0, stdout="me@x\n", stderr=""),
                ("log", "--oneline"): SimpleNamespace(returncode=0, stdout="", stderr=""),
                ("worktree", "list"): SimpleNamespace(
                    returncode=0, stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n", stderr=""
                ),
                ("status",): SimpleNamespace(returncode=0, stdout="", stderr=""),
            }
        )
        monkeypatch.setattr(consolidate_assemble, "default_run_git", run_git)

        for name in list(consolidate_apply._CLI_DISPATCH):
            monkeypatch.setitem(
                consolidate_apply._CLI_DISPATCH, name, lambda args, repo_root, _n=name: {"cli": _n}
            )

        exit_code, report = consolidate_apply.apply(repo_root=tmp_path)
        assert exit_code == consolidate_apply.APPLY_EXIT_OK
        assert "d-delete-stale" in report["landed"]
        assert "d-fetch-prune" in report["landed"]


class TestDeleteBranchLocalLeg:
    """`brief` emits `delete-only` for remote-only branches too (`is_local:
    false`), so the local leg must be conditional — an unconditional `git
    branch -d` on one exits 1 and halts the whole apply."""

    def _recorder(self, local_exists: bool):
        calls: list[list[str]] = []

        def run_git(args, cwd):
            calls.append(list(args))
            if args[0] == "show-ref":
                return SimpleNamespace(returncode=0 if local_exists else 1, stdout="", stderr="")
            if args[0] == "branch" and not local_exists:
                return SimpleNamespace(returncode=1, stdout="", stderr="error: branch not found")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return calls, run_git

    def test_remote_only_branch_skips_local_delete(self, monkeypatch, tmp_path):
        calls, run_git = self._recorder(local_exists=False)
        monkeypatch.setattr(consolidate_apply, "_run_git", run_git)

        detail = consolidate_apply._delete_branch("remote-only", True, tmp_path)

        assert detail["local_deleted"] is None
        assert detail["remote_deleted"] == "remote-only"
        assert not any(c[0] == "branch" for c in calls)
        assert ["push", "origin", "--delete", "remote-only"] in calls

    def test_local_branch_still_deletes_locally(self, monkeypatch, tmp_path):
        calls, run_git = self._recorder(local_exists=True)
        monkeypatch.setattr(consolidate_apply, "_run_git", run_git)

        detail = consolidate_apply._delete_branch("has-local", False, tmp_path)

        assert detail["local_deleted"] == "has-local"
        assert ["branch", "-d", "has-local"] in calls

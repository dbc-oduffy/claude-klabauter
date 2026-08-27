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

Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C8
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble
from coordinator_core.consolidate_assemble import apply as consolidate_apply


def _git(returncode=0, stdout="", stderr=""):
    return lambda args, cwd: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _show_stdout(shas):
    """Mimics `git show --stat <sha>...`: concatenated per-commit blocks, each
    opening with a column-0 `commit <sha>` line and carrying a four-space
    indented message body."""
    blocks = [
        f"commit {sha}0000\nAuthor: me <me@x>\n\n    subject for {sha}\n\n f | 1 +\n" for sha in shas
    ]
    return "\n".join(blocks)


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
            if args == ["branch", "-a"]:
                return SimpleNamespace(returncode=0, stdout=branch_lines, stderr="")
            if args[0] == "for-each-ref":
                names = []
                for raw_line in branch_lines.splitlines():
                    line = raw_line.strip().lstrip("* ").strip()
                    if not line or "->" in line:
                        continue
                    names.append(line[len("remotes/"):] if line.startswith("remotes/") else line)
                out = "\n".join(f"{name} {tip_author}" for name in names)
                return SimpleNamespace(returncode=0, stdout=out + ("\n" if out else ""), stderr="")
            if args[0] == "branch" and args[1] == "--merged":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "log" and args[1] == "-1":
                return SimpleNamespace(returncode=0, stdout=f"{tip_author}\n", stderr="")
            if args[0] == "log" and args[1] == "--oneline":
                return SimpleNamespace(
                    returncode=0, stdout="\n".join(unique_commits) + ("\n" if unique_commits else ""), stderr=""
                )
            if args[0] == "show":
                shas = [a for a in args[2:]]
                return SimpleNamespace(returncode=0, stdout=_show_stdout(shas), stderr="")
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

    def test_inspection_gather_is_one_show_spawn_for_all_branches(self, monkeypatch, tmp_path):
        """PINS: one `git show --stat` spawn TOTAL for the whole `brief()`
        call, regardless of stale-branch count -- not one per stale branch.

        History: this test previously asserted `len(show_calls) ==
        len(stale)`, i.e. one spawn PER BRANCH (already a won collapse off
        an earlier per-COMMIT shape). That assertion could not tell a
        genuine global collapse apart from the per-branch shape it was
        pinning, because its fixture only ever exercised a single stale
        branch (`len(stale) == 1` in every case), so `len(show_calls) ==
        len(stale)` was compatible with either design. This version uses
        TWO stale branches with disjoint sha sets specifically so the two
        designs diverge: per-branch would spawn twice here, the global
        collapse spawns once. A git spawn is ~100ms on Windows (DoE
        spawn-cost memo, 2026-08-08); collapsing "per branch" to "per brief
        call" removes a second multiplier the same way the earlier
        per-commit collapse removed the first.

        Attribution argument (why this collapse is safe, unlike the sibling
        `unique_commits`/`branch_reachable` per-branch spawns this file
        keeps): a commit's `git show --stat` block is a pure function of
        that commit alone -- it does not vary with which other revs ride
        alongside it in the same argv, and is unaffected by which branch's
        `current..stale` range surfaced the sha. So `brief()` doesn't need
        `inspect_commits` to know about branches at all: it dedups every
        stale branch's shas into one flat argv, spawns once, and each
        branch fans back out by keying the one shared result dict on its
        own shas. This is asserted below by confirming BOTH branches'
        inspections resolve correct, distinct, byte-identical stat blocks
        from that single spawn.

        Also regression-pins a second leak the two-pass split introduced and
        this test's prior version did not catch: the absorb directive's
        `args` need `ref`, which the pass-1 loop binds (`name, ref =
        entry["name"], entry["ref"]`) but pass 2 does not automatically
        inherit -- an earlier version of the split read whatever `ref`
        pass 1's LAST iteration happened to leave bound, giving every
        branch's absorb directive the SAME (wrong-for-all-but-one) ref, a
        `pytest` pass with silently corrupt directives. `stale-b` here is
        REMOTE-ONLY (`ref == "origin/stale-b"`, distinct from both its own
        `name` and from `stale-a`'s local `ref == "stale-a"`), so a leaked
        single value cannot satisfy both branches' ref assertions below by
        coincidence."""
        calls = []

        def run_git(args, cwd):
            if args[:2] == ["config", "user.email"]:
                return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
            if args[:2] == ["rev-parse", "--verify"]:
                ok = args[3] == "main"
                return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
            if args == ["branch", "-a"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="* current\n  main\n  stale-a\n  remotes/origin/stale-b\n",
                    stderr="",
                )
            if args[0] == "for-each-ref":
                out = "current me@x\nmain me@x\nstale-a me@x\norigin/stale-b me@x\n"
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            if args[0] == "branch" and args[1] == "--merged":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "log" and args[1] == "-1":
                return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
            if args[0] == "log" and args[1] == "--oneline":
                ref = args[2].split("..", 1)[1]
                commits = {
                    "stale-a": ["aaa111 first"],
                    "origin/stale-b": ["bbb222 second", "ccc333 third"],
                }[ref]
                return SimpleNamespace(returncode=0, stdout="\n".join(commits) + "\n", stderr="")
            if args[0] == "show":
                return SimpleNamespace(returncode=0, stdout=_show_stdout(args[2:]), stderr="")
            if args[0] == "worktree" and args[1] == "list":
                return SimpleNamespace(
                    returncode=0, stdout=f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n", stderr=""
                )
            if args[0] == "merge-base":
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if args[0] == "status":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        def recording(args, cwd):
            calls.append(args)
            return run_git(args, cwd)

        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=recording)
        stale = [b for b in do["gates"]["branches"] if b["category"] == "mine-stale"]
        assert len(stale) == 2  # two branches feeding the one spawn below, not one

        show_calls = [args for args in calls if args[0] == "show"]
        assert len(show_calls) == 1
        assert show_calls[0] == ["show", "--stat", "aaa111", "bbb222", "ccc333"]

        jp_a = next(jp for jp in do["judgment_points"] if jp["id"] == "j-absorb-stale-a")
        jp_b = next(jp for jp in do["judgment_points"] if jp["id"] == "j-absorb-stale-b")
        inspections_a = jp_a["evidence"]["inspections"]
        inspections_b = jp_b["evidence"]["inspections"]
        assert [i["sha"] for i in inspections_a] == ["aaa111"]
        assert [i["sha"] for i in inspections_b] == ["bbb222", "ccc333"]
        # Each block is byte-identical to that commit's own `git show --stat` -- the batch
        # separator git emits BETWEEN objects never bleeds into a neighboring commit's evidence,
        # and the fan-out from the one shared spawn attributes each block to the right branch.
        for i in inspections_a + inspections_b:
            assert i["stat"].startswith(f"commit {i['sha']}")
            assert i["stat"] == _show_stdout([i["sha"]])

        # Regression: each branch's absorb directive must carry ITS OWN
        # `ref`, not whatever `ref` the pass-1 loop last left bound.
        # `stale-a` is local (`ref == name == "stale-a"`); `stale-b` is
        # remote-only (`ref == "origin/stale-b"`, and its directive also
        # appends "origin" as the remote-delete arg) -- the two refs differ
        # from each other AND from `stale-b`'s own name, so a single leaked
        # value cannot satisfy both.
        absorb_a = next(d for d in do["directives"] if d["id"] == "d-absorb-stale-a")
        absorb_b = next(d for d in do["directives"] if d["id"] == "d-absorb-stale-b")
        assert absorb_a["args"] == ["stale-a", "stale-a"]
        assert absorb_b["args"] == ["stale-b", "origin/stale-b", "origin"]

    def test_tip_author_and_branch_reachable_spawns_do_not_grow_with_branch_or_worktree_count(
        self, monkeypatch, tmp_path
    ):
        """PINS: `tip_author`'s branch-loop spawns collapse to ONE
        `for-each-ref` call and `branch_reachable`'s worktree-loop spawns
        collapse to ONE `git branch --merged` call, both independent of N
        (here: 3 stale/others branches, 2 non-current/main worktrees) --
        not one `git log -1`/`git merge-base --is-ancestor` per item. Model:
        `test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
        test_process_count_does_not_grow_with_the_set`."""
        calls: list[list[str]] = []

        def run_git(args, cwd):
            calls.append(list(args))
            if args[:2] == ["config", "user.email"]:
                return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
            if args[:2] == ["rev-parse", "--verify"]:
                ok = args[2] == "main"
                return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
            if args == ["branch", "-a"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="* current\n  main\n  other-a\n  other-b\n  other-c\n",
                    stderr="",
                )
            if args[0] == "for-each-ref":
                out = (
                    "current me@x\nmain me@x\nother-a me@x\nother-b me@x\nother-c me@x\n"
                )
                return SimpleNamespace(returncode=0, stdout=out, stderr="")
            if args[0] == "branch" and args[1] == "--merged":
                return SimpleNamespace(returncode=0, stdout="  other-a\n  other-b\n", stderr="")
            if args[0] == "log" and args[1] == "--oneline":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "worktree" and args[1] == "list":
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n"
                        "\n"
                        "worktree /wt-a\nHEAD aaa\nbranch refs/heads/other-a\n"
                        "\n"
                        "worktree /wt-b\nHEAD bbb\nbranch refs/heads/other-b\n"
                    ),
                    stderr="",
                )
            if "status" in args:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected git call: {args}")

        do = consolidate_assemble.brief(repo_root=tmp_path, run_git=run_git)

        for_each_ref_calls = [c for c in calls if c[0] == "for-each-ref"]
        merged_calls = [c for c in calls if c[0] == "branch" and c[1] == "--merged"]
        log_dash1_calls = [c for c in calls if c[0] == "log" and c[1] == "-1"]
        merge_base_calls = [c for c in calls if c[0] == "merge-base"]

        assert len(for_each_ref_calls) == 1
        assert len(merged_calls) == 1
        assert log_dash1_calls == []
        assert merge_base_calls == []

        wt_categories = {w["branch"]: w["category"] for w in do["gates"]["worktrees"]}
        assert wt_categories["other-a"] == "stale-absorbed"
        assert wt_categories["other-b"] == "stale-absorbed"

    def test_inspect_commits_on_empty_sha_list_spawns_nothing(self, tmp_path):
        def run_git(args, cwd):
            raise AssertionError(f"unexpected git call: {args}")

        assert consolidate_assemble.inspect_commits(run_git, tmp_path, []) == {}

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
                ("branch", "-a"): SimpleNamespace(returncode=0, stdout="* current\n  main\n  stale\n", stderr=""),
                ("for-each-ref",): SimpleNamespace(
                    returncode=0, stdout="current me@x\nmain me@x\nstale me@x\n", stderr=""
                ),
                ("branch", "--merged"): SimpleNamespace(returncode=0, stdout="", stderr=""),
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
                ("branch", "-a"): SimpleNamespace(returncode=0, stdout="* current\n  stale\n", stderr=""),
                ("for-each-ref",): SimpleNamespace(returncode=0, stdout="current me@x\nstale me@x\n", stderr=""),
                ("branch", "--merged"): SimpleNamespace(returncode=0, stdout="", stderr=""),
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


class TestMainBriefTransportFailure:
    """`main()`'s brief-half transport-failure branch: exit 3 means compute
    never ran, so stdout must stay empty (no fabricated/partial decision
    object) and the diagnostic goes to stderr only."""

    def test_transport_failure_leaves_stdout_empty(self, monkeypatch, capsys):
        def _boom():
            raise RuntimeError("git binary not found")

        monkeypatch.setattr(consolidate_assemble, "brief", _boom)

        exit_code = consolidate_assemble.main(["brief"])

        captured = capsys.readouterr()
        assert exit_code == consolidate_assemble.EXIT_TRANSPORT_FAIL
        assert captured.out == ""
        assert "git binary not found" in captured.err

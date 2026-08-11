"""test_scoped_git_commit_cli.py — the `scoped-git-commit` CLI's contract.

`coordinator/bin/scoped-git-commit` is the caller-reachable entrypoint for
`ceremony.scoped_git_commit`, the op example-doctrine-repo doctrine routes ordinary scoped
commits to. The op was registered and correct for a week with NO entrypoint,
which is what let a scoped-commit advisory point at `coordinator-safe-commit`
instead (example-doctrine-repo-em, 2026-07-29). These tests hold the two properties that
made adding it worth doing:

  1. It has NO unscoped mode. A missing `-- <paths>` is a usage error, never
     an inferred scope — otherwise it becomes another way to sweep a shared
     branch's dirty tree.
  2. It picks the agree-case vs. private-index mechanism for the caller. The
     diverged-index test below is the whole value proposition: a caller who
     hand-rolls the agree-case form on a diverged path commits the WRONG
     content, which is exactly the mistake the memo's author got lucky on.

Each end-to-end case runs the real CLI as a subprocess against a scratch git
repo — never the session's own worktree.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

from coordinator_core.win_portability import no_console_creationflags

_CLI = pathlib.Path(__file__).resolve().parents[1] / "scoped-git-commit"

#: Deterministic test session identity (C4c ownership gate — see
#: `test_scoped_git_commit_ownership.py`'s docstring for the strict
#: `assert_paths_in_session_scope` polarity this CLI now composes). Set via
#: `COORDINATOR_SESSION_ID` — the tier-1 override
#: `coordinator_core.session.core.resolve_session_id` reads first, ahead of
#: any ambient `CLAUDE_CODE_SESSION_ID` the test process inherits — so every
#: CLI invocation in this file resolves to the SAME session identity, and
#: `_seed_session_scope` can seed a real ownership claim for it.
_TEST_SESSION_ID = "scoped-git-commit-cli-test-session"


def _git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout


def _run_cli(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # The CLI resolves claude-klabauter via cc_invoke's ladder; pin rung 1 so the test is
    # independent of whether this machine has an install.
    env["CLAUDE_KLABAUTER_ROOT"] = str(pathlib.Path(__file__).resolve().parents[3])
    env["COORDINATOR_SESSION_ID"] = _TEST_SESSION_ID
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        **no_console_creationflags(),
    )


def _seed_session_scope(repo: pathlib.Path, owned_paths: list[str]) -> None:
    """Seed a real `.git/coordinator-sessions/<_TEST_SESSION_ID>/touched.txt`
    ownership claim -- the same on-disk shape `track_touched_files` writes --
    so the C4c sink's strict `assert_paths_in_session_scope` gate genuinely
    resolves `owned_paths` as this session's own, with no mocking of the
    predicate itself. Mirrors `test_scoped_git_commit_ownership.py`'s own
    `_seed_session_scope` helper.
    """
    sdir = repo / ".git" / "coordinator-sessions" / _TEST_SESSION_ID
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text(
        "\n".join(owned_paths) + ("\n" if owned_paths else ""), encoding="utf-8"
    )


@pytest.fixture
def scratch_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    (repo / "peer.txt").write_text("peer\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


class TestIncludeOrphans:
    """`--include-orphans` is the reachable form of the op's own offer — see
    the refusal string `scoped_git_commit.py` emits ending "-- re-invoke with
    include_orphans: true to include the orphan path(s)". These tests hold
    the CLI-seam contract that flag exists to close: it threads
    `include_orphans` into the op params, defaults to unset, and never
    weakens the no-unscoped-mode invariant.
    """

    def test_flag_threads_include_orphans_true_into_op_params(self, monkeypatch):
        module = _load_cli_module()
        captured = {}

        def _fake_route_mutation(op, params, worktree_root, legacy_fn):
            captured["op"] = op
            captured["params"] = params
            return {"committed": False, "reason": "empty-commit-set"}

        monkeypatch.setattr(module, "route_mutation", _fake_route_mutation)
        rc = module.main(
            ["-m", "subject", "--include-orphans", "--", "some/orphan.txt"]
        )
        assert rc == module._EXIT_OK
        assert captured["params"]["include_orphans"] is True

    def test_flag_absent_leaves_include_orphans_false(self, monkeypatch):
        module = _load_cli_module()
        captured = {}

        def _fake_route_mutation(op, params, worktree_root, legacy_fn):
            captured["params"] = params
            return {"committed": False, "reason": "empty-commit-set"}

        monkeypatch.setattr(module, "route_mutation", _fake_route_mutation)
        rc = module.main(["-m", "subject", "--", "mine.txt"])
        assert rc == module._EXIT_OK
        assert captured["params"]["include_orphans"] is False

    def test_include_orphans_does_not_relax_the_separator_and_paths_requirement(
        self, scratch_repo
    ):
        result = _run_cli(scratch_repo, "-m", "subject", "--include-orphans")
        assert result.returncode == 2
        assert "separator is required" in result.stderr


class TestNoUnscopedMode:
    def test_missing_pathspec_separator_is_a_usage_error(self, scratch_repo):
        result = _run_cli(scratch_repo, "-m", "subject")
        assert result.returncode == 2
        assert "separator is required" in result.stderr

    def test_separator_with_no_paths_is_a_usage_error(self, scratch_repo):
        result = _run_cli(scratch_repo, "-m", "subject", "--")
        assert result.returncode == 2

    def test_missing_separator_error_names_the_powershell_quoting_fix(
        self, scratch_repo
    ):
        result = _run_cli(scratch_repo, "-m", "subject")
        assert result.returncode == 2
        assert "separator is required" in result.stderr
        assert "'--'" in result.stderr
        assert "PowerShell" in result.stderr

    def test_missing_separator_error_names_the_restricted_policy_fallback(
        self, scratch_repo
    ):
        """The Restricted-policy half of the message must name a concrete,
        invocable `python <path> ...` command — never the literal
        `<path-to-extensionless-forwarder>` template placeholder, and the
        path it names must be this CLI's own resolvable location (see the
        reviewer finding this pins: `scoped-git-commit` IS the extensionless
        forwarder the message is telling the operator to re-invoke)."""
        result = _run_cli(scratch_repo, "-m", "subject")
        assert result.returncode == 2
        assert "Restricted" in result.stderr
        assert "<path-to-extensionless-forwarder>" not in result.stderr
        assert "python %s" % str(_CLI.resolve()) in result.stderr

    def test_missing_subject_is_a_usage_error(self, scratch_repo):
        result = _run_cli(scratch_repo, "--", "mine.txt")
        assert result.returncode == 2

    def test_repeated_dash_m_is_a_usage_error_not_a_silent_overwrite(
        self, scratch_repo
    ):
        """A second -m must never silently discard the first subject: this
        tool takes exactly one subject and does not concatenate repeated -m
        like `git commit` does."""
        result = _run_cli(
            scratch_repo, "-m", "subject one", "-m", "subject two", "--", "mine.txt"
        )
        assert result.returncode == 2
        assert "second" in result.stderr
        assert "-m" in result.stderr
        assert "-F" in result.stderr or "--file" in result.stderr

    def test_single_dash_m_still_parses_unchanged(self, scratch_repo):
        result = _run_cli(scratch_repo, "-m", "subject", "--", "mine.txt")
        assert result.returncode == 0

    def test_paths_are_not_mistaken_for_flags_after_the_separator(self, scratch_repo):
        """Everything after `--` is a pathspec in git's own sense; a path that
        looks like a flag must not be re-parsed as one."""
        result = _run_cli(scratch_repo, "-m", "subject", "--", "--json")
        # Not a usage error: `--json` was taken as a (nonexistent) path, so the
        # op finds nothing to commit rather than rejecting the argv.
        assert result.returncode == 0
        assert "no commit landed" in result.stdout


class TestScopeIsHonored:
    def test_commits_only_the_named_paths(self, scratch_repo):
        (scratch_repo / "mine.txt").write_text("mine changed\n", encoding="utf-8")
        (scratch_repo / "peer.txt").write_text("peer changed\n", encoding="utf-8")
        _seed_session_scope(scratch_repo, ["mine.txt"])

        result = _run_cli(scratch_repo, "-m", "scoped: mine only", "--", "mine.txt")
        assert result.returncode == 0, result.stderr
        assert "committed" in result.stdout

        committed = _git(scratch_repo, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == ["mine.txt"]
        assert "peer.txt" in _git(scratch_repo, "status", "--short")

    def test_nothing_to_commit_is_a_benign_zero_exit(self, scratch_repo):
        result = _run_cli(scratch_repo, "-m", "subject", "--", "mine.txt")
        assert result.returncode == 0
        assert "no commit landed" in result.stdout


class TestMechanismSelection:
    def test_diverged_index_commits_the_staged_content_and_preserves_the_worktree(
        self, scratch_repo
    ):
        """The private-index branch, which is the reason this op exists.

        Staged content and worktree content differ at the same path. The
        agree-case form (`git add -- p && git commit -- p`) would commit the
        WORKTREE content, silently discarding the deliberate staging; the
        private-index form commits what was staged and leaves the worktree
        alone. The caller does not have to know which case they are in.
        """
        target = scratch_repo / "mine.txt"
        target.write_text("STAGED\n", encoding="utf-8")
        _git(scratch_repo, "add", "--", "mine.txt")
        target.write_text("WORKTREE-ONLY\n", encoding="utf-8")
        _seed_session_scope(scratch_repo, ["mine.txt"])

        result = _run_cli(scratch_repo, "-m", "diverged: commit staged", "--", "mine.txt")
        assert result.returncode == 0, result.stderr

        assert _git(scratch_repo, "show", "HEAD:mine.txt") == "STAGED\n"
        assert target.read_text(encoding="utf-8") == "WORKTREE-ONLY\n"
        # P1 fix (state/bug-backlog/2026-08-10-scoped-git-commit-reports-
        # success-while-334e90d707f9.yaml): a staged-only commit is a
        # legitimate success (exit 0, asserted above), but must not print a
        # bare `committed <sha>` — the excluded worktree edit is named.
        assert "committed" in result.stdout
        assert "WARNING" in result.stdout
        assert "mine.txt" in result.stdout

    def test_clean_path_output_carries_no_exclusion_warning(self, scratch_repo):
        """Silence on the clean path (agree branch): byte-identical to
        today's output when nothing diverged."""
        target = scratch_repo / "mine.txt"
        target.write_text("content\n", encoding="utf-8")
        _seed_session_scope(scratch_repo, ["mine.txt"])

        result = _run_cli(scratch_repo, "-m", "agree: commit worktree", "--", "mine.txt")
        assert result.returncode == 0, result.stderr

        assert "WARNING" not in result.stdout


def _load_cli_module():
    """Import the extension-less CLI as a module for its render unit tests."""
    loader = importlib.machinery.SourceFileLoader("scoped_git_commit_cli", str(_CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def render():
    return _load_cli_module()._render


class TestPushReporting:
    """The line a human reads must never render an UNCONFIRMED push as a FAILED
    one (example-doctrine-repo-em, 2026-07-30). The old two-valued map printed
    `(not pushed)` on three commits that had all pushed, because the op's own
    push had lost the race to the detached auto-push. A reader told a commit
    did not push reaches for a re-push, an amend, or a force-push — and on an
    auto-push-armed shared branch with concurrent sessions, those corrections
    are more dangerous than the symptom.
    """

    def test_pushed_renders_as_pushed(self, render):
        assert render({"committed": True, "sha": "a" * 40, "push_state": "pushed"}) == (
            "committed %s (pushed)" % ("a" * 12)
        )

    def test_unconfirmed_does_not_read_as_a_failure(self, render):
        line = render({"committed": True, "sha": "b" * 40, "push_state": "unconfirmed"})
        assert "unconfirmed" in line
        assert "not pushed" not in line
        assert "FAILED" not in line

    def test_real_failure_stays_loud(self, render):
        line = render({"committed": True, "sha": "c" * 40, "push_state": "push-failed"})
        assert "PUSH FAILED" in line

    def test_no_remote_is_neither_success_nor_failure(self, render):
        assert "no remote" in render(
            {"committed": True, "sha": "d" * 40, "push_state": "no-remote"}
        )

    def test_pre_tristate_engine_degrades_to_unconfirmed_not_failure(self, render):
        """A CLI newer than the engine across the seam sees a bare `pushed`
        bool. False there cannot distinguish failed from in-flight, so it must
        degrade to the honest state, not re-mint the false negative.
        """
        line = render({"committed": True, "sha": "e" * 40, "pushed": False})
        assert "unconfirmed" in line
        assert "not pushed" not in line


class TestWorktreeExcludedReporting:
    """P1 fix (state/bug-backlog/2026-08-10-scoped-git-commit-reports-
    success-while-334e90d707f9.yaml): the private-index branch's success
    must be loud about which worktree edits it excluded, and silent
    (byte-identical to today) when nothing was excluded.
    """

    def test_worktree_excluded_renders_a_loud_warning(self, render):
        line = render(
            {
                "committed": True,
                "sha": "a" * 40,
                "push_state": "no-remote",
                "worktree_excluded": ["mine.txt"],
            }
        )
        assert "WARNING" in line
        assert "mine.txt" in line

    def test_no_exclusion_key_renders_exactly_as_before(self, render):
        line = render({"committed": True, "sha": "b" * 40, "push_state": "no-remote"})
        assert "WARNING" not in line
        assert line == "committed %s (no remote)" % ("b" * 12)

    def test_empty_exclusion_list_renders_no_warning(self, render):
        line = render(
            {
                "committed": True,
                "sha": "c" * 40,
                "push_state": "no-remote",
                "worktree_excluded": [],
            }
        )
        assert "WARNING" not in line


class TestRefusalReporting:
    """A REFUSAL must not render as a benign no-op (observed live, session
    fb5fa766, 2026-07-31). `commit_scoped` deliberately rejects a directory
    pathspec — a directory matches whatever is inside it AT COMMIT TIME,
    including a peer's file added after the path set was computed — and returns
    `commit_failed` plus a diagnostic naming that cause. The renderer read only
    `reason`, so the refusal printed as "no commit landed: nothing to commit"
    while the caller's files stayed STAGED on a shared branch, where the next
    bare commit by any session absorbs them. An EM checking for residue was
    told clean when it was not: a wrong answer to a safety question.
    """

    def test_directory_pathspec_refusal_names_its_cause(self, render):
        line = render(
            {
                "committed": False,
                "commit_failed": True,
                "diagnostics": [
                    "commit_scoped: directory pathspec 'state/subagent-share/sid/' rejected "
                    "-- a directory matches whatever is inside it AT COMMIT TIME"
                ],
            }
        )
        assert "REFUSED" in line
        assert "directory pathspec" in line
        assert "nothing to commit" not in line

    def test_benign_already_committed_noop_does_not_cry_wolf(self, render):
        """`git commit` exits 1 on an empty commit set, so the ordinary
        already-committed no-op also arrives as `commit_failed` carrying a
        single content-free `exit_code=N`. Rendering THAT as a refusal would
        fire on the most common outcome there is.
        """
        line = render(
            {"committed": False, "commit_failed": True, "diagnostics": ["exit_code=1"]}
        )
        assert "REFUSED" not in line
        assert "nothing to commit" in line

    def test_engine_supplied_empty_commit_set_reason_is_preserved(self, render):
        line = render(
            {"committed": False, "commit_failed": False, "reason": "empty-commit-set"}
        )
        assert "REFUSED" not in line
        assert "empty-commit-set" in line

    def test_explanatory_diagnostic_wins_over_a_bare_exit_code(self, render):
        """A real refusal that also carries exit-code noise must still be loud."""
        line = render(
            {
                "committed": False,
                "commit_failed": True,
                "diagnostics": ["exit_code=128", "commit_scoped: pathspec escapes repo root"],
            }
        )
        assert "REFUSED" in line
        assert "escapes repo root" in line

    def test_partial_decline_never_leads_with_bare_committed(self, render):
        """P1 live incident (2026-08-10): a 55-path call that landed 1 path
        and DECLINED 54 rendered `"committed <sha> (declined) — DECLINED 54
        named path(s) ..."` -- a reader (or a caller grepping `^committed`)
        sees the word `committed` first and the decline second, exactly
        backwards for a run that mostly failed.
        """
        line = render(
            {
                "committed": True,
                "sha": "a" * 40,
                "push_state": "pushed",
                "declined_paths": [
                    {"path": "x.yaml", "reason": "not found in the worktree or index"}
                ],
            }
        )
        assert not line.startswith("committed")
        assert line.startswith("PARTIAL")
        assert "DECLINED 1 named path(s)" in line

    def test_push_declined_note_never_collides_with_a_path_decline(self, render):
        """`push_state == "declined"` (a branch-policy push decline) used to
        render as the bare word "declined" -- visually identical to
        `_declined_note`'s own "DECLINED N named path(s)" wording, on every
        commit whose branch policy declines the push, including a fully
        clean one with nothing else wrong.
        """
        line = render({"committed": True, "sha": "a" * 40, "push_state": "declined"})
        assert "branch policy" in line
        assert "DECLINED" not in line


@pytest.fixture(scope="module")
def exit_code_for_result():
    return _load_cli_module()._exit_code_for_result


class TestRefusalExitCode:
    """A REFUSAL exiting 0 is a silent-data-loss shape (2026-08-06 fix, live
    incident): `main()` previously printed `_render`'s "REFUSED — no commit
    landed" line and then unconditionally returned `_EXIT_OK` regardless of
    what it had just printed. A caller checking only the return code — the
    entire point of having one — saw success with nothing committed. These
    pin `_exit_code_for_result` directly against the same result shapes
    `TestRefusalReporting` above already exercises through `_render`, so the
    render and the exit code can never drift apart again.
    """

    def test_gate_refusal_exits_nonzero(self, exit_code_for_result):
        result = {
            "committed": False,
            "commit_failed": True,
            "diagnostics": [
                "Deleted-claim NOT staged for deletion: week-changelog/2026-07-20.md"
            ],
        }
        assert exit_code_for_result(result) != 0

    def test_ownership_denial_error_shape_exits_nonzero(self, exit_code_for_result):
        result = {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: rejected -- path(s) outside session scope: ...",
        }
        assert exit_code_for_result(result) != 0

    def test_committed_exits_zero(self, exit_code_for_result):
        result = {"committed": True, "sha": "a" * 40, "push_state": "pushed"}
        assert exit_code_for_result(result) == 0

    def test_empty_commit_set_noop_exits_zero(self, exit_code_for_result):
        result = {"committed": False, "commit_failed": False, "reason": "empty-commit-set"}
        assert exit_code_for_result(result) == 0

    def test_benign_already_committed_noop_exits_zero(self, exit_code_for_result):
        """Mirrors `TestRefusalReporting.test_benign_already_committed_noop_
        does_not_cry_wolf` -- the bare `exit_code=N` no-op renders quietly AND
        must exit 0, not just render quietly."""
        result = {"committed": False, "commit_failed": True, "diagnostics": ["exit_code=1"]}
        assert exit_code_for_result(result) == 0

    def test_partial_decline_exits_nonzero_even_when_committed(self, exit_code_for_result):
        """P1 live incident (2026-08-10): `committed: true` (1 of 55 landed)
        previously exited 0 regardless of `declined_paths` -- a caller
        checking only the return code saw success on a run that silently
        dropped 54 named paths.
        """
        result = {
            "committed": True,
            "sha": "a" * 40,
            "push_state": "pushed",
            "declined_paths": [{"path": "x.yaml", "reason": "..."}],
        }
        assert exit_code_for_result(result) != 0


class TestMessageFromFile:
    """`-F <path>` (and `-F -` for stdin) mirrors `git commit -F` -- the way
    out of the here-string trap on this seam: `-m` forces a multi-line
    message through a single argv token, and this box has already mangled a
    commit subject once via PowerShell's `@'...'@` idiom used from the Bash
    tool.
    """

    def test_dash_f_produces_the_same_subject_as_equivalent_dash_m(
        self, scratch_repo, tmp_path
    ):
        (scratch_repo / "mine.txt").write_text("mine changed\n", encoding="utf-8")
        _seed_session_scope(scratch_repo, ["mine.txt"])
        msg_path = tmp_path / "msg.txt"
        msg_path.write_text("scoped: from file", encoding="utf-8")

        result = _run_cli(scratch_repo, "-F", str(msg_path), "--", "mine.txt")
        assert result.returncode == 0, result.stderr
        subject = _git(scratch_repo, "show", "-s", "--format=%s", "HEAD").strip()
        assert subject == "scoped: from file"

    def test_dash_m_and_dash_f_together_is_a_usage_error(self, scratch_repo, tmp_path):
        msg_path = tmp_path / "msg.txt"
        msg_path.write_text("subject", encoding="utf-8")
        result = _run_cli(
            scratch_repo, "-m", "subject", "-F", str(msg_path), "--", "mine.txt"
        )
        assert result.returncode == 2
        assert "-m" in result.stderr
        assert "-F" in result.stderr

    def test_dash_f_with_nonexistent_path_fails_loudly_before_staging(
        self, scratch_repo, tmp_path
    ):
        missing = tmp_path / "does-not-exist.txt"
        result = _run_cli(scratch_repo, "-F", str(missing), "--", "mine.txt")
        assert result.returncode == 2
        assert str(missing) in result.stderr
        # Nothing staged as a side effect of the failed read.
        assert _git(scratch_repo, "status", "--short").strip() == ""

    def test_multiline_body_from_dash_f_survives_intact(self, scratch_repo, tmp_path):
        (scratch_repo / "mine.txt").write_text("mine changed\n", encoding="utf-8")
        _seed_session_scope(scratch_repo, ["mine.txt"])
        msg_path = tmp_path / "msg.txt"
        msg_path.write_text(
            "scoped: multiline subject\n\nbody line one\nbody line two\n",
            encoding="utf-8",
        )

        result = _run_cli(scratch_repo, "-F", str(msg_path), "--", "mine.txt")
        assert result.returncode == 0, result.stderr
        full = _git(scratch_repo, "show", "-s", "--format=%B", "HEAD")
        assert "body line one" in full
        assert "body line two" in full


class TestMoveSet:
    """A pathspec naming both the deleted source and the created destination
    of a move must commit that move (2026-08-06 fix, live incident: commit
    `64acc1254`, ~15 changelog blocks relocated into an archive directory,
    refused with "Deleted-claim NOT staged for deletion" for every moved
    path). Root cause: `explicit_stage` correctly classifies the vacated
    source as a deletion and the composed message correctly claims it, but
    once both the source and an identical-content destination are staged in
    the same batch, git's own `--find-renames` diff pairs them into one
    `R100` line instead of a `D`+`A` pair -- and `commit_gates`'
    Assertion-1 previously only recognized bare `D` lines as "staged for
    deletion", failing a claim the pipeline's own staging logic had made
    accurate. See `commit_gates._parse_name_status_rename_sources`'s own
    docstring for the full fix.
    """

    def test_move_set_commits_end_to_end(self, scratch_repo):
        old_path = scratch_repo / "mine.txt"
        content = old_path.read_text(encoding="utf-8")
        old_path.unlink()
        new_dir = scratch_repo / "archive"
        new_dir.mkdir()
        new_path = new_dir / "mine.txt"
        new_path.write_text(content, encoding="utf-8")

        _seed_session_scope(scratch_repo, ["mine.txt", "archive/mine.txt"])

        result = _run_cli(
            scratch_repo,
            "-m",
            "scoped: move mine.txt into archive/",
            "--",
            "mine.txt",
            "archive/mine.txt",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "committed" in result.stdout

        # `git show --name-status -M` (unlike bare `--name-only`, which
        # reports only the DESTINATION of a detected rename) names both
        # sides of the move explicitly.
        name_status = _git(scratch_repo, "show", "--name-status", "-M", "--format=", "HEAD")
        assert "mine.txt" in name_status
        assert "archive/mine.txt" in name_status
        assert not (scratch_repo / "mine.txt").exists()
        assert (scratch_repo / "archive" / "mine.txt").exists()


class TestMangledCarriageReturnPaths:
    """P1 live incident (2026-08-10): a 55-path `scoped-git-commit` call
    composed its pathspec from a CRLF-authored file via unquoted
    `$(cat file | tr '\\n' ' ')` (a plausible, common Windows-side pattern --
    Notepad, PowerShell `Out-File`, `git status --porcelain` piped through a
    tool that re-adds CRLF). `tr` only ever sees `\\n`, so the `\\r` half of
    each CRLF survives into the shell word, gluing a trailing carriage
    return onto every path token but the last. `explicit_stage()`'s
    `(root / p).exists()` check then, HONESTLY, finds nothing at
    `<path>\\r` -- 54 of 55 paths declined with a reason that reads, in
    isolation, exactly like a real absence. The fix belongs at this CLI's
    own argument boundary (never in `explicit_stage`, which behaved
    correctly against the input it was actually given): no real path can
    legitimately end in `\\r`, so a trailing `\\r` on a `-- <paths>` token is
    unambiguous transport damage, stripped here with a named warning.

    Isolated-fixture bisection at N up to 60 (both a clean argv array and
    the exact word-splitting shell form) never reproduced the incident
    shape until the CRLF ingredient was added -- confirming the defect was
    never a batch-size threshold in the engine.
    """

    def test_parse_args_strips_trailing_cr_and_reports_it(self):
        module = _load_cli_module()
        subject, repo, paths, as_json, include_orphans, deliverable_id, mangled = (
            module._parse_args(
                ["-m", "msg", "--", "a/one.txt\r", "a/two.txt\r", "a/three.txt"]
            )
        )
        assert paths == ["a/one.txt", "a/two.txt", "a/three.txt"]
        assert mangled == ["a/one.txt\r", "a/two.txt\r"]

    def test_parse_args_reports_no_mangling_on_clean_paths(self):
        module = _load_cli_module()
        _, _, paths, _, _, _, mangled = module._parse_args(
            ["-m", "msg", "--", "a/one.txt", "a/two.txt"]
        )
        assert paths == ["a/one.txt", "a/two.txt"]
        assert mangled == []

    def test_crlf_word_split_pathspec_commits_end_to_end(self, scratch_repo, tmp_path):
        """The exact repro shape: N tracked files, all but the last named
        with a trailing `\\r` (the shape `tr '\\n' ' '` over a CRLF file with
        no final CRLF on the last line produces), passed through unquoted
        `$(...)` word-splitting. Must commit all N, not decline N-1.
        """
        names = []
        for i in range(12):
            name = "state/sizings/2026-08-10-mangled-cr-fixture-%02d.yaml" % i
            (scratch_repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
            (scratch_repo / name).write_text("initial\n", encoding="utf-8")
            names.append(name)
        _git(scratch_repo, "add", "-A")
        _git(scratch_repo, "commit", "-qm", "seed sizings")
        for name in names:
            with open(scratch_repo / name, "a", encoding="utf-8") as fh:
                fh.write("dirty\n")

        _seed_session_scope(scratch_repo, names)

        # Mirror the incident's own composition: every token but the last
        # carries a trailing '\r' -- the shape `tr '\n' ' '` produces over a
        # CRLF-authored, no-final-newline file.
        mangled_argv = [name + "\r" for name in names[:-1]] + [names[-1]]

        result = _run_cli(
            scratch_repo, "-m", "commit all sizings", "--json", "--", *mangled_argv
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "stripped a trailing carriage return" in result.stderr
        assert "declined_paths" not in result.stdout

        import json

        payload = json.loads(result.stdout)
        assert payload.get("committed") is True
        assert not payload.get("declined_paths")

        status = _git(scratch_repo, "status", "--porcelain")
        assert status.strip() == ""

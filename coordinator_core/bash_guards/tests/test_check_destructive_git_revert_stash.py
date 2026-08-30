"""Regression tests for the EM-path unscoped-`git stash` gate in
``coordinator_core.bash_guards.dispatch_checks.check_destructive_git_revert``.

Subject: the shared-working-tree data-loss family spun off in
``state/handoffs/2026-07-28-unscoped-stash-peer-sweep-data-loss.md`` — an
agent or ceremony runs a tree-wide `git stash`, which takes EVERY concurrent
session's uncommitted work, not its own, and the peer sessions lose changes
mid-session with no signal.

The subagent half of this was closed 2026-07-26 in
``block_subagent_destructive_action`` ("UNSCOPED-STASH GAP CLOSE"). That guard
is identity-gated and deliberately never fires on the EM main-loop ("no
agent_id -> allow"), so the EM path is covered only by this check — and it had
two independent defects, both fixed 2026-07-28 and both pinned below:

  1. SHAPE. The branch ran only when `-u`/`-a`/`--include-untracked`/`--all`
     was present. A bare `git stash` / `git stash push` — which already sweeps
     every tracked modification in the tree — was never examined at all.
     `-u` widens a stash to also take UNTRACKED files; it is not what makes a
     stash a sweep.
  2. COLLECTION. Even when it did run, `affected` collected only `??`
     (untracked) porcelain rows, so a stash sweeping a peer's tracked
     in-flight edits found nothing to report and allowed silently. Tracked
     modifications are precisely what every stash write shape takes.

`TestBareStashSweepingPeerTrackedEdits` is the AC3 regression proof: a peer's
uncommitted tracked work survives the path that previously took it.

`TestMentionIsNotInvocation` pins the false-positive class the shape widening
briefly introduced and that `_command_really_invokes` closes: `_split_segments`
is not quote-aware, so a `|` inside a quoted operand (`grep -i "...\\|git
stash"`) manufactures a bogus `git stash"` fragment that the free-text
classifier reads as a real invocation. Confirmed live 2026-07-28 — the widened
check denied an ordinary read-only grep in this repo. Per-segment
corroboration cannot fix this (the fragment does not tokenize, so it would
fail closed on exactly the input it must dismiss); corroboration runs over the
intact command, where a quoted operand stays one token.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _command_really_invokes,
    check_destructive_git_revert,
    check_destructive_git_revert_advisory,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _deny_reason(result) -> str:
    """The deny text out of a PreToolUse hook payload (see `_deny`)."""
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture()
def repo_with_peer_work(tmp_path: Path) -> Path:
    """A git repo holding a committed load-bearing file that a *peer* session
    has since modified but not committed — the exact state an unscoped stash
    silently sweeps.
    """
    repo = tmp_path / "shared-tree"
    (repo / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    peer_file = repo / "state" / "peer-in-flight.md"
    peer_file.write_text("committed baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    # The peer's uncommitted, tracked, git-unrecoverable work.
    peer_file.write_text("committed baseline\npeer's in-flight edit\n", encoding="utf-8")
    return repo


class TestBareStashSweepingPeerTrackedEdits:
    """Defect 1 + 2 together: the shape that carries the real-world harm."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "git stash",
            "git stash push",
            "git stash -u",
            'git stash push -m "wip"',
            # `save` is `push` under its pre-2.16 deprecated name -- identical
            # working-tree sweep. Excluding it as an "other subcommand" was a
            # live bypass in both this guard and its subagent-side sibling.
            "git stash save",
            "git stash save -u",
            'git stash save "wip"',
        ],
    )
    def test_sweep_shapes_are_denied(self, repo_with_peer_work: Path, cmd: str) -> None:
        result = check_destructive_git_revert(
            "git -C %s %s" % (repo_with_peer_work, cmd.removeprefix("git "))
        )
        assert result is not None, "unscoped %r swept a peer's tracked work undetected" % cmd
        assert "state/peer-in-flight.md" in _deny_reason(result)

    @pytest.mark.parametrize(
        "template",
        [
            "git add -A && git -C %s stash",
            "git status; git -C %s stash -u",
            "git -C %s stash",  # control: no preceding invocation
            "git log --oneline | head -3; git -C %s stash save",
        ],
    )
    def test_a_preceding_git_invocation_does_not_mask_the_stash(
        self, repo_with_peer_work: Path, template: str
    ) -> None:
        """An unrelated `git` earlier in a compound command must not consume
        the corroboration step's answer for the whole command. Resolving only
        the FIRST `git` token let `git add -A && git stash` walk to `add`,
        conclude "not a stash", and wave the real sweep straight through.
        """
        cmd = template % repo_with_peer_work
        result = check_destructive_git_revert(cmd)
        assert result is not None, "%r masked the stash behind an earlier git invocation" % cmd
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_pathspec_scoped_stash_is_allowed(self, repo_with_peer_work: Path) -> None:
        """A `--`-delimited pathspec scopes the stash to the caller's own
        paths — the named safe forward path, which must stay reachable."""
        result = check_destructive_git_revert(
            "git -C %s stash push -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    @pytest.mark.parametrize("sub", ["list", "show", "create", "store"])
    def test_non_sweep_subcommands_are_allowed(self, repo_with_peer_work: Path, sub: str) -> None:
        result = check_destructive_git_revert("git -C %s stash %s" % (repo_with_peer_work, sub))
        assert result is None


class TestWindowsExeStashRealEntrypoint:
    """Review: code-reviewer -- Finding 3 (P2, 2026-07-28): the Windows-exe
    stash regression coverage in `TestCommandReallyInvokes` below asserts
    only the private `_command_really_invokes` corroboration helper, never
    `check_destructive_git_revert` itself -- the function `dispatch.py`
    actually registers and calls. Confirmed live while adding this class:
    the corroboration helper alone was NOT sufficient -- `_gr_is_revert_
    segment`/`_GR_BASE_RE` (this file's OWN verb-resolution step, upstream
    of the corroboration call) never recognized a `.exe`-suffixed or
    case-varied `git` at command position at all, so `check_destructive_
    git_revert("git.exe -C <repo> stash")` silently ALLOWED against a repo
    with real uncommitted peer work -- exactly the "tests pass on an
    unwired guard" failure mode this session exists to eliminate. Fixed by
    `_normalize_git_exe_head_to_bare` (this module), asserted here at the
    real entrypoint, not the helper.
    """

    def test_git_exe_bare_stash_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("git.exe -C %s stash" % repo_with_peer_work)
        assert result is not None, "git.exe stash swept a peer's tracked work undetected"
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_git_exe_uppercase_bare_stash_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("GIT.EXE -C %s stash" % repo_with_peer_work)
        assert result is not None, "GIT.EXE stash swept a peer's tracked work undetected"
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_windows_spaced_path_backslash_git_exe_stash_denies(
        self, repo_with_peer_work: Path
    ) -> None:
        result = check_destructive_git_revert(
            "C:\\Program Files\\Git\\bin\\git.exe -C %s stash" % repo_with_peer_work
        )
        assert result is not None, "spaced-path git.exe stash swept a peer's tracked work undetected"
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_windows_spaced_path_forward_slash_git_exe_stash_denies(
        self, repo_with_peer_work: Path
    ) -> None:
        result = check_destructive_git_revert(
            "C:/Program Files/Git/bin/git.exe -C %s stash" % repo_with_peer_work
        )
        assert result is not None, "spaced-path git.exe stash swept a peer's tracked work undetected"
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_git_exe_pathspec_scoped_stash_still_allowed(self, repo_with_peer_work: Path) -> None:
        # Negative control: the `--`-delimited scoped-stash safe-forward path
        # must remain reachable through the Windows-exe spelling too.
        result = check_destructive_git_revert(
            "git.exe -C %s stash push -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    def test_gitk_exe_bare_invocation_not_treated_as_git(self, repo_with_peer_work: Path) -> None:
        # Negative control: a lookalike binary through the same normalizer
        # must not be treated as `git`.
        result = check_destructive_git_revert("gitk.exe -C %s stash" % repo_with_peer_work)
        assert result is None


class TestTrackedRowsAreCollected:
    """Defect 2 in isolation: with NO untracked file present, a `-u` stash
    must still be denied on the peer's tracked modification alone. Pre-fix
    this returned None — `affected` only ever collected `??` rows.
    """

    def test_tracked_only_tree_still_denies(self, repo_with_peer_work: Path) -> None:
        assert not [p for p in repo_with_peer_work.rglob("*") if p.name.startswith("untracked")]
        result = check_destructive_git_revert("git -C %s stash -u" % repo_with_peer_work)
        assert result is not None
        assert "state/peer-in-flight.md" in _deny_reason(result)


class TestCommandReallyInvokes:
    """Direct contract for the corroboration helper, pinned independent of its
    caller so a regression in the argv walk is diagnosed here rather than
    through the full deny path.
    """

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            # Plain resolution.
            ("git stash", True),
            ("git status", False),
            ("", False),
            # Global options that consume an operand -- the value must not be
            # mistaken for the subcommand.
            ("git -C /some/dir stash", True),
            ("git -c user.name=x stash", True),
            ("git --git-dir /d/.git stash", True),
            ("git --work-tree=/d stash", True),
            # No-operand globals.
            ("git --no-pager stash", True),
            ("git -P stash", True),
            # Multiple git tokens: only a LATER one matches.
            ("git add -A && git stash", True),
            ("git status; git stash -u", True),
            ("git add -A && git commit -m x", False),
            # Absolute path to the binary.
            ("/usr/bin/git stash", True),
            # Mention, not invocation -- a quoted operand stays one token.
            (r'grep -i "git stash" f.py', False),
            ('echo "git stash"', False),
            # Windows-shaped executable tokens must resolve to the same
            # identity as the POSIX `git` token (guard-fails-open-on-Windows
            # fix, 2026-07-28) -- `git.exe`, a backslash-separated absolute
            # path (with an unescaped space in `Program Files`, which shlex
            # would otherwise mis-tokenize once the backslashes are eaten),
            # and a mixed-separator spelling all corroborate as real
            # invocations.
            ("git.exe stash", True),
            (r"C:\Program Files\Git\bin\git.exe stash", True),
            ("C:/Program Files/Git/bin/git.exe stash", True),
            # Negative control: a basename that merely CONTAINS "git" must
            # never be treated as `git` -- exact-basename normalization only,
            # never substring matching.
            ("gitk stash", False),
            ("git-foo stash", False),
            ("legit stash", False),
        ],
    )
    def test_resolution(self, cmd: str, expected: bool) -> None:
        assert _command_really_invokes(cmd, "stash") is expected

    @pytest.mark.parametrize(
        "cmd",
        [
            "git --unknown-flag stash",      # unrecognized global: operand shape unknown
            "git --unknown-flag status",     # ...even when a later token would not match
            'git commit -m "unterminated',   # untokenizable
        ],
    )
    def test_unresolvable_fails_closed(self, cmd: str) -> None:
        """Ambiguity must resolve to "treat as an invocation, keep checking" --
        this helper exists to remove false positives, never to open a bypass.
        """
        assert _command_really_invokes(cmd, "stash") is True

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            # `push` is `stash`'s own subcommand token here, not a real
            # `git push` invocation -- the CHECK 2 false-positive shape
            # (2026-07-28, example-game-repo-em cross-repo report).
            ("git stash push", False),
            ('git stash push -m "x" -- +path', False),
            ("git stash push -f -- path", False),
            ("git stash save", False),
            # Genuine `git push`, every spelling CHECK 2 must still catch.
            ("git push", True),
            ("git push origin main", True),
            ("git push --force", True),
            ("git push -f origin main", True),
            ("git push origin +main:main", True),
            ("git -C /some/dir push --force", True),
            ("git --namespace n push --force", True),
            # Multiple git tokens: an unrelated preceding invocation must
            # not mask a real trailing push.
            ("git stash push && git push --force", True),
        ],
    )
    def test_resolution_push_subcommand(self, cmd: str, expected: bool) -> None:
        assert _command_really_invokes(cmd, "push") is expected


class TestMentionIsNotInvocation:
    """The verb appearing in text is not the verb being invoked."""

    @pytest.mark.parametrize(
        "cmd",
        [
            # The live 2026-07-28 false positive: `\|` alternation inside a
            # quoted grep pattern, which _split_segments breaks into a bogus
            # `git stash"` fragment.
            r'grep -i "def test\|stash -u\|git stash" f.py',
            'echo "run git stash first"',
            'git commit -m "document why git stash is banned here"',
            "rg --files-with-matches 'git stash'",
        ],
    )
    def test_mentions_do_not_deny(self, repo_with_peer_work: Path, cmd: str) -> None:
        assert check_destructive_git_revert(cmd) is None


class TestMentionIsNotInvocationOtherVerbs:
    """The stash-only regression above, widened to `reset`/`checkout`/
    `restore` (Review: staff-eng, Finding 3, 2026-08-05): the same
    `_split_segments`-manufactured bogus fragment class applies to every
    verb this guard classifies, not stash alone -- and since the advisory
    floor (2026-08-05) now turns a dirty, non-load-bearing tree into a
    LIVE advisory where it previously produced nothing, a bogus segment on
    these three verbs is newly reachable the same way the stash one was
    confirmed live 2026-07-28. Run over `repo_with_ordinary_dirty_file`
    (not `repo_with_peer_work`) so a false positive would show up as an
    advisory, not just a deny -- the shape the advisory floor exists for.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            r'grep -i "def test\|reset --hard\|git reset --hard" f.py',
            'echo "run git reset --hard first"',
            "rg --files-with-matches 'git reset --hard'",
        ],
    )
    def test_reset_mentions_are_silent(
        self, repo_with_ordinary_dirty_file: Path, cmd: str
    ) -> None:
        assert check_destructive_git_revert(cmd) is None
        assert check_destructive_git_revert_advisory(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            r'grep -i "def test\|checkout .\|git checkout ." f.py',
            'echo "run git checkout . first"',
            "rg --files-with-matches 'git checkout .'",
        ],
    )
    def test_checkout_mentions_are_silent(
        self, repo_with_ordinary_dirty_file: Path, cmd: str
    ) -> None:
        assert check_destructive_git_revert(cmd) is None
        assert check_destructive_git_revert_advisory(cmd) is None

    @pytest.mark.parametrize(
        "cmd",
        [
            r'grep -i "def test\|restore .\|git restore ." f.py',
            'echo "run git restore . first"',
            "rg --files-with-matches 'git restore .'",
        ],
    )
    def test_restore_mentions_are_silent(
        self, repo_with_ordinary_dirty_file: Path, cmd: str
    ) -> None:
        assert check_destructive_git_revert(cmd) is None
        assert check_destructive_git_revert_advisory(cmd) is None


# ---------------------------------------------------------------------------
# Advisory floor (2026-08-05): `affected` non-empty, `deny_paths` empty --
# see this function's own "Advisory floor" comment and
# cross-repo/inbox/2026-08-05-doe-claude-em-unscoped-stash-has-no-main-
# loop-guard.md. Covers `reset --hard`/`checkout .` alongside `stash`, since
# all three previously fell through the same silent `None` on a dirty-but-
# not-load-bearing tree.
# ---------------------------------------------------------------------------


def _advisory_context(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def repo_with_ordinary_dirty_file(tmp_path: Path) -> Path:
    """A real git repo with an uncommitted tracked edit OUTSIDE any
    load-bearing prefix (`_is_loadbearing`'s `state/`-rooted check) and no
    peer claim on it -- `affected` non-empty, `deny_paths` empty, the exact
    shape the advisory floor exists for."""
    repo = tmp_path / "ordinary-tree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    tracked = repo / "app.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    tracked.write_text("x = 2\n", encoding="utf-8")
    return repo


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    """A real git repo with no uncommitted changes at all -- `affected`
    stays empty, so neither a deny nor an advisory is ever warranted."""
    repo = tmp_path / "clean-tree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    return repo


class TestAdvisoryFloorDirtyNotLoadbearing:
    """A dirty tree with nothing load-bearing/peer-claimed in it: allow +
    additionalContext, never a deny -- per verb.

    Split 2026-08-05 (Review: staff-eng, Finding 0): the advisory now comes
    ONLY from `check_destructive_git_revert_advisory` -- the hard-deny leg
    (`check_destructive_git_revert`, still exercised directly below and
    throughout this file) returns `None` for this exact fixture shape, by
    design. `TestWirePathThroughDispatch` further down proves the two legs
    combine correctly through `evaluate_payload_json`, which is the shape
    that actually matters for chain-shadowing."""

    @pytest.mark.parametrize(
        "cmd_tail",
        ["stash", "stash push", "stash -u"],
    )
    def test_stash_advises(self, repo_with_ordinary_dirty_file: Path, cmd_tail: str) -> None:
        cmd = "git -C %s %s" % (repo_with_ordinary_dirty_file, cmd_tail)
        assert check_destructive_git_revert(cmd) is None
        result = check_destructive_git_revert_advisory(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "ADVISORY" in hso["additionalContext"]

    def test_reset_hard_advises(self, repo_with_ordinary_dirty_file: Path) -> None:
        cmd = "git -C %s reset --hard" % repo_with_ordinary_dirty_file
        assert check_destructive_git_revert(cmd) is None
        result = check_destructive_git_revert_advisory(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "ADVISORY" in hso["additionalContext"]

    @pytest.mark.parametrize("verb_cmd", ["checkout .", "checkout -- .", "restore ."])
    def test_checkout_restore_dot_advises(
        self, repo_with_ordinary_dirty_file: Path, verb_cmd: str
    ) -> None:
        cmd = "git -C %s %s" % (repo_with_ordinary_dirty_file, verb_cmd)
        assert check_destructive_git_revert(cmd) is None
        result = check_destructive_git_revert_advisory(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "ADVISORY" in hso["additionalContext"]


class TestCleanTreeStaysSilent:
    """`affected` empty (nothing uncommitted at all): no advisory, no deny."""

    @pytest.mark.parametrize(
        "cmd_tail",
        ["stash", "stash -u", "reset --hard", "checkout .", "restore ."],
    )
    def test_clean_tree_returns_none(self, clean_repo: Path, cmd_tail: str) -> None:
        assert check_destructive_git_revert("git -C %s %s" % (clean_repo, cmd_tail)) is None


class TestAdvisoryNeverDemotesADeny:
    """Regression: the advisory floor must not soften an existing deny --
    load-bearing/peer-claimed paths still hard-deny, per verb."""

    def test_reset_hard_on_loadbearing_still_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("git -C %s reset --hard" % repo_with_peer_work)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "state/peer-in-flight.md" in hso["permissionDecisionReason"]

    def test_checkout_dot_on_loadbearing_still_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("git -C %s checkout ." % repo_with_peer_work)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "state/peer-in-flight.md" in hso["permissionDecisionReason"]


class TestDenySegmentWinsOverAdvisorySegment:
    """One segment would only advise (ordinary dirty tree), a later segment
    would deny (load-bearing tree) -- the deny must win outright, per this
    function's own deny-precedence contract."""

    def test_deny_wins_across_segments(
        self, repo_with_ordinary_dirty_file: Path, repo_with_peer_work: Path
    ) -> None:
        cmd = "git -C %s stash; git -C %s stash" % (
            repo_with_ordinary_dirty_file,
            repo_with_peer_work,
        )
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "state/peer-in-flight.md" in hso["permissionDecisionReason"]

    def test_deny_wins_reverse_order(
        self, repo_with_ordinary_dirty_file: Path, repo_with_peer_work: Path
    ) -> None:
        cmd = "git -C %s stash; git -C %s stash" % (
            repo_with_peer_work,
            repo_with_ordinary_dirty_file,
        )
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "state/peer-in-flight.md" in hso["permissionDecisionReason"]


class TestScopedStashStillNone:
    """A `--`-delimited pathspec stays fully silent even on an ordinary
    dirty tree -- no advisory, no deny (unchanged forward-path)."""

    def test_scoped_stash_on_ordinary_dirty_tree_is_none(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        result = check_destructive_git_revert(
            "git -C %s stash push -- some/other/path.py" % repo_with_ordinary_dirty_file
        )
        assert result is None


def _wire_payload(command, cwd, agent_id=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(cwd),
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    return p


class TestWirePathThroughDispatch:
    """End-to-end through `dispatch.evaluate_payload_json`, not the bare
    function -- proves Finding 0's fix: the advisory leg
    (`destructive-git-revert-advisory`, ADVISORY_REWRITE, registered after
    every CONFINEMENT_DENY guard) never shadows a downstream hard deny.
    Before the fix, `check_destructive_git_revert` itself returned the
    advisory from its CONFINEMENT_DENY chain slot and `evaluate_payload_
    json` returned on the first non-None envelope -- these three repro
    commands are exactly the ones the reviewer reproduced live: all three
    used to come back `allow` (advisory) with the true downstream verdict
    never reached.

    Import deferred to inside each test body via `dispatch.evaluate_
    payload_json` (module attribute lookup, not a top-level import) so a
    JSON payload round-trip -- not the bare function -- is what is under
    test, matching every other wire-path suite in this package
    (`test_block_dev_repo_sentinel_removal.py`'s own "dispatch-level"
    section)."""

    def test_unscoped_stash_then_drop_denies_at_wire_level(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        import json

        from coordinator_core.bash_guards import dispatch

        cmd = "git -C %s stash && git -C %s stash drop" % (
            repo_with_ordinary_dirty_file,
            repo_with_ordinary_dirty_file,
        )
        out = dispatch.evaluate_payload_json(
            json.dumps(_wire_payload(cmd, repo_with_ordinary_dirty_file))
        )
        assert out is not None, "expected a deny envelope, got silent allow"
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny", (
            "block-stash-destruction must win over the advisory leg; got %r" % hso
        )

    def test_bare_stash_drop_denies_at_wire_level(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        import json

        from coordinator_core.bash_guards import dispatch

        cmd = "git -C %s stash drop" % repo_with_ordinary_dirty_file
        out = dispatch.evaluate_payload_json(
            json.dumps(_wire_payload(cmd, repo_with_ordinary_dirty_file))
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"

    def test_subagent_bare_stash_denies_at_wire_level(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        import json

        from coordinator_core.bash_guards import dispatch

        cmd = "git -C %s stash" % repo_with_ordinary_dirty_file
        out = dispatch.evaluate_payload_json(
            json.dumps(
                _wire_payload(cmd, repo_with_ordinary_dirty_file, agent_id="sub-1")
            )
        )
        assert out is not None, "expected a deny envelope, got silent allow"
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny", (
            "block-subagent-stash-creation must win over the advisory leg; got %r" % hso
        )

    def test_plain_reset_hard_still_advises_when_nothing_downstream_denies(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        """The floor's own advisory must still fire end-to-end when no
        downstream hard-deny guard has anything to say about this command --
        the fix must not turn the advisory into dead code."""
        import json

        from coordinator_core.bash_guards import dispatch

        cmd = "git -C %s reset --hard" % repo_with_ordinary_dirty_file
        out = dispatch.evaluate_payload_json(
            json.dumps(_wire_payload(cmd, repo_with_ordinary_dirty_file))
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "ADVISORY" in hso["additionalContext"]


class TestShellCRescanAdvisoryFloor:
    """The `_shell_c_unwrap_payloads` rescan branch of `_check_destructive_
    git_revert_full` (Finding 2): an advisory buried inside a `sh -c '...'`
    wrapper must still surface from `check_destructive_git_revert_advisory`,
    and a deny found by the rescan must still win outright over any
    advisory already pending from the outer scan -- exercising the
    `deny_result is not None: return deny_result, None` / `if pending_
    advisory is None: pending_advisory = advisory_result` branches that
    were previously untested (the pre-split function conflated both into a
    single `pending_advisory` accumulation with no dedicated coverage of
    the rescan leg specifically)."""

    def test_advisory_surfaces_through_sh_c_wrapper(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        cmd = "sh -c 'git -C %s stash'" % repo_with_ordinary_dirty_file
        assert check_destructive_git_revert(cmd) is None
        result = check_destructive_git_revert_advisory(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "ADVISORY" in hso["additionalContext"]

    def test_rescan_deny_wins_over_outer_pending_advisory(
        self, repo_with_ordinary_dirty_file: Path, repo_with_peer_work: Path
    ) -> None:
        # Outer scan: ordinary dirty tree -> would only advise.
        # Rescan (inside the sh -c wrapper): load-bearing tree -> denies.
        # The deny must win outright, and the hard-deny leg must surface it.
        cmd = "git -C %s stash; sh -c 'git -C %s stash'" % (
            repo_with_ordinary_dirty_file,
            repo_with_peer_work,
        )
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "state/peer-in-flight.md" in hso["permissionDecisionReason"]


class TestForceCheckoutWholeTree:
    """`git checkout -f` discards every uncommitted modification in the tree
    -- strictly MORE than the `git checkout .` this guard already denied.

    Until 2026-08-30 only a literal `.` pathspec built an `affected` set, so
    the larger clobber passed through silently while the smaller one was
    blocked. `block_subagent_destructive_action` had been hardened for this
    exact shape, but engages only after a subagent-identity check -- a
    main-loop or EM session on a shared worktree reached nothing. Observed
    live: a peer session destroyed ~40 files of in-flight work across this
    tree, unrecoverable (no commit, no stash, no reflog for worktree state).
    """

    @pytest.mark.parametrize(
        "flags",
        [
            "-f",
            "--force",
            "-f main",
            "-fb throwaway",
            "--force main",
        ],
    )
    def test_force_checkout_denies_over_peer_work(
        self, repo_with_peer_work: Path, flags: str
    ) -> None:
        result = check_destructive_git_revert(
            "git -C %s checkout %s" % (repo_with_peer_work, flags)
        )
        assert result is not None, "force checkout must not pass silently"
        assert "BLOCKED" in _deny_reason(result)

    def test_force_with_explicit_pathspec_stays_scoped(
        self, repo_with_peer_work: Path
    ) -> None:
        # `git checkout -f -- <paths>` IS scoped to those paths — narrower
        # than force alone, and must not be widened to whole-tree. The
        # pathspec named here is not the peer's file.
        result = check_destructive_git_revert(
            "git -C %s checkout -f -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    def test_force_dot_pathspec_still_denies(self, repo_with_peer_work: Path) -> None:
        # The pre-existing dotspec leg is unchanged by the force widening.
        result = check_destructive_git_revert(
            "git -C %s checkout -f -- ." % repo_with_peer_work
        )
        assert result is not None

    @pytest.mark.parametrize(
        "cmd",
        [
            "checkout main",
            "checkout -b feature",
            "checkout HEAD~1",
            "checkout -- app.py",
        ],
    )
    def test_benign_checkout_shapes_stay_silent(
        self, repo_with_peer_work: Path, cmd: str
    ) -> None:
        # The widening must not turn ordinary branch work into a deny: none
        # of these discards uncommitted content in the whole tree.
        result = check_destructive_git_revert(
            "git -C %s %s" % (repo_with_peer_work, cmd)
        )
        assert result is None, cmd

    def test_force_checkout_on_clean_tree_stays_silent(self, clean_repo: Path) -> None:
        # Nothing to destroy is not a deny — the guard denies on demonstrated
        # loss, never on the verb alone.
        result = check_destructive_git_revert(
            "git -C %s checkout -f" % clean_repo
        )
        assert result is None


class TestForceSwitchWholeTree:
    """`git switch -f <branch>` discards uncommitted work exactly as
    `git checkout -f <branch>` does (`git switch -h`: "-f, --force ... throw
    away local modifications"), and it is the spelling git's own docs steer
    people toward -- but `switch` was absent from this guard's verb set
    entirely until 2026-08-30, so every one of those invocations resolved to
    no verb and returned before any oracle ran.
    """

    @pytest.mark.parametrize("flags", ["-f main", "--force main", "-f"])
    def test_force_switch_denies_over_peer_work(
        self, repo_with_peer_work: Path, flags: str
    ) -> None:
        result = check_destructive_git_revert(
            "git -C %s switch %s" % (repo_with_peer_work, flags)
        )
        assert result is not None, "force switch must not pass silently"
        assert "BLOCKED" in _deny_reason(result)

    @pytest.mark.parametrize("cmd", ["switch main", "switch -c feature"])
    def test_benign_switch_shapes_stay_silent(
        self, repo_with_peer_work: Path, cmd: str
    ) -> None:
        result = check_destructive_git_revert(
            "git -C %s %s" % (repo_with_peer_work, cmd)
        )
        assert result is None, cmd

    def test_force_switch_on_clean_tree_stays_silent(self, clean_repo: Path) -> None:
        result = check_destructive_git_revert("git -C %s switch -f main" % clean_repo)
        assert result is None


def test_every_verb_resolution_path_shares_one_verb_set() -> None:
    """The verb set was three identical inline tuples, so a verb could be
    added to one resolution path and silently missed in the other two -- the
    shape that let `switch` be absent from all of them at once. Pin the
    single constant instead."""
    from coordinator_core.bash_guards.dispatch_checks import _GR_VERBS

    assert set(_GR_VERBS) == {"checkout", "restore", "reset", "stash", "switch"}

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

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _deny_reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture()
def repo_with_peer_work(tmp_path: Path) -> Path:
    repo = tmp_path / "shared-tree"
    (repo / "state").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    peer_file = repo / "state" / "peer-in-flight.md"
    peer_file.write_text("committed baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())

    peer_file.write_text("committed baseline\npeer's in-flight edit\n", encoding="utf-8")
    return repo


class TestBareStashSweepingPeerTrackedEdits:

    @pytest.mark.parametrize(
        "cmd",
        [
            "git stash",
            "git stash push",
            "git stash -u",
            'git stash push -m "wip"',
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
        # SC-DR-023 (CEPEF-B6): every one of these shapes is a whole-tree
        # form now denied from the command string alone -- the reason no
        # longer names a specific probed path.
        assert "SC-DR-023" in _deny_reason(result)

    @pytest.mark.parametrize(
        "template",
        [
            "git add -A && git -C %s stash",
            "git status; git -C %s stash -u",
            "git -C %s stash",
            "git log --oneline | head -3; git -C %s stash save",
        ],
    )
    def test_a_preceding_git_invocation_does_not_mask_the_stash(
        self, repo_with_peer_work: Path, template: str
    ) -> None:
        cmd = template % repo_with_peer_work
        result = check_destructive_git_revert(cmd)
        assert result is not None, "%r masked the stash behind an earlier git invocation" % cmd
        assert "SC-DR-023" in _deny_reason(result)

    def test_pathspec_scoped_stash_is_allowed(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert(
            "git -C %s stash push -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    @pytest.mark.parametrize("sub", ["list", "show", "create", "store"])
    def test_non_sweep_subcommands_are_allowed(self, repo_with_peer_work: Path, sub: str) -> None:
        result = check_destructive_git_revert("git -C %s stash %s" % (repo_with_peer_work, sub))
        assert result is None


class TestWindowsExeStashRealEntrypoint:
    """2026-07-28): the Windows-exe
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
        assert "SC-DR-023" in _deny_reason(result)

    def test_git_exe_uppercase_bare_stash_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("GIT.EXE -C %s stash" % repo_with_peer_work)
        assert result is not None, "GIT.EXE stash swept a peer's tracked work undetected"
        assert "SC-DR-023" in _deny_reason(result)

    def test_windows_spaced_path_backslash_git_exe_stash_denies(
        self, repo_with_peer_work: Path
    ) -> None:
        result = check_destructive_git_revert(
            "C:\\Program Files\\Git\\bin\\git.exe -C %s stash" % repo_with_peer_work
        )
        assert result is not None, "spaced-path git.exe stash swept a peer's tracked work undetected"
        assert "SC-DR-023" in _deny_reason(result)

    def test_windows_spaced_path_forward_slash_git_exe_stash_denies(
        self, repo_with_peer_work: Path
    ) -> None:
        result = check_destructive_git_revert(
            "C:/Program Files/Git/bin/git.exe -C %s stash" % repo_with_peer_work
        )
        assert result is not None, "spaced-path git.exe stash swept a peer's tracked work undetected"
        assert "SC-DR-023" in _deny_reason(result)

    def test_git_exe_pathspec_scoped_stash_still_allowed(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert(
            "git.exe -C %s stash push -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    def test_gitk_exe_bare_invocation_not_treated_as_git(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("gitk.exe -C %s stash" % repo_with_peer_work)
        assert result is None


class TestTrackedRowsAreCollected:

    def test_tracked_only_tree_still_denies(self, repo_with_peer_work: Path) -> None:
        assert not [p for p in repo_with_peer_work.rglob("*") if p.name.startswith("untracked")]
        result = check_destructive_git_revert("git -C %s stash -u" % repo_with_peer_work)
        assert result is not None
        assert "SC-DR-023" in _deny_reason(result)


class TestCommandReallyInvokes:

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("git stash", True),
            ("git status", False),
            ("", False),
            ("git -C /some/dir stash", True),
            ("git -c user.name=x stash", True),
            ("git --git-dir /d/.git stash", True),
            ("git --work-tree=/d stash", True),
            ("git --no-pager stash", True),
            ("git -P stash", True),
            ("git add -A && git stash", True),
            ("git status; git stash -u", True),
            ("git add -A && git commit -m x", False),
            ("/usr/bin/git stash", True),
            (r'grep -i "git stash" f.py', False),
            ('echo "git stash"', False),
            ("git.exe stash", True),
            (r"C:\Program Files\Git\bin\git.exe stash", True),
            ("C:/Program Files/Git/bin/git.exe stash", True),
            # Negative control: a basename that merely CONTAINS "git" must
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
            "git --unknown-flag stash",
            "git --unknown-flag status",
            'git commit -m "unterminated',
        ],
    )
    def test_unresolvable_fails_closed(self, cmd: str) -> None:
        assert _command_really_invokes(cmd, "stash") is True

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("git stash push", False),
            ('git stash push -m "x" -- +path', False),
            ("git stash push -f -- path", False),
            ("git stash save", False),
            ("git push", True),
            ("git push origin main", True),
            ("git push --force", True),
            ("git push -f origin main", True),
            ("git push origin +main:main", True),
            ("git -C /some/dir push --force", True),
            ("git --namespace n push --force", True),
            ("git stash push && git push --force", True),
        ],
    )
    def test_resolution_push_subcommand(self, cmd: str, expected: bool) -> None:
        assert _command_really_invokes(cmd, "push") is expected


class TestMentionIsNotInvocation:

    @pytest.mark.parametrize(
        "cmd",
        [
            r'grep -i "def test\|stash -u\|git stash" f.py',
            'echo "run git stash first"',
            'git commit -m "document why git stash is banned here"',
            "rg --files-with-matches 'git stash'",
        ],
    )
    def test_mentions_do_not_deny(self, repo_with_peer_work: Path, cmd: str) -> None:
        assert check_destructive_git_revert(cmd) is None


class TestMentionIsNotInvocationOtherVerbs:

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


def _advisory_context(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def repo_with_ordinary_dirty_file(tmp_path: Path) -> Path:
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
    """SC-DR-023 (CEPEF-B6): `stash`/`stash push`, bare `reset --hard`, and
    `checkout .`/`checkout -- .`/`restore .` are whole-tree forms and now
    deny unconditionally, from the command string alone -- they no longer
    fall through to this advisory floor. `stash -u` is the same bare-sweep
    shape (the `-u` flag widens WHAT is swept, not whether it is a sweep)
    and denies too."""

    @pytest.mark.parametrize(
        "cmd_tail",
        ["stash", "stash push", "stash -u"],
    )
    def test_stash_denies(self, repo_with_ordinary_dirty_file: Path, cmd_tail: str) -> None:
        cmd = "git -C %s %s" % (repo_with_ordinary_dirty_file, cmd_tail)
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]

    def test_reset_hard_denies(self, repo_with_ordinary_dirty_file: Path) -> None:
        cmd = "git -C %s reset --hard" % repo_with_ordinary_dirty_file
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]

    @pytest.mark.parametrize("verb_cmd", ["checkout .", "checkout -- .", "restore ."])
    def test_checkout_restore_dot_denies(
        self, repo_with_ordinary_dirty_file: Path, verb_cmd: str
    ) -> None:
        cmd = "git -C %s %s" % (repo_with_ordinary_dirty_file, verb_cmd)
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]


class TestCleanTreeStillDeniesWholeTreeForms:
    """SC-DR-023 (CEPEF-B6): a whole-tree form denies even against a clean
    tree, because the classification never runs `git status` at all --
    "clean tree" is not something this check can see for these shapes
    anymore. Named to replace the retired `TestCleanTreeStaysSilent`
    (pre-B6, when these same shapes fell through to `None`/advisory on a
    clean tree)."""

    @pytest.mark.parametrize(
        "cmd_tail",
        ["stash", "stash -u", "reset --hard", "checkout .", "restore ."],
    )
    def test_clean_tree_still_denies(self, clean_repo: Path, cmd_tail: str) -> None:
        result = check_destructive_git_revert("git -C %s %s" % (clean_repo, cmd_tail))
        assert result is not None
        assert "SC-DR-023" in result["hookSpecificOutput"]["permissionDecisionReason"]


class TestAdvisoryNeverDemotesADeny:
    """SC-DR-023 (CEPEF-B6): these two shapes are now whole-tree forms that
    deny unconditionally, before any `git status` probe -- so the deny
    reason no longer names a specific peer-claimed path (there was no probe
    to find one). The load-bearing/peer-claimed tree is kept here only to
    prove the deny still fires when there IS something at risk, not merely
    when the tree happens to be clean."""

    def test_reset_hard_on_loadbearing_still_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("git -C %s reset --hard" % repo_with_peer_work)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]

    def test_checkout_dot_on_loadbearing_still_denies(self, repo_with_peer_work: Path) -> None:
        result = check_destructive_git_revert("git -C %s checkout ." % repo_with_peer_work)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]


class TestDenySegmentWinsOverAdvisorySegment:
    """SC-DR-023 (CEPEF-B6): both segments here are bare `git stash` -- a
    whole-tree form that now denies unconditionally, from the command
    string alone, on the FIRST such segment encountered. Neither segment
    reaches the load-bearing/peer-claim probe any more, so this class no
    longer exercises deny-vs-pending-advisory precedence for stash; it
    pins that a chained pair of whole-tree segments still denies, in
    either order."""

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
        assert "SC-DR-023" in hso["permissionDecisionReason"]

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
        assert "SC-DR-023" in hso["permissionDecisionReason"]


class TestScopedStashStillNone:

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

    def test_plain_reset_hard_now_denies_at_wire_level(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        """SC-DR-023 (CEPEF-B6): bare `git reset --hard` is a whole-tree
        form and denies unconditionally -- it no longer reaches the
        advisory leg this test used to pin."""
        import json

        from coordinator_core.bash_guards import dispatch

        cmd = "git -C %s reset --hard" % repo_with_ordinary_dirty_file
        out = dispatch.evaluate_payload_json(
            json.dumps(_wire_payload(cmd, repo_with_ordinary_dirty_file))
        )
        assert out is not None
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]


class TestShellCRescanAdvisoryFloor:
    """SC-DR-023 (CEPEF-B6): a bare `git stash`, unwrapped from an `sh -c`
    payload, is a whole-tree form and now denies unconditionally -- it no
    longer reaches the advisory leg this suite's name once pinned."""

    def test_rescanned_bare_stash_now_denies(
        self, repo_with_ordinary_dirty_file: Path
    ) -> None:
        cmd = "sh -c 'git -C %s stash'" % repo_with_ordinary_dirty_file
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]

    def test_rescan_deny_wins_over_outer_pending_advisory(
        self, repo_with_ordinary_dirty_file: Path, repo_with_peer_work: Path
    ) -> None:
        cmd = "git -C %s stash; sh -c 'git -C %s stash'" % (
            repo_with_ordinary_dirty_file,
            repo_with_peer_work,
        )
        result = check_destructive_git_revert(cmd)
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert "SC-DR-023" in hso["permissionDecisionReason"]


class TestForceCheckoutWholeTree:

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
        result = check_destructive_git_revert(
            "git -C %s checkout -f -- some/other/path.py" % repo_with_peer_work
        )
        assert result is None

    def test_force_dot_pathspec_still_denies(self, repo_with_peer_work: Path) -> None:
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
        result = check_destructive_git_revert(
            "git -C %s %s" % (repo_with_peer_work, cmd)
        )
        assert result is None, cmd

    def test_force_checkout_on_clean_tree_stays_silent(self, clean_repo: Path) -> None:
        result = check_destructive_git_revert(
            "git -C %s checkout -f" % clean_repo
        )
        assert result is None


class TestForceSwitchWholeTree:

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
    from coordinator_core.bash_guards.dispatch_checks import _GR_VERBS

    assert set(_GR_VERBS) == {"checkout", "restore", "reset", "stash", "switch"}

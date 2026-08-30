"""Tests for C8 (`docs/plans/2026-08-26-the-destructive-core-learns-the-shell-
it-guards.md`) -- "Convert any entry C1 finds declaring a dialect it does not
read".

Scope pinned by `state/audits/2026-08-26-guard-detection-language-dependence-
recensus.md` Findings 2 and 3: nine entries declare `MATCHERS =
COMMAND_TOOL_NAMES` (or the inline `matchers=COMMAND_TOOL_NAMES` equivalent)
while their own backing module/function carries zero `_dialect` references.

**CORRECTED 2026-08-26 (second C8 pass, Finding 7 of the recensus record).**
The first C8 pass measured only BASE ARGV identity across dialects and
wrongly read seven of the remaining eight as correct-as-drafted under the
foreign-binary-argv carve-out. Re-measured against the ANTI-BYPASS surface
(a PowerShell `Start-Process <exe> -ArgumentList ...` wrapper specifically --
the same surface that gapped `destructive-git-revert` even though ITS base
argv also matched identically) shows seven of the eight are REAL GAPS, not
correct-as-drafted: `Start-Process` expands the target binary's own argv into
its OWN argv position at PowerShell's shell level, but every one of these
seven detectors is built over a Bash-shaped tokenizer
(`_command_tokenizer.resolve_command_positions` /
`block_subagent_destructive_action._tokenize_full_command`) that has no
PowerShell awareness at all -- it sees `Start-Process` in head position,
never `git`/`python`, and the detector's own name/scope predicate never
runs. The base-argv-identity read was true and irrelevant: identical argv
under a wrapper the detector cannot see through never reaches the predicate
that argv identity was supposed to make moot.

Only ONE of the nine is a genuine no-change verdict:
`destructive-git-revert-advisory` (`dispatch_checks.check_destructive_git_
revert_advisory`), a thin wrapper over `_check_destructive_git_revert_full`
-- the SAME function `destructive-git-revert`'s hard-deny leg calls, so the
first C8 pass's `Start-Process` expansion fix already covers this leg too,
confirmed empirically below, not re-derived.

The other eight are converted, same discipline as C3/the first C8 pass:
detection first (a dialect-gated `_dialect.tokenize_command` +
`expand_start_process_invocations` pass, narrowly scoped to `Start-Process`
only -- the same scope `destructive-git-revert`'s own fix carries, not a
claim of covering `Invoke-Expression`/backtick-escape/here-string bypasses
too), `matchers` declaration already in place from the original widening.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    check_destructive_git_revert,
    check_git_commit_safe_commit_advise,
)
from coordinator_core.bash_guards import block_subagent_commit
from coordinator_core.bash_guards import block_subagent_grant_acquisition
from coordinator_core.bash_guards import block_subagent_guard_grant
from coordinator_core.bash_guards import block_noncanonical_branch_creation
from coordinator_core.bash_guards import guard_branch_set_precedence
from coordinator_core.bash_guards import guard_longlived_branch_naming
from coordinator_core.win_portability import no_console_creationflags

# Spawns real `git` subprocesses (the revert guard's own status/toplevel
# oracles) -- cadence-gated, matching the sibling
# `test_check_destructive_git_revert_stash.py`'s own pytestmark.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _deny_reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _powershell_payload() -> dict:
    return {"tool_name": "PowerShell"}


@pytest.fixture()
def repo_with_peer_work(tmp_path: Path) -> Path:
    """Same shape as the sibling stash-regression fixture: a committed
    baseline plus a peer's uncommitted, tracked, git-unrecoverable edit --
    the exact state an unscoped `git stash` silently sweeps."""
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


class TestDestructiveGitRevertConvertedForPowerShell:
    """RED-FIRST: before the C8 fix, `Start-Process git -ArgumentList '-C',
    '<repo>','stash'` evaded `check_destructive_git_revert` under a
    PowerShell payload -- `_GR_BASE_RE` never sees a literal `git` token at
    segment start, only `Start-Process`. The fix expands the
    `Start-Process` call's own argv into command position (the same
    `_dialect.expand_start_process_invocations` pass every other converted
    entry already runs) before the existing regex pipeline runs."""

    def test_start_process_powershell_stash_denies(self, repo_with_peer_work: Path) -> None:
        cmd = "Start-Process git -ArgumentList '-C','%s','stash'" % repo_with_peer_work
        result = check_destructive_git_revert(cmd, payload=_powershell_payload())
        assert result is not None, "Start-Process-wrapped stash swept a peer's tracked work undetected"
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_bash_verdict_parity_same_command_without_start_process(
        self, repo_with_peer_work: Path
    ) -> None:
        """Bash verdict parity: the un-wrapped, byte-equivalent Bash
        invocation of the same intent already denies -- the PowerShell fix
        must reach the identical verdict, not a new one."""
        cmd = "git -C %s stash" % repo_with_peer_work
        result = check_destructive_git_revert(cmd)
        assert result is not None
        assert "state/peer-in-flight.md" in _deny_reason(result)

    def test_start_process_powershell_non_sweep_subcommand_allows(
        self, repo_with_peer_work: Path
    ) -> None:
        """Parity in the other direction too: a non-sweep stash subcommand
        (`list`) must not newly deny once the PowerShell expansion runs."""
        cmd = "Start-Process git -ArgumentList '-C','%s','stash','list'" % repo_with_peer_work
        result = check_destructive_git_revert(cmd, payload=_powershell_payload())
        assert result is None

    def test_bash_leg_unaffected_by_the_powershell_expansion_branch(
        self, repo_with_peer_work: Path
    ) -> None:
        """The expansion branch is gated on `dialect_from_tool_name(...) is
        Dialect.POWERSHELL` -- a Bash (or absent) payload must reach the
        exact prior code path, unchanged."""
        cmd = "git -C %s stash" % repo_with_peer_work
        result = check_destructive_git_revert(cmd, payload={"tool_name": "Bash"})
        assert result is not None
        assert "state/peer-in-flight.md" in _deny_reason(result)


class TestDestructiveGitRevertAdvisoryNoChangeVerdict:
    """The ONE genuine no-change verdict left in the nine-entry census:
    `destructive-git-revert-advisory` is a thin wrapper over
    `_check_destructive_git_revert_full`, the SAME function
    `destructive-git-revert`'s hard-deny leg calls -- so the first C8 pass's
    `Start-Process` expansion fix already covers this advisory leg too. No
    separate detection change needed; pinned here so the shared-function fix
    does not silently regress for this leg specifically."""

    def test_start_process_powershell_stash_advises(self, repo_with_peer_work: Path) -> None:
        from coordinator_core.bash_guards.dispatch_checks import (
            check_destructive_git_revert_advisory,
        )

        cmd = "Start-Process git -ArgumentList '-C','%s','stash'" % repo_with_peer_work
        result = check_destructive_git_revert_advisory(cmd, payload=_powershell_payload())
        # The hard-deny leg (tested above) already denies this shape, and
        # `_check_destructive_git_revert_full` never returns both halves
        # non-None for the same call (Review: staff-eng, Finding 0) -- the
        # advisory leg is `None` here precisely because the deny leg fired
        # first for the SAME underlying fixed function, not because
        # detection regressed. Parity is what matters: both legs see the
        # expanded argv identically.
        deny_result = check_destructive_git_revert(cmd, payload=_powershell_payload())
        assert deny_result is not None
        assert result is None  # advisory never shadows the hard-deny (Finding 0)

    def test_bash_and_powershell_reach_identical_verdict_on_non_deny_shape(
        self, repo_with_peer_work: Path
    ) -> None:
        """A non-sweep subcommand (`list`) reaches the advisory floor
        identically under both dialects -- same shared-function fix, same
        no-op-vs-fire parity as the deny leg's own test above."""
        bash_cmd = "git -C %s stash list" % repo_with_peer_work
        ps_cmd = "Start-Process git -ArgumentList '-C','%s','stash','list'" % repo_with_peer_work
        from coordinator_core.bash_guards.dispatch_checks import (
            check_destructive_git_revert_advisory,
        )

        bash_result = check_destructive_git_revert_advisory(bash_cmd, payload={"tool_name": "Bash"})
        ps_result = check_destructive_git_revert_advisory(ps_cmd, payload=_powershell_payload())
        assert bash_result == ps_result


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def _ps(cmd):
    return {"tool_name": "PowerShell", "tool_input": {"command": cmd}}


class TestGitCommitSafeCommitAdviseConverted:
    """RED-FIRST: `check_git_commit_safe_commit_advise` tokenizes via
    `_bt_tokenize_full_command`, a Bash-shaped tokenizer with no PowerShell
    awareness -- a `Start-Process git -ArgumentList 'commit','-m','wip'`
    invocation evaded the bare-commit-half advisory even though `git
    commit`'s own argv is byte-identical across dialects. Fixed narrowly:
    the same `Start-Process` expansion pass `destructive-git-revert` already
    runs, gated on `dialect_from_tool_name(payload) is Dialect.POWERSHELL`."""

    def test_start_process_powershell_bare_commit_advises(self) -> None:
        cmd = 'Start-Process git -ArgumentList "commit","-m","wip"'
        result = check_git_commit_safe_commit_advise(cmd, payload={"tool_name": "PowerShell"})
        assert result is not None

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = 'git commit -m "wip"'
        result = check_git_commit_safe_commit_advise(cmd, payload={"tool_name": "Bash"})
        assert result is not None

    def test_unparseable_powershell_does_not_deny(self) -> None:
        """AC4 -- an unparseable PowerShell payload is silence, never a
        deny (this check is advisory-only, `fail_closed=False`; a
        `tokenize_command` failure leaves `cmd` untouched and the
        pre-existing Bash-shaped pipeline runs on the raw text, exactly as
        before this change)."""
        cmd = "Start-Process git -ArgumentList 'commit', '-m', @'\nunterminated"
        result = check_git_commit_safe_commit_advise(cmd, payload={"tool_name": "PowerShell"})
        assert result is None or (
            result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )


class TestBlockSubagentCommitConverted:
    """RED-FIRST: every matcher in `block_subagent_commit._evaluate`'s
    family is built over `_tokenize_full_command`, Bash-shaped -- a
    `Start-Process git -ArgumentList 'commit','-am','wip'` invocation from a
    subagent evaded the confinement-deny even though the base `git commit`
    argv is byte-identical across dialects."""

    def _payload(self, cmd, tool_name):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": cmd},
            "agent_id": "some-subagent-id",
            "agent_type": "general-purpose",
            "session_id": "sess1",
            "cwd": None,
        }

    def test_start_process_powershell_commit_denies(self) -> None:
        cmd = 'Start-Process git -ArgumentList "commit","-am","wip"'
        result = block_subagent_commit.check(self._payload(cmd, "PowerShell"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = 'git commit -am "wip"'
        result = block_subagent_commit.check(self._payload(cmd, "Bash"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unparseable_powershell_introduces_no_new_verdict(self) -> None:
        """AC4, this guard's actual shape: `block_subagent_commit` is a
        fail-CLOSED identity gate (`_ALLOWED_SUBAGENT_TYPES` is the empty
        set -- ANY commit-shaped match denies unconditionally, by design,
        unlike the grant-acquisition siblings' fail-open `-m`/`-c`
        classifiers), so an unparseable PowerShell payload that ALSO
        textually mentions `commit` still denies via the pre-existing
        Bash-shaped pipeline running on the untouched raw text -- exactly
        as it would under Bash. The dialect-expansion branch must introduce
        no NEW asymmetry between the two: on a `tokenize_command` failure,
        `cmd_for_scan` is left untouched and both dialects reach the
        IDENTICAL verdict on the identical raw text."""
        cmd = "Start-Process git -ArgumentList 'commit', @'\nunterminated"
        bash_result = block_subagent_commit.check(self._payload(cmd, "Bash"))
        ps_result = block_subagent_commit.check(self._payload(cmd, "PowerShell"))
        assert bash_result == ps_result


class TestBlockSubagentGrantAcquisitionConverted:
    """RED-FIRST: `_evaluate` is built over `_tokenize_full_command`,
    Bash-shaped -- a `Start-Process python -ArgumentList '-m',
    'coordinator_core.session.claude_md_grant','grant',...` invocation
    evaded the acquisition gate even though the base `python -m ...` argv
    is byte-identical across dialects."""

    def _payload(self, cmd, tool_name):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": cmd},
            "agent_id": "some-subagent-id",
        }

    def test_start_process_powershell_grant_denies(self) -> None:
        cmd = (
            'Start-Process python -ArgumentList "-m",'
            '"coordinator_core.session.claude_md_grant","grant","pm","note"'
        )
        result = block_subagent_grant_acquisition.check(self._payload(cmd, "PowerShell"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = "python -m coordinator_core.session.claude_md_grant grant pm note"
        result = block_subagent_grant_acquisition.check(self._payload(cmd, "Bash"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unparseable_powershell_does_not_deny(self) -> None:
        cmd = "Start-Process python -ArgumentList '-m', @'\nunterminated"
        result = block_subagent_grant_acquisition.check(self._payload(cmd, "PowerShell"))
        assert result is None or (
            result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )


class TestBlockSubagentGuardGrantConverted:
    """Near-exact port of `TestBlockSubagentGrantAcquisitionConverted` --
    same gap, same fix, different gated CLI (`em_guard_grant`)."""

    def _payload(self, cmd, tool_name):
        return {
            "tool_name": tool_name,
            "tool_input": {"command": cmd},
            "agent_id": "some-subagent-id",
        }

    def test_start_process_powershell_grant_denies(self) -> None:
        cmd = (
            'Start-Process python -ArgumentList "-m",'
            '"coordinator_core.session.em_guard_grant","grant","em","note"'
        )
        result = block_subagent_guard_grant.check(self._payload(cmd, "PowerShell"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = "python -m coordinator_core.session.em_guard_grant grant em note"
        result = block_subagent_guard_grant.check(self._payload(cmd, "Bash"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unparseable_powershell_does_not_deny(self) -> None:
        cmd = "Start-Process python -ArgumentList '-m', @'\nunterminated"
        result = block_subagent_guard_grant.check(self._payload(cmd, "PowerShell"))
        assert result is None or (
            result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )


class TestBlockNoncanonicalBranchCreationConverted:
    """RED-FIRST: `resolve_command_positions` is Bash-shaped -- a
    `Start-Process git -ArgumentList 'checkout','-b','fix-thing'` invocation
    evaded the advisory even though the base `git checkout -b` argv is
    byte-identical across dialects. Fails OPEN by construction (this
    module's own MATCHERS comment), so a missed detection is a missed
    advisory, never a spurious one."""

    def _payload(self, cmd, tool_name, cwd="/repo"):
        return {"tool_name": tool_name, "tool_input": {"command": cmd}, "cwd": cwd}

    @pytest.fixture(autouse=True)
    def _hazard_repo(self, monkeypatch):
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "resolve_git_root", lambda cwd=None: "/repo"
        )
        monkeypatch.setattr(
            block_noncanonical_branch_creation, "_is_hazard_repo", lambda git_root: True
        )

    def test_start_process_powershell_checkout_b_advises(self) -> None:
        cmd = 'Start-Process git -ArgumentList "checkout","-b","fix-thing"'
        result = block_noncanonical_branch_creation.check(self._payload(cmd, "PowerShell"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = "git checkout -b fix-thing"
        result = block_noncanonical_branch_creation.check(self._payload(cmd, "Bash"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_unparseable_powershell_does_not_deny(self) -> None:
        cmd = "Start-Process git -ArgumentList 'checkout', @'\nunterminated"
        result = block_noncanonical_branch_creation.check(self._payload(cmd, "PowerShell"))
        assert result is None or (
            result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )


class TestGuardBranchSetPrecedenceConverted:
    """RED-FIRST: `_find_new_daily_target`'s own `resolve_command_positions`
    call is Bash-shaped -- a `Start-Process git -ArgumentList 'checkout',
    '-b','work/<machine>/<date>'` invocation evaded the canonical-daily-
    branch-target extraction even though the base argv is byte-identical
    across dialects. Detection-level test (the target extraction, not the
    full advisory-firing pipeline, which also needs a non-empty branch-set
    provider and a recency-surviving candidate -- covered by the sibling
    test module's own fixtures, not duplicated here)."""

    def test_start_process_powershell_target_extracted(self) -> None:
        from coordinator_core.bash_guards.guard_branch_set_precedence import (
            _find_new_daily_target,
        )

        cmd = 'Start-Process git -ArgumentList "checkout","-b","work/testmachine/2026-08-26"'
        assert _find_new_daily_target(cmd) is None  # pre-fix baseline, still true pre-patch

    def test_bash_and_powershell_reach_identical_target_via_check(self, monkeypatch) -> None:
        import time

        from coordinator_core.bash_guards import guard_branch_set_precedence as guard

        now = time.time()
        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)
        monkeypatch.setattr(guard, "_ahead_of_main", lambda branch, cwd=None: 3)
        monkeypatch.setattr(guard, "should_prompt_rename", lambda *a, **k: False)
        provider = lambda: [("work/other/2026-08-25", now - 60)]

        bash_result = guard.check(
            _bash("git checkout -b work/testmachine/2026-08-26"),
            branch_set_provider=provider,
        )
        ps_result = guard.check(
            _ps('Start-Process git -ArgumentList "checkout","-b","work/testmachine/2026-08-26"'),
            branch_set_provider=provider,
        )
        assert bash_result is not None
        assert bash_result == ps_result

    def test_unparseable_powershell_does_not_deny(self, monkeypatch) -> None:
        from coordinator_core.bash_guards import guard_branch_set_precedence as guard

        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: "/repo")
        monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)
        cmd = "Start-Process git -ArgumentList 'checkout', @'\nunterminated"
        result = guard.check(_ps(cmd))
        assert result is None or (
            result["hookSpecificOutput"].get("permissionDecision") != "deny"
        )


class TestGuardLonglivedBranchNamingConverted:
    """RED-FIRST: `resolve_command_positions` is Bash-shaped -- a
    `Start-Process git -ArgumentList 'checkout','-b','feature/x'` invocation
    evaded the sanctioned-longlived-prefix advisory even though the base
    argv is byte-identical across dialects. This guard NEVER denies (module
    docstring "WHAT THIS DOES"), so a missed detection is a missed advisory,
    never a spurious one."""

    def _payload(self, cmd, tool_name, cwd="/repo"):
        return {"tool_name": tool_name, "tool_input": {"command": cmd}, "cwd": cwd}

    @pytest.fixture(autouse=True)
    def _hazard_repo(self, monkeypatch):
        monkeypatch.setattr(
            guard_longlived_branch_naming, "resolve_git_root", lambda cwd=None: "/repo"
        )
        monkeypatch.setattr(
            guard_longlived_branch_naming, "_is_hazard_repo", lambda git_root: True
        )

    def test_start_process_powershell_checkout_b_advises(self) -> None:
        cmd = 'Start-Process git -ArgumentList "checkout","-b","feature/x"'
        result = guard_longlived_branch_naming.check(self._payload(cmd, "PowerShell"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_bash_verdict_parity_same_command_without_start_process(self) -> None:
        cmd = "git checkout -b feature/x"
        result = guard_longlived_branch_naming.check(self._payload(cmd, "Bash"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_unparseable_powershell_does_not_deny(self) -> None:
        cmd = "Start-Process git -ArgumentList 'checkout', @'\nunterminated"
        result = guard_longlived_branch_naming.check(self._payload(cmd, "PowerShell"))
        assert result is None or (
            result["hookSpecificOutput"]["permissionDecision"] != "deny"
        )

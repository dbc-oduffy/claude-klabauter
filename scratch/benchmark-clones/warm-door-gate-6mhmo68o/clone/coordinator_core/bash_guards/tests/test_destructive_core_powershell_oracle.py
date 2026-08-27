"""coordinator_core.bash_guards.tests.test_destructive_core_powershell_oracle
-- C3 of `docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-
guards.md` (AC5).

Pins the eight-row oracle from that plan's own `## Problem` section as a
runtime test, going through the REAL entrypoint,
`coordinator_core.bash_guards.dispatch.evaluate_payload_json`, never
`dispatch_checks.check_destructive_rm(...)` directly -- bypassing the
dispatcher's registration order (a hand-built literal list) would miss a
swallowed slot or a `matchers` gate that never actually widened.

Before this chunk, one of the eight rows denied: `Bash` / `rm -rf .git`.
After this chunk, every `Remove-Item`/`ri` spelling of the identical intent
denies under BOTH `tool_name` values.

Those rows stayed dual-legged when AC2a was retired on 2026-08-26 and the
git-shaped anti-bypass scan started gating on the declared dialect, and the
distinction is the point: `check_destructive_rm`'s PowerShell verb scan is
what makes `Remove-Item -Recurse -Force .git` deny under a `"Bash"` label,
so gating IT would have dropped this oracle from 8/8 to 5/8. The dialect
gate belongs on `_ps_git_bypass_segments`, which only ever ADDED a Bash-leg
deny, never carried one of these. The original justification for both
running ungated -- DoE-claude's `_rearm_command_tool_name` relabeling
genuine PowerShell payloads to `"Bash"` ahead of dispatch -- is gone with
that symbol (their D1, `47f4aedfe`); these rows survive on their own merit,
which is that PowerShell removal vocabulary in a bash-labeled payload is
still a removal nobody should get to perform.

Spec backlink: pln-the-destructive-core-learns-the-she § C3, AC5
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.bash_guards import dispatch

#: NO module-level `cadence`/`spawns_process` mark. Every test below runs
#: against `repo_cwd` (this checkout, read-only) and spawns nothing, so
#: these rows belong on the fast tier. The one oracle class that DID need a
#: real `git init` -- `destructive-git-clean` -- moved to
#: `test_destructive_core_powershell_oracle_git_clean.py` on 2026-08-26,
#: which carries the module-level mark for the reason that file's docstring
#: gives. Before that split its fixture tiered all sixteen rows here onto
#: `cadence`, and retiring AC2a changed three of them while the fast tier
#: reported green -- a gate pinning cross-plane guard behaviour has to run
#: on the tier people actually run.


#: The four spellings of "delete the git store" from the plan's own oracle
#: table, kept as a list (not four separate hardcoded calls) so the row
#: shape stays visibly 1:1 with the plan body. Row 0 (`rm -rf .git`) is the
#: pre-existing Bash-covered row; rows 1-3 are the PowerShell spellings C3
#: adds. Both tests below index this list rather than re-spelling it, so a
#: row edited here cannot silently diverge from what is actually asserted.
_SPELLINGS = [
    "rm -rf .git",
    "Remove-Item -Recurse -Force .git",
    "ri -Recurse -Force .git",
    "Remove-Item .git -Recurse -Force",
]

#: `tool_name` values the oracle exercises PER spelling -- the plan's table
#: covers `Bash` for all four rows plus `PowerShell` for the latter three
#: (row 1, `rm -rf .git` under `PowerShell`, is out of the plan's own named
#: eight rows -- `rm` is not native PowerShell vocabulary at all -- so this
#: table stays the exact eight the plan names, not a fabricated ninth).
_TOOL_NAMES = ("Bash", "PowerShell")


def _payload(command: str, tool_name: str, cwd: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "oracle-sess",
        "cwd": cwd,
    }


def _denies(command: str, tool_name: str, cwd: str) -> bool:
    out = dispatch.evaluate_payload_json(json.dumps(_payload(command, tool_name, cwd)))
    if out is None:
        return False
    # `evaluate_payload_json` is typed `Dict | List[Dict] | None` -- the
    # list shape is what an advisory-collecting dispatch returns, and a
    # bare `.get` on it raises `AttributeError` INSIDE the assert helper,
    # which surfaces as an oracle error rather than as the deny/silence
    # verdict this file exists to read. Handle both shapes.
    rows = out if isinstance(out, list) else [out]
    return any(
        row.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        for row in rows
    )


@pytest.fixture()
def repo_cwd() -> str:
    """`cwd` = this repo root, per the plan's own oracle preamble.

    `check_destructive_rm`'s target-existence probe (`os.path.exists`) and
    its `git rev-parse --show-toplevel` calls resolve against the PROCESS's
    own working directory, not `payload["cwd"]` -- this fixture asserts that
    invariant holds for wherever this test suite happens to run, rather than
    silently passing against the wrong `.git` (or none at all).
    """
    cwd = os.getcwd()
    assert os.path.isdir(os.path.join(cwd, ".git")), (
        "this oracle requires the process cwd to be a real git checkout "
        "(found no %r) -- see check_destructive_rm's own docstring for why "
        "cwd, not payload['cwd'], governs its target-existence probe"
        % os.path.join(cwd, ".git")
    )
    return cwd


class TestEightRowOracle:
    """AC5: `rm -rf .git` denies under both tool names (it already did for
    `Bash`; `PowerShell` is new coverage this chunk did not have to add
    detection for -- `rm` has no PowerShell-native spelling, so this row is
    unaffected by C3's vocabulary work and is pinned here only so a future
    regression on the `Bash` leg cannot hide behind this file's own
    silence). Every `Remove-Item`/`ri` spelling denies under both."""

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_rm_rf_dot_git_denies(self, repo_cwd, tool_name) -> None:
        assert _denies(_SPELLINGS[0], tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    @pytest.mark.parametrize("command", _SPELLINGS[1:])
    def test_powershell_spelling_denies(self, repo_cwd, command, tool_name) -> None:
        assert _denies(command, tool_name, repo_cwd), (
            "%r under tool_name=%r must deny post-C3 -- pre-C3 this was "
            "one of the seven silent rows the plan's own oracle table "
            "names" % (command, tool_name)
        )


class TestVocabularyDetail:
    """AC3: abbreviated flags, flag order, and the least-common alias
    (`erase`/`rd`/`del`) all resolve -- not merely the three exact spellings
    the plan's own table lists."""

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_abbreviated_flags_deny(self, repo_cwd, tool_name) -> None:
        assert _denies("Remove-Item -r -fo .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_rd_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("rd -Recurse -Force .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_del_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("del -Recurse -Force .git", tool_name, repo_cwd)

    @pytest.mark.parametrize("tool_name", _TOOL_NAMES)
    def test_erase_alias_denies(self, repo_cwd, tool_name) -> None:
        assert _denies("erase -Recurse -Force .git", tool_name, repo_cwd)

    def test_ordinary_powershell_file_removal_still_allowed(self, repo_cwd) -> None:
        """Negative spec: an unrelated `Remove-Item` on a non-hazard path
        must not deny -- this guard polices the TARGET, not the verb."""
        assert not _denies(
            "Remove-Item -Recurse -Force ./scratch-not-a-real-path-oracle-probe",
            "PowerShell",
            repo_cwd,
        )

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        """AC4: a PowerShell command the tree-sitter-pwsh parser cannot
        tokenize must yield silence, never a deny -- fail-closed-on-
        unparseable is a BASH-leg-only property (see `_dialect.py`'s own
        "THE CRITICAL PROPERTY IS FAIL DIRECTION")."""
        assert not _denies("Remove-Item -Recurse -Force @'", "PowerShell", repo_cwd)


class TestBashLegUnchanged:
    """AC2: the Bash leg's own verdict on non-`.git` targets, and on the
    existing corpus, is untouched by this chunk's PowerShell addition."""

    def test_bash_rm_of_ordinary_file_untouched(self, tmp_path, repo_cwd) -> None:
        scratch = tmp_path / "scratch-oracle-file.txt"
        scratch.write_text("scratch")
        assert not _denies("rm %s" % scratch, "Bash", repo_cwd)


class TestDestructiveGitOrphanForcePush:
    """C3: `destructive-git-orphan`'s CHECK 2 (force push) as the anti-
    bypass trigger, per the dispatch brief's explicit warning not to build
    this check's own oracle on `git reset --hard` at chain altitude (a
    dirty tree's stateful peer-claim guard denies first and masks this
    guard entirely). Force-push is a pure argv/text check with no such
    stateful leg."""

    def test_plain_force_push_denies_under_both_dialects(self, repo_cwd) -> None:
        """Before C3, `tool_name="PowerShell"` never reached this check at
        all (`matchers=("Bash",)`) -- this is the before/after transition
        AC5 pins, not new detection vocabulary (git argv is unchanged)."""
        cmd = "git push --force origin main"
        assert _denies(cmd, "Bash", repo_cwd)
        assert _denies(cmd, "PowerShell", repo_cwd)

    def test_backtick_escaped_git_denies_under_both_dialects(self, repo_cwd) -> None:
        """The anti-bypass surface itself: a backtick inside the literal
        word `git` defeats the pre-existing `\\bgit\\b` raw-text scan (the
        backtick is a non-word character, splitting `git` into two
        tokens) but is resolved cleanly by tree-sitter-pwsh's own escape
        handling, then rewritten back to plain `git ...` text by
        `_ps_git_bypass_segments` before the existing regex ladder ever
        sees it.

        AC2a RETIRED (2026-08-26). This row used to assert a deny under
        BOTH declared `tool_name` values, because `_ps_git_bypass_
        segments` ran unconditionally -- forced, while DoE-claude's
        `_rearm_command_tool_name` relabeled genuine PowerShell payloads
        to `"Bash"` ahead of dispatch. That relabel is gone (their D1,
        `47f4aedfe`), so the scan gates on the declared dialect and
        `"Bash"` is once again evidence the caller really used bash.
        Under real bash a backtick inside `git` is command substitution,
        which would never have run `git push --force` -- the Bash-leg
        silence restores pre-change behaviour, it does not lose a deny."""
        cmd = "g`it push --force origin main"
        assert _denies(cmd, "PowerShell", repo_cwd)
        assert not _denies(cmd, "Bash", repo_cwd)

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        """AC4: a command that is BOTH backtick-broken (so the raw
        `\\bgit\\b` scan cannot catch it on its own -- otherwise this row
        would be a true positive via the pre-existing scan, proving
        nothing about the PowerShell parse-failure path) AND carries an
        unterminated here-string (so `resolve_segments_for_dialect`
        itself returns `None`) must yield silence, never a deny, under
        the widened matchers."""
        assert not _denies("g`it push --force origin main @'", "PowerShell", repo_cwd)
        assert not _denies("g`it push --force origin main @'", "Bash", repo_cwd)



@pytest.mark.real_home
class TestBlanketGitAdd:
    """C3: `blanket-git-add` requires a fleet-registered hazard repo
    (`_is_hazard_repo`) -- `repo_cwd` (this checkout, claude-klabauter
    itself) rather than a fresh throwaway repo. `real_home`: `_is_hazard_
    repo`'s fleet registry read resolves through the real machine-local
    registry -- under the suite's default home-quarantine this checkout
    is never recognized as hazard-registered and the guard silently
    no-ops, which is a test-harness artifact, not a guard defect (read-
    only oracle, per this marker's own docstring)."""

    def test_backtick_escaped_git_add_denies_on_the_powershell_leg_only(self, repo_cwd) -> None:
        """`_ga_ps_segments` gates on the declared dialect (same rationale
        as the two sibling classes above -- AC2a retired 2026-08-26), so
        this denies on the PowerShell leg and is silent on the Bash leg."""
        cmd = "g`it add -A"
        assert _denies(cmd, "PowerShell", repo_cwd)
        assert not _denies(cmd, "Bash", repo_cwd)

    def test_plain_git_add_dash_a_denies_under_both_dialects(self, repo_cwd) -> None:
        cmd = "git add -A"
        assert _denies(cmd, "Bash", repo_cwd)
        assert _denies(cmd, "PowerShell", repo_cwd)

    def test_unparseable_powershell_does_not_deny(self, repo_cwd) -> None:
        """AC4: backtick-broken AND an unterminated here-string -- silence
        under both dialects, never a deny."""
        assert not _denies("g`it add -A @'", "PowerShell", repo_cwd)
        assert not _denies("g`it add -A @'", "Bash", repo_cwd)

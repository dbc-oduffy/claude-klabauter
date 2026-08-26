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
denies under BOTH `tool_name` values -- `Bash` (the `tool_name: "Bash"`
carrying PowerShell-native command text shape DoE-claude's own
`_rearm_command_tool_name` produces, per the plan's own "Row 2 is the
load-bearing row") and `PowerShell` (the harness's own native tool_name,
once every declared-matchers/master-gate seam actually admits it).

Spec backlink: pln-the-destructive-core-learns-the-she § C3, AC5
"""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.bash_guards import dispatch


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

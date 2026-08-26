"""coordinator_core.bash_guards.tests.test_destructive_core_powershell_
oracle_git_clean -- the spawning half of the C3/AC5 oracle
(`docs/plans/2026-08-26-the-destructive-core-learns-the-shell-it-guards.md`).

SPLIT OUT 2026-08-26. `destructive-git-clean` is the one oracle class whose
fixture builds a real repo with `git init`, and the spawn ratchet's
per-function `spawns_process`/`cadence` marks are INERT for a spawner pytest
reaches only via fixture injection (`coordinator_core/tests/test_no_new_
spawning_tests.py` § Rule 4, condition (ii)). The module-level form is
therefore the only honest way to tier it -- and while this class shared a
module with the other fifteen oracle rows, that honesty tiered all of them
onto the cadence gate too, by the sibling's own docstring admission.

The cost was live, not theoretical: retiring AC2a on 2026-08-26 changed the
verdict of three oracle rows, and the fast tier reported green throughout
because every row asserting the old behaviour sat behind `cadence`. A gate
that pins cross-plane guard behaviour has to run on the tier people actually
run. The fifteen non-spawning rows now do, in the sibling file; these three
stay here, correctly marked, because a real `git init` genuinely is what they
are asserting against.

Spec backlink: pln-the-destructive-core-learns-the-she § C3, AC5
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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



@pytest.fixture()
def fresh_git_repo(tmp_path, monkeypatch):
    """An isolated, freshly-`git init`ed repo -- `check_destructive_git_
    clean`'s oracle resolves the process cwd (no `-C` in the synthetic
    segment), so this fixture `chdir`s into it for the test's duration
    rather than fabricating output. NOT a fleet-registered repo (see
    `_is_hazard_repo`), so unsuitable for `blanket-git-add` tests -- those
    use `repo_cwd` (this checkout) instead."""
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.email", "oracle@example.invalid"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        ["git", "config", "user.name", "oracle"], cwd=str(tmp_path), check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "scratch.txt").write_text("load-bearing per _GC_LOADBEARING_PREFIXES")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDestructiveGitCleanLoadBearing:
    """C3: `destructive-git-clean`'s own top-of-function gate (`\\bgit\\b`
    AND `\\bclean\\b` over raw `cmd`) is a second bypass point beyond the
    per-segment scan -- widened via `_gc_ps_segments` alongside the loop
    itself, or a backtick-escaped invocation would short-circuit to
    `None` before ever reaching the segment walk."""

    def test_backtick_escaped_git_clean_denies_on_the_powershell_leg_only(self, fresh_git_repo) -> None:
        """`_gc_ps_segments` gates on the declared dialect (same rationale
        as `TestDestructiveGitOrphanForcePush`'s own backtick row -- AC2a
        retired 2026-08-26 once DoE's relabel was removed), so this denies
        on the PowerShell leg and is silent on the Bash leg."""
        cmd = "g`it clean -fdx"
        cwd = str(fresh_git_repo)
        assert _denies(cmd, "PowerShell", cwd)
        assert not _denies(cmd, "Bash", cwd)

    def test_plain_git_clean_denies_under_both_dialects(self, fresh_git_repo) -> None:
        cmd = "git clean -fdx"
        cwd = str(fresh_git_repo)
        assert _denies(cmd, "Bash", cwd)
        assert _denies(cmd, "PowerShell", cwd)

    def test_unparseable_powershell_does_not_deny(self, fresh_git_repo) -> None:
        """AC4: backtick-broken (defeats the raw top-of-function gate) AND
        an unterminated here-string (`resolve_segments_for_dialect`
        returns `None`) -- silence under both dialects, never a deny."""
        cwd = str(fresh_git_repo)
        assert not _denies("g`it clean -fdx @'", "PowerShell", cwd)
        assert not _denies("g`it clean -fdx @'", "Bash", cwd)


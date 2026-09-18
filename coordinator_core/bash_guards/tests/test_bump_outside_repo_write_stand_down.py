"""The OUTSIDE-repo write bump (C5) stands down where its premise is false.

WHY THIS FILE EXISTS AT ALL. The environment stand-down was authored in C4
(`bump_foreign_repo_write.py`) and never ported, so on a managed remote
container -- one session, a closed set of repos granted to it together, no
peer team, no memo reader -- C4 allowed a cross-repo `git commit` while this
guard and C7 still denied the identical write. That asymmetry, not the
stand-down, was the defect. These tests pin THIS surface's half of the fix;
the shared mechanism's own contract is tested in
`test_bump_foreign_repo_write_stand_down.py` and lives in
`_write_bump_stand_down.py`.

THREE PROPERTIES, both legs (this guard has a bash and a PowerShell deny
site, and a port that reached only one of them is the same bug again):

  1. `fleet_present` false -> the guard returns `None`, prints an
     after-the-fact advisory, and leaves a durable audit line. `None`, not an
     envelope: `dispatch`'s chain loop is `if out is not None: return out`, so
     any value here would claim the slot and silently skip every guard
     registered after this one.
  2. `fleet_present` true -> the deny is byte-for-byte what it was. A
     stand-down that leaks onto a fleet workstation is a deleted guard
     wearing a warning's clothes.
  3. The capability layer raising -> the guard behaves exactly as it did
     before the stand-down existed (deny). Fail open toward TODAY'S
     behaviour, like every other branch in this family.

The package `conftest.py` pins `fleet_present` on for every test here
(`coordinator_core/testing/fleet_pin.py`); the `cloud_container` fixture
below is what overrides that pin, and it runs after the autouse one.
"""

from __future__ import annotations

import pytest

from coordinator_core import environment
from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.bash_guards.tests.test_bump_outside_repo_write import (
    _clean_bump_env,  # noqa: F401 -- reused fixture, same isolation this guard's own suite needs.
    _posix,
    _set_anchor,
    env,  # noqa: F401 -- reused fixture: anchor repo + an outside-any-repo scratch dir.
    requires_powershell_grammar,
)

# The `env` fixture builds real git repos; declared, not inherited silently.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture()
def cloud_container(monkeypatch):
    """A managed remote container: the capability pin the package conftest
    applies is removed and the venue markers a real one sets are put back."""
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")


@pytest.fixture()
def exploding_capability(monkeypatch):
    """The capability layer itself unhappy -- the fail-open leg. Patched at
    `coordinator_core.environment.capability`, the attribute
    `_write_bump_stand_down` imports inside its own function body, so the
    raise happens where the guard actually consults it."""

    def boom(*args, **kwargs):
        raise RuntimeError("capability layer unavailable")

    monkeypatch.setattr(environment, "capability", boom)


def _outside_write_cmd(env) -> str:
    return "echo hi > %s" % _posix(env["outside"] / "redir.txt")


# ---------------------------------------------------------------------------
# Bash leg -- `check_bump_outside_repo_write`.
# ---------------------------------------------------------------------------


def test_bash_leg_stands_down_on_a_host_with_no_fleet(env, monkeypatch, capsys):
    """Property 1. The write proceeds, and says so rather than scrolling
    past in silence."""
    _set_anchor(monkeypatch, env, "sess-sd-bash")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")

    result = guard.check_bump_outside_repo_write(
        _outside_write_cmd(env), "sess-sd-bash", str(env["anchor"]), {}
    )

    assert result is None, (
        "a stood-down bump must return None so the guard chain continues -- "
        "any envelope claims dispatch's slot and skips every later guard"
    )
    assert "outside-repo-write" in capsys.readouterr().err


def test_bash_leg_stand_down_leaves_a_durable_audit_line(env, monkeypatch):
    """Property 1's other half. Without the line, "permissive and warn"
    degrades to "permissive" and the boundary stops existing rather than
    becoming advisory."""
    _set_anchor(monkeypatch, env, "sess-sd-bash-audit")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")

    assert (
        guard.check_bump_outside_repo_write(
            _outside_write_cmd(env), "sess-sd-bash-audit", str(env["anchor"]), {}
        )
        is None
    )

    logs = list(env["anchor"].rglob("overrides.log"))
    assert logs, "the stand-down left no trace in the session's own repo"
    body = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    assert "STAND-DOWN-OUTSIDE-REPO-WRITE" in body, (
        "the audit token is per-surface on purpose -- three guards standing "
        "down must leave three distinguishable records"
    )


def test_bash_leg_still_denies_where_the_premise_holds(env, monkeypatch):
    """Property 2, resting on the package pin rather than restating it."""
    _set_anchor(monkeypatch, env, "sess-sd-bash-fleet")

    result = guard.check_bump_outside_repo_write(
        _outside_write_cmd(env), "sess-sd-bash-fleet", str(env["anchor"]), {}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_leg_denies_when_the_capability_layer_raises(
    env, monkeypatch, cloud_container, exploding_capability
):
    """Property 3. The venue markers say "stand down" and the capability
    layer cannot answer -- the guard must then behave as it did before the
    stand-down existed, not guess."""
    _set_anchor(monkeypatch, env, "sess-sd-bash-broken-cap")

    result = guard.check_bump_outside_repo_write(
        _outside_write_cmd(env), "sess-sd-bash-broken-cap", str(env["anchor"]), {}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# PowerShell leg -- `_check_bump_outside_repo_write_powershell`. A separate
# deny site, so a separate set of the same three properties: the C4-only
# window this fix closes is precisely what a half-applied port recreates.
# ---------------------------------------------------------------------------


@requires_powershell_grammar
def test_powershell_leg_stands_down_on_a_host_with_no_fleet(env, monkeypatch, capsys):
    _set_anchor(monkeypatch, env, "sess-sd-ps")
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")
    cmd = "New-Item -Path %s -ItemType File" % (env["outside"] / "ps-newfile.txt")

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-sd-ps", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is None
    err = capsys.readouterr().err
    assert "outside-repo-write" in err
    logs = list(env["anchor"].rglob("overrides.log"))
    assert logs and any(
        "STAND-DOWN-OUTSIDE-REPO-WRITE" in p.read_text(encoding="utf-8") for p in logs
    )


@requires_powershell_grammar
def test_powershell_leg_still_denies_where_the_premise_holds(env, monkeypatch):
    _set_anchor(monkeypatch, env, "sess-sd-ps-fleet")
    cmd = "New-Item -Path %s -ItemType File" % (env["outside"] / "ps-fleet.txt")

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-sd-ps-fleet", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


@requires_powershell_grammar
def test_powershell_leg_denies_when_the_capability_layer_raises(
    env, monkeypatch, cloud_container, exploding_capability
):
    _set_anchor(monkeypatch, env, "sess-sd-ps-broken-cap")
    cmd = "New-Item -Path %s -ItemType File" % (env["outside"] / "ps-broken.txt")

    result = guard.check_bump_outside_repo_write(
        cmd, "sess-sd-ps-broken-cap", str(env["anchor"]), {"tool_name": "PowerShell"}
    )

    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

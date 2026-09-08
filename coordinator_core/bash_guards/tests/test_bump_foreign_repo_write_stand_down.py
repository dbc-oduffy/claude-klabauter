"""The cross-repo write bump stands down where its premise is false.

WHY. Measured on a managed remote container, 2026-09-05: the session holds a
closed set of repos by explicit grant, coordinator is not installed, and no
other agent runs on the box. The "foreign" repo is one this same session was
handed and is the only writer of. Denying that write protects nobody -- while
the alternative doctrine offers (send a cross-repo memo) has no reader on that
host either. So the guard forbade the only correct move while leaving the
incorrect one available, inverting the north star it exists to serve.

WHAT MUST NOT DRIFT, and each of these is a separate way to get the fix wrong:

  1. Standing down is NOT auto-approving. The advisory envelope carries no
     `permissionDecision`, so the ordinary permission prompt still happens.
     Returning `allow` here would be MORE permissive than deleting the guard.
  2. Standing down is NOT silent. A block leaves evidence by stopping the
     world; a warning scrolls past. Without the audit line, "permissive and
     warn" degrades to "permissive" and the boundary stops existing rather
     than becoming advisory.
  3. On a real fleet machine NOTHING changes. A stand-down that leaks into a
     workstation is a deleted guard wearing a warning's clothes.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as guard


@pytest.fixture()
def fleet_machine(monkeypatch, tmp_path):
    """A durable host with coordinator installed — the environment every line
    of this guard was written for."""
    home = tmp_path / "settings-home"
    home.mkdir()
    (home / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("coordinator_core._settings_home.settings_home", lambda: home)
    for var in ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_CONTAINER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")


@pytest.fixture()
def cloud_container(monkeypatch):
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")


def test_fleet_machine_enforces_exactly_as_before(fleet_machine):
    """Property 3. The stand-down must be invisible where the premise holds."""
    assert guard._environment_stands_the_bump_down() is None


def test_cloud_container_stands_the_bump_down(cloud_container):
    stood = guard._environment_stands_the_bump_down()
    assert stood is not None
    assert stood.value is False
    assert stood.evidence.strip()


def test_stand_down_returns_none_so_the_guard_chain_continues(cloud_container, capsys):
    """Property 1, REWRITTEN after the first version of it missed a critical.

    The original asserted only that the advisory carried no
    `permissionDecision` — true, and beside the point. `dispatch`'s chain loop
    is `if out is not None: return out`, so ANY non-None envelope claims the
    slot and ends evaluation: the advisory silently skipped every guard
    registered after this one, `validate-commit` included. The deny it replaced
    short-circuited identically, which is why it looked safe — a deny makes the
    skipped guards moot, an allow does not.

    So the property is not "decides nothing"; it is "returns nothing". That is
    what lets the chain continue, and it is the only shape that means stand-down
    rather than override.
    """
    assert guard._stand_down_notice("some message") is None
    assert "some message" in capsys.readouterr().err
    assert not hasattr(guard, "_warn"), (
        "the envelope-returning form must stay deleted, not merely unused — "
        "a caller reaching for it reintroduces the chain short-circuit"
    )


def test_stand_down_writes_a_durable_audit_line(cloud_container, tmp_path):
    """Property 2. Without this line the downgrade is indistinguishable from
    deleting the guard."""
    git_root = tmp_path / "repo"
    (git_root / ".git").mkdir(parents=True)

    guard._log_environment_stand_down(
        str(git_root), "sess-1", "/somewhere/other-repo", "no fleet on this host"
    )

    logs = list(git_root.rglob("overrides.log"))
    assert logs, "no overrides.log written — the stand-down left no trace"
    body = logs[0].read_text(encoding="utf-8")
    assert "STAND-DOWN-FOREIGN-REPO-WRITE" in body
    assert "sess-1" in body
    assert "other-repo" in body
    assert "no fleet on this host" in body


def test_an_unwritable_audit_log_still_lets_the_write_proceed(cloud_container, capsys):
    """Fail-open, but LOUDLY: an unrecorded stand-down is a visibility defect
    worth a line on stderr, never a reason to block the operator's write."""
    guard._log_environment_stand_down(
        "/definitely/not/a/real/root", "sess-2", "target", "evidence"
    )
    # No exception. Whether it printed depends on where the failure landed;
    # the contract under test is that it returned rather than raising.


def test_the_override_restores_enforcement(cloud_container, monkeypatch):
    """A host that DOES carry a fleet but reads as remote must be correctable
    by the operator, or the heuristic becomes the new impossible ask."""
    monkeypatch.setenv("COORDINATOR_CAP_FLEET_PRESENT", "1")
    assert guard._environment_stands_the_bump_down() is None


def test_a_broken_capability_layer_leaves_the_guard_untouched(cloud_container, monkeypatch):
    """Fail open toward TODAY'S behaviour: if the capability module cannot be
    consulted, the bump enforces exactly as it did before it existed."""
    import builtins

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if name == "coordinator_core.environment":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exploding_import)
    assert guard._environment_stands_the_bump_down() is None

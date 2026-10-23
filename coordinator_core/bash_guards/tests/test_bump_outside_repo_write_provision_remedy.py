"""bump-outside-repo-write names `repo.provision_local` in its deny text for an absent sibling
target, with the verdict unchanged, and names nothing for a target outside the anchor's parent."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as applicability
from coordinator_core.bash_guards import _write_bump_session_start as session_start
from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.ops.sibling_repo_provision_contract import ROUTE_REMEDY
from coordinator_core.testing.home_sandbox import sandbox_home
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    for var in ("CLAUDE_PROJECT_DIR", "CLAUDE_HOME", "HOME", "USERPROFILE",
                "MACHINE_LOCAL_REGISTRY_DIR", "TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(var, raising=False)
    settings = root / "settings-home"
    settings.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    fake_tmp = root / "not-the-system-temp"
    fake_tmp.mkdir()
    monkeypatch.setattr(applicability.tempfile, "gettempdir", lambda: str(fake_tmp))
    monkeypatch.setattr(applicability, "_posix_tmp_literal", lambda: str(fake_tmp))

    anchor = root / "work" / "anchor"
    anchor.mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=anchor, check=True, capture_output=True,
                       **no_console_creationflags())
    (anchor / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=anchor, check=True, capture_output=True,
                   **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=anchor, check=True,
                   capture_output=True, **no_console_creationflags())
    home = root / "home"
    home.mkdir()
    sandbox_home(monkeypatch, home)
    session_start.write_session_start_record("sess-remedy", launch_cwd=str(anchor))
    return {"anchor": anchor, "sibling": anchor.parent / "absent-sibling",
            "elsewhere": root / "elsewhere" / "deep"}


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_mkdir_into_absent_sibling_denies_and_names_route(world):
    cmd = f"mkdir -p {world['sibling']}/x"
    result = _enveloped(guard.check_bump_outside_repo_write)(cmd, "sess-remedy", str(world["anchor"]), {})
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _reason(result).endswith(ROUTE_REMEDY)


def test_redirect_into_absent_sibling_denies_and_names_route(world):
    cmd = f"echo hi > {world['sibling']}/f.txt"
    result = _enveloped(guard.check_bump_outside_repo_write)(cmd, "sess-remedy", str(world["anchor"]), {})
    assert result is not None
    assert _reason(result).endswith(ROUTE_REMEDY)


def test_non_sibling_outside_target_denies_without_route(world):
    cmd = f"mkdir -p {world['elsewhere']}"
    result = _enveloped(guard.check_bump_outside_repo_write)(cmd, "sess-remedy", str(world["anchor"]), {})
    assert result is not None
    assert ROUTE_REMEDY not in _reason(result)


def test_remedy_is_text_only_verdict_matches_unremedied(world, monkeypatch):
    cmd = f"mkdir -p {world['sibling']}/x"
    with_remedy = _enveloped(guard.check_bump_outside_repo_write)(cmd, "sess-remedy", str(world["anchor"]), {})
    monkeypatch.setattr(guard, "_with_route_remedy", lambda message, *_a: message)
    without = _enveloped(guard.check_bump_outside_repo_write)(cmd, "sess-remedy", str(world["anchor"]), {})
    assert with_remedy is not None and without is not None
    assert _reason(with_remedy) == _reason(without) + "\n" + ROUTE_REMEDY
    assert with_remedy["hookSpecificOutput"]["permissionDecision"] == (
        without["hookSpecificOutput"]["permissionDecision"]
    )

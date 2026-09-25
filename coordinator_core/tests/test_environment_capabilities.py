"""The capability primitive doctrine branches on.

Two properties are load-bearing, and each is a defect this file exists
because of:

  1. ENV IS PER CALL. The guard chain is hosted by a long-lived warm server.
     An earlier version cached `os.environ` at module scope, which froze the
     first session's verdict for the daemon's life and made the documented
     override unreachable from every session it went on to serve.
  2. FAIL OPEN IS A PROPERTY OF THIS MODULE, not of its callers. An earlier
     docstring promised it while only one probe carried a handler.
"""

from __future__ import annotations

import pytest

from coordinator_core import environment as env_mod


# ---------------------------------------------------------------------------
# 1. Env per call.
# ---------------------------------------------------------------------------


def test_env_is_read_per_call_not_from_module_state():
    """The whole point: two callers in ONE process, different env, different
    answers. A module-scope cache makes this impossible, silently."""
    remote = {"CLAUDE_CODE_ENTRYPOINT": "remote"}
    workstation = {"CLAUDECODE": "1", "COORDINATOR_SETTINGS_HOME": "/nonexistent-home"}

    assert env_mod.capability("ephemeral_host", remote).value is True
    assert env_mod.capability("ephemeral_host", workstation).value is False
    assert env_mod.capability("ephemeral_host", remote).value is True


def test_override_arrives_through_the_passed_env(monkeypatch):
    """The override must be reachable from a per-call env — under a warm
    daemon the caller's environ is not the daemon's."""
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    forced = env_mod.capability(
        "fleet_present", {"CLAUDE_CODE_ENTRYPOINT": "remote", "COORDINATOR_CAP_FLEET_PRESENT": "1"}
    )
    assert forced.value is True
    assert forced.overridden is True
    assert "inferred False" in forced.evidence


def test_a_typo_in_an_override_is_ignored_not_read_as_false():
    cap = env_mod.capability(
        "ephemeral_host", {"CLAUDE_CODE_ENTRYPOINT": "remote", "COORDINATOR_CAP_EPHEMERAL_HOST": "maybe"}
    )
    assert cap.value is True and cap.overridden is False


def test_engine_installed_accepts_more_than_one_witness(tmp_path):
    """The earlier probe stat-ed `settings.json` alone and read "not
    installed" on a box whose settings home held `machine-local/` and more."""
    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    cap = env_mod.capability("engine_installed", {"COORDINATOR_SETTINGS_HOME": str(home)})
    assert cap.value is True
    assert "machine-local/" in cap.evidence


def test_engine_not_installed_when_no_witness_is_present(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    assert env_mod.capability("engine_installed", {"COORDINATOR_SETTINGS_HOME": str(home)}).value is False


def test_durable_repo_probes_for_a_real_remote(tmp_path, monkeypatch):
    """It returned a hardcoded True with no consumer. It now answers a
    question, and the stand-down audit leg consults it."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert env_mod.capability("durable_repo", {}).value is False

    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@example.com:x/y.git\n', encoding="utf-8"
    )
    assert env_mod.capability("durable_repo", {}).value is True


# ---------------------------------------------------------------------------
# 3. Fail open, and cost.
# ---------------------------------------------------------------------------


def test_a_raising_probe_degrades_permissive_without_a_caller_handler(monkeypatch):
    """The module's own promise, not the consumers'. A third consumer that
    trusts the docstring and omits its own try/except must still be safe."""
    def boom(env, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(env_mod._PROBES, "fleet_present", boom)
    cap = env_mod.capability("fleet_present", {})
    assert cap.value is True
    assert "probe raised RuntimeError" in cap.evidence


def test_unknown_capability_is_permissive_not_an_exception():
    cap = env_mod.capability("not_a_real_capability", {})
    assert cap.value is True and "defaulting permissive" in cap.evidence


def test_asking_for_one_capability_never_runs_another(monkeypatch, tmp_path):
    """Laziness is a cost property: `fleet_present` is on the PreToolUse hot
    path and must never trigger `durable_repo`'s file-reading probe."""
    ran = []
    real = env_mod._PROBES["durable_repo"]

    def watched(env):
        ran.append("durable")
        return real(env)

    monkeypatch.setitem(env_mod._PROBES, "durable_repo", watched)
    env_mod.capability("fleet_present", {"CLAUDE_CODE_ENTRYPOINT": "remote"})
    assert ran == []


def test_probing_spawns_no_subprocess(monkeypatch):
    import subprocess

    def forbidden(*a, **k):
        raise AssertionError("environment probes must not spawn a subprocess")

    for name in ("run", "Popen", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    env_mod.capabilities({"CLAUDE_CODE_ENTRYPOINT": "remote"})


def test_capabilities_report_is_immutable():
    """It is a shared view; a caller mutating it must not corrupt anyone."""
    caps = env_mod.capabilities({})
    with pytest.raises(TypeError):
        caps["fleet_present"] = None


def test_every_capability_carries_evidence():
    for name, cap in env_mod.capabilities({}).items():
        assert cap.evidence.strip() and cap.name == name

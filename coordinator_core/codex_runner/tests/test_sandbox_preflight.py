"""Tests for the no-spawn codex workspace-write preflight."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from coordinator_core.codex_runner import sandbox_preflight as sp


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("preflight must not spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(sp, "_is_windows", lambda: True)


def _home(tmp_path, body):
    home = tmp_path / "codexhome"
    home.mkdir()
    if body is not None:
        (home / "config.toml").write_text(body, encoding="utf-8")
    return home


def _prove(repo, roots):
    rec = repo / sp.PROVEN_ROOTS_RELPATH
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps(roots), encoding="utf-8")


UNELEVATED = '[windows]\nsandbox = "unelevated"\n'


class _Probe:
    def __init__(self, rc=0, hang=False, pid=4242):
        self.rc, self.hang, self.pid, self.killed = rc, hang, pid, False

    def wait(self, timeout=None):
        if self.hang:
            raise subprocess.TimeoutExpired("codex", timeout)
        return self.rc

    def kill(self):
        self.killed = True

    terminate = kill


def _spawner(monkeypatch, probe):
    calls = []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return probe

    monkeypatch.setattr(subprocess, "Popen", fake)
    return calls


def test_unproven_root_without_argv_is_allowed_with_advisory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = sp.check_workspace_write(repo, codex_home=_home(tmp_path, UNELEVATED))
    assert out.startswith("advisory:")


def test_probe_success_records_root_and_is_not_repeated(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path, UNELEVATED)
    calls = _spawner(monkeypatch, _Probe(rc=0))
    assert sp.check_workspace_write(repo, codex_argv=["codex"], codex_home=home) is None
    assert len(calls) == 1
    cmd, kw = calls[0]
    assert cmd[:4] == ["codex", "sandbox", "-c", 'sandbox_mode="workspace-write"']
    assert cmd[-4:] == ["cmd", "/c", "exit", "0"] and kw["cwd"] == str(repo)
    assert sp._normalise(repo) in sp._proven_roots(repo)
    assert sp.check_workspace_write(repo, codex_argv=["codex"], codex_home=home) is None
    assert len(calls) == 1


def test_probe_outliving_cap_is_left_running_not_killed(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path, UNELEVATED)
    probe = _Probe(hang=True)
    calls = _spawner(monkeypatch, probe)
    out = sp.check_workspace_write(repo, codex_argv=["codex"], codex_home=home)
    assert out.startswith("advisory:") and not probe.killed
    assert sp._normalise(repo) not in sp._proven_roots(repo)
    assert sp._warming_pid.__name__ and json.loads((repo / sp.WARMUP_RELPATH).read_text())[sp._normalise(repo)] == 4242
    assert calls[0][1]["creationflags"] == sp._DETACHED


def test_live_warmup_is_not_respawned(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path, UNELEVATED)
    calls = _spawner(monkeypatch, _Probe())
    monkeypatch.setattr(sp, "_warming_pid", lambda r, k: 4242)
    out = sp.check_workspace_write(repo, codex_argv=["codex"], codex_home=home)
    assert "warm-up" in out and calls == []


def test_probe_failure_never_refuses_or_records(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _spawner(monkeypatch, _Probe(rc=1))
    out = sp.check_workspace_write(repo, codex_argv=["codex"], codex_home=_home(tmp_path, UNELEVATED))
    assert out.startswith("advisory:") and not (repo / sp.PROVEN_ROOTS_RELPATH).exists()


def test_passes_when_root_is_proven(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _prove(repo, [str(repo)])
    assert sp.check_workspace_write(repo, codex_home=_home(tmp_path, UNELEVATED)) is None


def test_proven_record_read_from_repo_root_for_other_workspace(tmp_path):
    repo = tmp_path / "repo"
    stage = tmp_path / "stage"
    repo.mkdir()
    stage.mkdir()
    _prove(repo, [str(stage)])
    home = _home(tmp_path, UNELEVATED)
    assert sp.check_workspace_write(stage, repo_root=repo, codex_home=home) is None
    assert sp.check_workspace_write(repo, repo_root=repo, codex_home=home).startswith("advisory:")


@pytest.mark.parametrize("record", ["not json", '{"a": 1}', "[1, null]"])
def test_unreadable_or_malformed_record_reads_as_empty(tmp_path, record):
    repo = tmp_path / "repo"
    (repo / ".coordinator-local").mkdir(parents=True)
    (repo / sp.PROVEN_ROOTS_RELPATH).write_text(record, encoding="utf-8")
    assert sp.check_workspace_write(repo, codex_home=_home(tmp_path, UNELEVATED)).startswith("advisory:")


@pytest.mark.parametrize(
    "body",
    [None, "not = [valid", '[windows]\nsandbox = "elevated"\n', "[tui]\nx = 1\n"],
)
def test_passes_without_unelevated_config(tmp_path, body):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert sp.check_workspace_write(repo, codex_home=_home(tmp_path, body)) is None


def test_passes_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_is_windows", lambda: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert sp.check_workspace_write(repo, codex_home=_home(tmp_path, UNELEVATED)) is None


def _pwsh_dir(tmp_path, *parts):
    d = tmp_path.joinpath(*parts)
    d.mkdir(parents=True)
    (d / "pwsh.exe").write_text("", encoding="utf-8")
    return d


def test_pwsh_first_under_windowsapps_is_refused(tmp_path):
    store = _pwsh_dir(tmp_path, "Microsoft", "WindowsApps")
    good = _pwsh_dir(tmp_path, "pf", "PowerShell", "7")
    env = {"PATH": os.pathsep.join([str(store), str(good)]), "ProgramFiles": str(tmp_path / "pf")}
    with pytest.raises(sp.SandboxSetupRefused, match="WindowsApps") as exc:
        sp.check_pwsh_launchable(env)
    assert str(good) in str(exc.value)


def test_pwsh_outside_windowsapps_passes(tmp_path):
    store = _pwsh_dir(tmp_path, "Microsoft", "WindowsApps")
    good = _pwsh_dir(tmp_path, "pf", "PowerShell", "7")
    sp.check_pwsh_launchable({"Path": os.pathsep.join([str(good), str(store)])})


def test_no_pwsh_on_path_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(sp.SandboxSetupRefused, match="no pwsh.exe"):
        sp.check_pwsh_launchable({"PATH": str(tmp_path / "empty")})


def test_alias_first_pwsh_returns_alias_path_for_alias_first_path(tmp_path):
    store = _pwsh_dir(tmp_path, "Microsoft", "WindowsApps")
    good = _pwsh_dir(tmp_path, "pf", "PowerShell", "7")
    env = {"PATH": os.pathsep.join([str(store), str(good)])}
    assert sp.alias_first_pwsh(env) == os.path.join(str(store), "pwsh.exe")


def test_alias_first_pwsh_none_when_real_first_or_absent(tmp_path):
    store = _pwsh_dir(tmp_path, "Microsoft", "WindowsApps")
    good = _pwsh_dir(tmp_path, "pf", "PowerShell", "7")
    assert sp.alias_first_pwsh({"Path": os.pathsep.join([str(good), str(store)])}) is None
    (tmp_path / "empty").mkdir()
    assert sp.alias_first_pwsh({"PATH": str(tmp_path / "empty")}) is None


@pytest.mark.parametrize("p", ["Users\\x\\AppData\\Local\\Microsoft\\WindowsApps", "/a/windowsapps/b", "x/WINDOWSAPPS"])
def test_is_windowsapps_path_matches_component_on_any_host(p):
    assert sp.is_windowsapps_path(p)


@pytest.mark.parametrize("p", ["Program Files\\PowerShell\\7", "/a/windowsapps2/b", ""])
def test_is_windowsapps_path_rejects_non_component(p):
    assert not sp.is_windowsapps_path(p)


def test_pwsh_check_is_noop_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_is_windows", lambda: False)
    sp.check_pwsh_launchable({"PATH": str(_pwsh_dir(tmp_path, "WindowsApps"))})


def test_codex_home_env_is_honoured(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(_home(tmp_path, UNELEVATED)))
    assert sp.check_workspace_write(repo).startswith("advisory:")

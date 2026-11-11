"""supervise() spawns a codex worker with a shaped env and refuses a WindowsApps-only PATH pre-spawn."""
import os
from types import SimpleNamespace

import pytest

from coordinator_core.codex_runner import sandbox_preflight
from coordinator_core.worker_contracts import worker

PARENT = "00000000-0000-4000-8000-000000000001"


def _pwsh_dir(root, *parts):
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    (d / "pwsh.exe").write_text("")
    return str(d)


def _wire(monkeypatch, base):
    monkeypatch.setattr(worker, "_child_env", lambda requested: base)
    monkeypatch.setattr(worker, "settings_home_child_env", lambda env: env)
    monkeypatch.setattr(sandbox_preflight, "_is_windows", lambda: True)
    monkeypatch.setattr(worker.slot_grant, "check_slot_grant", lambda *a, **k: (True, {}))
    monkeypatch.setattr(worker.capacity, "admit_before_spawn",
                        lambda *a, **k: SimpleNamespace(admitted=True, peak_gib=1))
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a, **k: None)
    monkeypatch.setattr(worker.admission_post, "post", lambda *a, **k: {"delivered": True})
    monkeypatch.setattr(worker.roster, "write_record", lambda *a, **k: None)
    monkeypatch.setattr(worker.roster, "remove_record", lambda *a, **k: None)


class _Process:
    pid = 123
    returncode = 0

    def communicate(self, prompt=None):
        return ("", "")


def _supervise(repo, popen, provider="codex"):
    assignment = {"session_id": PARENT, "run": "assigned", "declared_peak_mb": 1024}
    return worker.supervise(repo, "task", assignment, [provider, "exec", "-"],
                            provider=provider, mode="worker", popen=popen)


def test_codex_worker_spawns_with_ps7_ahead_of_windowsapps_and_writable_temp(tmp_path, monkeypatch):
    apps = _pwsh_dir(tmp_path, "WindowsApps")
    ps7 = _pwsh_dir(tmp_path, "ProgramFiles", "PowerShell", "7")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    _wire(monkeypatch, {"PATH": apps})
    repo = tmp_path / "repo"
    repo.mkdir()
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs["env"])
        return _Process()

    assert _supervise(repo, popen) == 0
    entries = seen["PATH"].split(os.pathsep)
    assert entries.index(ps7) < entries.index(apps)
    want = str(repo / "scratch" / "codex-tmp")
    assert seen["TEMP"] == want and seen["TMP"] == want
    assert (repo / "scratch" / "codex-tmp").is_dir()


def test_codex_worker_refused_before_popen_on_windowsapps_only_path(tmp_path, monkeypatch):
    apps = _pwsh_dir(tmp_path, "WindowsApps")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    _wire(monkeypatch, {"PATH": apps})
    released = []
    monkeypatch.setattr(worker.capacity, "release_spawn", lambda *a, **k: released.append(a))

    def popen(*a, **k):
        raise AssertionError("spawned")

    with pytest.raises(ValueError, match="WindowsApps"):
        _supervise(tmp_path, popen)
    assert released


def test_non_codex_worker_env_is_not_shaped(tmp_path, monkeypatch):
    apps = _pwsh_dir(tmp_path, "WindowsApps")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "ProgramFiles"))
    _wire(monkeypatch, {"PATH": apps})
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs["env"])
        return _Process()

    assert _supervise(tmp_path, popen, provider="agy") == 0
    assert seen["PATH"] == apps and "TEMP" not in seen
    assert not (tmp_path / "scratch" / "codex-tmp").exists()

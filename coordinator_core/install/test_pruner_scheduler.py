from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.install import pruner_runner as pr
from coordinator_core.install import pruner_scheduler as ps


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    for var in ("COORDINATOR_SETTINGS_HOME", "CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["schtasks.exe"], returncode=returncode, stdout=stdout, stderr=stderr)


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            os.symlink(Path(d) / "t", Path(d) / "l")
        except (OSError, NotImplementedError):
            return False
    return True


class TestFileBased:
    def test_darwin_plist_runs_runner_module_without_spawning(self, fake_home, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "darwin"), \
                mock.patch("subprocess.run") as run_mock:
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
        run_mock.assert_not_called()
        data = plistlib.loads((fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist").read_bytes())
        assert data["ProgramArguments"] == ["/x/py", "-m", ps.RUNNER_MODULE]
        assert data["StartInterval"] == ps._INTERVAL_MINUTES * 60
        assert data["WorkingDirectory"] == str(tmp_path)

    def test_darwin_plist_logs_to_the_settings_home_pruner_log(self, fake_home, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "darwin"):
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
        data = plistlib.loads((fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist").read_bytes())
        log = fake_home / ".coordinator-claude-settings" / "logs" / "pruner.log"
        assert data["StandardOutPath"] == data["StandardErrorPath"] == str(log)
        assert log.parent.is_dir()

    def test_darwin_install_rewrites_an_existing_keyless_plist(self, fake_home, tmp_path):
        target = fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist"
        target.parent.mkdir(parents=True)
        target.write_bytes(plistlib.dumps({"Label": ps.PLIST_LABEL, "RunAtLoad": True}))
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "darwin"):
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
        data = plistlib.loads(target.read_bytes())
        assert data["StandardOutPath"] and data["StandardErrorPath"] == data["StandardOutPath"]

    def test_linux_service_appends_stdout_and_stderr_to_the_same_log(self, fake_home, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "linux"):
            ps.register_pruner_task(tmp_path, python_exe="/x/py")  # return value depends on symlink privilege
        text = (fake_home / ".config/systemd/user/coordinator-pruner.service").read_text()
        log = fake_home / ".coordinator-claude-settings" / "logs" / "pruner.log"
        assert f"StandardOutput=append:{log}\n" in text and f"StandardError=append:{log}\n" in text

    def test_log_honours_the_settings_home_override(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
        assert ps._log_path() == tmp_path / "sh" / "logs" / "pruner.log"

    def test_darwin_idempotent(self, fake_home, tmp_path):
        target = fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist"
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "darwin"):
            ps.register_pruner_task(tmp_path, python_exe="/x/py")
            first = target.read_bytes()
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
        assert target.read_bytes() == first

    @pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks (no privilege)")
    def test_linux_units_and_enable_symlink(self, fake_home, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "linux"):
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is True
        udir = fake_home / ".config/systemd/user"
        assert f"OnUnitActiveSec={ps._INTERVAL_MINUTES}min" in (udir / "coordinator-pruner.timer").read_text()
        assert f"ExecStart=/x/py -m {ps.RUNNER_MODULE}" in (udir / "coordinator-pruner.service").read_text()
        link = udir / "timers.target.wants/coordinator-pruner.timer"
        assert link.is_symlink() and link.resolve() == (udir / "coordinator-pruner.timer").resolve()

    def test_unregister_removes_everything_and_tolerates_absence(self, fake_home, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", False):
            with mock.patch.object(ps.sys, "platform", "linux"):
                ps.register_pruner_task(tmp_path, python_exe="/x/py")
            with mock.patch.object(ps.sys, "platform", "darwin"):
                ps.register_pruner_task(tmp_path, python_exe="/x/py")
            assert ps.unregister_pruner_task() is True
            assert ps.unregister_pruner_task() is True
        assert not list((fake_home / ".config/systemd/user").rglob("coordinator-pruner.*"))
        assert not (fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist").exists()

    def test_sampler_job_untouched_by_pruner_unregister(self, fake_home, tmp_path):
        sampler = fake_home / "Library/LaunchAgents/com.coordinator.hostsampler.plist"
        sampler.parent.mkdir(parents=True)
        sampler.write_bytes(b"sampler")
        with mock.patch.object(ps, "_IS_WINDOWS", False):
            assert ps.unregister_pruner_task() is True
        assert sampler.read_bytes() == b"sampler"

    def test_kill_switch_refuses_register_and_unregister(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", "darwin"):
            assert ps.register_pruner_task(tmp_path) is False
            assert ps.unregister_pruner_task() is False
        assert not (fake_home / "Library").exists()

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_redirected_root_leaves_existing_job(self, fake_home, tmp_path, monkeypatch, capsys, platform):
        existing = (
            fake_home / "Library/LaunchAgents/com.coordinator.pruner.plist"
            if platform == "darwin"
            else fake_home / ".config/systemd/user/coordinator-pruner.timer"
        )
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"real-box-job")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "sandbox" / ".claude"))
        with mock.patch.object(ps, "_IS_WINDOWS", False), mock.patch.object(ps.sys, "platform", platform):
            assert ps.register_pruner_task(tmp_path, python_exe="/x/py") is False
        assert existing.read_bytes() == b"real-box-job"
        assert "[ADVISORY] pruner job not registered: CLAUDE_CONFIG_DIR=" in capsys.readouterr().out


class TestWindows:
    def test_create_with_xml_force(self, tmp_path):
        fake_py = str(tmp_path / "py.exe")
        seen = {}
        real = Path.write_text

        def capture(self, data, *a, **k):
            if self.suffix == ".xml":
                seen["xml"] = data
            return real(self, data, *a, **k)

        with mock.patch.object(ps, "_IS_WINDOWS", True), \
                mock.patch("subprocess.run", return_value=_completed(0)) as run_mock, \
                mock.patch.object(Path, "write_text", capture):
            assert ps.register_pruner_task(tmp_path, python_exe=fake_py) is True
        (argv,), _ = run_mock.call_args
        assert argv[0] == "schtasks.exe" and "/Create" in argv and "/F" in argv and "/XML" in argv
        assert ps.TASK_NAME in argv
        xml = seen["xml"]
        assert fake_py in xml and f"<WorkingDirectory>{tmp_path}</WorkingDirectory>" in xml
        assert f"-m {ps.RUNNER_MODULE}" in xml and f"PT{ps._INTERVAL_MINUTES}M" in xml
        assert "cmd /c" not in xml

    def test_failures_degrade_to_false(self, tmp_path):
        with mock.patch.object(ps, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", side_effect=OSError("nf")):
                assert ps.register_pruner_task(tmp_path) is False
            with mock.patch("subprocess.run", return_value=_completed(1, stderr="denied")):
                assert ps.register_pruner_task(tmp_path) is False

    def test_unregister_paths(self):
        with mock.patch.object(ps, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", return_value=_completed(0)) as run_mock:
                assert ps.unregister_pruner_task() is True
            (argv,), _ = run_mock.call_args
            assert "/Delete" in argv and ps.TASK_NAME in argv
            with mock.patch("subprocess.run", return_value=_completed(1, stderr="ERROR: cannot find the file")):
                assert ps.unregister_pruner_task() is True
            with mock.patch("subprocess.run", return_value=_completed(1, stderr="access denied")):
                assert ps.unregister_pruner_task() is False
            with mock.patch("subprocess.run", side_effect=OSError("nf")):
                assert ps.unregister_pruner_task() is False

    def test_kill_switch_spawns_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(ps, "_IS_WINDOWS", True), mock.patch("subprocess.run") as run_mock:
            assert ps.register_pruner_task(tmp_path) is False
            assert ps.unregister_pruner_task() is False
        run_mock.assert_not_called()

    def test_redirected_root_does_not_call_schtasks(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sandbox" / "settings"))
        with mock.patch.object(ps, "_IS_WINDOWS", True), mock.patch.object(ps, "_run_schtasks") as schtasks:
            assert ps.register_pruner_task(tmp_path, python_exe="py.exe") is False
        schtasks.assert_not_called()
        assert "[ADVISORY] pruner job not registered" in capsys.readouterr().out


class TestRunner:
    def test_each_registered_repo_gets_hygiene_apply_and_emitted_prune(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        hygiene = mock.Mock(return_value={"walk": {"deleted": 1}})
        prune = mock.Mock(return_value={"pruned": ["x", "y"]})
        with mock.patch.object(pr, "run_hygiene", hygiene), \
                mock.patch("coordinator_core.ops.fleet.prune_emitted.prune_emitted_output", prune), \
                mock.patch("coordinator_core.lifecycle.git_common_dir", lambda r: r / ".git"), \
                mock.patch.object(pr, "registered_repo_roots", return_value=[a, b]):
            results = pr.run_once()
        assert [r["repo"] for r in results] == [str(a), str(b)]
        assert all(r["emitted_pruned"] == 2 for r in results)
        params = hygiene.call_args_list[0].args[0]
        assert params["apply"] is True and params["repo_root"] == str(a)
        from coordinator_core.ops.fleet.scratch_hygiene import CADENCES

        assert params["cadence"] in CADENCES
        assert prune.call_args_list[0].kwargs == {"dry_run": False}

    def test_one_repo_failing_does_not_stop_the_tick(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()

        def hygiene(params):
            if params["repo_root"] == str(a):
                raise RuntimeError("boom")
            return {"walk": {}}

        with mock.patch.object(pr, "run_hygiene", hygiene), \
                mock.patch("coordinator_core.ops.fleet.prune_emitted.prune_emitted_output",
                           return_value={"pruned": []}), \
                mock.patch("coordinator_core.lifecycle.git_common_dir", lambda r: r / ".git"):
            results = pr.run_once([a, b])
        assert "RuntimeError" in results[0]["hygiene"]
        assert results[0]["emitted_pruned"] == 0
        assert results[1]["hygiene"] == {}

    def test_missing_repo_dir_skipped_and_registry_failure_is_empty(self, tmp_path):
        assert pr.run_once([tmp_path / "gone"]) == []
        with mock.patch.object(pr, "registered_repo_roots", side_effect=RuntimeError("x")):
            assert pr.run_once() == []

    def test_real_hygiene_on_empty_scratch_repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "scratch").mkdir()
        (tmp_path / "scratch" / "old").mkdir()
        res = pr.prune_repo(tmp_path)
        assert isinstance(res["hygiene"], dict)
        assert res["emitted_pruned"] == 0

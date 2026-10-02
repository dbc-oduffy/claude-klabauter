
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.install import host_sampler_scheduler as hss


@pytest.fixture(autouse=True)
def _kill_switch_off(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["schtasks.exe"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestRegisterHostSamplerTask:
    def test_success_invokes_create_with_force_and_returns_true(self, tmp_path):
        fake_py = str(tmp_path / "py.exe")
        written_xml = {}

        real_write_text = Path.write_text

        def _capture_write_text(self, data, *args, **kwargs):
            if self.suffix == ".xml":
                written_xml["path"] = self
                written_xml["content"] = data
            return real_write_text(self, data, *args, **kwargs)

        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", return_value=_completed(0)) as run_mock:
                with mock.patch.object(Path, "write_text", _capture_write_text):
                    result = hss.register_host_sampler_task(tmp_path, python_exe=fake_py)
        assert result is True
        (argv,), kwargs = run_mock.call_args
        assert argv[0] == "schtasks.exe"
        assert "/Create" in argv
        assert "/F" in argv
        assert hss.TASK_NAME in argv
        assert "/XML" in argv
        xml_content = written_xml["content"]
        assert fake_py in xml_content
        assert str(tmp_path) in xml_content
        assert "<WorkingDirectory>" in xml_content

    def test_task_xml_sets_native_working_directory(self, tmp_path):
        fake_py = str(tmp_path / "py.exe")
        xml = hss._task_xml(fake_py, tmp_path)
        assert f"<WorkingDirectory>{tmp_path}</WorkingDirectory>" in xml
        assert "cmd /c" not in xml
        assert fake_py in xml

    def test_schtasks_missing_degrades_to_advisory_false(self, tmp_path):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", side_effect=OSError("not found")):
                result = hss.register_host_sampler_task(tmp_path)
        assert result is False

    def test_nonzero_exit_degrades_to_advisory_false(self, tmp_path):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", return_value=_completed(1, stderr="denied")):
                result = hss.register_host_sampler_task(tmp_path)
        assert result is False


class TestUnregisterHostSamplerTask:
    def test_non_windows_is_success_noop(self):
        with mock.patch.object(hss, "_IS_WINDOWS", False):
            with mock.patch("subprocess.run") as run_mock:
                result = hss.unregister_host_sampler_task()
        assert result is True
        run_mock.assert_not_called()

    def test_absent_task_is_success(self):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch(
                "subprocess.run",
                return_value=_completed(
                    1, stderr="ERROR: The system cannot find the file specified."
                ),
            ):
                result = hss.unregister_host_sampler_task()
        assert result is True

    def test_present_task_removed_successfully(self):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", return_value=_completed(0)) as run_mock:
                result = hss.unregister_host_sampler_task()
        assert result is True
        (argv,), kwargs = run_mock.call_args
        assert "/Delete" in argv
        assert hss.TASK_NAME in argv

    def test_genuine_failure_returns_false(self):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch(
                "subprocess.run", return_value=_completed(1, stderr="access denied")
            ):
                result = hss.unregister_host_sampler_task()
        assert result is False

    def test_schtasks_missing_degrades_to_false(self):
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run", side_effect=OSError("not found")):
                result = hss.unregister_host_sampler_task()
        assert result is False


class TestKillSwitch:
    def test_register_refuses_without_spawning_schtasks(self, monkeypatch, tmp_path):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run") as run_mock:
                assert hss.register_host_sampler_task(tmp_path) is False
        run_mock.assert_not_called()

    def test_unregister_refuses_without_spawning_schtasks(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(hss, "_IS_WINDOWS", True):
            with mock.patch("subprocess.run") as run_mock:
                assert hss.unregister_host_sampler_task() is False
        run_mock.assert_not_called()


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class TestFileBasedRegistration:
    def test_darwin_writes_launch_agent_without_spawning(self, fake_home, tmp_path):
        import plistlib

        with mock.patch.object(hss, "_IS_WINDOWS", False), \
                mock.patch.object(hss.sys, "platform", "darwin"), \
                mock.patch("subprocess.run") as run_mock:
            assert hss.register_host_sampler_task(tmp_path, python_exe="/x/py") is True
        run_mock.assert_not_called()
        data = plistlib.loads((fake_home / "Library/LaunchAgents/com.coordinator.hostsampler.plist").read_bytes())
        assert data["StartInterval"] == hss._INTERVAL_MINUTES * 60
        assert data["ProgramArguments"][0] == "/x/py"
        assert data["ProgramArguments"][1].endswith("host_sampler.py")
        assert data["WorkingDirectory"] == str(tmp_path)

    def test_darwin_is_idempotent(self, fake_home, tmp_path):
        with mock.patch.object(hss, "_IS_WINDOWS", False), mock.patch.object(hss.sys, "platform", "darwin"):
            hss.register_host_sampler_task(tmp_path, python_exe="/x/py")
            first = (fake_home / "Library/LaunchAgents/com.coordinator.hostsampler.plist").read_bytes()
            assert hss.register_host_sampler_task(tmp_path, python_exe="/x/py") is True
        assert (fake_home / "Library/LaunchAgents/com.coordinator.hostsampler.plist").read_bytes() == first

    def test_linux_writes_timer_units_and_enable_symlink(self, fake_home, tmp_path):
        with mock.patch.object(hss, "_IS_WINDOWS", False), mock.patch.object(hss.sys, "platform", "linux"):
            assert hss.register_host_sampler_task(tmp_path, python_exe="/x/py") is True
            assert hss.register_host_sampler_task(tmp_path, python_exe="/x/py") is True
        udir = fake_home / ".config/systemd/user"
        assert f"OnUnitActiveSec={hss._INTERVAL_MINUTES}min" in (udir / "coordinator-host-sampler.timer").read_text()
        assert "ExecStart=/x/py " in (udir / "coordinator-host-sampler.service").read_text()
        link = udir / "timers.target.wants/coordinator-host-sampler.timer"
        assert link.is_symlink() and link.resolve() == (udir / "coordinator-host-sampler.timer").resolve()

    def test_kill_switch_writes_nothing(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(hss, "_IS_WINDOWS", False), mock.patch.object(hss.sys, "platform", "darwin"):
            assert hss.register_host_sampler_task(tmp_path) is False
        assert not (fake_home / "Library").exists()

    def test_unregister_removes_everything_and_tolerates_absence(self, fake_home, tmp_path):
        with mock.patch.object(hss, "_IS_WINDOWS", False):
            with mock.patch.object(hss.sys, "platform", "linux"):
                hss.register_host_sampler_task(tmp_path, python_exe="/x/py")
            with mock.patch.object(hss.sys, "platform", "darwin"):
                hss.register_host_sampler_task(tmp_path, python_exe="/x/py")
            assert hss.unregister_host_sampler_task() is True
            assert hss.unregister_host_sampler_task() is True
        assert not list((fake_home / ".config/systemd/user").rglob("coordinator-host-sampler.*"))
        assert not (fake_home / "Library/LaunchAgents/com.coordinator.hostsampler.plist").exists()

    def test_unregister_refused_by_kill_switch(self, fake_home, monkeypatch):
        monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
        with mock.patch.object(hss, "_IS_WINDOWS", False):
            assert hss.unregister_host_sampler_task() is False

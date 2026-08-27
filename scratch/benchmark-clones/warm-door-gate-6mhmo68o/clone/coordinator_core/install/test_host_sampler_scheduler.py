"""Unit tests for coordinator_core.install.host_sampler_scheduler.

Every test stubs subprocess.run so nothing here touches a real Task
Scheduler entry -- live registration is verified separately, out of band
(see the run-report sidecar for this dispatch)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.install import host_sampler_scheduler as hss


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["schtasks.exe"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestRegisterHostSamplerTask:
    def test_non_windows_is_a_clean_advisory_noop(self):
        with mock.patch.object(hss, "_IS_WINDOWS", False):
            with mock.patch("subprocess.run") as run_mock:
                result = hss.register_host_sampler_task(Path("/tmp/repo"))
        assert result is False
        run_mock.assert_not_called()

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
        assert "/F" in argv  # idempotent overwrite, not error-on-duplicate
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

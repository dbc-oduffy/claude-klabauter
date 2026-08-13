"""
Regression coverage for chunk C4 of
docs/plans/2026-08-07-no-window-subprocess-primitive.md: the two bare
hook-path git-probe spawn sites (`example_retrieval_repo_detect._git` and
`context_pressure_precompact._run_git`) now splat
`coordinator_core.win_portability.no_console_creationflags()` into their
`subprocess.run` call, suppressing the Windows `conhost.exe` popup those
read-only git probes previously triggered on every invocation.

Spec backlink: pln-no-window-subprocess-primitive-750d2d, row C4.
"""

from __future__ import annotations

from unittest import mock

from coordinator_core.hooks import context_pressure_precompact, example_retrieval_repo_detect


def _fake_run(*, returncode=0, stdout="ok\n"):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    return proc


def test_example_retrieval_repo_detect_git_passes_suppression_kwargs_on_windows():
    with mock.patch(
        "coordinator_core.win_portability.no_console_creationflags",
        return_value={"creationflags": 0x08000000},
    ) as fake_flags, mock.patch("subprocess.run", return_value=_fake_run()) as fake_run:
        result = example_retrieval_repo_detect._git("fake-repo-root", "log", "-1")

    assert result == "ok"
    fake_flags.assert_called_once_with()
    _, kwargs = fake_run.call_args
    assert kwargs.get("creationflags") == 0x08000000
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 10


def test_example_retrieval_repo_detect_git_unchanged_on_posix():
    with mock.patch(
        "coordinator_core.win_portability.no_console_creationflags",
        return_value={},
    ) as fake_flags, mock.patch("subprocess.run", return_value=_fake_run()) as fake_run:
        result = example_retrieval_repo_detect._git("fake-repo-root", "log", "-1")

    assert result == "ok"
    fake_flags.assert_called_once_with()
    _, kwargs = fake_run.call_args
    assert "creationflags" not in kwargs
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 10


def test_context_pressure_precompact_run_git_passes_suppression_kwargs_on_windows():
    with mock.patch(
        "coordinator_core.win_portability.no_console_creationflags",
        return_value={"creationflags": 0x08000000},
    ) as fake_flags, mock.patch("subprocess.run", return_value=_fake_run()) as fake_run:
        result = context_pressure_precompact._run_git(["log", "-1"], cwd="fake-repo-root")

    assert result == "ok"
    fake_flags.assert_called_once_with()
    _, kwargs = fake_run.call_args
    assert kwargs.get("creationflags") == 0x08000000
    assert kwargs.get("cwd") == "fake-repo-root"
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 10


def test_context_pressure_precompact_run_git_unchanged_on_posix():
    with mock.patch(
        "coordinator_core.win_portability.no_console_creationflags",
        return_value={},
    ) as fake_flags, mock.patch("subprocess.run", return_value=_fake_run()) as fake_run:
        result = context_pressure_precompact._run_git(["log", "-1"], cwd="fake-repo-root")

    assert result == "ok"
    fake_flags.assert_called_once_with()
    _, kwargs = fake_run.call_args
    assert "creationflags" not in kwargs
    assert kwargs.get("cwd") == "fake-repo-root"
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 10

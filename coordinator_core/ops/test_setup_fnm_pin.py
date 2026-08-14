"""Characterization tests for coordinator_core.ops.setup_fnm_pin.

Ported oracle: coordinator/tests/test_fnm_pin.bats (DoE-claude) — every case
in that plain-bash harness has a corresponding case here, run against the
Python port instead of the bash original, plus the same env-var injection
seams (COORDINATOR_FNM_CMD / COORDINATOR_FNM_ABSENT) used by the bash test.

Port of: setup-fnm-pin.sh (DoE 6fb5fb37, 2026-07-22).
Spec backlink: docs/plans/2026-06-23-setup-time-substrate-completeness.md § C1d (AC6)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.setup_fnm_pin import main
from coordinator_core.testing.fake_machine_local import write_fake_executable


def _make_mock_fnm(tmp_path: Path) -> tuple[Path, Path]:
    """Write a cross-platform executable mock `fnm` that logs its args and exits 0."""
    calls_log = tmp_path / "fnm-calls.log"
    calls_log_repr = repr(str(calls_log))
    python_body = (
        "import sys\n"
        "from pathlib import Path\n"
        f"log = Path({calls_log_repr})\n"
        "log.parent.mkdir(parents=True, exist_ok=True)\n"
        "with open(log, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    )
    mock = write_fake_executable(tmp_path, "mock-fnm", python_body)
    return mock, calls_log


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


def test_node_version_present_fnm_present_installs_and_emits_guidance(repo, tmp_path, capsys, monkeypatch):
    (repo / ".node-version").write_text("v24.2.0\n", encoding="utf-8")
    mock, calls_log = _make_mock_fnm(tmp_path)
    monkeypatch.setenv("COORDINATOR_FNM_CMD", str(mock))

    rc = main([str(repo)])

    assert rc == 0
    assert calls_log.read_text(encoding="utf-8").strip() == "install v24.2.0"
    out = capsys.readouterr().out
    assert 'eval "$(fnm env)"' in out
    assert "PATH=" in out


def test_nvmrc_present_fnm_present_reads_nvmrc(repo, tmp_path, monkeypatch):
    (repo / ".nvmrc").write_text("24.2.0\n", encoding="utf-8")
    mock, calls_log = _make_mock_fnm(tmp_path)
    monkeypatch.setenv("COORDINATOR_FNM_CMD", str(mock))

    rc = main([str(repo)])

    assert rc == 0
    assert calls_log.read_text(encoding="utf-8").strip() == "install 24.2.0"


def test_node_version_takes_precedence_over_nvmrc(repo, tmp_path, monkeypatch):
    (repo / ".node-version").write_text("v24.2.0\n", encoding="utf-8")
    (repo / ".nvmrc").write_text("22.0.0\n", encoding="utf-8")
    mock, calls_log = _make_mock_fnm(tmp_path)
    monkeypatch.setenv("COORDINATOR_FNM_CMD", str(mock))

    rc = main([str(repo)])

    assert rc == 0
    logged = calls_log.read_text(encoding="utf-8")
    assert "install v24.2.0" in logged
    assert "install 22.0.0" not in logged


def test_nvmrc_present_fnm_absent_exits_1_with_remediation(repo, capsys, monkeypatch):
    (repo / ".nvmrc").write_text("24.2.0\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_FNM_ABSENT", "1")

    rc = main([str(repo)])

    assert rc == 1
    err = capsys.readouterr().err
    assert (
        "fnm not installed — run coordinator:install to install the Node "
        "toolchain manager, then re-run repo-setup" in err
    )


def test_node_version_present_fnm_absent_exits_1_with_remediation(repo, capsys, monkeypatch):
    (repo / ".node-version").write_text("v24.2.0\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_FNM_ABSENT", "1")

    rc = main([str(repo)])

    assert rc == 1
    assert "fnm not installed" in capsys.readouterr().err


def test_no_pin_file_exits_0_no_output(repo, capsys):
    rc = main([str(repo)])

    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_no_pin_file_does_not_invoke_fnm(repo, tmp_path, monkeypatch):
    mock, calls_log = _make_mock_fnm(tmp_path)
    monkeypatch.setenv("COORDINATOR_FNM_CMD", str(mock))

    rc = main([str(repo)])

    assert rc == 0
    assert not calls_log.exists() or calls_log.read_text(encoding="utf-8") == ""


def test_idempotent_pin_file_fnm_present_both_exit_0(repo, tmp_path, monkeypatch):
    (repo / ".node-version").write_text("v24.2.0\n", encoding="utf-8")
    mock, calls_log = _make_mock_fnm(tmp_path)
    monkeypatch.setenv("COORDINATOR_FNM_CMD", str(mock))

    assert main([str(repo)]) == 0
    calls_log.unlink()
    assert main([str(repo)]) == 0
    assert "install v24.2.0" in calls_log.read_text(encoding="utf-8")


def test_idempotent_no_pin_file_both_exit_0(repo, capsys):
    assert main([str(repo)]) == 0
    assert main([str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_missing_repo_root_defaults_to_cwd_no_pin_file(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert main([]) == 0


def test_nonexistent_repo_root_exits_1(tmp_path, capsys):
    nonexistent = tmp_path / "does-not-exist"

    rc = main([str(nonexistent)])

    assert rc == 1
    assert "repo root does not exist" in capsys.readouterr().err


def test_pin_file_empty_exits_1(repo, capsys):
    (repo / ".node-version").write_text("", encoding="utf-8")

    rc = main([str(repo)])

    assert rc == 1
    assert "pin file is empty" in capsys.readouterr().err

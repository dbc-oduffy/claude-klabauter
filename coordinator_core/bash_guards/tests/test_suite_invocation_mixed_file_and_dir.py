"""A directory operand makes a pytest run suite-shaped whatever file or node-id operands sit
beside it; a run of only files and node ids stays Tier T. ``-n`` does not change the shape.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py (_is_unscoped_directory)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.guard_messages.testing import envelope_of

_DIR = "coordinator/tests/fixtures/sweeps"
_FILE = "coordinator_core/ops/test_a.py"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["coordinator_core"]\n', encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / _DIR).mkdir(parents=True)
    (tmp_path / "coordinator_core/ops").mkdir(parents=True)
    (tmp_path / _FILE).write_text("", encoding="utf-8")
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _denied(repo, command) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "session_id": "s", "cwd": str(repo)}
    return envelope_of(guard.check(payload)) is not None


@pytest.mark.parametrize("operands", [
    _DIR,
    f"{_FILE} {_DIR}",
    f"{_DIR} {_FILE}",
    f"{_FILE}::test_x {_DIR}",
    f"{_FILE} {_FILE}::test_x {_DIR}",
])
@pytest.mark.parametrize("flags", ["", "-n 2 -q"])
def test_directory_operand_is_suite_shaped(repo, operands, flags) -> None:
    cmd = f"python -m pytest {operands} {flags}".strip()
    assert _denied(repo, cmd)
    assert guard.classify_command(cmd, cwd=str(repo))


def _verdict(repo, command) -> tuple:
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "session_id": "s", "cwd": str(repo)}
    out = envelope_of(guard.check(payload))
    if out is None:
        return None, ""
    hook = out["hookSpecificOutput"]
    return hook["permissionDecision"], hook.get("permissionDecisionReason", "")


_SUITE = 'python -m pytest -m "not cadence" -n 7'


@pytest.mark.parametrize("cmd", [
    f"python -m pytest {_FILE} {_DIR}",
    f"python -m pytest {_FILE} {_DIR} -n 4 -q",
    _SUITE,
    f"with-suite-mutex --wait && {_SUITE}",
    f"with-suite-mutex --wait ; {_SUITE}",
    f"with-suite-mutex --wait\n{_SUITE}",
    f"with-suite-mutex --wait &&\n{_SUITE}",
    f"echo start\n{_SUITE}",
    f"cd .\n\n{_SUITE}",
])
def test_unwrapped_suite_run_is_refused_with_the_wrapped_rewrite(repo, cmd) -> None:
    decision, reason = _verdict(repo, cmd)
    assert decision == "deny"
    assert "with-suite-mutex -- " in reason


@pytest.mark.parametrize("cmd", [
    f"with-suite-mutex -- python -m pytest {_FILE} {_DIR}",
    f"with-suite-mutex -- {_SUITE}",
    f"with-suite-mutex --wait\nwith-suite-mutex -- {_SUITE}",
])
def test_wrapped_suite_run_is_not_denied(repo, cmd) -> None:
    assert _verdict(repo, cmd)[0] != "deny"


def test_a_heredoc_body_naming_a_suite_run_is_not_a_run(repo) -> None:
    cmd = f"cat <<'EOF'\n{_SUITE}\nEOF\ntrue"
    assert _verdict(repo, cmd)[0] != "deny"
    assert guard.classify_command(cmd, cwd=str(repo)) == []


def test_a_here_string_does_not_glue_a_following_suite_run(repo) -> None:
    cmd = f"cat <<<word\n{_SUITE}"
    assert _verdict(repo, cmd)[0] == "deny"


def test_a_suite_run_after_a_heredoc_is_still_refused(repo) -> None:
    cmd = f"cat <<'EOF'\nnotes\nEOF\n{_SUITE}"
    assert _verdict(repo, cmd)[0] == "deny"


@pytest.mark.parametrize("cmd", [
    f"python -m pytest {_FILE} -n 2 -q",
    f"python -m pytest {_FILE}::test_x {_FILE} -p no:cacheprovider",
    f"pytest {_FILE}::test_x",
])
def test_files_and_node_ids_stay_unguarded(repo, cmd) -> None:
    assert not _denied(repo, cmd)
    assert guard.classify_command(cmd, cwd=str(repo)) == []

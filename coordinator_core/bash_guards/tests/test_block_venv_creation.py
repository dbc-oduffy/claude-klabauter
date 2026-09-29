from __future__ import annotations

from coordinator_core.bash_guards import block_venv_creation as guard


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }


def _advisory(out):
    assert out is not None, "expected an advisory envelope, got silent allow"
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    return hso["additionalContext"]


class TestNonMatchingAllowsSilently:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("")) is None

    def test_malformed_tool_input_allows(self):
        payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
        assert guard.check(payload) is None

    def test_grep_venv_allows(self):
        assert guard.check(_payload("grep venv requirements.txt")) is None

    def test_ls_dot_venv_allows(self):
        assert guard.check(_payload("ls .venv")) is None

    def test_cat_mentioning_venv_allows(self):
        assert guard.check(_payload('cat notes.txt | grep "python -m venv"')) is None

    def test_python_run_script_allows(self):
        assert guard.check(_payload("python3 script.py --venv-path .venv")) is None


class TestVenvCreationShapesWarn:
    def test_python3_m_venv_warns(self):
        reason = _advisory(guard.check(_payload("python3 -m venv .venv")))
        assert "PER-REPO-VENVS-ARE-BANNED-FLEET-WIDE" in reason

    def test_python_m_venv_warns(self):
        _advisory(guard.check(_payload("python -m venv .venv")))

    def test_py_m_venv_warns(self):
        _advisory(guard.check(_payload("py -m venv .venv")))

    def test_virtualenv_warns(self):
        _advisory(guard.check(_payload("virtualenv .venv")))

    def test_uv_venv_warns(self):
        _advisory(guard.check(_payload("uv venv")))

    def test_uv_sync_warns(self):
        _advisory(guard.check(_payload("uv sync")))

    def test_pipenv_install_warns(self):
        _advisory(guard.check(_payload("pipenv install")))

    def test_pipenv_sync_warns(self):
        _advisory(guard.check(_payload("pipenv sync")))

    def test_pipenv_shell_warns(self):
        _advisory(guard.check(_payload("pipenv shell")))

    def test_poetry_install_warns(self):
        _advisory(guard.check(_payload("poetry install")))

    def test_conda_create_warns(self):
        _advisory(guard.check(_payload("conda create -n foo python=3.11")))

    def test_chained_command_warns(self):
        _advisory(guard.check(_payload("cd /repo && python3 -m venv .venv")))

    def test_never_denies(self):
        # Amended posture: this guard is advisory-only -- it must never
        # return a "deny" permissionDecision for any recognized shape.
        for cmd in (
            "python3 -m venv .venv",
            "virtualenv .venv",
            "uv venv",
            "pipenv install",
            "poetry install",
            "conda create -n foo",
        ):
            out = guard.check(_payload(cmd))
            assert out is not None
            assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

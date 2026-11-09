"""Reachability of engine_ask.submit, workflow.fire_status and a ceremony op through the
real `coordinator-invoke.py` front door for a caller with no Claude envelope.

Each case spawns the CLI as a subprocess with every CLAUDE_* variable stripped; a
-32601 (method not found) is the defect this file pins out. The in-tree engine is
unstamped, so the cases pass `--allow-unstamped-dispatch`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[1]
ENGINE_ROOT = BIN_DIR.parents[1]
_SCRIPT = BIN_DIR / "coordinator-invoke.py"
METHOD_NOT_FOUND = -32601
_IDENTITY_VARS = (
    "COORDINATOR_SESSION_ID",
    "CODEX_THREAD_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "COORDINATOR_AGENT_SESSION",
)


def _envelope_free_env(**extra: str) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k not in _IDENTITY_VARS
    }
    env["COORDINATOR_ENGINE_ROOT"] = str(ENGINE_ROOT)
    env.update(extra)
    return env


def _invoke(op: str, params: dict, repo: Path, tmp_path: Path, **env_extra: str) -> dict:
    params_file = tmp_path / "params.json"
    params_file.write_text(json.dumps(params), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            op,
            "--params-file",
            str(params_file),
            "--repo",
            str(repo),
            "--allow-unstamped-dispatch",
        ],
        env=_envelope_free_env(**env_extra),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    start = proc.stdout.find("{")
    assert start >= 0, f"no JSON on stdout (rc={proc.returncode}): {proc.stderr[-800:]}"
    return json.loads(proc.stdout[start:])


def _assert_registered(response: dict) -> None:
    code = (response.get("error") or {}).get("code")
    assert code != METHOD_NOT_FOUND, response


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    # A real repo root is the behaviour under test: the ops resolve it from --repo.
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path


def _submit_params(repo: Path, session_id: str) -> dict:
    return {
        "ask": "route probe",
        "ask_source": "relayed",
        "session_id": session_id,
        "caller": "codex",
        "cwd": str(repo),
        "fire": False,
    }


def test_engine_ask_submit_returns_a_run_for_a_codex_identity(repo, tmp_path):
    sid = str(uuid.uuid4())
    response = _invoke(
        "engine_ask.submit",
        _submit_params(repo, sid),
        repo,
        tmp_path,
        CODEX_THREAD_ID=sid,
    )
    _assert_registered(response)
    result = response["result"]
    assert "refused" not in result, result
    assert result["run_id"] and result["script_path"] and len(result["sha256"]) == 64
    assert "fire" not in result


def test_engine_ask_submit_without_any_identity_is_refused_not_unregistered(repo, tmp_path):
    response = _invoke(
        "engine_ask.submit", _submit_params(repo, str(uuid.uuid4())), repo, tmp_path
    )
    _assert_registered(response)
    result = response["result"]
    assert result["refused"] == "gate-refused"
    assert "run_id" not in result


def test_engine_ask_submit_refuses_a_session_id_that_is_not_the_caller(repo, tmp_path):
    response = _invoke(
        "engine_ask.submit",
        _submit_params(repo, str(uuid.uuid4())),
        repo,
        tmp_path,
        CODEX_THREAD_ID=str(uuid.uuid4()),
    )
    _assert_registered(response)
    assert response["result"]["refused"] == "gate-refused"


def test_workflow_fire_status_resolves_without_an_envelope(repo, tmp_path):
    response = _invoke("workflow.fire_status", {"fire_id": "none"}, repo, tmp_path)
    _assert_registered(response)
    assert response["result"]["found"] is False


def test_a_read_only_ceremony_op_is_registered_without_an_envelope(repo, tmp_path):
    response = _invoke(
        "ceremony.result", {"run_id": "ask-20261109T000000-abcdef"}, repo, tmp_path
    )
    _assert_registered(response)

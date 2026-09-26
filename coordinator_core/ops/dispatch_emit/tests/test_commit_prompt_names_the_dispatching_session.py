
from coordinator_core.ops.dispatch_emit.emit import (
    _commit_agent_call,
    _preflight_agent_call,
    _wave_agent_calls,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_UUID = "7f8efcbc-1234-4abc-89ab-0123456789ab"


def test_commit_prompt_names_the_dispatching_session_when_supplied():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], session_id=_UUID
    )
    assert f"Dispatching Session-Id: {_UUID}" in call


def test_commit_prompt_omits_the_line_when_session_id_is_absent():
    call = _commit_agent_call(["a.py"], "Commit wave 1", 0, ["C1"])
    assert "Dispatching Session-Id" not in call


def test_commit_prompt_omits_the_line_when_session_id_is_empty():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], session_id=""
    )
    assert "Dispatching Session-Id" not in call


def test_commit_prompt_omits_the_line_when_session_id_is_not_uuid_shaped():
    call = _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], session_id="not-a-uuid"
    )
    assert "Dispatching Session-Id" not in call


def _wave_row(chunk_id: str, writes: list[str]) -> WaveRow:
    return WaveRow(
        id=chunk_id,
        title=f"title-{chunk_id}",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=None,
        agent_model=None,
    )


def test_executor_wave_prompt_never_carries_the_session_line():
    wave = [_wave_row("C1", ["a.py"])]
    call = _wave_agent_calls(wave, "Wave 1: C1", "docs/plans/example.md", None, None)
    assert "Dispatching Session-Id" not in call


def test_preflight_prompt_never_carries_the_session_line():
    call = _preflight_agent_call(["a.py"], "Preflight: commit claimability")
    assert "Dispatching Session-Id" not in call

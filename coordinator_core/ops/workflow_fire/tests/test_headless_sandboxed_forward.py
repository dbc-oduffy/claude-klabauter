"""headless.start forwards workflow.fire to the warm engine from a restricted token and never Popens."""

from __future__ import annotations

import pytest

from coordinator_core.ops.workflow_fire import headless
from coordinator_core.warm import sandboxed_spawn
from pathlib import Path


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    script = repo / "run.mjs"
    script.write_text("export const meta={};\nreturn 1;\n", encoding="utf-8")

    def no_popen(*_a, **_kw):
        raise AssertionError("worker must not be Popen'd from here")

    monkeypatch.setattr(headless.subprocess, "Popen", no_popen)
    return script, repo


def test_sandboxed_forwards_and_never_spawns(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    script, repo = rig
    seen = {}
    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: True)

    def forward(op, params, repo_arg) -> dict[str, str]:
        seen.update(op=op, params=params, repo=repo_arg)
        return {"fire_id": "warm-record", "route": "warm"}

    monkeypatch.setattr(sandboxed_spawn, "forward_spawn_to_warm", forward)
    result = headless.start(str(script), cwd=str(repo), backend="codex", run_id="r1",
                            caller_session_id="s1", notify_to="em", args={"a": 1})
    assert result == {"fire_id": "warm-record", "route": "warm"}
    assert seen["op"] == "workflow.fire"
    params = seen["params"]
    assert params["script_path"] == str(script.resolve())
    assert (params["backend"], params["run_id"], params["caller_session_id"], params["notify_to"]) == (
        "codex", "r1", "s1", "em")
    assert params["args"] == {"a": 1}
    assert not any(key.startswith("CODEX_SANDBOX") for key in params)


def test_sandboxed_warm_miss_refuses_naming_cause_and_never_spawns(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    script, repo = rig
    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: True)

    def miss(op, params, repo_arg):
        raise sandboxed_spawn.SandboxedSpawnRefused(
            "child would spawn inside the codex sandbox without network; warm engine unreachable: no_server")

    monkeypatch.setattr(sandboxed_spawn, "forward_spawn_to_warm", miss)
    with pytest.raises(sandboxed_spawn.SandboxedSpawnRefused, match="no_server") as info:
        headless.start(str(script), cwd=str(repo))
    assert "sandbox" in str(info.value)


def test_unsandboxed_takes_todays_path_and_never_forwards(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    script, repo = rig
    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: False)

    def forbidden(*_a, **_kw):
        raise AssertionError("must not forward when the probe is False")

    monkeypatch.setattr(sandboxed_spawn, "forward_spawn_to_warm", forbidden)
    reached = []

    def reserve(*_a, **_kw):
        reached.append(True)
        raise RuntimeError("stop after reserve")

    monkeypatch.setattr(headless.fire, "reserve_fire", reserve)
    with pytest.raises(RuntimeError, match="stop after reserve"):
        headless.start(str(script), cwd=str(repo))
    assert reached == [True]

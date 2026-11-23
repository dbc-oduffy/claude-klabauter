import json

from coordinator_core.agent_pipe.agy import DEFAULT_ALLOW_COMMANDS, AgyAdapter
from coordinator_core.agent_pipe.agy_model import (
    MODEL_ENV,
    REPLY_INSTRUCTION,
    job_argv,
)
from coordinator_core.agent_pipe.model_tiers import ModelSlotBlocked
import pytest


def test_argv_extends_adapter_spawn_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "env-model")
    argv = job_argv("do the thing")
    assert argv[: len(AgyAdapter().spawn_argv("x"))][1] == "-p"
    assert argv[argv.index("-p") + 1] == "do the thing" + REPLY_INSTRUCTION
    assert argv[-2:] == ["--model", "env-model"]
    assert "--output-format" in argv and "stream-json" in argv


def test_no_model_resolves_the_executor_slot_not_an_opus_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODEL_ENV, raising=False)
    assert job_argv("p")[-2:] == ["--model", "gemini-4-hydrogen-medium"]


def test_no_model_blocks_on_an_unverified_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.agent_pipe import model_tiers

    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.setitem(model_tiers.PROVIDER_TIER_MODELS["agy"], "sonnet", None)
    with pytest.raises(ModelSlotBlocked, match="agy has no verified sonnet slot"):
        job_argv("p")


def test_env_overrides_default_and_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "env-model")
    assert job_argv("p")[-2:] == ["--model", "env-model"]
    assert job_argv("p", model="arg-model")[-2:] == ["--model", "arg-model"]


def test_empty_env_falls_through_to_the_role_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "")
    assert job_argv("p")[-1] == "gemini-4-hydrogen-medium"


def test_never_bypasses_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ENV, "env-model")
    argv = job_argv("p")
    assert "--dangerously-skip-permissions" not in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    allow = settings["permissions"]["allow"]
    assert allow == [f"command({c} *)" for c in DEFAULT_ALLOW_COMMANDS]
    assert not {"command(python *)", "command(python3 *)", "command(git *)", "command(nohup *)"} & set(allow)


def test_adapter_fresh_spawn_is_job_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.agent_pipe.agy_model import AgyModelAdapter

    monkeypatch.setenv(MODEL_ENV, "env-model")
    assert AgyModelAdapter().spawn_argv("job") == job_argv("job")


def test_adapter_resume_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.agent_pipe.agy_model import AgyModelAdapter

    argv = AgyModelAdapter().spawn_argv("hi", conversation="c1")
    assert argv == AgyAdapter().resume_argv("c1", "hi")
    assert "--model" not in argv


def test_worker_loads_job_argv_adapter_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay worker, given the adapter env override, builds its child argv from job_argv."""
    from coordinator_core.agent_pipe import __main__ as worker

    monkeypatch.setenv(MODEL_ENV, "env-model")
    monkeypatch.setenv(worker.ADAPTER_ENV, "coordinator_core.agent_pipe.agy_model:AgyModelAdapter")
    adapter = worker.load_adapter("antigravity")
    assert adapter.spawn_argv("p") == job_argv("p")

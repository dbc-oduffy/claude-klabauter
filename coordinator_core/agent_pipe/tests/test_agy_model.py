from coordinator_core.agent_pipe.agy import AgyAdapter
from coordinator_core.agent_pipe.agy_model import (
    DEFAULT_MODEL,
    MODEL_ENV,
    REPLY_INSTRUCTION,
    job_argv,
)


def test_default_argv_extends_adapter_spawn_argv(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    argv = job_argv("do the thing")
    assert argv[: len(AgyAdapter().spawn_argv("x"))][1] == "-p"
    assert argv[argv.index("-p") + 1] == "do the thing" + REPLY_INSTRUCTION
    assert argv[-2:] == ["--model", DEFAULT_MODEL]
    assert "--output-format" in argv and "stream-json" in argv


def test_env_overrides_default_and_arg_overrides_env(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "env-model")
    assert job_argv("p")[-2:] == ["--model", "env-model"]
    assert job_argv("p", model="arg-model")[-2:] == ["--model", "arg-model"]


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "")
    assert job_argv("p")[-1] == DEFAULT_MODEL


def test_never_bypasses_permissions(monkeypatch):
    monkeypatch.delenv(MODEL_ENV, raising=False)
    argv = job_argv("p")
    assert "--dangerously-skip-permissions" not in argv
    assert "--settings" not in argv


def test_adapter_fresh_spawn_is_job_argv(monkeypatch):
    from coordinator_core.agent_pipe.agy_model import AgyModelAdapter

    monkeypatch.delenv(MODEL_ENV, raising=False)
    assert AgyModelAdapter().spawn_argv("job") == job_argv("job")


def test_adapter_resume_is_unchanged(monkeypatch):
    from coordinator_core.agent_pipe.agy_model import AgyModelAdapter

    argv = AgyModelAdapter().spawn_argv("hi", conversation="c1")
    assert argv == AgyAdapter().resume_argv("c1", "hi")
    assert "--model" not in argv


def test_worker_loads_job_argv_adapter_via_env(monkeypatch):
    """The relay worker, given the adapter env override, builds its child argv from job_argv."""
    from coordinator_core.agent_pipe import __main__ as worker

    monkeypatch.delenv(MODEL_ENV, raising=False)
    monkeypatch.setenv(worker.ADAPTER_ENV, "coordinator_core.agent_pipe.agy_model:AgyModelAdapter")
    adapter = worker.load_adapter("antigravity")
    assert adapter.spawn_argv("p") == job_argv("p")

"""A workflow run's test-runner leg is never denied by the suite guard or the heavy-admission guard for the
shape of its scoped targets (file count, node ids, baseline-export wrapper, PowerShell quoting); broad runs
and every other subagent stay governed."""

from __future__ import annotations

import types

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as suite
from coordinator_core.bash_guards import guard_heavy_command_admission as heavy
from coordinator_core.session import touch_record

TARGET = "tests/test_c2.py"
OTHER = "tests/test_c3.py"

_RUN_TRANSCRIPT = "projects/p/sess/subagents/workflows/wf_abc/agent-a1.jsonl"
_RUN_TRANSCRIPT_WIN = _RUN_TRANSCRIPT.replace("/", "\\")
_AD_HOC_TRANSCRIPT = "projects/p/sess/subagents/agent-a1.jsonl"

SHAPES = {
    "single-file": f"python -m pytest {TARGET}",
    "single-node-id": f"python -m pytest {TARGET}::test_a -q",
    "multi-file": f"python -m pytest {TARGET} {OTHER} -q",
    "multi-node-id": f"python -m pytest {TARGET}::test_a {OTHER}::test_b",
    "export-run": f"baseline-export --sha abc123 -- python -m pytest {TARGET}",
    "export-run-multi": f"baseline-export --sha abc123 --link node_modules -- python -m pytest {TARGET} {OTHER}",
    "export-run-module": f"python -m coordinator_core.testing.baseline_export --sha abc123 -- python -m pytest {TARGET} {OTHER}",
}

BROAD = {
    "bare": "python -m pytest",
    "directory": "python -m pytest tests/",
    "workers": f"python -m pytest {TARGET} -n 4",
    "export-run-bare": "baseline-export --sha abc123 -- python -m pytest",
}


@pytest.fixture(autouse=True)
def healthy_box(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(heavy, "_read_config", lambda: {"heavy_admission.free_ram_floor_mb": "4096"})
    monkeypatch.setattr(
        heavy, "_read_available", lambda: types.SimpleNamespace(trusted=True, avail_mb=32000, source="t")
    )
    monkeypatch.setattr(heavy, "_read_box_hold", lambda: None)
    monkeypatch.setattr(heavy, "_append_log", lambda *a, **k: None)
    monkeypatch.setattr(
        touch_record,
        "project_live_claims",
        lambda *a, **k: touch_record.TouchProjection(claims={}, degraded=False),
    )


def _payload(repo, command, *, agent_type, transcript):
    p = {
        "tool_name": "Bash",
        "session_id": "sess-A",
        "agent_id": "a1",
        "cwd": str(repo),
        "transcript_path": transcript,
        "tool_input": {"command": command},
    }
    if agent_type:
        p["agent_type"] = agent_type
    return p


def _denied(result) -> bool:
    return result is not None and getattr(result, "verdict_kind", None) == "deny"


AGENT_TYPES = ["coordinator:test-runner"]
EXECUTOR = "coordinator:executor"
OTHER_AGENT_TYPES = [None, "coordinator:code-reviewer"]


@pytest.mark.parametrize("transcript", [_RUN_TRANSCRIPT, _RUN_TRANSCRIPT_WIN])
@pytest.mark.parametrize("agent_type", AGENT_TYPES)
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_run_agent_scoped_test_target_is_allowed_by_both_guards(tmp_path, shape, agent_type, transcript):
    payload = _payload(tmp_path, SHAPES[shape], agent_type=agent_type, transcript=transcript)
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))


@pytest.mark.parametrize("transcript", [_RUN_TRANSCRIPT, _AD_HOC_TRANSCRIPT])
@pytest.mark.parametrize("agent_type", OTHER_AGENT_TYPES)
@pytest.mark.parametrize("shape", ["single-file", "multi-file", "export-run"])
def test_a_non_test_runner_subagent_stays_refused_on_identity(tmp_path, shape, agent_type, transcript):
    payload = _payload(tmp_path, SHAPES[shape], agent_type=agent_type, transcript=transcript)
    assert _denied(suite.check(payload))
    assert _denied(heavy.check(payload))


@pytest.mark.parametrize("transcript", [_RUN_TRANSCRIPT, _AD_HOC_TRANSCRIPT])
@pytest.mark.parametrize("shape", ["single-file", "single-node-id", "multi-file", "export-run"])
def test_an_executor_row_gate_file_scoped_run_is_allowed_by_both_guards(tmp_path, shape, transcript):
    payload = _payload(tmp_path, SHAPES[shape], agent_type=EXECUTOR, transcript=transcript)
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))


@pytest.mark.parametrize("agent_type", AGENT_TYPES + [EXECUTOR])
@pytest.mark.parametrize("shape", sorted(BROAD))
def test_a_broad_run_stays_governed_for_a_run_agent(tmp_path, shape, agent_type):
    payload = _payload(tmp_path, BROAD[shape], agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    assert _denied(suite.check(payload))
    assert _denied(heavy.check(payload))


def test_the_test_runner_leg_is_the_one_that_admits(tmp_path):
    from coordinator_core.bash_guards import _test_run_admission as adm

    p = _payload(tmp_path, SHAPES["multi-file"], agent_type="coordinator:test-runner", transcript=_RUN_TRANSCRIPT)
    argvs = [["python", "-m", "pytest", TARGET, OTHER]]
    assert adm.test_run_verdict(p, argvs, str(tmp_path), is_subagent=True).allowed
    p["agent_type"] = EXECUTOR
    assert adm.test_run_verdict(p, argvs, str(tmp_path), is_subagent=True).allowed
    bare = adm.test_run_verdict(p, [["python", "-m", "pytest"]], str(tmp_path), is_subagent=True)
    assert not bare.allowed and bare.leg == "scope"
    p["agent_type"] = "coordinator:code-reviewer"
    v = adm.test_run_verdict(p, argvs, str(tmp_path), is_subagent=True)
    assert not v.allowed and v.leg == "identity"


def _ps_payload(repo, command, agent_type):
    p = _payload(repo, command, agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    p["tool_name"] = "PowerShell"
    return p


@pytest.mark.parametrize("agent_type", ["coordinator:test-runner"])
@pytest.mark.parametrize(
    "command",
    [
        f"python -m pytest '{TARGET}'",
        f'python -m pytest "{TARGET}"',
        f"& python -m pytest '{TARGET}' '{OTHER}'",
        f"python -m pytest '{TARGET}::test_a' -q",
    ],
    ids=["single-quoted", "double-quoted", "call-operator-multi", "quoted-node-id"],
)
def test_a_powershell_quoted_file_target_is_allowed_for_a_run_agent(tmp_path, command, agent_type):
    payload = _ps_payload(tmp_path, command, agent_type)
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))


@pytest.mark.parametrize("agent_type", ["coordinator:test-runner", "coordinator:executor"])
@pytest.mark.parametrize("command", ["python -m pytest 'tests/'", "python -m pytest 'notes.txt'", "python -m pytest '' -n 4"])
def test_a_powershell_quoted_directory_or_non_test_target_stays_refused(tmp_path, command, agent_type):
    payload = _ps_payload(tmp_path, command, agent_type)
    assert _denied(suite.check(payload))


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
@pytest.mark.parametrize("targets", [[TARGET], [TARGET, OTHER]])
def test_the_leg_shape_from_the_shared_function_and_the_emitter_passes_both_guards(tmp_path, tool, targets):
    """One shape: the guard module's `scoped_leg_argv` names it, the emitter's leg prompt template carries
    its head, and the export wrapper plus that argv reaches neither guard's deny."""
    from coordinator_core.bash_guards._test_run_admission import scoped_leg_argv
    from coordinator_core.ops.dispatch_emit import ask_compose, clean_overlay_verify
    from coordinator_core.ops.dispatch_emit.engine_command import engine_module_command

    head = scoped_leg_argv("PY", [])
    assert ask_compose._export_argv_shape(None).startswith("`python3 -m pytest")
    assert head[1:] == ["-m", "pytest"]
    export = engine_module_command(
        "coordinator_core.ops.dispatch_emit.clean_overlay_verify", str(tmp_path),
        "--export-run --run-id r1 --repo-root %s --" % tmp_path.as_posix(),
    )
    python = clean_overlay_verify.engine_python_command(tmp_path)
    leg = " ".join(scoped_leg_argv(python, [f"'{t}'" if tool == "PowerShell" else t for t in targets]))
    payload = _payload(tmp_path, f"{export} {leg}", agent_type="coordinator:test-runner", transcript=_RUN_TRANSCRIPT)
    payload["tool_name"] = tool
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))
    direct = _payload(tmp_path, leg, agent_type="coordinator:test-runner", transcript=_RUN_TRANSCRIPT)
    direct["tool_name"] = tool
    assert not _denied(suite.check(direct))
    assert not _denied(heavy.check(direct))


_PYTHONS = {
    "posix": "/usr/bin/python3",
    "windows-drive": "C:/Users/dev/Python313/python.exe",  # abs-path-ok: synthetic fixture value
    "windows-backslash": r"C:\Users\dev\Python313\python.exe",  # abs-path-ok: synthetic fixture value
    "spaced-quoted": '"C:/Program Files/Python313/python.exe"',  # abs-path-ok: synthetic fixture value
}
_SHELLS = {"bash-c": ("Bash", "bash"), "powershell-c": ("PowerShell", "powershell"), "module": ("Bash", None)}
_TYPELESS = [None, ""]


def _export_command(repo, shell, python, targets, tool):
    from coordinator_core.bash_guards._test_run_admission import scoped_leg_argv
    from coordinator_core.ops.dispatch_emit.engine_command import OVERLAY_VERIFY_MODULE, engine_module_command

    wrapper = engine_module_command(
        OVERLAY_VERIFY_MODULE, str(repo), "--export-run --run-id r1 --repo-root %s --" % repo.as_posix(), shell=shell,
    )
    quoted = [f"'{t}'" if tool == "PowerShell" else t for t in targets]
    return f"{wrapper} {' '.join(scoped_leg_argv(python, quoted))}"


@pytest.mark.parametrize("agent_type", _TYPELESS)
@pytest.mark.parametrize("python", sorted(_PYTHONS))
@pytest.mark.parametrize("form", sorted(_SHELLS))
@pytest.mark.parametrize("targets", [[TARGET], [TARGET, OTHER], [TARGET + "::test_a"]])
def test_the_engine_export_run_leg_is_allowed_for_a_subagent_with_no_agent_type(tmp_path, form, python, targets, agent_type):
    """`clean_overlay_verify --export-run -- <python> -m pytest <files>` from a caller whose payload carries no
    agent_type (a workflow-phase agent) reaches neither guard's deny."""
    tool, shell = _SHELLS[form]
    command = _export_command(tmp_path, shell, _PYTHONS[python], targets, tool)
    payload = _payload(tmp_path, command, agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    payload["tool_name"] = tool
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))


@pytest.mark.parametrize("agent_type", _TYPELESS)
@pytest.mark.parametrize("form", sorted(_SHELLS))
@pytest.mark.parametrize("targets", [[], ["tests/"], [TARGET, "-n", "4"], ["notes.txt"]], ids=["bare", "directory", "workers", "non-test"])
def test_the_engine_export_run_leg_with_a_broad_scope_stays_refused_with_no_agent_type(tmp_path, form, targets, agent_type):
    tool, shell = _SHELLS[form]
    command = _export_command(tmp_path, shell, _PYTHONS["posix"], targets, tool)
    payload = _payload(tmp_path, command, agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    payload["tool_name"] = tool
    assert _denied(suite.check(payload))


@pytest.mark.parametrize("agent_type", _TYPELESS)
@pytest.mark.parametrize("command", [
    f"python -m pytest {TARGET}",
    f"cd /repo && python -m pytest {TARGET}",
    f"python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id r1 -- python -m pytest {TARGET}",
    f"python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --export-run python -m pytest {TARGET}",
    f"python -m other_module --export-run -- python -m pytest {TARGET}",
    f"python -m coordinator_core.ops.dispatch_emit.clean_overlay_verify --export-run -- python -m pytest {TARGET}"
    f" ; python -m pytest {OTHER}",
], ids=["direct", "chained-direct", "no-export-flag", "no-separator", "other-module", "export-then-direct-chain"])
def test_only_the_export_wrapper_admits_a_subagent_with_no_agent_type(tmp_path, command, agent_type):
    payload = _payload(tmp_path, command, agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    assert _denied(suite.check(payload))


@pytest.mark.parametrize("agent_type", ["coordinator:code-reviewer", "general-purpose"])
@pytest.mark.parametrize("form", sorted(_SHELLS))
def test_a_typed_non_test_runner_subagent_stays_refused_on_the_engine_export_run_leg(tmp_path, form, agent_type):
    tool, shell = _SHELLS[form]
    command = _export_command(tmp_path, shell, _PYTHONS["posix"], [TARGET], tool)
    payload = _payload(tmp_path, command, agent_type=agent_type, transcript=_RUN_TRANSCRIPT)
    payload["tool_name"] = tool
    assert _denied(suite.check(payload))


def test_the_export_wrapper_stem_names_the_engine_module():
    from coordinator_core.bash_guards import _test_run_admission as adm
    from coordinator_core.ops.dispatch_emit.engine_command import OVERLAY_VERIFY_MODULE

    assert OVERLAY_VERIFY_MODULE.rsplit(".", 1)[-1] == adm._EXPORT_RUN_STEM
    assert adm.is_engine_export_run(["python", "-m", OVERLAY_VERIFY_MODULE, "--export-run", "--", "python", "-m", "pytest", TARGET])
    assert adm.is_engine_export_run(["python", "/e/" + adm._EXPORT_RUN_STEM + ".py", "--export-run", "--", "x"])
    assert not adm.is_engine_export_run(["python", "-m", OVERLAY_VERIFY_MODULE, "--", "python", "-m", "pytest", TARGET])
    assert not adm.is_engine_export_run(["python", "-m", OVERLAY_VERIFY_MODULE, "--export-run"])


@pytest.mark.parametrize("transcript", [_RUN_TRANSCRIPT, _RUN_TRANSCRIPT_WIN])
@pytest.mark.parametrize("targets", [[TARGET], [TARGET, OTHER], [TARGET + "::test_a", OTHER + "::test_b"]])
def test_the_command_the_leg_emits_passes_both_guards(tmp_path, targets, transcript):
    """The emitted export-run wrapper (module name read from the emitter, not retyped) over named
    test files or node ids reaches neither guard's deny."""
    from coordinator_core.ops.dispatch_emit import emit

    command = "python -m %s --sha abc123 -- python -m pytest %s" % (emit._BASELINE_EXPORT_MODULE, " ".join(targets))
    payload = _payload(tmp_path, command, agent_type="coordinator:test-runner", transcript=transcript)
    assert not _denied(suite.check(payload))
    assert not _denied(heavy.check(payload))

from __future__ import annotations

import importlib.util
import json
import types

import pytest

from coordinator_core.ops.dispatch_emit import family_credit
from coordinator_core.ops.dispatch_emit import family_invoke as fi
from coordinator_core.ops.dispatch_emit import family_invoke_record as fir


def _setup(tmp_path):
    (tmp_path / "a.txt").write_text("old", encoding="utf-8")
    (tmp_path / "brief.md").write_text("do it", encoding="utf-8")


def _writer(text, exit_code=0, backend="agy"):
    def run(prompt, *, backend: str, repo_root, artifact_dir):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "stdout.jsonl").write_text('{"type":"thread.started","model":"m-1"}\n', encoding="utf-8")
        (artifact_dir / "stderr.txt").write_text("warn", encoding="utf-8")
        assert prompt == "do it"
        if text is not None:
            (repo_root / "a.txt").write_text(text, encoding="utf-8")
        return {"argv": ["cli", "--x"], "exit_code": exit_code, "error": None if exit_code == 0 else f"exited {exit_code}"}
    return run


def _invoke(tmp_path, runner, family="agy", stage="write"):
    return fi.invoke(tmp_path, "r1", "C1", family, stage, "brief.md", ["a.txt"], runner=runner)


def test_a_changed_declared_path_is_authored_and_credited(tmp_path, monkeypatch):
    monkeypatch.setattr(fi.sys, "platform", "linux")
    _setup(tmp_path)
    out = _invoke(tmp_path, _writer("new"), family="codex")
    assert out["family_authored"] is True and out["verdict"] == "OK" and out["vendor_model"] == "m-1"
    rec = fir.read(tmp_path, "r1", "C1")
    assert len(rec) == 1 and set(fir.RECORD_KEYS) <= set(rec[0]) and rec[0]["argv"] == ["cli", "--x"]
    assert fir.authored_paths(rec[0]) == frozenset({"a.txt"})
    assert family_credit._unauthored_cause(tmp_path, "r1", "C1", "a.txt") is None
    assert fir.raw_stdout_path(tmp_path, "r1", "C1", "write").read_text(encoding="utf-8").startswith('{"type"')
    assert fir.raw_stderr_path(tmp_path, "r1", "C1", "write").read_text(encoding="utf-8") == "warn"


def test_an_unchanged_write_is_blocked_not_authored(tmp_path):
    _setup(tmp_path)
    out = _invoke(tmp_path, _writer(None))
    assert out["family_authored"] is False and out["verdict"] == "BLOCKED"
    assert out["blocked_reason"] == "unchanged across the CLI process"
    assert family_credit._unauthored_cause(tmp_path, "r1", "C1", "a.txt") == "unchanged across the CLI process"


def test_a_runner_cannot_self_report_authorship(tmp_path):
    _setup(tmp_path)

    def liar(prompt, **kw):
        return {"argv": [], "exit_code": 0, "family_authored": True}

    assert _invoke(tmp_path, liar)["family_authored"] is False


def test_a_read_stage_is_authored_on_exit_zero_without_a_diff(tmp_path):
    _setup(tmp_path)
    assert _invoke(tmp_path, _writer(None), stage="scout")["family_authored"] is True


def test_a_non_zero_exit_with_a_changed_path_credits_nothing_and_keeps_the_record(tmp_path):
    _setup(tmp_path)
    out = _invoke(tmp_path, _writer("partial", exit_code=3))
    assert out["family_authored"] is False and out["verdict"] == "FAILED" and out["exit_code"] == 3
    rec = fir.read(tmp_path, "r1", "C1")[0]
    assert rec["exit_code"] == 3 and fir.authored_paths(rec) == frozenset()
    assert fir.raw_stdout_path(tmp_path, "r1", "C1", "write").is_file()
    assert family_credit._unauthored_cause(tmp_path, "r1", "C1", "a.txt") == "CLI exit 3"


def test_a_spawn_failure_is_a_recorded_failed_result_that_credits_nothing(tmp_path):
    _setup(tmp_path)

    def boom(prompt, **kw):
        (kw["repo_root"] / "a.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("agy exited 3")

    out = _invoke(tmp_path, boom)
    assert out["family_authored"] is False and out["verdict"] == "FAILED" and "agy exited 3" in out["blocked_reason"]
    assert family_credit._unauthored_cause(tmp_path, "r1", "C1", "a.txt") == "CLI exit 1"
    assert fir.raw_stderr_path(tmp_path, "r1", "C1", "write").is_file()


def test_cli_not_found_through_the_default_runner_writes_the_record_with_the_error(tmp_path, monkeypatch):
    from coordinator_core.codex_runner import workflow

    _setup(tmp_path)

    def missing(*a, **kw):
        raise workflow.AgentFailed("agy executable unavailable: agy not found on PATH")

    monkeypatch.setattr(workflow, "run_agent", missing)
    out = _invoke(tmp_path, None)
    rec = fir.read(tmp_path, "r1", "C1")[0]
    assert out["family_authored"] is False and rec["exit_code"] == 1 and "executable unavailable" in rec["error"]
    assert "executable unavailable" in out["blocked_reason"]
    assert fir.raw_stdout_path(tmp_path, "r1", "C1", "write").is_file()


def test_a_family_without_a_vendor_cli_is_refused_with_a_record(tmp_path):
    _setup(tmp_path)
    out = fi.invoke(tmp_path, "r1", "C1", "claude", "write", "brief.md", ["a.txt"], runner=_writer("x"))
    assert out["family_authored"] is False and out["verdict"] == "FAILED"
    assert fir.read(tmp_path, "r1", "C1")[0]["exit_code"] == 1


def test_a_stale_capture_from_an_earlier_attempt_never_becomes_the_raw_output(tmp_path):
    _setup(tmp_path)
    _invoke(tmp_path, _writer("new"))

    def silent(prompt, **kw):
        raise RuntimeError("no spawn")

    _invoke(tmp_path, silent)
    assert fir.raw_stdout_path(tmp_path, "r1", "C1", "write").read_bytes() == b""


def test_the_model_is_read_from_each_clis_own_output(tmp_path):
    (tmp_path / "stdout.jsonl").write_text('{"type":"thread.started","model":"gpt-x"}\n', encoding="utf-8")
    assert fi._vendor_model(tmp_path, "codex") == "gpt-x"
    (tmp_path / "stdout.jsonl").write_text(json.dumps({"modelUsage": {"grok-4": {"modelCalls": 3}}}), encoding="utf-8")
    assert fi._vendor_model(tmp_path, "grok") == "grok-4"
    (tmp_path / "stdout.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "agy.log").write_text('selected model override to backend: label="Gemini 3"\n', encoding="utf-8")
    assert fi._vendor_model(tmp_path, "agy") == "Gemini 3"


def test_the_model_is_null_when_the_cli_reported_none(tmp_path):
    (tmp_path / "stdout.jsonl").write_text('{"type":"turn.started"}\n', encoding="utf-8")
    assert fi._vendor_model(tmp_path, "codex") is None
    assert fi._vendor_model(tmp_path / "absent", "agy") is None


def _fake_codex(monkeypatch, tmp_path, seen):
    from coordinator_core.codex_runner import resolve, workflow
    from coordinator_core.warm import sandboxed_spawn

    monkeypatch.setattr(sandboxed_spawn, "caller_is_sandboxed", lambda: False)
    monkeypatch.setattr(workflow, "_environment", lambda: {"PATH": "p"})
    monkeypatch.setattr(resolve, "preflight_auth", lambda **kw: None)
    monkeypatch.setattr(resolve, "resolve_codex", lambda **kw: ["codex"])
    monkeypatch.setattr("coordinator_core.codex_runner.codex_spawn_env.check_pwsh_launchable", lambda env: None)

    def admitted(argv, prompt, *, env, repo_root, stdout, stderr, **kw):
        seen.update(argv=argv, env=dict(env), prompt=prompt)
        stdout.write(b'{"type":"thread.started","model":"gpt-x"}\n')
        stderr.write(b"codex-err")
        (repo_root / "a.txt").write_text("by codex", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(workflow, "_run_admitted", admitted)


def test_the_codex_branch_runs_scoped_with_exactly_the_worktree_and_its_temp_dir(tmp_path, monkeypatch):
    _setup(tmp_path)
    seen: dict = {}
    _fake_codex(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(fi.sys, "platform", "linux")
    out = _invoke(tmp_path, None, family="codex")
    argv = seen["argv"]
    assert out["family_authored"] is True and out["vendor_model"] == "gpt-x"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    roots = json.loads(next(a for a in argv if a.startswith("sandbox_workspace_write.writable_roots=")).split("=", 1)[1])
    assert roots == [str(tmp_path.absolute()), str(tmp_path.absolute() / "scratch" / "codex-tmp")]
    assert seen["env"]["TEMP"] == seen["env"]["TMP"] == roots[1]
    rec = fir.read(tmp_path, "r1", "C1")[0]
    assert rec["argv"] == argv and fir.raw_stderr_path(tmp_path, "r1", "C1", "write").read_bytes() == b"codex-err"


def test_a_win32_write_stage_never_spawns_codex_and_still_leaves_the_record(tmp_path, monkeypatch):
    _setup(tmp_path)
    seen: dict = {}
    _fake_codex(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(fi.sys, "platform", "win32")
    out = _invoke(tmp_path, None, family="codex")
    assert not seen and out["family_authored"] is False and "win32" in out["blocked_reason"]
    assert fir.read(tmp_path, "r1", "C1")[0]["exit_code"] == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "old"


def test_a_win32_read_stage_still_runs_codex(tmp_path, monkeypatch):
    _setup(tmp_path)
    seen: dict = {}
    _fake_codex(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(fi.sys, "platform", "win32")
    assert _invoke(tmp_path, None, family="codex", stage="scout")["family_authored"] is True and seen


def test_main_prints_the_result_and_exits_non_zero_unless_authored(tmp_path, capsys):
    _setup(tmp_path)
    code = fi.main(["--root", str(tmp_path), "--run-id", "r1", "--row", "C1", "--family", "claude",
                    "--stage", "write", "--prompt", "brief.md", "--writes", "a.txt"])
    printed = json.loads(capsys.readouterr().out)
    assert code == 1 and printed["family_authored"] is False and printed["verdict"] == "FAILED"


def test_the_emitted_invoker_module_exists_and_has_a_cli():
    assert importlib.util.find_spec(fir.INVOKE_MODULE) is not None
    assert callable(fi.main)

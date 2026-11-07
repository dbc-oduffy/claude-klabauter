"""agy review arm against the fake agy: argv form, NDJSON turns, findings, cumulative usage, process reuse."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.codex_runner import agy
from coordinator_core.codex_runner.contract import FALLBACK_REVIEWER, RunRequest
from coordinator_core.codex_runner.resolve import CodexUnavailable

pytestmark = pytest.mark.complete

FIX = Path(__file__).parent / "fixtures"
FAKE = [sys.executable, str(FIX / "fake_agy.py")]
GOOD = {
    "verdict": "WARN",
    "findings": [
        {"severity": "high", "title": "add subtracts", "location": "artifact.py:2", "detail": "a - b"}
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("FAKE_AGY_EXIT", "FAKE_AGY_STDERR", "FAKE_AGY_DIE_AFTER", "FAKE_AGY_HANG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_AGY_JSONL", str(FIX / "agy_review_run.jsonl"))
    monkeypatch.setenv("FAKE_AGY_ARGV_OUT", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_AGY_STDIN_OUT", str(tmp_path / "stdin.txt"))
    monkeypatch.setenv("FAKE_AGY_PID_OUT", str(tmp_path / "pids.txt"))
    real_env = agy._build_env

    def env_with_fake_knobs(*a, **kw):
        # The allowlist drops FAKE_AGY_*; only the stand-in needs them.
        import os

        out = real_env(*a, **kw)
        if not a and "environ" not in kw:
            out.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_AGY_")})
        return out

    monkeypatch.setattr(agy, "_build_env", env_with_fake_knobs)
    monkeypatch.setattr(agy, "resolve_agy", lambda: FAKE)
    artifact = tmp_path / "artifact.py"
    artifact.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    req = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(artifact))
    return tmp_path, tmp_path / "runs" / "run1", req, monkeypatch


def test_spawn_argv_and_single_user_line_with_artifact_inlined(env):
    tmp_path, run_dir, req, _ = env
    pids: list[int] = []
    res = agy.review_run(req, run_dir, pids.append)
    assert res.status == "ok" and res.verdict == "WARN" and res.findings == GOOD["findings"]
    assert res.confinement["breached"] is False
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "-p=" in argv
    assert argv[argv.index("--mode") + 1] == "plan" and "--sandbox" in argv
    assert argv[argv.index("--json-schema") + 1].endswith("review_findings.schema.json")
    assert "--dangerously-skip-permissions" not in argv and "--conversation" not in argv
    lines = (tmp_path / "stdin.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    turn = json.loads(lines[0])
    assert turn["event"] == "user" and "return a - b" in turn["message"]["content"]
    assert pids == [res.codex_pid] and res.codex_pid


def test_result_carries_cumulative_usage_and_per_turn(env):
    _, run_dir, req, _ = env
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.usage["input_tokens"] == 1000 and res.usage["per_turn"] == {"input_tokens": 1000, "output_tokens": 50}
    assert res.usage["turn_seconds"] >= 0
    assert (run_dir / "agy-events.jsonl").read_text(encoding="utf-8").count("\n") >= 3


def test_two_turns_reuse_one_process_and_usage_is_a_difference_never_a_sum(env):
    tmp_path, run_dir, _, _ = env
    with agy.AgyConversation(run_dir, FAKE, None) as conv:
        first = conv.send("one")
        assert conv.usage == {"input_tokens": 1000, "output_tokens": 50}
        second = conv.send("two")
    assert json.loads(first["response"])["verdict"] == "WARN"
    assert json.loads(second["response"])["verdict"] == "OK"
    assert conv.usage == {"input_tokens": 1800, "output_tokens": 90}
    assert conv.turn_usage == [
        {"input_tokens": 1000, "output_tokens": 50},
        {"input_tokens": 800, "output_tokens": 40},
    ]
    assert len((tmp_path / "pids.txt").read_text(encoding="utf-8").split()) == 1
    assert len((tmp_path / "stdin.txt").read_text(encoding="utf-8").splitlines()) == 2


def test_usage_delta():
    assert agy.usage_delta(None, {"a": 5}) == {"a": 5}
    assert agy.usage_delta({"a": 5, "b": 1}, {"a": 8, "b": 1, "c": 2, "s": "x"}) == {"a": 3, "b": 0, "c": 2}
    assert agy.usage_delta({"a": 1}, None) is None


def test_agy_absent_is_named_unavailable(env, monkeypatch):
    _, run_dir, req, mp = env
    mp.undo()
    monkeypatch.setattr(agy.shutil, "which", lambda name: None)
    with pytest.raises(agy.AgyUnavailable) as ei:
        agy.resolve_agy()
    assert isinstance(ei.value, CodexUnavailable)
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "AgyUnavailable" in res.error


def test_child_env_has_no_api_key_and_keeps_path(env):
    _, run_dir, req, mp = env
    mp.setenv("GEMINI_API_KEY", "leak")
    mp.setenv("OPENAI_API_KEY", "leak")
    seen = {}
    real = agy.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        assert kw["shell"] is False
        return real(argv, **kw)

    mp.setattr(agy.subprocess, "Popen", spy)
    assert agy.review_run(req, run_dir, lambda pid: None).status == "ok"
    assert "GEMINI_API_KEY" not in seen and "OPENAI_API_KEY" not in seen and "PATH" in seen


def test_env_allowlist():
    src = {"PATH": "/p", "HOME": "/h", "GEMINI_API_KEY": "k", "SECRET": "x", "SYSTEMROOT": "S"}
    assert agy._build_env(src, windows=False) == {"PATH": "/p", "HOME": "/h"}
    assert agy._build_env(src, windows=True)["SYSTEMROOT"] == "S"
    assert "GEMINI_API_KEY" not in agy._build_env(src, windows=True)


def _spy_env(mp):
    seen = {}
    real = agy.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(agy.subprocess, "Popen", spy)
    return seen


def test_flag_off_scrubs_key_and_passes_no_settings(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("GEMINI_API_KEY", "leak")
    mp.delenv("CODEX_RUNNER_ALLOW_API_KEY", raising=False)
    mp.setattr(agy, "read_api_key", lambda *a, **k: pytest.fail("keychain read with flag off"))
    seen = _spy_env(mp)
    assert agy.review_run(req, run_dir, lambda pid: None).status == "ok"
    assert "GEMINI_API_KEY" not in seen
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert "--settings" not in argv


def test_flag_on_reads_key_sets_child_env_and_settings_not_argv(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("GEMINI_API_KEY", "parent-leak")
    mp.setattr(agy, "read_api_key", lambda *a, **k: "paid-key")
    seen = _spy_env(mp)
    on = RunRequest(mode="review", repo_root=req.repo_root, artifact=req.artifact, allow_api_key=True)
    assert agy.review_run(on, run_dir, lambda pid: None).status == "ok"
    assert seen["GEMINI_API_KEY"] == "paid-key"
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert json.loads(argv[argv.index("--settings") + 1]) == {"modelProvider": "gemini"}
    assert "paid-key" not in " ".join(argv)
    assert "--dangerously-skip-permissions" not in argv


def test_flag_on_via_env_flag_also_enables(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("CODEX_RUNNER_ALLOW_API_KEY", "1")
    mp.setattr(agy, "read_api_key", lambda *a, **k: "paid-key")
    seen = _spy_env(mp)
    assert agy.review_run(req, run_dir, lambda pid: None).status == "ok"
    assert seen["GEMINI_API_KEY"] == "paid-key"


def test_flag_on_without_key_fails_and_never_spawns_or_falls_back(env):
    _, run_dir, req, mp = env
    mp.setattr(agy, "read_api_key", lambda *a, **k: None)
    mp.setattr(agy.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    on = RunRequest(mode="review", repo_root=req.repo_root, artifact=req.artifact, allow_api_key=True)
    res = agy.review_run(on, run_dir, lambda pid: pytest.fail("spawned"))
    assert res.status == "failed" and "gemini-use-api-key" in res.error


def test_paid_exhaustion_is_limit_with_remedy(env):
    tmp_path, run_dir, req, mp = env
    mp.setattr(agy, "read_api_key", lambda *a, **k: "paid-key")
    mp.setenv("FAKE_AGY_DIE_AFTER", "0")
    mp.setenv("FAKE_AGY_EXIT", "3")
    mp.setenv("FAKE_AGY_STDERR", 'AGY_ERROR: {"code": 402, "status": "RESOURCE_EXHAUSTED"}')
    on = RunRequest(mode="review", repo_root=req.repo_root, artifact=req.artifact, allow_api_key=True)
    res = agy.review_run(on, run_dir, lambda pid: None)
    assert res.status == "limit_exhausted" and "ai.studio/projects" in res.error


def test_read_api_key_per_os():
    calls = []

    class CP:
        returncode, stdout = 0, "kc-key\n"

    def run(argv, **kw):
        calls.append(argv)
        return CP()

    assert agy.read_api_key("darwin", {}, run) == "kc-key"
    assert calls == [["security", "find-generic-password", "-s", "gemini-use-api-key", "-w"]]
    assert agy.read_api_key("win32", {"GEMINI_API_KEY": "e"}, run) == "e"
    assert agy.read_api_key("win32", {}, run) is None and len(calls) == 1

    class Miss:
        returncode, stdout = 44, ""

    assert agy.read_api_key("darwin", {"GEMINI_API_KEY": "e"}, lambda *a, **k: Miss()) is None

    def boom(*a, **k):
        raise OSError("no security")

    assert agy.read_api_key("darwin", {}, boom) is None


def test_env_with_api_key_only_when_passed():
    src = {"PATH": "/p", "GEMINI_API_KEY": "parent"}
    assert "GEMINI_API_KEY" not in agy._build_env(src, windows=False)
    assert agy._build_env(src, windows=False, api_key="k")["GEMINI_API_KEY"] == "k"


def _fixture(tmp_path, monkeypatch, response, status="SUCCESS", **extra):
    p = tmp_path / "turn.jsonl"
    result = {"status": status, "response": response, "usage": {"input_tokens": 1}, **extra}
    p.write_text(json.dumps({"event": "result", "result": result}) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_AGY_JSONL", str(p))


@pytest.mark.parametrize("bad", ["not json", json.dumps({"verdict": "OK"}), json.dumps({"verdict": "MAYBE", "findings": []}),
                                 json.dumps({"verdict": "OK", "findings": [], "extra": 1})])
def test_schema_invalid_response_is_failed(env, bad):
    tmp_path, run_dir, req, mp = env
    _fixture(tmp_path, mp, bad)
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "schema-valid" in res.error


def test_fenced_json_response_parses(env):
    tmp_path, run_dir, req, mp = env
    _fixture(tmp_path, mp, "```json\n" + json.dumps(GOOD) + "\n```")
    assert agy.review_run(req, run_dir, lambda pid: None).findings == GOOD["findings"]


def test_error_status_is_failed_and_exhaustion_is_limit_with_fallback(env):
    tmp_path, run_dir, req, mp = env
    _fixture(tmp_path, mp, "kaboom", status="ERROR")
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and res.fallback is None and "kaboom" in res.error
    _fixture(tmp_path, mp, "402 RESOURCE_EXHAUSTED: prepayment depleted", status="ERROR")
    res = agy.review_run(req, tmp_path / "runs" / "run2", lambda pid: None)
    assert res.status == "limit_exhausted" and res.fallback == FALLBACK_REVIEWER
    assert "RESOURCE_EXHAUSTED" in res.error


def test_death_before_result_is_failed_with_stderr(env):
    _, run_dir, req, mp = env
    mp.setenv("FAKE_AGY_DIE_AFTER", "0")
    mp.setenv("FAKE_AGY_EXIT", "3")
    mp.setenv("FAKE_AGY_STDERR", "AGY_ERROR: {\"code\": 500}")
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "exited 3" in res.error and "AGY_ERROR" in res.error


def test_exit_with_exhaustion_stderr_is_limit_exhausted(env):
    _, run_dir, req, mp = env
    mp.setenv("FAKE_AGY_DIE_AFTER", "0")
    mp.setenv("FAKE_AGY_EXIT", "3")
    mp.setenv("FAKE_AGY_STDERR", 'AGY_ERROR: {"code": 402, "status": "RESOURCE_EXHAUSTED"}')
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "limit_exhausted" and res.fallback == FALLBACK_REVIEWER


def test_timeout_kills_the_process_and_fails(env):
    _, run_dir, _, mp = env
    mp.setenv("FAKE_AGY_HANG", "1")
    conv = agy.AgyConversation(run_dir, FAKE, None, timeout_s=0.5).open()
    try:
        with pytest.raises(agy.AgyError, match="timed out"):
            conv.send("hi")
        assert conv.proc.wait(timeout=5) is not None
    finally:
        conv.close()


def test_missing_artifact_is_failed_and_never_spawns(env):
    tmp_path, run_dir, _, mp = env
    mp.setattr(agy.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    gone = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(tmp_path / "nope"))
    assert agy.review_run(gone, run_dir, lambda pid: pytest.fail("spawned")).status == "failed"


def test_directory_artifact_is_inlined_with_headers(env):
    tmp_path, run_dir, _, _ = env
    d = tmp_path / "art"
    (d / "sub").mkdir(parents=True)
    (d / "a.py").write_text("A_BODY", encoding="utf-8")
    (d / "sub" / "b.py").write_text("B_BODY", encoding="utf-8")
    name, text = agy._artifact_text(str(d))
    assert name == "art" and "--- a.py ---\nA_BODY" in text and "--- sub/b.py ---\nB_BODY" in text


def test_oversize_artifact_is_refused(env, monkeypatch):
    tmp_path, run_dir, req, mp = env
    mp.setattr(agy, "MAX_ARTIFACT_CHARS", 5)
    res = agy.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "too large" in res.error

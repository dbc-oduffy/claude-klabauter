"""grok review arm against the fake grok: argv form, findings, and bounded login/quota/silence outcomes."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.codex_runner import grok
from coordinator_core.codex_runner.contract import FALLBACK_REVIEWER, RunRequest
from coordinator_core.codex_runner.resolve import CodexUnavailable

FIX = Path(__file__).parent / "fixtures"
FAKE = [sys.executable, str(FIX / "fake_grok.py")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("FAKE_GROK_EXIT", "FAKE_GROK_STDERR", "FAKE_GROK_HANG", "FAKE_GROK_JSONL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_GROK_ARGV_OUT", str(tmp_path / "argv.json"))
    real_env = grok._build_env

    def env_with_fake_knobs(*a, **kw):
        out = real_env(*a, **kw)
        out.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_GROK_")})
        return out

    monkeypatch.setattr(grok, "_build_env", env_with_fake_knobs)
    artifact = tmp_path / "artifact.py"
    artifact.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    req = RunRequest(mode="grok-review", repo_root=str(tmp_path), artifact=str(artifact))
    return tmp_path, tmp_path / "runs" / "run1", req, monkeypatch


def _run(env, fixture=None, **kw):
    _, run_dir, req, mp = env
    if fixture:
        mp.setenv("FAKE_GROK_JSONL", str(FIX / fixture))
    return grok.review_run(req, run_dir, lambda pid: None, argv_prefix=FAKE, **kw)


def test_review_ok_returns_findings_with_provenance_and_read_only_argv(env):
    res = _run(env, "grok_review_run.jsonl")
    assert res.status == "ok" and res.verdict == "WARN" and res.findings[0]["title"] == "add subtracts"
    assert res.confinement["breached"] is False and res.codex_pid
    assert res.usage["input_tokens"] == 1000
    assert res.usage["provenance"]["from_kind"] == "grok" and res.usage["provenance"]["source"] == "grok -p"
    argv = json.loads((env[0] / "argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--tools") + 1] == "Read,Grep"
    assert "--permission-mode" not in argv
    assert all(flag in argv for flag in ("--no-subagents", "--no-plan", "--no-auto-update"))
    assert argv[argv.index("--output-format") + 1] == "streaming-json"
    assert "--always-approve" not in argv
    assert "return a - b" in argv[argv.index("-p") + 1]


def test_build_argv_never_contains_auto_approve_and_refuses_it():
    argv = grok.build_argv(["grok"], "p", session_id="s")
    assert argv[:3] == ["grok", "-p", "p"] and "--always-approve" not in argv
    assert argv[argv.index("-s") + 1] == "s" and argv[argv.index("--max-turns") + 1] == str(grok.GROK_MAX_TURNS)
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--deny"] == list(grok.DENY_RULES)
    with pytest.raises(ValueError):
        grok.build_argv(["grok", "--always-approve"], "p")


def test_quota_event_is_limit_exhausted_with_cause(env):
    res = _run(env, "grok_quota_exhausted.jsonl")
    assert res.status == "limit_exhausted" and res.fallback == FALLBACK_REVIEWER
    assert res.error.startswith("quota_exhausted:") and "free-usage-exhausted" in res.error


def test_login_event_is_login_required_with_remedy(env):
    res = _run(env, "grok_login_required.jsonl")
    assert res.status == "login_required" and "grok login" in res.error
    assert res.error.startswith("login_required:")


def test_login_on_stderr_without_events_is_login_required(env):
    env[3].setenv("FAKE_GROK_STDERR", "error: not logged in")
    env[3].setenv("FAKE_GROK_EXIT", "1")
    assert _run(env).status == "login_required"


def test_quota_text_in_a_message_event_is_not_a_cause(env, tmp_path):
    fx = tmp_path / "msg.jsonl"
    fx.write_text(
        json.dumps({"type": "assistant", "text": "the code hits a rate limit"}) + "\n"
        + json.dumps({"type": "result", "result": json.dumps({"verdict": "OK", "findings": []})}) + "\n",
        encoding="utf-8",
    )
    env[3].setenv("FAKE_GROK_JSONL", str(fx))
    res = _run(env)
    assert res.status == "ok" and res.verdict == "OK"


def test_silent_agent_fails_turn_timeout_within_deadline(env):
    env[3].setenv("FAKE_GROK_HANG", "1")
    t0 = time.monotonic()
    res = _run(env, idle_s=0.5, timeout_s=30)
    assert time.monotonic() - t0 < 15
    assert res.status == "failed" and res.error.startswith("turn_timeout:")


def test_total_deadline_also_fires(env):
    env[3].setenv("FAKE_GROK_HANG", "1")
    res = _run(env, idle_s=30, timeout_s=0.5)
    assert res.status == "failed" and "turn_timeout" in res.error and "0.5s" in res.error


def test_empty_exit_without_result_is_failed(env):
    env[3].setenv("FAKE_GROK_STDERR", "boom")
    env[3].setenv("FAKE_GROK_EXIT", "3")
    res = _run(env)
    assert res.status == "failed" and "exited 3" in res.error and "boom" in res.error


def test_non_json_response_is_failed_not_ok(env, tmp_path):
    fx = tmp_path / "bad.jsonl"
    fx.write_text(json.dumps({"type": "result", "result": "no json here"}) + "\n", encoding="utf-8")
    env[3].setenv("FAKE_GROK_JSONL", str(fx))
    res = _run(env)
    assert res.status == "failed" and "no schema-valid findings" in res.error


def _no_native(monkeypatch, tmp_path):
    empty = tmp_path / "emptyhome"
    empty.mkdir(exist_ok=True)
    for key in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(key, str(empty))
    monkeypatch.setenv("PATH", str(empty))


def test_grok_resolves_from_home_bin_when_path_lacks_it(tmp_path, monkeypatch):
    _no_native(monkeypatch, tmp_path)
    home = tmp_path / "emptyhome"
    exe = home / ".grok" / "bin" / ("grok.exe" if sys.platform.startswith("win") else "grok")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    assert grok.resolve_grok() == [str(exe)]


def test_grok_absent_is_named_unavailable(env, monkeypatch, tmp_path):
    _, run_dir, req, mp = env
    _no_native(monkeypatch, tmp_path)
    with pytest.raises(grok.GrokUnavailable) as ei:
        grok.resolve_grok()
    assert isinstance(ei.value, CodexUnavailable)
    res = grok.review_run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "GrokUnavailable" in res.error


def test_child_env_forwards_no_api_keys(env):
    _, run_dir, req, mp = env
    mp.setenv("XAI_API_KEY", "leak")
    seen = {}
    real = grok.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        assert kw["shell"] is False
        return real(argv, **kw)

    mp.setattr(grok.subprocess, "Popen", spy)
    mp.setenv("FAKE_GROK_JSONL", str(FIX / "grok_review_run.jsonl"))
    assert grok.review_run(req, run_dir, lambda pid: None, argv_prefix=FAKE).status == "ok"
    assert "XAI_API_KEY" not in seen and "PATH" in seen


def test_windows_env_keeps_essentials():
    keys = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "COMSPEC")
    src = {k: f"v-{k}" for k in keys} | {"XAI_API_KEY": "k"}
    win = grok._build_env(src, windows=True)
    assert all(win[k] == f"v-{k}" for k in keys) and "XAI_API_KEY" not in win
    assert {"SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "COMSPEC"}.isdisjoint(grok._build_env(src, windows=False))


def test_env_allowlist():
    src = {"PATH": "/p", "HOME": "/h", "XAI_API_KEY": "k", "GROK_HOME": "/g", "SYSTEMROOT": "S"}
    assert {k:v for k,v in grok._build_env(src, windows=False).items() if not k.startswith("COORDINATOR_")} == {"PATH": "/p", "HOME": "/h", "GROK_HOME": "/g"}
    assert grok._build_env(src, windows=True)["SYSTEMROOT"] == "S"

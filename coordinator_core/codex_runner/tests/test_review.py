"""review.run against the fake codex: schema parse, confinement audit, limit fallback, spawn form."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.codex_runner import review
from coordinator_core.codex_runner.contract import FALLBACK_REVIEWER, RunRequest
from coordinator_core.codex_runner.resolve import CodexUnavailable

FIX = Path(__file__).parent / "fixtures"
GOOD = {
    "verdict": "WARN",
    "findings": [
        {"severity": "high", "title": "add subtracts", "location": "artifact.py:2", "detail": "a - b"}
    ],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "resolve_codex", lambda: [sys.executable, str(FIX / "fake_codex.py")])
    for k in ("FAKE_CODEX_JSONL", "FAKE_CODEX_LAST_JSON", "FAKE_CODEX_EXIT", "FAKE_CODEX_STDERR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKE_CODEX_ARGV_OUT", str(tmp_path / "argv.json"))
    (tmp_path / "chome").mkdir()
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "chome"))
    monkeypatch.delenv("CODEX_RUNNER_ALLOW_API_KEY", raising=False)
    real_env = review._build_env

    def env_with_fake_knobs(*a, **kw):
        # The allowlist drops FAKE_CODEX_*; only the stand-in needs them.
        out = real_env(*a, **kw)
        if not a and "environ" not in kw:
            out.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_CODEX_")})
        return out

    monkeypatch.setattr(review, "_build_env", env_with_fake_knobs)
    artifact = tmp_path / "artifact.py"
    artifact.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "run1"
    req = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(artifact))
    return tmp_path, run_dir, req, monkeypatch


def _last(tmp_path: Path, payload) -> str:
    p = tmp_path / "last_src.json"
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return str(p)


def _events(tmp_path: Path, commands: list[str]) -> str:
    p = tmp_path / "events_src.jsonl"
    lines = [{"type": "thread.started", "thread_id": "t"}]
    for c in commands:
        lines.append({"type": "item.completed", "item": {"type": "command_execution", "command": c,
                                                         "exit_code": 0, "status": "completed"}})
    lines.append({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return str(p)


def test_ok_run_returns_schema_valid_findings_and_spawn_form(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("FAKE_CODEX_JSONL", str(FIX / "review_run.jsonl"))
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, GOOD))
    pids: list[int] = []
    res = review.run(req, run_dir, pids.append)
    assert res.status == "ok"
    assert res.verdict == "WARN" and res.findings == GOOD["findings"]
    assert res.confinement["breached"] is False
    assert res.usage["input_tokens"] == 32578
    assert pids == [res.codex_pid] and res.codex_pid
    assert (run_dir / "input" / "artifact.py").read_text(encoding="utf-8").startswith("def add")
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8").count("\n") >= 5
    argv = json.loads((tmp_path / "argv.json").read_text(encoding="utf-8"))
    assert argv[:6] == ["exec", "--json", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check"]
    assert argv[argv.index("--output-schema") + 1].endswith("review_findings.schema.json")
    assert argv[argv.index("-o") + 1] == str(run_dir / "last.json")
    assert "input/artifact.py" in argv[-1]


def test_limit_fixture_yields_limit_exhausted_with_fallback(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("FAKE_CODEX_JSONL", str(FIX / "limit_exhausted.jsonl"))
    mp.setenv("FAKE_CODEX_EXIT", "1")
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "limit_exhausted"
    assert res.fallback == FALLBACK_REVIEWER == "claude-reviewer"
    assert "usage limit" in res.error
    assert res.findings == []


def test_nonlimit_failure_has_no_fallback(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("FAKE_CODEX_EXIT", "2")
    mp.setenv("FAKE_CODEX_STDERR", "boom")
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and res.fallback is None
    assert "boom" in res.error


@pytest.mark.parametrize("bad", ["not json", {"verdict": "OK"}, {"verdict": "MAYBE", "findings": []},
                                 {"verdict": "OK", "findings": [], "extra": 1}])
def test_schema_invalid_last_json_is_failed(env, bad):
    tmp_path, run_dir, req, mp = env
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, bad))
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "schema-valid" in res.error


def test_missing_last_json_is_failed(env):
    _, run_dir, req, _ = env
    assert review.run(req, run_dir, lambda pid: None).status == "failed"


def test_read_outside_run_dir_sets_breached_and_keeps_findings(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("FAKE_CODEX_JSONL", _events(tmp_path, ["/bin/bash -lc 'cat input/artifact.py'",
                                                     "/bin/bash -lc 'cat /etc/hosts'"]))
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, GOOD))
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "confinement_breach"
    assert res.confinement["breached"] is True
    assert res.confinement["paths"] == ["/etc/hosts"]
    assert res.findings == GOOD["findings"] and res.verdict == "WARN"


def test_real_review_fixture_is_not_a_breach(env):
    tmp_path, run_dir, _, _ = env
    events = review._events(FIX / "review_run.jsonl")
    assert review.audit_confinement(events, run_dir)["breached"] is False


@pytest.mark.parametrize("command,paths", [
    ("cat input/a.py", []),
    ("/usr/bin/cat input/a.py | /usr/bin/wc -l", []),
    ("cat ../secret", ["../secret"]),
    ("sh -c 'cat ~/.ssh/id_rsa'", ["~/.ssh/id_rsa"]),
    ("grep -r --include=/etc/x foo input", ["/etc/x"]),
    ("cat input/a.py; cat /var/log/x", ["/var/log/x"]),
    ("echo hi > /dev/null", []),
])
def test_audit_confinement_commands(tmp_path, command, paths):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    ev = [{"type": "item.completed", "item": {"type": "command_execution", "command": command}}]
    assert review.audit_confinement(ev, run_dir)["paths"] == paths


def test_absolute_path_inside_run_dir_is_allowed(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    ev = [{"item": {"type": "command_execution", "command": f"cat {run_dir}/input/a.py"}}]
    assert review.audit_confinement(ev, run_dir)["breached"] is False


def test_env_allowlist_drops_api_key():
    src = {"PATH": "/p", "HOME": "/h", "OPENAI_API_KEY": "sk", "CODEX_HOME": "/c", "LANG": "C",
           "SYSTEMROOT": "S", "SECRET": "x"}
    assert review._build_env(src, windows=False) == {"PATH": "/p", "HOME": "/h", "CODEX_HOME": "/c", "LANG": "C"}
    assert review._build_env(src, windows=True)["SYSTEMROOT"] == "S"
    assert "OPENAI_API_KEY" not in review._build_env(src, windows=True)


def test_child_env_never_sees_api_key(env):
    tmp_path, run_dir, req, mp = env
    mp.setenv("OPENAI_API_KEY", "sk-leak")
    seen = {}
    real = review.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(review.subprocess, "Popen", spy)
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, GOOD))
    review.run(req, run_dir, lambda pid: None)
    assert "OPENAI_API_KEY" not in seen and "PATH" in seen


def test_missing_artifact_and_unavailable_codex_are_failed(env):
    tmp_path, run_dir, _, mp = env
    gone = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(tmp_path / "nope"))
    assert review.run(gone, run_dir, lambda pid: None).status == "failed"

    def boom():
        raise CodexUnavailable("no fnm")

    mp.setattr(review, "resolve_codex", boom)
    req = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(tmp_path / "artifact.py"))
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "failed" and "no fnm" in res.error


def _api_key_login(tmp_path):
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")


def test_api_key_login_refused_before_spawn(env):
    tmp_path, run_dir, req, mp = env
    _api_key_login(tmp_path)
    mp.setattr(review.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    res = review.run(req, run_dir, lambda pid: pytest.fail("spawned"))
    assert res.status == "failed" and "API key" in res.error


def test_api_key_login_accepted_with_flag_and_key_passes_env(env):
    tmp_path, run_dir, req, mp = env
    _api_key_login(tmp_path)
    mp.setenv("CODEX_RUNNER_ALLOW_API_KEY", "1")
    mp.setenv("OPENAI_API_KEY", "sk-x")
    seen = {}
    real = review.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(review.subprocess, "Popen", spy)
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, GOOD))
    res = review.run(req, run_dir, lambda pid: None)
    assert res.status == "ok" and seen.get("OPENAI_API_KEY") == "sk-x"


def test_request_flag_allows_api_key_login_and_env(env):
    tmp_path, run_dir, _, mp = env
    _api_key_login(tmp_path)
    mp.setenv("OPENAI_API_KEY", "sk-x")
    seen = {}
    real = review.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(review.subprocess, "Popen", spy)
    mp.setenv("FAKE_CODEX_LAST_JSON", _last(tmp_path, GOOD))
    on = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(tmp_path / "artifact.py"),
                    allow_api_key=True)
    assert review.run(on, run_dir, lambda pid: None).status == "ok"
    assert seen.get("OPENAI_API_KEY") == "sk-x"
    off = RunRequest(mode="review", repo_root=str(tmp_path), artifact=str(tmp_path / "artifact.py"))
    assert review.run(off, tmp_path / "runs" / "run2", lambda pid: None).status == "failed"


def test_env_allowlist_flag_unset_drops_and_set_keeps_keys():
    src = {"PATH": "/p", "OPENAI_API_KEY": "sk", "CODEX_API_KEY": "ck"}
    assert review._build_env(src, windows=False) == {"PATH": "/p"}
    on = dict(src, CODEX_RUNNER_ALLOW_API_KEY="true")
    assert review._build_env(on, windows=False) == {"PATH": "/p", "OPENAI_API_KEY": "sk", "CODEX_API_KEY": "ck"}

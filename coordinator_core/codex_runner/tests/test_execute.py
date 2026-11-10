"""Tests for codex_runner.execute against the fake codex (never the real CLI)."""
import json
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.codex_runner import execute
from coordinator_core.codex_runner.contract import RunRequest

FIXTURES = Path(__file__).parent / "fixtures"
FAKE = str(FIXTURES / "fake_codex.py")

PLAN = """---
title: t
---
# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: "Edit things"
  surface: src
  writes:
    - src/a.py
    - src/sub/b.py
  body: |
    Make the edit.
  disposition: open
- id: C2
  title: "No writes"
  surface: src
  body: nothing
  disposition: open
- id: C3
  title: "Already done"
  surface: src
  writes:
    - src/c.py
  disposition: coded
```
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    plan = root / "plan.md"
    plan.write_text(PLAN, encoding="utf-8")
    monkeypatch.setattr(execute, "resolve_codex", lambda: [sys.executable, FAKE])
    argv_out = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_CODEX_ARGV_OUT", str(argv_out))
    (tmp_path / "chome").mkdir()
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "chome"))
    monkeypatch.delenv("CODEX_RUNNER_ALLOW_API_KEY", raising=False)
    real_env = execute._build_env

    def env_with_fake_knobs(*a, **kw):
        out = real_env(*a, **kw)
        if not a and "environ" not in kw:
            out.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_CODEX_")})
        return out

    monkeypatch.setattr(execute, "_build_env", env_with_fake_knobs)
    return root, plan, argv_out, tmp_path, monkeypatch


def _jsonl(tmp_path, monkeypatch, paths, extra=()):
    changes = [{"path": p, "kind": "update"} for p in paths]
    lines = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
    if paths:
        for phase in ("in_progress", "completed"):
            lines.append({
                "type": "item.started" if phase == "in_progress" else "item.completed",
                "item": {"id": "i", "type": "file_change", "changes": changes, "status": phase},
            })
    lines.extend(extra)
    lines.append({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 1}})
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_CODEX_JSONL", str(f))


def _req(root, plan, chunk):
    return RunRequest(mode="execute", repo_root=str(root), plan=str(plan), chunk=chunk)


def _run(root, plan, chunk, tmp_path):
    pids = []
    res = execute.run(_req(root, plan, chunk), tmp_path / "run1", pids.append)
    return res, pids


def test_in_scope_file_change_is_ok(env):
    root, plan, argv_out, tmp_path, mp = env
    _jsonl(tmp_path, mp, [str(root / "src" / "a.py"), str(root / "src" / "sub" / "b.py")])
    res, pids = _run(root, plan, "C1", tmp_path)
    assert res.status == "ok", res.error
    assert res.scope == {"breached": False, "paths": [], "changed": ["src/a.py", "src/sub/b.py"]}
    assert res.usage == {"input_tokens": 3, "output_tokens": 1}
    assert pids == [res.codex_pid]


def test_spawn_form_is_workspace_write_in_shared_tree(env):
    root, plan, argv_out, tmp_path, mp = env
    _jsonl(tmp_path, mp, [])
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "ok"
    argv = json.loads(argv_out.read_text())
    assert argv[:4] == ["exec", "--json", "--sandbox", "workspace-write"]
    prompt = argv[4]
    assert "Edit things" in prompt and "Make the edit." in prompt
    assert "- src/a.py" in prompt and "Write only these files" in prompt


def test_out_of_writes_path_is_scope_breach_naming_it(env):
    root, plan, _, tmp_path, mp = env
    _jsonl(tmp_path, mp, [str(root / "src" / "a.py"), str(root / "src" / "evil.py")])
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "scope_breach"
    assert res.scope["paths"] == ["src/evil.py"]
    assert "src/evil.py" in res.error


def test_path_outside_repo_root_is_breach(env):
    root, plan, _, tmp_path, mp = env
    outside = tmp_path / "elsewhere" / "a.py"
    _jsonl(tmp_path, mp, [str(outside)])
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "scope_breach"
    assert len(res.scope["paths"]) == 1


def test_backslash_declared_write_matches(env):
    root, plan, _, tmp_path, mp = env
    assert execute.audit_scope(
        [{"item": {"type": "file_change", "changes": [{"path": str(root / "src" / "sub" / "b.py")}]}}],
        ["src\\sub\\b.py"],
        str(root),
    )["breached"] is False


@pytest.mark.parametrize("chunk,why", [
    ("C2", "declares no writes"),
    ("C3", "is not open"),
    ("C9", "not found"),
])
def test_refused_before_spawn(env, chunk, why):
    root, plan, argv_out, tmp_path, mp = env
    _jsonl(tmp_path, mp, [])
    res, pids = _run(root, plan, chunk, tmp_path)
    assert res.status == "failed"
    assert res.error.startswith("refused:") and why in res.error
    assert pids == []
    assert not argv_out.exists()


def test_limit_exhausted_detected(env):
    root, plan, _, tmp_path, mp = env
    mp.setenv("FAKE_CODEX_JSONL", str(FIXTURES / "limit_exhausted.jsonl"))
    mp.setenv("FAKE_CODEX_EXIT", "1")
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "limit_exhausted"


def test_nonzero_exit_without_events_is_failed(env):
    root, plan, _, tmp_path, mp = env
    _jsonl(tmp_path, mp, [])
    mp.setenv("FAKE_CODEX_EXIT", "2")
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "failed"
    assert "2" in res.error


def test_codex_unavailable_is_failed(env):
    root, plan, _, tmp_path, mp = env

    def boom():
        raise execute.CodexUnavailable("nope")

    mp.setattr(execute, "resolve_codex", boom)
    res, pids = _run(root, plan, "C1", tmp_path)
    assert res.status == "failed" and "unavailable" in res.error and pids == []


def test_api_key_login_refused_before_spawn(env):
    root, plan, _, tmp_path, mp = env
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")
    mp.setattr(execute.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    res, pids = _run(root, plan, "C1", tmp_path)
    assert res.status == "failed" and "API key" in res.error and pids == []


def test_api_key_login_accepted_with_flag_and_env_scrubbed_otherwise(env):
    root, plan, _, tmp_path, mp = env
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")
    mp.setenv("OPENAI_API_KEY", "sk-x")
    seen = {}
    real = execute.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(execute.subprocess, "Popen", spy)
    mp.setenv("CODEX_RUNNER_ALLOW_API_KEY", "1")
    _jsonl(tmp_path, mp, ["src/a.py"])
    res, _ = _run(root, plan, "C1", tmp_path)
    assert res.status == "ok" and seen.get("OPENAI_API_KEY") == "sk-x"
    seen.clear()
    mp.delenv("CODEX_RUNNER_ALLOW_API_KEY")
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "chatgpt"}', encoding="utf-8")
    _run(root, plan, "C1", tmp_path / "second")
    assert "OPENAI_API_KEY" not in seen and "PATH" in seen


def test_request_flag_admits_api_key_login_and_env(env):
    root, plan, _, tmp_path, mp = env
    (tmp_path / "chome" / "auth.json").write_text('{"auth_mode": "apikey"}', encoding="utf-8")
    mp.setenv("OPENAI_API_KEY", "sk-x")
    seen = {}
    real = execute.subprocess.Popen

    def spy(argv, **kw):
        seen.update(kw["env"])
        return real(argv, **kw)

    mp.setattr(execute.subprocess, "Popen", spy)
    _jsonl(tmp_path, mp, ["src/a.py"])
    req = RunRequest(mode="execute", repo_root=str(root), plan=str(plan), chunk="C1", allow_api_key=True)
    res = execute.run(req, tmp_path / "run1", lambda _pid: None)
    assert res.status == "ok" and seen.get("OPENAI_API_KEY") == "sk-x"


def test_unelevated_unproven_root_runs_with_advisory(env):
    from coordinator_core.codex_runner import sandbox_preflight as sp
    root, plan, argv_out, tmp_path, mp = env
    (tmp_path / "chome" / "config.toml").write_text('[windows]\nsandbox = "unelevated"\n', encoding="utf-8")
    mp.setattr(sp, "_is_windows", lambda: True)
    mp.setattr(execute, "check_pwsh_launchable", lambda env: None)
    seen = {}

    def fake_check(root_, **kw):
        seen.update(kw)
        return "advisory: warming"

    mp.setattr(execute, "check_workspace_write", fake_check)
    _jsonl(tmp_path, mp, [str(root / "src" / "a.py")])
    res, pids = _run(root, plan, "C1", tmp_path)
    assert not (res.error or "").startswith("refused:") and pids
    assert seen["codex_argv"] == [sys.executable, FAKE]
    assert "advisory: warming" in (tmp_path / "run1" / "codex.stderr").read_text(encoding="utf-8")


def test_unelevated_proven_root_still_runs(env):
    import json as _json
    from coordinator_core.codex_runner import sandbox_preflight as sp
    root, plan, argv_out, tmp_path, mp = env
    (tmp_path / "chome" / "config.toml").write_text('[windows]\nsandbox = "unelevated"\n', encoding="utf-8")
    mp.setattr(sp, "_is_windows", lambda: True)
    mp.setattr(execute, "check_pwsh_launchable", lambda env: None)
    rec = root / sp.PROVEN_ROOTS_RELPATH
    rec.parent.mkdir(parents=True)
    rec.write_text(_json.dumps([str(root)]), encoding="utf-8")
    _jsonl(tmp_path, mp, [str(root / "src" / "a.py")])
    res, _ = _run(root, plan, "C1", tmp_path)
    assert not (res.error or "").startswith("refused:")


def test_windowsapps_pwsh_refused_before_spawn(env):
    import subprocess
    from coordinator_core.codex_runner import sandbox_preflight as sp
    root, plan, argv_out, tmp_path, mp = env
    store = tmp_path / "Microsoft" / "WindowsApps"
    store.mkdir(parents=True)
    (store / "pwsh.exe").write_text("", encoding="utf-8")
    mp.setattr(sp, "_is_windows", lambda: True)
    mp.setattr(execute, "_child_env", lambda requested: {"Path": str(store), "ProgramFiles": str(tmp_path)})
    mp.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    res, pids = _run(root, plan, "C1", tmp_path)
    assert res.status == execute.STATUS_FAILED
    assert "WindowsApps" in res.error and str(tmp_path / "PowerShell" / "7") in res.error
    assert pids == []

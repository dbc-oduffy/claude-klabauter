"""Zero-op replay: a non-Claude EM that hand-edits and never invokes an op is nagged through the shim, never denied."""
from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core import warp_fire
from coordinator_core.host_hooks import shim
from coordinator_core.session import op_dispatch_nag as nag
from coordinator_core.warm import telemetry

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NAG = "NAG-TEXT"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, creationflags=_NO_WINDOW)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for i in range(3):
        (root / f"f{i}.txt").write_text("a\n" * 5)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    for i in range(3):
        (root / f"f{i}.txt").write_text("b\n" * 40)
    monkeypatch.setattr(nag.xs_lane_budget, "lane_path", lambda r, sid: tmp_path / "hub" / sid / "lane.json")
    # The dirt above is every session's own hand edit: the lane record attributes it to the caller.
    monkeypatch.setattr(
        nag.xs_lane_budget, "read_lane",
        lambda r, sid: {"kind": "xs", "state": "open", "base_sha": "x", "paths": ["f0.txt", "f1.txt", "f2.txt"]},
    )
    monkeypatch.setattr(warp_fire, "nag_text", lambda sid, sizing_rel=None, state_dir=None, host=None, edit_size=None, edit_size_truncated=False: NAG)
    for key in ("COORDINATOR_AGENT_PROVIDER", "COORDINATOR_AGENT_VENDOR", "COORDINATOR_AGENT_ROLE",
                "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID", "COORDINATOR_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(telemetry, "record_degrade", lambda **kw: None)
    return root


def _payload(host, root, sid):
    if host == "agy":
        return {"conversationId": sid, "workspacePaths": [str(root)],
                "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
    return {"hook_event_name": "PreToolUse", "session_id": sid, "cwd": str(root),
            "tool_name": "exec_command" if host == "codex" else "Bash", "tool_input": {"command": "ls"}}


def _shows_nag(host, out):
    if host == "agy":
        # An `ask` is auto-approved under agy -p; the nag is an explicit allow stashed for the next PreInvocation.
        return out.get("decision") == "allow" and "injectSteps" not in out and NAG in str(out.get("reason"))
    return NAG in str(out) and "deny" not in str(out)


@pytest.mark.parametrize("engine", ["down", "allow"])
@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_zero_op_session_gets_advisory_then_throttled(monkeypatch, repo, host, engine):
    monkeypatch.setattr(shim, "_dial", lambda request: None if engine == "down" else {"result": {}})
    first = shim.run(host, json.dumps(_payload(host, repo, "zero-op")))
    assert _shows_nag(host, first), first
    shim.mark_if_delivered(shim.take_pending_nag(), first)
    spawns = []
    real = subprocess.run
    monkeypatch.setattr(nag.subprocess, "run", lambda *a, **k: spawns.append(a) or real(*a, **k))
    second = shim.run(host, json.dumps(_payload(host, repo, "zero-op")))
    assert not _shows_nag(host, second), second
    assert spawns == []


@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_session_with_no_own_edit_is_not_nagged_for_peer_dirt(monkeypatch, repo, host):
    monkeypatch.setattr(nag.xs_lane_budget, "read_lane", lambda r, sid: None)
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    assert not _shows_nag(host, shim.run(host, json.dumps(_payload(host, repo, "no-own-edit"))))


def test_engine_deny_is_untouched(monkeypatch, repo):
    deny = {"permissionDecision": "deny", "permissionDecisionReason": "no"}
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": deny})
    out = shim.run("codex", json.dumps(_payload("codex", repo, "deny-sid")))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert NAG not in str(out)


def test_existing_advisory_is_merged(monkeypatch, repo):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {"additionalContext": "OLD"}})
    out = shim.run("codex", json.dumps(_payload("codex", repo, "merge-sid")))
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "OLD" in text and NAG in text


@pytest.mark.parametrize("event", ["PostToolUse", "Stop", "SessionStart"])
def test_non_pretooluse_fire_gets_no_nag(monkeypatch, repo, event):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {"additionalContext": "OLD"}})
    event_nag = shim._nag_text("codex", shim._adapter("codex").adapt_event({**_payload("codex", repo, "ev-sid"), "hook_event_name": event}),
                               {})
    assert event_nag == ""


def test_subagent_fire_gets_no_nag(monkeypatch, repo):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {}})
    payload = {**_payload("codex", repo, "sub-sid"), "agent_id": "child"}
    assert NAG not in str(shim.run("codex", json.dumps(payload)))


def _write_payload(host, root, sid):
    if host == "agy":
        return {"conversationId": sid, "workspacePaths": [str(root)],
                "toolCall": {"name": "write_file", "args": {"TargetFile": str(root / "f0.txt"), "CodeContent": "c"}}}
    tool = {"codex": "apply_patch", "grok": "write_file"}[host]
    return {"hook_event_name": "PreToolUse", "session_id": sid, "cwd": str(root), "tool_name": tool,
            "tool_input": {"path": str(root / "f0.txt"), "file_path": str(root / "f0.txt"), "content": "c"}}


@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_cold_engine_write_nags_without_the_cold_chain(monkeypatch, repo, host):
    """A warm miss on a hand-edit answers the nag before the ~800ms cold guard chain, which the host guard's
    500ms wall killed every time (goal-1 probes, 2026-11-14: no nag on agy, grok or codex)."""
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    monkeypatch.setattr(shim, "_cold_verdict", lambda *a, **k: pytest.fail("cold chain ran before the nag"))
    out = shim.run(host, json.dumps(_write_payload(host, repo, "cold-write")))
    assert _shows_nag(host, out), out


def _stamp(tmp_path, sid):
    return tmp_path / "hub" / sid / nag._STAMP_FILENAME


def _drive_main(monkeypatch, capsys, host, root, sid):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(host, root, sid))))
    assert shim.main(["--host", host]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_stamp_absent_after_run_and_present_after_main_writes_the_nag(monkeypatch, repo, tmp_path, capsys, host):
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    sid = f"stamp-{host}"
    writes = []
    real_write = shim.sys.stdout.write
    monkeypatch.setattr(shim.sys.stdout, "write", lambda text: writes.append(_stamp(tmp_path, sid).exists()) or real_write(text))
    out = _drive_main(monkeypatch, capsys, host, repo, sid)
    assert _shows_nag(host, out)
    assert writes == [False]
    assert _stamp(tmp_path, sid).exists()


def test_main_stamps_nothing_when_the_answer_carries_no_nag(monkeypatch, repo, tmp_path, capsys):
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    monkeypatch.setattr(shim, "run", lambda host, text: shim.neutral_fallback(host))
    shim._pending[:] = [(str(repo), "no-nag-sid", NAG)]
    _drive_main(monkeypatch, capsys, "codex", repo, "no-nag-sid")
    assert not _stamp(tmp_path, "no-nag-sid").exists()


def test_main_stamps_nothing_when_the_pending_nag_was_not_merged(monkeypatch, repo, tmp_path, capsys):
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": {}})
    monkeypatch.setattr(shim, "run", lambda host, text: {"hookSpecificOutput": {"additionalContext": "OTHER"}})
    monkeypatch.setattr(shim, "take_pending_nag", lambda: (str(repo), "dropped-sid", NAG))
    _drive_main(monkeypatch, capsys, "codex", repo, "dropped-sid")
    assert not _stamp(tmp_path, "dropped-sid").exists()


def _load_guard():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "unimatrix-host-guard.py"
    spec = importlib.util.spec_from_file_location("unimatrix_host_guard_nag", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive_guard(monkeypatch, capsys, repo, sid, budget=None):
    import io

    guard = _load_guard()
    monkeypatch.setattr(guard, "_load_shim", lambda root: shim)
    if budget is not None:
        monkeypatch.setattr(guard, "DEFAULT_BUDGET_SECS", budget)
        monkeypatch.setattr(guard.guard, "__defaults__", (budget, None))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload("grok", repo, sid))))
    guard.main(["--host", "grok", "--engine-root", str(repo)])
    return json.loads(capsys.readouterr().out.strip())


def test_host_guard_stamps_after_it_writes_the_grok_nag(monkeypatch, repo, tmp_path, capsys):
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    out = _drive_guard(monkeypatch, capsys, repo, "guard-sid")
    assert _shows_nag("grok", out)
    assert _stamp(tmp_path, "guard-sid").exists()


def test_host_guard_timeout_writes_no_nag_and_no_stamp(monkeypatch, repo, tmp_path, capsys):
    import threading

    release = threading.Event()
    real_run = shim.run
    monkeypatch.setattr(shim, "run", lambda host, text: (real_run(host, text), release.wait(5))[0])
    monkeypatch.setattr(shim, "_dial", lambda request: None)
    try:
        out = _drive_guard(monkeypatch, capsys, repo, "guard-timeout", budget=0.2)
    finally:
        release.set()
    assert not _shows_nag("grok", out)
    assert not _stamp(tmp_path, "guard-timeout").exists()

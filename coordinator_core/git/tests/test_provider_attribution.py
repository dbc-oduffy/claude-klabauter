"""Caller attribution survives cold/warm routing without daemon leakage."""
import pytest

from coordinator_core.git.provider_attribution import attribution_value, caller_attribution
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.warm.entry_seam import per_request_state


@pytest.mark.parametrize("provider,label", [("codex", "Codex"), ("claude", "Claude"), ("agy", "agy"), ("grok", "Grok"), ("mistral", "Mistral"), ("deepseek", "DeepSeek")])
def test_explicit_provider(monkeypatch, provider, label):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", provider)
    assert attribution_value().startswith(label + " <")


def test_codex_automatic(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AGENT_PROVIDER", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    assert attribution_value() == "Codex <noreply@openai.com>"


def test_warm_caller_beats_daemon_and_resets(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "claude")
    with per_request_state(warm_served=True, isolated=False, env={"COORDINATOR_AGENT_PROVIDER": "codex"}):
        assert attribution_value() == "Codex <noreply@openai.com>"
        with caller_attribution({"COORDINATOR_AGENT_PROVIDER": "grok"}):
            assert attribution_value().startswith("Grok ")
        assert attribution_value().startswith("Codex ")
    assert attribution_value().startswith("Claude ")


def test_unidentified_warm_caller_omits_daemon(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "claude")
    with per_request_state(warm_served=True, isolated=False, env={}):
        assert attribution_value() == ""


def test_codex_commit_preserves_other_contributors_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "codex")
    monkeypatch.setattr("coordinator_core.git.commit_trailers._resolve_git_dir", lambda cwd: "")
    msg = "fix: caller identity\n\nCo-Authored-By: Human <human@example.com>\n"
    once = apply_missing_trailers(msg, tmp_path)
    twice = apply_missing_trailers(once, tmp_path)
    assert twice == once
    assert "Co-Authored-By: Codex <noreply@openai.com>" in once
    assert "Human <human@example.com>" in once
    assert "Claude <" not in once


def test_unknown_provider_omitted(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "unregistered")
    assert attribution_value() == ""


@pytest.mark.parametrize("identity,label", [("CODEX_THREAD_ID", "Codex"), ("CLAUDE_CODE_SESSION_ID", "Claude"), ("CLAUDE_SESSION_ID", "Claude")])
def test_warm_platform_identity_infers_provider_without_explicit_marker(monkeypatch, identity, label):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "grok")
    with per_request_state(warm_served=True, isolated=False, env={identity: "01a1b63c-040a-76f2-a9b9-929d9a0d0098"}):
        assert attribution_value().startswith(label + " <")


def test_sequential_warm_callers_do_not_leak(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "grok")
    for provider, label in [("codex", "Codex"), ("claude", "Claude"), ("codex", "Codex")]:
        with per_request_state(warm_served=True, isolated=False, env={"COORDINATOR_AGENT_PROVIDER": provider}):
            assert attribution_value().startswith(label + " <")


@pytest.mark.spawns_process
def test_real_git_hook_attributes_codex_preserves_human(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from coordinator_core.win_portability import no_console_creationflags

    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ, COORDINATOR_AGENT_PROVIDER="codex", PYTHONPATH=str(root),
               GIT_AUTHOR_NAME="Human Operator", GIT_AUTHOR_EMAIL="human@example.com",
               GIT_COMMITTER_NAME="Human Operator", GIT_COMMITTER_EMAIL="human@example.com")
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True,
                              capture_output=True, text=True, **no_console_creationflags()).stdout

    git("init", "-q")
    git("config", "user.name", "Human Operator")
    git("config", "user.email", "human@example.com")
    # Exercise Git's actual prepare-commit-msg delivery with the shared hook body.
    hook = tmp_path / ".git/hooks/prepare-commit-msg"
    script = root / "coordinator/bin/coordinator-prepare-commit-msg.py"
    hook.write_text(f'#!/bin/sh\n"{Path(sys.executable).as_posix()}" "{script.as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    (tmp_path / "owned.txt").write_text("owned", encoding="utf-8")
    git("add", "--", "owned.txt")
    git("commit", "-m", "test: native Codex attribution")
    message = git("log", "-1", "--format=%B")
    assert "Co-Authored-By: Codex <noreply@openai.com>" in message
    assert "Co-Authored-By: Claude" not in message
    assert git("log", "-1", "--format=%an <%ae>").strip() == "Human Operator <human@example.com>"


def test_warm_reentrant_op_preserves_outer_provider(monkeypatch):
    from coordinator_core.warm import entry_seam
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "grok")
    monkeypatch.setattr(entry_seam, "_resolve_handler", lambda name: lambda params, repo_root=None: attribution_value())
    with per_request_state(warm_served=True, isolated=False, env={"COORDINATOR_AGENT_PROVIDER": "codex"}):
        assert entry_seam.reentrant_dispatch("test.provider", {}) == "Codex <noreply@openai.com>"
        assert attribution_value() == "Codex <noreply@openai.com>"
    assert attribution_value().startswith("Grok ")

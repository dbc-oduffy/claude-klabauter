from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.hooks import session_start_cloud_focus as mod
from coordinator_core.ipc import _REGISTRY

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _cloud_env(**extra) -> dict:
    env = {"CLAUDE_CODE_REMOTE": "true", mod.TEAMS_FLAG_ENV: "1"}
    env.update(extra)
    return env


def _focus_repo(tmp_path: Path, branch_ahead: bool) -> Path:
    """A checkout with `origin` fetched into `refs/remotes/origin/main` (no
    actual remote reachable over the network -- just the ref layout a real
    clone leaves behind) and the session on `work/z`, optionally carrying
    one commit ahead of that ref."""
    repo = tmp_path / "acme"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/acme.git")
    main_sha = _git(repo, "rev-parse", "main").stdout.strip()
    (repo / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (repo / ".git" / "refs" / "remotes" / "origin" / "main").write_text(main_sha + "\n")
    _git(repo, "checkout", "-q", "-b", "work/z")
    if branch_ahead:
        (repo / "seed.txt").write_text("seed2\n", encoding="utf-8", newline="\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "work")
    return repo


def test_op_registered():
    assert "hooks.session_start_cloud_focus" in _REGISTRY


def test_no_op_off_cloud():
    result = mod._handler({"env": {mod.TEAMS_FLAG_ENV: "1"}})
    assert result.get("hookSpecificOutput", {}).get("additionalContext") is None


def test_no_op_cloud_but_nothing_to_report():
    result = mod._handler({"env": _cloud_env()})
    assert result == {}


def test_malformed_payload_safe():
    assert mod._handler({}) == {}
    assert mod._handler({"env": "not-a-mapping"}) == {}
    assert mod._handler(None) == {}


def test_teams_flag_missing_reports():
    context = mod.compute_context({"env": {"CLAUDE_CODE_REMOTE": "true"}})
    assert context is not None
    assert "agent teams OFF" in context


def test_cloud_gating_anchors_level_branch_and_hands_off(tmp_path, monkeypatch):
    repo = _focus_repo(tmp_path, branch_ahead=False)
    monkeypatch.setattr(mod, "CHECKOUT_ROOTS", ())

    payload = {
        "cwd": str(tmp_path),
        "env": _cloud_env(**{mod.FOCUS_ENV: "acme/acme", mod.SESSION_ID_ENV: "cse_123"}),
    }
    context = mod.compute_context(payload)
    assert context is not None
    assert "CLOUD FOCUS: acme/acme, branch work/z -> main" in context
    assert "coordinator:cloud-channel" in context

    log = _git(repo, "log", "--format=%s", "-1").stdout.strip()
    assert log == "cloud: open session channel"
    trailer = _git(repo, "log", "-1", "--format=%b").stdout
    assert "Cloud-Session-Id: cse_123" in trailer

    ahead = _git(repo, "rev-list", "--count", "refs/remotes/origin/main..HEAD").stdout.strip()
    assert ahead == "1"


def test_cloud_focus_already_ahead_no_anchor_needed(tmp_path, monkeypatch):
    repo = _focus_repo(tmp_path, branch_ahead=True)
    monkeypatch.setattr(mod, "CHECKOUT_ROOTS", ())
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    payload = {"cwd": str(tmp_path), "env": _cloud_env(**{mod.FOCUS_ENV: "acme/acme"})}
    context = mod.compute_context(payload)
    assert context is not None
    assert "coordinator:cloud-channel" in context
    assert "level with base" not in context

    after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert before == after  # no anchor written -- branch was already ahead


def test_cloud_focus_no_checkout_match(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CHECKOUT_ROOTS", ())
    payload = {"cwd": str(tmp_path), "env": _cloud_env(**{mod.FOCUS_ENV: "nobody/nowhere"})}
    context = mod.compute_context(payload)
    assert context is not None
    assert "no checkout matches nobody/nowhere" in context


def test_render_teams_line():
    assert mod.render_teams_line("1") is None
    assert mod.render_teams_line("") is not None


def test_parse_focus():
    assert mod.parse_focus("acme/acme") == ("acme", "acme")
    assert mod.parse_focus("acme/acme.git") == ("acme", "acme")
    assert mod.parse_focus("acme") == (None, "acme")


def test_render_focus_line_detached_and_level():
    assert "detached" in mod.render_focus_line("f", "acme/acme", None, "main", False)
    assert "cut a branch" in mod.render_focus_line("f", "acme/acme", "main", "main", False)
    assert "level with base" in mod.render_focus_line("f", "acme/acme", "z", "main", False)
    assert "First act" in mod.render_focus_line("f", "acme/acme", "z", "main", True)

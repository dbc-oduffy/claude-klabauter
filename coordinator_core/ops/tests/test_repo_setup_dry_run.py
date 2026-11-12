"""repo.setup dry_run and undeclared-param refusal, driven through the handler on a scratch git repo."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import repo_setup as mod


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

    plugin = tmp_path / "plugin"
    templates = plugin / "skills" / "repo-setup" / "templates"
    templates.mkdir(parents=True)
    (templates / "CLAUDE.md.template").write_text("# {{PROJECT_NAME}}\n", encoding="utf-8")
    (templates / "README.md.template").write_text("# [PROJECT_NAME]\n", encoding="utf-8")
    (templates / "gitignore.project.template").write_text(".coordinator-local/\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_plugin_root", lambda: plugin)
    monkeypatch.setattr(mod, "_machine_profile", lambda: "consumer")

    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    return target


def _tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in root.rglob("*")
        if p.relative_to(root).parts[0] != ".git"
    }


def _git_state(root: Path) -> tuple:
    hooks = sorted(p.name for p in (root / ".git" / "hooks").iterdir() if not p.name.endswith(".sample"))
    cfg = (root / ".git" / "config").read_bytes()
    return hooks, cfg


def test_dry_run_true_writes_nothing_and_reports_would_create(repo) -> None:
    before_tree, before_git = _tree(repo), _git_state(repo)
    verdict = mod.handle({"root": str(repo), "dry_run": True})
    assert _tree(repo) == before_tree
    assert _git_state(repo) == before_git
    assert verdict["created"], "dry run must report what it would create"
    assert all(i["detail"].startswith("dry-run, not written:") or "would" in i["detail"] for i in verdict["created"])
    assert not (repo / "coordinator.local.md").exists()
    assert not (repo / "CLAUDE.md").exists()


def test_dry_run_false_and_absent_still_write(repo) -> None:
    verdict = mod.handle({"root": str(repo), "dry_run": False})
    assert (repo / "coordinator.local.md").is_file()
    assert (repo / "CLAUDE.md").is_file()
    assert verdict["created"]
    assert not any(i["detail"].startswith("dry-run") for i in verdict["created"])


def test_absent_dry_run_defaults_to_writing(repo) -> None:
    mod.handle({"root": str(repo)})
    assert (repo / "coordinator.local.md").is_file()


@pytest.mark.parametrize("bad", ["true", 1, None, "false"])
def test_non_bool_dry_run_is_refused_before_any_write(repo, bad) -> None:
    before = _tree(repo)
    verdict = mod.handle({"root": str(repo), "dry_run": bad})
    assert verdict["created"] == []
    assert verdict["needs_attention"][0]["key"] == "dry_run"
    assert _tree(repo) == before


def test_unknown_param_is_refused_naming_key_and_accepted_keys_before_any_write(repo) -> None:
    before = _tree(repo)
    verdict = mod.handle({"root": str(repo), "dryrun": True})
    assert verdict["created"] == []
    item = verdict["needs_attention"][0]
    assert item["key"] == "dryrun"
    for accepted in ("root", "answers", "dry_run"):
        assert accepted in item["detail"]
    assert _tree(repo) == before


def test_unknown_param_lists_sorted_accepted_keys(repo) -> None:
    verdict = mod.handle({"root": str(repo), "dryrun": True})
    assert "answers, dry_run, root" in verdict["needs_attention"][0]["detail"]


def test_envelope_underscore_key_is_not_refused_and_dry_run_still_writes_nothing(repo) -> None:
    before = _tree(repo)
    verdict = mod.handle({"root": str(repo), "dry_run": True, "_origin_worktree": str(repo)})
    assert verdict["created"]
    assert _tree(repo) == before


def test_dispatch_refuses_unknown_key_and_dry_run_via_real_entry_point(repo) -> None:
    from coordinator_core import ipc

    before_tree, before_git = _tree(repo), _git_state(repo)
    refused = asyncio.run(ipc.dispatch_message(
        {"jsonrpc": "2.0", "id": 1, "method": "repo.setup", "params": {"root": str(repo), "dryrun": True}}
    ))
    assert "error" in refused
    assert "dryrun" in refused["error"]["message"]
    for accepted in ("answers", "dry_run", "root"):
        assert accepted in refused["error"]["message"]
    assert _tree(repo) == before_tree

    ok = asyncio.run(ipc.dispatch_message(
        {"jsonrpc": "2.0", "id": 2, "method": "repo.setup", "params": {"root": str(repo), "dry_run": True}}
    ))
    assert ok["result"]["created"]
    assert _tree(repo) == before_tree
    assert _git_state(repo) == before_git


def test_params_schema_is_strict() -> None:
    schema = mod._PARAMS_SCHEMA
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["root"]
    assert schema["properties"]["dry_run"] == {"type": "boolean"}

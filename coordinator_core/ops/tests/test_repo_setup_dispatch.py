"""
coordinator_core/ops/tests/test_repo_setup_dispatch.py -- "repo.setup" through ipc.dispatch_message.

Subject: the registered op reached by method name with only `root` (no handler call, no module
import in the test): registry, scope and authz wiring are all on the path.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.ops import repo_setup as mod

ENGINE_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
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
    monkeypatch.setattr(mod, "_claude_home_env", lambda: {"CLAUDE_CONFIG_DIR": str(claude_home)})
    monkeypatch.setattr("coordinator_core.machine_resolver.merged_flat_registry", lambda: {})
    ghi = mod._git_hook_install()
    assert ghi is not None
    monkeypatch.setattr(ghi, "_resolve_coord_bin", lambda bin_dir, script, **kw: str(ENGINE_BIN))


def _dispatch(params: dict, origin: Path) -> dict:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "repo.setup",
        "params": params,
        "_origin_worktree": str(origin),
    }
    return asyncio.run(ipc.dispatch_message(msg))


def test_repo_setup_is_a_classified_write_op() -> None:
    assert classify("repo.setup") is OpClass.MUTATING


def test_root_only_dispatch_returns_the_four_verdict_arrays(tmp_path, sandbox) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    d = _dispatch({"root": str(repo)}, origin=tmp_path)
    assert "error" not in d, d
    result = d["result"]
    for key in ("created", "already_existed", "open", "needs_attention"):
        assert isinstance(result[key], list), key
    assert result["created"], result
    assert (repo / "CLAUDE.md").is_file()


def test_bad_answers_key_dispatches_to_needs_attention_naming_the_key(tmp_path, sandbox) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    d = _dispatch({"root": str(repo), "answers": {"bogus_key": 1}}, origin=tmp_path)
    assert "error" not in d, d
    items = d["result"]["needs_attention"]
    assert items and items[0]["key"] == "bogus_key"
    assert d["result"]["created"] == []
    assert not (repo / "CLAUDE.md").exists()

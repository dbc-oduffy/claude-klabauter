"""G-EM receiver reroute and home sender id in `_memo_resolver` (C1)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_resolver import (
    reroute_owner,
    resolve_receiver_inbox,
    resolve_self_em_id,
)
from coordinator_core.ops.fleet.memo_check_addressee import compute_check_addressee_candidate

GEM_IDS = ("group-em-em", "  GROUP-EM-EM ")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    (settings / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


def _registry(env: Path, repos: dict[str, Path], extra: str = "") -> None:
    lines = [f'"repos.{k}" = {json.dumps(str(v))}' for k, v in repos.items()]
    (env / "settings" / "machine-local" / "registry.local.toml").write_text(
        "\n".join(lines) + "\n" + extra, encoding="utf-8"
    )


def _central(env: Path, root: Path) -> None:
    schemas = root / "coordinator" / "schemas"
    schemas.mkdir(parents=True, exist_ok=True)
    (schemas / "coordinator-registry.manifest.json").write_text(
        json.dumps({"identity": {"repoAliases": [], "redirectAliases": [],
                                 "centralReceiverIds": ["coordinator-content-repo-em"]}}),
        encoding="utf-8",
    )
    (env / "settings" / "machine-local" / ".coordinator-content-root").write_text(
        str(root), encoding="utf-8"
    )


def _home(env: Path, name: str = "gem-box") -> Path:
    home = env / name
    home.mkdir()
    (home / ".git").mkdir()
    reg = env / "settings" / "machine-local" / "registry.local.toml"
    prior = reg.read_text(encoding="utf-8") if reg.exists() else ""
    reg.write_text(prior + f'"coordinator.group_em_home" = {json.dumps(str(home))}\n', encoding="utf-8")
    return home


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


def test_gem_ids_and_home_path_reroute_to_central(env, monkeypatch):
    doe = env / "doe"
    (doe / "state" / "cross-repo").mkdir(parents=True)
    _registry(env, {"content_root": doe})
    _central(env, doe)
    home = _home(env)
    _no_spawn(monkeypatch)
    baseline = resolve_receiver_inbox("coordinator-content-repo-em")
    assert baseline[0] == doe / "state" / "cross-repo" / "inbox"
    for rid in (*GEM_IDS, str(home)):
        got = resolve_receiver_inbox(rid)
        assert got[:2] == baseline[:2], rid
    assert reroute_owner("group-em-em") == "coordinator-content-repo-em"


def test_no_central_reroutes_to_claude_config_dir(env, monkeypatch):
    _registry(env, {})
    home = _home(env)
    cfg = env / "cfg"
    _no_spawn(monkeypatch)
    for rid in (*GEM_IDS, str(home)):
        inbox, root, _ = resolve_receiver_inbox(rid)
        assert root == cfg
        assert inbox == cfg / "state" / "cross-repo" / "inbox"
    assert reroute_owner("group-em-em") is None


def test_fallback_without_config_dir_env_uses_dot_claude_under_home(env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    _registry(env, {})
    fake_home = env / "home"
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    inbox, root, _ = resolve_receiver_inbox("group-em-em")
    assert root == fake_home / ".claude"
    assert inbox == fake_home / ".claude" / "state" / "cross-repo" / "inbox"


def test_legacy_claude_root_layout_is_honoured(env):
    _registry(env, {})
    (env / "cfg" / "cross-repo").mkdir(parents=True)
    inbox, root, _ = resolve_receiver_inbox("group-em-em")
    assert inbox == env / "cfg" / "cross-repo" / "inbox"


def test_ordinary_receivers_unchanged(env):
    repo = env / "other"
    repo.mkdir()
    _registry(env, {"other": repo})
    inbox, root, _ = resolve_receiver_inbox("other-em")
    assert root == repo
    assert resolve_receiver_inbox("coordinator-content-repo-em")[:2] == (None, None)


def test_check_addressee_matches_from_central_repo(env):
    doe = env / "doe"
    (doe / ".git").mkdir(parents=True)
    _registry(env, {"content_root": doe})
    _central(env, doe)
    assert compute_check_addressee_candidate(doe, "group-em-em")["verdict"] == "MATCH"


def test_resolve_self_em_id_home_signs_group_em(env):
    _registry(env, {})
    home = _home(env, "gem-box")
    assert resolve_self_em_id(home) == "group-em-em"
    other = env / "plain"
    other.mkdir()
    assert resolve_self_em_id(other) == "plain-em"


def test_registered_root_beats_home_rule(env):
    repo = env / "reg"
    repo.mkdir()
    _registry(env, {"reg": repo})
    assert resolve_self_em_id(repo) == "reg-em"

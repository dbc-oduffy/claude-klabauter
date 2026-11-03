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


def test_gem_ids_and_home_path_resolve_to_the_home_inbox(env, monkeypatch):
    doe = env / "doe"
    (doe / "state" / "cross-repo").mkdir(parents=True)
    _registry(env, {"content_root": doe})
    _central(env, doe)
    home = _home(env)
    _no_spawn(monkeypatch)
    assert resolve_receiver_inbox("coordinator-content-repo-em")[0] == doe / "state" / "cross-repo" / "inbox"
    for rid in (*GEM_IDS, str(home)):
        inbox, root, repos = resolve_receiver_inbox(rid)
        assert inbox == home / "state" / "cross-repo" / "inbox", rid
        assert root == home, rid
        assert "repos.doe_claude" in repos
    assert reroute_owner("group-em-em") is None


def test_no_central_still_resolves_to_the_home_inbox(env, monkeypatch):
    _registry(env, {})
    home = _home(env)
    _no_spawn(monkeypatch)
    for rid in (*GEM_IDS, str(home)):
        inbox, root, _ = resolve_receiver_inbox(rid)
        assert root == home
        assert inbox == home / "state" / "cross-repo" / "inbox"
    assert reroute_owner("group-em-em") is None


def test_default_home_under_settings_is_the_target(env):
    _registry(env, {})
    inbox, root, _ = resolve_receiver_inbox("group-em-em")
    assert root == env / "settings" / "group-em"
    assert inbox == root / "state" / "cross-repo" / "inbox"


def test_home_refused_inside_a_checkout_resolves_to_no_receiver(env):
    checkout = env / "checkout"
    (checkout / ".git").mkdir(parents=True)
    _registry(env, {}, f'"coordinator.group_em_home" = {json.dumps(str(checkout / "home"))}\n')
    inbox, root, repos = resolve_receiver_inbox("group-em-em")
    assert (inbox, root) == (None, None)
    assert repos == {}


def test_ordinary_receivers_unchanged(env):
    repo = env / "other"
    repo.mkdir()
    _registry(env, {"other": repo})
    inbox, root, _ = resolve_receiver_inbox("other-em")
    assert root == repo
    assert resolve_receiver_inbox("coordinator-content-repo-em")[:2] == (None, None)


def test_check_addressee_matches_from_the_home_only(env):
    doe = env / "doe"
    (doe / ".git").mkdir(parents=True)
    _registry(env, {"content_root": doe})
    _central(env, doe)
    home = _home(env)
    assert compute_check_addressee_candidate(home, "group-em-em")["verdict"] == "MATCH"
    assert compute_check_addressee_candidate(doe, "group-em-em")["verdict"] != "MATCH"


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


from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.machine_resolver import canonical_repo_key_for_root

try:
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    _DOE_ROOT = coordinator_doe_root()
except Exception:  # noqa: BLE001 — no DoE checkout is a skip, never a suite error
    _DOE_ROOT = None

_CANONICAL_KEY = "repos.claude_klabauter"
_ALIAS_KEY = "repos.example_orchestration_hub_repo"


def _registry(tmp_path: Path, declared: tuple[str, ...], live: dict[str, Path]) -> Path:
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    roster = "schema = 1\n" + "".join(f'"{key}" = ""\n' for key in declared)
    (machine_local / "registry.toml").write_text(roster, encoding="utf-8")
    body = "".join(
        f'"{key}" = "{str(path).replace(chr(92), chr(92) * 2)}"\n'
        for key, path in live.items()
    )
    (machine_local / "registry.local.toml").write_text(body, encoding="utf-8")
    return claude_home


@pytest.fixture
def collided_registry(tmp_path, monkeypatch):
    repo = tmp_path / "claude-klabauter"
    repo.mkdir()
    claude_home = _registry(
        tmp_path,
        declared=(_CANONICAL_KEY,),
        live={_ALIAS_KEY: repo, _CANONICAL_KEY: repo},
    )
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(machine_local))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    return repo


@pytest.mark.parametrize("alias_first", [True, False])
def test_declared_roster_key_wins_whatever_the_enumeration_order(
    collided_registry, alias_first
):
    repo = collided_registry
    keys = [_ALIAS_KEY, _CANONICAL_KEY] if alias_first else [_CANONICAL_KEY, _ALIAS_KEY]
    paths = {key: str(repo) for key in keys}
    assert canonical_repo_key_for_root(str(repo), paths) == _CANONICAL_KEY


def test_self_identity_resolves_to_the_canonical_em_id(collided_registry):
    from coordinator_core.ops.fleet import _memo_resolver

    importlib.reload(_memo_resolver)
    assert _memo_resolver.resolve_self_em_id(collided_registry) == "claude-klabauter-em"


def test_cli_sender_identity_resolves_to_the_canonical_em_id(
    collided_registry, monkeypatch
):
    if not _DOE_ROOT:
        pytest.skip("no DoE-claude checkout — coordinator_registry cannot import")
    # The fixture redirects CLAUDE_HOME, which is one rung of the ladder this
    monkeypatch.setenv("REPO_DOE_CLAUDE", _DOE_ROOT)
    lib_dir = str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import coordinator_registry

    repo = collided_registry
    paths = {key: str(repo) for key in sorted((_ALIAS_KEY, _CANONICAL_KEY))}
    assert coordinator_registry.em_id_for_root(str(repo), paths) == "claude-klabauter-em"


@pytest.mark.parametrize("reverse", [True, False])
def test_undeclared_collision_is_stable_rather_than_arbitrary(
    tmp_path, monkeypatch, reverse
):
    repo = tmp_path / "some-repo"
    repo.mkdir()
    keys = ("repos.zulu_name", "repos.alpha_name")
    claude_home = _registry(
        tmp_path, declared=(), live={key: repo for key in keys}
    )
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(machine_local))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    paths = {key: str(repo) for key in (reversed(keys) if reverse else keys)}
    assert canonical_repo_key_for_root(str(repo), paths) == "repos.alpha_name"


def test_alias_still_resolves_as_a_receiver(collided_registry):
    from coordinator_core.ops.fleet import _memo_resolver

    importlib.reload(_memo_resolver)
    inbox, repo_path, _ = _memo_resolver.resolve_receiver_inbox(
        "example-orchestration-hub-repo-em"
    )
    assert repo_path is not None
    assert os.path.samefile(str(repo_path), str(collided_registry))

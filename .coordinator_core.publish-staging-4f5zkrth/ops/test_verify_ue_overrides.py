"""Tests for coordinator_core.ops.verify_ue_overrides.

Port of: verify-ue-overrides.sh (DoE b5a4192c, 2026-07-20), DOE-PORT R1.

Converted 2026-08-16 (C7b): `_ml_get` now reads the machine-local registry
in-process (`machine_resolver.registry_get`), so scenarios that previously
drove a fake `machine-local` CLI stub (resolved via a monkeypatched
`vuo.shutil.which`) now seed a scratch registry FILE instead
(`MACHINE_LOCAL_REGISTRY_DIR` pointed at an empty `tmp_path` subdirectory,
per
`state/lessons/2026-07-17-redirect-state-home-env-to-tmp-in-unit-t-*.yaml`).
The `_resolve_ml_bin`-specific characterization tests (settings-home vs
legacy `~/.claude/bin` CLI-binary resolution ladder, and the "machine-local
not found" gate) are removed along with that now-deleted resolution ladder
-- registry_get needs no CLI binary at all.
"""
from __future__ import annotations

import json
import os

import pytest

from coordinator_core.ops import verify_ue_overrides as vuo


def _seed_registry(tmp_path, **pairs: str):
    """Write a scratch `registry.toml` under `tmp_path` and point
    `MACHINE_LOCAL_REGISTRY_DIR` at it -- replaces the old PATH-injected
    fake-CLI shape. Values are TOML-escaped via `json.dumps` (a superset of
    JSON's escaping) so a Windows path value's backslashes survive intact."""
    reg_dir = tmp_path / "ml-registry"
    reg_dir.mkdir(exist_ok=True)
    lines = "".join(f"{json.dumps(k)} = {json.dumps(v)}\n" for k, v in pairs.items())
    (reg_dir / "registry.toml").write_text(lines)
    return reg_dir


def _write_settings(path, enabled: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"enabledPlugins": enabled}, fh)


_FULLY_ENABLED = {
    "holodeck-control@claude-unreal-holodeck": True,
    "holodeck@claude-unreal-holodeck": True,
    "game-dev@claude-unreal-holodeck": True,
}


def _setup_success_tree(tmp_path):
    holodeck_dir = tmp_path / "holodeck-repo"
    project_rag = tmp_path / "project-rag-repo"
    home = tmp_path / "home"
    for d in (holodeck_dir, project_rag, home):
        d.mkdir(parents=True, exist_ok=True)
    _write_settings(str(holodeck_dir / ".claude" / "settings.json"), _FULLY_ENABLED)
    _write_settings(str(project_rag / ".claude" / "settings.json"), _FULLY_ENABLED)
    _write_settings(str(home / ".claude" / "settings.json"), _FULLY_ENABLED)
    return holodeck_dir, project_rag, home


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    """Scratch-scoped, always-empty-unless-seeded registry dir -- shields
    every test from the operator's REAL machine-local registry (C7b). A test
    that needs a specific key seeds it into this same directory via
    `_seed_registry(tmp_path, ...)`."""
    empty_registry = tmp_path / "ml-registry"
    empty_registry.mkdir(exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_registry))


def test_missing_required_key(monkeypatch, tmp_path, capsys):
    # No keys seeded -- registry is empty.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "machine-local key 'repos.claude_unreal_holodeck' not set" in err


def test_missing_directory_fails_loud(monkeypatch, tmp_path, capsys):
    _seed_registry(
        tmp_path,
        **{
            "repos.claude_unreal_holodeck": str(tmp_path / "does-not-exist"),
            "repos.project_rag": str(tmp_path / "also-missing"),
        },
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist on this machine" in err


def test_missing_settings_json(monkeypatch, tmp_path, capsys):
    holodeck_dir = tmp_path / "holodeck-repo"
    project_rag = tmp_path / "project-rag-repo"
    home = tmp_path / "home"
    for d in (holodeck_dir, project_rag, home):
        d.mkdir(parents=True, exist_ok=True)
    _seed_registry(
        tmp_path,
        **{"repos.claude_unreal_holodeck": str(holodeck_dir), "repos.project_rag": str(project_rag)},
    )
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "MISSING:" in err
    assert "claude-ue-bootstrap.sh" in err


def test_wrong_key_value(monkeypatch, tmp_path, capsys):
    holodeck_dir, project_rag, home = _setup_success_tree(tmp_path)
    _write_settings(
        str(holodeck_dir / ".claude" / "settings.json"),
        {"holodeck-control@claude-unreal-holodeck": False, "holodeck@claude-unreal-holodeck": True,
         "game-dev@claude-unreal-holodeck": True},
    )
    _seed_registry(
        tmp_path,
        **{"repos.claude_unreal_holodeck": str(holodeck_dir), "repos.project_rag": str(project_rag)},
    )
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "WRONG:" in err
    assert "holodeck-control@claude-unreal-holodeck] = false" in err


def test_no_game_dev_vendor_enabled(monkeypatch, tmp_path, capsys):
    holodeck_dir, project_rag, home = _setup_success_tree(tmp_path)
    _write_settings(
        str(holodeck_dir / ".claude" / "settings.json"),
        {"holodeck-control@claude-unreal-holodeck": True, "holodeck@claude-unreal-holodeck": True},
    )
    _seed_registry(
        tmp_path,
        **{"repos.claude_unreal_holodeck": str(holodeck_dir), "repos.project_rag": str(project_rag)},
    )
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no game-dev vendor enabled" in err


def test_dronesim_optional_when_unset(monkeypatch, tmp_path, capsys):
    holodeck_dir, project_rag, home = _setup_success_tree(tmp_path)
    _seed_registry(
        tmp_path,
        **{"repos.claude_unreal_holodeck": str(holodeck_dir), "repos.project_rag": str(project_rag)},
    )
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "all known UE-context dirs carry the expected override" in out


def test_success_all_expected(monkeypatch, tmp_path, capsys):
    holodeck_dir, project_rag, home = _setup_success_tree(tmp_path)
    dronesim_dir = tmp_path / "dronesim-repo"
    dronesim_dir.mkdir()
    _write_settings(str(dronesim_dir / ".claude" / "settings.json"), _FULLY_ENABLED)
    _seed_registry(
        tmp_path,
        **{
            "repos.claude_unreal_holodeck": str(holodeck_dir),
            "repos.project_rag": str(project_rag),
            "repos.dronesim": str(dronesim_dir),
        },
    )
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "all known UE-context dirs carry the expected override" in out

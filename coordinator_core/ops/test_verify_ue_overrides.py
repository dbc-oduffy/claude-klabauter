"""Tests for coordinator_core.ops.verify_ue_overrides.

Port of: verify-ue-overrides.sh (example-doctrine-repo b5a4192c, 2026-07-20), DOE-PORT R1.
"""
from __future__ import annotations

import json
import os
import stat
import textwrap

import pytest

from coordinator_core.ops import verify_ue_overrides as vuo
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _make_ml_bin(tmp_path, values: dict) -> str:
    """Write a fake `machine-local` script: `get <key>` prints values[key] and exits 0,
    or exits 1 (empty stdout) if the key is absent from `values`.

    Callers monkeypatch `vuo.shutil.which` to return this exact path, so the
    returned path must itself be directly invocable by `subprocess.run` -- see
    `coordinator_core.testing.fake_machine_local` for why a plain POSIX shebang
    path can't be exec'd on Windows and what `write_fake_executable` returns
    instead on that platform (a co-located `.cmd` launcher).
    """
    python_body = (
        "import sys\n"
        "if len(sys.argv) < 2 or sys.argv[1] != 'get':\n"
        "    sys.exit(2)\n"
        f"values = {values!r}\n"
        "key = sys.argv[2] if len(sys.argv) > 2 else None\n"
        "if key in values:\n"
        "    print(values[key])\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    ml_bin = write_fake_machine_local(tmp_path, python_body)
    return str(ml_bin)


def _write_settings(path, enabled: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"enabledPlugins": enabled}, fh)


_FULLY_ENABLED = {
    "example-game-repo-control@example-game-workbench-repo": True,
    "example-game-repo@example-game-workbench-repo": True,
    "game-dev@example-game-workbench-repo": True,
}


def _setup_success_tree(tmp_path):
    example-game-repo = tmp_path / "example-game-repo-repo"
    example_retrieval_repo = tmp_path / "example-retrieval-repo-repo"
    home = tmp_path / "home"
    for d in (example-game-repo, example_retrieval_repo, home):
        d.mkdir(parents=True, exist_ok=True)
    _write_settings(str(example-game-repo / ".claude" / "settings.json"), _FULLY_ENABLED)
    _write_settings(str(example_retrieval_repo / ".claude" / "settings.json"), _FULLY_ENABLED)
    _write_settings(str(home / ".claude" / "settings.json"), _FULLY_ENABLED)
    return example-game-repo, example_retrieval_repo, home


def _write_fake_ml_stub(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_resolve_ml_bin_prefers_settings_home_over_legacy(monkeypatch, tmp_path):
    """Settings-home (DR-072, canonical) must be tried before the legacy
    ~/.claude/bin rung, retired 2026-07-28 and kept only for machines that
    predate the move — see [[check_registry_codename_leak]]/
    [[verify_dist_publish_repo_sync]] sibling resolvers for the same ordering."""
    settings_ml = tmp_path / "settings" / "bin" / "machine-local"
    _write_fake_ml_stub(settings_ml)
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml_stub(legacy_ml)

    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert vuo._resolve_ml_bin(str(tmp_path)) == str(settings_ml)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_resolve_ml_bin_falls_back_to_legacy_when_settings_home_absent(monkeypatch, tmp_path):
    """Back-compat: a machine that predates the settings-home move must keep
    resolving via the legacy ~/.claude/bin rung."""
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml_stub(legacy_ml)

    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-here"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert vuo._resolve_ml_bin(str(tmp_path)) == str(legacy_ml)


def test_missing_ml_bin(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setenv("HOME", str(tmp_path / "nope"))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "machine-local not found on PATH" in err


def test_missing_required_key(monkeypatch, tmp_path, capsys):
    ml_bin = _make_ml_bin(tmp_path, {})  # no keys resolve
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "machine-local key 'repos.example_game_workbench_repo' not set" in err


def test_missing_directory_fails_loud(monkeypatch, tmp_path, capsys):
    ml_bin = _make_ml_bin(
        tmp_path,
        {
            "repos.example_game_workbench_repo": str(tmp_path / "does-not-exist"),
            "repos.example_retrieval_repo": str(tmp_path / "also-missing"),
        },
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist on this machine" in err


def test_missing_settings_json(monkeypatch, tmp_path, capsys):
    example-game-repo = tmp_path / "example-game-repo-repo"
    example_retrieval_repo = tmp_path / "example-retrieval-repo-repo"
    home = tmp_path / "home"
    for d in (example-game-repo, example_retrieval_repo, home):
        d.mkdir(parents=True, exist_ok=True)
    ml_bin = _make_ml_bin(
        tmp_path,
        {"repos.example_game_workbench_repo": str(example-game-repo), "repos.example_retrieval_repo": str(example_retrieval_repo)},
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "MISSING:" in err
    assert "claude-ue-bootstrap.sh" in err


def test_wrong_key_value(monkeypatch, tmp_path, capsys):
    example-game-repo, example_retrieval_repo, home = _setup_success_tree(tmp_path)
    _write_settings(
        str(example-game-repo / ".claude" / "settings.json"),
        {"example-game-repo-control@example-game-workbench-repo": False, "example-game-repo@example-game-workbench-repo": True,
         "game-dev@example-game-workbench-repo": True},
    )
    ml_bin = _make_ml_bin(
        tmp_path,
        {"repos.example_game_workbench_repo": str(example-game-repo), "repos.example_retrieval_repo": str(example_retrieval_repo)},
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "WRONG:" in err
    assert "example-game-repo-control@example-game-workbench-repo] = false" in err


def test_no_game_dev_vendor_enabled(monkeypatch, tmp_path, capsys):
    example-game-repo, example_retrieval_repo, home = _setup_success_tree(tmp_path)
    _write_settings(
        str(example-game-repo / ".claude" / "settings.json"),
        {"example-game-repo-control@example-game-workbench-repo": True, "example-game-repo@example-game-workbench-repo": True},
    )
    ml_bin = _make_ml_bin(
        tmp_path,
        {"repos.example_game_workbench_repo": str(example-game-repo), "repos.example_retrieval_repo": str(example_retrieval_repo)},
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no game-dev vendor enabled" in err


def test_example_sim_repo_optional_when_unset(monkeypatch, tmp_path, capsys):
    example-game-repo, example_retrieval_repo, home = _setup_success_tree(tmp_path)
    ml_bin = _make_ml_bin(
        tmp_path,
        {"repos.example_game_workbench_repo": str(example-game-repo), "repos.example_retrieval_repo": str(example_retrieval_repo)},
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "all known UE-context dirs carry the expected override" in out


def test_success_all_expected(monkeypatch, tmp_path, capsys):
    example-game-repo, example_retrieval_repo, home = _setup_success_tree(tmp_path)
    example-sim-repo = tmp_path / "example-sim-repo-repo"
    example-sim-repo.mkdir()
    _write_settings(str(example-sim-repo / ".claude" / "settings.json"), _FULLY_ENABLED)
    ml_bin = _make_ml_bin(
        tmp_path,
        {
            "repos.example_game_workbench_repo": str(example-game-repo),
            "repos.example_retrieval_repo": str(example_retrieval_repo),
            "repos.example-sim-repo": str(example-sim-repo),
        },
    )
    monkeypatch.setattr(vuo.shutil, "which", lambda *_a, **_k: ml_bin)
    monkeypatch.setenv("HOME", str(home))
    rc = vuo.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "all known UE-context dirs carry the expected override" in out

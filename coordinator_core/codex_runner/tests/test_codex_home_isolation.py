"""shape_codex_child gives a sandboxed parent's codex child a writable per-run CODEX_HOME."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.codex_runner import codex_spawn_env as cse


@pytest.fixture(autouse=True)
def _no_pwsh_check(monkeypatch):
    monkeypatch.setattr(cse, "check_pwsh_launchable", lambda env: None)


def _real_home(tmp_path: Path) -> Path:
    real = tmp_path / "real-codex"
    (real / "sessions").mkdir(parents=True)
    (real / "auth.json").write_text('{"k": 1}', encoding="utf-8")
    (real / "config.toml").write_text("m = 1", encoding="utf-8")
    (real / "sessions" / "s.jsonl").write_text("x", encoding="utf-8")
    return real


@pytest.mark.parametrize("flag", ["CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"])
def test_sandboxed_parent_gets_run_home_seeded_with_auth(tmp_path, flag):
    real = _real_home(tmp_path)
    run = tmp_path / "run"
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=run, environ={flag: "1", "CODEX_HOME": str(real)})
    home = Path(env["CODEX_HOME"])
    assert home.is_relative_to(run)
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"k": 1}'
    assert (home / "config.toml").is_file()
    assert not (home / "sessions").exists()


def test_writable_real_home_without_sandbox_is_left_alone(tmp_path):
    real = _real_home(tmp_path)
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    assert "CODEX_HOME" not in env
    assert not (tmp_path / "run" / "scratch" / "codex-home").exists()


def test_unwritable_real_home_gets_run_home(tmp_path, monkeypatch):
    real = _real_home(tmp_path)
    monkeypatch.setattr(cse, "_home_writable", lambda home: False)
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    assert (Path(env["CODEX_HOME"]) / "auth.json").is_file()

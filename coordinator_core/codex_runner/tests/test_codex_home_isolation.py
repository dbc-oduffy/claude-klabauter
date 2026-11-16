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


def test_writable_real_home_without_writable_root_is_left_alone(tmp_path):
    real = _real_home(tmp_path)
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=None, environ={"CODEX_HOME": str(real)})
    assert "CODEX_HOME" not in env


def test_worker_launch_on_writable_real_home_gets_home_without_em_agents_md(tmp_path):
    real = _real_home(tmp_path)
    (real / "hooks.json").write_text("{}", encoding="utf-8")
    (real / "AGENTS.md").write_text(
        "run `coordinator-invoke engine_ask.submit --params-file .coordinator-local/em-submit.json`\n",
        encoding="utf-8",
    )
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    home = Path(env["CODEX_HOME"])
    assert home.is_relative_to(tmp_path / "run")
    assert (home / "auth.json").is_file() and (home / "config.toml").is_file() and (home / "hooks.json").is_file()
    assert not (home / "AGENTS.md").exists()
    effective = "".join(p.read_text(encoding="utf-8") for p in home.iterdir() if p.is_file())
    assert "engine_ask.submit" not in effective and "em-submit.json" not in effective


def test_worker_home_drops_a_stale_agents_md(tmp_path):
    real = _real_home(tmp_path)
    stale = tmp_path / "run" / "scratch" / "codex-home"
    stale.mkdir(parents=True)
    (stale / "AGENTS.md").write_text("engine_ask.submit", encoding="utf-8")
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    assert not (Path(env["CODEX_HOME"]) / "AGENTS.md").exists()


def test_unwritable_real_home_gets_run_home(tmp_path, monkeypatch):
    real = _real_home(tmp_path)
    monkeypatch.setattr(cse, "_home_writable", lambda home: False)
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    assert (Path(env["CODEX_HOME"]) / "auth.json").is_file()


def test_worker_home_keeps_the_model_catalogue_for_effort_validation(tmp_path):
    real = _real_home(tmp_path)
    (real / "models_cache.json").write_text('{"models": []}', encoding="utf-8")
    env: dict[str, str] = {}
    cse.shape_codex_child(env, writable_root=tmp_path / "run", environ={"CODEX_HOME": str(real)})
    assert (Path(env["CODEX_HOME"]) / "models_cache.json").read_text(encoding="utf-8") == '{"models": []}'

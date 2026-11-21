"""Tests for `group_em.gem_home` -- the resolve-only port of DoE `_gem_home`."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.group_em import gem_home


pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    reg = tmp_path / "registry"
    settings.mkdir()
    reg.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for k in ("MACHINE_LOCAL_COORDINATOR_GROUP_EM_HOME", "MACHINE_LOCAL_COORDINATOR_GENERAL_WORK_HOME"):
        monkeypatch.delenv(k, raising=False)

    def write_registry(text: str) -> None:
        (reg / "registry.toml").write_text(text, encoding="utf-8")

    return settings, reg, write_registry


def _toml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


@pytest.fixture
def migrate_env(env, tmp_path, monkeypatch):
    settings, _, _ = env
    monkeypatch.setattr(gem_home.shutil, "which", lambda name: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(gem_home, "gem_holder", lambda: None)
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(session_registry, "read_rows", lambda *a, **k: [])
    return settings


def _git(cwd, *args):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, **no_console_creationflags())


def test_ignore_list_ignores_box_runtime_and_tracks_inbox(migrate_env):
    home = gem_home.ensure_home()
    for rel in (
        "state/group-em-watch.json",
        "state/group-em-watch-spool.jsonl",
        "state/group-em-watch-parked.json",
        "scratch/governor.log",
        "pm_turns.jsonl",
        "state/residue/inbox-blitz/x.md",
        "state/audits/a.md",
        "state/block-discharge/s.jsonl",
        ".coordinator-local/x",
    ):
        assert _git(home, "check-ignore", "-q", rel).returncode == 0, rel
    for rel in ("state/cross-repo/inbox/m.md", "state/handoffs/h.md", "CLAUDE.md"):
        assert _git(home, "check-ignore", "-q", rel).returncode == 1, rel


def test_ensure_report_without_gh_is_local_only(migrate_env):
    rep = gem_home.ensure_home_report()
    assert rep["remote"] is None and rep["remote_opt_out"] == "gh-absent"
    assert rep["seed"] == "no-template"
    assert rep["home"] == migrate_env / "group-em"
    assert gem_home.ensure_home_outcome() == (rep["home"], rep["seed"])
    assert _git(rep["home"], "remote").stdout.strip() == ""


def test_ensure_report_attaches_recorded_remote(migrate_env, env, tmp_path):
    _, _, write = env
    seed_repo = tmp_path / "seed"
    seed_repo.mkdir()
    _git(seed_repo, "init", "-q", "-b", "main")
    (seed_repo / "state").mkdir()
    (seed_repo / "state" / "note.md").write_text("from remote")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed_repo), str(bare))
    write(f'[coordinator]\ngroup_em_home_remote_url = "{_toml_path(bare)}"\n')
    rep = gem_home.ensure_home_report()
    assert rep["remote"] == _toml_path(bare) and rep["remote_opt_out"] is None
    assert (rep["home"] / "state" / "note.md").read_text() == "from remote"
    assert _git(rep["home"], "remote", "get-url", "origin").stdout.strip() == _toml_path(bare)
    assert str(bare) not in (rep["home"] / ".gitignore").read_text()
    again = gem_home.ensure_home_report()
    assert again["remote"] == rep["remote"]

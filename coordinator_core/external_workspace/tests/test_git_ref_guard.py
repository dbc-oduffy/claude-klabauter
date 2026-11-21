"""Pins the reference-transaction hook: ref writes in registered external workspaces refuse; fetch and the token pass."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core import machine_resolver as mr
from coordinator_core.external_workspace import git_ref_guard as g
from coordinator_core.external_workspace import vcs_guard_contract as c

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
_REAL_HOOK = Path(c.HOOK_DIR) / c.HOOK_NAME
_MARK = "external_workspace.rehydrate"


def _git(args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, creationflags=_NO_WINDOW
    )


class _Box:
    def __init__(self, root: Path):
        exe = Path(sys.executable).as_posix()
        if " " in exe:
            pytest.skip("interpreter path with a space cannot be a shebang")
        self.root = root
        self.settings = root / "settings"
        self.settings.mkdir()
        self.hooks = root / "hooks"
        self.hooks.mkdir()
        wrapper = self.hooks / c.HOOK_NAME
        wrapper.write_text(
            f"#!{exe}\nimport runpy\nrunpy.run_path({str(_REAL_HOOK)!r}, run_name='__main__')\n",
            encoding="utf-8",
            newline="\n",
        )
        wrapper.chmod(0o755)
        self.reg = root / "ml"
        self.reg.mkdir()
        self.global_cfg = root / "global.gitconfig"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(self.global_cfg),
            "GIT_CONFIG_NOSYSTEM": "1",
            "MACHINE_LOCAL_REGISTRY_DIR": str(self.reg),
        }
        self.env.pop(c.TOKEN_ENV, None)
        self.src = root / "src"
        self.src.mkdir()
        assert _git(["init", "-q"], cwd=self.src, env=self.env).returncode == 0
        r = _git([*_IDENT, "commit", "-q", "--allow-empty", "-m", "seed"], cwd=self.src, env=self.env)
        assert r.returncode == 0, r.stderr

    def register(self, ws: Path, registry_text: str | None = None) -> None:
        (self.reg / "registry.local.toml").write_text(
            registry_text
            if registry_text is not None
            else f'[external_workspaces.ws]\npath = "{ws.as_posix()}"\nvcs = "git"\n',
            encoding="utf-8",
        )
        (self.settings / c.INCLUDE_FILE_NAME).write_text(
            c.render_include_stanza([ws]), encoding="utf-8", newline="\n"
        )
        (self.settings / c.HOOKS_CONFIG_FILE_NAME).write_text(
            c.render_hooks_config(self.hooks), encoding="utf-8", newline="\n"
        )
        self.global_cfg.write_text(
            f"[include]\n\tpath = {c.to_git_posix(self.settings / c.INCLUDE_FILE_NAME)}\n",
            encoding="utf-8",
            newline="\n",
        )

    def token(self) -> dict:
        return {**self.env, c.TOKEN_ENV: "1"}

    def hydrated(self) -> Path:
        ws = self.root / "ws"
        self.register(ws)
        r = _git(["clone", "-q", str(self.src), str(ws)], env=self.token())
        assert r.returncode == 0, r.stderr
        return ws


@pytest.fixture
def box(tmp_path):
    return _Box(tmp_path)


def test_clone_into_missing_registered_path_refused_then_allowed_with_token(box):
    ws = box.root / "ws"
    box.register(ws)
    assert not ws.exists()
    refused = _git(["clone", "-q", str(box.src), str(ws)], env=box.env)
    assert refused.returncode != 0
    assert _MARK in refused.stderr
    assert _git(["clone", "-q", str(box.src), str(ws)], env=box.token()).returncode == 0


def test_commit_branch_tag_reset_refused_and_commit_allowed_with_token(box):
    ws = box.hydrated()
    for args in (
        [*_IDENT, "commit", "--allow-empty", "-m", "x"],
        ["branch", "feature"],
        [*_IDENT, "tag", "-a", "-m", "t", "v1"],
        ["update-ref", "refs/heads/zz", "HEAD"],
    ):
        r = _git(args, cwd=ws, env=box.env)
        assert r.returncode != 0, args
        assert _MARK in r.stderr, args
    assert _git([*_IDENT, "commit", "--allow-empty", "-m", "x"], cwd=ws, env=box.token()).returncode == 0


def test_fetch_passes_without_token(box):
    ws = box.hydrated()
    assert _git([*_IDENT, "commit", "-q", "--allow-empty", "-m", "more"], cwd=box.src, env=box.env).returncode == 0
    r = _git(["fetch", "-q", "origin"], cwd=ws, env=box.env)
    assert r.returncode == 0, r.stderr


def test_unreadable_registry_refuses_ref_write(box):
    ws = box.hydrated()
    (box.reg / "registry.local.toml").write_text("[broken = \n", encoding="utf-8")
    r = _git([*_IDENT, "commit", "--allow-empty", "-m", "x"], cwd=ws, env=box.env)
    assert r.returncode != 0
    assert "unreadable" in r.stderr


def test_decide_allows_non_prepared_remotes_only_and_unregistered(box, monkeypatch):
    ws = box.root / "ws"
    box.register(ws)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(box.reg))
    mr._external_ws_cache.clear()
    line = "0" * 40 + " " + "1" * 40 + " refs/heads/main\n"
    remote = "0" * 40 + " " + "1" * 40 + " refs/remotes/origin/main\n"
    inside = str(ws)
    assert g.decide("committed", line, inside, None, {}) is None
    assert g.decide("prepared", remote, inside, None, {}) is None
    assert g.decide("prepared", line, str(box.root / "other"), None, {}) is None
    assert g.decide("prepared", line, inside, None, {c.TOKEN_ENV: "1"}) is None
    assert _MARK in g.decide("prepared", line, inside, None, {})
    assert g.decide("prepared", line, str(box.root / "ws-extra"), None, {}) is None


def test_main_decision_process_time_under_200ms_in_process(box, monkeypatch):
    ws = box.root / "ws"
    box.register(ws)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(box.reg))
    monkeypatch.chdir(box.root)
    mr._external_ws_cache.clear()
    line = "0" * 40 + " " + "1" * 40 + " refs/heads/main\n"
    t0 = time.process_time()
    assert g.main(["hook", "prepared"], line) == 0
    assert time.process_time() - t0 < 0.2

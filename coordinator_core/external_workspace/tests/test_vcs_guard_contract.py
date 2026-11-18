"""Spike and pin: includeIf-scoped reference-transaction refusal of git writes (clone, commit)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.external_workspace import vcs_guard_contract as c

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
_REFUSAL = "EXTERNAL-WS-REFUSED"


def _git(args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, creationflags=_NO_WINDOW
    )


def _write_hook(hooks_dir: Path, shebang: str) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / c.HOOK_NAME
    hook.write_text(
        f"#!{shebang}\n"
        "import os, sys\n"
        "if sys.argv[1] != 'prepared':\n"
        "    sys.exit(0)\n"
        f"if os.environ.get({c.TOKEN_ENV!r}):\n"
        "    sys.exit(0)\n"
        f"sys.stderr.write({_REFUSAL!r} + '\\n')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


class _Box:
    def __init__(self, root: Path, shebang: str):
        self.root = root
        self.settings = root / "settings"
        self.settings.mkdir()
        self.hooks = root / "hooks"
        _write_hook(self.hooks, shebang)
        self.global_cfg = root / "global.gitconfig"
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(self.global_cfg),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.env.pop(c.TOKEN_ENV, None)
        self.src = root / "src"
        self.src.mkdir()
        assert _git(["init", "-q"], cwd=self.src, env=self.env).returncode == 0
        r = _git([*_IDENT, "commit", "-q", "--allow-empty", "-m", "seed"], cwd=self.src, env=self.env)
        assert r.returncode == 0, r.stderr

    def register(self, *workspaces: Path) -> None:
        (self.settings / c.INCLUDE_FILE_NAME).write_text(
            c.render_include_stanza(list(workspaces)), encoding="utf-8", newline="\n"
        )
        (self.settings / c.HOOKS_CONFIG_FILE_NAME).write_text(
            c.render_hooks_config(self.hooks), encoding="utf-8", newline="\n"
        )
        self.global_cfg.write_text(
            f"[include]\n\tpath = {c.to_git_posix(self.settings / c.INCLUDE_FILE_NAME)}\n",
            encoding="utf-8",
            newline="\n",
        )

    def with_token(self) -> dict:
        return {**self.env, c.TOKEN_ENV: "1"}


@pytest.fixture
def box(tmp_path):
    exe = Path(sys.executable).as_posix()
    if " " in exe:
        pytest.skip("interpreter path with a space cannot be a shebang")
    return _Box(tmp_path, exe)


def test_clone_into_missing_registered_path_is_refused_then_allowed_with_token(box):
    ws = box.root / "ws"
    box.register(ws)
    assert not ws.exists()

    refused = _git(["clone", "-q", str(box.src), str(ws)], env=box.env)
    assert refused.returncode != 0
    assert _REFUSAL in refused.stderr

    allowed = _git(["clone", "-q", str(box.src), str(ws)], env=box.with_token())
    assert allowed.returncode == 0, allowed.stderr
    assert (ws / ".git").exists()


def test_commit_inside_registered_workspace_is_refused_then_allowed_with_token(box):
    ws = box.root / "ws"
    box.register(ws)
    assert _git(["clone", "-q", str(box.src), str(ws)], env=box.with_token()).returncode == 0

    refused = _git([*_IDENT, "commit", "--allow-empty", "-m", "x"], cwd=ws, env=box.env)
    assert refused.returncode != 0
    assert _REFUSAL in refused.stderr

    allowed = _git([*_IDENT, "commit", "--allow-empty", "-m", "x"], cwd=ws, env=box.with_token())
    assert allowed.returncode == 0, allowed.stderr


def test_unregistered_path_pays_no_hook(box):
    ws = box.root / "ws"
    other = box.root / "other"
    box.register(ws)
    ok = _git(["clone", "-q", str(box.src), str(other)], env=box.env)
    assert ok.returncode == 0, ok.stderr
    assert _git([*_IDENT, "commit", "--allow-empty", "-m", "x"], cwd=other, env=box.env).returncode == 0


def test_sibling_prefix_path_is_not_scoped(box):
    ws = box.root / "ws"
    sibling = box.root / "ws-extra"
    box.register(ws)
    ok = _git(["clone", "-q", str(box.src), str(sibling)], env=box.env)
    assert ok.returncode == 0, ok.stderr


def test_missing_interpreter_fails_closed(tmp_path):
    box = _Box(tmp_path, "/nonexistent/interpreter-for-c0-spike")
    ws = box.root / "ws"
    box.register(ws)
    r = _git(["clone", "-q", str(box.src), str(ws)], env=box.env)
    assert r.returncode != 0
    assert not (ws / ".git").exists()


def test_env_python3_shebang_resolution_is_recorded(tmp_path, record_property):
    """Records which interpreter `#!/usr/bin/env python3` resolves to under git's hook shell.

    A resolution that cannot run Python (missing, Store stub) must refuse the write; one that
    runs must refuse it with the hook's own message. Never a silent pass.
    """
    box = _Box(tmp_path, "/usr/bin/env python3")
    ws = box.root / "ws"
    box.register(ws)
    r = _git(["clone", "-q", str(box.src), str(ws)], env=box.env)
    record_property("env_python3_stderr", r.stderr.strip()[:200])
    assert r.returncode != 0
    assert not (ws / ".git").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="drive-letter paths are Windows-only")
def test_drive_letter_path_scopes_the_hook(box):
    ws = box.root / "ws"
    assert str(ws)[1] == ":"
    box.register(ws)
    stanza = (box.settings / c.INCLUDE_FILE_NAME).read_text(encoding="utf-8")
    assert f'gitdir:{ws.as_posix()}/' in stanza
    r = _git(["clone", "-q", str(box.src), str(ws)], env=box.env)
    assert r.returncode != 0 and _REFUSAL in r.stderr


def test_render_include_stanza_shape():
    drive = "X" + ":"
    out = c.render_include_stanza(["/b/ws2", drive + "\\ws1\\", "/b/ws2"])
    assert out.count("[includeIf") == 2
    assert f'[includeIf "gitdir:{drive}/ws1/"]' in out
    assert '[includeIf "gitdir:/b/ws2/"]' in out
    assert f"path = {c.HOOKS_CONFIG_FILE_NAME}" in out
    assert c.render_include_stanza([]) == ""


def test_hooks_config_points_at_hook_dir():
    assert f"hooksPath = {c.to_git_posix(c.HOOK_DIR)}" in c.render_hooks_config()
    assert c.HOOK_DIR.name == "hooks"

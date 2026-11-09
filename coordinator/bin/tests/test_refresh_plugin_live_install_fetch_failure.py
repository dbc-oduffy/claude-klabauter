"""A failed fetch in refresh-plugin-live-install.py names git's last stderr line, the
origin URL and, for an ssh agent failure, an unlock-the-agent remediation (git-leg and
editable_sibling_venv)."""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).parent.parent
_ORIGIN_URL = "git@example.invalid:team/plugin.git"
_AGENT_STDERR = (
    "sign_and_send_pubkey: signing failed for ED25519 \"/x/key\" from agent: agent refused operation\n"
    "git@example.invalid: Permission denied (publickey).\n"
    "fatal: Could not read from remote repository.\n"
)
_PLAIN_STDERR = "fatal: unable to access 'https://example.invalid/x.git/': Could not resolve host\n"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install_fetch_failure_under_test",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _run(args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        **_mod._no_console_kwargs(),
    )


def _repo_with_origin(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["init"], path)
    _run(["config", "user.email", "t@example.com"], path)
    _run(["config", "user.name", "t"], path)
    (path / "seed.txt").write_text("seed")
    _run(["add", "-A"], path)
    _run(["commit", "-m", "seed"], path)
    _run(["remote", "add", "origin", _ORIGIN_URL], path)
    return path


def _failing_fetch(monkeypatch, stderr: str) -> None:
    real_git = _mod._git

    def _git(args, cwd):
        if args[:1] == ["fetch"]:
            return subprocess.CompletedProcess(args, 128, "", stderr)
        return real_git(args, cwd)

    monkeypatch.setattr(_mod, "_git", _git)


def _git_leg(tmp_path: Path, monkeypatch) -> int:
    real_exists = Path.exists

    def _fake_exists(self):
        if self.name == "check-plugin-drift.py":
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _fake_exists)
    plugins_dir = tmp_path / "plugins"
    live_path = _repo_with_origin(plugins_dir / "my-plugin")
    source_path = tmp_path / "source-checkout"
    source_path.mkdir()
    return _mod._handle_default(
        "my-plugin", str(source_path), str(live_path), "", "origin/main", "my-dist",
        False, False, plugins_dir, tmp_path / "snapshots", tmp_path / "refresh-log",
    )


def _sibling_venv(tmp_path: Path) -> int:
    source_path = _repo_with_origin(tmp_path / "addon-source")
    return _mod._handle_editable_sibling_venv(
        "my-addon", str(source_path), str(tmp_path / "live"), "origin/main", "my-dist",
        False, tmp_path / "refresh-log",
    )


@pytest.mark.parametrize("site", ["git_leg", "sibling_venv"])
def test_agent_refusal_names_stderr_line_url_and_unlock_first_remediation(
    site, tmp_path, monkeypatch, capsys
):
    _failing_fetch(monkeypatch, _AGENT_STDERR)
    rc = _git_leg(tmp_path, monkeypatch) if site == "git_leg" else _sibling_venv(tmp_path)

    err = capsys.readouterr().err
    assert rc == 1
    assert "fatal: Could not read from remote repository." in err
    assert _ORIGIN_URL in err
    assert "Unlock or approve your SSH agent" in err
    assert err.index("Unlock or approve") < err.index("IdentityFile")
    assert "IdentityAgent none" not in err
    assert re.search(r"(^|\s)/[a-z]", err.split("Unlock", 1)[1]) is None


@pytest.mark.parametrize("site", ["git_leg", "sibling_venv"])
def test_non_ssh_failure_names_stderr_line_and_url_without_ssh_remediation(
    site, tmp_path, monkeypatch, capsys
):
    _failing_fetch(monkeypatch, _PLAIN_STDERR)
    rc = _git_leg(tmp_path, monkeypatch) if site == "git_leg" else _sibling_venv(tmp_path)

    err = capsys.readouterr().err
    assert rc == 1
    assert "Could not resolve host" in err
    assert _ORIGIN_URL in err
    assert "SSH agent" not in err
    assert "ssh-add" not in err

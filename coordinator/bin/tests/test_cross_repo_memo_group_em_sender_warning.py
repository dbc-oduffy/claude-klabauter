"""The UNREGISTERED sender warning is silent for the group-em home and kept for other repos."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "cross_repo_memo_gem_sender_warning", str(_BIN_DIR / "cross-repo-memo.py")
    )
    spec = importlib.util.spec_from_loader("cross_repo_memo_gem_sender_warning", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


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
    return settings


def test_home_root_prints_no_warning(env, capsys, monkeypatch):
    home = env / "group-em"
    home.mkdir()
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(home))
    monkeypatch.setattr(mod, "_known_sender_paths", lambda: {})
    mod._warn_if_unregistered_sender()
    assert "UNREGISTERED" not in capsys.readouterr().err


def test_non_home_unregistered_still_warns(env, tmp_path, capsys, monkeypatch):
    other = tmp_path / "fresh-clone"
    other.mkdir()
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(other))
    monkeypatch.setattr(mod, "_known_sender_paths", lambda: {})
    mod._warn_if_unregistered_sender()
    assert "UNREGISTERED" in capsys.readouterr().err


def test_pr_comment_alternative_only_for_delivery_failures(env):
    mod = _load_cli_module()

    class _Refusal(RuntimeError):
        result = {"ok": False}

    assert mod._pr_comment_is_alternative(RuntimeError("transport down")) is True
    assert mod._pr_comment_is_alternative(_Refusal("receiver unknown")) is False
    assert mod._pr_comment_is_alternative(RuntimeError(f"x. {mod._PR_COMMENT_FALLBACK}")) is False

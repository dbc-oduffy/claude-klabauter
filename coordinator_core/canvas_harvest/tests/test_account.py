"""Pins the login resolver: CLAUDE_CONFIG_DIR injection, home fallback, None on bad input."""

from __future__ import annotations

import json

import pytest

from coordinator_core.canvas_harvest.account import current_login


def _write(dir_, payload):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / ".claude.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_two_logins_through_config_dirs(tmp_path, monkeypatch):
    _write(tmp_path / "a", {"oauthAccount": {"emailAddress": "a@x.com"}})
    _write(tmp_path / "b", {"oauthAccount": {"emailAddress": "b@x.com"}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    assert current_login() == "a@x.com"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "b"))
    assert current_login() == "b@x.com"


def test_home_fallback_when_unset(tmp_path, monkeypatch):
    _write(tmp_path, {"oauthAccount": {"emailAddress": "h@x.com"}})
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert current_login() == "h@x.com"


@pytest.mark.parametrize(
    "payload",
    [None, "{not json", "[]", {}, {"oauthAccount": None}, {"oauthAccount": {}},
     {"oauthAccount": {"emailAddress": ""}}, {"oauthAccount": {"emailAddress": 3}}],
)
def test_none_when_unresolved(tmp_path, monkeypatch, payload):
    if payload is not None:
        _write(tmp_path, payload)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert current_login() is None

from __future__ import annotations

import re

import pytest

from coordinator_core.guard_routes import keys


def test_route_key_shape():
    assert keys.route_key("check-blanket-git-add") == "gr:check-blanket-git-add"
    assert re.fullmatch(keys.ROUTE_KEY_PATTERN, keys.route_key("a1-b2"))


@pytest.mark.parametrize("bad", ["", "Has_Under", "UPPER", "-lead", "trail-", "a--b", "a b"])
def test_route_key_refuses_non_kebab(bad):
    with pytest.raises(ValueError):
        keys.route_key(bad)


def test_route_line_has_no_backtick_and_names_key_and_folder():
    line = keys.route_line("some-guard")
    assert "`" not in line
    assert "gr:some-guard" in line
    assert keys.ROUTES_DIR_DISPLAY in line


def test_reachability_false_on_absent_home(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert keys.routes_index_reachable() is False


def test_reachability_true_when_index_present(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    d = tmp_path / ".coordinator-claude-settings" / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
    d.mkdir(parents=True)
    (d / keys.ROUTES_INDEX).write_text("x", encoding="utf-8")
    assert keys.routes_index_reachable() is True


def test_reachability_probes_settings_home_override(tmp_path, monkeypatch):
    home = tmp_path / "elsewhere"
    d = home / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
    d.mkdir(parents=True)
    (d / keys.ROUTES_INDEX).write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nohome"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    assert keys.routes_index_reachable() is True
    (d / keys.ROUTES_INDEX).unlink()
    assert keys.routes_index_reachable() is False


def test_reachability_true_when_settings_home_override_equals_displayed_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    home = tmp_path / ".coordinator-claude-settings"
    d = home / "coordinator-claude" / "docs" / "wiki" / "guard-routes"
    d.mkdir(parents=True)
    (d / keys.ROUTES_INDEX).write_text("x", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    assert keys.routes_index_reachable() is True


def test_reachability_false_on_exception(monkeypatch):
    def boom(self):
        raise RuntimeError("no home")

    monkeypatch.setattr(keys.Path, "expanduser", boom)
    assert keys.routes_index_reachable() is False

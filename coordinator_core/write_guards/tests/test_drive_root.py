"""Tests for the shared drive-root / home new-top-level-entry predicate."""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import _write_bump_sink_shapes as sinks
from coordinator_core.write_guards import _drive_root as dr


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (root / "existing").mkdir()
    (home / "dotdir").mkdir()
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    return root, home


def test_new_top_level_under_root_returns_name(world):
    root, _ = world
    assert dr.new_top_level_entry(str(root / "fresh" / "f.txt"), None) == "fresh"
    assert dr.new_top_level_entry(str(root / "fresh"), None) == "fresh"


def test_existing_nested_and_root_itself_allowed(world):
    root, _ = world
    assert dr.new_top_level_entry(str(root / "existing" / "a" / "b.txt"), None) is None
    assert dr.new_top_level_entry(str(root / "existing"), None) is None
    assert dr.new_top_level_entry(str(root), None) is None


def test_placed_checkout_is_existing_entry(world):
    root, _ = world
    (root / "placed" / ".git").mkdir(parents=True)
    assert dr.new_top_level_entry(str(root / "placed" / "x" / "f"), None) is None
    assert dr.is_checkout_or_link(str(root / "placed"))
    assert not dr.is_checkout_or_link(str(root / "existing"))


def test_home_depth_two_new_first_component(world):
    _, home = world
    assert dr.new_top_level_entry(str(home / "newdir" / "f.txt"), None) == "newdir"
    assert dr.new_top_level_entry(str(home / "dotdir" / "f.txt"), None) is None


def test_home_depth_one_left_to_home_root_pair(world):
    _, home = world
    assert dr.new_top_level_entry(str(home / "f.txt"), None) is None


def test_relative_target_with_cwd_at_root(world):
    root, _ = world
    assert dr.new_top_level_entry("fresh", str(root)) == "fresh"
    assert dr.new_top_level_entry("existing/x", str(root)) is None
    assert dr.new_top_level_entry("fresh", "") is None


def test_outside_configured_roots_allowed(world, tmp_path):
    assert dr.new_top_level_entry(str(tmp_path / "elsewhere" / "f"), None) is None


def test_payload_env_pins_roots(world, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    env = {"COORDINATOR_DRIVE_ROOTS": str(other)}
    assert dr.new_top_level_entry(str(other / "n" / "f"), None, env=env) == "n"


def test_windows_bare_slash_rerooted_on_cwd_drive(monkeypatch):
    monkeypatch.setattr(sinks, "_host_is_windows", lambda: True)
    # `configured_drive_roots` splits on `os.pathsep`; a POSIX ":" would cut "Z:\\" into "Z" and "\\".
    monkeypatch.setattr(os, "pathsep", ";")
    z, y, q = ("%s:\\" % c for c in "ZYQ")  # abs-path-ok: synthetic nonexistent drives
    env = {"COORDINATOR_DRIVE_ROOTS": z, "HOME": q + "nohome", "USERPROFILE": q + "nohome"}
    kw = {"bare_slash_is_drive_root": True, "env": env}
    assert dr.new_top_level_entry("/newdir/f", z + "work", **kw) == "newdir"
    assert dr.new_top_level_entry("/newdir/f", y + "work", **kw) is None
    assert dr.new_top_level_entry("/z/newdir/f", y + "work", **kw) == "newdir"
    assert dr.new_top_level_entry("//server/share/x", z + "work", **kw) is None
    assert dr.new_top_level_entry("/newdir/f", z + "work", env=env) is None


def test_override_active_is_caller_keyed(monkeypatch):
    monkeypatch.delenv(dr.OVERRIDE_KEY, raising=False)
    assert dr.override_active({"env": {dr.OVERRIDE_KEY: "1"}})
    monkeypatch.setenv(dr.OVERRIDE_KEY, "1")
    assert not dr.override_active({"env": {}})
    assert dr.override_active({})
    assert dr.override_active(None)
    monkeypatch.delenv(dr.OVERRIDE_KEY)
    assert not dr.override_active({"env": {dr.OVERRIDE_KEY: "0"}})


def test_configured_drive_roots(monkeypatch):
    assert dr.configured_drive_roots({}) == ()
    joined = os.pathsep.join(["a", "", "b"])
    assert dr.configured_drive_roots({"COORDINATOR_DRIVE_ROOTS": joined}) == ("a", "b")


def test_deny_reason_names_scratch_and_leaks_nothing():
    text = dr.deny_reason("fresh")
    assert "<repo>/scratch/" in text
    assert "fresh" in text
    assert "COORDINATOR_" not in text
    assert not any(tok.startswith((os.sep, "/")) and len(tok) > 1 for tok in text.split())


def test_scope_equals_enforcement(world):
    root, home = world
    in_scope = [
        str(root / "n1"),
        str(root / "n2" / "deep" / "f.txt"),
        str(home / "n3" / "f.txt"),
    ]
    out_of_scope = [
        str(root / "existing" / "deep" / "f.txt"),
        str(root / "existing"),
        str(root),
        str(home / "dotdir" / "x"),
        str(home / "top.txt"),
        "",
    ]
    assert all(dr.new_top_level_entry(t, None) for t in in_scope)
    assert not any(dr.new_top_level_entry(t, None) for t in out_of_scope)

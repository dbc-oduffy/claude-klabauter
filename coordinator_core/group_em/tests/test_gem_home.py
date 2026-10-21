"""Tests for `group_em.gem_home` -- the resolve-only port of DoE `_gem_home`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.group_em import gem_home


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


def test_default_home(env):
    settings, _, _ = env
    assert gem_home.resolve_home() == settings / "group-em"


def test_configured_key_wins(env, tmp_path):
    _, _, write = env
    home = tmp_path / "custom-home"
    write(f'[coordinator]\ngroup_em_home = "{_toml_path(home)}"\ngeneral_work_home = "{_toml_path(tmp_path / "old")}"\n')
    assert gem_home.resolve_home() == home


def test_legacy_key_fallback(env, tmp_path):
    _, _, write = env
    old = tmp_path / "old-home"
    write(f'[coordinator]\ngeneral_work_home = "{_toml_path(old)}"\n')
    assert gem_home.resolve_home() == old


def test_empty_string_key_is_unset(env):
    settings, _, write = env
    write('[coordinator]\ngroup_em_home = ""\n')
    assert gem_home.resolve_home() == settings / "group-em"


def test_legacy_dirname_only_when_default_absent(env):
    settings, _, _ = env
    (settings / "general-work").mkdir()
    assert gem_home.resolve_home() == settings / "general-work"
    (settings / "group-em").mkdir()
    assert gem_home.resolve_home() == settings / "group-em"


def test_home_under_git_ancestor_refused(env, tmp_path, monkeypatch):
    _, _, write = env
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    home = checkout / "sub" / "home"
    write(f'[coordinator]\ngroup_em_home = "{_toml_path(home)}"\n')
    with pytest.raises(gem_home.HomeRefused) as ei:
        gem_home.resolve_home()
    assert Path(ei.value.checkout).resolve() == checkout.resolve()
    assert gem_home.is_home(home) is False


def test_home_equal_to_repos_root_refused(env, tmp_path):
    _, _, write = env
    root = tmp_path / "some-repo"
    root.mkdir()
    write(
        f'[repos]\nsome = "{_toml_path(root)}"\n'
        f'[coordinator]\ngroup_em_home = "{_toml_path(root)}"\n'
    )
    with pytest.raises(gem_home.HomeRefused):
        gem_home.resolve_home()


def test_home_under_repos_root_refused(env, tmp_path):
    _, _, write = env
    root = tmp_path / "some-repo"
    write(
        f'[repos]\nsome = "{_toml_path(root)}"\n'
        f'[coordinator]\ngroup_em_home = "{_toml_path(root / "inner")}"\n'
    )
    with pytest.raises(gem_home.HomeRefused):
        gem_home.resolve_home()


def test_contains_home_child_and_sibling_prefix(tmp_path):
    home = tmp_path / "home"
    assert gem_home.contains(home, home)
    assert gem_home.contains(home, home / "a" / "b")
    assert not gem_home.contains(home, tmp_path / "home-2")
    assert not gem_home.contains(home, tmp_path)


def test_is_home(env):
    settings, _, _ = env
    assert gem_home.is_home(settings / "group-em" / "x")
    assert not gem_home.is_home(settings / "elsewhere")


def test_is_group_em_receiver_case_and_whitespace_insensitive():
    assert gem_home.is_group_em_receiver("group-em-em")
    assert gem_home.is_group_em_receiver("  GENERAL-WORK-EM ")
    assert not gem_home.is_group_em_receiver("coordinator-content-repo-em")
    assert gem_home.SENDER_ID in gem_home.RECEIVER_IDS


def test_home_inbox_dirs_both_layouts(tmp_path):
    assert gem_home.home_inbox_dirs(tmp_path) == (
        tmp_path / "state" / "cross-repo" / "inbox",
        tmp_path / "cross-repo" / "inbox",
    )


def test_alternate_spellings_of_one_home_both_resolve(env):
    settings, _, _ = env
    home = settings / "group-em"
    home.mkdir()
    posix = home.as_posix() + "/child"
    native = str(settings / "other" / ".." / "group-em" / "child")
    assert gem_home.is_home(posix)
    assert gem_home.is_home(native)
    if sys.platform == "win32":
        assert gem_home.is_home(str(home).replace("\\", "/"))
        assert gem_home.is_home(str(home).upper())


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths are Windows-only")
def test_case_differing_path_contained_on_windows(tmp_path):
    home = tmp_path / "Home"
    assert gem_home.contains(home, str(home).swapcase() + "\\child")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths are case-sensitive")
def test_case_differing_path_not_contained_on_posix(tmp_path):
    home = tmp_path / "Home"
    assert not gem_home.contains(home, tmp_path / "home" / "child")

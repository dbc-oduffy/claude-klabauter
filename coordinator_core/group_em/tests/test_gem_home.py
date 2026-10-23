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
    assert gem_home.is_group_em_receiver("  GROUP-EM-EM ")
    assert not gem_home.is_group_em_receiver("  GENERAL-WORK-EM ")
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


@pytest.fixture
def migrate_env(env, tmp_path, monkeypatch):
    settings, _, _ = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(gem_home, "gem_holder", lambda: None)
    from coordinator_core.group_em import session_registry

    monkeypatch.setattr(session_registry, "read_rows", lambda *a, **k: [])
    return settings


def test_migrate_no_legacy(migrate_env):
    assert gem_home.migrate_legacy_home() == "no-legacy"


def test_migrate_moves_legacy(migrate_env):
    (migrate_env / "general-work").mkdir()
    (migrate_env / "general-work" / "keep.txt").write_text("x")
    assert gem_home.migrate_legacy_home() == "moved"
    assert not (migrate_env / "general-work").exists()
    assert (migrate_env / "group-em" / "keep.txt").read_text() == "x"


def test_migrate_both_present_untouched(migrate_env):
    (migrate_env / "general-work").mkdir()
    (migrate_env / "group-em").mkdir()
    assert gem_home.migrate_legacy_home() == "both-present"
    assert (migrate_env / "general-work").exists()


def test_migrate_configured_untouched(env, tmp_path):
    settings, _, write = env
    (settings / "general-work").mkdir()
    write(f'[coordinator]\ngroup_em_home = "{_toml_path(tmp_path / "cfg")}"\n')
    assert gem_home.migrate_legacy_home() == "configured"
    assert (settings / "general-work").exists()


def test_migrate_refused_inside_checkout(migrate_env, tmp_path, monkeypatch):
    (migrate_env / "general-work").mkdir()
    monkeypatch.setattr(gem_home, "inside_checkout", lambda p: str(tmp_path))
    assert gem_home.migrate_legacy_home() == "refused"
    assert (migrate_env / "general-work").exists()


def test_migrate_live_when_cwd_inside_legacy(migrate_env, monkeypatch):
    legacy = migrate_env / "general-work"
    legacy.mkdir()
    monkeypatch.chdir(legacy)
    assert gem_home.migrate_legacy_home() == "live"
    assert legacy.exists()


def test_migrate_live_when_holder_root_inside_legacy(migrate_env, monkeypatch):
    legacy = migrate_env / "general-work"
    legacy.mkdir()
    monkeypatch.setattr(gem_home, "gem_holder", lambda: {"live": True, "repo_root": str(legacy)})
    assert gem_home.migrate_legacy_home() == "live"


def test_migrate_live_when_registry_row_alive(migrate_env, monkeypatch):
    from types import SimpleNamespace

    from coordinator_core.group_em import session_registry

    legacy = migrate_env / "general-work"
    legacy.mkdir()
    monkeypatch.setattr(
        session_registry, "read_rows", lambda *a, **k: [SimpleNamespace(cwd=str(legacy), pid=1)]
    )
    monkeypatch.setattr(session_registry, "pid_alive", lambda pid: True)
    assert gem_home.migrate_legacy_home() == "live"


def test_migrate_live_fails_closed_on_error(migrate_env, monkeypatch):
    (migrate_env / "general-work").mkdir()

    def boom():
        raise RuntimeError

    monkeypatch.setattr(gem_home, "gem_holder", boom)
    assert gem_home.migrate_legacy_home() == "live"


def test_migrate_busy_on_oserror(migrate_env, monkeypatch):
    (migrate_env / "general-work").mkdir()

    def deny(*a, **k):
        raise PermissionError

    monkeypatch.setattr(gem_home.os, "rename", deny)
    assert gem_home.migrate_legacy_home() == "busy"


def test_ensure_home_seeds_and_is_idempotent(migrate_env, tmp_path):
    tpl = tmp_path / "tpl.md"
    tpl.write_text("seed v1", encoding="utf-8")
    home = gem_home.ensure_home(template=tpl)
    assert home == migrate_env / "group-em"
    assert (home / ".git").is_dir() and (home / "state").is_dir()
    assert (home / "CLAUDE.md").read_text() == "seed v1"
    assert (home / ".gitignore").read_text().splitlines() == [".coordinator-local/"]
    (home / "CLAUDE.md").write_text("local edit")
    tpl.write_text("seed v2", encoding="utf-8")
    gem_home.ensure_home(template=tpl)
    assert (home / "CLAUDE.md").read_text() == "local edit"
    assert (home / ".gitignore").read_text().splitlines() == [".coordinator-local/"]
    gem_home.ensure_home(template=tpl, refresh=True)
    assert (home / "CLAUDE.md").read_text() == "seed v2"


def test_ensure_home_missing_template_skips_seed(migrate_env, tmp_path):
    home = gem_home.ensure_home(template=tmp_path / "absent.md")
    assert not (home / "CLAUDE.md").exists()


def test_default_template_resolves_from_mirror_shaped_content_root(migrate_env, env, tmp_path):
    _, _, write = env
    doe = tmp_path / "doe"
    tpl = doe / "coordinator" / "templates" / "group-em-home" / "home-CLAUDE.md"
    tpl.parent.mkdir(parents=True)
    tpl.write_text("doe seed", encoding="utf-8")
    write(f'[repos]\ncontent_root = "{_toml_path(doe)}"\n')
    assert gem_home.default_template() == tpl
    home, outcome = gem_home.ensure_home_outcome()
    assert outcome == "seeded"
    assert (home / "CLAUDE.md").read_text() == "doe seed"
    assert gem_home.ensure_home_outcome()[1] == "kept"


def test_default_template_none_reports_no_template(migrate_env, env, tmp_path, monkeypatch):
    _, _, write = env
    empty = tmp_path / "empty-root"
    empty.mkdir()
    write(f'[repos]\ncontent_root = "{_toml_path(empty)}"\n')
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert gem_home.default_template() is None
    home, outcome = gem_home.ensure_home_outcome()
    assert outcome == "no-template"
    assert not (home / "CLAUDE.md").exists()


def test_ensure_home_migrates_legacy_first(migrate_env, tmp_path):
    (migrate_env / "general-work").mkdir()
    home = gem_home.ensure_home(template=tmp_path / "absent.md")
    assert home == migrate_env / "group-em"
    assert not (migrate_env / "general-work").exists()


def test_ensure_home_refuses_checkout(env, tmp_path):
    _, _, write = env
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    write(f'[coordinator]\ngroup_em_home = "{_toml_path(checkout / "h")}"\n')
    with pytest.raises(gem_home.HomeRefused):
        gem_home.ensure_home()
    assert not (checkout / "h").exists()

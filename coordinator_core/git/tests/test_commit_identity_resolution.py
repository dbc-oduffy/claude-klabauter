"""`commit._identity` -- which name/email this route stamps, and at what cost.

WHY THIS FILE EXISTS. `commit_paths` writes the commit object itself, so it
stamps whatever `_identity` resolves; it never inherits the identity a plain
`git commit` picks up from `user.name`/`user.email`. `_identity` used to read
only the `GIT_*` env vars, so in any environment that configures identity the
ordinary way -- git config, no env vars -- EVERY commit this route made was
stamped `coordinator <coordinator@local>`, which GitHub renders Unverified.
Measured 2026-09-17: a dispatched wave's commit landed unverified beside the
EM's own verified commits on the same branch, differing only in which route
wrote them.

THE TRAP THIS FILE IS REALLY GUARDING. `test_commit_zero_spawn.py` pins
`assert spawns == []` on this path, and it sets `GIT_COMMITTER_*`. So it never
reaches the config rung, and a first fix that asked `git config --get-regexp`
kept it green while adding a spawn to the commit path in exactly the
environment cloud sessions run. Worse, a `subprocess.run` spy reported zero
spawns, because `run_git` hand-rolls `Popen` -- the wrong instrument gave the
comfortable answer. Every spawn assertion here therefore clears the env vars
FIRST and spies `Popen`.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.git import commit as commit_mod

_ENV_KEYS = (
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
)


@pytest.fixture
def no_env_identity(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    yield
    commit_mod._CONFIG_IDENTITY_MEMO.clear()


@pytest.fixture
def isolated_config(tmp_path, monkeypatch, no_env_identity):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _write(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _popen_spy(monkeypatch):
    seen: "list[list[str]]" = []
    real = subprocess.Popen

    class Spy(real):  # type: ignore[misc,valid-type]
        def __init__(self, cmd, *a, **kw):
            seen.append(cmd if isinstance(cmd, str) else list(cmd)[:4])
            super().__init__(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return seen


def test_identity_resolves_with_zero_spawns_when_env_is_absent(
    isolated_config, monkeypatch
):
    _write(isolated_config / "global-gitconfig", "[user]\n\tname = Cfg\n\temail = c@x\n")
    seen = _popen_spy(monkeypatch)

    assert commit_mod._identity(None) == ("Cfg", "c@x")
    assert seen == [], f"identity resolution spawned {seen}"


def test_identity_is_memoized_across_repeated_resolves(isolated_config):
    _write(isolated_config / "global-gitconfig", "[user]\n\temail = memo@x\n")
    first = commit_mod._config_identity(None)
    for _ in range(5):
        assert commit_mod._config_identity(None) == first
    assert len(commit_mod._CONFIG_IDENTITY_MEMO) == 1


def test_env_outranks_config(isolated_config, monkeypatch):
    _write(isolated_config / "global-gitconfig", "[user]\n\tname = Cfg\n\temail = c@x\n")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "EnvName")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "env@x")
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    assert commit_mod._identity(None) == ("EnvName", "env@x")


def test_repo_local_config_outranks_global(isolated_config):
    _write(isolated_config / "global-gitconfig", "[user]\n\tname = Global\n\temail = g@x\n")
    repo = isolated_config / "repo"
    _write(repo / ".git" / "config", "[user]\n\temail = local@x\n")
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    assert commit_mod._identity(repo) == ("Global", "local@x")


def test_the_two_fields_resolve_independently(isolated_config, monkeypatch):
    _write(isolated_config / "global-gitconfig", "[user]\n\temail = only@x\n")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "FromEnv")
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    assert commit_mod._identity(None) == ("FromEnv", "only@x")


def test_synthetic_fallback_survives_a_world_with_no_identity(isolated_config):
    assert commit_mod._identity(None) == ("coordinator", "coordinator@local")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("[user]\n\tname = Plain\n\temail = p@x\n", ("Plain", "p@x")),
        ('[user]\n\tname = "Quoted Name"\n', ("Quoted Name", None)),
        ("[USER]\n\tName = Upper\n", ("Upper", None)),
        ("[user]\nname=Tight\n", ("Tight", None)),
        ("[user]\n# name = Commented\n\tname = Real\n", ("Real", None)),
        ("[user]\n; email = Commented\n\temail = real@x\n", (None, "real@x")),
        ("[core]\n\tname = NotUser\n", (None, None)),
        ("[user]\n\tname = First\n\tname = Second\n", ("First", None)),
        ("[user \"sub\"]\n\tname = Subsection\n", ("Subsection", None)),
        ("[unclosed\n\tname = Ignored\n", (None, None)),
        ("[user]\n\tname =\n", (None, None)),
        ("", (None, None)),
    ],
)
def test_user_section_parsing(tmp_path, body, expected):
    path = tmp_path / "cfg"
    path.write_text(body, encoding="utf-8")
    assert commit_mod._user_section_identity(path) == expected


def test_a_later_section_ends_the_user_section(tmp_path):
    path = tmp_path / "cfg"
    path.write_text(
        "[user]\n\temail = u@x\n[core]\n\tname = CoreName\n", encoding="utf-8"
    )
    assert commit_mod._user_section_identity(path) == (None, "u@x")


def test_an_unreadable_config_is_none_not_an_exception(tmp_path):
    assert commit_mod._user_section_identity(tmp_path / "does-not-exist") == (None, None)
    assert commit_mod._user_section_identity(tmp_path) == (None, None)


def test_includes_are_not_followed_and_fall_through(isolated_config):
    included = isolated_config / "included-config"
    _write(included, "[user]\n\tname = Included\n\temail = inc@x\n")
    _write(
        isolated_config / "global-gitconfig",
        f"[include]\n\tpath = {included}\n",
    )
    commit_mod._CONFIG_IDENTITY_MEMO.clear()
    assert commit_mod._identity(None) == ("coordinator", "coordinator@local")

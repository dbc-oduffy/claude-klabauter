"""Spike and pin: includeIf-scoped reference-transaction refusal of git writes (clone, commit)."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.external_workspace import vcs_guard_contract as c

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
_REFUSAL = "EXTERNAL-WS-REFUSED"


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

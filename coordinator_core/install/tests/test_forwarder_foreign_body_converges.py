"""A default setup run converges a settings-home claude-author body that is another root's forwarder."""

from __future__ import annotations

import os

import pytest

from coordinator_core.install import maximalist
from coordinator_core.install.test_maximalist import stub_env  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX settings-home body contract")

_FOREIGN_BODY = (
    "#!/usr/bin/env python3\n"
    "from _resolve_claude_klabauter import exec_cli\n"
    'exec_cli("claude-author")\n'
)


def _standalone_plugin_clone(stub_env) -> str:  # noqa: F811
    plugin = stub_env["claude_home"].parent / "standalone-plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text("{}")
    return str(plugin)


def _run(stub_env, doe_clone: str, *, check_only: bool) -> int:  # noqa: F811
    return maximalist.run(
        check_only=check_only,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=doe_clone,
        claude_home_dir=str(stub_env["claude_home"]),
    )


def test_default_setup_on_standalone_plugin_clone_rewrites_foreign_body(stub_env) -> None:  # noqa: F811
    wrapper_src = stub_env["claude_klabauter_root"] / "coordinator" / "bin" / "claude-author.py"
    body = stub_env["settings_bin"] / "claude-author"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(_FOREIGN_BODY)

    assert _run(stub_env, _standalone_plugin_clone(stub_env), check_only=False) == 0
    assert body.read_bytes() == wrapper_src.read_bytes()
    assert os.access(body, os.X_OK)


def test_check_only_leaves_foreign_body_untouched(stub_env) -> None:  # noqa: F811
    body = stub_env["settings_bin"] / "claude-author"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text(_FOREIGN_BODY)

    assert _run(stub_env, _standalone_plugin_clone(stub_env), check_only=True) == 0
    assert body.read_text() == _FOREIGN_BODY


def test_default_setup_on_working_repo_clone_rewrites_foreign_body(stub_env) -> None:  # noqa: F811
    wrapper_src = stub_env["claude_klabauter_root"] / "coordinator" / "bin" / "claude-author.py"
    body = stub_env["settings_bin"] / "claude-author"

    assert _run(stub_env, str(stub_env["doe_clone"]), check_only=False) == 0
    assert body.read_bytes() == wrapper_src.read_bytes()

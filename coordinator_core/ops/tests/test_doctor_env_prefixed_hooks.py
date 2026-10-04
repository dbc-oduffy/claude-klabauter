"""Doctor parses env-prefixed hook commands (`VAR=val "<launcher>" ...`) without false BROKEN."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops import doctor

_ENV_CMD = (
    'COORDINATOR_DOOR_STDIN_MODE=hook '
    '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/hook-run" '
    "hooks.preuse_bash_dispatch"
)


def _doc(command: str) -> dict:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}}


@pytest.fixture
def hooks_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "content"
    (root / "coordinator" / "hooks").mkdir(parents=True)
    monkeypatch.setenv("MACHINE_LOCAL_REPOS_CONTENT_ROOT", str(root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg-empty"))
    return root


def test_env_prefix_is_stripped_from_argv():
    argv = doctor._hook_argv({"command": _ENV_CMD})
    assert argv[0].endswith("/bin/hook-run")
    assert doctor._is_launcher_argv(argv)


def test_env_prefixed_launcher_is_not_broken_in_either_layer(hooks_env: Path):
    (hooks_env / "coordinator" / "hooks" / "hooks.json").write_text(json.dumps(_doc(_ENV_CMD)))
    status, findings, present = doctor._check_one_hooks_doc(
        hooks_env / "coordinator" / "hooks" / "hooks.json", str(hooks_env), "hooks.json"
    )
    assert present and status == "ok", findings
    layer = doctor._check_hook_interpreter_resolvability()
    assert layer.status == "ok", layer.findings


def test_unresolvable_interpreter_behind_env_prefix_still_fails(hooks_env: Path, monkeypatch, tmp_path):
    (hooks_env / "coordinator" / "hooks" / "hooks.json").write_text(
        json.dumps(_doc("FOO=1 not-a-real-interpreter x.py"))
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    layer = doctor._check_hook_interpreter_resolvability()
    assert layer.status == "broken"
    assert "not-a-real-interpreter" in layer.findings[0].message


def test_shipped_hooks_json_has_no_false_broken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from coordinator_core.content_root import read_content_root

    root = read_content_root()
    content = doctor.content_root_for(root) if root else None
    shipped = None if content is None else content / "hooks" / "hooks.json"
    if shipped is None or not shipped.is_file():
        pytest.skip("shipped hooks.json not resolvable on this box")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg-empty"))
    status, findings, _ = doctor._check_one_hooks_doc(shipped, str(root), "hooks.json")
    launcher_findings = [f for f in findings if "not understood" in f.message]
    assert not launcher_findings, launcher_findings


def test_layer_names_carry_no_private_names():
    layer = doctor._check_sibling_resolution()
    for private in ("coordinator-content-repo", "claude-klabauter", "claude-klabauter"):
        assert private not in layer.name
        assert all(private not in f.message for f in layer.findings)

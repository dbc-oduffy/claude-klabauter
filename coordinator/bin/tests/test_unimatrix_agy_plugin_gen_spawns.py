"""Behavior pins for unimatrix-plugin-gen.py (table-driven plugin tree translation) and its installer wiring."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load("unimatrix_plugin_gen", "unimatrix-plugin-gen.py")
ui = _load("unimatrix_install_for_gen", "unimatrix-install.py")

_DOOR = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/hook-run"'
_PASS2 = "; rc=$?; [ $rc -eq 2 ] && rc=0; exit $rc"


def _door_cmd(name) -> str:
    return f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.{name}"


HOOKS = {
    "description": "top level string agy refuses",
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "description": "d", "command": _door_cmd("a")}]},
            {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": _door_cmd("b")}]},
            {"matcher": "Weird", "hooks": [{"type": "command", "command": _door_cmd("c")}]},
        ],
        "SessionStart": [{"hooks": [{"type": "command", "command": "python ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/run.py"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "z"}]}],
    },
}


def _agent(name, model) -> str:
    return f"---\r\nname: {name}\r\nmodel: {model}\r\ntools: [Read]\r\n---\r\nbody ${{CLAUDE_PLUGIN_ROOT}}/x\r\n"


@pytest.fixture
def plugin(tmp_path: Path):
    root = tmp_path / "src" / "plugin"
    for n, m in (("a-opus", "opus"), ("a-sonnet", "sonnet"), ("a-haiku", "haiku"), ("a-odd", "gpt-9")):
        (root / "agents").mkdir(parents=True, exist_ok=True)
        (root / "agents" / f"{n}.md").write_bytes(_agent(n, m).encode())
    (root / "commands" / "sub").mkdir(parents=True)
    (root / "commands" / "foo.md").write_bytes(b"---\r\ndescription: Foo\r\n---\r\nrun foo\r\n")
    (root / "commands" / "sub" / "bar.md").write_bytes(b"no frontmatter, no trailing newline")
    (root / "skills" / "real-dir").mkdir(parents=True)
    (root / "skills" / "real-dir" / "SKILL.md").write_bytes(b"---\nname: real-name\ndescription: R\n---\nbody\n")
    (root / "skills" / "real-dir" / "ref.txt").write_bytes(b"ref\r\n")
    (root / "skills" / "flat.md").write_bytes(b"flat skill\n")
    (root / "hooks" / "scripts").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps(HOOKS), encoding="utf-8")
    (root / "hooks" / "scripts" / "run.py").write_bytes(b"print(1)\r\n")
    (root / "extras").mkdir()
    (root / "extras" / "x.txt").write_bytes(b"x\n")
    (root.parent / "global-doctrine" / "rules").mkdir(parents=True)
    (root.parent / "global-doctrine" / "CLAUDE.md").write_bytes(b"# doctrine\r\n")
    (root.parent / "global-doctrine" / "rules" / "rules.md").write_bytes(b"- rule\n")
    return root


def _door_hooks(plugin, commands) -> None:
    groups = [{"matcher": "Bash", "hooks": [{"type": "command", "shell": "bash", "timeout": 5, "command": c}]}
              for c in commands]
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": groups}}), encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="door stand-in is a POSIX script; the mocked leg covers Windows")
def test_shim_runs_end_to_end_with_a_real_child_process(plugin, tmp_path: Path) -> None:
    _door_hooks(plugin, [f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a"])
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    exe = home / "bin" / "hook-run"
    exe.write_text('#!/bin/sh\ncat; echo "$COORDINATOR_DOOR_STDIN_MODE $*"; exit 2\n', encoding="utf-8")
    exe.chmod(0o755)
    r = subprocess.run([sys.executable, str(out / "hooks" / "agy-hook-run.py"), "--pass-2", "hooks.a"],
                       input="EVT", capture_output=True, text=True, env={"COORDINATOR_SETTINGS_HOME": str(home)})
    assert r.returncode == 0 and r.stdout == "EVThook hooks.a\n"


def _stub_home(tmp_path, body):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    exe = home / "bin" / "hook-run"
    exe.write_text(body, encoding="utf-8")
    exe.chmod(0o755)
    return home


@pytest.mark.skipif(sys.platform == "win32", reason="door stand-in is a POSIX script")
def test_shim_pipes_payload_to_door_and_returns_promptly_with_open_stdin(plugin, tmp_path: Path) -> None:
    _door_hooks(plugin, [f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a"])
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    home = _stub_home(tmp_path, '#!/bin/sh\ncat\n')
    r = subprocess.run([sys.executable, str(out / "hooks" / "agy-hook-run.py"), "hooks.a"],
                       input=b'{"cwd": "/w"}', capture_output=True, timeout=30,
                       env={"COORDINATOR_SETTINGS_HOME": str(home)})
    assert r.returncode == 0 and r.stdout == b'{"cwd": "/w"}'


@pytest.mark.skipif(sys.platform == "win32", reason="door stand-in is a POSIX script")
def test_door_stderr_surfaces_without_quiet_and_not_with_it(plugin, tmp_path: Path) -> None:
    _door_hooks(plugin, [f"COORDINATOR_DOOR_STDIN_MODE=hook {_DOOR} hooks.a"])
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    home = _stub_home(tmp_path, '#!/bin/sh\ncat >/dev/null\necho "door says no" >&2\nexit 1\n')
    shim = str(out / "hooks" / "agy-hook-run.py")
    env = {"COORDINATOR_SETTINGS_HOME": str(home)}
    loud = subprocess.run([sys.executable, shim, "hooks.a"], input=b"x", capture_output=True, timeout=30, env=env)
    quiet = subprocess.run([sys.executable, shim, "--quiet", "hooks.a"], input=b"x", capture_output=True,
                           timeout=30, env=env)
    assert loud.returncode == 1 and b"door says no" in loud.stderr
    assert quiet.returncode == 1 and quiet.stderr == b""


_REAL_RUN = subprocess.run
AGY_EVENT = {
    "cwd": "work\\repo", "workspacePaths": ["work\\repo"], "hookEventName": "PreToolUse",
    "conversationId": "bdf34fcf-e5e4-4186-a45f-3eaac72afe97",
    "toolCall": {"name": "run_command", "args": {"CommandLine": "coordinator-invoke engine_ask.submit --repo X",
                                                 "Cwd": "work\\repo", "Blocking": True}},
}


_LOADER = "import runpy, sys; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
_JSON_PAYLOAD = '{"a": true, "b": false, "c": null}'


def _run_generated(command, payload):
    """Run one generated hook command the way agy does (no shell), interpreter swapped for this one."""
    exe = gen._agy_python()
    assert command.startswith(exe + " ")
    if gen.os.name == "nt":
        return subprocess.run(gen._win_cmdline([sys.executable]) + command[len(exe):], input=payload,
                              capture_output=True, text=True, timeout=60)
    import shlex
    return subprocess.run([sys.executable, *shlex.split(command)[1:]], input=payload,
                          capture_output=True, text=True, timeout=60)


def test_args_form_hook_entries_run_their_script_not_the_stdin_payload(plugin, tmp_path: Path) -> None:
    (plugin / "hooks" / "scripts" / "pre.py").write_text(
        "import json, sys\nd = json.load(sys.stdin)\nprint('RAN', sys.argv[1:], d['a'], d['b'], d['c'])\n",
        encoding="utf-8")
    entry = {"type": "command", "command": "python3",
             "args": ["-c", _LOADER, "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/pre.py", "boot arg"]}
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [entry]}]}}), encoding="utf-8")
    out = tmp_path / "out"
    gen.generate(plugin, out, "agy")
    data = json.loads((out / "hooks.json").read_text(encoding="utf-8"))
    handlers = data["session-start"]["SessionStart"]
    assert len(handlers) == 1 and all("args" not in h for h in handlers)
    for h in handlers:
        r = _run_generated(h["command"], _JSON_PAYLOAD)
        assert "NameError" not in r.stderr and r.returncode == 0, r.stderr
        assert "RAN ['boot arg'] True False None" in r.stdout

"""The door's native guard leg agrees with the three Python guards it mirrors, with no warm engine.

Each deny and allow command from the three guards' own tests is fed to the built door as a Bash and as a
PowerShell PreToolUse payload. The engine root is a fixture (no pipe or socket, no cold entrypoint), so the
only possible deny is the door's native leg. Its verdict must equal the OR of the three Python `check`s.
`block-held-push` is never a native row. The over-deny control is the spike's 27 ordinary commands plus an
unheld `git push`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import (
    block_empty_source_push,
    block_worktree_creation,
    guard_uncapped_pytest_plugin,
)
from coordinator_core.warm.tests.door_test_support import (
    DOOR_CORE_C,
    DOOR_EXE,
    door_under_default_name,
    make_stub_engine_root,
)
from coordinator_core.warm.tests.test_door_read_deadline_posix import (
    _make_stub_engine_root as _make_posix_stub_engine_root,
    runtime_base,  # noqa: F401 -- fixture
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_OP = "hooks.preuse_bash_dispatch"
_TOOLS = ("Bash", "PowerShell")
_SHA = "0123456789abcdef0123456789abcdef01234567"

_EMPTY_SOURCE = "block-empty-source-push"
_WORKTREE = "block-worktree-creation"
_PYTEST_PLUGIN = "guard-uncapped-pytest-plugin"
_GUARD_NAMES = {_EMPTY_SOURCE, _WORKTREE, _PYTEST_PLUGIN}
_GUARDS = {
    _EMPTY_SOURCE: block_empty_source_push,
    _WORKTREE: block_worktree_creation,
    _PYTEST_PLUGIN: guard_uncapped_pytest_plugin,
}

_DENY = {
    _EMPTY_SOURCE: [
        "git push origin $C:refs/heads/x",
        "git push origin :refs/heads/x",
        "git push --delete origin x",
        "git push origin -d x",
        "git push -df origin x",
        'git push origin "${C}":main',
        "git push origin +$C:refs/heads/x",
        "git push origin $(git rev-parse FETCH_HEAD):refs/heads/x",
        "cd /tmp && git push origin $C:refs/heads/x",
    ],
    _WORKTREE: [
        "git worktree add ../wt-1 feature-branch",
        "git worktree move ../wt-1 ../wt-2",
        "git worktree repair",
        "git worktree lock ../wt-1",
        "git worktree unlock ../wt-1",
        "cd /x && git worktree add y",
        "git worktree frobnicate",
        "GIT_TRACE=1 git worktree add ../wt-1 x",
        "echo git worktree add x && git worktree add ../wt-1 y",
        "git worktree add ../wt-1 x # it's",
        "git -C /repo worktree add ../wt-1 x # it's",
        "git.exe worktree add ../wt-1 x # it's",
        "Get-Location; git worktree add ../wt-1 x",
        "cd /tmp; git worktree add ../y",
        "git -C /tmp worktree add ../y",
        "git worktree add ../wt-1 x\ncat <<EOF\nharmless\nEOF\n",
    ],
    _PYTEST_PLUGIN: [
        "python -m pytest -p myplugin tests/",
        "python3 -m pytest -pmyplugin tests/",
        "pytest -p=myplugin tests/",
        "py.test -q -p some.adhoc.mod tests/",
        "cd x && python.exe -u -m pytest -p adhoc tests/",
        "env FOO=1 pytest -p adhoc tests/",
        "python -X dev -W ignore -m pytest -p adhoc tests/",
    ],
}

_ALLOW = {
    _EMPTY_SOURCE: [
        f"git push origin {_SHA}:refs/heads/x",
        "git push origin main",
        "git push",
        "git push --dry-run origin :x",
        "git push -n origin $C:x",
        "git push origin HEAD:refs/heads/x",
        "git status",
    ],
    _WORKTREE: [
        "git worktree list",
        "git worktree remove ../wt-1",
        "git worktree prune",
        "git worktree",
        "git restore --worktree foo.py",
        "git restore -W foo.py",
        "git status && ls -la",
        "echo git worktree add x",
        "printf 'git worktree add\\n'",
        'grep -n "git worktree add" file.py',
        'git commit -m "document git worktree add usage"',
        "Get-Location; git worktree list",
        "cat <<EOF > /tmp/review.md\nDiscussion of git worktree add and block_worktree_creation.py behavior.\nEOF\n",
        "cat <<EOF > /tmp/review.md\nSee notes; git worktree add x is denied by design.\nEOF\n",
        "bash <<EOF\ngit worktree add ../wt-1 x\nEOF\n",
    ],
    _PYTEST_PLUGIN: [
        "pytest -p no:cacheprovider tests/",
        "python -m pytest -p no:cacheprovider -q tests/",
        "pytest -p no:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa tests/",
        "pytest -p xdist -n 2 tests/",
        "pytest -p coordinator_core.testing.nested_session_guard tests/",
        "python -m coordinator_core.testing.scoped_pytest --allow tests -- -p myplugin tests/",
        "pytest tests/ -q",
        "echo pytest -p myplugin",
        "grep -p myplugin pytest.ini",
        "pytest -- -p myplugin",
    ],
}

# The spike's over-deny control (verdict.md Q3), plus the unheld push the ruling names.
_OVER_DENY_CONTROL = [
    "git status", "git log -5", "git commit -m 'git push later'", "git stash list", "git stash pop",
    "git worktree list", "git worktree remove x", "rm scratch/a.txt", "rm -rf scratch/spike-8993bd/tmpdir",
    "rm -rf __pycache__", "git clean -n -d", "git clean -fdn", "git branch -d old", "git branch --list",
    "pytest -p no:cacheprovider tests/x.py", "pytest -p xdist -n 2 a.py", "find . -name '*.py'",
    "find /d/claude-klabauter/scratch -name x", "grep -rn foo coordinator_core/x.py", "taskkill /PID 123 /F",
    "ls /", "echo find / -name x", "git diff --stat", "python -m pytest tests/a.py", "gh pr view 3",
    "p4 sync", "git revert --abort",
    "git push origin feature-unheld",
]


def _payload(tool: str, command: str, env: dict | None = None) -> dict:
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "native-guard-parity",
        "cwd": ".",
    }
    if env is not None:
        body["env"] = env
    return body


def _python_denies(tool: str, command: str, env: dict | None = None) -> bool:
    payload = _payload(tool, command, env)
    return any(guard.check(payload) is not None for guard in _GUARDS.values())


@pytest.fixture(autouse=True)
def _isolated_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    if os.name == "nt":
        if not DOOR_EXE.exists():
            pytest.skip(f"no built door at {DOOR_EXE}")
        return DOOR_EXE
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', "-o", str(out),
         str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


@pytest.fixture(scope="module")
def engineless(door, tmp_path_factory):
    """A runner for hook payloads against an engine root with no listener and no cold entrypoint, so a
    deny can only come from the door's native leg and an allow is the door's loud pass."""
    base = tmp_path_factory.mktemp("native-guard")
    root = make_stub_engine_root(base) if os.name == "nt" else _make_posix_stub_engine_root(base)
    for script in (root / "coordinator" / "bin").glob("*.py"):
        script.unlink()
    exe = door_under_default_name(root) if os.name == "nt" else door
    short_runtime = Path(tempfile.mkdtemp(prefix="ngp", dir="/tmp" if os.name != "nt" else None))
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(short_runtime),
        COORDINATOR_SETTINGS_HOME=str(base / "settings-home"),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    env.pop("COORDINATOR_OVERRIDE_PUSH_DELETE", None)

    def run(payload: dict) -> tuple[bool, str]:
        proc = subprocess.run(
            [str(exe), _OP], input=json.dumps(payload).encode(), capture_output=True, env=env,
            cwd=str(root), timeout=60, **no_console_creationflags(),
        )
        assert proc.returncode == 0, proc.stderr
        body = json.loads(proc.stdout.decode("utf-8", "replace"))
        decision = body.get("hookSpecificOutput", {}).get("permissionDecision", "")
        return decision == "deny", proc.stdout.decode("utf-8", "replace")

    try:
        yield run
    finally:
        shutil.rmtree(short_runtime, ignore_errors=True)


def _cases(table: dict[str, list[str]]):
    return [
        pytest.param(guard, tool, cmd, id=f"{guard}-{tool}-{i}")
        for guard, cmds in table.items()
        for i, cmd in enumerate(cmds)
        for tool in _tools_for(cmd)
    ]


def _tools_for(command: str) -> tuple[str, ...]:
    """A heredoc is POSIX shell syntax; PowerShell has none, so those cases are Bash-only."""
    return ("Bash",) if "<<" in command else _TOOLS


@pytest.mark.parametrize("guard,tool,command", _cases(_DENY))
def test_every_python_deny_case_is_denied_natively_and_names_its_guard(engineless, guard, tool, command):
    assert _python_denies(tool, command), "fixture drifted: the Python guards no longer deny this case"
    denied, out = engineless(_payload(tool, command))
    assert denied, f"native leg allowed a Python deny case: {out}"
    assert f"BLOCKED by {guard}" in out


@pytest.mark.parametrize("guard,tool,command", _cases(_ALLOW))
def test_every_python_allow_case_is_allowed_natively(engineless, guard, tool, command):
    assert not _python_denies(tool, command), "fixture drifted: the Python guards now deny this case"
    denied, out = engineless(_payload(tool, command))
    assert not denied, f"native over-deny of a Python allow case: {out}"


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("command", _OVER_DENY_CONTROL)
def test_over_deny_control_has_zero_native_denies_the_python_guards_allow(engineless, tool, command):
    if _python_denies(tool, command):
        pytest.skip("a Python guard denies this control; the allow side does not apply")
    denied, out = engineless(_payload(tool, command))
    assert not denied, f"native over-deny: {out}"


@pytest.mark.parametrize("tool", _TOOLS)
def test_an_unheld_git_push_is_not_denied_natively(engineless, tool):
    denied, out = engineless(_payload(tool, "git push origin feature-unheld"))
    assert not denied, out


def test_differential_has_full_agreement_over_all_cases(engineless):
    """Aggregate form of the per-case tests: the single figure A1 reports."""
    cases = [(t, c) for table in (_DENY, _ALLOW) for cmds in table.values() for c in cmds for t in _tools_for(c)]
    cases += [(t, c) for c in _OVER_DENY_CONTROL for t in _TOOLS]
    disagreements = [
        (tool, cmd) for tool, cmd in cases if engineless(_payload(tool, cmd))[0] != _python_denies(tool, cmd)
    ]
    assert not disagreements, disagreements


@pytest.mark.parametrize("command", _DENY[_EMPTY_SOURCE][:1])
def test_the_push_override_in_the_payload_env_allows_natively_as_in_python(engineless, command):
    env = {"COORDINATOR_OVERRIDE_PUSH_DELETE": "1"}
    assert not _python_denies("Bash", command, env)
    denied, out = engineless(_payload("Bash", command, env))
    assert not denied, out
    other = {"COORDINATOR_OVERRIDE_PUSH_DELETE": "0"}
    assert _python_denies("Bash", command, other)
    assert engineless(_payload("Bash", command, other))[0]


def _native_row_names() -> list[str]:
    source = DOOR_CORE_C.read_text(encoding="utf-8")
    table = re.search(r"door_native_guard_rows\[\]\s*=\s*\{(.*?)\n\};", source, re.DOTALL)
    assert table, "door_native_guard_rows[] not found in door_core.c"
    return re.findall(r'^\s*\{\s*"([a-z0-9-]+)"', table.group(1), re.MULTILINE)


def test_native_row_names_are_exactly_the_three_guards_and_exclude_block_held_push():
    names = _native_row_names()
    assert len(names) == len(set(names)) == 3
    assert set(names) == _GUARD_NAMES
    assert "block-held-push" not in names

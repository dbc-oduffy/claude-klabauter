"""coordinator_core.ops.tests.test_invoke_from_argv_stdin -- the door's
hook-mode `params.stdin` reaches the named entrypoint as its `sys.stdin`.

The door (`door_posix.c` / `door.c`, `COORDINATOR_DOOR_STDIN_MODE=hook`)
sends the caller's stdin payload inside `params`. Before this was consumed,
`invoke.from_argv` read only `argv`/`cwd`/`entrypoint`, so `hook-run.py`
served warm read the SERVER's own stdin, parsed an empty event, lost `cwd`,
and every working-tree-scoped guard returned `-32602` -- byte-identical for a
payload that must be denied and one that must be allowed.

Coverage:
  (a) a declared payload is exactly what the entrypoint reads from stdin;
  (b) no declared payload reads as empty -- never the server's own handle;
  (c) the server's `sys.stdin` is restored after the call, success or raise;
  (d) a non-string `stdin` is a params-validation error.

Hermetic: `_resolve_entrypoint_script` is pointed at a throwaway script that
echoes its stdin, so no allowlisted CLI, engine, or repo state is involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.ops import invoke_from_argv as ifa

_ECHO_STDIN = (
    "import sys\n"
    "def main(argv=None):\n"
    "    sys.stdout.write('<' + sys.stdin.read() + '>')\n"
    "    return 0\n"
)

_RAISES = (
    "import sys\n"
    "def main(argv=None):\n"
    "    sys.stdin.read()\n"
    "    raise RuntimeError('boom')\n"
)


def _point_at(monkeypatch, tmp_path: Path, body: str) -> None:
    script = tmp_path / "stdin-probe.py"
    script.write_text(body, encoding="utf-8")
    monkeypatch.setattr(ifa, "_resolve_entrypoint_script", lambda _name: script)


def test_declared_payload_is_the_entrypoints_stdin(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _ECHO_STDIN)
    payload = '{"hook_event_name": "PreToolUse", "cwd": "/x"}'
    result = ifa._invoke_from_argv(
        {"argv": [], "cwd": str(tmp_path), "entrypoint": "stdin-probe", "stdin": payload}
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "<" + payload + ">"


def test_absent_payload_reads_empty_not_the_servers_handle(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, _ECHO_STDIN)
    result = ifa._invoke_from_argv(
        {"argv": [], "cwd": str(tmp_path), "entrypoint": "stdin-probe"}
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "<>"


@pytest.mark.parametrize("body", [_ECHO_STDIN, _RAISES])
def test_server_stdin_is_restored_after_the_call(monkeypatch, tmp_path, body):
    _point_at(monkeypatch, tmp_path, body)
    before = sys.stdin
    ifa._invoke_from_argv(
        {"argv": [], "cwd": str(tmp_path), "entrypoint": "stdin-probe", "stdin": "x"}
    )
    assert sys.stdin is before


def test_stdin_must_be_a_string_when_present(tmp_path):
    with pytest.raises(ValueError, match="params.stdin"):
        ifa._invoke_from_argv(
            {"argv": [], "cwd": str(tmp_path), "entrypoint": "hook-run", "stdin": 7}
        )

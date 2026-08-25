"""End-to-end guard for chunk C3 of
docs/plans/2026-08-18-quote-safe-payloads-through-the-cmd-forw.md.

Drives a REAL `.cmd` forwarder, generated through the production
`_write_agent_cmd_forwarder` code path (never a hand-authored `.cmd`
string), with a payload that carries both a double quote and a space —
the combination the plan's Problem section measured as both stripped
(quotes) and split (the space) by the un-re-quoted `%*` expansion every
generated forwarder ends in.

The spawn shape matters: a `subprocess.run([cmd_half, *args])` list-form
spawn from Python does NOT reproduce the corruption, because Windows'
`CreateProcess` hands the child a command line built directly from the
argv list, bypassing the quoting a real interactive shell performs on the
way in. The corruption is a property of how an operator's shell (this
repo's operators launch from PowerShell) re-serializes its own argument
list into the single command-line string `cmd.exe` then re-parses through
`%*`. Reproducing it faithfully requires spawning via `powershell.exe
-Command`, letting PowerShell's own quoting do what a real invocation
does. This was verified by hand before writing this test: the identical
payload spawned in list-form round-trips intact; spawned through
`powershell.exe -Command` it arrives corrupted exactly as the plan's
evidence table records.

Spec backlink: docs/plans/2026-08-18-quote-safe-payloads-through-the-cmd-forw.md, chunk C3
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install.substrate import _write_agent_cmd_forwarder
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# Carries both a double quote and a space -- the plan's Problem section
# measured this combination as the one that is both stripped (the quotes)
# AND split (the space) by the forwarder's un-re-quoted `%*` expansion.
_HOSTILE_PAYLOAD = '{"a": "b c"}'

_TARGET_BODY = (
    "import sys\n"
    "argv = sys.argv[1:]\n"
    "if '--decisions-file' in argv:\n"
    "    path = argv[argv.index('--decisions-file') + 1]\n"
    "    with open(path, 'r', encoding='utf-8') as fh:\n"
    "        sys.stdout.write('FILE_CHANNEL:' + fh.read())\n"
    "else:\n"
    "    sys.stdout.write('INLINE_CHANNEL:' + repr(argv))\n"
)


def _render_forwarder(tmp_path: Path) -> Path:
    """Write a throwaway Unix-half target plus its generated `.cmd` half
    through the REAL production generator (`_write_agent_cmd_forwarder`),
    matching the house pattern in test_substrate.py's
    `_render_forwarder_pair` -- a hand-authored `.cmd` string here would
    stop tracking substrate.py's actual template and this guard would go
    stale silently the day that template changes."""
    name = "decisions-file-channel-probe"
    unix_half = tmp_path / name
    unix_half.write_text(_TARGET_BODY, encoding="utf-8")
    cmd_half = tmp_path / f"{name}.cmd"
    _write_agent_cmd_forwarder(
        name, cmd_half, False, python3_cmd_resolved_bin=sys.executable, target=f"{name}.py"
    )
    return cmd_half


def _run_via_powershell(command: str) -> subprocess.CompletedProcess:
    """Spawns through a real `powershell.exe -Command`, the shell real
    operators invoke coordinator CLIs from. See module docstring: this is
    the spawn shape that actually reproduces the corruption -- a
    list-form `subprocess.run` does not."""
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
    )


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_decisions_file_channel_round_trips_hostile_payload_byte_identical(tmp_path):
    cmd_half = _render_forwarder(tmp_path)
    payload_file = tmp_path / "decisions.json"
    payload_file.write_text(_HOSTILE_PAYLOAD, encoding="utf-8")

    proc = _run_via_powershell(f'& "{cmd_half}" brief --decisions-file "{payload_file}"')

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("FILE_CHANNEL:")
    received = proc.stdout[len("FILE_CHANNEL:"):]
    assert received == _HOSTILE_PAYLOAD


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_decisions_inline_channel_still_arrives_corrupted(tmp_path):
    # This is the negative half AC3 requires, and it is the point of the
    # chunk, not padding: it pins WHY the file channel exists. The risk it
    # guards is a future change to the generic `%*` tail every forwarder
    # ends in -- a re-quoting scheme, a shift to `%1 %2 ...`, or any other
    # attempt to make argv survive the child's CRT parse. If one of those
    # makes the inline path carry a quote-and-space payload intact, this
    # assertion fails and sends whoever did it to the plan's Problem
    # section before the file channel gets removed as redundant.
    #
    # It does NOT guard `_RAW_CMDLINE_TARGETS` (anti-scoped in the same
    # plan), despite the temptation to claim so: that set gates only the
    # `%CMDCMDLINE%` capture side-channel, which the probe target below
    # never reads, so widening it would not change this outcome either
    # way.
    cmd_half = _render_forwarder(tmp_path)

    proc = _run_via_powershell(f"& \"{cmd_half}\" brief --decisions '{_HOSTILE_PAYLOAD}'")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("INLINE_CHANNEL:")
    received = proc.stdout[len("INLINE_CHANNEL:"):]
    # The corrupted argv must NOT equal what an intact channel would have
    # produced (a 3-element list with the payload as one token) --
    # quotes stripped and the embedded space splitting it into an extra
    # token, exactly as the plan's evidence table measured.
    intact = repr(["brief", "--decisions", _HOSTILE_PAYLOAD])
    assert received != intact
    assert '"' not in received

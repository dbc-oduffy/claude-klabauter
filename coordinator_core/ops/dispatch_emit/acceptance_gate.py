"""Acceptance gate: which plan rows name a test command, and demotion of met rows whose command is red.

Pure predicates, no spawn. `test_command_rows` runs at compose time (Python reads the spine);
`demote_red` runs in the composed script, so it ships a JS twin (`DEMOTE_RED_JS`) that ask_compose
emits. A result without an integer exit is red: an agent that skipped the command cannot leave a row met.
"""

from __future__ import annotations

import shlex
from typing import Any, Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit import objective_check

__all__ = ["DEMOTE_RED_JS", "demote_red", "test_command_rows"]

_RUNNER_HEADS = ("python", "python3", "py")


def _written_argv(command: str) -> Optional[list]:
    """`objective_check._argv_for(command)` with argv[0] restored to the token as written."""
    argv = objective_check._argv_for(command)
    if not argv:
        return None
    match = objective_check._BACKTICK_RE.search(command)
    try:
        written = shlex.split(match.group(1) if match else command)
    except ValueError:
        return None
    if written:
        argv[0] = written[0]
    return argv


def _strip_exits_clause(argv: list) -> list:
    """Drop a trailing prose `exits <n>` clause that shlex would hand pytest as paths."""
    if len(argv) >= 2 and argv[-2] == "exits" and argv[-1].isdigit():
        return argv[:-2]
    return argv


def _names_test_runner(argv: list) -> bool:
    head = argv[0].replace("\\", "/").rsplit("/", 1)[-1]
    if head in ("pytest", "pytest.exe"):
        return True
    return head.removesuffix(".exe") in _RUNNER_HEADS and argv[1:3] == ["-m", "pytest"]


def test_command_rows(rows: Iterable[Mapping[str, Any]]) -> list:
    """`[(row_id, register_rows, argv)]` for each spine row whose `acceptance:` names pytest or
    `python -m pytest`; prose acceptance is skipped. argv[0] stays as written and a trailing
    `exits <n>` clause is removed."""
    out = []
    for row in rows:
        acceptance = row.get("acceptance") if isinstance(row, Mapping) else None
        if not isinstance(acceptance, str) or not acceptance.strip():
            continue
        argv = _written_argv(acceptance)
        if not argv:
            continue
        argv = _strip_exits_clause(argv)
        if not argv or not _names_test_runner(argv):
            continue
        regs = row.get("register_rows")
        out.append((str(row.get("id")), [str(r) for r in regs] if isinstance(regs, list) else [], argv))
    return out


def _exit_of(result: Mapping[str, Any]) -> Optional[int]:
    code = result.get("exit")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def demote_red(verdicts: list, results: list) -> list:
    """A copy of `verdicts` where every `met` item named in a red result's `register_rows` becomes
    `not_met` with `remaining: ["acceptance exit <n>: <command>"]`. A result is `{row_id,
    register_rows, command, exit}`; red is any exit that is not the integer 0 (`<n>` reads
    `unreported` when absent or non-integer)."""
    red = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        code = _exit_of(result)
        if code == 0:
            continue
        shown = "unreported" if code is None else str(code)
        regs = result.get("register_rows")
        red.append((regs if isinstance(regs, list) else [], f"acceptance exit {shown}: {result.get('command')}"))
    out = []
    for item in verdicts:
        if isinstance(item, Mapping) and item.get("status") == "met":
            for regs, message in red:
                if item.get("id") in regs:
                    item = {**item, "status": "not_met", "remaining": [message]}
                    break
        out.append(item)
    return out


DEMOTE_RED_JS = """\
const _demoteRed = (verdicts, results) => {
  const red = [];
  for (const r of results ?? []) {
    if (!r || typeof r !== 'object') continue;
    const ok = Number.isInteger(r.exit);
    if (ok && r.exit === 0) continue;
    red.push([Array.isArray(r.register_rows) ? r.register_rows : [], 'acceptance exit ' + (ok ? String(r.exit) : 'unreported') + ': ' + String(r.command)]);
  }
  return (verdicts ?? []).map((item) => {
    if (item && typeof item === 'object' && item.status === 'met') {
      for (const [regs, message] of red) {
        if (regs.includes(item.id)) return { ...item, status: 'not_met', remaining: [message] };
      }
    }
    return item;
  });
};
"""

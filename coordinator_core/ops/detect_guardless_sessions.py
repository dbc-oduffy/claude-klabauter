"""
coordinator_core.ops.detect_guardless_sessions — observe live `claude.exe`
processes launched WITHOUT the coordinator plugin, after the fact.

Purpose: coordinator guards reach a session only because it was launched via
`claude-doe`, which execs `claude --plugin-dir <DoE>/coordinator`. A session
launched as bare `claude` (cmd.exe, `-NoProfile`, or explicit `claude.exe`)
gets ZERO coordinator guards, in any repo, silently. Session records under
`.git/coordinator-sessions/` are written BY the coordinator hooks, so a
guardless session leaves no record there — absence in that ledger is
unobservable by construction. This module derives its signal from something
that exists INDEPENDENTLY of coordinator hooks instead: the live OS process
table's command lines.

Detection only, never repair. This module does not kill processes, does not
mutate settings, and does not intercept future launches — that is a separate
workstream (interception), explicitly out of scope here.

Signal chosen: `psutil.process_iter` (in-process, no subprocess spawned),
filtered client-side to `name == claude.exe`, then the `--plugin-dir` flag's
*value* (not just the flag's presence) is checked against the resolved
coordinator plugin directory. Rejected alternatives, and why:

  - Harness-written per-session transcripts (`~/.claude/projects/<proj>/
    <session-id>.jsonl`) — real, verified to exist independently of
    coordinator hooks (see run-report sidecar), but the delta against
    `.git/coordinator-sessions/<id>/` is NOT a clean guardless signal:
    dispatched subagents get their own transcript files too, without ever
    writing their own coordinator-sessions directory (only the top-level EM
    session writes one). Diffing those two populations conflates "guardless
    top-level launch" with "ordinary subagent dispatch", producing a flood of
    false positives with no cheap way to tell them apart from a filename
    alone. It also only observes PAST sessions whose harness process has
    already exited and been swept, which does not fit this op's "safe to
    call from a routine surface, cheap, live" requirement as directly as a
    single process-table read.
  - `claude-doe` writing its own launch ledger — does not exist; building it
    is spawn-time interception (a separate, explicitly out-of-scope
    workstream), not read-only detection of the state the machine is
    ALREADY in.

The process table is authoritative for "right now": a `claude.exe` process's
own command line is the ground truth of how it was launched, sourced from the
OS itself, not from anything coordinator wrote or could fail to write.

Negative-spec:
    - Read-only. No process is signalled, killed, or modified. No file is
      written. No settings are mutated.
    - Never collapses "cannot determine" into "clean". `cannot_determine=True`
      is a distinct verdict, always inspected before `guardless` is treated
      as an authoritative empty list.
    - Windows-first, not Windows-only: on a non-Windows platform, or if the
      psutil enumeration fails, this degrades to `cannot_determine=True`
      with a reason — it does not raise, and it does not report a clean
      verdict it could not actually observe.
    - No subprocess spawned at all — `psutil.process_iter` enumerates and
      filters in-process, in a single pass over the OS process table.
    - A process whose command line cannot be read (`psutil.AccessDenied`,
      e.g. a `claude.exe` running as another user) is never silently folded
      into "clean" or "guarded" — it surfaces the whole result as
      `cannot_determine=True` (see `_run_process_probe`/`_CommandLineUnavailable`).
      Measured zero occurrences on this box (55/55 `claude.exe` command lines
      readable), but the guard exists for the case where it happens.
    - A zero-process result on Windows is treated as `cannot_determine`, not
      clean — this detector runs from inside a live `claude` session, so a
      genuinely empty process table is impossible and is more likely a
      transient enumeration hiccup that returned an empty collection
      without raising.

Spec backlink: dispatch brief "guardless-session-detector" (2026-08-10),
coordinator guard-bypass observability workstream.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

_PLUGIN_DIR_VALUE_RE = re.compile(
    r'--plugin-dir(?:=|\s+)("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+)'
)

#: Item 38.2 -- a short-lived helper `claude.exe` (e.g. a brief non-interactive
#: tool invocation) is not a guardless SESSION and must not be flagged as one.
#: One named constant, read against `ProcessObservation.age_seconds`.
_MIN_AGE_SECONDS = 5.0

#: Parent-process names that indicate a direct, interactive launch (a shell
#: or the desktop shell), as opposed to a helper spawned as another
#: program's subprocess. Lower-cased comparison.
_INTERACTIVE_PARENT_NAMES = frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe", "explorer.exe", "windowsterminal.exe"}
)


@dataclass
class ProcessObservation:
    pid: int
    command_line: str
    guarded: bool
    #: Seconds since the process started, or `None` when unknown (e.g. a
    #: fixture/older caller that never populated it). `None` never
    #: suppresses the guardless flag -- see `_counts_as_guardless`.
    age_seconds: Optional[float] = None
    #: Whether the process's parent is a known interactive shell, `None`
    #: when unresolvable. `None` never suppresses the guardless flag.
    has_interactive_parent: Optional[bool] = None


@dataclass
class DetectionResult:

    cannot_determine: bool
    reason: Optional[str]
    observed: List[ProcessObservation] = field(default_factory=list)
    guardless: List[ProcessObservation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cannot_determine": self.cannot_determine,
            "reason": self.reason,
            "observed_count": len(self.observed),
            "guardless_count": len(self.guardless),
            "guardless": [
                {"pid": p.pid, "command_line": p.command_line}
                for p in self.guardless
            ],
        }


def _plugin_dir_value(command_line: str) -> Optional[str]:
    """Extract the `--plugin-dir` flag's value from *command_line*, or
    `None` if the flag is absent. Strips a matching pair of surrounding
    quotes.

    LAST-WINS on a repeated `--plugin-dir` flag, via `re.findall` +
    taking the final match, matching ordinary CLI-argument-parsing
    semantics for a repeated flag (a later occurrence overrides an earlier
    one) -- not `.search`'s first-match behaviour. No current launcher
    emits two `--plugin-dir` flags on one command line (see this module's
    own docstring, "Signal chosen" -- a bare single invocation), but a
    future wrapper script that appends an override after a default would
    otherwise be classified off the discarded first value (Review:
    coordinatorcode-reviewer-ad7b843b P3).
    """
    if not command_line:
        return None
    matches = _PLUGIN_DIR_VALUE_RE.findall(command_line)
    if not matches:
        return None
    value = matches[-1]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        value = value[1:-1]
    return value


def _resolved_coordinator_plugin_dir() -> Optional[str]:
    try:
        from coordinator_core.resolution.facade import resolve_operator_config
        from coordinator_core.data_root import content_root_for

        doe_root = resolve_operator_config()["doe_root"]
    except Exception:
        return None
    content_root = content_root_for(doe_root)
    if content_root is None:
        return None
    return os.path.normcase(os.path.normpath(str(content_root)))


def _is_guarded(command_line: str) -> bool:
    value = _plugin_dir_value(command_line)
    if value is None:
        return False

    resolved = _resolved_coordinator_plugin_dir()
    if resolved is not None:
        try:
            candidate = os.path.normcase(os.path.normpath(value))
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None:
            return candidate == resolved

    return value.rstrip("/\\").rstrip("\"'").endswith("coordinator")


class _CommandLineUnavailable(Exception):

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"cmdline for claude.exe pid {pid} could not be read")


def _parent_is_interactive(ppid: Optional[int]) -> Optional[bool]:
    """`True`/`False` when the parent's process name resolves, `None`
    (unknown) otherwise -- e.g. no `ppid` in hand, the parent already
    exited, or its name can't be read. Never raises; `None` is the
    fail-open case a caller must not collapse into "not interactive".
    """
    if not ppid:
        return None
    try:
        import psutil

        return psutil.Process(int(ppid)).name().lower() in _INTERACTIVE_PARENT_NAMES
    except Exception:
        return None


def _run_process_probe() -> List[ProcessObservation]:
    import psutil

    observations: List[ProcessObservation] = []
    now = time.time()
    for proc in psutil.process_iter(["pid", "name", "create_time", "ppid"]):
        name = (proc.info.get("name") or "").lower()
        if name != "claude.exe":
            continue
        try:
            cmdline_list = proc.cmdline()
        except psutil.AccessDenied as exc:
            raise _CommandLineUnavailable(proc.pid) from exc
        except psutil.NoSuchProcess:
            continue
        command_line = " ".join(cmdline_list)
        create_time = proc.info.get("create_time")
        age_seconds = (
            now - create_time if isinstance(create_time, (int, float)) else None
        )
        observations.append(
            ProcessObservation(
                pid=proc.pid,
                command_line=command_line,
                guarded=_is_guarded(command_line),
                age_seconds=age_seconds,
                has_interactive_parent=_parent_is_interactive(proc.info.get("ppid")),
            )
        )
    return observations


def _counts_as_guardless(obs: ProcessObservation) -> bool:
    """True iff *obs* is an unguarded top-level session -- never a
    short-lived helper subprocess (item 38.2). A helper is either younger
    than `_MIN_AGE_SECONDS` or not launched directly from an interactive
    shell. `age_seconds`/`has_interactive_parent` of `None` (unknown)
    never suppresses the flag -- this module's Negative-spec never
    collapses "cannot determine" into "clean"."""
    if obs.guarded:
        return False
    if obs.age_seconds is not None and obs.age_seconds < _MIN_AGE_SECONDS:
        return False
    if obs.has_interactive_parent is False:
        return False
    return True


def detect(platform_system: Optional[str] = None) -> DetectionResult:
    system = platform_system if platform_system is not None else platform.system()
    if system != "Windows":
        return DetectionResult(
            cannot_determine=True,
            reason=f"process-table probe is Windows-only (platform.system() == {system!r})",
        )

    try:
        observations = _run_process_probe()
    except _CommandLineUnavailable as exc:
        return DetectionResult(
            cannot_determine=True,
            reason=str(exc),
        )
    except Exception as exc:
        return DetectionResult(
            cannot_determine=True,
            reason=f"psutil process-table enumeration failed: {exc}",
        )

    if not observations:
        return DetectionResult(
            cannot_determine=True,
            reason=(
                "psutil probe observed zero claude.exe processes, which "
                "is impossible since this detector runs from inside a live "
                "claude session — treating as a probe failure (e.g. a "
                "transient enumeration hiccup) rather than a clean verdict"
            ),
        )

    guardless = [o for o in observations if _counts_as_guardless(o)]
    return DetectionResult(
        cannot_determine=False,
        reason=None,
        observed=observations,
        guardless=guardless,
    )


def main(argv: List[str]) -> int:
    result = detect()
    print(json.dumps(result.to_dict(), indent=2))
    if result.cannot_determine:
        return 2
    if result.guardless:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

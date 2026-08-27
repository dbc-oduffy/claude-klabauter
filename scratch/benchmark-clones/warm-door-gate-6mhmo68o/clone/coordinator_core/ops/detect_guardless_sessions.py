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
from dataclasses import dataclass, field
from typing import List, Optional

# Matches the --plugin-dir flag's VALUE (quoted or bare), not just the
# flag's presence — a value is required before a command line can be
# classified guarded at all (Review: coordinatorcode-reviewer-5d457f48 —
# substring-only test false-positives on an unrelated --plugin-dir and
# false-negatives a coordinator-guarded process whose value merely doesn't
# match a hard-coded literal).
#
# The quoted alternatives use `(?:\\.|[^"\\])*`/`(?:\\.|[^'\\])*` rather than
# a bare `[^"]*`/`[^']*` so a backslash immediately before the closing quote
# is treated as escaping that quote, not as the value's own trailing
# character — matching Windows CRT argv parsing, where
# `--plugin-dir="X:\DoE-claude\coordinator\"` (abs-path-ok: illustrative
# example command line in prose, not a runtime path reference) reads the
# `\"` as an escaped literal quote and the real value continues past it. A
# bare `[^"]*` stops at that `\"` and mis-parses the value short (Review:
# coordinatorcode-reviewer-ad7b843b P2).
_PLUGIN_DIR_VALUE_RE = re.compile(
    r'--plugin-dir(?:=|\s+)("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+)'
)


@dataclass
class ProcessObservation:
    pid: int
    command_line: str
    guarded: bool


@dataclass
class DetectionResult:
    """Three-way verdict — `cannot_determine` is always checked first.

    - cannot_determine=True: the probe could not run (non-Windows, probe
      failure, malformed output). `reason` explains why. `guardless` is
      always [] in this case and MUST NOT be read as "clean".
    - cannot_determine=False and guardless == []: probe ran successfully and
      observed zero guardless claude.exe processes. This IS the clean
      verdict.
    - cannot_determine=False and guardless != []: at least one live
      claude.exe process has no --plugin-dir in its command line.
    """

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
    """Resolve the coordinator plugin directory the same way the rest of
    the repo does (`coordinator_core.resolution.facade.resolve_operator_config`
    -> `doe_root`/`coordinator`), normalized for path comparison. Returns
    `None` if operator config cannot be resolved (e.g. corrupt/missing
    registry) — callers fall back to an approximation in that case."""
    try:
        from coordinator_core.resolution.facade import resolve_operator_config

        doe_root = resolve_operator_config()["doe_root"]
    except Exception:
        return None
    return os.path.normcase(os.path.normpath(os.path.join(doe_root, "coordinator")))


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

    # Deliberate approximation when operator-config resolution is
    # unavailable: match a --plugin-dir value that names a directory
    # literally called "coordinator". False-positive shape: any
    # --plugin-dir pointing at an unrelated directory that merely happens
    # to be named "coordinator" reads as guarded when it has nothing to do
    # with the DoE coordinator plugin (Review: coordinatorcode-reviewer-5d457f48).
    return value.rstrip("/\\").rstrip("\"'").endswith("coordinator")


class _CommandLineUnavailable(Exception):
    """Raised by `_run_process_probe` when a live `claude.exe` process's
    command line could not be read (`psutil.AccessDenied`).

    Caught by `detect()` and turned into `cannot_determine=True` — an
    unreadable command line is an observation this module could not make,
    never a silent "clean" or "guarded" default (see module docstring
    negative-spec). Measured zero occurrences on this box (55/55 claude.exe
    command lines readable via psutil 7.2.2), but the guard exists for a
    claude.exe running as another user.
    """

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"cmdline for claude.exe pid {pid} could not be read")


def _run_process_probe() -> List[ProcessObservation]:
    """Enumerate every live `claude.exe` process and its command line via
    `psutil` (a hard dependency, see `pyproject.toml`), in-process — no
    subprocess spawned (see module docstring "Signal chosen").

    Raises `psutil.Error` on an enumeration failure and
    `_CommandLineUnavailable` if a claude.exe process's cmdline could not be
    read — both are caught by `detect()` and turned into
    `cannot_determine=True`.
    """
    import psutil

    observations: List[ProcessObservation] = []
    # process_iter(attrs=["pid", "name"]) defaults `ad_value=None`, so an
    # unreadable `name`/`pid` attr comes back as `None` here rather than
    # raising AccessDenied out of this loop (verified against psutil 7.2.2 —
    # Review: coordinator:code-reviewer). The `or ""` guard below
    # is what makes that `None` safe to `.lower()` and filter on; the
    # explicit `proc.cmdline()` try/except a few lines down is a separate,
    # deliberately-covered AccessDenied path (_CommandLineUnavailable).
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name != "claude.exe":
            continue
        try:
            cmdline_list = proc.cmdline()
        except psutil.AccessDenied as exc:
            raise _CommandLineUnavailable(proc.pid) from exc
        except psutil.NoSuchProcess:
            continue
        # `cmdline` is a list[str]; joined with a single space to feed the
        # existing string-based --plugin-dir matcher (_is_guarded /
        # _plugin_dir_value) unchanged rather than reshaping it to take a
        # list.
        command_line = " ".join(cmdline_list)
        observations.append(
            ProcessObservation(
                pid=proc.pid,
                command_line=command_line,
                guarded=_is_guarded(command_line),
            )
        )
    return observations


def detect(platform_system: Optional[str] = None) -> DetectionResult:
    """Detect guardless `claude.exe` processes currently live on this host.

    `platform_system` is injectable for tests; defaults to
    `platform.system()`.
    """
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
    except Exception as exc:  # psutil.Error and anything else process-iter can raise
        return DetectionResult(
            cannot_determine=True,
            reason=f"psutil process-table enumeration failed: {exc}",
        )

    if not observations:
        # A zero-process result is indistinguishable from a transient
        # enumeration hiccup that returns an empty collection without
        # raising. It is also provably wrong: this detector itself runs
        # from inside a live `claude` session, so the process table cannot
        # actually contain zero claude.exe rows. Treat it as
        # cannot_determine, not clean (Review: coordinatorcode-reviewer-
        # 5d457f48 — the exact collapse-into-clean failure mode this module
        # exists to prevent).
        return DetectionResult(
            cannot_determine=True,
            reason=(
                "psutil probe observed zero claude.exe processes, which "
                "is impossible since this detector runs from inside a live "
                "claude session — treating as a probe failure (e.g. a "
                "transient enumeration hiccup) rather than a clean verdict"
            ),
        )

    guardless = [o for o in observations if not o.guarded]
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

"""
coordinator_core.install.host_sampler_scheduler -- installs the OS-level
Windows Task Scheduler entry that fires
``coordinator_core.telemetry.host_sampler`` on a fixed cadence.

Purpose: ``coordinator_core.telemetry.host_sampler`` was built with a
load-bearing requirement that it run OUTSIDE the coordinator engine's own
process tree (see that module's docstring) -- an unscheduled sampler samples
nothing, and a scheduler that is itself a coordinator/engine process
reproduces the exact blind spot the sampler exists to end (2026-08-15's
2h28m total telemetry blackout, state/handoffs/2026-08-15-kill-it-if-it-
cannot-pay-for-itself.md AC4). This module is the install-surface node that
closes that gap: it registers a Windows Task Scheduler task, owned entirely
by the OS (``schtasks.exe`` / the Task Scheduler service), which keeps
firing the sampler on schedule even when every ``claude``/``python``
process on the box has died at once.

Why ``schtasks.exe`` rather than a Python-native mechanism: registering a
Windows Scheduled Task has no stdlib or ctypes-only equivalent short of
implementing the ITaskService COM interface by hand -- a large, drift-prone
surface for one install step, and Task Scheduler's *own* command-line
front end is the documented, stable way to drive it. This is a genuine
shell-out (``docs/reference/shell-out-carve-outs.md`` is a closed list),
and it does NOT fit any of that doc's existing classes (a)-(f): it is not a
3rd-party installer run as-published (a), not a git-hook artifact (b), not
an interpreter/shell self-probe (d), not a live-shell-environment artifact
under test (e), and not an optional 3rd-party verification tool (f) --
Task Scheduler is a first-party Windows OS surface with no bash/sh
involvement at all (unlike b/d/e). This call-site is deliberately flagged
in the module docstring, install dispatch report, and run-report sidecar
as requiring an explicit PM ruling to add a new sanctioned class (or amend
an existing one) -- it is NOT silently treated as already covered.

Cold path: this module runs only from the installer (``scripts/setup.py``)
and the uninstaller (``coordinator_core.install.uninstall_legs``), never on
the commit/session-start hot path
(``docs/reference/shell-out-carve-outs.md``'s hot-path classification).

Negative-spec (mirrors ``install_machine_identity``'s advisory shape):
    - Never fails the caller. Registration/removal failures are printed as
      [ADVISORY] and swallowed -- the installer/uninstaller as a whole must
      still succeed even if Task Scheduler is unavailable, denied by policy,
      or ``schtasks.exe`` is missing (non-Windows).
    - No-op (advisory, not an error) on non-Windows -- Task Scheduler does
      not exist there; cron wiring is explicitly out of this module's scope
      until named.
    - Idempotent: ``schtasks /Create ... /F`` overwrites an existing task of
      the same name in place rather than erroring or duplicating it, so
      running the installer twice yields exactly one task, unchanged in
      identity, refreshed in definition.

Spec backlink: state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md (AC4)
               docs/wiki/cost-budgets-and-the-kill-disposition.md
               coordinator_core/telemetry/host_sampler.py (module docstring)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.win_portability import no_console_creationflags

# Fixed task name -- the identity idempotency keys on. Changing this string
# orphans any previously-registered task under the old name; do not rename
# without adding an uninstall leg for the old name too.
TASK_NAME = "CoordinatorHostSampler"

# Mirrors coordinator_core.telemetry.host_sampler._DEFAULT_INTERVAL_SECS
# (1200s == 20 minutes -- PM ruling 2026-08-16, superseding the original
# 120s/2min figure; see that module's docstring "Cadence arithmetic" for
# the tradeoff arithmetic). Kept as an independent literal rather than an
# import of that module's private constant -- this is an OS-scheduler
# cadence declaration, not a runtime read of the sampler's own tuning, and
# the two are allowed to drift apart if a future change retunes one without
# the other (an explicit rebuild record either way, per that module's own
# ratchet doctrine).
_INTERVAL_MINUTES = 20

_IS_WINDOWS = os.name == "nt" or sys.platform == "win32"


def _schtasks_argv(*args: str) -> list:
    return ["schtasks.exe", *args]


def _run_schtasks(*args: str) -> "subprocess.CompletedProcess | None":
    """Run ``schtasks.exe`` with the given args. Returns the completed
    process, or ``None`` if ``schtasks.exe`` itself could not be spawned
    (missing/PATH issue) -- never raises."""
    try:
        return subprocess.run(
            _schtasks_argv(*args),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )
    except OSError:
        return None


def _host_sampler_script_path(repo_root: Path) -> Path:
    """Absolute path to the sampler's own source file under ``repo_root``."""
    return repo_root / "coordinator_core" / "telemetry" / "host_sampler.py"


def _task_xml(python_exe: str, repo_root: Path) -> str:
    """Build the full Task Scheduler XML task definition, setting
    ``WorkingDirectory`` NATIVELY (2026-08-16 fix) rather than shelling
    through ``cmd /c cd /d ... &&`` to fake it.

    ``schtasks /Create`` has no simple-flag equivalent of "start-in
    directory" -- that only exists in the Task Scheduler XML schema's
    ``<Actions><Exec><WorkingDirectory>`` element, set via ``schtasks
    /Create /XML <file>``. Using it removes an avoidable ``cmd.exe`` hop on
    a recurring path (CLAUDE.md's Runtime conventions: shell wrapping
    Python is not this repo's pattern) while preserving the exact behaviour
    ``host_sampler.sample_once()`` depends on -- the process's cwd is
    ``repo_root`` at invocation, so its direct-``.git``-check-at-cwd
    resolution (no upward walk, see that module's docstring) still holds.

    The sampler is still invoked by FILE PATH
    (``python <abs-path>\\host_sampler.py``), not ``-m`` -- see
    ``coordinator_core.telemetry.host_sampler``'s docstring
    "Invocation-cost ratchet" for why direct-script invocation is the
    deployed path.

    This XML is data handed to Task Scheduler for IT to parse and exec
    later; it is not a subprocess this module spawns itself. ``LogonType``
    ``InteractiveToken`` mirrors the pre-existing registration's observed
    "Logon Mode: Interactive only" -- no stored password, runs as the
    installing user's already-unlocked session, unchanged behaviour from
    the ``/SC MINUTE /MO`` form this replaces.
    """
    from xml.sax.saxutils import escape

    script = escape(str(_host_sampler_script_path(repo_root)))
    python_exe_esc = escape(str(python_exe))
    repo_root_esc = escape(str(repo_root))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Fires {TASK_NAME} every {_INTERVAL_MINUTES} minutes -- see "
        "coordinator_core/telemetry/host_sampler.py.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <TimeTrigger>\n"
        "      <StartBoundary>2024-01-01T00:00:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <Repetition>\n"
        f"        <Interval>PT{_INTERVAL_MINUTES}M</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT72H</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{python_exe_esc}</Command>\n"
        f'      <Arguments>"{script}"</Arguments>\n'
        f"      <WorkingDirectory>{repo_root_esc}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def register_host_sampler_task(
    repo_root: Path, python_exe: Optional[str] = None
) -> bool:
    """Register (or idempotently refresh) the Windows Task Scheduler entry
    that fires the host sampler every ``_INTERVAL_MINUTES`` minutes.

    Advisory / never raises -- see module docstring's negative-spec. Returns
    True on a confirmed successful registration, False on any skip/failure
    (a False return is informational only; callers must not fail the
    install on it).
    """
    print()
    print("--- Install: host-resource sampler scheduled task (Windows Task Scheduler) ---")

    if not _IS_WINDOWS:
        print(
            "[ADVISORY] not running on Windows — skipping host-sampler task "
            "registration (Task Scheduler is Windows-only; cron wiring is "
            "not yet in scope)."
        )
        return False

    if python_exe is None:
        python_exe = sys.executable

    task_xml = _task_xml(python_exe, repo_root)

    # Task Scheduler XML is documented as UTF-16 (see _task_xml's XML
    # declaration) -- schtasks /Create /XML reads the file's own declared
    # encoding, so it must actually be written that way, not UTF-8-with-a-
    # UTF-16-label. Written to a real temp file (not stdin) because
    # schtasks's /XML flag takes a path, not piped content.
    xml_path = None
    try:
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="host-sampler-task-")
        os.close(fd)
        Path(xml_path).write_text(task_xml, encoding="utf-16", newline="\n")

        proc = _run_schtasks(
            "/Create",
            "/TN", TASK_NAME,
            "/XML", xml_path,
            "/F",
        )
    finally:
        if xml_path is not None:
            try:
                os.remove(xml_path)
            except OSError:
                pass

    if proc is None:
        print(
            "[ADVISORY] schtasks.exe could not be launched — skipping "
            "host-sampler task registration. The sampler module itself is "
            "unaffected; it simply will not be invoked on a schedule."
        )
        return False
    if proc.returncode != 0:
        print(
            f"[ADVISORY] schtasks /Create failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
        print("  Host-sampler task registration skipped; install continues.")
        return False

    print(f"PASS [host-sampler] scheduled task '{TASK_NAME}' registered (every {_INTERVAL_MINUTES}m).")
    return True


def unregister_host_sampler_task() -> bool:
    """Remove the Windows Task Scheduler entry registered by
    ``register_host_sampler_task``, if present.

    Advisory / never raises. Idempotent: an absent task is treated as
    success (nothing to remove), matching the bool contract every other
    ``uninstall_legs`` leg uses. Returns False only on a genuine,
    non-absent-related failure."""
    if not _IS_WINDOWS:
        return True

    proc = _run_schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if proc is None:
        print(
            "[ADVISORY] schtasks.exe could not be launched — could not "
            "confirm removal of host-sampler scheduled task "
            f"'{TASK_NAME}'.",
            file=sys.stderr,
        )
        return False
    if proc.returncode != 0:
        combined = (proc.stderr or proc.stdout or "").strip()
        # schtasks /Delete on an absent task prints "ERROR: The system
        # cannot find the file specified." and exits non-zero -- that is
        # success (nothing to remove), not a failure, matching every other
        # leg's already-absent-is-success contract (e.g. git config --unset
        # exit 5 in this same module).
        if "cannot find" in combined.lower():
            return True
        print(
            f"[ADVISORY] schtasks /Delete failed (exit {proc.returncode}): {combined}",
            file=sys.stderr,
        )
        return False
    return True

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
    - macOS/Linux: plain-file registration (LaunchAgent plist / systemd
      user timer units), no spawned process; active from next login.
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
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.install.substrate import _refuse_machine_mutation
from coordinator_core.win_portability import no_console_creationflags

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


def _plist_label() -> str:
    return "com.coordinator.hostsampler"


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_plist_label()}.plist"


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _launch_agent_plist(python_exe: str, repo_root: Path) -> bytes:
    import plistlib

    return plistlib.dumps(
        {
            "Label": _plist_label(),
            "ProgramArguments": [str(python_exe), str(_host_sampler_script_path(repo_root))],
            "WorkingDirectory": str(repo_root),
            "StartInterval": _INTERVAL_MINUTES * 60,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "LowPriorityIO": True,
        }
    )


def _systemd_units(python_exe: str, repo_root: Path) -> "dict[str, str]":
    script = _host_sampler_script_path(repo_root)
    return {
        "coordinator-host-sampler.service": (
            "[Unit]\nDescription=Coordinator host-resource sampler\n\n"
            "[Service]\nType=oneshot\n"
            f"WorkingDirectory={repo_root}\n"
            f"ExecStart={shlex.quote(str(python_exe))} {shlex.quote(str(script))}\n"
        ),
        "coordinator-host-sampler.timer": (
            "[Unit]\nDescription=Coordinator host-resource sampler cadence\n\n"
            f"[Timer]\nOnBootSec=2min\nOnUnitActiveSec={_INTERVAL_MINUTES}min\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }


def _register_file_based(repo_root: Path, python_exe: Optional[str]) -> bool:
    """macOS LaunchAgent / Linux systemd user timer, written as plain files so
    no ``launchctl``/``systemctl``/``crontab`` process is spawned. Both are
    picked up at next login; nothing is loaded into the running session."""
    from coordinator_core.atomic_replace import atomic_write_bytes

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        print(f"[ADVISORY] no host-sampler scheduler for platform {sys.platform!r}; skipping.")
        return False
    blocked = _refuse_machine_mutation(TASK_NAME, what="register the host-sampler job", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}")
        return False
    python_exe = python_exe or sys.executable
    try:
        if sys.platform == "darwin":
            target = _launch_agent_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, _launch_agent_plist(python_exe, repo_root))
            where = f"LaunchAgent {target}"
        else:
            udir = _systemd_user_dir()
            wants = udir / "timers.target.wants"
            wants.mkdir(parents=True, exist_ok=True)
            for name, text in _systemd_units(python_exe, repo_root).items():
                atomic_write_bytes(udir / name, text.encode("utf-8"))
            link = wants / "coordinator-host-sampler.timer"
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to("../coordinator-host-sampler.timer")
            where = f"systemd user timer in {udir}"
    except OSError as exc:
        print(f"[ADVISORY] host-sampler registration failed ({exc}); install continues.")
        return False
    print(f"PASS [host-sampler] {where} (every {_INTERVAL_MINUTES}m; active from next login).")
    return True


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
    print("--- Install: host-resource sampler scheduled job ---")

    if not _IS_WINDOWS:
        return _register_file_based(repo_root, python_exe)

    blocked = _refuse_machine_mutation(TASK_NAME, what="register the host-sampler scheduled task", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}")
        return False

    if python_exe is None:
        python_exe = sys.executable

    task_xml = _task_xml(python_exe, repo_root)

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


def _unregister_file_based() -> bool:
    blocked = _refuse_machine_mutation(TASK_NAME, what="remove the host-sampler job", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}", file=sys.stderr)
        return False
    udir = _systemd_user_dir()
    paths = [
        _launch_agent_path(),
        udir / "coordinator-host-sampler.service",
        udir / "coordinator-host-sampler.timer",
        udir / "timers.target.wants" / "coordinator-host-sampler.timer",
    ]
    ok = True
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[ADVISORY] could not remove {path}: {exc}", file=sys.stderr)
            ok = False
    return ok


def unregister_host_sampler_task() -> bool:
    if not _IS_WINDOWS:
        return _unregister_file_based()

    blocked = _refuse_machine_mutation(TASK_NAME, what="remove the host-sampler scheduled task", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}", file=sys.stderr)
        return False

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
        if "cannot find" in combined.lower():
            return True
        print(
            f"[ADVISORY] schtasks /Delete failed (exit {proc.returncode}): {combined}",
            file=sys.stderr,
        )
        return False
    return True

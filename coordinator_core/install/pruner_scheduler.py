"""coordinator_core.install.pruner_scheduler -- registers the periodic pruner job.

Purpose: fires ``coordinator_core.install.pruner_runner`` (scratch hygiene and emitted-output
pruning across the registered repos) on a fixed cadence, owned by the OS rather than by any
coordinator process. Same shape as ``host_sampler_scheduler`` -- macOS LaunchAgent plist and Linux
systemd user timer as plain files (no spawn, active from next login), Windows Task Scheduler via
``schtasks.exe /Create /XML`` -- and reuses its ``schtasks`` runner and systemd directory. The
job is its own entry, not folded into the sampler's tick.

Cold path: runs only from the installer and uninstaller.

Negative-spec: never fails the caller -- failures print ``[ADVISORY]`` and return False;
idempotent (a re-register refreshes the one job in place); a redirected install root or the
machine-mutation kill switch writes nothing.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.install.host_sampler_scheduler import _run_schtasks, _systemd_user_dir
from coordinator_core.install.install_root_scope import redirected_install_reason
from coordinator_core.install.substrate import _refuse_machine_mutation
from coordinator_core.temp_layout import managed_tempdir

TASK_NAME = "CoordinatorPruner"
PLIST_LABEL = "com.coordinator.pruner"
RUNNER_MODULE = "coordinator_core.install.pruner_runner"

# Cadence of the pruner tick; each tick is budget-bounded per repo and resumes via the hygiene cursor.
_INTERVAL_MINUTES = 15

_IS_WINDOWS = os.name == "nt" or sys.platform == "win32"

_SERVICE = "coordinator-pruner.service"
_TIMER = "coordinator-pruner.timer"


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def _launch_agent_plist(python_exe: str, repo_root: Path) -> bytes:
    import plistlib

    return plistlib.dumps(
        {
            "Label": PLIST_LABEL,
            "ProgramArguments": [str(python_exe), "-m", RUNNER_MODULE],
            "WorkingDirectory": str(repo_root),
            "StartInterval": _INTERVAL_MINUTES * 60,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
        }
    )


def _systemd_units(python_exe: str, repo_root: Path) -> "dict[str, str]":
    return {
        _SERVICE: (
            "[Unit]\nDescription=Coordinator scratch and emitted-output pruner\n\n"
            "[Service]\nType=oneshot\nNice=10\nIOSchedulingClass=idle\n"
            f"WorkingDirectory={repo_root}\n"
            f"ExecStart={shlex.quote(str(python_exe))} -m {RUNNER_MODULE}\n"
        ),
        _TIMER: (
            "[Unit]\nDescription=Coordinator pruner cadence\n\n"
            f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={_INTERVAL_MINUTES}min\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }


def _task_xml(python_exe: str, repo_root: Path) -> str:
    """Task Scheduler XML; ``WorkingDirectory`` is set natively so ``-m`` resolves against the
    install's repo root. Runs on battery: a low-disk laptop is the case the job exists for."""
    from xml.sax.saxutils import escape

    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>Fires {TASK_NAME} every {_INTERVAL_MINUTES} minutes -- see "
        "coordinator_core/install/pruner_runner.py.</Description>\n"
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
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(str(python_exe))}</Command>\n"
        f"      <Arguments>-m {RUNNER_MODULE}</Arguments>\n"
        f"      <WorkingDirectory>{escape(str(repo_root))}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _redirected_skip() -> bool:
    reason = redirected_install_reason()
    if reason is None:
        return False
    print(
        f"[ADVISORY] pruner job not registered: {reason} "
        "(machine-global job belongs to the default install)"
    )
    return True


def _register_file_based(repo_root: Path, python_exe: Optional[str]) -> bool:
    from coordinator_core.atomic_replace import atomic_write_bytes

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        print(f"[ADVISORY] no pruner scheduler for platform {sys.platform!r}; skipping.")
        return False
    blocked = _refuse_machine_mutation(TASK_NAME, what="register the pruner job", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}")
        return False
    if _redirected_skip():
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
            link = wants / _TIMER
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(f"../{_TIMER}")
            where = f"systemd user timer in {udir}"
    except OSError as exc:
        print(f"[ADVISORY] pruner registration failed ({exc}); install continues.")
        return False
    print(f"PASS [pruner] {where} (every {_INTERVAL_MINUTES}m; active from next login).")
    return True


def register_pruner_task(repo_root: Path, python_exe: Optional[str] = None) -> bool:
    """Register (or idempotently refresh) the periodic pruner job. Advisory: returns True on a
    confirmed registration, False on any skip or failure; never raises."""
    print()
    print("--- Install: periodic pruner job ---")

    if not _IS_WINDOWS:
        return _register_file_based(repo_root, python_exe)

    blocked = _refuse_machine_mutation(TASK_NAME, what="register the pruner scheduled task", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}")
        return False
    if _redirected_skip():
        return False

    task_xml = _task_xml(python_exe or sys.executable, repo_root)
    xml_path = None
    try:
        fd, xml_path = tempfile.mkstemp(
            suffix=".xml", prefix="pruner-task-", dir=managed_tempdir(repo_root)
        )
        os.close(fd)
        Path(xml_path).write_text(task_xml, encoding="utf-16", newline="\n")
        proc = _run_schtasks("/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F")
    except OSError as exc:
        print(f"[ADVISORY] pruner task registration failed ({exc}); install continues.")
        return False
    finally:
        if xml_path is not None:
            try:
                os.remove(xml_path)
            except OSError:
                pass

    if proc is None:
        print("[ADVISORY] schtasks.exe could not be launched; pruner task not registered.")
        return False
    if proc.returncode != 0:
        print(
            f"[ADVISORY] schtasks /Create failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
        return False
    print(f"PASS [pruner] scheduled task '{TASK_NAME}' registered (every {_INTERVAL_MINUTES}m).")
    return True


def _unregister_file_based() -> bool:
    blocked = _refuse_machine_mutation(TASK_NAME, what="remove the pruner job", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}", file=sys.stderr)
        return False
    udir = _systemd_user_dir()
    ok = True
    for path in (
        _launch_agent_path(),
        udir / _SERVICE,
        udir / _TIMER,
        udir / "timers.target.wants" / _TIMER,
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[ADVISORY] could not remove {path}: {exc}", file=sys.stderr)
            ok = False
    return ok


def unregister_pruner_task() -> bool:
    """Remove the pruner job; an absent job is success. Advisory: False on a genuine failure.
    Not gated on a redirected install root (parity with ``unregister_host_sampler_task``)."""
    if not _IS_WINDOWS:
        return _unregister_file_based()

    blocked = _refuse_machine_mutation(TASK_NAME, what="remove the pruner scheduled task", check_temp_path=False)
    if blocked:
        print(f"[ADVISORY] {blocked}", file=sys.stderr)
        return False
    proc = _run_schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if proc is None:
        print(
            f"[ADVISORY] schtasks.exe could not be launched; removal of '{TASK_NAME}' unconfirmed.",
            file=sys.stderr,
        )
        return False
    if proc.returncode != 0:
        combined = (proc.stderr or proc.stdout or "").strip()
        if "cannot find" in combined.lower():
            return True
        print(f"[ADVISORY] schtasks /Delete failed (exit {proc.returncode}): {combined}", file=sys.stderr)
        return False
    return True

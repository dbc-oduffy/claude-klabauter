"""Roll-call far delivery: one detached worker sends the ask to each far addressee.

``launch(record_path)`` starts ``sys.executable -m coordinator_core.group_em.roll_call_deliver
<record_path>`` (closed argv, ``shell=False``, no stdio, never awaited) so the op that opened the
roll call returns inside its budget. The worker calls ``send_pass.send_to_peer`` once per far
addressee (it never raises) and merges each ``{ok, reason}`` into the record through
``roll_call.record_deliveries`` (under ``atomic_record.holder_lock``). A key already delivered
``ok`` is not sent again, so a re-run is safe.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.group_em import roll_call

ADMISSION_KEY = "roll-call-deliver"
ADMISSION_PEAK_GIB = 0.25

Sender = Callable[[str, str, Optional[str]], dict]


def detach_kwargs(os_name: Optional[str] = None) -> dict:
    """Popen keyword arguments that detach the child, per platform."""
    if (os_name or os.name) == "nt":
        # Trap: DETACHED_PROCESS must not be ORed in; Win32 then ignores CREATE_NO_WINDOW.
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        }
    return {"start_new_session": True}


def worker_argv(record_path: Path) -> list:
    """The closed argv the worker runs under."""
    return [sys.executable, "-m", "coordinator_core.group_em.roll_call_deliver", str(record_path)]


def launch(record_path: Path, popen: Any = None, admit: Any = None) -> dict:
    """Admit, then start the delivery worker detached; return at once, never raising.

    Returns ``{launched: True, pid}`` or ``{launched: False, reason}``. ``popen`` and ``admit``
    are test seams (``subprocess.Popen`` and ``admit_before_spawn`` by default).
    """
    try:
        from coordinator_core.bash_guards import _heavy_command_class as capacity

        host = capacity._load_box_memory_host()
        admission = (admit or capacity.admit_before_spawn)(
            ADMISSION_KEY, ADMISSION_PEAK_GIB, host=host, register_pid=os.getpid()
        )
        if not admission.admitted:
            return {"launched": False, "reason": f"spawn held: {admission.reason}"}
        package_root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
        try:
            proc = (popen or subprocess.Popen)(  # noqa: S603 -- closed argv, shell=False
                worker_argv(Path(record_path)),
                cwd=package_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **detach_kwargs(),
            )
            capacity.register_spawn(ADMISSION_KEY, admission.peak_gib, proc.pid, host)
        finally:
            capacity.release_spawn(ADMISSION_KEY, os.getpid(), host)
        return {"launched": True, "pid": proc.pid}
    except Exception as exc:  # noqa: BLE001 -- a launch failure is a reply, never a raise
        return {"launched": False, "reason": f"launch_failed:{type(exc).__name__}:{exc}"}


def deliver(record_file: Path, send: Optional[Sender] = None, now: Optional[float] = None) -> dict:
    """Send the record's ask to each far addressee not yet delivered ``ok``; return the outcomes.

    Each outcome is recorded as soon as its send returns, so ``status`` shows progress.
    ``send`` is ``send_pass.send_to_peer`` by default.
    """
    record_file = Path(record_file)
    roll_call_id = record_file.stem
    home = record_file.parent.parent
    record = roll_call.load_record(roll_call_id, home)
    if record is None:
        return {"ok": False, "reason": "unknown_roll_call"}
    if send is None:
        from coordinator_core.group_em import send_pass

        send = send_pass.send_to_peer
    text = record.get("text", "")
    done = {k for k, v in record.get("deliveries", {}).items() if v.get("ok")}
    outcomes: dict = {}
    for row in record.get("addressees", {}).get("far", []):
        key = row.get("key")
        if not key or key in done:
            continue
        try:
            outcome = send(key, text, None)
        except Exception as exc:  # noqa: BLE001 -- one bad send never stops the rest
            outcome = {"ok": False, "reason": f"send_failed:{type(exc).__name__}:{exc}"}
        outcome = outcome if isinstance(outcome, dict) else {"ok": False, "reason": "bad_reply"}
        outcomes[key] = outcome
        roll_call.record_deliveries(roll_call_id, {key: outcome}, now=now, home=home)
    return {"ok": True, "sent": len(outcomes), "outcomes": outcomes}


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m coordinator_core.group_em.roll_call_deliver <record_path>", file=sys.stderr)
        return 2
    result = deliver(Path(args[0]))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

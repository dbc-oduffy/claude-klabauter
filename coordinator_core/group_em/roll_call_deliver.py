"""Roll-call delivery: one detached worker sends the ask to every local and far addressee.

``launch(record_path)`` starts ``sys.executable -m coordinator_core.group_em.roll_call_deliver
<record_path>`` (closed argv, ``shell=False``, no stdio, never awaited) so the op that opened the
roll call returns inside its budget. The worker delivers each local row through ``deliver_local``
and calls ``send_pass.send_to_peer`` once per reachable far addressee (it never raises), merging each
``{ok, reason}`` into the record through ``roll_call.record_deliveries`` (under
``atomic_record.holder_lock``). A key already delivered ``ok`` is not sent again, so a re-run is safe.
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
LOCAL_SENDER = "coordinator-roll-call"
HANDOFF_WAIT_S = 5.0
_ROUTED_OK = ("delivered", "handed-off", "queued")

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


def deliver_local(
    record: dict,
    append: Optional[Callable] = None,
    route: Optional[Callable] = None,
    resolve: Optional[Callable] = None,
    on_outcome: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """``{session_id: {ok, reason}}`` for every ``record["addressees"]["local"]`` row; never raises.

    A claude row is appended to its agent_pipe mailbox; any other kind is routed through the bridge
    by its bound name. ``on_outcome(key, outcome)`` fires as each send returns; handed-off legs are
    then awaited under one shared deadline so the worker does not exit under a running leg. Writes
    nothing to the record.
    """
    import time
    import uuid
    from datetime import datetime, timezone

    outcomes: dict = {}
    pending: list = []
    text = record.get("text", "")
    rc_id = record.get("roll_call_id") or "roll-call"
    for row in record.get("addressees", {}).get("local", []):
        key = row.get("session_id")
        if not key:
            continue
        try:
            if (row.get("kind") or "claude") == "claude":
                from coordinator_core.agent_pipe import mailbox
                from coordinator_core.agent_pipe.contract import PipeMessage

                msg = PipeMessage(
                    id=uuid.uuid4().hex,
                    sender_agent=LOCAL_SENDER,
                    sender_conversation=rc_id,
                    to_session=key,
                    content=text,
                    ts=datetime.now(timezone.utc).isoformat(),
                )
                (append or mailbox.append)(msg)
                outcome = {"ok": True, "reason": None}
            else:
                from coordinator_core.agent_pipe import bridge, roster

                name = row.get("name") or ""
                if not (resolve or roster.resolve)(name):
                    outcome = {"ok": False, "reason": "no_binding"}
                else:
                    delivery = (route or bridge.route)(bridge.make_message(LOCAL_SENDER, name, text))
                    ok = delivery.status in _ROUTED_OK
                    outcome = {"ok": ok, "reason": None if ok else f"{delivery.status}:{delivery.reason}"}
                    if ok and delivery.status == "handed-off":
                        pending.append(delivery)
        except Exception as exc:  # noqa: BLE001 -- one bad addressee never stops the rest
            outcome = {"ok": False, "reason": f"local_failed:{type(exc).__name__}:{exc}"}
        outcomes[key] = outcome
        if on_outcome is not None:
            try:
                on_outcome(key, outcome)
            except Exception:  # noqa: BLE001 -- recording trouble never stops delivery
                pass
    deadline = time.monotonic() + HANDOFF_WAIT_S
    for delivery in pending:
        try:
            delivery.wait(max(0.0, deadline - time.monotonic()))
        except Exception:  # noqa: BLE001
            pass
    return outcomes


def deliver(record_file: Path, send: Optional[Sender] = None, now: Optional[float] = None, **local_seams: Any) -> dict:
    """Deliver the record's ask to every addressee not yet ``ok``; return the outcomes by key.

    Local rows go through ``deliver_local`` (``local_seams``: append, route, resolve); far rows
    through ``send`` (``send_pass.send_to_peer`` by default); a far row flagged ``unreachable``
    records ``box_unreachable`` without a send. Each outcome is recorded as soon as it returns, so
    ``status`` shows progress.
    """
    record_file = Path(record_file)
    roll_call_id = record_file.stem
    home = record_file.parent.parent
    record = roll_call.load_record(roll_call_id, home)
    if record is None:
        return {"ok": False, "reason": "unknown_roll_call"}
    text = record.get("text", "")
    done = {k for k, v in record.get("deliveries", {}).items() if v.get("ok")}
    outcomes: dict = {}

    def _record(key: str, outcome: dict) -> None:
        outcomes[key] = outcome
        roll_call.record_deliveries(roll_call_id, {key: outcome}, now=now, home=home)

    addressed = record.get("addressees", {})
    local_rows = [r for r in addressed.get("local", []) if r.get("session_id") and r["session_id"] not in done]
    if local_rows:
        deliver_local(
            {"roll_call_id": roll_call_id, "text": text, "addressees": {"local": local_rows}},
            on_outcome=_record,
            **local_seams,
        )
    far_rows = [r for r in addressed.get("far", []) if r.get("key") and r["key"] not in done]
    for row in far_rows:
        if row.get("unreachable"):
            _record(row["key"], {"ok": False, "reason": "box_unreachable"})
    if send is None:
        from coordinator_core.group_em import send_pass

        send = send_pass.send_to_peer
    for row in far_rows:
        key = row["key"]
        if row.get("unreachable"):
            continue
        try:
            outcome = send(key, text, None)
        except Exception as exc:  # noqa: BLE001 -- one bad send never stops the rest
            outcome = {"ok": False, "reason": f"send_failed:{type(exc).__name__}:{exc}"}
        outcome = outcome if isinstance(outcome, dict) else {"ok": False, "reason": "bad_reply"}
        _record(key, outcome)
    return {"ok": True, "sent": len(outcomes), "outcomes": outcomes}


def record_worker_failure(record_file: Path, exc: BaseException, now: Optional[float] = None) -> dict:
    """Stamp every addressee key not yet ``ok`` with ``worker_failed:<type>:<msg>``; never raises."""
    try:
        record_file = Path(record_file)
        roll_call_id = record_file.stem
        home = record_file.parent.parent
        record = roll_call.load_record(roll_call_id, home)
        if record is None:
            return {"ok": False, "reason": "unknown_roll_call"}
        done = {k for k, v in record.get("deliveries", {}).items() if v.get("ok")}
        addressed = record.get("addressees", {})
        keys = [r.get("session_id") for r in addressed.get("local", [])]
        keys += [r.get("key") for r in addressed.get("far", [])]
        reason = f"worker_failed:{type(exc).__name__}:{exc}"[:500]
        failed = {k: {"ok": False, "reason": reason} for k in keys if k and k not in done}
        return roll_call.record_deliveries(roll_call_id, failed, now=now, home=home)
    except Exception as inner:  # noqa: BLE001 -- the failure recorder must not mask the original
        return {"ok": False, "reason": f"record_failed:{type(inner).__name__}:{inner}"}


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m coordinator_core.group_em.roll_call_deliver <record_path>", file=sys.stderr)
        return 2
    try:
        result = deliver(Path(args[0]))
    except Exception as exc:  # noqa: BLE001 -- stdio is DEVNULL; the record is the only trace
        record_worker_failure(Path(args[0]), exc)
        return 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Pending-external-oracle rows for `dispatch.record_halted_run_landed`.

A relayed row whose closing evidence is owned outside the run is held open, never met: an entry names an
`owner`, a `closing_check` and a `requester_citation`. `accepted_partial` is not a state anywhere. Entry
writes one PM-facing flag record per (run, row); the run stays halted and is refired through an ask carrying
`resume_run_id`, never closed while a row is pending; each refusal spools that refire (`spool_refire`; the
`MAX_AUTO_REFIRES` cap is the callers', `halted_run_landed` and `stranded_wait`, while the periodic
`refire_closed` tick spools the one refire past it once a closing check passes).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.win_portability import no_console_creationflags

REQUIRED_FIELDS = ("owner", "closing_check", "requester_citation")

#: Refusal text for the PM-rejected terminal state, in any key or value position.
ACCEPTED_PARTIAL_REFUSAL = "accepted_partial is not a state"

#: One check runs per pending row per call, so a hung oracle must not hold the op past the 500ms
#: brightline; a timeout leaves the row pending.
CLOSING_CHECK_TIMEOUT_S = 0.5

#: Automatic refires spooled per run while a row stays pending; past it the PM flag is the only owner.
MAX_AUTO_REFIRES = 3

#: PM-facing flag records, one file per (run, row), under the main worktree.
PM_FLAGS_DIR = "state/pm-flags"

_CLOSED_SUFFIX = ".closed"

#: Seconds between `on_idle_tick` closing-check passes per repo; a check spawns a process.
TICK_INTERVAL_S = 300.0

_last_tick: dict = {}

_BANNED = "accepted_partial"


def _mentions_banned(value: Any) -> bool:
    if isinstance(value, str):
        return _BANNED in value.lower()
    if isinstance(value, Mapping):
        return any(_mentions_banned(k) or _mentions_banned(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_mentions_banned(v) for v in value)
    return False


def _check_problem(row: str, check: Any) -> Optional[str]:
    if not isinstance(check, Mapping):
        return f"pending_oracle row {row}: closing_check must be an object {{command: argv list, expect: str}}"
    command, expect = check.get("command"), check.get("expect")
    if isinstance(command, str):
        return f"pending_oracle row {row}: closing_check.command must be an argv list, not a string"
    if not isinstance(command, list) or not command or not all(isinstance(a, str) and a for a in command):
        return f"pending_oracle row {row}: closing_check.command must be a non-empty argv list of strings"
    if not isinstance(expect, str) or not expect.strip():
        return f"pending_oracle row {row}: closing_check.expect must be a non-empty string"
    return None


def validate(entries: Any) -> Optional[str]:
    """The refusal text for a `{row id: entry}` map, None when every entry is fully owned and cited."""
    if _mentions_banned(entries):
        return ACCEPTED_PARTIAL_REFUSAL
    if not isinstance(entries, Mapping) or not entries:
        return "pending_oracle must be a non-empty object {row id: entry}"
    for row, entry in entries.items():
        if not isinstance(row, str) or not safe_id(row):
            return f"pending_oracle row id {row!r} is not a safe id"
        if not isinstance(entry, Mapping):
            return f"pending_oracle row {row}: entry must be an object with {', '.join(REQUIRED_FIELDS)}"
        for field in REQUIRED_FIELDS:
            value = entry.get(field)
            if value is None or (isinstance(value, (str, list, dict)) and not value):
                return f"pending_oracle row {row}: missing or empty field {field}"
            if isinstance(value, str) and not value.strip():
                return f"pending_oracle row {row}: missing or empty field {field}"
        problem = _check_problem(row, entry["closing_check"])
        if problem:
            return problem
    return None


def flag_path(root: Path, run_id: str, row: str) -> Path:
    """Where the PM flag for (run_id, row) lives, under the main worktree."""
    return main_worktree_root(Path(root)) / PM_FLAGS_DIR / f"{run_id}.{row}.json"


def write_pm_flag(root: Path, run_id: str, row: str, entry: Mapping[str, Any]) -> Path:
    """Write the PM flag record for (run_id, row) and return its path; a repeat call writes nothing.

    The first write also hands a best-effort notice to the live Group EM (the EM session that fronts the
    PM); the durable record is the file, never the notice. The record carries the refire ask."""
    if not (safe_id(run_id) and safe_id(row)):
        raise ValueError(f"unsafe run id or row id: {run_id!r}, {row!r}")
    path = flag_path(root, run_id, row)
    if path.exists():
        return path
    record = {
        "kind": "pending_oracle",
        "run_id": run_id,
        "row": row,
        "owner": entry.get("owner"),
        "closing_check": entry.get("closing_check"),
        "requester_citation": entry.get("requester_citation"),
        "refire": {"resume_run_id": run_id},
        "needs_pm": True,
    }
    atomic_write_bytes(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    try:
        from coordinator_core.ops.dispatch_emit.halt_split_tree import send_live_gem

        send_live_gem(
            main_worktree_root(Path(root)),
            f"PM flag: run {run_id} row {row} is pending external oracle (owner {entry.get('owner')}); "
            f"requester citation: {entry.get('requester_citation')}. Refire with resume_run_id: {run_id}. "
            f"Record: {path}",
        )
    except Exception:  # noqa: BLE001 -- the flag file is the record; the notice is best-effort
        pass
    return path


def spool_refire(
    root: Path, run_id: str, sizing_rel: str, attempt: int, *, source: str = "pending-oracle", reason: str = "",
) -> bool:
    """Spool the `engine-ask {sizing, resume_run_id}` refire of *run_id*; True when a new entry was written.

    Enqueue only: no drain, mint or spawn runs here; the warm server's drain mints it. *attempt* keys the
    spool id, so a repeat call for the same attempt writes nothing. *source* names the caller and prefixes the
    spool id and session id, so two callers refiring one run never dedupe each other. Never raises."""
    try:
        from coordinator_core.ask_spool import record as spool
        from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_RUN_ID_ARG

        repo = str(Path(main_worktree_root(Path(root))).resolve())
        tool_use_id = f"{source}-{run_id}-{attempt}"
        payload = {
            "tool_name": "Workflow",
            "tool_input": {
                "name": "coordinator:engine-ask",
                "args": {"sizing": sizing_rel, RESUME_RUN_ID_ARG: run_id, "repo": repo},
            },
            "session_id": source,
            "tool_use_id": tool_use_id,
        }
        _, created = spool.spool_ask(
            repo, json.dumps(payload, sort_keys=True).encode("utf-8"),
            session_id=source, tool_use_id=tool_use_id, reason=reason or "pending-oracle: row still open",
        )
        return created
    except Exception:  # noqa: BLE001 -- the PM flag and the refusal still carry the refire ask
        return False


def open_flags(root: Path) -> list:
    """The unresolved PM flag records under the main worktree, oldest name first; a corrupt file is skipped.

    The reader the EM session notice path calls so a pending row reaches the PM durably."""
    flags_dir = main_worktree_root(Path(root)) / PM_FLAGS_DIR
    out: list = []
    try:
        names = sorted(p for p in flags_dir.glob("*.json") if not p.name.endswith(_CLOSED_SUFFIX))
    except OSError:
        return out
    for path in names:
        if path.with_name(path.name[: -len(".json")] + _CLOSED_SUFFIX).exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("kind") == "pending_oracle":
            out.append(record)
    return out


def notice_text(root: Path) -> Optional[str]:
    """One line per open flag for the EM session notice, None when nothing is pending."""
    flags = open_flags(root)
    if not flags:
        return None
    lines = [
        f"pending oracle: run {f.get('run_id')} row {f.get('row')} owned by {f.get('owner')}; "
        f"refire resume_run_id {f.get('run_id')} once its closing check passes"
        for f in flags
    ]
    return "\n".join(lines)


def refire_closed(root: Path) -> list:
    """One periodic tick: run each open flag's closing check once; spool the run's refire for each that passes.

    Returns the `(run_id, row)` pairs refired. A passed row is marked closed beside its flag, so the next
    tick spawns nothing for it. Enqueue only (`spool_refire`); never raises."""
    refired: list = []
    for flag in open_flags(root):
        run_id, row = flag.get("run_id"), flag.get("row")
        if not (isinstance(run_id, str) and isinstance(row, str) and safe_id(run_id) and safe_id(row)):
            continue
        if not closing_check_passes(root, flag.get("closing_check") or {}):
            continue
        try:
            from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_run_landed

            marker = halted_run._read(ask_contract.record_dir(root, run_id)) or {}
            sizing = halted_run_landed._resume_sizing(root, run_id, marker)
        except Exception:  # noqa: BLE001 -- an unreadable run keeps its flag open for the next tick
            continue
        if not sizing:
            continue
        spool_refire(root, run_id, sizing, MAX_AUTO_REFIRES + 1)
        path = flag_path(root, run_id, row)
        try:
            atomic_write_bytes(path.with_name(path.name[: -len(".json")] + _CLOSED_SUFFIX), b"closed\n")
        except OSError:
            continue
        refired.append((run_id, row))
    return refired


def on_idle_tick(repos: Iterable[object], now: Optional[float] = None) -> bool:
    """Run `refire_closed` over each served repo at most once per `TICK_INTERVAL_S`; True when any refire was
    spooled. A repo with no open flag costs one directory listing. Never raises."""
    moment = time.time() if now is None else now
    any_refired = False
    for repo in repos:
        root = Path(str(repo))
        key = str(root)
        if moment - _last_tick.get(key, float("-inf")) < TICK_INTERVAL_S:
            continue
        _last_tick[key] = moment
        try:
            if refire_closed(root):
                any_refired = True
        except Exception:  # noqa: BLE001 -- no pending-oracle failure may escape the idle tick
            pass
    return any_refired


def closing_check_passes(root: Path, check: Mapping[str, Any], timeout: Optional[float] = None) -> bool:
    """Run the check's argv once, no shell, from `root`; True only on exit 0 with `expect` in stdout.

    *timeout* (seconds, capped at `CLOSING_CHECK_TIMEOUT_S`) lets a caller share one deadline across rows;
    a spent deadline spawns nothing. A timeout, a spawn error or non-matching output is False: the row
    stays pending, never a pass."""
    if _check_problem("check", check):
        return False
    limit = CLOSING_CHECK_TIMEOUT_S if timeout is None else min(timeout, CLOSING_CHECK_TIMEOUT_S)
    if limit <= 0:
        return False
    try:
        done = subprocess.run(
            list(check["command"]),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=limit,
            shell=False,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return done.returncode == 0 and check["expect"] in (done.stdout or "")

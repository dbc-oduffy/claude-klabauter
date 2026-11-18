"""Post-land re-run of the importer tripwires a test verdict recorded NOT RUN: one detached process runs `test_leg_run`
at the landed sha, then `react`s: a red (or lost) result reopens the run, adds a ledger row and messages the owner and G-EM."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence

from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import TRIPWIRES_NOT_RUN_KEY

RERUN_RECORD = "tripwire-rerun.json"
_SCRIPT = "coordinator_core/ops/dispatch_emit/tripwire_rerun.py"
SENDER = "tripwire-rerun"
_REDS_SHOWN = 8

#: Test seam: a callable `(owner_session_id, text)` replacing the bridge route.
_OWNER_SENDER = None


def launch(root: Path, run_dir: Optional[Path], landed_sha: str, verdict: Optional[Mapping]) -> Optional[list]:
    """Start one detached `tripwire_rerun` process over exactly `verdict[tripwires_not_run]` at `landed_sha`: it runs
    `test_leg_run` writing `<run_dir>/tripwire-rerun.json`, then reacts to a red result. Returns its argv, or None
    when nothing was launched (no tripwires, no run dir or sha, spawn refused). `--touched` routes the leg through
    the shared-clone mirror at the sha. Never awaited and never raises: the land has already happened."""
    raw = verdict.get(TRIPWIRES_NOT_RUN_KEY) if isinstance(verdict, Mapping) else None
    files = list(dict.fromkeys(f for f in raw if isinstance(f, str) and f)) if isinstance(raw, (list, tuple)) else []
    if not files or run_dir is None or not landed_sha:
        return None
    args = ["--run-id", Path(run_dir).name, "--head-sha", landed_sha]
    for f in files:
        args += ["--touched", f]
    args += ["--out", str(Path(run_dir) / RERUN_RECORD), "--", *files]
    try:
        started = spawn_detached(str(root), _SCRIPT, args)
    except Exception:  # noqa: BLE001 -- the landed commit stands; a lost re-run is not a land failure
        return None
    return args if started else None


def _reds_of(record: Optional[Mapping]) -> list[str]:
    """The reasons `record` reads red, empty when it is a readable green. A missing record is red."""
    if not isinstance(record, Mapping):
        return ["tripwire re-run left no readable record"]
    reds = [str(r) for r in (record.get("new_red") or ())]
    if reds:
        return reds
    if record.get("over_budget"):
        return ["tripwire re-run over budget"]
    if record.get("status") in ("fail", "error"):
        detail = record.get("failing_detail")
        first = detail[0].get("message") if isinstance(detail, list) and detail and isinstance(detail[0], dict) else None
        return [f"tripwire re-run status {record['status']}" + (f": {first}" if first else "")]
    return []


def _clip(text: str, cap: int) -> str:
    return text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")


def _send_owner(owner: str, text: str) -> bool:
    if _OWNER_SENDER is not None:
        _OWNER_SENDER(owner, text)
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    from coordinator_core.agent_pipe import bridge

    bridge.route(bridge.make_message(SENDER, owner, text))
    return True


def react(root: Path, run_id: str, sha: str, record: Optional[Mapping]) -> dict:
    """Act on a finished re-run. A readable green returns `{"red": False}` and writes nothing. Red (a new red, a
    failed or errored status, over budget, or no readable record) reopens the run's marker, appends a ledger row and
    messages the owner session and the live G-EM; each step is independent and never raises. No git spawn."""
    reds = _reds_of(record)
    if not reds:
        return {"red": False}
    out: dict = {"red": True, "reds": reds}
    shown = ", ".join(reds[:_REDS_SHOWN]) + (f" (+{len(reds) - _REDS_SHOWN} more)" if len(reds) > _REDS_SHOWN else "")
    head = f"post-land tripwire re-run red at {sha[:12]}"
    try:
        from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

        halted_run.reopen_red(ask_contract.record_dir(root, run_id), sha, reds)
        out["reopened"] = True
    except Exception as exc:  # noqa: BLE001 -- later steps still run
        out["reopened"] = False
        out["reopen_error"] = repr(exc)
    try:
        from coordinator_core.ops.dispatch_emit import requirements_ledger

        requirements_ledger.revise(
            Path(root), run_id,
            verbatim=_clip(f"{head}: {shown}", requirements_ledger.VERBATIM_BYTE_CAP), sender=SENDER,
        )
        out["ledger_row"] = True
    except Exception as exc:  # noqa: BLE001 -- LedgerRefused included: an unbound run keeps marker and messages
        out["ledger_row"] = False
        out["ledger_error"] = repr(exc)
    text = f"run {run_id}: {head}: {shown}. The run is reopened as halted; the land stands."
    try:
        from coordinator_core.ops.dispatch_emit import halted_run

        owner = halted_run.recorded_emitter(Path(root), run_id)
        out["owner_sent"] = bool(owner) and _send_owner(owner, text)
    except Exception as exc:  # noqa: BLE001
        out["owner_sent"] = False
        out["owner_error"] = repr(exc)
    try:
        from coordinator_core.ops.dispatch_emit import halt_split_tree

        out["gem_sent"] = bool(halt_split_tree.send_live_gem(Path(root), text))
    except Exception as exc:  # noqa: BLE001
        out["gem_sent"] = False
        out["gem_error"] = repr(exc)
    return out


def _value_of(argv: Sequence[str], flag: str) -> Optional[str]:
    head = list(argv[: argv.index("--")] if "--" in argv else argv)
    i = head.index(flag) if flag in head else -1
    return head[i + 1] if 0 <= i < len(head) - 1 else None


def main(argv: Sequence[str]) -> int:
    """Script entry: strip `--run-id`, run `test_leg_run.main` over the rest in-process, then `react` to its `--out`
    record (`None` when the leg raised or wrote none). Exits with the leg's code, 1 when it raised."""
    import json

    from coordinator_core.ops import test_leg_run

    rest = list(argv)
    run_id = None
    if "--run-id" in rest:
        i = rest.index("--run-id")
        run_id = rest[i + 1] if i + 1 < len(rest) else None
        del rest[i:i + 2]
    out_path = _value_of(rest, "--out")
    sha = _value_of(rest, "--head-sha") or ""
    if out_path:
        try:
            Path(out_path).unlink()
        except OSError:
            pass
    code = 1
    try:
        code = test_leg_run.main(rest)
    except Exception:  # noqa: BLE001 -- the lost leg still reacts, as red
        code = 1
    if not run_id:
        return code
    record = None
    if out_path:
        try:
            record = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
    react(test_leg_run._repo_root(Path(os.path.realpath(os.getcwd()))), run_id, sha, record)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

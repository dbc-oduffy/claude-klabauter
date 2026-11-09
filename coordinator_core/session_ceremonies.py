"""One isolated ceremony composition worker. No Claude-harness tools required."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core.agent_pipe import bridge
from coordinator_core.ops.invoke_from_argv import _run_entrypoint

_retry_lock = threading.Lock()
_pending_scan = None
_pending_active = {}


def _write_json(path: Path, value: dict):
    staged = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    try:
        staged.write_text(json.dumps(value, indent=2), encoding="utf-8", newline="\n")
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def _advance(request: dict) -> dict:
    from coordinator_core.ops.session_ceremonies import _run_dir

    prior = _run_dir(request["prior_run_id"])
    original = json.loads((prior / "request.json").read_text(encoding="utf-8"))
    for key in ("session_id", "repo_root", "ceremony"):
        if original[key] != request[key]:
            raise ValueError("prior phase belongs to a different caller, repository or ceremony")
    result = json.loads((prior / "result.json").read_text(encoding="utf-8"))
    envelope = result.get("decision_object") or {}
    original_directives = {row["id"]: row for row in envelope.get("directives", [])}
    # Recompute before any mutation: a persisted brief is evidence, not authority
    # to act on a target whose native directive changed since that brief.
    current = perform({**request, "phase": "brief"})
    if current.get("exit_code") != 0:
        return current
    envelope = current.get("decision_object") or {}
    directives = {row["id"]: row for row in envelope.get("directives", [])}
    points = {row["id"]: row for row in envelope.get("judgment_points", [])}
    from coordinator_core.contract import apply_base
    decisions, malformed = apply_base.normalize_decisions(request.get("decisions", {}))
    if malformed:
        raise ValueError(f"malformed judgment entries {malformed}")
    for key in decisions:
        if key not in points or apply_base.declared_disposition_for(points[key], decisions) is None:
            raise ValueError(f"unknown judgment or invalid disposition for {key}")
    outcomes = []
    landed = set()
    for key in request.get("directive_ids", []):
        if key not in directives:
            raise ValueError(f"unknown native directive {key}")
        directive = directives[key]
        old = original_directives.get(key)
        if old is None or any(old.get(field) != directive.get(field) for field in ("cli", "args", "depends_on")):
            raise ValueError(f"native directive changed since prior phase: {key}")
        open_, blocked = apply_base.directive_gate_open(directive, points, decisions)
        if not open_:
            raise ValueError(f"unresolved judgments gate {key}: {blocked}")
        if directive.get("already_satisfied") or directive.get("discharged_by"):
            continue
        dependencies = directive.get("depends_on") or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if any(dep not in points and dep not in landed and not directives.get(dep, {}).get("already_satisfied") for dep in dependencies):
            raise ValueError(f"unmet directive dependency for {key}")
        cli, argv = directive.get("cli"), directive.get("args", [])
        if not isinstance(cli, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", cli):
            raise ValueError(f"{key} requires a host action, not a native CLI")
        outcome = _run_entrypoint(cli, argv, request["repo_root"])
        outcomes.append({"id": key, **outcome})
        if outcome.get("exit_code") != 0:
            return {"state": "phase-failed", "exit_code": outcome.get("exit_code"),
                    "ceremony_completed": False, "directive_outcomes": outcomes}
        landed.add(key)
    refreshed = perform({**request, "phase": "brief"})
    for directive in refreshed["pending_actions"]["directives"]:
        key = directive.get("id")
        old = directives.get(key, {})
        if key in landed and all(directive.get(field) == old.get(field) for field in ("cli", "args", "depends_on")):
            directive["discharged_by"] = "ceremony-phase:" + request.get("_phase_run_id", "this-phase")
    refreshed["directive_outcomes"] = outcomes
    refreshed["decisions"] = decisions
    refreshed["phase"] = "advance"
    unresolved = refreshed["pending_actions"]["judgment_points"]
    refreshed["pending_actions"]["judgment_points"] = [p for p in unresolved if p.get("id") not in decisions or p != points.get(p.get("id"))]
    refreshed["ceremony_completed"] = _completed(request, refreshed)
    return refreshed


def _completed(request: dict, result: dict) -> bool:
    if result.get("exit_code") != 0 or result.get("workflow_fires") or result.get("pending_workflows"):
        return False
    pending = result.get("pending_actions", {})
    if pending.get("judgment_points") or any(
        not d.get("already_satisfied") and not d.get("discharged_by") for d in pending.get("directives", [])
    ):
        return False
    name, phase = request["ceremony"], request["phase"]
    if name == "workstream_start":
        return phase == "advance"
    if name == "quick_wrap":
        gates = (result.get("decision_object") or {}).get("gates", {})
        entry, close = gates.get("entry_test", {}), gates.get("run_close", {})
        return bool(request.get("work_finished") and "computed_failures" in entry and not entry["computed_failures"]
                    and gates.get("commit_outcome", {}).get("status") in ("committed", "empty")
                    and close.get("verdict") in ("applicable", "not-applicable")
                    and not (set(close.get("outstanding", [])) - {
                        row["id"] for row in result.get("directive_outcomes", []) if row.get("exit_code") == 0
                    }))
    if name in ("pickup", "handoff"):
        return phase == "apply"
    if name in ("workday_start", "workday_complete") and phase == "finish":
        from coordinator_core.ceremony_common.run import ledger, spec
        native = ledger.run_dir(Path(request["repo_root"]), spec.load_spec(name).run_dir_name, request["run_id"])
        return (native / "finished.json").is_file() and not any(row.get("status") == "failed" for row in ledger.read(native))
    return False


def native_invocation(request: dict) -> tuple[str, list[str]]:
    name, phase = request["ceremony"], request["phase"]
    if name in ("workday_start", "workday_complete"):
        argv = [name.replace("_", "-"), phase]
        if phase == "finish":
            argv += ["--run-id", request["run_id"]]
            if request.get("agent_results"):
                argv += ["--agent-results", request["agent_results"]]
        else:
            for key, value in request.get("flags", {}).items():
                if not isinstance(key, str) or not key.startswith("--"):
                    raise ValueError("ceremony flags must be native named flags")
                if value is True:
                    argv.append(key)
                elif value is not False and value is not None:
                    if not isinstance(value, str):
                        raise ValueError("ceremony flag values must be strings or booleans")
                    argv += [key, value]
        return "ceremony-run", argv
    if name == "workstream_start":
        return "orient-assemble", ["brief", "--cadence", "session"]
    if name == "quick_wrap":
        return "quick-wrap-assemble", ["brief"]
    argv = [phase]
    if name == "handoff":
        argv += ["handoff"]
    if request.get("artifact_path"):
        if request["artifact_path"].startswith("-"):
            raise ValueError("artifact_path must not be a flag")
        argv += [request["artifact_path"]]
    if request.get("decisions") is not None:
        argv += ["--decisions", json.dumps(request["decisions"])]
    if name == "handoff" and request.get("title"):
        argv += ["--title", request["title"]]
    if phase != "brief":
        argv += ["--session-id", request["session_id"]]
    return ("pickup-assemble" if name == "pickup" else "baton-assemble"), argv


def _decision_object(stdout: str):
    decoder = json.JSONDecoder()
    fallback = None
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            if any(key in value for key in ("directives", "judgment_points", "gates")):
                return value
            if fallback is None:
                fallback = value
    return fallback


def _fire(script: str, request: dict):
    from coordinator_core.warm.entry_seam import reentrant_dispatch_async

    return asyncio.run(reentrant_dispatch_async("workflow.fire", {
        "script_path": script, "notify_to": request.get("notify_to") or request["session_id"],
    }, repo_root=Path(request["repo_root"])))


def perform(request: dict) -> dict:
    if request["phase"] == "advance":
        return _advance(request)
    entrypoint, argv = native_invocation(request)
    output = _run_entrypoint(entrypoint, argv, request["repo_root"])
    stdout = output.get("stdout", "")
    result = {**output, "ceremony": request["ceremony"], "phase": request["phase"],
              "state": "phase-done" if output.get("exit_code") == 0 else "phase-failed",
              "ceremony_completed": False, "decision_object": _decision_object(stdout)}
    # Native code owns the run and final receipts. Driver exit alone never
    # establishes that the ceremony's decisions and closing gates passed.
    scripts = re.findall(r'Workflow\(\{\s*scriptPath:\s*"([^"]+)"\s*\}\)', stdout)
    result["workflow_fires"] = []
    if scripts and request.get("fire", True):
        for script in scripts:
            fired = _fire(script, request)
            result["workflow_fires"].append(fired)
        result["state"] = "workflow-dispatched"
    elif scripts:
        result["pending_workflows"] = scripts
        result["state"] = "awaiting-workflow"
    decision = result["decision_object"] or {}
    result["pending_actions"] = {
        "judgment_points": decision.get("judgment_points", []),
        "directives": decision.get("directives", []),
        "next_move": decision.get("next_move"),
    }
    # These obligations are durable and visible even if the caller compacts.
    result["completion_requirement"] = (
        "Resolve the native receipt's judgments/directives and inspect its closing gates. "
        "A successful phase or workflow driver does not certify ceremony completion."
    )
    result["ceremony_completed"] = _completed(request, result)
    return result


def _run(run_dir: Path) -> dict:
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    request["_phase_run_id"] = run_dir.name
    try:
        if request.get("admission_key"):
            # A pipe token has no wall deadline: a descheduled admitting parent
            # is not a failed parent. EOF proves the startup writer disappeared.
            if sys.stdin.buffer.readline() != b"admitted\n":
                raise RuntimeError("parent closed ceremony startup before admission")
        result = perform(request)
    except Exception as exc:
        result = {"state": "phase-failed", "exit_code": 1, "ceremony_completed": False,
                  "error": f"{type(exc).__name__}: {exc}"}
    result["run_id"] = run_dir.name
    target = request.get("notify_to")
    # Persist the result before sending a notice: a notice always names readable truth.
    path = run_dir / "result.json"
    _write_json(path, result)
    if target:
        pending = run_dir.parent / "pending-notifications"
        pending.mkdir(exist_ok=True)
        _write_json(pending / (run_dir.name + ".json"), {"run_dir": str(run_dir), "target": target})
        _notify(run_dir, target, pending / (run_dir.name + ".json"))
        result = json.loads(path.read_text(encoding="utf-8"))
    return result


def run(run_dir: Path) -> dict:
    request = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    try:
        return _run(run_dir)
    finally:
        if request.get("admission_key"):
            from coordinator_core.bash_guards._heavy_command_class import release_spawn
            release_spawn(request["admission_key"], os.getpid())


def _notify(run_dir: Path, target: str, marker: Path):
    import psutil

    claim = run_dir / "notify.lock"
    try:
        if claim.exists() and time.time() - claim.stat().st_mtime > 30:
            try:
                owner = json.loads(claim.read_text(encoding="ascii"))
                pid = owner.get("pid", 0) if isinstance(owner, dict) else int(owner)
                witness = owner.get("birth") if isinstance(owner, dict) else None
                try:
                    live = psutil.Process(pid)
                    abandoned = witness is not None and live.create_time() != witness
                except psutil.NoSuchProcess:
                    abandoned = True
                except psutil.AccessDenied:
                    abandoned = False
            except (OSError, ValueError, TypeError):
                abandoned = True
            if abandoned:
                claim.unlink(missing_ok=True)
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "birth": psutil.Process(os.getpid()).create_time()}))
        _notify_claimed(run_dir, target, marker)
    finally:
        claim.unlink(missing_ok=True)


def _notify_claimed(run_dir: Path, target: str, marker: Path):
    path = run_dir / "result.json"
    if not path.is_file():
        from coordinator_core.ops.session_ceremonies import result as read_result
        result = read_result({"run_id": run_dir.name})
        if result.get("state") in ("running", "indeterminate"):
            return
        _write_json(path, result)
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("notified_at"):
        marker.unlink(missing_ok=True)
        return
    if _recipient_ended(target):
        return
    try:
        delivery = bridge.route(bridge.make_message("ceremony.run", target,
            f"ceremony phase finished: run_id={run_dir.name} state={result['state']}; "
            f"ceremony_completed={result.get('ceremony_completed', False)}; read ceremony.result."))
        result["notify_status"] = delivery.status
        if delivery.status in ("refused", "unroutable"):
            result["notify_error"] = getattr(delivery, "reason", None) or delivery.status
        if delivery.status not in ("refused", "unroutable"):
            result["notified_at"] = datetime.now(timezone.utc).isoformat()
            result.pop("notify_error", None)
    except Exception as exc:
        result["notify_error"] = str(exc)
    _write_json(path, result)
    if result.get("notified_at"):
        marker.unlink(missing_ok=True)


def _recipient_ended(target):
    try:
        from coordinator_core.ops.workflow_fire.completion import recipient_ended
        return recipient_ended(target)
    except ImportError:
        return False


def _ended_terminal_notice(value):
    return _recipient_ended(value.get("target")) and (Path(value["run_dir"]) / "result.json").is_file()


def retry_pending_notifications():
    """Discover eight and rotate one active delivery per tick, retaining I/O failures."""
    global _pending_scan
    from coordinator_core.ops.session_ceremonies import _runs
    pending = _runs() / "pending-notifications"
    if not pending.is_dir():
        return False
    if not _retry_lock.acquire(blocking=False):
        return bool(_pending_active)
    try:
        if _pending_scan is None:
            _pending_scan = os.scandir(pending)
        for _ in range(8):
            entry = next(_pending_scan, None)
            if entry is None:
                _pending_scan.close()
                _pending_scan = None
                break
            if entry.name.endswith(".json"):
                marker = Path(entry.path)
                try:
                    value = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("pending marker must be an object")
                    if _ended_terminal_notice(value):
                        continue
                except (OSError, ValueError):
                    pass
                _pending_active.setdefault(marker, None)
        marker = next(iter(_pending_active), None)
        if marker is None:
            return False
        _pending_active.pop(marker, None)
        _pending_active[marker] = None
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("pending marker must be an object")
            if _ended_terminal_notice(value):
                _pending_active.pop(marker, None)
                return bool(_pending_active)
            _notify(Path(value["run_dir"]), value["target"], marker)
        except FileNotFoundError:
            if not marker.exists():
                _pending_active.pop(marker, None)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not marker.exists():
            _pending_active.pop(marker, None)
        return bool(_pending_active)
    except OSError:
        return True
    finally:
        _retry_lock.release()


if __name__ == "__main__":
    run(Path(sys.argv[1]))

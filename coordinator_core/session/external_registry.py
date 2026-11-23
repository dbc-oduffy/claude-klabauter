"""Engine-owned Codex thread bindings, independent of the caller's repository.
Claude EMs get a parallel row under host-sessions/claude (`--provider claude` command hook).

PID plus birth time confirms the observed host process, not an individual thread.
SessionEnd retains an ended tombstone; a crashed host fails the birth-time check.
Never write synthetic records into the Claude harness's own session registry.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
from pathlib import Path
import sys
import time
import uuid

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.locked_write import held_lock

_RMW_TIMEOUT = 0.25
PROVIDERS = ("codex", "agy")


def registry_dir(directory: Path | None = None, *, provider: str = "codex") -> Path:
    if directory is not None:
        return Path(directory)
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    return settings_home() / "host-sessions" / provider


def _search_dirs(directory: Path | None = None) -> list[Path]:
    if directory is not None:
        return [Path(directory)]
    return [settings_home() / "host-sessions" / provider for provider in PROVIDERS]


def _require_thread_lifecycle(payload: dict | None) -> None:
    """Subagent lifecycle payloads carry their parent's SID, not a new thread binding."""
    if payload is not None and (
        payload.get("agent_id") or payload.get("hook_event_name") in ("SubagentStart", "SubagentStop")
    ):
        raise ValueError("subagent lifecycle events cannot register or end a parent thread binding")


def _sid(value) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value.lower():
        raise ValueError("session_id must be a canonical UUID")
    return value.lower()


_ADDRESS = re.compile(r"[a-z0-9][a-z0-9-]{0,19}-codex-[a-z0-9][a-z0-9-]{0,11}-([1-9][0-9]{0,18})")
_AGY_ADDRESS = re.compile(r"[a-z0-9][a-z0-9-]{0,19}-agy-[a-z0-9][a-z0-9-]{0,11}-([1-9][0-9]{0,18})")
_AGY_ROUTER_NAME = re.compile(r"agy-[A-Za-z0-9._-]{1,60}")
_MAX_SEQUENCE = (1 << 63) - 1


def _slug(value: str, cap: int, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:cap].rstrip("-") or fallback


def _valid_address_name(name: object, provider: str) -> bool:
    if not isinstance(name, str) or len(name) > 64:
        return False
    if provider == "codex":
        return _ADDRESS.fullmatch(name) is not None
    if provider == "agy":
        return _AGY_ADDRESS.fullmatch(name) is not None or _AGY_ROUTER_NAME.fullmatch(name) is not None
    return False


def _retained_address(row, sid, provider: str = "codex"):
    return (isinstance(row, dict) and type(row.get("address_version")) is int and
            row.get("address_version") == 1 and row.get("schema_version") == 1 and row.get("provider") == provider and
            row.get("session_id") == sid and isinstance(row.get("box"), str) and bool(row["box"]) and
            _valid_address_name(row.get("name"), provider))


def _allocate_address(directory: Path, cwd: str, *, provider: str = "codex", preferred_name: str | None = None) -> dict:
    """Caller holds record lock; sequence lock is always acquired second."""
    box = socket.gethostname()
    if provider == "agy" and _valid_address_name(preferred_name, "agy"):
        return {"address_version": 1, "name": preferred_name, "box": box}
    pattern = _AGY_ADDRESS if provider == "agy" else _ADDRESS
    counter = directory / ".alias-sequence"
    with held_lock(counter, timeout=_RMW_TIMEOUT, holder_label="external-registry-alias-sequence"):
        try:
            sequence = int(counter.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            sequence = 0
        sequence = max(sequence, 0)
        # Surviving rows prevent reuse even if the sequence file was reset.
        for path in directory.glob("*.json"):
            try:
                name = json.loads(path.read_text(encoding="utf-8")).get("name")
                match = pattern.fullmatch(name) if isinstance(name, str) else None
                if match:
                    sequence = max(sequence, int(match[1]))
            except (OSError, ValueError, AttributeError):
                continue
        if sequence >= _MAX_SEQUENCE:
            raise ValueError("external session alias sequence exhausted")
        sequence += 1
        name = f"{_slug(Path(cwd).name, 20, 'repo')}-{provider}-{_slug(box, 12, 'box')}-{sequence}"
        atomic_write_bytes(counter, f"{sequence}\n".encode("ascii"))
        return {"address_version": 1, "name": name, "box": box}


_NATIVE_CODEX = re.compile(
    r"codex(-(aarch64|arm64|x86_64|x64)-(apple-darwin|unknown-linux-(musl|gnu)|pc-windows-msvc))?")


def _is_codex_host(process) -> bool:
    """True for the Codex host: the native binary (bare or target-triple suffixed) or
    `node .../codex.js`. Never a name prefix match, never arbitrary node."""
    try:
        stem = process.name().lower().removesuffix(".exe")
        if _NATIVE_CODEX.fullmatch(stem):
            return True
        if stem != "node":
            return False
        argv = process.cmdline()
    except Exception:
        return False
    return len(argv) > 1 and Path(str(argv[1]).replace("\\", "/")).name.lower() == "codex.js"


def _is_agy_host(process) -> bool:
    """True for the Agy CLI exe or its daemon (`agy`, `agy-*`, `agy_*`, `antigravity*`)."""
    try:
        stem = process.name().lower().removesuffix(".exe")
    except Exception:
        return False
    return stem == "agy" or stem.startswith(("agy-", "agy_", "antigravity"))


def _find_agy_host(parents):
    """Nearest Agy CLI ancestor, falling back to the nearest Agy daemon ancestor."""
    daemon = None
    for process in parents:
        if _is_agy_host(process):
            try:
                stem = process.name().lower().removesuffix(".exe")
            except Exception:
                stem = ""
            if stem == "agy":
                return process
            if daemon is None:
                daemon = process
    return daemon


def register_current(payload: dict, directory: Path | None = None) -> dict:
    """The sole external binding constructor; only observed Codex or Agy ancestors qualify."""
    _require_thread_lifecycle(payload)
    import psutil

    provider = payload.get("provider") or "codex"
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    raw_sid = payload.get("session_id")
    if raw_sid is None and provider == "agy":
        raw_sid = payload.get("conversationId")
    sid = _sid(raw_sid)
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("cwd must be absolute")
    parents = list(psutil.Process(os.getpid()).parents())
    if provider == "agy":
        host = _find_agy_host(parents)
        if host is None:
            raise ValueError("no observed Agy ancestor")
    else:
        host = None
        for process in parents:
            if _is_codex_host(process):
                host = process
                break
        if host is None:
            raise ValueError("no observed Codex ancestor")
    epoch = int(host.create_time())
    record = {"schema_version": 1, "session_id": sid, "provider": provider, "pid": host.pid,
              "start_epoch": epoch, "cwd": str(Path(cwd).resolve()),
              "name": f"{provider}-{Path(cwd).name}-{sid[:8]}", "observed_at": time.time()}
    # This records supplied SessionStart payload evidence, not proof that a
    # native hook fired. Never substitute requested/env/argv model settings.
    model = payload.get("model")
    if payload.get("hook_event_name") == "SessionStart" and isinstance(model, str) and model.strip():
        record["session_start_model"] = model
        evidence = {"source":"session_start_payload", "observed_at":record["observed_at"]}
        session_source = payload.get("source")
        if isinstance(session_source, str) and session_source.strip():
            evidence["session_source"] = session_source
        turn = payload.get("turn_id")
        if isinstance(turn, str) and turn.strip():
            evidence["turn_id"] = turn
        record["session_start_model_evidence"] = evidence
    from coordinator_core.session.agent_role import classify, observed_agy_launch, observed_cli_launch
    try:
        argv = host.cmdline()
    except (AttributeError, psutil.Error):
        argv = []
    if provider == "agy":
        classify_payload = {**payload, "session_id": sid}
        record["role"] = classify(classify_payload, os.environ, **observed_agy_launch(argv))
    else:
        if argv and Path(argv[0].replace("\\", "/")).stem.lower() == "node":
            argv = argv[1:]  # the launcher script stands in for the codex binary at argv[0]
        record["role"] = classify(payload, os.environ, **observed_cli_launch(argv))
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and Path(transcript).is_absolute():
        record["transcript_path"] = str(Path(transcript))
    parent = payload.get("parent_session_id") or os.environ.get("COORDINATOR_PARENT_SESSION_ID")
    if parent:
        try:
            record["parent_session_id"] = _sid(parent)
        except (ValueError, TypeError, AttributeError):
            pass
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        record["reported_task_title"] = title
    destination = registry_dir(directory, provider=provider)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{sid}.json"
    with held_lock(path, timeout=_RMW_TIMEOUT, holder_label="external-registry-register"):
        prior = None
        # Missing resume fields do not erase previously observed parent/transcript/title evidence.
        try:
            prior = json.loads((destination / f"{sid}.json").read_text(encoding="utf-8"))
            if isinstance(prior, dict) and prior.get("provider") == provider and prior.get("session_id") == sid:
                if prior.get("schema_version") == 1:
                    initial = prior.get("session_start_model_initial") or prior.get("session_start_model")
                    if isinstance(initial, str) and initial.strip():
                        # Historical only: missing resume data never promotes it
                        # back into the fresh observation or its evidence.
                        record["session_start_model_initial"] = initial
                        current = record.get("session_start_model")
                        if current is not None and current != initial:
                            record["session_start_model_conflict"] = True
                    if prior.get("session_start_model_conflict") is True:
                        record["session_start_model_conflict"] = True
                ended = prior.get("ended_at")
                if isinstance(ended, (int, float)) and not isinstance(ended, bool) and math.isfinite(ended) and ended >= record["observed_at"]:
                    record["ended_at"] = ended
                for field in ("transcript_path", "parent_session_id", "reported_task_title", "original_task_title"):
                    if field not in record and field in prior:
                        value = prior[field]
                        valid = (isinstance(value, str) and Path(value).is_absolute()) if field == "transcript_path" else isinstance(value, str)
                        if field == "parent_session_id":
                            try:
                                value = _sid(value)
                            except (ValueError, TypeError, AttributeError):
                                valid = False
                        if valid:
                            record[field] = value
        except (OSError, ValueError):
            pass
        if "session_start_model" in record and "session_start_model_initial" not in record:
            record["session_start_model_initial"] = record["session_start_model"]
        if _retained_address(prior, sid, provider=provider):
            record.update({field:prior[field] for field in ("address_version", "name", "box")})
        else:
            preferred = payload.get("name") if provider == "agy" else None
            record.update(_allocate_address(destination, cwd, provider=provider, preferred_name=preferred))
        atomic_write_bytes(destination / f"{sid}.json", (json.dumps(record) + "\n").encode("utf-8"))
    # The same constructor that all other session writers use remains the only
    # constructor of repository-local coordinator session metadata.
    from coordinator_core.session import core
    try:
        core.ensure_session(sid, cwd=cwd)
    except (OSError, ValueError):
        pass  # Non-git working directories still have valid host bindings.
    return record


def unregister(session_id: str, directory: Path | None = None, *, payload: dict | None = None) -> None:
    _require_thread_lifecycle(payload)
    sid = _sid(session_id)
    for folder in _search_dirs(directory):
        path = folder / f"{sid}.json"
        if not path.is_file():
            continue
        with held_lock(path, timeout=_RMW_TIMEOUT, holder_label="external-registry-end"):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(row, dict) or row.get("schema_version") != 1 or row.get("provider") not in PROVIDERS or row.get("session_id") != sid:
                continue
            row["ended_at"] = time.time()
            atomic_write_bytes(path, (json.dumps(row) + "\n").encode("utf-8"))
            return


def is_ended(session_id: str, directory: Path | None = None) -> bool:
    """Explicit thread terminal evidence overrides a surviving shared host PID."""
    try:
        sid = _sid(session_id)
        for folder in _search_dirs(directory):
            path = folder / f"{sid}.json"
            if not path.is_file():
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            stamp = row.get("ended_at")
            if (row.get("schema_version") == 1 and row.get("provider") in PROVIDERS and
                    row.get("session_id") == sid and isinstance(stamp, (int, float)) and
                    not isinstance(stamp, bool) and math.isfinite(stamp) and stamp > 0):
                return True
        return False
    except Exception:
        return False


def _verified_row(session_id, directory: Path, provider: "str | tuple[str, ...]") -> dict | None:
    try:
        sid = _sid(session_id)
        providers = (provider,) if isinstance(provider, str) else tuple(provider)
        row = json.loads((directory / f"{sid}.json").read_text(encoding="utf-8"))
        if not isinstance(row, dict) or row.get("schema_version") != 1 or row.get("provider") not in providers or row.get("session_id") != sid:
            return None
        if "ended_at" in row:
            return None
        pid, epoch = row.get("pid"), row.get("start_epoch")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(epoch) or epoch <= 0:
            return None
        if not isinstance(row.get("cwd"), str) or not Path(row["cwd"]).is_absolute():
            return None
        from coordinator_core.session import core
        if not core.stable_pid_alive(pid, stored_start_epoch=str(int(epoch))):
            return None
        return row
    except Exception:
        return None


def lookup(session_id: str, directory: Path | None = None) -> dict | None:
    """Return one birth-verified binding; malformed/dead/reused PIDs are absent."""
    try:
        sid = _sid(session_id)
        for folder in _search_dirs(directory):
            row = _lookup_in_dir(sid, folder)
            if row is not None:
                return row
        return None
    except Exception:
        return None


def _lookup_in_dir(sid: str, folder: Path) -> dict | None:
    return _verified_row(sid, folder, PROVIDERS)


def claude_registry_dir(directory: Path | None = None) -> Path:
    return Path(directory) if directory is not None else settings_home() / "host-sessions" / "claude"


_CLAUDE_NODE_ENTRY = frozenset({"cli.js", "claude.js", "claude"})


def _is_claude_host(process) -> bool:
    """The Claude Code host: native `claude(.exe)`, or `node .../cli.js` under a `claude` path.
    Never a name-prefix match, never arbitrary node."""
    try:
        stem = process.name().lower().removesuffix(".exe")
        if stem == "claude":
            return True
        if stem != "node":
            return False
        argv = process.cmdline()
    except Exception:
        return False
    if len(argv) < 2:
        return False
    entry = str(argv[1]).replace("\\", "/").lower()
    return Path(entry).name in _CLAUDE_NODE_ENTRY and "claude" in entry


def _claude_host():
    import psutil
    for process in psutil.Process(os.getpid()).parents():
        if _is_claude_host(process):
            return process
    return None


def register_claude_current(payload: dict, directory: Path | None = None) -> dict:
    """Claude twin of `register_current`: a row only for an observed Claude ancestor, role from the
    SessionStart payload plus env. Subagent lifecycle payloads never write a parent binding."""
    _require_thread_lifecycle(payload)
    sid = _sid(payload.get("session_id"))
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("cwd must be absolute")
    host = _claude_host()
    if host is None:
        raise ValueError("no observed Claude ancestor")
    from coordinator_core.session.agent_role import classify
    record = {"schema_version": 1, "session_id": sid, "provider": "claude", "pid": host.pid,
              "start_epoch": int(host.create_time()), "cwd": str(Path(cwd).resolve()),
              "role": classify({**payload, "hook_event_name": "SessionStart"}, os.environ, native_main=True),
              "observed_at": time.time()}
    destination = claude_registry_dir(directory)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{sid}.json"
    with held_lock(path, timeout=_RMW_TIMEOUT, holder_label="external-registry-register-claude"):
        atomic_write_bytes(path, (json.dumps(record) + "\n").encode("utf-8"))
    return record


def lookup_claude(session_id: str, directory: Path | None = None) -> dict | None:
    """One live, birth-verified Claude row; ended, malformed, dead or reused-PID rows are absent."""
    return _verified_row(session_id, claude_registry_dir(directory), "claude")


def unregister_claude(session_id: str, directory: Path | None = None, *, payload: dict | None = None) -> None:
    _require_thread_lifecycle(payload)
    sid = _sid(session_id)
    path = claude_registry_dir(directory) / f"{sid}.json"
    with held_lock(path, timeout=_RMW_TIMEOUT, holder_label="external-registry-end-claude"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(row, dict) or row.get("schema_version") != 1 or row.get("provider") != "claude" or row.get("session_id") != sid:
            return
        row["ended_at"] = time.time()
        atomic_write_bytes(path, (json.dumps(row) + "\n").encode("utf-8"))


def preserve_task_title(session_id: str, title: str, directory: Path | None = None) -> dict | None:
    """Preserve first observed task title for one live, birth-verified binding.

    A displayed cost/alias title must never replace the actual original task.
    No record is constructed here and no alias/name allocation is performed.
    """
    sid = _sid(session_id)
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    for folder in _search_dirs(directory):
        path = folder / f"{sid}.json"
        if not path.is_file():
            continue
        with held_lock(path, timeout=_RMW_TIMEOUT, holder_label="external-registry-task-title"):
            row = _lookup_in_dir(sid, folder)
            if row is None:
                continue
            original = row.get("original_task_title")
            if isinstance(original, str) and original.strip():
                return row
            row["original_task_title"] = title
            atomic_write_bytes(path, (json.dumps(row) + "\n").encode("utf-8"))
            return row
    return None


def snapshot(directory: Path | None = None) -> dict[str, dict]:
    result = {}
    try:
        for folder in _search_dirs(directory):
            if not folder.is_dir():
                continue
            for path in folder.glob("*.json"):
                row = _lookup_in_dir(path.stem, folder)
                if row is not None:
                    result[row["session_id"]] = row
    except Exception:
        pass
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", choices=("SessionStart", "SessionEnd"), required=True)
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        claude = args.provider == "claude"
        if args.event == "SessionStart":
            (register_claude_current if claude else register_current)(payload)
        else:
            (unregister_claude if claude else unregister)(payload.get("session_id"), payload=payload)
    except Exception as exc:
        print(f"external-session: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

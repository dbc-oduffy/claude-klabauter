"""Bounded JSONL reads from explicit source paths; no databases or prompt output."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from coordinator_core.atomic_replace import atomic_write_bytes
from .model import empty, ingest, thread, warn, attach_parent

BYTE_BUDGET = 4 * 1024 * 1024


def load(path: Path, root: str) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("version") == 2 and state.get("root") == root
                and all(isinstance(state.get(k), dict) for k in ("threads", "seen", "files", "coverage"))
                and isinstance(state.get("warnings"), list)):
            return state
        if state.get("version") == 1 and state.get("root") == root and isinstance(state.get("files"), dict):
            migrated = empty(root)
            migrated["files"] = {p: {"offset": 0, "parent_override": row.get("parent_override")}
                                 for p, row in state["files"].items() if isinstance(row, dict)}
            return migrated  # Reprice known sources incrementally rather than guessing old recorder ownership.
    except (OSError, ValueError, AttributeError):
        pass
    return empty(root)


def save(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(state, separators=(",", ":")) + "\n").encode("utf-8"))


def bind_subagent_stop(state: dict, parent: str, path: Path) -> dict:
    """Use the child's first real metadata and the harness event's ownership.

    The parent event UUID is never substituted for a missing child UUID. A real
    native parent wins over an absent parent field in a later hook/registry view.
    """
    with path.open("rb") as handle:
        header = handle.readline(512 * 1024)
    if not header.endswith(b"\n"):
        raise ValueError("child transcript first metadata exceeds the bounded header read")
    event = json.loads(header)
    metadata = event.get("payload") or {}
    child = metadata.get("id")
    if event.get("type") != "session_meta" or not isinstance(child, str) or str(uuid.UUID(child)) != child.lower():
        raise ValueError("agent transcript has no actual first child session UUID")
    child = child.lower()
    if child == parent:
        raise ValueError("agent transcript identifies the parent, not a child")
    actual_parent = metadata.get("parent_thread_id")
    if actual_parent is None and isinstance(metadata.get("source"), dict):
        actual_parent = metadata["source"].get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
    selected_parent = actual_parent or parent
    source = state["files"].setdefault(str(path.resolve()), {"offset": 0})
    source.update(parent_override=selected_parent, parent_binding_source="SubagentStop agent_transcript_path",
                  observed_child_session_id=child, event_parent_session_id=parent)
    attach_parent(state, thread(state, child), selected_parent)
    return {"child_session_id": child, "parent_session_id": selected_parent,
            "ownership_source": "native first session_meta" if actual_parent else "SubagentStop ownership"}


def advance(state: dict, paths: list[Path], *, byte_budget: int = BYTE_BUDGET) -> dict:
    """Consume complete appended records only, retaining offsets and unique-event sums."""
    remaining = max(0, byte_budget)
    complete = True
    for path in paths:
        key = str(path.resolve())
        source = state["files"].setdefault(key, {"offset": 0})
        try:
            observed = path.stat()
            size = observed.st_size
            identity = [observed.st_dev, observed.st_ino]
            if size < source["offset"] or (source.get("identity") is not None and source["identity"] != identity):
                override = source.get("parent_override")
                source.clear()
                source.update(offset=0, parent_override=override)
                warn(state, "source rotated or truncated; retained priced history and restarted its cursor")
            source["identity"] = identity
            if size <= source["offset"]:
                continue
            if remaining <= 0:
                complete = False
                continue
            with path.open("rb") as handle:
                handle.seek(source["offset"])
                chunk = handle.read(remaining)
            remaining -= len(chunk)
            if source.get("skip_line"):
                cut = chunk.find(b"\n")
                if cut < 0:
                    source["offset"] += len(chunk)
                    complete = False
                    continue
                source["offset"] += cut + 1
                chunk = chunk[cut + 1:]
                source.pop("skip_line", None)
            end = chunk.rfind(b"\n")
            if end < 0:
                if len(chunk) >= byte_budget and source["offset"] + len(chunk) < size:
                    # An oversized prompt/image row must not block all later metrics.
                    source["offset"] += len(chunk)
                    source["skip_line"] = True
                    warn(state, "oversized JSONL record skipped; coverage may be incomplete")
                complete = False
                continue
            for raw in chunk[:end + 1].splitlines():
                try:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        ingest(state, event, source)
                except (ValueError, TypeError, KeyError, AttributeError):
                    warn(state, "malformed metric record ignored")
            source["offset"] += end + 1
            complete &= source["offset"] >= size
        except OSError:
            warn(state, "a registered usage source could not be read")
            complete = False
    state["backlog_complete"] = complete
    return state


def discover(directory: Path, root: str, *, max_files: int = 256, header_budget: int = BYTE_BUDGET) -> tuple[list[Path], bool]:
    """Discover one explicitly selected rollout directory by first-record ancestry.

    A matching cwd/title never joins sessions. Callers needing multiple dates pass
    multiple explicit directories or the engine's registered transcript paths.
    """
    rows = []
    complete = True
    consumed = 0
    try:
        for index, path in enumerate(directory.glob("rollout-*.jsonl")):
            if index >= max_files or consumed >= header_budget:
                complete = False
                break
            with path.open("rb") as handle:
                header = handle.readline(min(512 * 1024, header_budget - consumed))
            consumed += len(header)
            if not header.endswith(b"\n"):
                complete = False
                continue
            try:
                event = json.loads(header)
                data = event["payload"]
                if event.get("type") == "session_meta":
                    parent = data.get("parent_thread_id")
                    if parent is None and isinstance(data.get("source"), dict):
                        parent = data["source"].get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
                    rows.append((path, data.get("id"), parent))
            except (ValueError, KeyError, TypeError, AttributeError):
                complete = False
    except OSError:
        return [], False
    selected = {root}
    while True:
        added = {sid for _, sid, parent in rows if parent in selected}
        if added <= selected:
            break
        selected |= added
    return [path for path, sid, _ in sorted(rows, key=lambda row: row[1] != root) if sid in selected], complete


def registered_paths(directory: Path, root: str, state: dict | None = None) -> tuple[list[Path], bool]:
    """Read engine-owned host bindings, including retired threads, without liveness filtering."""
    rows = []
    budget = BYTE_BUDGET
    complete = True
    try:
        for index, file in enumerate(directory.glob("*.json")):
            if index >= 512:
                complete = False
                break
            try:
                row = json.loads(file.read_text(encoding="utf-8"))
                raw = row.get("transcript_path")
                if row.get("schema_version") == 1 and row.get("provider") == "codex" and isinstance(raw, str) and Path(raw).is_absolute():
                    if budget <= 0:
                        complete = False
                        break
                    path = Path(raw)
                    with path.open("rb") as handle:
                        header = handle.readline(min(512 * 1024, budget))
                    budget -= len(header)
                    if not header.endswith(b"\n"):
                        complete = False
                        continue
                    event = json.loads(header)
                    metadata = event.get("payload") or {}
                    if event.get("type") != "session_meta" or metadata.get("id") != row.get("session_id"):
                        complete = False
                        continue
                    parent = metadata.get("parent_thread_id") or row.get("parent_session_id")
                    if parent is None and isinstance(metadata.get("source"), dict):
                        parent = metadata["source"].get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id")
                    rows.append((path, metadata["id"], parent))
            except (OSError, ValueError, AttributeError):
                complete = False
    except OSError:
        return [], False
    selected = {root}
    while True:
        added = {sid for _, sid, parent in rows if parent in selected}
        if added <= selected:
            break
        selected |= added
    if state is not None:
        for path, sid, parent in rows:
            if sid in selected:
                source = state["files"].setdefault(str(path.resolve()), {"offset": 0})
                if parent is not None:
                    source["parent_override"] = parent
    return [path for path, sid, _ in sorted(rows, key=lambda row: row[1] != root) if sid in selected], complete

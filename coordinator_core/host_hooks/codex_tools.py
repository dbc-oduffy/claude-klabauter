"""Bounded native patch projections for the shared write guard engine.

This is not an apply_patch implementation. Only unique exact hunks are
reconstructed; native fuzzy matching remains the native tool's responsibility.
Unknown bodies still receive path guards and a truthful coverage advisory.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_PREIMAGE = 256 * 1024
MAX_PATCH = 1024 * 1024
MAX_TARGETS = 32


def _target(path: str, cwd: str) -> str:
    target = Path(path)
    # The resident engine's process cwd belongs to another caller. Never use it
    # to resolve a relative native write target.
    if not target.is_absolute() and cwd:
        target = Path(cwd) / target
    return str(target)


def _preimage(path: str, cwd: str) -> str | None:
    try:
        target = Path(path)
        if not target.is_absolute():
            target = Path(cwd) / target
        with target.open("rb") as stream:
            data = stream.read(MAX_PREIMAGE + 1)
        if len(data) > MAX_PREIMAGE:
            return None
        # Match the shared engine's universal-newline text preimage; these are
        # guard projections, never the arguments sent to the native writer.
        return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError, ValueError):
        return None


def _updated(before: str | None, lines: list[str]) -> str | None:
    if before is None or (before and not before.endswith(("\n", "\r"))):
        return None
    # Preserve the file's existing line endings. A missing final newline is
    # intentionally ambiguous unless the patch explicitly marks end of file.
    newline = "\r\n" if "\r\n" in before else "\n"
    current = before.splitlines()
    hunks: list[list[str]] = []
    for line in lines:
        if line.startswith("@@"):
            hunks.append([])
        elif line == "*** End of File":
            continue
        elif line[:1] in (" ", "+", "-"):
            if not hunks:
                hunks.append([])
            hunks[-1].append(line)
        else:
            return None
    cursor = 0
    for hunk in hunks:
        old = [line[1:] for line in hunk if line[0] != "+"]
        new = [line[1:] for line in hunk if line[0] != "-"]
        if not old:
            return None  # position depends on native anchors/fuzzy semantics
        candidates = [i for i in range(cursor, len(current) - len(old) + 1)
                      if current[i:i + len(old)] == old]
        if len(candidates) != 1:
            return None
        start = candidates[0]
        current[start:start + len(old)] = new
        cursor = start + len(new)
    if not hunks:
        return None
    return newline.join(current) + (newline if before.endswith(("\n", "\r")) else "")


def patch_events(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Project every native patch target, returning events and coverage warnings."""
    args = payload.get("tool_input") or {}
    patch = args.get("command") if isinstance(args, dict) else None
    if not isinstance(patch, str) or len(patch) > MAX_PATCH:
        return [], ["Patch guard could not inspect absent/oversize native patch (1 MiB limit)."]
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return [], ["Patch guard could not project native patch framing."]
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    index = 1
    while index < len(lines) - 1:
        if len(events) >= MAX_TARGETS:
            warnings.append("Patch guard target limit (32) reached; remaining targets were not inspected.")
            break
        header = lines[index]
        kind = next((kind for kind in ("Add", "Update", "Delete")
                     if header.startswith(f"*** {kind} File: ")), None)
        if kind is None:
            warnings.append("Patch guard encountered an unrecognized file header.")
            break
        path = header.split(": ", 1)[1]
        path = _target(path, str(payload.get("cwd") or ""))
        index += 1
        destination = None
        if index < len(lines) and lines[index].startswith("*** Move to: "):
            destination = lines[index].split(": ", 1)[1]
            destination = _target(destination, str(payload.get("cwd") or ""))
            index += 1
        body: list[str] = []
        while index < len(lines) - 1 and not lines[index].startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            body.append(lines[index])
            index += 1
        if not payload.get("cwd") and any(not Path(p).is_absolute() for p in (path, destination) if p):
            warnings.append("Patch guard requires caller cwd for relative targets; daemon cwd was not used.")
            continue
        base = {k: v for k, v in payload.items() if k != "tool_input"}
        base["origin_tool_name"] = "apply_patch"
        before = _preimage(path, str(payload.get("cwd") or "")) if kind == "Update" else None
        after = ("\n".join(line[1:] for line in body) + "\n"
                 if kind == "Add" and all(line.startswith("+") for line in body)
                 else _updated(before, body) if kind == "Update" else None)
        if kind == "Add" and after is not None:
            events.append(dict(base, tool_name="Write", tool_input={"file_path": path, "content": after}))
        else:
            args = {"file_path": path}
            if kind == "Update" and after is not None and before and not destination:
                args.update(old_string=before, new_string=after)
            events.append(dict(base, tool_name="Edit", tool_input=args))
        if destination:
            target = {"file_path": destination}
            if after is not None:
                target["content"] = after
            events.append(dict(base, tool_name="Write", tool_input=target))
        if after is None:
            warnings.append(f"Patch guard checked target path {path!r}; prospective body is unavailable ({kind.lower()} or ambiguous hunks).")
    _share_projections(events)
    return events, warnings


def _share_projections(events: list[dict[str, Any]]) -> None:
    """Give every event of a multi-file patch the whole patch's projected files, so the write budget
    prices the patch once with true file and line totals instead of once per file."""
    projections = []
    for index, event in enumerate(events):
        event["patch_event"] = index
        args = event["tool_input"]
        content = args.get("content") if event["tool_name"] == "Write" else args.get("new_string")
        if isinstance(content, str):
            projections.append({"file_path": args["file_path"], "content": content, "event": index})
    if len(projections) > 1:
        for event in events:
            event["patch_projections"] = projections
    else:
        for event in events:
            event.pop("patch_event", None)

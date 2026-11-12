"""Model-free structure client: the atlas's dead-paths need from example-retrieval-repo, as structure.json.

Only a batched `project_doc_links` door the cost record names (`needs.dead-paths.batched_door`)
and marks `cost_class: within` is ever called, once per run. Without one the need is written
covered:false and nothing is called, so example-retrieval-repo stays optional (the tree census and
cartography remain the fallback). Never a per-page call, never a daemon start or wait.
Imports stdlib and this package only.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from coordinator_core.architecture_survey import contract

DEAD_RESOLUTIONS: frozenset[str] = frozenset({"target_missing", "unresolved"})
PENDING_REASON = "batched-doc-links-door-pending"
OVER_BAR_REASON = "door-over-bar"
NOT_FETCHED_REASON = "not-fetched-by-this-client"

Post = Callable[[str, dict[str, Any], float], dict[str, Any]]


class StructureUnavailable(Exception):
    """example-retrieval-repo cannot answer for this repo now: daemon unreachable, or it answered for another project."""


def _address(costs: dict[str, Any]) -> str:
    """Resolve `${VAR:-default}` in the first token of transport.address_rule."""
    rule = str(costs["transport"]["address_rule"]).split()[0]
    return re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: os.environ.get(m.group(1)) or m.group(2), rule)


def _default_post(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """POST one JSON-RPC tools/call; return the tool envelope parsed from the SSE `data:` line."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - loopback address from the cost record
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 503:
            raise StructureUnavailable("rag daemon unreachable (503)") from exc
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(str(exc.reason)) from exc
        raise StructureUnavailable(f"rag daemon unreachable ({exc.reason})") from exc
    for line in body.splitlines():
        if line.startswith("data:"):
            body = line[5:].strip()
            break
    return json.loads(json.loads(body)["result"]["content"][0]["text"])


def _call(post: Post, url: str, door: str, arguments: dict[str, Any], timeout_ms: float) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": door, "arguments": arguments},
    }
    return post(url, payload, timeout_ms / 1000.0)


def _degraded(envelope: dict[str, Any]) -> bool:
    verdict = envelope.get("verdict")
    return bool(envelope.get("degraded")) or (verdict is not None and verdict != "ok")


def _need(covered: bool, door: str | None, calls: int, reason: str | None, payload: Any) -> dict[str, Any]:
    return {"covered": covered, "door": door, "calls": calls, "reason": reason, "payload": payload}


def _uncalled_need(name: str, costs: dict[str, Any]) -> dict[str, Any]:
    spec = costs.get("needs", {}).get(name, {})
    door = spec.get("door")
    reason = spec.get("reason") or NOT_FETCHED_REASON
    if spec.get("covered") and door and costs.get("doors", {}).get(door, {}).get("cost_class") == "over-bar":
        return _need(False, None, 0, f"{OVER_BAR_REASON}: {reason}", None)
    if spec.get("covered"):
        return _need(False, None, 0, f"{NOT_FETCHED_REASON}: {reason}", None)
    return _need(False, None, 0, reason, None)


def _batched_door(costs: dict[str, Any]) -> str | None:
    """The batched dead-paths door, only when the cost record names it and marks it within the bar."""
    name = costs.get("needs", {}).get("dead-paths", {}).get("batched_door")
    if name and costs.get("doors", {}).get(name, {}).get("cost_class") == "within":
        return str(name)
    return None


def _same_root(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _inside(cwd: str | Path, root: str | Path) -> bool:
    c, r = os.path.normcase(os.path.realpath(cwd)), os.path.normcase(os.path.realpath(root))
    return c == r or c.startswith(r.rstrip(os.sep) + os.sep)


def _identity(post: Post, url: str, repo_root: str, costs: dict[str, Any]) -> str | None:
    """Prove rag answers for repo_root; return the project name its envelope states, if any."""
    binding = costs["transport"]["project_binding"]
    if binding.get("rule") == "cwd" and not _inside(os.getcwd(), repo_root):
        raise StructureUnavailable("working directory is outside repo_root; rag binds by cwd")
    door = binding["identity_door"]
    timeout_ms = costs.get("doors", {}).get(door, {}).get("timeout_ms", 1000)
    envelope = _call(post, url, door, {}, timeout_ms)
    node: Any = envelope
    for part in binding["identity_field"].split("."):
        node = node.get(part) if isinstance(node, dict) else None
    if not isinstance(node, str) or not _same_root(node, repo_root):
        raise StructureUnavailable("rag answered for another project")
    return (envelope.get("provenance") or {}).get("source_name")


def _dead_rows(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    data = envelope.get("data") or {}
    blocks = data.get("results") if isinstance(data.get("results"), list) else [data]
    rows: list[dict[str, Any]] = []
    for block in blocks:
        for link in block.get("outbound") or []:
            if link.get("resolution") in DEAD_RESOLUTIONS:
                rows.append({
                    "page": block.get("file"),
                    "target": link.get("target"),
                    "link_kind": link.get("link_kind"),
                    "resolution": link.get("resolution"),
                })
    return rows


def _dead_paths(
    post: Post, url: str, door: str, atlas_pages: list[str], costs: dict[str, Any], source_name: str | None
) -> dict[str, Any]:
    timeout_ms = costs["doors"][door].get("timeout_ms", 1000)
    try:
        envelope = _call(post, url, door, {"paths": list(atlas_pages)}, timeout_ms)
    except (TimeoutError, OSError, ValueError, KeyError) as exc:
        return _need(False, door, 1, f"door-failed: {type(exc).__name__}", None)
    named = (envelope.get("provenance") or {}).get("source_name")
    if source_name and named and named != source_name:
        raise StructureUnavailable("rag answered for another project")
    if _degraded(envelope):
        return _need(False, door, 1, "door-degraded", None)
    return _need(True, door, 1, None, {"dead": _dead_rows(envelope)})


def _run_id(run_dir: Path) -> str:
    if re.match(r"^[0-9]{8}T[0-9]{6}Z", run_dir.name):
        return run_dir.name
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fetch(
    repo_root: str | Path,
    run_dir: str | Path,
    atlas_pages: list[str],
    *,
    costs: dict[str, Any],
    post: Post | None = None,
) -> dict[str, Any]:
    """Write `<run_dir>/structure.json` and return it; raise StructureUnavailable when rag cannot answer.

    dead-paths is the only need fetched, and only through a batched door `costs` marks within the
    bar; every other need, and dead-paths without that door, is written covered:false with its
    reason and no call. An unreachable daemon or a foreign project raises; a failed or degraded
    dead-paths call is written covered:false. Never retries.
    """
    run_dir = Path(run_dir)
    door = _batched_door(costs)
    dead = _need(False, None, 0, PENDING_REASON, None)
    if door is not None:
        post_fn = post or _default_post
        url = _address(costs)
        try:
            source_name = _identity(post_fn, url, str(repo_root), costs)
        except (TimeoutError, OSError, ValueError, KeyError) as exc:
            raise StructureUnavailable(f"identity probe failed: {type(exc).__name__}") from exc
        dead = _dead_paths(post_fn, url, door, atlas_pages, costs, source_name)
    needs = {n: _uncalled_need(n, costs) for n in contract.NEEDS}
    needs["dead-paths"] = dead
    structure = {
        "run_id": _run_id(run_dir),
        "index": {"commit": "unstated", "commits_behind": 0, "state": "behind", "dropped_paths": 0, "unindexed": []},
        "needs": needs,
        "systems": [],
    }
    errors = contract.validate_structure(structure)
    if errors:
        raise ValueError("invalid structure: " + "; ".join(errors))
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "structure.json").write_text(json.dumps(structure, indent=2) + "\n", encoding="utf-8")
    return structure

"""coordinator_core.hooks.rewrite_sendmessage_peer_name -- PreToolUse(SendMessage)
advisory rewrite of a near-miss peer name to the one live session it means.

The harness refuses a `to` that is not an exact live session name. When `to` names no
live session but exactly one live session matches it by an added repo prefix
(`claude-klabauter-c8` -> `claude-klabauter-c8`), a dropped prefix, or a dash-bounded suffix, `to` is
rewritten via `updatedInput` (every other input field kept) with one advisory line.
Zero or several matches, an exact live name, or any unreadable input pass through
untouched. This hook never denies.

Roster: one `harness_registry.snapshot()` read per call, names via
`reachability.resolve_candidates` -- not cwd-filtered, since a peer in another repo is a
legitimate target. No subprocess, no cache.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name`, `tool_input.to`).

Hook wiring: PreToolUse, matcher `SendMessage`, op `hooks.rewrite_sendmessage_peer_name`.

Entrypoints: check(payload) -> Optional[envelope]; main() -- stdin JSON, stdout JSON.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import no_advisory, payload_of, rewrite_input
from coordinator_core.ipc import register_op

GUARD_NAME = "rewrite-sendmessage-peer-name"
CLASS = "advisory"
MATCHERS = ("SendMessage",)


def _match_peer_name(to: str, candidates: list) -> Optional[Any]:
    """The single live candidate `to` near-misses, else None.

    None when `to` is exactly a live name or address, or when zero or several
    candidates match. A match is `to` plus a dash-bounded prefix, `to` minus one, or a
    dash-bounded suffix of a live name.
    """
    if any(to == c.name or to == c.address for c in candidates):
        return None
    hits = [
        c
        for c in candidates
        if c.name.endswith("-" + to) or to.endswith("-" + c.name)
    ]
    return hits[0] if len(hits) == 1 else None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    to = tool_input.get("to")
    if not isinstance(to, str) or not to.strip():
        return None
    to = to.strip()
    try:
        from coordinator_core.session import harness_registry, reachability

        candidates = reachability.resolve_candidates(harness_registry.snapshot())
    except Exception:
        return None
    hit = _match_peer_name(to, candidates)
    if hit is None:
        return None
    return rewrite_input(
        "PreToolUse",
        {**tool_input, "to": hit.name},
        f"PEER NAME MAPPED: {to!r} -> {hit.name!r}.",
    )


@register_op("hooks.rewrite_sendmessage_peer_name")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        return check(payload_of(params)) or no_advisory()
    except Exception:
        return no_advisory()


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    envelope = check(payload)
    if envelope:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""coordinator_core.hooks.observe_post_compact — PostCompact hook, OBSERVE
ONLY, payload-agnostic by hard constraint.

Port of: DoE-claude `coordinator/hooks/scripts/observe-post-compact.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

`PostCompact` has never been empirically fired on any harness version — its
payload is INFERRED from adjacent bundle strings, not measured. Reading a
guessed field name that silently comes back absent is exactly the failure
this handler exists to avoid, so it does the opposite: it never reads a
named field out of the payload at all — it records the payload dict whole,
verbatim, into the observation log. That makes it correct without knowing
the real shape, and it is the cheapest available instrument for the open
reachability question. If it never fires, the empty log is itself the
measurement that has been missing.

Do NOT "fix" this handler later by adding field reads for a guessed summary
or trigger key until a real fired payload has actually been captured and
inspected. That is precisely the guessed-field-name failure this shape is
built to avoid.

Contract: always returns `no_advisory()` — this hook injects no context; its
only product is the log line. Never raises. Malformed input or an
unwritable log directory must never propagate.

Where the record goes:
`<git-common-dir>/coordinator-sessions/hook-observations/PostCompact.jsonl`
(one JSON object per line), same DR-091 rationale and same
`coordinator_core.hooks.support.git_common_dir` resolution as
`observe_config_change.py` (its sibling in the DoE source, ported alongside
it in this same chunk).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.ipc import register_op

_EVENT_NAME = "PostCompact"


def _find_git_common_dir(start: Path) -> Optional[Path]:
    """Walk up from `start` to the nearest `.git`, then resolve its common
    dir via the shared support helper. Returns None on any failure — never
    raises."""
    try:
        probe = start.resolve()
    except Exception:
        return None

    git_root = None
    for candidate in (probe, *probe.parents):
        try:
            if (candidate / ".git").exists():
                git_root = candidate
                break
        except Exception:
            return None
    if git_root is None:
        return None

    common = resolve_git_common_dir(str(git_root))
    return Path(common) if common else None


def _append_record(git_common_dir: Path, record: dict) -> None:
    try:
        log_dir = git_common_dir / "coordinator-sessions" / "hook-observations"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with (log_dir / f"{_EVENT_NAME}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        return


def run(payload: Optional[dict]) -> None:
    """Core logic — takes an already-parsed payload (dict) or None on parse
    failure. No named-field access anywhere below — the whole point of this
    handler. Never raises."""
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if payload is None:
        record = {
            "observed_at": observed_at,
            "hook_event_name": _EVENT_NAME,
            "parse_error": True,
        }
        cwd_hint = Path.cwd()
    else:
        record = {
            "observed_at": observed_at,
            "hook_event_name": _EVENT_NAME,
            "payload": payload,
        }
        cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
        cwd_hint = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()

    git_common_dir = _find_git_common_dir(cwd_hint)
    if git_common_dir is None:
        return  # fail-open — no resolvable git tree, nothing to write into

    _append_record(git_common_dir, record)


@register_op("hooks.observe_post_compact")
def _handler(params: dict, repo_root=None) -> dict:
    """IPC/dispatch_message adapter over `run()`. `params` IS the raw
    PostCompact payload dict — dumped back out whole, no per-field access
    (see module docstring).

    Always returns `no_advisory()` — this event's output is not surfaced to
    the model.
    """
    try:
        run(params if isinstance(params, dict) else None)
    except Exception:
        pass
    return no_advisory()

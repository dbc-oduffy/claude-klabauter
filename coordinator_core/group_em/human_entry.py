"""
coordinator_core.group_em.human_entry -- proof that a session's Group EM entry was a human-typed
`/group-em`, read from the session's own transcript.

Ported from DoE `coordinator/hooks/scripts/_group_em_human_entry.py` (same names, same return
shape, so that copy can forward here). The transcript path is derived from the session id and
never accepted from a caller. Only a `type: user` line with `origin.kind: human` and STRING
`message.content` carrying the slash-command tag counts: tool results, cross-session deliveries
and Skill expansions arrive as list content or a non-human origin. Fails closed: None means
"not verified".
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from coordinator_core.atomic_replace import atomic_write_bytes

#: Generator-provenance declaration (coordinator_core/ops/generator_census). The verdict cache lands
#: beside the nomination record (`nomination._verdict_cache_path`), under `settings_home()`: an
#: operator-home cache, never a repo artifact. Same disposition as `nomination.py`.
GENERATES = []

REFUSAL = "GROUP-EM-NOT-HUMAN-ENTERED"
REFUSAL_MESSAGE = f"{REFUSAL}: no human-typed /group-em in this session's transcript; type /group-em yourself"

_COMMAND_MARKERS = (
    "<command-name>/group-em</command-name>",
    "<command-name>/coordinator:group-em</command-name>",
)


PENDING_WINDOW_SECONDS = 60.0

# A harness session id is a UUID; anything outside this alphabet (separators, `..`, glob
# metacharacters) could make the transcript glob match ANOTHER session's file.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and bool(_SESSION_ID_RE.match(session_id))


def _parse_iso(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_FINAL_STATUSES = ("verified", "rejected")

HOME_START = "home-start"
HOME_START_REFUSAL_MESSAGE = (
    f"{REFUSAL}: this session did not start interactively in the group-em home; "
    "type /group-em yourself"
)

# A `-p`/SDK launch records `sdk-cli`; only an interactive terminal launch records `cli`, so a
# headless spawn in the home cannot claim the PM's start.
_INTERACTIVE_ENTRYPOINTS = frozenset({"cli"})

_CWD_NEEDLE = b'"cwd"'

_Key = tuple[str, "str | None", "str | None"]


def _evidence_key(record: dict) -> _Key | None:
    """`(session_id, prompt_id, kind)` the verdict is keyed on; a None prompt_id with a None kind
    selects the legacy predicate. A home-start record carries its `claimed_at` in the second slot
    so a re-claim is judged afresh, and its kind in the third so it can never collide with the
    legacy key. None when the session id is unusable (the record can never verify)."""
    session_id = record.get("session_id")
    if not valid_session_id(session_id):
        return None
    evidence = record.get("entry_evidence")
    if isinstance(evidence, dict) and evidence.get("kind") == HOME_START:
        claimed_at = evidence.get("claimed_at")
        return session_id, claimed_at if isinstance(claimed_at, str) else "", HOME_START
    prompt_id = evidence.get("prompt_id") if isinstance(evidence, dict) else None
    if isinstance(prompt_id, str) and prompt_id:
        return session_id, prompt_id, None
    # Records written before claim-time prompt_id capture (no `entry_evidence`, or the earlier
    # `{uuid, transcript, timestamp}` shape). Their holders may be unable to re-enter, so they
    # verify on the original predicate: ANY human /group-em in the holder's own transcript.
    # Still unforgeable by a tool/CLI caller -- the transcript path derives from the session id.
    return session_id, None, None


def _read_cache(cache_path: Path | None, key: _Key) -> dict | None:
    if cache_path is None:
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if key[2] == HOME_START:
        stored = [data.get("session_id"), data.get("claimed_at"), data.get("kind")]
    else:
        stored = [data.get("session_id"), data.get("prompt_id"), data.get("kind")]
    if stored != list(key):
        return None
    return data


def _write_cache(cache_path: Path | None, key: _Key, verdict: dict, offsets: dict) -> None:
    """Best-effort atomic write; a lost write only costs a later reader one more tail scan.

    Lock-free by construction: `offsets` only ever cover bytes already scanned WITHOUT a match,
    so a stale pending write landing over a final verdict re-finds the entry on the next tail
    scan. A pending write also declines to replace a final verdict it can see."""
    if cache_path is None:
        return
    if verdict["status"] == "pending":
        prior = _read_cache(cache_path, key)
        if prior is not None and prior.get("status") in _FINAL_STATUSES:
            return
    payload = {"session_id": key[0], "offsets": offsets, **verdict}
    if key[2] is None:
        payload["prompt_id"] = key[1]
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(cache_path, json.dumps(payload).encode("utf-8"), preserve_mode=False)
    except OSError:
        pass


def resolve_entry_evidence(
    record: dict, now: float, projects_root: Path | None = None, cache_path: Path | None = None
) -> dict:
    """Read-time verdict on a nomination record's entry evidence.

    Returns `{status: verified|pending|rejected, ...}`. Verified iff the record's OWN session
    transcript holds a human `/group-em` entry whose `promptId` equals the recorded `prompt_id`
    (a legacy record without one: any human `/group-em` entry -- see `_evidence_key`).
    Pending while that entry is absent and `claimed_at` is under 60s old (the hook fires before
    the harness writes the entry); rejected otherwise.

    `cache_path` is what makes this safe on per-tool-call hooks and the statusline: a final
    verdict is read back without opening the transcript, and a pending one resumes at the byte
    offsets already covered, so no transcript byte is scanned twice per (session, prompt_id)."""
    key = _evidence_key(record)
    if key is None:
        return {"status": "rejected", "reason": "bad-session-id"}
    session_id, prompt_id, kind = key
    cached = _read_cache(cache_path, key)
    if cached is not None and cached.get("status") in _FINAL_STATUSES:
        return {k: v for k, v in cached.items() if k not in ("session_id", "offsets")}
    if kind == HOME_START:
        verdict = _home_start_verdict(session_id, key[1], now, projects_root)
        _write_cache(cache_path, key, verdict, {})
        return verdict
    prior_offsets = (cached or {}).get("offsets")
    found, offsets = _scan_transcripts(
        session_id, projects_root, prompt_id, prior_offsets if isinstance(prior_offsets, dict) else {}
    )
    if found:
        verdict = {"status": "verified", "prompt_id": prompt_id, **found}
    elif prompt_id is None:
        verdict = {"status": "rejected", "reason": "no-human-entry-legacy", "prompt_id": None}
    else:
        evidence = record.get("entry_evidence") or {}
        claimed = _parse_iso(evidence.get("claimed_at"))
        if claimed is not None and 0 <= now - claimed < PENDING_WINDOW_SECONDS:
            verdict = {
                "status": "pending", "prompt_id": prompt_id, "claimed_at": evidence.get("claimed_at")
            }
        else:
            verdict = {"status": "rejected", "reason": "no-matching-human-entry", "prompt_id": prompt_id}
    _write_cache(cache_path, key, verdict, offsets)
    return verdict


def _in_home(cwd: str) -> bool | None:
    """Whether `cwd` lies in the resolved group-em home; None when the home cannot be resolved.

    The import is lazy: hooks and the statusline import this module for prompt_id records and
    must not pay for a registry read they never use."""
    from coordinator_core.group_em import gem_home

    try:
        return gem_home.contains(gem_home.resolve_home(), cwd)
    except (gem_home.HomeRefused, OSError):
        return None


def earliest_cwd_entry(session_id: str, projects_root: Path | None = None) -> dict | None:
    """The session's earliest cwd-bearing transcript entry, or None.

    Leading `last-prompt`/`mode` lines carry no cwd; the first cwd line is the SessionStart
    attachment. Each file is read only to its first cwd line. Returns
    `{transcript, uuid, timestamp, cwd, entrypoint}`."""
    best: dict | None = None
    best_ts = float("inf")
    for path in transcript_paths(session_id, projects_root):
        try:
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        break
                    if _CWD_NEEDLE not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not (isinstance(entry, dict) and isinstance(entry.get("cwd"), str)):
                        continue
                    ts = _parse_iso(entry.get("timestamp"))
                    ts = float("inf") if ts is None else ts
                    if best is None or ts < best_ts:
                        best_ts = ts
                        best = {
                            "transcript": str(path),
                            "uuid": entry.get("uuid"),
                            "timestamp": entry.get("timestamp"),
                            "cwd": entry["cwd"],
                            "entrypoint": entry.get("entrypoint"),
                        }
                    break
        except OSError:
            continue
    return best


def _home_start_reason(entry: dict) -> str | None:
    in_home = _in_home(entry["cwd"])
    if in_home is None:
        return "home-unresolved"
    if not in_home:
        return "cwd-outside-home"
    if entry.get("entrypoint") not in _INTERACTIVE_ENTRYPOINTS:
        return "not-interactive"
    return None


def home_start_refusal(session_id: str, projects_root: Path | None = None) -> str | None:
    """Why a home-start claim is refused now, or None when nothing refutes it yet (including
    when the session has no cwd-bearing entry on disk)."""
    if not valid_session_id(session_id):
        return "bad-session-id"
    entry = earliest_cwd_entry(session_id, projects_root)
    return None if entry is None else _home_start_reason(entry)


def _home_start_verdict(
    session_id: str, claimed_at: str, now: float, projects_root: Path | None
) -> dict:
    base = {"kind": HOME_START, "claimed_at": claimed_at}
    entry = earliest_cwd_entry(session_id, projects_root)
    if entry is None:
        claimed = _parse_iso(claimed_at)
        if claimed is not None and 0 <= now - claimed < PENDING_WINDOW_SECONDS:
            return {"status": "pending", **base}
        return {"status": "rejected", "reason": "no-start-entry", **base}
    reason = _home_start_reason(entry)
    if reason is not None:
        return {"status": "rejected", "reason": reason, **base}
    return {"status": "verified", **base, **entry}


def _projects_root() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "projects"


def transcript_paths(session_id: str, projects_root: Path | None = None) -> list[Path]:
    """Every transcript the harness wrote for `session_id`, across project dirs."""
    if not session_id or any(sep in session_id for sep in ("/", "\\", "..")):
        return []
    root = projects_root or _projects_root()
    try:
        return sorted(root.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return []


def _is_human_group_em_entry(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    origin = entry.get("origin")
    if not (isinstance(origin, dict) and origin.get("kind") == "human"):
        return False
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, str) and any(m in content for m in _COMMAND_MARKERS)


def find_human_entry(
    session_id: str, projects_root: Path | None = None, prompt_id: str | None = None
) -> dict | None:
    """The latest human-typed `/group-em` in `session_id`'s transcript, or None.

    With `prompt_id`, only an entry whose `promptId` equals it counts.
    Returns `{transcript, uuid, timestamp}`, the evidence a peer can re-check."""
    if not valid_session_id(session_id):
        return None
    return _scan_transcripts(session_id, projects_root, prompt_id, {})[0]


def _scan_transcripts(
    session_id: str, projects_root: Path | None, prompt_id: str | None, offsets: dict
) -> tuple[dict | None, dict]:
    """Scan each transcript from its byte offset in `offsets`; return (latest match, offsets).

    Offsets advance only past complete lines (a line mid-write is rescanned next time) and reset
    to 0 when a file is shorter than its offset. Bytes-level needles keep `json.loads` off every
    line that cannot match -- including the prompt id itself when one is required."""
    found = None
    new_offsets: dict = {}
    needles = [b"command-name>/", b"group-em"]
    if prompt_id is not None:
        needles.append(prompt_id.encode("utf-8"))
    for path in transcript_paths(session_id, projects_root):
        start = offsets.get(str(path))
        try:
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if not isinstance(start, int) or not 0 <= start <= size:
                    start = 0
                handle.seek(start)
                position = start
                for line in handle:
                    if not line.endswith(b"\n"):
                        break
                    position += len(line)
                    if not all(needle in line for needle in needles):
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(entry, dict)
                        and (prompt_id is None or entry.get("promptId") == prompt_id)
                        and _is_human_group_em_entry(entry)
                    ):
                        found = {
                            "transcript": str(path),
                            "uuid": entry.get("uuid"),
                            "timestamp": entry.get("timestamp"),
                        }
        except OSError:
            continue
        new_offsets[str(path)] = position
    return found, new_offsets

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
import tempfile
from datetime import datetime
from pathlib import Path

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


def _evidence_key(record: dict) -> tuple[str, str | None] | None:
    """`(session_id, prompt_id)` the verdict is keyed on; a None prompt_id selects the legacy
    predicate. None when the session id is unusable (the record can never verify)."""
    session_id = record.get("session_id")
    if not valid_session_id(session_id):
        return None
    evidence = record.get("entry_evidence")
    prompt_id = evidence.get("prompt_id") if isinstance(evidence, dict) else None
    if isinstance(prompt_id, str) and prompt_id:
        return session_id, prompt_id
    # Records written before claim-time prompt_id capture (no `entry_evidence`, or the earlier
    # `{uuid, transcript, timestamp}` shape). Their holders may be unable to re-enter, so they
    # verify on the original predicate: ANY human /group-em in the holder's own transcript.
    # Still unforgeable by a tool/CLI caller -- the transcript path derives from the session id.
    return session_id, None


def _read_cache(cache_path: Path | None, key: tuple[str, str | None]) -> dict | None:
    if cache_path is None:
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or [data.get("session_id"), data.get("prompt_id")] != list(key):
        return None
    return data


def _write_cache(
    cache_path: Path | None, key: tuple[str, str | None], verdict: dict, offsets: dict
) -> None:
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
    payload = {"session_id": key[0], "offsets": offsets, **verdict, "prompt_id": key[1]}
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(cache_path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh)
            os.replace(tmp, cache_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
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
    session_id, prompt_id = key
    cached = _read_cache(cache_path, key)
    if cached is not None and cached.get("status") in _FINAL_STATUSES:
        return {k: v for k, v in cached.items() if k not in ("session_id", "offsets")}
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

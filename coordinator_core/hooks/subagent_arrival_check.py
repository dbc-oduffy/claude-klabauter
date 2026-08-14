"""
coordinator_core.hooks.subagent_arrival_check — pull/poll subagent-arrival op.

Purpose: Answer "is this dispatched subagent still working, or has it arrived at
its final turn" from a caller-supplied `agent_id`, by reading the LAST record of
that subagent's OWN transcript — never the whole file. This is a sibling of
hooks.subagent_zero_tool_use_resolve (same pull/poll posture, same DEBOUNCE-vs-
"trust the shape immediately" tension) but answers a different question: resolve
answers "did this agent do any work", this op answers "is this agent still
mid-turn". Read hooks.subagent_zero_tool_use.py's fail-toward-UNKNOWN discipline
before touching this file — it is inherited here verbatim, not re-derived: an
absent, unreadable, empty, or unparseable transcript resolves to `state: "unknown"`
and is NEVER reported as `"arrived"`.

Path derivation is this op's job, not the caller's — DoE (the caller) must not open
any file or compute any path itself. Given the PARENT session's `transcript_path`
(as carried on PostToolUse) and `agent_id`, the subagent transcript path is:

    dirname(transcript_path) / stem(transcript_path) / "subagents" / f"agent-{agent_id}.jsonl"

TWO ID NAMESPACES — the caller may hand either, and this op resolves both. An
unnamed dispatch's `agent_id` is bare hex, and the two namespaces coincide: the
formula above lands on the real file. A NAMED teammate has a subagent-side id of
the form `a<name>-<16 hex>` while every EM-side tracking surface (the caller's
`dispatched-agents.txt` among them) records the canonical form `<name>@session-
<short8>`. Feeding the canonical form to the formula derives
`subagents/agent-<name>@session-<short8>.jsonl`, which CANNOT EXIST — so every
named teammate resolved "unknown" and the caller nudged regardless of whether the
agent had already returned. The oracle worked; it never got to look.

The forward map (subagent-side → canonical) is lossy — the 16-hex suffix is
dropped — so the reverse is a directory probe, not a string transform:
`_resolve_subagent_transcript_path` tries the direct formula first (bare-hex fast
path, unchanged), then, for a canonical-form id, scans the subagents directory for
`agent-a<name>-<16 hex>.jsonl` and disambiguates via each candidate's `.meta.json`
sidecar (`name` + `teamName == "session-<short8>"`), falling back to newest-mtime.
The probe never builds a path from the id — it filters `os.listdir` output — so a
hostile id cannot traverse out of the directory.

States (pinned; never conflate `"unknown"` with `"arrived"`):
    "arrived" — the LAST record satisfies ALL of:
        - record `"type"` == "assistant" AND `message.role` == "assistant"
        - NO `"tool_use"` block in `message.content`
        - AND ( `message.stop_reason` in {"end_turn", "stop_sequence"}
                OR the record's `"timestamp"` is older than the debounce
                (120s, a fixed module constant — see `_DEBOUNCE_SECONDS`) )
    "running" — the last record is any other shape (a `tool_use` block present,
        or the record is a `"user"`/tool-result record, or `type`/`message.role`
        don't match "assistant"), OR the shape matches the arrival predicate but
        the record's timestamp has not yet cleared the debounce window (the
        "flap" case below).
    "unknown" — the subagent transcript is absent, unreadable, empty, or its
        last line is not parseable JSON / not a JSON object. Never treated as a
        verified running OR arrived state — this is the same load-bearing branch
        hooks.subagent_zero_tool_use.py pins for its own UNKNOWN case, restated
        here because this op has an independent read path over a different file.

Read the LAST record only — seek from the end, never a full-file parse. These
transcripts reach ~900KB in steady dispatches and the calling hook runs on a 5s
timeout; a full read-and-parse here is a correctness defect (timeout risk), not a
style concern. `_read_last_nonempty_line` tails the file in bounded chunks from
EOF and stops at the first newline found, capped well above any single JSONL
line's realistic size.

Why the debounce exists — empirical basis (measured on this machine, 2026-07-30):
    - The arrival shape predicate (assistant record, assistant role, no tool_use
      block) identifies genuine arrival for 99.41% of 7,812 settled agents; the
      0.59% residual resolves to "running" under this op (fails toward the
      caller still nudging — the safe direction, never toward a false arrival).
    - The debounce exists because an assistant TEXT block streams to disk BEFORE
      the `tool_use` block that follows it in the same turn — a still-running
      agent transiently satisfies the shape predicate the instant its text block
      lands, before its next tool call is written. Calibrated on 176,949 mid-run
      windows: p50 1s, p90 6s, p99 55s elapsed between a transient shape-match
      and the next tool_use landing; a 120s debounce leaves 0.31% surviving as a
      false arrival.
    - `stop_reason` short-circuits the debounce for ~82% of true arrivals — a
      turn that ends with `end_turn`/`stop_sequence` needs no age check at all.

    The 120s figure is a fixed module constant (`_DEBOUNCE_SECONDS`), not a
    per-call parameter. Moving it is a measurement change — re-run the
    calibration above against fresh window data and edit the constant — never a
    runtime choice a caller makes per invocation. A caller-supplied override
    would let any single caller defeat the streaming-race protection this op
    exists to provide (a `0` override reads every transient in-turn flap as a
    genuine arrival, the exact false positive the debounce is calibrated
    against), so there is no parameter path to it at all.

Negative-spec:
    This op answers "is this agent still working", NOT "did it complete versus
    get stood down". A `TaskStop` (the agent being killed/cancelled) leaves no
    on-disk record of its own — a killed agent's last written record is
    indistinguishable from a healthy arrival, and this op WILL read it as
    "arrived". Do not add a fourth calibration constant, a kill-signal probe, or
    any other mechanism here to paper over that gap — it is out of scope for a
    transcript-shape read and belongs, if anywhere, to a caller that also has
    visibility into TaskStop.

    Does NOT write anything — pure read/compute, same posture as
    hooks.subagent_zero_tool_use_resolve and hooks.subagent_zero_tool_use_surface.

    Does NOT retry, wait, or poll on a timer — a single point-in-time read. The
    caller re-invokes this op itself if it wants a fresher read.

    Does NOT parse or hold open the PARENT transcript at `transcript_path` —
    only its path is used, to derive the subagent transcript path. The parent
    transcript itself is never opened by this op.

Spec backlink: cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-engine-op-contract.md
    (sibling contract; this op documents its own empirical basis above rather
    than re-deriving hooks.subagent_zero_tool_use's — the two ops answer
    different questions over different files)
"""

from __future__ import annotations

import datetime
import json
import os
import re

from coordinator_core.ipc import register_op
from coordinator_core.hooks._payload import field

_ARRIVAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# Fixed module constant, not a per-call parameter — see the calibration basis in
# the module docstring above and "Moving it is a measurement change" there. No
# caller may override this; the 0.31% false-arrival residual at 120s is a
# property of THIS default, not of the op, and a caller-supplied override would
# hand any single caller a way to defeat the streaming-race protection.
_DEBOUNCE_SECONDS = 120

# EM-side canonical dispatch id: "<name>@session-<short8>". The name charset is
# the intersection of what the harness puts in an agent name and what the caller's
# own id guard admits before a row ever reaches this op.
_EM_CANONICAL_ID_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)@session-(?P<short>[0-9a-fA-F]{8})$")

_TAIL_CHUNK_BYTES = 8192
_TAIL_MAX_CHUNKS = 32  # bounds the tail read at ~256KB from EOF
_TAIL_CAP_BYTES = _TAIL_MAX_CHUNKS * _TAIL_CHUNK_BYTES


def _derive_subagent_transcript_path(transcript_path: str, agent_id: str) -> str:
    """dirname(transcript_path)/stem(transcript_path)/subagents/agent-<agent_id>.jsonl

    Pure string/path derivation — this is the ONE place path shape lives; callers
    (DoE) must not derive or open this path themselves.
    """
    directory = os.path.dirname(transcript_path)
    basename = os.path.basename(transcript_path)
    stem, _, _ext = basename.rpartition(".")
    stem = stem or basename
    return os.path.join(directory, stem, "subagents", f"agent-{agent_id}.jsonl")


def _is_path_safe_agent_id(agent_id: str) -> bool:
    """False for any id that would steer the direct derivation out of the subagents dir.

    The direct derivation drops `agent_id` into a filename component verbatim, so a
    separator or a `..` segment inside it escapes the directory — `../../x` derives
    `subagents/agent-../../x.jsonl`, which normalizes outside. The caller's own id
    guard already rejects those bytes, but this op must not inherit its safety from a
    caller it does not control: an id like this resolves to "unknown", never to a read
    of whatever the traversal landed on.
    """
    if not agent_id or "\x00" in agent_id:
        return False
    if "/" in agent_id or "\\" in agent_id or os.sep in agent_id:
        return False
    return ".." not in agent_id


def _resolve_subagent_transcript_path(transcript_path: str, agent_id: str) -> str:
    """Resolve `agent_id` to a real subagent transcript path, across both id namespaces.

    Order:
        1. The direct derivation (`_derive_subagent_transcript_path`). If that file
           exists, use it — the bare-hex / already-subagent-side case, unchanged.
        2. If `agent_id` is the EM-side canonical form `<name>@session-<short8>`,
           probe the subagents directory for `agent-a<name>-<16 hex>.jsonl`.
        3. Otherwise (and on any probe failure) return the direct derivation, so the
           caller's "absent, unreadable, or empty" reason names a concrete path and
           the state stays "unknown" — the fail-toward-nudge direction.

    Never raises. Never builds a path out of `agent_id`'s own bytes beyond step 1's
    single filename component: the probe filters `os.listdir` results against a
    regex, so a traversal-shaped name matches nothing rather than escaping the
    directory.
    """
    direct = _derive_subagent_transcript_path(transcript_path, agent_id)
    if os.path.isfile(direct):
        return direct

    canonical = _EM_CANONICAL_ID_RE.match(agent_id)
    if not canonical:
        return direct

    subagents_dir = os.path.dirname(direct)
    name = canonical.group("name")
    team_name = "session-" + canonical.group("short").lower()
    filename_re = re.compile(r"^agent-a" + re.escape(name) + r"-[0-9a-fA-F]{16}\.jsonl$")

    try:
        entries = os.listdir(subagents_dir)
    except OSError:
        return direct

    sidecar_confirmed: list[str] = []
    filename_only: list[str] = []
    for entry in entries:
        if not filename_re.match(entry):
            continue
        candidate = os.path.join(subagents_dir, entry)
        if _sidecar_identifies(candidate, name, team_name):
            sidecar_confirmed.append(candidate)
        else:
            filename_only.append(candidate)

    # A sidecar-confirmed match beats a filename-only one; within either tier the
    # newest file wins, because a name re-dispatched inside one session yields two
    # files whose canonical ids are byte-identical — the EM-side form carries
    # nothing to tell them apart, and the newest is the dispatch being nudged.
    pool = sidecar_confirmed or filename_only
    if not pool:
        return direct
    return max(pool, key=_mtime_or_zero)


def _mtime_or_zero(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _sidecar_identifies(transcript_candidate: str, name: str, team_name: str) -> bool:
    """True when `<candidate>.meta.json` names this teammate AND this session team.

    The sidecar is written next to every subagent transcript and carries the two
    fields that reconstruct the canonical id exactly (`name`, `teamName`), so a
    confirmed sidecar removes the guesswork a bare filename match leaves. Absent,
    unreadable, or non-conforming sidecar -> False (candidate demoted to the
    filename-only tier, never discarded).
    """
    sidecar = transcript_candidate[: -len(".jsonl")] + ".meta.json"
    try:
        with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(meta, dict):
        return False
    meta_team = meta.get("teamName")
    return (
        meta.get("name") == name
        and isinstance(meta_team, str)
        and meta_team.lower() == team_name
    )


def _read_last_nonempty_line(path: str) -> tuple[str | None, bool]:
    """Return (last non-empty line of `path`, cap_reached), read by tailing from EOF.

    `cap_reached` is True when the tail read hit `_TAIL_CAP_BYTES` (~256KB)
    without ever finding a newline — either the final on-disk record is
    itself larger than the tail cap, or it is preceded by a run of blank
    padding large enough on its own to exhaust the cap before any record
    content is reached; either way whatever ended up in `buf` is a FRAGMENT,
    not the record. Callers must not hand a cap-reached fragment to
    `_classify` and report its parse outcome — that would blame a truncation
    on the transcript's JSON being malformed, which it may not be.
    `cap_reached` is always False when the line is None (nothing to be a
    fragment of).

    Returns (None, False) on: file absent, not a regular file, unreadable for
    any OSError reason, or present-but-empty-of-non-blank-lines. Never raises.

    Bounded tail read: reads at most `_TAIL_CAP_BYTES` from the end of the
    file, growing backward only until a newline is found (or the file start is
    reached) — never a full-file read. 256KB is well above any single
    realistic JSONL transcript line; a record that exceeds it is the
    exceptional case `cap_reached` exists to name.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size == 0:
                return None, False

            buf = b""
            pos = file_size
            chunks_read = 0
            found_newline = False
            while pos > 0 and chunks_read < _TAIL_MAX_CHUNKS:
                read_size = min(_TAIL_CHUNK_BYTES, pos)
                pos -= read_size
                fh.seek(pos)
                buf = fh.read(read_size) + buf
                chunks_read += 1
                # Review: coordinator:code-reviewer — b"\n".rstrip(b"\n") only strips a
                # trailing run of pure newline bytes; a trailing blank line carrying any
                # other whitespace (e.g. "...}\n   \n") left an embedded \n from the
                # record/blank-line delimiter, which falsely satisfied this check and
                # truncated the real last record mid-read. Stripping trailing whitespace
                # generally (not just b"\n") before checking for an embedded newline
                # closes that gap without changing behavior for the pure-newline case.
                if b"\n" in buf.rstrip():
                    found_newline = True
                    break
    except OSError:
        return None, False

    cap_reached = not found_newline and chunks_read >= _TAIL_MAX_CHUNKS

    text = buf.decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None, False
    return lines[-1], cap_reached


def _has_tool_use_block(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)


def _parse_timestamp(raw) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _classify(last_line: str, *, now: datetime.datetime) -> tuple[str, str]:
    """Classify one raw JSONL line into (state, reason). Never raises.

    Only a genuine parse failure (invalid JSON, or JSON that is not an object)
    resolves to "unknown" here — every other non-matching shape resolves to
    "running", since the file itself was readable and produced a well-formed
    record that simply isn't an arrival. Callers must not pass a tail-cap
    fragment here (see `_read_last_nonempty_line`'s `cap_reached` signal) — a
    fragment's JSON-parse failure would masquerade as this function's ordinary
    "not valid JSON" outcome, and the tail-cap case needs its own reason string
    instead (see the handler).

    Debounce window is the fixed module constant `_DEBOUNCE_SECONDS` — there is
    no per-call override (see that constant's docstring comment).
    """
    try:
        record = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return "unknown", "last transcript line is not valid JSON"
    if not isinstance(record, dict):
        return "unknown", "last transcript line did not parse to a JSON object"

    message = record.get("message")
    if (
        record.get("type") != "assistant"
        or not isinstance(message, dict)
        or message.get("role") != "assistant"
        or _has_tool_use_block(message)
    ):
        return "running", "last record does not match the arrival shape (tool_use present, or not a terminal assistant turn)"

    stop_reason = message.get("stop_reason")
    if stop_reason in _ARRIVAL_STOP_REASONS:
        return "arrived", f"terminal assistant turn with stop_reason={stop_reason!r}"

    timestamp = _parse_timestamp(record.get("timestamp"))
    if timestamp is None:
        return "running", "arrival shape matched but record carries no parseable timestamp to clear the debounce"

    age_seconds = (now - timestamp).total_seconds()
    if age_seconds >= _DEBOUNCE_SECONDS:
        return "arrived", f"arrival shape matched and record is {age_seconds:.1f}s old (debounce {_DEBOUNCE_SECONDS}s)"
    return "running", f"arrival shape matched but record is only {age_seconds:.1f}s old (debounce {_DEBOUNCE_SECONDS}s) — possible in-turn flap"


def _envelope(state: str, agent_id: str, subagent_transcript_path: str, reason: str) -> dict:
    return {
        "state": state,
        "agent_id": agent_id,
        "subagent_transcript_path": subagent_transcript_path,
        "reason": reason,
    }


@register_op("hooks.subagent_arrival_check")
async def _handler(params: dict, repo_root=None) -> dict:
    """Compute-only op: classify one subagent as arrived / running / unknown.

    Inputs (flat scalar, extracted via _payload.field(); "" treated as absent):
        transcript_path — the PARENT session's transcript path (as carried on
            PostToolUse); used ONLY to derive the subagent transcript path, never
            opened directly.
        agent_id — the subagent's id, in EITHER namespace: the subagent-side id
            (bare hex, or `a<name>-<16 hex>`) or the EM-side canonical form
            `<name>@session-<short8>` that every EM tracking surface records. See
            the module docstring's TWO ID NAMESPACES note — resolution across the
            two is this op's job, not the caller's.

    There is deliberately no debounce override input — the 120s debounce
    (`_DEBOUNCE_SECONDS`) is a fixed module constant, not a per-call parameter.
    See the module docstring and that constant's comment for why.

    Returns the pinned {"state", "agent_id", "subagent_transcript_path", "reason"}
    shape directly (structured JSON-RPC result, not an advisory envelope) — never
    raises; every failure path resolves to state "unknown" with a specific reason.
    """
    import asyncio

    transcript_path = field(params, "transcript_path")
    agent_id = field(params, "agent_id")

    if not agent_id:
        return _envelope("unknown", "", "", "no agent_id supplied — nothing to check")
    if not transcript_path:
        return _envelope("unknown", agent_id, "", "no transcript_path supplied — cannot derive the subagent transcript path")
    if not _is_path_safe_agent_id(agent_id):
        return _envelope(
            "unknown",
            agent_id,
            "",
            "agent_id carries a path separator or a '..' segment — it would steer the "
            "derivation outside the subagents directory, so no read is attempted",
        )

    subagent_transcript_path = await asyncio.to_thread(
        _resolve_subagent_transcript_path, transcript_path, agent_id
    )

    last_line, cap_reached = await asyncio.to_thread(_read_last_nonempty_line, subagent_transcript_path)
    if last_line is None:
        return _envelope(
            "unknown",
            agent_id,
            subagent_transcript_path,
            f"subagent transcript at {subagent_transcript_path} is absent, unreadable, or empty",
        )
    if cap_reached:
        return _envelope(
            "unknown",
            agent_id,
            subagent_transcript_path,
            f"no newline found within the {_TAIL_CAP_BYTES}-byte tail read of "
            f"{subagent_transcript_path} — either the final record exceeds the tail cap, or it "
            "is preceded by a run of blank padding large enough on its own to exhaust the cap "
            "(this is a truncation, not a JSON parse failure)",
        )

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    state, reason = _classify(last_line, now=now)
    return _envelope(state, agent_id, subagent_transcript_path, reason)

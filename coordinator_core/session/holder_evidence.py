"""
coordinator_core.session.holder_evidence — decidable evidence about
WHAT a claim/handoff holder is actually doing, not merely THAT it is live.

Purpose: `compute_claim_grant`/`compute_competing_claim` (`pickup_assemble/
__init__.py`) answer a boolean `holder_live` plus a raw claim age. Both are
unfalsifiable on their own — a bare `True` plus a stale-looking age is
exactly the shape that gets overridden by reflex (state/handoffs/
2026-07-26_..._sizing-lobby-slate-cascade.md, the incident this module
exists to close: a genuinely live peer read as a stale-mtime false positive
because there was nothing to check the verdict against). This module adds
the evidence layer: WHICH liveness layer concluded live, how fresh the
signal actually is, and what the holder was last touching.

READ-ONLY and FAIL-SOFT by construction: every field here is `None` (or an
explicit error marker) rather than an exception or a changed verdict on any
read failure. This module never re-derives or overrides `holder_live` —
that stays sourced from `coordinator_core.session.liveness` exactly as
before; `holder_evidence` only explains an existing decision, never makes
a new one. See `liveness.py`'s own module docstring: a caught liveness
exception is an INDETERMINATE read, never confirmed-dead, and this module
must never launder that into a `False`.

`scope_overlap` (C3, docs/plans/2026-08-16-trace-a-claim-back-to-its-
session.md): previously derived from the holder's TRANSCRIPT
`recent_paths` (<=10 entries, most-recent-first) — a fragile proxy for
what a session is actually working on, and answering `None` whenever
either side was empty, which made it `None` for effectively every
candidate on a live box (a handoff declares no `scope:`, so the empty side
was EVERY handoff). Re-pointed at the same substrate C1
(`coordinator_core.session.claim_neighbours`) joins against:
`claim_index.lookup()`'s `touched.txt`-derived claimant set, which answers
what a session has actually CLAIMED, not merely last touched in a
transcript tail. Two call shapes, chosen by whichever of `scope`/
`artifact_path` the caller supplies (see `holder_evidence()`'s docstring):
a bare `scope` list is looked up directly via `claim_index.lookup()`; an
`artifact_path` routes through `claim_neighbours.find_neighbours()` for
its full two-tier file-set resolution (the `deliverable_id` bridge a
handoff needs, AC2) plus its own live-peer filtering. The three-valued
`True`/`False`/`None` contract is preserved exactly — `None` still means
genuinely unresolvable, never merely empty, and this module's own
`_scope_path_overlaps`-era either-side-empty-collapses-to-None bug does
not survive the re-point (see `_claim_scope_overlap`'s docstring).

Spec backlink: pln-pickup-as-a-code-computed-deci-7394dc,
pln-trace-a-claim-back-to-its-sess-25ae79 (C3)

Relocated (2026-08-19, docs/plans/2026-08-19-fleet-work-state-who-holds-
which-baton.md, chunk C1a) from `coordinator_core.pickup_assemble.
holder_evidence` into `coordinator_core.session` — unchanged in behaviour
except the `_resolve_transcript` import, which moved from module scope to
function-local inside `holder_evidence()` to break an import cycle through
`coordinator_core.ops` (see that function's docstring note and the chunk's
own cycle trace). `session/` is light and on no eager-import path;
`pickup_assemble` is heavy and on the cold-invocation budget — nothing in
this module may import `pickup_assemble`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.session import claim_index
from coordinator_core.session import claim_neighbours
from coordinator_core.session import core
from coordinator_core.session import liveness as _liveness

_TRANSCRIPT_TAIL_BYTES = 256 * 1024

_TRANSCRIPT_TAIL_RECORDS = 200

_RECENT_PATHS_CAP = 10

_PATH_INPUT_KEYS = ("file_path", "notebook_path", "path")

_COMMAND_PATH_RE = re.compile(r"[./A-Za-z0-9_\-]*(?:/[./A-Za-z0-9_\-]+)+")


def liveness_basis(holder_sid: str, cwd: Optional[str] = None) -> str:
    """Which liveness LAYER concluded live/dead for `holder_sid` — the single
    highest-value evidence field (see module docstring). Does not repeat the
    liveness decision itself, only names its source.

    Reads the basis off `coordinator_core.session.liveness.session_verdict`
    — the per-id entry point over the SAME shared derivation
    `live_session_verdicts` uses (`_verdict_for_sdir`) — rather than
    re-deriving it here. This module used to call `core.stable_pid_alive`
    directly, which violated `liveness.py`'s own D5 single-liveness-key
    invariant ("called from exactly ONE place here — `session_live`");
    reading off the seam instead restores that invariant as a free side
    effect (Review: staff-eng second pass, Finding 7). `session_verdict` was
    added (Review: staff-eng-review C, 2026-08-10) to drop the incidental
    whole-corpus `live_session_verdicts(cwd)` scan this function used to run
    just to discard every entry but one.

    Public name (promoted from `_liveness_basis`) so `coordinator/bin/
    session-claim-cli`'s `is-session-live` subcommand can reuse this exact
    derivation for AC8 rather than computing a second, drift-prone one — see
    docs/plans/2026-08-10-stable-pid-capture-breadcrumb-and-liveness-basis.md
    § C3. `_liveness_basis` is kept below as an alias for this module's own
    internal caller (`holder_evidence`).

    Vocabulary (eight values; see `live_session_verdicts`'s docstring for the
    full per-arm derivation):
      "harness-registry"      — harness-written process identity, stronger
                                 evidence than `"stable-pid"`; `age_sec` is
                                 always `None` on this basis.
      "stable-pid"            — Layer 1 (PPID-authoritative) was consulted.
      "stable-pid-shared"     — Layer 1 was consulted, but this `stable_pid`
                                 is carried by more than one session on this
                                 box, so it proves only that SOMETHING under
                                 that shared ancestor is alive. An
                                 indeterminate wearing a live answer's clothes
                                 is what this value exists to stop; `live`
                                 stays True (conservative for every caller),
                                 but the basis no longer claims the answer is
                                 about this session.
      "recency-window"        — Layer 2 recency fallback, `last_activity`
                                 present and parseable.
      "recency-window-mtime"  — Layer 2 recency fallback with the meta-less/
                                 mid-write mtime-substitution recency source.
      "no-record"              — no harness-registry entry and no non-empty
                                 file anywhere under the session dir, so the
                                 recency fallback above had nothing but the
                                 bare directory's own mtime to substitute —
                                 not evidence of a process. `live` is always
                                 False here and `age_sec` is always `None`,
                                 same as `"harness-registry"`.
      "harness-registry-elsewhere" — `session_verdict`-ONLY (never produced
                                 by `live_session_verdicts`'s whole-corpus
                                 scan, which never leaves this repo's own
                                 session dirs): no session dir for
                                 `holder_sid` exists in THIS repo, but a
                                 confirmed harness-registry record for it
                                 does — a live session working in ANOTHER
                                 repo, most likely, but inferred from
                                 dir-absence alone, so worded "elsewhere",
                                 never "confirmed reachable". `age_sec` is
                                 NOT meaningful on this basis either —
                                 `session_verdict`'s third tuple slot carries
                                 the peer's `cwd` string here instead, which
                                 this function's own three-way unpack
                                 discards.
      "unknown"               — either the underlying process-liveness check
                                 itself raised (never launder that into a
                                 stronger claim than can be supported), or
                                 `holder_sid` has no verdict at all (absent
                                 from the sessions dir enumeration, e.g. the
                                 `no-session` sentinel) — evidence-gap, not a
                                 stronger claim either way.
    """
    entry = _liveness.session_verdict(holder_sid, cwd)
    if entry is None:
        return "unknown"
    _live, basis, _age_sec = entry
    return basis


_liveness_basis = liveness_basis


def _last_activity_age_sec(sdir: str) -> Optional[int]:
    last_iso = core.read_meta_field(sdir, "last_activity")
    if not last_iso:
        return None
    last_epoch = core.iso_to_epoch(last_iso)
    if last_epoch <= 0:
        return None
    age = core.now_epoch() - last_epoch
    return age if age >= 0 else 0


def _meta_str_or_none(sdir: str, field: str) -> Optional[str]:
    value = core.read_meta_field(sdir, field)
    return value or None


def _parse_scope_entry(entry: str) -> tuple[Optional[str], str]:
    match = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", entry.strip())
    if match is None:
        return None, entry.strip()
    return match.group(1), match.group(2).strip()


def _local_scope_paths(scope: list[str]) -> list[str]:
    paths: list[str] = []
    for entry in scope:
        repo_id, path = _parse_scope_entry(entry)
        if repo_id is not None:
            continue
        path = path.rstrip("/")
        if path:
            paths.append(path)
    return paths


def _claim_scope_overlap(
    holder_sid: str,
    repo_root: Path,
    *,
    scope: Optional[list[str]],
    artifact_path: Optional[str],
    caller_sid: Optional[str],
) -> Optional[bool]:
    """True/False/None per `holder_evidence()`'s documented three-valued
    `scope_overlap` contract — now derived from `claim_index.lookup()`
    (what `holder_sid` has actually CLAIMED, via `touched.txt`) rather than
    the holder's transcript `recent_paths`.

    Two shapes, chosen by which of `artifact_path`/`scope` the caller
    supplies:

    `artifact_path` given (preferred — this is "C1's join" in full):
    delegates to `claim_neighbours.find_neighbours()`, which resolves
    `artifact_path`'s own file set through its two-tier resolution (a
    `scope:` if present, else the `deliverable_id` bridge to a plan's
    `scope:` — the handoff case, AC2) and returns only LIVE peer
    claimants. `holder_sid` membership in that neighbour set is the
    answer. `UNRESOLVABLE` (file set could not be determined) maps to
    `None` here — never `False`; that distinction is the whole point of
    C1's status-first result and must not be flattened back into the
    conflation this re-point exists to close (see module docstring).

    `scope` given instead (back-compat path for callers that have
    already resolved a file set themselves, e.g. a plan/sizing artifact's
    own frontmatter `scope:`): looked up directly via `claim_index.
    lookup()`. `scope is None` (no scope resolved and no `artifact_path`
    to bridge from) is genuinely unresolvable -> `None`. `scope == []`
    (an artifact that explicitly declares no scoped paths) IS resolved,
    just empty -> deterministically `False`, never `None` — this is the
    either-side-empty-collapses-to-None bug this re-point fixes: an empty
    RESOLVED file set proves zero overlap, it does not mean "unknown".

    A path `claim_index.lookup()` itself could not answer (aborted
    rebuild — `claim_index.UNANSWERABLE`) is excluded from a `True`/
    `False` verdict on its own; if EVERY local path comes back
    unanswerable (and none confirm an overlap), the honest answer is
    `None`, not a false-negative `False`.

    Never raises (module's fail-soft contract) — any exception here
    yields `None`, the same "evidence gap" a raising `claim_index`/
    `claim_neighbours` call already degrades to internally.
    """
    try:
        if artifact_path is not None:
            result = claim_neighbours.find_neighbours(
                artifact_path, caller_sid=caller_sid, cwd=str(repo_root)
            )
            if result.status == claim_neighbours.UNRESOLVABLE:
                return None
            return any(n.session_id == holder_sid for n in result.neighbours)

        if scope is None:
            return None

        local_paths = _local_scope_paths(scope)
        if not local_paths:
            return False

        lookup = claim_index.lookup(local_paths, cwd=str(repo_root))
        found_overlap = False
        found_unanswerable = False
        for path in local_paths:
            claimants = lookup.get(path, [])
            if claim_index.UNANSWERABLE in claimants:
                found_unanswerable = True
                continue
            if holder_sid in claimants:
                found_overlap = True
        if found_overlap:
            return True
        return None if found_unanswerable else False
    except Exception:  # noqa: BLE001 - evidence, never a raise (module contract)
        return None


def _extract_paths_from_tool_use(tool_input: dict, repo_root: Path) -> list[str]:
    found: list[str] = []

    for key in _PATH_INPUT_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            found.append(value)

    command = tool_input.get("command")
    if isinstance(command, str) and command:
        found.extend(_COMMAND_PATH_RE.findall(command))

    normalized: list[str] = []
    for raw in found:
        try:
            p = Path(raw)
            if p.is_absolute():
                p = p.resolve()
                try:
                    rel = p.relative_to(repo_root.resolve())
                    normalized.append(str(rel))
                    continue
                except ValueError:
                    continue
            normalized.append(raw)
        except (OSError, ValueError):
            continue
    return normalized


def _read_transcript_tail_records(transcript_path: str) -> list[dict]:
    """Read at most the trailing `_TRANSCRIPT_TAIL_BYTES` of `transcript_path`,
    discard the first (possibly partial) line, and parse up to the last
    `_TRANSCRIPT_TAIL_RECORDS` well-formed jsonl records. Never raises —
    any I/O or decode failure yields an empty list."""
    try:
        size = Path(transcript_path).stat().st_size
        truncated = size > _TRANSCRIPT_TAIL_BYTES
        with open(transcript_path, "rb") as fh:
            if truncated:
                fh.seek(size - _TRANSCRIPT_TAIL_BYTES)
            raw = fh.read()
    except OSError:
        return []

    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if truncated and len(lines) > 1:
        lines = lines[1:]

    records: list[dict] = []
    for line in lines[-_TRANSCRIPT_TAIL_RECORDS:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _recent_paths_from_transcript(transcript_path: str, repo_root: Path) -> list[str]:
    """Most-recent-first, de-duplicated, capped at `_RECENT_PATHS_CAP` list
    of file-ish paths touched by `tool_use` blocks in the transcript tail."""
    records = _read_transcript_tail_records(transcript_path)
    paths: list[str] = []
    seen: set[str] = set()

    for record in reversed(records):
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            for path in _extract_paths_from_tool_use(tool_input, repo_root):
                if path in seen:
                    continue
                seen.add(path)
                paths.append(path)
                if len(paths) >= _RECENT_PATHS_CAP:
                    return paths
    return paths


def holder_evidence(
    holder_sid: Optional[str],
    repo_root: Path,
    *,
    scope: Optional[list[str]] = None,
    artifact_path: Optional[str] = None,
    caller_sid: Optional[str] = None,
    want_activity: bool = False,
) -> dict[str, Any]:
    """Decidable evidence about what `holder_sid` is actually doing, to
    accompany (never replace) the `holder_live` boolean `compute_claim_grant`
    / `compute_competing_claim` already resolve via
    `coordinator_core.session.liveness`.

    Always returns a dict with these keys (each `None` when unknown):
      liveness_basis        - "harness-registry" | "stable-pid" |
                                 "recency-window" | "recency-window-mtime" |
                                 "no-record" | "harness-registry-elsewhere" |
                                 "unknown"
      last_activity_age_sec  - int seconds since meta.json's last_activity
      holder_goal            - meta.json's "goal" field
      holder_goal_state       - "declared" | "undeclared" | "unreadable" —
                                 disambiguates `holder_goal`'s two
                                 pre-existing `None`-producing paths
                                 (2026-08-13): "declared" means meta.json was
                                 read and `goal` was non-empty; "undeclared"
                                 means meta.json was read and `goal` is
                                 empty/absent — the honest "this session
                                 declared no goal"; "unreadable" means no
                                 `holder_sid`, no session dir, or the
                                 fail-soft `except` path fired below — a
                                 goal-machinery-absent/evidence-gap case,
                                 never conflated with "declared no goal"
      holder_branch          - meta.json's "branch" field
      recent_paths            - list[str], most-recent-first, deduplicated,
                                 <= 10 entries; only populated when
                                 `want_activity=True`
      recent_paths_source    - "transcript" | "unavailable"
      scope_overlap           - True/False/None; whether `holder_sid` is a
                                 CLAIMANT (`claim_index.lookup()`, i.e. what
                                 it has actually touched/claimed — see
                                 `_claim_scope_overlap`) on the file set
                                 resolved from `artifact_path` (preferred,
                                 routes through `claim_neighbours.
                                 find_neighbours()` for the full
                                 `deliverable_id` handoff bridge) or, absent
                                 that, `scope` directly. `None` means
                                 genuinely unresolvable — no `scope` AND no
                                 `artifact_path`, or `artifact_path`
                                 resolved `UNRESOLVABLE` — NEVER merely
                                 "either side was empty" (an explicit
                                 `scope: []` is resolved-empty and reads
                                 `False`). This is evidence, not a gate: NOT
                                 the "either-side-empty counts as overlap"
                                 convention `_scopes_intersect` uses for
                                 blocking decisions. Only computed when
                                 `want_activity=True`, same activity-cap
                                 cost gate `recent_paths` uses (AC6) — a
                                 `claim_index.lookup()` rebuild is O(claims-
                                 corpus), not free, per that module's own
                                 docstring.

    Fail-soft (module contract): any exception during evidence-gathering
    yields `evidence_error` and NEVER raises, changes a verdict, or hangs.
    Fields already successfully resolved before the exception fired are
    preserved rather than clobbered, by omission — the handler never
    reassigns them:
      `liveness_basis`/`last_activity_age_sec` — always preserved by
        omission (never reassigned in the handler; their pre-exception
        value, `None` or resolved, stands either way).
      `holder_goal`/`holder_goal_state`/`holder_branch` — preserved only
        when `holder_goal_state` had already reached `"declared"`/
        `"undeclared"` (i.e. the goal read completed before the raise);
        otherwise reset to `None`/`"unreadable"`.
    `recent_paths`, `recent_paths_source`, `scope_overlap` always reset —
    they are only ever produced after the point where this exception can
    fire. This applies ONLY to the evidence fields — it never touches
    `holder_live`/`verdict`, which the caller already resolved via
    `session_live`/`claim_holder_live` before ever calling this function.

    `holder_sid` is Optional because a claim dir CAN name no holder and still
    read live: `liveness.claim_holder_live` falls back to the ephemeral-pid
    test for a legacy dir with no `session_id` file, and a half-written dir
    can lose that file to a concurrent takeover between two reads. A `None`
    holder is an evidence gap, not an error — it yields the all-`None` result
    below by the same fail-soft contract, never an exception into a caller
    that is mid-verdict.

    `artifact_path` (Optional) is the CALLER's own claimed artifact (a
    plan, sizing, or handoff) — pass it to get `scope_overlap`'s full C1
    join, including a handoff's `deliverable_id` bridge (AC2). `caller_sid`
    is an optional pass-through to `claim_neighbours.find_neighbours()` (the
    calling session's own id, excluded from its neighbour set) — same
    default-resolves-if-omitted contract that function documents. When
    `artifact_path` is omitted, `scope` (already-resolved paths, e.g. a
    plan's own frontmatter `scope:`) is used directly against
    `claim_index.lookup()` instead — the pre-C3 call shape, kept for
    callers that have already resolved a file set themselves.
    """
    result: dict[str, Any] = {
        "liveness_basis": None,
        "last_activity_age_sec": None,
        "holder_goal": None,
        "holder_goal_state": "unreadable",
        "holder_branch": None,
        "recent_paths": [],
        "recent_paths_source": "unavailable",
        "scope_overlap": None,
    }

    if not holder_sid:
        return result

    try:
        sdir = core.session_dir(holder_sid, str(repo_root))
        if not sdir or not Path(sdir).is_dir():
            return result

        result["liveness_basis"] = _liveness_basis(holder_sid, str(repo_root))
        result["last_activity_age_sec"] = _last_activity_age_sec(sdir)
        result["holder_goal"] = _meta_str_or_none(sdir, "goal")
        result["holder_goal_state"] = "declared" if result["holder_goal"] else "undeclared"
        result["holder_branch"] = _meta_str_or_none(sdir, "branch")

        if not want_activity:
            return result

        from coordinator_core.ops.check_em_environment import _resolve_transcript

        home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        user_claude = Path(home) / ".claude" if home else Path(".claude")
        transcript = _resolve_transcript("", user_claude, holder_sid)
        if not transcript:
            return result

        recent_paths = _recent_paths_from_transcript(transcript, repo_root)
        result["recent_paths"] = recent_paths
        result["recent_paths_source"] = "transcript"

        result["scope_overlap"] = _claim_scope_overlap(
            holder_sid,
            repo_root,
            scope=scope,
            artifact_path=artifact_path,
            caller_sid=caller_sid,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - fail-soft is the contract here
        result["evidence_error"] = f"{type(exc).__name__}: {exc}"
        if result["holder_goal_state"] == "unreadable":
            result["holder_goal"] = None
            result["holder_branch"] = None
        result["recent_paths"] = []
        result["recent_paths_source"] = "unavailable"
        result["scope_overlap"] = None
        return result

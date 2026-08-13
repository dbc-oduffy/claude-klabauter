"""
coordinator_core.pickup_assemble.holder_evidence — decidable evidence about
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

Spec backlink: pln-pickup-as-a-code-computed-deci-7394dc
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.check_em_environment import _resolve_transcript
from coordinator_core.session import core
from coordinator_core.session import liveness as _liveness

#: Cap on how much of a transcript's tail we ever read, regardless of file
#: size — a multi-MB transcript must not be fully parsed just to answer
#: "what were the last few tool calls". 256 KiB comfortably covers the
#: ~200 most-recent jsonl records on a normal transcript.
_TRANSCRIPT_TAIL_BYTES = 256 * 1024

#: How many trailing well-formed jsonl records to actually parse once the
#: tail bytes are decoded — bounds CPU on a transcript with many short lines
#: inside the byte cap.
_TRANSCRIPT_TAIL_RECORDS = 200

#: Cap on the number of recent paths returned — most-recent-first.
_RECENT_PATHS_CAP = 10

#: `tool_use` input keys that name a file-ish path directly.
_PATH_INPUT_KEYS = ("file_path", "notebook_path", "path")

#: Loose path-shaped token extractor for `command` strings (Bash tool
#: invocations) — matches a relative/absolute-looking path segment
#: containing at least one `/` or a recognizable extension. Intentionally
#: permissive: false positives here just add a noisy `recent_paths` entry,
#: never a wrong verdict.
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

    Vocabulary (six values; see `live_session_verdicts`'s docstring for the
    full per-arm derivation):
      "harness-registry"      — harness-written process identity, stronger
                                 evidence than `"stable-pid"`; `age_sec` is
                                 always `None` on this basis.
      "stable-pid"            — Layer 1 (PPID-authoritative) was consulted.
      "recency-window"        — Layer 2 recency fallback, `last_activity`
                                 present and parseable.
      "recency-window-mtime"  — Layer 2 recency fallback with the meta-less/
                                 mid-write mtime-substitution recency source.
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


#: Back-compat alias — `holder_evidence()` below is this module's own
#: pre-existing internal caller; kept private-named so no behaviour changes
#: for it while `liveness_basis` becomes the public entry point.
_liveness_basis = liveness_basis


def _last_activity_age_sec(sdir: str) -> Optional[int]:
    """Seconds between now and meta.json's `last_activity`, or `None` when
    the field is absent/unparseable — an evidence gap, not a zero age."""
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
    """Local re-derivation of `pickup_assemble._parse_scope_entry`'s split
    of one `scope:` entry into `(repo_id, path)` — kept as a private copy
    rather than an upward import (this module sits below `pickup_assemble`'s
    `__init__`, which is the one importing THIS module, not the reverse)."""
    match = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", entry.strip())
    if match is None:
        return None, entry.strip()
    return match.group(1), match.group(2).strip()


def _scope_path_overlaps(scope: list[str], candidate_path: str) -> bool:
    """True iff `candidate_path` (a repo-relative path pulled from recent
    tool-use activity) falls under, or matches, any bare-local entry in
    `scope`. Sibling-repo-qualified scope entries (`<repo-id>: <path>`) are
    skipped here — `recent_paths` is always local-repo-relative, so a
    cross-repo scope entry cannot be evaluated against it."""
    cand = candidate_path.strip().rstrip("/")
    if not cand:
        return False
    for entry in scope:
        repo_id, path = _parse_scope_entry(entry)
        if repo_id is not None:
            continue
        path = path.rstrip("/")
        if not path:
            continue
        if cand == path or cand.startswith(path + "/") or path.startswith(cand + "/"):
            return True
    return False


def _extract_paths_from_tool_use(tool_input: dict, repo_root: Path) -> list[str]:
    """Pull file-ish paths out of one `tool_use` block's `input` dict,
    normalized to repo-relative when they resolve under `repo_root`."""
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
        # Only the truncated-read case can start mid-line; a read from byte
        # 0 has no partial first line to discard.
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
    want_activity: bool = False,
) -> dict[str, Any]:
    """Decidable evidence about what `holder_sid` is actually doing, to
    accompany (never replace) the `holder_live` boolean `compute_claim_grant`
    / `compute_competing_claim` already resolve via
    `coordinator_core.session.liveness`.

    Always returns a dict with these keys (each `None` when unknown):
      liveness_basis        - "harness-registry" | "stable-pid" |
                                 "recency-window" | "recency-window-mtime" |
                                 "harness-registry-elsewhere" | "unknown"
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
      scope_overlap           - True/False/None; None when either
                                 `recent_paths` or `scope` is empty (unknown
                                 reads as unknown here — NOT the "either-side-
                                 empty counts as overlap" convention
                                 `_scopes_intersect` uses for blocking
                                 decisions; this is evidence, not a gate)

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

        home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        user_claude = Path(home) / ".claude" if home else Path(".claude")
        transcript = _resolve_transcript("", user_claude, holder_sid)
        if not transcript:
            return result

        recent_paths = _recent_paths_from_transcript(transcript, repo_root)
        result["recent_paths"] = recent_paths
        result["recent_paths_source"] = "transcript"

        scope = scope or []
        if recent_paths and scope:
            result["scope_overlap"] = any(
                _scope_path_overlaps(scope, path) for path in recent_paths
            )
        return result
    except Exception as exc:  # noqa: BLE001 - fail-soft is the contract here
        # Review: code-reviewer P3 (2026-08-13) — only clobber fields that
        # were never resolved before the exception fired. A transcript/
        # recent-paths hiccup after holder_goal/holder_goal_state/
        # holder_branch were already read must not discard a genuinely
        # `declared` goal down to `unreadable` — that reintroduces the
        # conflation holder_goal_state exists to prevent (spec AC3).
        result["evidence_error"] = f"{type(exc).__name__}: {exc}"
        if result["holder_goal_state"] == "unreadable":
            result["holder_goal"] = None
            result["holder_branch"] = None
        result["recent_paths"] = []
        result["recent_paths_source"] = "unavailable"
        result["scope_overlap"] = None
        return result

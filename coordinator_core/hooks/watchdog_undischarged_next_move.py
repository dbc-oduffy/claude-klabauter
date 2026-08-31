"""
coordinator_core.hooks.watchdog_undischarged_next_move — PostToolUse(Skill|Agent) +
Stop warm-door counterpart of DoE-claude's
`coordinator/hooks/scripts/watchdog-undischarged-next-move.py`, folded together
with its three sibling transport modules (`_next_move_ledger.py`, `_posture.py`,
`_touch_record.py` — 550 + 700 + 197 + 123 = 1,570 combined DoE source lines).
This op folds all four into one module rather than four files: DoE's split is a
`sys.path.insert` sibling-import convenience for a per-invocation script; a warm
op imports the resident engine's own package tree and has no such constraint.
Measured own line count: see this module's own length at the bottom of this
docstring's spec backlink trail — re-measured, not assumed against the 1,570
estimate (docs/reference/warm-hook-migration.md's own instruction).

TWO EVENTS, ONE OP. Registered on both `PostToolUse` (matcher `Skill|Agent`) and
`Stop` (matcher ""), exactly like the source script's dual `hooks.json`
registration — `_handler` branches on the payload shape (`tool_name` a string ->
PostToolUse; `transcript_path` present -> Stop) since the two events carry
different stdin shapes and neither wire format sends a distinguishing
`hook_event_name` field. Verbatim precedence rule from the source script: a
payload carrying a non-string `tool_name` (e.g. `null`) alongside
`transcript_path` still routes to Stop.

CLASSIFICATION — MUTATING, not COMPUTE_ONLY (per this chunk's own dispatch
brief SCOPE DECISION). Five-question checklist, mirroring the predecessor C2's
own COMPUTE_ONLY justification in shape, opposite in every answer:
  1. Does it write, delete, or reorder any state file, queue, or git object?
     YES — the PostToolUse leg opens/discharges obligations by rewriting
     `next-move-ledger.jsonl`; the Stop leg additionally drains and deletes
     `obligations-inbound.jsonl` and stamps the one-fire latch.
  2. Does it write into rag's relational store?                          No.
     Write target is `state/subagent-share/<session_id>/` (session-runtime
     bookkeeping, matching `coordinator_core.group_em.obligations`'s own
     writer of the sibling `obligations-inbound.jsonl` in the SAME directory
     — see "LEDGER LOCATION" below), never `state/`'s substrate proper.
  3. Does it open any file for write (including sentinel creation)?      YES.
  4. Does it mutate shared mutable state outside its own module?         YES —
     the ledger is a durable, cross-call, cross-session-lifetime record; a
     second Stop call for the same obligation reads what this call wrote.
  5. Does it produce side effects observable across process boundaries?  YES —
     `coordinator_core.group_em.send_pass.undischarged_obligations` and
     `coordinator_core.group_em.obligations.for_peer` both read the exact
     ledger this op writes.
Durable cross-call state, keyed by session, read back by a LATER, INDEPENDENT
invocation (the next Stop, or an entirely different plane's read) is the
opposite of the in-memory-only, single-call-lifetime shape
`nudge_autonomous_askuserquestion`'s COMPUTE_ONLY classification rests on.

LEDGER LOCATION — repo_root-relative, NOT the git dir. `state/subagent-
share/<session_id>/next-move-ledger.jsonl` is the CANONICAL path: it is what
DoE's own `_next_move_ledger.ledger_path()` resolves to (repo-root anchored,
per that module's own docstring), and — load-bearing for this port —
`coordinator_core.group_em.obligations` and `coordinator_core.group_em.
send_pass` (already shipped in THIS repo) read/append the ledger and its
`obligations-inbound.jsonl` sibling at exactly this same repo-root-relative
path. Placing the ledger anywhere else (e.g. under the git dir) would silently
break interop with that already-shipped producer/reader — two callers
disagreeing about a shared file's location is worse than either being merely
inconvenient. `repo_root` here is resolved from `payload["cwd"]` via
`coordinator_core.git.repo_root.show_toplevel` (zero-spawn walk), never the
framework-supplied `repo_root` handler argument (see op_scopes.py entry —
this op is scope "none", matching every other payload-cwd-resolving hooks.*
op in this family).

GIT DIR — resolve via `resolve_git_dir`, per this chunk's own dispatch brief.
The sizing-object lookup (the PostToolUse leg's `coordinator:sizing`/`
coordinator:plan` branch) needs to read the SAME session's touch-record to
find "the sizing object this session most recently touched" — the source
script reads that from `<git_dir>/coordinator-sessions/<session_id>/
touch-record.jsonl`. This op resolves that `git_dir` through `coordinator_core.
git.git_dir.resolve_git_dir` (given an already-walked repo root), in-process
and zero-spawn — never re-deriving the `.git`-file-vs-directory indirection
by hand the way DoE's own `_resolve_git_dir` did, and never spawning `git
rev-parse`. This IS the worktree-PRIVATE gitdir (not the common dir another
op, `track_touched_files`, uses to WRITE that same touch-record) — identical
to the common dir in the overwhelmingly common non-worktree case, and an
honest, named limitation in the rare linked-worktree case: this is a
byte-faithful port of what the SOURCE SCRIPT already did (it also resolved
the private gitdir, via its own `_repo_root`/`_resolve_git_dir` pair), not a
regression introduced by this port.

TWO TRAPS (dispatch brief, verbatim):
  (1) The ledger is keyed on the session TAKEN FROM THE PAYLOAD
      (`payload["session_id"]`), never from this engine process's own
      environment or session — the resident engine serves ~50 concurrent
      sessions, and none of them is this process's own.
  (2) `_posture.resolve_posture()`'s unkeyed module-level cache
      (`_cached_posture` / `_cached_posture_by_root` in the DoE source) is an
      ADAPTER hazard specific to that per-invocation script's process
      lifetime — cached for the life of ONE hook process, which is correct
      there and WRONG here, where the same module object outlives every
      caller across every session. This op does NOT reproduce that cache: it
      reuses `nudge_autonomous_askuserquestion._resolve_posture`, this
      family's own already-built, already-warm-safe posture resolver (C2),
      which re-reads `payload["cwd"]`'s `coordinator.local.md` then
      `~/.claude/coordinator-identity.yaml` fresh on every call and holds no
      module-level cache at all — the correct shape for a resident process,
      not a defect in the DoE source to be filed against DoE's tree.

ROUTE TERMINALS, SEAM TABLE, EXEMPTIONS — verbatim ports of the source
script's own static tables; see each constant's own comment below for the
rationale, kept short here since the source script's docstring (linked below)
carries the full account and this port changes none of the decisions, only
the transport.

ONE FIRE PER OBLIGATION (source script's A6) — `_mark_fired` is the latch; a
second Stop with the same obligation open finds it already fired and stays
silent. Ported with the same race-closing token-confirm technique as DoE's
own `mark_fired` (two Stop calls racing the same unfired obligation must not
both win the latch) — necessary here too: this op's own MUTATING
classification serialises calls THROUGH THIS ENGINE, but the ledger file
itself can still be touched by `coordinator_core.group_em.obligations`'s
intake writer concurrently, so the read-then-write race this token exists to
close is not fully foreclosed by in-engine MUTATING serialisation alone.

DRAIN INTAKE — simplified relative to DoE's own `.draining`-claim-file
crash-safety protocol. That protocol exists to let TWO INDEPENDENT PROCESSES
race a drain safely with no cross-process lock. This op's own Stop leg is the
SOLE drainer, and DR-208's MUTATING-ops-are-serial-by-construction posture
(`op_scopes.py` — "HTTP/UDS gating vacated by DR-215 ... MUTATING ops are
serial-by-construction in the in-process model") means no second in-engine
Stop call for the same session can be mid-drain concurrently. The two-phase
claim/orphan-recovery dance therefore has nothing left to protect against in
this process model; a direct read-fold-delete is behaviourally equivalent for
every case that can actually arise here, and is implemented as such. Rows
malformed against the intake schema are silently skipped (not quarantined to
a `.rejected.jsonl` file) — a deliberate, named reduction in scope: this
op's own producer (`coordinator_core.group_em.obligations.record`) already
validates before writing (its own `_validate_row`), so a malformed row
reaching this drain would mean a THIRD, as-yet-nonexistent producer, which
this port does not attempt to diagnose.

SEVERITY BY POSTURE (source script's A8) — precision: advisory, non-blocking.
default/substrate-free: blocking, so the turn does not end and the EM
continues in-turn with the command in hand. Represented here by REUSING the
two existing generic envelope builders (`allow_advisory` / `deny`) against
event_name="Stop" rather than inventing a seventh shape: both builders are
already event-name-agnostic (see their own docstrings), and no envelope
builder in this suite has a Stop-specific shape today because no other
`hooks.*` op fires on Stop yet. `warm/hook_http.py`'s `SERVED_EVENTS` /
`BLOCKING_EVENTS` (currently PreToolUse-only) do not yet route a Stop
registration through the warm door at all — widening that transport is
explicitly OUT OF SCOPE for this chunk (`warm/hook_http.py` is not in this
chunk's `writes:` list); this op's return contract is written against "the
same JSON shape the source script printed to stdout" per the runbook's own
Step 2 obligation, and the transport-level Stop wiring is inherited as a
named gap for whichever later chunk widens `SERVED_EVENTS`, the same way C2
named an env-threading gap for `nudge_autonomous_askuserquestion`.

Contract:
  PostToolUse payload -- tool_name, tool_input, session_id, cwd, agent_id...
  PostToolUse return  -- always no_advisory() (silent bookkeeping leg)
  Stop payload        -- session_id, transcript_path, cwd, stop_hook_active,
                          agent_id...
  Stop return         -- no_advisory() (nothing undischarged, or suppressed);
                          allow_advisory("Stop", text) at posture "precision";
                          deny("Stop", text) at posture "default"/
                          "substrate-free"

Graceful degradation: any failure to resolve the repo root, git dir, or read
the ledger falls through to a silent no-op — `no_advisory()` — unconditionally,
matching the source script's own fail-open-everywhere posture.

Spec backlink: docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md
(chunk C4); docs/reference/warm-hook-migration.md (candidate-selection input,
PostToolUse Skill|Agent row); DoE-claude
`coordinator/hooks/scripts/watchdog-undischarged-next-move.py` (source,
spec backlink docs/plans/2026-08-10-posture-scaled-autonomous-disposition.md
chunk C3); DoE-claude `coordinator/docs/wiki/obligations-inbound-intake.md`
(the cross-plane ledger contract this op now consumes on the writing side,
alongside `coordinator_core.group_em.obligations`'s existing producer).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from typing import Any, Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import allow_advisory, deny, no_advisory
from coordinator_core.hooks.nudge_autonomous_askuserquestion import (
    _resolve_posture as _resolve_posture_for_cwd,
)
from coordinator_core.ipc import register_op

# ---------------------------------------------------------------------------
# Static seam table (emission side) -- verbatim port of the source script's
# own table. See the source script's module docstring for the full rationale
# behind each row; unchanged by this port.
# ---------------------------------------------------------------------------

_SEAM_SIZING_ROUTED = "sizing-routed"
_SEAM_PLAN_REVIEW = "plan->review"
_SEAM_REVIEW_A1_A2 = "review-a1-a2"
_SEAM_EXECUTE_WAVE = "execute->wave"
_SEAM_PICKUP_NEXT_MOVE = "pickup->next-move"

# route -> the literal next_action a routed sizing object machine-resolves.
# `pm-decision` and `goal-setting` are deliberately absent -- their whole
# point is that the next move is a PM call, not a machine-resolved one.
_ROUTE_TERMINAL = {
    "dispatch": "Agent(coordinator:executor)",
    "spec-dispatch": "Agent(coordinator:executor)",
    "plan": "Skill(coordinator:plan)",
    "shape": "Skill(coordinator:plan)",
    "roadmap": "Skill(coordinator:plan)",
}

_REVIEW_NEXT_ACTION = "Agent(<named Opus reviewer>)"
_EXECUTE_NEXT_ACTION = "Agent|Workflow(coordinator:executor or the emitted plan script)"
_REVIEW_TERMINAL = "Skill(coordinator:review)"

_ANY_CALL_KIND = "Skill|Agent"
_PICKUP_NEXT_ACTION = "Skill|Agent(the narrated next move)"

_SIZING_PATH_RE = re.compile(r"^state/sizings/[^/]+\.ya?ml$")
_APPETITE_DIVERGENCE_DETENT = "appetite_exceeded"
_POST_SIZE_PROMPT_DETENT = "post_size_prompt_pending"


# ---------------------------------------------------------------------------
# Repo root / git dir resolution -- zero-spawn, per this chunk's own brief.
# ---------------------------------------------------------------------------


def _repo_root_for(cwd: Any) -> Optional[str]:
    """Zero-spawn repo-root walk from `cwd` (never this process's own cwd)."""
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        toplevel = show_toplevel(cwd)
    except Exception:
        return None
    return toplevel if isinstance(toplevel, str) and toplevel else None


def _git_dir_for(cwd: Any) -> Optional[str]:
    """Zero-spawn git-dir resolution from `cwd`, via `resolve_git_dir` per the
    dispatch brief -- never a hand-rolled `.git`-file-vs-directory walk."""
    repo_root = _repo_root_for(cwd)
    if repo_root is None:
        return None
    try:
        return str(resolve_git_dir(repo_root))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ledger storage -- state/subagent-share/<session_id>/{next-move-ledger.jsonl,
# obligations-inbound.jsonl}, repo-root relative. See module docstring's
# "LEDGER LOCATION" for why this is NOT under the git dir.
# ---------------------------------------------------------------------------

_LEDGER_FILENAME = "next-move-ledger.jsonl"
_INTAKE_FILENAME = "obligations-inbound.jsonl"
_INTAKE_SCHEMA = 1
_INTAKE_OPS = ("open", "progress", "blocked", "discharge")


def _session_share_dir(repo_root: str, session_id: str) -> str:
    return os.path.join(repo_root, "state", "subagent-share", session_id)


def _ledger_path(repo_root: str, session_id: str) -> str:
    return os.path.join(_session_share_dir(repo_root, session_id), _LEDGER_FILENAME)


def _intake_path(repo_root: str, session_id: str) -> str:
    return os.path.join(_session_share_dir(repo_root, session_id), _INTAKE_FILENAME)


def _read_records(repo_root: str, session_id: str) -> list:
    path = _ledger_path(repo_root, session_id)
    if not os.path.isfile(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _write_records(repo_root: str, session_id: str, records: list) -> bool:
    """Atomically replace the ledger file (temp file + `os.replace`, same
    directory) -- atomic on both POSIX and Windows, closing the same
    read-modify-write race DoE's own `_write_records` closes."""
    path = _ledger_path(repo_root, session_id)
    directory = os.path.dirname(path)
    tmp_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".next-move-ledger-", suffix=".tmp", dir=directory
        )
        try:
            try:
                handle = os.fdopen(tmp_fd, "w", encoding="utf-8")
            except Exception:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
                raise
            with handle:
                for record in records:
                    handle.write(json.dumps(record, sort_keys=True))
                    handle.write("\n")
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None and os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except (OSError, TypeError, ValueError):
        return False
    return True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _open_obligation(
    repo_root: str, session_id: str, obligation_id: str, seam: str, next_action: str
) -> bool:
    """Idempotent against a re-observed seam-opening call: a no-op if an open
    (undischarged) record with this `obligation_id` already exists."""
    records = _read_records(repo_root, session_id)
    for record in records:
        if record.get("obligation_id") == obligation_id and record.get("discharged_at") is None:
            return False
    records.append(
        {
            "obligation_id": obligation_id,
            "seam": seam,
            "next_action": next_action,
            "opened_at": _now_iso(),
            "progressed_at": None,
            "blocked_at": None,
            "blocked_on_session_id": None,
            "blocked_on_name": None,
            "discharged_at": None,
            "fired": False,
        }
    )
    return _write_records(repo_root, session_id, records)


def _discharge_obligation(repo_root: str, session_id: str, obligation_id: str) -> bool:
    records = _read_records(repo_root, session_id)
    changed = False
    for record in records:
        if record.get("obligation_id") == obligation_id and record.get("discharged_at") is None:
            record["discharged_at"] = _now_iso()
            record["blocked_at"] = None
            record["blocked_on_session_id"] = None
            record["blocked_on_name"] = None
            changed = True
    if not changed:
        return False
    return _write_records(repo_root, session_id, records)


def _mark_fired(repo_root: str, session_id: str, obligation_id: str) -> bool:
    """One-fire-per-obligation latch (source script's A6), race-closed the
    same way DoE's own `mark_fired` is: tag this write with a private
    one-shot token and re-read after the replace, so only the writer whose
    token survives is told it won the latch."""
    records = _read_records(repo_root, session_id)
    changed = False
    token = uuid.uuid4().hex
    for record in records:
        if record.get("obligation_id") == obligation_id and not record.get("fired"):
            record["fired"] = True
            record["fire_token"] = token
            changed = True
    if not changed:
        return False
    if not _write_records(repo_root, session_id, records):
        return False
    for record in _read_records(repo_root, session_id):
        if record.get("obligation_id") == obligation_id:
            return record.get("fire_token") == token
    return False


def _find_undischarged_unfired(repo_root: str, session_id: str) -> Optional[dict]:
    for record in _read_records(repo_root, session_id):
        if record.get("discharged_at") is None and not record.get("fired"):
            return record
    return None


def _validate_intake_row(row: Any, session_id: str) -> Optional[str]:
    if not isinstance(row, dict):
        return "not a JSON object"
    if row.get("schema") != _INTAKE_SCHEMA:
        return "unsupported schema"
    if row.get("session_id") != session_id:
        return "session_id does not match this file's session"
    op = row.get("op")
    if op not in _INTAKE_OPS:
        return "unknown op"
    obligation_id = row.get("obligation_id")
    if not isinstance(obligation_id, str) or not obligation_id:
        return "missing obligation_id"
    if op == "open":
        for field_name in ("seam", "next_action"):
            value = row.get(field_name)
            if not isinstance(value, str) or not value:
                return "op=open missing " + field_name
    if op == "blocked":
        blocked_on = row.get("blocked_on_session_id")
        if not isinstance(blocked_on, str) or not blocked_on:
            return "op=blocked missing blocked_on_session_id"
    return None


def _apply_intake_row(repo_root: str, session_id: str, row: dict) -> bool:
    op = row["op"]
    obligation_id = row["obligation_id"]
    if op == "open":
        return _open_obligation(repo_root, session_id, obligation_id, row["seam"], row["next_action"])
    if op == "discharge":
        return _discharge_obligation(repo_root, session_id, obligation_id)
    # "progress" / "blocked" rows carry no observable effect on THIS op's own
    # read surface (`_find_undischarged_unfired` only reads discharged_at /
    # fired) -- accepted (not rejected) so a valid row of either kind is
    # consumed rather than left to accumulate, but reported as a no-op.
    return False


def _drain_intake(repo_root: str, session_id: str) -> None:
    """Fold `obligations-inbound.jsonl` into the ledger, then delete it.

    Simplified relative to DoE's own two-phase `.draining`-claim protocol --
    see module docstring's "DRAIN INTAKE" section for why that crash-safety
    dance has nothing left to protect against in this op's process model.
    Never raises; a fold that cannot complete leaves the intake file in
    place for the next Stop call on this session to retry.
    """
    path = _intake_path(repo_root, session_id)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return

    lines = text.split("\n")
    has_trailing_partial = bool(lines[-1])
    if not has_trailing_partial:
        lines = lines[:-1]

    try:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if _validate_intake_row(row, session_id) is not None:
                continue
            _apply_intake_row(repo_root, session_id, row)
    except Exception:
        # A fold that cannot commit keeps the file: rows survive to the next
        # drain, where the replay is idempotent (open/discharge both dedupe).
        return

    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Self-contained sizing-object resolution -- reads the session's touch-record
# under the resolved GIT DIR (see module docstring) to find "the sizing
# object THIS session routed", mirroring the source script's own
# `_newest_touched_sizing_path` / `_sizing_route_and_exemption`.
# ---------------------------------------------------------------------------


def _touch_record_jsonl_paths(session_dir: str) -> list:
    paths = []
    try:
        with open(
            os.path.join(session_dir, "touch-record.jsonl"),
            "r",
            encoding="utf-8",
            errors="replace",
        ) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                rel = row.get("path")
                if isinstance(rel, str) and rel:
                    paths.append(rel)
    except OSError:
        pass
    return paths


def _touched_txt_paths(session_dir: str) -> list:
    paths = []
    try:
        with open(
            os.path.join(session_dir, "touched.txt"), "r", encoding="utf-8", errors="replace"
        ) as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 3 and parts[0] in ("T", "R"):
                    paths.append(parts[-1])
                else:
                    paths.append(line)
    except OSError:
        pass
    return paths


def _newest_touched_sizing_path(git_dir: str, session_id: str) -> Optional[str]:
    """Source-then-recency: the last new-file match wins; the legacy file is
    consulted only when the new file has none. See the source script's own
    docstring for why naive concatenation-then-last-match is wrong for a
    partially-migrated session."""
    session_dir = os.path.join(git_dir, "coordinator-sessions", session_id)

    candidate = None
    for rel in _touch_record_jsonl_paths(session_dir):
        if _SIZING_PATH_RE.match(rel):
            candidate = rel
    if candidate is not None:
        return candidate

    for rel in _touched_txt_paths(session_dir):
        if _SIZING_PATH_RE.match(rel):
            candidate = rel
    return candidate


def _extract_scalar(lines, key: str) -> Optional[str]:
    prefix = key + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            return value.strip("'\"")
    return None


def _extract_detents(lines) -> list:
    values = []
    collecting = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not collecting:
            if not stripped.startswith("detents:"):
                continue
            rest = stripped[len("detents:"):].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1]
                return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
            collecting = True
            continue
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip("'\""))
        elif stripped == "":
            continue
        else:
            break
    return values


def _is_null_scalar(value) -> bool:
    return value is None or value in ("null", "~", "None", "")


def _sizing_route_and_exemption(repo_root: str, rel_path: str):
    """Return (route, exempt). Any read failure returns (None, True) --
    "cannot prove the exemption doesn't apply" fails toward silence."""
    try:
        with open(os.path.join(repo_root, rel_path), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None, True

    route = _extract_scalar(lines, "route")
    fork = _extract_scalar(lines, "fork")
    xl_exit = _extract_scalar(lines, "xl_exit")
    detents = _extract_detents(lines)

    fork_open = (
        _APPETITE_DIVERGENCE_DETENT in detents or _POST_SIZE_PROMPT_DETENT in detents
    ) and _is_null_scalar(fork)
    xl_open = route == "pm-decision" and _is_null_scalar(xl_exit)
    return route, (fork_open or xl_open)


# ---------------------------------------------------------------------------
# Discharge matching.
# ---------------------------------------------------------------------------


def _split_call(next_action: str):
    if not next_action or "(" not in next_action or not next_action.endswith(")"):
        return None, None
    kind, _, rest = next_action.partition("(")
    return kind, rest[:-1]


def _matches_next_action(next_action: str, tool_name, tool_input) -> bool:
    kind, _ident = _split_call(next_action)
    if kind is None:
        return False
    if "|" in kind:
        return tool_name in tuple(part for part in kind.split("|") if part)
    if kind == "Skill":
        if tool_name != "Skill" or not isinstance(tool_input, dict):
            return False
        skill = tool_input.get("skill")
        if not isinstance(skill, str):
            skill = tool_input.get("command")
        return skill == _ident
    if kind == "Agent":
        return tool_name == "Agent"
    return False


def _discharge_matching(repo_root: str, session_id: str, tool_name, tool_input) -> None:
    """Cap discharge at the single OLDEST matching open record per call, so
    one ambiguous terminal call never silently discharges an obligation it
    did not actually satisfy (verbatim reasoning from the source script)."""
    for record in _read_records(repo_root, session_id):
        if record.get("discharged_at") is not None:
            continue
        next_action = record.get("next_action")
        obligation_id = record.get("obligation_id")
        if not isinstance(next_action, str) or not isinstance(obligation_id, str):
            continue
        if _matches_next_action(next_action, tool_name, tool_input):
            _discharge_obligation(repo_root, session_id, obligation_id)
            return


# ---------------------------------------------------------------------------
# Emission (PostToolUse leg).
# ---------------------------------------------------------------------------


def _handle_post_tool_use(payload: Mapping) -> None:
    if payload.get("agent_id"):
        return  # a subagent's own tool call, not the EM's

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    repo_root = _repo_root_for(payload.get("cwd"))
    if repo_root is None:
        return

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Discharge first -- this same call may close an obligation opened by an
    # earlier turn, independent of anything it opens below.
    _discharge_matching(repo_root, session_id, tool_name, tool_input)

    if tool_name != "Skill":
        return

    skill = tool_input.get("skill")
    if not isinstance(skill, str):
        skill = tool_input.get("command")
    if not isinstance(skill, str):
        return

    if skill == "coordinator:review":
        _open_obligation(repo_root, session_id, _SEAM_REVIEW_A1_A2, _SEAM_REVIEW_A1_A2, _REVIEW_NEXT_ACTION)
        return

    if skill == "coordinator:pickup":
        _open_obligation(
            repo_root, session_id, _SEAM_PICKUP_NEXT_MOVE, _SEAM_PICKUP_NEXT_MOVE, _PICKUP_NEXT_ACTION
        )
        return

    if skill == "coordinator:execute-plan":
        _open_obligation(repo_root, session_id, _SEAM_EXECUTE_WAVE, _SEAM_EXECUTE_WAVE, _EXECUTE_NEXT_ACTION)
        return

    if skill not in ("coordinator:sizing", "coordinator:plan"):
        return

    git_dir = _git_dir_for(payload.get("cwd"))
    if git_dir is None:
        return
    rel_path = _newest_touched_sizing_path(git_dir, session_id)
    if rel_path is None:
        return
    route, exempt = _sizing_route_and_exemption(repo_root, rel_path)
    if exempt or route is None:
        return

    if skill == "coordinator:sizing":
        next_action = _ROUTE_TERMINAL.get(route)
        if next_action is not None:
            _open_obligation(repo_root, session_id, _SEAM_SIZING_ROUTED, _SEAM_SIZING_ROUTED, next_action)
        return

    # skill == "coordinator:plan" -- only the FULL "plan" terminal opens this
    # obligation; "spec-dispatch" (or any other route) does not.
    if route == "plan":
        _open_obligation(repo_root, session_id, _SEAM_PLAN_REVIEW, _SEAM_PLAN_REVIEW, _REVIEW_TERMINAL)


# ---------------------------------------------------------------------------
# Stop leg -- reads the ledger and NOTHING ELSE (per source script's own
# design intent: this hook infers nothing, it only reports an unresolved
# obligation another observation already opened).
# ---------------------------------------------------------------------------


def _handle_stop(payload: Mapping) -> dict:
    if payload.get("agent_id"):
        return no_advisory()
    if payload.get("stop_hook_active"):
        return no_advisory()  # avoid re-entering on our own already-fired Stop

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    repo_root = _repo_root_for(payload.get("cwd"))
    if repo_root is None:
        return no_advisory()

    try:
        _drain_intake(repo_root, session_id)
    except Exception:
        pass

    record = _find_undischarged_unfired(repo_root, session_id)
    if record is None:
        return no_advisory()

    next_action = record.get("next_action")
    obligation_id = record.get("obligation_id")
    if not isinstance(next_action, str) or not next_action or not isinstance(obligation_id, str):
        return no_advisory()

    text = (
        "An earlier turn resolved a next move that was never invoked "
        f"({record.get('seam', 'unknown-seam')}). Invoke it now: {next_action}"
    )

    # A failed latch write degrades to a silent miss, never a repeat fire --
    # a repeat is worse than a miss (source script's own Anti-scope).
    if not _mark_fired(repo_root, session_id, obligation_id):
        return no_advisory()

    try:
        posture = _resolve_posture_for_cwd(payload.get("cwd") or "")
    except Exception:
        posture = "precision"

    if posture in ("default", "substrate-free"):
        return deny("Stop", text)

    return allow_advisory("Stop", text)


# ---------------------------------------------------------------------------
# Registered op -- dispatches by payload shape, per the source script's own
# `main()` precedence rule (a truthy string `tool_name` always routes to
# PostToolUse, even alongside a `transcript_path` key).
# ---------------------------------------------------------------------------


@register_op("hooks.watchdog_undischarged_next_move")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(Skill|Agent) + Stop: emit/discharge next-move obligations,
    and report an undischarged-and-unfired one at Stop.

    Every input this handler reads comes from `params["payload"]` -- never
    from `os.environ` or this process's own `cwd`/session (see module
    docstring's Trap 1). `repo_root` (the framework-supplied handler
    argument) is unused -- this op is scope "none" and resolves its own repo
    root from `payload["cwd"]`, matching every other payload-cwd-resolving
    hooks.* op in this family (`nudge_autonomous_askuserquestion`,
    `sessionend_archive_session`).
    """
    try:
        payload = params.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}

        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str):
            _handle_post_tool_use(payload)
            return no_advisory()
        if "transcript_path" in payload:
            return _handle_stop(payload)
    except Exception:
        return no_advisory()

    return no_advisory()

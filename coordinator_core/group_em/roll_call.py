"""Roll call: who must be asked, the templated ask, and the per-id reply record.

One record per roll call at ``<gem-home>/roll-call/<roll_call_id>.json``, shaped ``{roll_call_id,
text, created, deadline, addressees: {local, far, excluded}, replies: {<session_key>: {goals,
received_at, late}}, deliveries: {<far key>: {ok, reason, at}}}``. A reply is per goal (goal_id,
target, measured_now, gap, next_lever, expected_gain, owner, eta, optional blocked_on); a reply
missing a field, naming TBD or lacking an ISO-date ETA is refused whole and stores nothing.
Free-form ``activity`` goes only to ``<roll_call_id>.log.jsonl`` beside the record, never into the
record or the status brief. Every read-modify-write runs under ``atomic_record.holder_lock`` on
the record path, so concurrent repliers never lose a reply. No spawn anywhere in this module.

Unimatrix-transport rows: the registry entry marks them ``unimatrixTransport: true`` /
``vendor: "transport"`` (``coordinator/bin/unimatrix-roster.py :: transport_row``) and registers
them as ``sessionId = "peer-<name>-<pid>"`` (``unimatrix-peer-router.py :: Registration``). The
engine roster (``PeerRow``) and the far snapshot (``remote_roster._FIELDS``) drop those marker
fields, so the surviving discriminator on both sides is the ``peer-`` session-id prefix, which
every router face carries (Codex mirrors are ``peer-codex-``).
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.group_em import atomic_record, gem_home, remote_identity, remote_roster

# Pinned equal to watch._TRANSIENT_CODEX_PREFIX by test; watch is too heavy to import here.
CODEX_MIRROR_PREFIX = "peer-codex-"
ROUTER_FACE_PREFIX = "peer-"
RECORD_DIRNAME = "roll-call"
DEFAULT_DEADLINE_MINUTES = 60
GOAL_FIELDS = (
    "goal_id", "target", "measured_now", "gap", "next_lever", "expected_gain", "owner", "eta",
)
BLOCKED_FIELDS = ("what", "owner", "date")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TBD_RE = re.compile(r"\btbd\b", re.IGNORECASE)


class RollCallReplyRefused(Exception):
    """A reply failed N1 validation; ``refusals`` is ``[{goal_id, field, reason}]`` for the EM."""

    def __init__(
        self, roll_call_id: Optional[str], session_key: Optional[str], refusals: list
    ) -> None:
        super().__init__(f"roll call reply refused: {len(refusals)} problem(s)")
        self.roll_call_id = roll_call_id
        self.session_key = session_key
        self.refusals = refusals

    def as_dict(self) -> dict:
        return {
            "roll_call_id": self.roll_call_id,
            "session_key": self.session_key,
            "refusals": self.refusals,
        }


def new_roll_call_id(now: Optional[float] = None) -> str:
    """A fresh, filename-safe id: ``rc-<UTC stamp>-<6 hex>``."""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(time.time() if now is None else now))
    return f"rc-{stamp}-{secrets.token_hex(3)}"


def record_path(roll_call_id: str, home: Optional[Path] = None) -> Path:
    """``<gem-home>/roll-call/<roll_call_id>.json``; ValueError for an unsafe id.

    ``gem_home.HomeRefused`` propagates when the resolved home is inside a checkout.
    """
    if not isinstance(roll_call_id, str) or not _ID_RE.fullmatch(roll_call_id):
        raise ValueError(f"invalid roll_call_id: {roll_call_id!r}")
    base = Path(home) if home is not None else gem_home.resolve_home()
    return base / RECORD_DIRNAME / (roll_call_id + ".json")


def _exclusion(session_id: str, kind: Optional[str]) -> Optional[str]:
    if session_id.startswith(CODEX_MIRROR_PREFIX):
        return "codex_mirror"
    if session_id.startswith(ROUTER_FACE_PREFIX):
        return "unimatrix_transport"
    if kind not in (None, "claude"):
        return "non_claude_worker"
    return None


def resolve_addressees(
    repo_root: Optional[str],
    caller_session_id: Optional[str],
    snapshot_dir: Optional[str] = None,
    local_id: Optional[str] = None,
) -> dict:
    """``{local, far, excluded}``: every live EM on this box and each reachable far box.

    ``local`` rows are ``{session_id, name}`` (addressed by name); ``far`` rows are ``{key,
    box_id, name}`` with ``key = <box_id>:<session_id>``; ``excluded`` rows are ``{key, reason}``
    (reasons: self, codex_mirror, unimatrix_transport, non_claude_worker, no_name,
    box_unreachable). One registry scan plus one listdir and one open per snapshot.
    """
    from coordinator_core.session import peer_roster

    local: list = []
    far: list = []
    excluded: list = []
    for row in peer_roster.build_roster(repo_root, box_wide=True):
        reason = _exclusion(row.session_id, row.kind)
        if reason is None and (row.is_self or row.session_id == caller_session_id):
            reason = "self"
        if reason is None and not row.name:
            reason = "no_name"
        if reason:
            excluded.append({"key": row.session_id, "reason": reason})
        else:
            local.append({"session_id": row.session_id, "name": row.name})

    base = Path(snapshot_dir) if snapshot_dir else settings_home() / "state" / "group-em"
    for box_id, snap in sorted(remote_roster.read_snapshot(base, local_id=local_id).items()):
        if not snap.get("reachable"):
            excluded.append({"key": box_id, "reason": "box_unreachable"})
            continue
        for sess in snap.get("sessions", []):
            sid = sess.get("session_id")
            if not isinstance(sid, str) or not sid:
                continue
            key = remote_identity.peer_key(box_id, sid)
            reason = _exclusion(sid, None)
            if reason is None and not sess.get("name"):
                reason = "no_name"
            if reason:
                excluded.append({"key": key, "reason": reason})
            else:
                far.append({"key": key, "box_id": box_id, "name": sess.get("name")})
    return {"local": local, "far": far, "excluded": excluded}


def render_ask(roll_call_id: str, note: Optional[str] = None) -> str:
    """The one templated ask every addressee receives."""
    lines = [
        f"ROLL CALL {roll_call_id}: stop and answer this before anything else.",
        "",
        "1. Prune your Tasks list: drop finished or stale items so it holds only live work.",
        "2. Refocus on your goals: re-read them and re-point the remaining work at them.",
        "3. Set a long /loop so you keep working after you answer.",
        "4. Reply with the groupem.roll_call_reply op (roll_call_id, session_key, goals, "
        "priority_goal_id, optional activity), one entry per goal with these fields, all required:",
        "   - goal_id, target, measured_now, gap",
        "   - next_lever and expected_gain (what you pull next, and what it buys)",
        "   - owner, and eta as an ISO date (YYYY-MM-DD)",
        "   - blocked_on {what, owner, date} when blocked: name who owns the blocker and an "
        "ISO date",
        "   - priority_goal_id: the one goal_id in your reply that is your priority item",
        "   A missing field, a TBD, or a missing ETA is refused and returned to you.",
        "   Put done/next narrative in activity; it goes to the log, not the brief.",
    ]
    if note:
        lines += ["", f"Note from the caller: {note}"]
    return "\n".join(lines)


def open_roll_call(
    roll_call_id: str,
    addressees: dict,
    text: str,
    deadline_minutes: Optional[float] = None,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> dict:
    """Create the record and return it; an id already on disk returns the existing record unchanged."""
    return open_roll_call_once(roll_call_id, addressees, text, deadline_minutes, now, home)[0]


def open_roll_call_once(
    roll_call_id: str,
    addressees: dict,
    text: str,
    deadline_minutes: Optional[float] = None,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> tuple[dict, bool]:
    """`open_roll_call` plus `created`: True only for the one caller that wrote the record."""
    when = time.time() if now is None else now
    minutes = DEFAULT_DEADLINE_MINUTES if deadline_minutes is None else deadline_minutes
    path = record_path(roll_call_id, home)
    with atomic_record.holder_lock(path):
        existing = atomic_record.read_json_tolerant(path)
        if existing is not None:
            return existing, False
        record = {
            "roll_call_id": roll_call_id,
            "text": text,
            "created": when,
            "deadline": when + minutes * 60,
            "addressees": {
                "local": list(addressees.get("local", [])),
                "far": list(addressees.get("far", [])),
                "excluded": list(addressees.get("excluded", [])),
            },
            "replies": {},
            "deliveries": {},
        }
        atomic_record.write_json_atomic(path, record)
        return record, True


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _field_problem(value: Any) -> Optional[str]:
    if _blank(value):
        return "missing"
    if isinstance(value, str) and _TBD_RE.search(value):
        return "tbd"
    return None


def _date_problem(value: Any) -> Optional[str]:
    problem = _field_problem(value)
    if problem:
        return problem
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value.strip()):
        return "not_iso_date"
    try:
        datetime.date.fromisoformat(value.strip())
    except ValueError:
        return "not_iso_date"
    return None


def validate_reply(goals: Any) -> list:
    """N1 check of a reply's goals: ``[{goal_id, field, reason}]``, empty when acceptable.

    Every GOAL_FIELDS entry is required and non-empty; ``eta`` is an ISO date (YYYY-MM-DD); a
    value reading ``TBD`` (any case) in any field is refused; ``blocked_on``, when given, needs
    ``what``, ``owner`` and an ISO ``date`` (reported as ``blocked_on.<part>``). Reasons:
    ``missing``, ``tbd``, ``not_iso_date``, ``not_an_object``, ``duplicate_goal_id``.
    """
    if not isinstance(goals, list) or not goals:
        return [{"goal_id": None, "field": "goals", "reason": "missing"}]
    out: list = []
    seen: set = set()
    for goal in goals:
        if not isinstance(goal, dict):
            out.append({"goal_id": None, "field": "goals", "reason": "not_an_object"})
            continue
        gid = goal.get("goal_id")
        label = gid.strip() if isinstance(gid, str) and gid.strip() else None
        for field in GOAL_FIELDS:
            problem = (
                _date_problem(goal.get(field)) if field == "eta" else _field_problem(goal.get(field))
            )
            if problem:
                out.append({"goal_id": label, "field": field, "reason": problem})
        if label is not None:
            if label in seen:
                out.append({"goal_id": label, "field": "goal_id", "reason": "duplicate_goal_id"})
            seen.add(label)
        blocked = goal.get("blocked_on")
        if blocked is None:
            continue
        if not isinstance(blocked, dict):
            out.append({"goal_id": label, "field": "blocked_on", "reason": "not_an_object"})
            continue
        for part in BLOCKED_FIELDS:
            problem = (
                _date_problem(blocked.get(part)) if part == "date" else _field_problem(blocked.get(part))
            )
            if problem:
                out.append({"goal_id": label, "field": f"blocked_on.{part}", "reason": problem})
    return out


def _priority_problems(goals: Any, priority_goal_id: Any) -> list:
    if not isinstance(priority_goal_id, str) or not priority_goal_id.strip():
        return [{"goal_id": None, "field": "priority_goal_id", "reason": "missing"}]
    ids = {
        g["goal_id"].strip()
        for g in (goals if isinstance(goals, list) else [])
        if isinstance(g, dict) and isinstance(g.get("goal_id"), str)
    }
    if priority_goal_id.strip() not in ids:
        return [{"goal_id": None, "field": "priority_goal_id", "reason": "not_a_reply_goal"}]
    return []


def _normalize_goals(goals: list) -> list:
    """Accepted goals reduced to the known fields, strings stripped (so a repeat compares equal)."""

    def clean(value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    out = []
    for goal in goals:
        row = {f: clean(goal[f]) for f in GOAL_FIELDS}
        if goal.get("blocked_on") is not None:
            row["blocked_on"] = {p: clean(goal["blocked_on"][p]) for p in BLOCKED_FIELDS}
        out.append(row)
    return out


def upsert_reply(
    roll_call_id: str,
    session_key: str,
    goals: Any,
    activity: Any = None,
    priority_goal_id: Any = None,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> dict:
    """Validate then store ``goals`` under ``session_key``; ``activity`` goes to the log only.

    ``priority_goal_id`` is required and must name one of the reply's goal_ids; otherwise the
    refusal row ``{goal_id: None, field: "priority_goal_id", reason}`` joins the ``validate_reply``
    rows. Raises ``RollCallReplyRefused`` (nothing stored, nothing logged) on any refusal.
    Otherwise returns ``{ok, result, late}`` with result ``stored`` (new or
    changed goals or priority) or ``unchanged`` (identical reply: a complete no-op, the first
    receipt time, late flag and log stand). A reply received after the record's ``deadline`` is
    stamped ``late: true``. An unknown roll call returns ``{ok: False, reason:
    "unknown_roll_call"}``.
    """
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("session_key must be a non-empty string")
    refusals = validate_reply(goals) + _priority_problems(goals, priority_goal_id)
    if refusals:
        raise RollCallReplyRefused(roll_call_id, session_key, refusals)
    when = time.time() if now is None else now
    accepted = _normalize_goals(goals)
    priority = priority_goal_id.strip()
    path = record_path(roll_call_id, home)
    with atomic_record.holder_lock(path):
        record = atomic_record.read_json_tolerant(path)
        if record is None:
            return {"ok": False, "reason": "unknown_roll_call"}
        replies = record.setdefault("replies", {})
        prior = replies.get(session_key)
        if (
            prior is not None
            and prior.get("goals") == accepted
            and prior.get("priority_goal_id") == priority
        ):
            return {"ok": True, "result": "unchanged", "late": bool(prior.get("late"))}
        late = when > record.get("deadline", float("inf"))
        replies[session_key] = {
            "goals": accepted, "priority_goal_id": priority, "received_at": when, "late": late,
        }
        atomic_record.write_json_atomic(path, record)
        if activity is not None:
            _append_log(path, {"session_key": session_key, "at": when, "late": late, "activity": activity})
        return {"ok": True, "result": "stored", "late": late}


def log_path(roll_call_id: str, home: Optional[Path] = None) -> Path:
    """``<gem-home>/roll-call/<roll_call_id>.log.jsonl``, the per-EM activity log."""
    return record_path(roll_call_id, home).with_suffix(".log.jsonl")


def _append_log(record_file: Path, entry: dict) -> None:
    with open(record_file.with_suffix(".log.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(entry) + "\n")


def record_deliveries(
    roll_call_id: str,
    outcomes: dict,
    now: Optional[float] = None,
    home: Optional[Path] = None,
) -> dict:
    """Merge ``{far key: {ok, reason}}`` delivery outcomes into the record, stamping ``at``."""
    when = time.time() if now is None else now
    path = record_path(roll_call_id, home)
    with atomic_record.holder_lock(path):
        record = atomic_record.read_json_tolerant(path)
        if record is None:
            return {"ok": False, "reason": "unknown_roll_call"}
        deliveries = record.setdefault("deliveries", {})
        for key, outcome in outcomes.items():
            deliveries[key] = {
                "ok": bool(outcome.get("ok")),
                "reason": outcome.get("reason"),
                "at": when,
            }
        atomic_record.write_json_atomic(path, record)
        return {"ok": True, "recorded": len(outcomes)}


def _addressee_ids(addressees: dict) -> list:
    """``[(key, {ids that count as a reply from it})]`` over local then far addressees."""
    out: list = []
    for row in addressees.get("local", []):
        sid = row.get("session_id")
        out.append((sid, {sid, row.get("name")} - {None, ""}))
    for row in addressees.get("far", []):
        key = row.get("key")
        out.append((key, {key, row.get("name")} - {None, ""}))
    return out


def status(roll_call_id: str, home: Optional[Path] = None) -> dict:
    """``{goals, replied, missing, deliveries}`` for one roll call; reads one file, never writes.

    ``goals`` is the progress-to-goals brief, one row per goal: ``{goal_id, owner, target,
    measured_now, gap, next_lever, expected_gain, eta, blocked_on, session_key, late, priority}``
    (priority: the goal the replier named as its priority item). Activity
    is never in it. ``replied`` lists the reply keys; ``missing`` lists ``{key, name}`` for each
    addressee no reply is keyed by (by key, session id or name). An unknown roll call returns
    ``{ok: False, reason: "unknown_roll_call"}``.
    """
    record = atomic_record.read_json_tolerant(record_path(roll_call_id, home))
    if record is None:
        return {"ok": False, "reason": "unknown_roll_call"}
    replies = record.get("replies", {})
    addressees = record.get("addressees", {})
    names = {}
    for row in addressees.get("local", []):
        names[row.get("session_id")] = row.get("name")
    for row in addressees.get("far", []):
        names[row.get("key")] = row.get("name")
    missing = [
        {"key": key, "name": names.get(key)}
        for key, ids in _addressee_ids(addressees)
        if not (ids & set(replies))
    ]
    brief = [
        {
            "goal_id": g["goal_id"],
            "owner": g["owner"],
            "target": g["target"],
            "measured_now": g["measured_now"],
            "gap": g["gap"],
            "next_lever": g["next_lever"],
            "expected_gain": g["expected_gain"],
            "eta": g["eta"],
            "blocked_on": g.get("blocked_on"),
            "session_key": key,
            "late": bool(rep.get("late")),
            "priority": g["goal_id"] == rep.get("priority_goal_id"),
        }
        for key, rep in replies.items()
        for g in rep.get("goals", [])
    ]
    return {
        "ok": True,
        "roll_call_id": roll_call_id,
        "deadline": record.get("deadline"),
        "goals": brief,
        "replied": list(replies),
        "missing": missing,
        "deliveries": record.get("deliveries", {}),
    }


def load_record(roll_call_id: str, home: Optional[Path] = None) -> Optional[dict]:
    """The raw record, or None when absent or unreadable."""
    return atomic_record.read_json_tolerant(record_path(roll_call_id, home))

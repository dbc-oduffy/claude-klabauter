"""Opt-in chatty workflow composition: roster, nonces, overseer, agent briefs.

Purpose: when a workflow spec sets ``chatty: true``, ``emit.compose_script``
asks this module for the run's roster (conforming to the vendored
``chatty-roster.schema.json``), each member's brief addendum, and the
overseer's prompt. Pure functions, no I/O beyond the one schema read.

Sends are advisory: the roster's ``caps`` and ``counters`` are neither
emitted nor read; validation drops them from the vendored schema's
``required``. The emitter writes no file: the roster template travels in the
overseer's brief and the overseer writes ``roster.json``.
"""

from __future__ import annotations

import functools
import json
import secrets
from pathlib import Path
from typing import Sequence

OVERSEER_ROLE = "overseer"
ROSTER_SCHEMA_VERSION = "1.0.0"

_ADVISORY_FIELDS = ("caps", "counters")

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "chatty-roster.schema.json"
)


@functools.cache
def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def build_roster(run_id: str, worker_roles: Sequence[str]) -> dict:
    """The roster template: the overseer plus one member per worker role,
    each with a fresh nonce and ``agent_id`` null until it registers."""
    members = [
        {"role": role, "nonce": secrets.token_hex(8), "agent_id": None, "state": "registering"}
        for role in (OVERSEER_ROLE, *worker_roles)
    ]
    roster = {
        "schema_version": ROSTER_SCHEMA_VERSION,
        "run_id": run_id,
        "overseer_role": OVERSEER_ROLE,
        "members": members,
    }
    errors = validate_roster(roster)
    if errors:
        raise ValueError("chatty roster violates its schema: " + "; ".join(errors))
    return roster


def validate_roster(roster: dict) -> list[str]:
    """Schema errors plus the read-side rules JSON Schema cannot express
    (unique nonces; ``overseer_role`` names a member). Empty when valid.
    A roster carrying ``caps``/``counters`` is still schema-checked for them."""
    import jsonschema

    schema = dict(_schema())
    schema["required"] = [k for k in schema["required"] if k not in _ADVISORY_FIELDS]
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(roster)]
    members = roster.get("members") or []
    nonces = [m.get("nonce") for m in members if isinstance(m, dict)]
    if len(nonces) != len(set(nonces)):
        errors.append("duplicate member nonce")
    if roster.get("overseer_role") not in [m.get("role") for m in members if isinstance(m, dict)]:
        errors.append("overseer_role names no member")
    return errors


def member_nonce(roster: dict, role: str) -> str:
    return next(m["nonce"] for m in roster["members"] if m["role"] == role)


_MAIL_LINE = '{"from":"<role>","text":"<summary>"}'


def member_brief(role: str, nonce: str) -> str:
    """Agent-facing addendum appended to a worker's brief."""
    return (
        "\n\nCHATTY RUN. "
        f"Role: {role}. Nonce: {nonce}.\n"
        "On start, self-ID: grep -rl for your nonce under your parent transcript's "
        "subagents/workflows/ tree; the matching agent-<agentId>.jsonl names your "
        "agentId and its directory is the run directory (never pick the newest "
        "file). Wait for roster.json there, then set your member's agent_id and "
        "state=running.\n"
        "Mail: <run-dir>/mail/<role>.jsonl is that role's mailbox. Send by "
        f"appending one JSON line {_MAIL_LINE} to the recipient's mailbox; read "
        f"yours at <run-dir>/mail/{role}.jsonl, then append {{\"read\":true}} to it. "
        "Never SendMessage a peer. Report findings to the "
        f"{OVERSEER_ROLE} by mailbox, as a summary, never a relay. Never message the EM.\n"
        "Before finishing, set your roster state=returned. If woken, read your "
        "mailbox, act, then set state=returned again."
    )


def overseer_prompt(roster: dict) -> str:
    """The overseer's provisioning brief: write the roster, register, finish."""
    nonce = member_nonce(roster, OVERSEER_ROLE)
    return (
        "You are the overseer of a chatty run. "
        f"Nonce: {nonce}.\n"
        "1. Self-ID: grep -rl for your nonce under your parent transcript's "
        "subagents/workflows/ tree; the matching agent-<agentId>.jsonl names your "
        "agentId and its directory "
        "(<parent transcript without .jsonl>/subagents/workflows/wf_<runId>/) is "
        "the run directory (never pick the newest file).\n"
        "2. First act: write the roster below to <run directory>/roster.json "
        "with your own agent_id set and state=running; create <run directory>/mail/. "
        "Workers wait for it.\n"
        "3. Then finish; a later stage reads the mail and wakes members.\n"
        "ROSTER:\n" + json.dumps(roster, indent=2)
    )


def overseer_wake_prompt(roster: dict) -> str:
    """The post-return stage brief: wake `returned` members holding unread mail,
    then report to the EM."""
    nonce = member_nonce(roster, OVERSEER_ROLE)
    return (
        "You are the overseer of a chatty run, after every row returned. "
        f"Nonce: {nonce}.\n"
        "1. Self-ID: grep -rl for your nonce under your parent transcript's "
        "subagents/workflows/ tree; the matching directory is the run directory.\n"
        "2. Read <run-dir>/roster.json and every <run-dir>/mail/<role>.jsonl. "
        "A mailbox is unread when it has a message line after its last "
        '{"read":true} line.\n'
        "3. For each member whose roster state is `returned` and whose mailbox "
        "is unread: set state=woken, then SendMessage its roster agent_id: "
        '"Read your mailbox." Wake nobody in any other state.\n'
        f"4. Read your own mailbox ({OVERSEER_ROLE}.jsonl). Report to the EM once, "
        "as a summary, never a relay."
    )

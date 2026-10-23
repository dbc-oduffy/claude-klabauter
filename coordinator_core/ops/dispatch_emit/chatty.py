"""Opt-in chatty workflow composition: roster, nonces, overseer, agent briefs.

Purpose: when a workflow spec sets ``chatty: true``, ``emit.compose_script``
asks this module for the run's roster (conforming to the vendored
``chatty-roster.schema.json``), each member's brief addendum, and the
overseer's prompt. Pure functions, no I/O beyond the one schema read.

The emitter writes no file: the roster template travels in the
overseer's brief and the overseer writes ``roster.json``.
"""

from __future__ import annotations

import functools
import json
import secrets
from pathlib import Path
from typing import Sequence

OVERSEER_ROLE = "overseer"
ROSTER_SCHEMA_VERSION = "2.1.0"

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
    (unique nonces; ``overseer_role`` names a member). Empty when valid."""
    import jsonschema

    errors = [e.message for e in jsonschema.Draft202012Validator(_schema()).iter_errors(roster)]
    members = roster.get("members") or []
    nonces = [m.get("nonce") for m in members if isinstance(m, dict)]
    if len(nonces) != len(set(nonces)):
        errors.append("duplicate member nonce")
    if roster.get("overseer_role") not in [m.get("role") for m in members if isinstance(m, dict)]:
        errors.append("overseer_role names no member")
    return errors


def member_nonce(roster: dict, role: str) -> str:
    return next(m["nonce"] for m in roster["members"] if m["role"] == role)


def _self_id() -> str:
    return (
        "Match the first user record only. Candidates: "
        "<session>/subagents/workflows/*/agent-*.jsonl under your parent transcript. "
        "With N = your MY-NONCE value and the glob expanded, run: python3 -c 'import json,sys;"
        "[print(f) for f in sys.argv[1:] if (lambda r: r and \"MY-NONCE=N\" in "
        "json.dumps(r))(next((json.loads(l) for l in open(f) if "
        "json.loads(l).get(\"type\")==\"user\"),None))]' <glob>. "
        "Exactly one file printed: that is you (agentId from the filename; run "
        "directory = its parent). Zero or more than one: stop and report, do not guess."
    )


_MAIL_LINE = '{"from":"<role>","text":"<summary>"}'

#: The run's one upward channel: the EM tails this mailbox (`em_mailbox` on the emit reply).
EM_MAILBOX = "_em.jsonl"
_ESCALATE = (
    "Never message the EM directly. A break-class finding the EM must act on before the run ends: "
    f'append {{"from":"<role>","escalate":true,"text":"<finding, file:line>"}} to <run-dir>/mail/{EM_MAILBOX}, '
    'adding "target_run" and "target_row" when the finding is for a row of another run; '
    "everything else goes by mailbox."
)


def member_brief(role: str, nonce: str) -> str:
    """Agent-facing addendum appended to a worker's brief."""
    return (
        "\n\nCHATTY RUN. "
        f"Role: {role}. MY-NONCE={nonce}\n"
        f"On start, self-ID: {_self_id()} Wait for roster.json there, then set your member's agent_id and "
        "state=running.\n"
        "Mail: <run-dir>/mail/<role>.jsonl is that role's mailbox. Send by "
        f"appending one JSON line {_MAIL_LINE} to the recipient's mailbox; read "
        f"yours at <run-dir>/mail/{role}.jsonl, then append {{\"read\":true}} to it. "
        f"Report findings to the {OVERSEER_ROLE} by mailbox, as a summary, never a relay. "
        f"{_ESCALATE}\n"
        "Before finishing, set your roster state=returned."
    )


NONCE_SLOT = "{NONCE}"


def continuation_brief(role: str) -> str:
    """Addendum for a fresh agent continuing a `returned` member of `role`.
    ``NONCE_SLOT`` is filled by the emitted script with a new nonce per dispatch."""
    return (
        "\n\nCHATTY CONTINUATION. "
        f"Role: {role}. MY-NONCE={NONCE_SLOT}\n"
        f"1. Self-ID: {_self_id()}\n"
        f"2. In <run-dir>/roster.json, your predecessor is the `{role}` member in state "
        "`returned` with no `continued_by`. Read its transcript: "
        "<run-dir>/agent-<its agent_id>.jsonl. Set its `continued_by` to your agentId. "
        f"Add your own member (role {role}, nonce above, your agent_id, state=running).\n"
        f"3. Read <run-dir>/mail/{role}.jsonl, then append {{\"read\":true}} to it. "
        "Mail: append one JSON line "
        f"{_MAIL_LINE} to the recipient's mailbox.\n"
        "4. Continue the work from the transcript and the mail, "
        f"then set your state=returned. {_ESCALATE}"
    )


def overseer_prompt(roster: dict) -> str:
    """The overseer's provisioning brief: write the roster, register, finish."""
    nonce = member_nonce(roster, OVERSEER_ROLE)
    return (
        "You are the overseer of a chatty run. "
        f"MY-NONCE={nonce}\n"
        f"1. Self-ID: {_self_id()}\n"
        "2. First act: write the roster below to <run directory>/roster.json "
        "with your own agent_id set and state=running; create <run directory>/mail/. "
        "Workers wait for it.\n"
        "3. Then finish; a later stage lists members needing continuation.\n"
        "ROSTER:\n" + json.dumps(roster, indent=2)
    )


def new_nonce() -> str:
    return secrets.token_hex(8)


def survey_prompt(nonce: str) -> str:
    """Post-return stage: report which `returned` members hold unread mail.
    Returns data only; the script dispatches the continuations."""
    return (
        "You are the overseer of a chatty run, after every row returned. "
        f"MY-NONCE={nonce}\n"
        f"1. Self-ID: {_self_id()}\n"
        "2. Read <run-dir>/roster.json and every <run-dir>/mail/<role>.jsonl. "
        "A mailbox is unread when it has a message line after its last "
        '{"read":true} line.\n'
        "3. Return `continuations`: one {role, agent_id} per member whose roster "
        "state is `returned`, has no `continued_by`, and whose mailbox is unread. "
        "Change nothing."
    )


def summary_prompt(nonce: str) -> str:
    """Final stage: the overseer's one report to the EM."""
    return (
        "You are the overseer of a chatty run, after all continuations. "
        f"MY-NONCE={nonce}\n"
        f"1. Self-ID: {_self_id()}\n"
        f"2. Read your mailbox ({OVERSEER_ROLE}.jsonl) and the roster. Report to the EM "
        "once, as a summary, never a relay."
    )


#: Longest plan-review ladder row (XL/XXL); the slots one baton's review wave mints.
REVIEW_WAVE_SLOTS = 3
#: planBlitz/roadmapBlitz launch-arg key carrying the review-wave template.
REVIEW_WAVE_ARG = "chattyReview"
#: Filled by the consumer with the baton id; scopes roster, mail and nonces per baton.
BATON_SLOT = "{BATON}"
PEERS_SLOT = "{PEERS}"

_WAVE_DIR_SENTENCE = (
    f"Your wave directory is <run directory>/{BATON_SLOT}/; every <run-dir> and every roster.json "
    "in this brief means that directory, except the transcript path <run-dir>/agent-<agent_id>.jsonl "
    f"and the EM mailbox <run-dir>/mail/{EM_MAILBOX}, which stay in <run directory>."
)

_REVIEW_WAVE_CLAUSE = (
    "\nREVIEW WAVE. Peers:\n" + PEERS_SLOT + "\n"
    "Apply your own findings by exact-text Edit. A failed Edit means a peer moved the text: "
    "re-read the plan and your mailbox, re-anchor or drop. A ledger row whose `after` text a peer "
    "later edited over is re-anchored to the text now present and the peer mailed; never "
    "hand-author around it. Mail the affected slot BEFORE applying a finding that shifts its ground "
    "(scope, row, AC, exit criterion). On a PIVOT or REJECTED verdict, mail every slot to stop "
    "applying. Re-read your mailbox after each section you review and before you return.\n"
    "Region claims. A region is one `## ` section of the plan body, one spine row by `id`, or "
    "`frontmatter`. CLAIM <region>: before your first Edit in it, append "
    '{"from":"<slot>","text":"CLAIM <region>"} to every peer\'s mailbox, then re-read yours. '
    "A region is held by the earliest unreleased CLAIM among those you received and those you sent. "
    "Crossed claims: the lower review-N holds, the higher yields. Edit no region a peer holds; "
    "mail the holder the finding. RELEASE <region>: once your edits there verify, append "
    '{"from":"<slot>","text":"RELEASE <region>"} to every peer\'s mailbox. '
    "A returned slot holds nothing."
)

_EM_KEEP = "\x00EM\x00"


def review_slot(index: int) -> str:
    """Slot name of review wave member `index` (0 -> "review-1"); refuses an index past the wave."""
    if not 0 <= index < REVIEW_WAVE_SLOTS:
        raise ValueError(f"review slot index {index} outside 0..{REVIEW_WAVE_SLOTS - 1}")
    return f"review-{index + 1}"


def _baton_scope(text: str) -> str:
    """Roster and mail paths into the per-baton wave directory; the EM mailbox and
    the transcript path stay run-level."""
    text = text.replace(f"<run-dir>/mail/{EM_MAILBOX}", _EM_KEEP)
    for base in ("<run-dir>", "<run directory>"):
        for leaf in ("/roster.json", "/mail/"):
            text = text.replace(base + leaf, f"{base}/{BATON_SLOT}{leaf}")
    return text.replace(_EM_KEEP, f"<run-dir>/mail/{EM_MAILBOX}")


def _scoped(text: str) -> str:
    return _baton_scope(text) + "\n" + _WAVE_DIR_SENTENCE


def review_member_brief(role: str, nonce: str) -> str:
    """`member_brief` scoped to the baton wave directory, plus the review-wave clause
    (exact-text Edit, ground-shift mail, region claims). `{PEERS}` is left for the
    consumer to fill with `review-N = <agentType>` lines."""
    base = _baton_scope(member_brief(role, nonce))
    marker = " Wait for roster.json there"
    base = base.replace(marker, " " + _WAVE_DIR_SENTENCE + marker, 1)
    return base + _REVIEW_WAVE_CLAUSE


def review_wave_args(run_id: str) -> dict:
    """Review-wave template for one baton: roster, overseer, member/continuation briefs,
    survey and summary prompts, every nonce and wave path carrying `{BATON}`.

    Unused slots stay `registering`; the survey reads only `returned` members."""
    slots = [review_slot(i) for i in range(REVIEW_WAVE_SLOTS)]
    roster = build_roster(run_id, slots)
    for member in roster["members"]:
        member["nonce"] = f"{BATON_SLOT}-{member['nonce']}"
    return {
        "roster": roster,
        "overseerPrompt": _scoped(overseer_prompt(roster)),
        "memberBriefs": {s: review_member_brief(s, member_nonce(roster, s)) for s in slots},
        "continuationBriefs": {s: _scoped(continuation_brief(s)) for s in slots},
        "surveyPrompt": _scoped(survey_prompt(f"{BATON_SLOT}-{new_nonce()}")),
        "summaryPrompt": _scoped(summary_prompt(f"{BATON_SLOT}-{new_nonce()}")),
        "nonceSlot": NONCE_SLOT,
        "peersSlot": PEERS_SLOT,
        "batonSlot": BATON_SLOT,
        "emMailbox": EM_MAILBOX,
    }

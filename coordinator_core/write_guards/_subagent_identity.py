"""coordinator_core.write_guards._subagent_identity — shared subagent
identity resolver, factored out of ``block_subagent_plan_body_write.py``
(2026-08-03) so ``block_subagent_archive_write.py`` can recognise the same
named-teammate ``agent_id`` shape rather than carrying its own,
narrower-fidelity bare-hex-only copy. See
``cross-repo/inbox/2026-08-03-example-doctrine-repo-em-archive-write-guard-pincer.md``
for the composed-defect writeup that prompted this extraction.

Port of example-doctrine-repo coordinator/lib/coordinator-session.sh (example-doctrine-repo ``e34f2484``,
2026-07-22) ``resolve_subagent_identity`` / ``cs_build_canonical_agent_id``.

Also carries ``_read_backpointer_subagent_type`` — the
``agent_id -> em_session_id -> dispatched-agents.txt`` row lookup both
guards need to resolve a canonical agent id to its dispatched
``subagent_type`` — so there is exactly one implementation of the
back-pointer chain, not two.

This module has NO ``check()`` entry point and is not itself a write guard;
it is imported by ``block_subagent_plan_body_write`` and
``block_subagent_archive_write`` only.
"""

from __future__ import annotations

import re
from pathlib import Path

#: coordinator-session.sh resolve_subagent_identity path (a): bare hex,
#: unnamed-agent fast path, session_id ignored.
_BARE_HEX_RE = re.compile(r"^[a-f0-9]{12,}$")

#: coordinator-session.sh resolve_subagent_identity path (b): named teammate
#: a<name>-<16hex> — capture group extracts <name> (dash-tolerant, e.g.
#: aprobe2-teammate-64cd7f42c270a899 -> probe2-teammate).
_NAMED_TEAMMATE_RE = re.compile(r"^a(.+)-[a-f0-9]{16}$")

#: em-session-id.txt back-pointer content format guard.
_SESSION_ID_FORMAT_RE = re.compile(r"^[a-zA-Z0-9_-]{3,}$")

#: Already-canonical teammate shape as the HARNESS hands it back verbatim on
#: dispatch (``tool_response.agentId`` / ``.agent_id``): ``<name>@session-<short>``.
#: Distinct from ``_NAMED_TEAMMATE_RE`` above, which matches the SUBAGENT-side
#: raw id (``a<name>-<16hex>``) seen from inside a dispatched teammate's own
#: tool calls — the two shapes are NOT the same string for the same teammate.
_TEAMMATE_CANONICAL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)@session-(?P<short>[a-z0-9-]+)$")


def normalize_teammate_agent_id(agent_id: str, live_session_id: str) -> str:
    """Rebuild a harness-supplied teammate canonical id against the LIVE session.

    Root cause (docs/research/spike-verdicts/2026-08-10-session-scoped-hooks-
    inside-a-teammate-session.md): the harness stamps a named teammate's
    ``teamName`` — and therefore the ``<short>`` embedded in its
    ``<name>@session-<short>`` canonical id — once, at team creation. A
    ``/clear`` (and plausibly ``resume``/``compact``/``fork``) mints a new LIVE
    session id without refreshing that stamp, so the harness keeps handing back
    an id whose embedded ``<short>`` names a session that may since have ended
    and been archived. Every OTHER bookkeeping writer in this codebase derives
    its own teammate canonical id fresh, from the raw subagent-side id plus the
    CURRENT firing ``session_id`` (see ``_resolve_subagent_identity`` above,
    ``coordinator_core.session.identity.resolve_subagent_identity``, and
    ``coordinator_core.hooks.track_touched_files._resolve_subagent_identity``)
    — so a harness-verbatim id with a stale embedded short silently keys a
    DIFFERENT ``.agents/<id>/`` directory than the one those writers use for
    the exact same teammate, and a join against one never finds the other.

    This function is the single place that re-derives the embedded short
    against ``live_session_id`` (``CLAUDE_CODE_SESSION_ID`` — authoritative for
    "which session am I actually in", per the same spike) so a harness-supplied
    canonical id and a freshly-built one land on the SAME directory.

    Returns ``agent_id`` unchanged (never rewrites) when:
      - it is not ``<name>@session-<short>`` shaped at all (bare hex, or any
        other unrecognised string — not this function's concern);
      - ``live_session_id`` is absent or shorter than 8 chars — fail-closed,
        never guess a short from an id too short to slice reliably;
      - the embedded short already equals ``live_session_id[:8]`` — a no-op
        rewrite would only churn callers for no behavior change.

    Pure function — no filesystem I/O, no side effects.
    """
    match = _TEAMMATE_CANONICAL_RE.fullmatch(agent_id or "")
    if not match:
        return agent_id
    live = live_session_id or ""
    if len(live) < 8:
        return agent_id
    live_short = live[:8]
    if match.group("short") == live_short:
        return agent_id
    return f"{match.group('name')}@session-{live_short}"


def _cs_build_canonical_agent_id(name: str, short_session: str) -> str:
    """Port of ``cs_build_canonical_agent_id``, example-doctrine-repo coordinator-session.sh
    (example-doctrine-repo ``e34f2484``, 2026-07-22).
    """
    return f"{name}@session-{short_session}"


def _resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    """Port of ``resolve_subagent_identity``, example-doctrine-repo coordinator-session.sh
    (example-doctrine-repo ``e34f2484``, 2026-07-22).

    Three paths, in order:
      (a) Bare hex ``^[a-f0-9]{12,}$`` -> unnamed agent fast path, returns
          ``agent_id`` unchanged; ``session_id`` is ignored.
      (b) Named teammate ``^a(.+)-[a-f0-9]{16}$`` -> requires a non-empty
          extracted ``<name>`` (guaranteed by the ``(.+)`` capture) AND
          ``session_id`` with >= 8 chars; on success returns
          ``cs_build_canonical_agent_id(name, session_id[:8])``. On a
          too-short/absent ``session_id``, returns ``""`` (fail-closed).
      (c) Anything else -> ``""`` (fail-closed — unknown agent shape).
    """
    if _BARE_HEX_RE.match(agent_id):
        return agent_id

    named = _NAMED_TEAMMATE_RE.match(agent_id)
    if named:
        name = named.group(1)
        if len(session_id) < 8:
            return ""
        short = session_id[:8]
        return _cs_build_canonical_agent_id(name, short)

    return ""


def _read_backpointer_subagent_type(git_root: str, agent_id: str) -> str:
    """Back-pointer chain: agent_id -> em_session_id -> dispatched-agents.txt row
    column 3. Any missing/unreadable/malformed link
    in the chain returns ``""`` (lookup-fail -> caller treats per its own
    fail-open/fail-closed policy — see each guard's own docstring; this
    shared helper does not decide that).
    """
    backptr = (
        Path(git_root)
        / ".git"
        / "coordinator-sessions"
        / ".agents"
        / agent_id
        / "em-session-id.txt"
    )
    try:
        content = backptr.read_text(encoding="utf-8")
    except OSError:
        return ""
    em_sid = content.splitlines()[0].strip() if content else ""
    if not _SESSION_ID_FORMAT_RE.match(em_sid):
        return ""

    dispatch_file = (
        Path(git_root) / ".git" / "coordinator-sessions" / em_sid / "dispatched-agents.txt"
    )
    try:
        rows = dispatch_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for row in rows:
        fields = row.split("\t")
        if len(fields) >= 3 and fields[0] == agent_id:
            return fields[2]
    return ""

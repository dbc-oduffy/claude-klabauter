"""
coordinator_core/workstream_complete/session_identity.py

Pure leaf: session id -> the set of `Deliverable-Id` git-trailer values
carried by that session's own commits, plus per-commit provenance for why
each one did or did not resolve.

Extraction, not a new mechanism (docs/plans/2026-08-20-wsc-identity-gates-
key-on-the-deliverable.md, C1). The git invocation shape is modelled on
`coordinator_core.coverage._commit_deliverable_id_trailers` (the `{sha:
Deliverable-Id trailer}` reader using
`%(trailers:key=Deliverable-Id,valueonly)`) — reused rather than reinvented.

The last-paragraph trailer trap (mirrored from
`coordinator_core.execute_plan_assemble.close_out_and_stamp
._resolve_deliverable_id`'s own docstring, which documents it in full): git
recognises ONLY a commit message's LAST paragraph as trailers. A commit
whose caller-supplied `Deliverable-Id:` line sits ahead of a trailing
paragraph the pipeline appends of its own (e.g. a `Commit-Token:`/
`Session-Id:` block separated by a blank line) has that line DEMOTED to
prose — `%(trailers:key=Deliverable-Id,valueonly)` returns empty for a
commit that visibly carries it. `close_out_and_stamp.py` carries a
line-anchored regex fallback over the commit body for exactly this trap;
this module mirrors that fallback (`_DELIVERABLE_ID_BODY_LINE_RE`, same
pattern) rather than trusting the trailer atom alone.

Purity contract (this plan's own C1 body): stdlib only, plus
`coordinator_core.win_portability.no_console_creationflags` (the one
bootstrap-safe, zero-subprocess, stdlib-only coordinator_core helper the git
spawn itself needs to stay console-popup-free on Windows) -- no
`resolve_operator_config`, no operator/config side effects, no other
coordinator_core import. Meant to be imported PLAIN by
`coordinator/bin/wsc-session-disposition.py` -- a bin script importing a
stdlib-only engine leaf module has no side effect and is a different
mechanism from the reverted `_load_session_disposition_module()`
delegation (that direction was bin `_resolve_claude_klabauter_bin()` ->
`resolve_operator_config`; this is the opposite direction and carries none
of that side effect). See this module's own callers for the "fourth
sanctioned coordinator_core import" convention this extends.

Negative-spec:
    - Does NOT filter/scope by path -- every commit reachable from `HEAD`
      whose own `Session-Id` trailer equals the queried session id is in
      scope, matching `session_attribution.bulk_trailer_session_map`'s own
      "a commit with no Session-Id trailer is simply absent" posture.
    - Does NOT silently first-wins on conflicting `Deliverable-Id` values
      across a session's commits -- `deliverable_ids` is a set, and
      `commits` carries the full per-commit record, so a caller can see
      every distinct value and which commit carried it.
    - Does NOT write any coordinator substrate -- read-only.
    - Does NOT call `resolve_operator_config` or any other engine
      config/operator seam -- see purity contract above.

Spec backlink: docs/plans/2026-08-20-wsc-identity-gates-key-on-the-deliverable.md
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple, Union

from coordinator_core.win_portability import no_console_creationflags

#: Record/field separators for the batched `git log` call below -- ASCII
#: unit/record-separator bytes, matching `coverage.py`'s own
#: `_COMMIT_HEADER_SENTINEL` idiom (a byte that cannot occur in a commit sha,
#: trailer value, or ordinary prose, so a record boundary is never confused
#: with content).
_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"

#: Body-line fallback for a `Deliverable-Id:` value git's own trailer parser
#: demoted to prose (see module docstring). Line-anchored, mirrored verbatim
#: (same pattern) from `coordinator_core.execute_plan_assemble
#: .close_out_and_stamp._DELIVERABLE_ID_BODY_LINE_RE`.
_DELIVERABLE_ID_BODY_LINE_RE = re.compile(
    r"^Deliverable-Id:[ \t]*(\S[^\r\n]*?)[ \t]*$", re.MULTILINE
)

StrPath = Union[str, "Path"]


@dataclass(frozen=True)
class CommitDeliverableIdentity:
    """One of the queried session's own commits, and how (if at all) its
    `Deliverable-Id` was resolved.

    `source` is one of:
      "trailer"       -- git's own `%(trailers:key=Deliverable-Id,...)`
                          parsed a non-empty value.
      "body-fallback" -- the trailer atom was empty, but the last-paragraph
                          regex fallback found a `Deliverable-Id:` line in
                          the commit body (see module docstring).
      "absent"        -- neither leg produced a value; `deliverable_id` is
                          `""`.
    """

    sha: str
    deliverable_id: str
    source: str


@dataclass(frozen=True)
class SessionDeliverableIdentity:
    """`session_deliverable_ids`'s return: the resolved id set, plus enough
    provenance for a caller to say WHY it resolved or did not.

    `ok` is `False` only when the underlying `git log` call itself failed
    (git not on PATH, non-zero exit, not a git repo) -- never for a
    genuinely empty result, which is `ok=True` with an empty `commits`/
    `deliverable_ids` and a `reason` naming the empty case. `reason` is a
    short, human-readable string for either case; it is diagnostic text,
    not a machine-parsed enum.
    """

    session_id: str
    ok: bool
    reason: str
    commits: Tuple[CommitDeliverableIdentity, ...]
    deliverable_ids: FrozenSet[str]


def _resolve_deliverable_id(trailer_value: str, body: str) -> Tuple[str, str]:
    """Returns `(deliverable_id, source)`. Trailer-first, never the reverse
    (mirrors `close_out_and_stamp._resolve_deliverable_id`'s own precedence
    contract): a non-empty parsed trailer value is authoritative and the
    body is never consulted when it is present. Only the empty-trailer case
    -- which previously meant "no evidence at all" for this reader's
    close_out_and_stamp counterpart -- consults the body, taking the LAST
    matching line (mirrors git's own preference for the last trailer block
    when a message carries several)."""
    value = trailer_value.strip()
    if value:
        return value, "trailer"
    matches = _DELIVERABLE_ID_BODY_LINE_RE.findall(body)
    if matches:
        return matches[-1].strip(), "body-fallback"
    return "", "absent"


def session_deliverable_ids(repo_root: StrPath, session_id: str) -> SessionDeliverableIdentity:
    """Return the set of `Deliverable-Id` trailer values carried by
    `session_id`'s own commits (its own `Session-Id` trailer, exact match)
    reachable from `HEAD` in the repo at `repo_root`, plus a per-commit
    provenance record.

    ONE batched `git log --no-merges` walk (sha, `Session-Id` trailer,
    `Deliverable-Id` trailer, and the raw body for the fallback leg), same
    single-spawn shape `coverage.py`'s own trailer readers use rather than
    one subprocess per commit.

    `session_id` is compared for exact equality against the parsed
    `Session-Id` trailer value -- no regex, no interpolation into the git
    invocation itself, so an oddly-shaped `session_id` cannot widen the
    match the way an unvalidated `git log --grep` interpolation could (see
    `coverage.py`'s own Session-Id UUID-shape guard for the sibling class of
    defect this sidesteps by construction, not by validating the input
    shape here too -- there is no interpolation site to protect).

    Returns `ok=False` (empty `commits`/`deliverable_ids`) only when the
    `git log` call itself fails; returns `ok=True` with an empty result and
    an explanatory `reason` when the session simply has no commits (a
    session that has not started, or a `session_id` that never committed) --
    the two cases are deliberately distinguishable so a caller can tell
    "could not check" from "checked, found nothing"."""
    if not session_id:
        return SessionDeliverableIdentity(
            session_id=session_id,
            ok=True,
            reason="empty session_id supplied -- no commits can match",
            commits=(),
            deliverable_ids=frozenset(),
        )

    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "--no-merges",
                "--format="
                + _RECORD_SEP
                + "%H"
                + _FIELD_SEP
                + "%(trailers:key=Session-Id,valueonly)"
                + _FIELD_SEP
                + "%(trailers:key=Deliverable-Id,valueonly)"
                + _FIELD_SEP
                + "%B",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SessionDeliverableIdentity(
            session_id=session_id,
            ok=False,
            reason=f"git log failed to spawn: {exc}",
            commits=(),
            deliverable_ids=frozenset(),
        )

    if proc.returncode != 0:
        return SessionDeliverableIdentity(
            session_id=session_id,
            ok=False,
            reason=f"git log exited {proc.returncode}: {proc.stderr.strip()}",
            commits=(),
            deliverable_ids=frozenset(),
        )

    commits: list = []
    for raw_record in (proc.stdout or "").split(_RECORD_SEP):
        if not raw_record.strip():
            continue
        fields = raw_record.split(_FIELD_SEP, 3)
        if len(fields) < 4:
            continue
        sha = fields[0].strip()
        if not sha:
            continue
        commit_session_id = fields[1].strip()
        if commit_session_id != session_id:
            continue
        trailer_value = fields[2]
        body = fields[3]
        deliverable_id, source = _resolve_deliverable_id(trailer_value, body)
        commits.append(CommitDeliverableIdentity(sha=sha, deliverable_id=deliverable_id, source=source))

    deliverable_ids = frozenset(c.deliverable_id for c in commits if c.deliverable_id)

    if not commits:
        reason = f"no commits reachable from HEAD carry a Session-Id trailer equal to {session_id!r}"
    elif not deliverable_ids:
        reason = f"{len(commits)} commit(s) matched session_id, but none carry a resolvable Deliverable-Id"
    elif len(deliverable_ids) == 1:
        reason = f"{len(commits)} commit(s) matched session_id, resolved 1 Deliverable-Id"
    else:
        reason = (
            f"{len(commits)} commit(s) matched session_id, resolved "
            f"{len(deliverable_ids)} CONFLICTING Deliverable-Id values"
        )

    return SessionDeliverableIdentity(
        session_id=session_id,
        ok=True,
        reason=reason,
        commits=tuple(commits),
        deliverable_ids=deliverable_ids,
    )

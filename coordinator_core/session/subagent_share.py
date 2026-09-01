"""The subagent-share directory layout: one owner for paths three modules
were each spelling out for themselves.

WHAT THIS IS. `<repo_root>/state/subagent-share/<session_id>/` and the files
under it -- the next-move ledger, the obligations intake, the Group EM send
log. Nothing here reads, writes, or interprets any of them; this module owns
only WHERE they are and WHO may name a directory.

WHY IT EXISTS. `group_em.send_pass`, `group_em.obligations` and
`hooks.watchdog_undischarged_next_move` each carried their own
`_session_share_dir` and their own `_LEDGER_FILENAME = "next-move-ledger.jsonl"`
-- the same directory join and the same string, retyped three times, with
`obligations` additionally reaching into `send_pass`'s underscore-private
namespace to borrow one of them. Two failure modes, neither of which any test
covered: a typo corrected in one copy and not the others silently splits the
producers and the readers onto different files, and a private symbol used
across a module boundary can be changed by an edit to its own module that has
no reason to look for foreign callers.

WHY `session/` AND NOT `group_em/`. The dependency arrow. `group_em` and
`hooks` both import `session`; `session` imports neither. Homing this in
`group_em` would have made a hook depend on the Group EM package for a path
join, which is the coupling this consolidation exists to remove rather than
relocate.

Negative-spec:
    - Owns paths and the id predicate. NOT the record shapes, NOT the intake
      op vocabulary, NOT the schema version -- those are contracts with a
      sibling repo's producers and each consumer states its own, deliberately
      (see `group_em.obligations`'s own note on why it does not import DoE's).
    - Stdlib only, and no import-time work: a Stop-family hook is on the
      per-turn path for every session on the box.
    - Never creates a directory. A caller that writes makes its own, so a
      reader importing this module cannot leave a trail of empty dirs behind.
"""

from __future__ import annotations

import os
import re
from typing import Any

LEDGER_FILENAME = "next-move-ledger.jsonl"
INTAKE_FILENAME = "obligations-inbound.jsonl"
SEND_LOG_FILENAME = "group-em-send-log.jsonl"

#: A session id arrives from the harness registry (peers) and the environment
#: (the caller), and is joined straight into a path a writer will `makedirs`.
#: The sibling reader (`receiver_state_reader.receiver_state_path`) rejects an
#: unsafe component, a bare `.`/`..` the character class alone would pass
#: included; the same guard applies here rather than trusting the producer.
_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_session_id(session_id: Any) -> bool:
    """Is `session_id` safe to join into a path? Never raises."""
    return (
        isinstance(session_id, str)
        and bool(session_id)
        and session_id not in (".", "..")
        and bool(_SAFE_SID_RE.match(session_id))
    )


def share_dir(repo_root: str, session_id: str) -> str:
    """`<repo_root>/state/subagent-share/<session_id>`.

    Repo-root relative, never under the git dir -- see
    `hooks.watchdog_undischarged_next_move`'s module docstring § LEDGER
    LOCATION for the reasoning, which is unchanged by this move.
    """
    return os.path.join(repo_root, "state", "subagent-share", session_id)


def ledger_path(repo_root: str, session_id: str) -> str:
    """This session's next-move ledger."""
    return os.path.join(share_dir(repo_root, session_id), LEDGER_FILENAME)


def intake_path(repo_root: str, session_id: str) -> str:
    """This session's obligations intake."""
    return os.path.join(share_dir(repo_root, session_id), INTAKE_FILENAME)


def send_log_path(repo_root: str, session_id: str) -> str:
    """The Group EM send log for the session that made the offers."""
    return os.path.join(share_dir(repo_root, session_id), SEND_LOG_FILENAME)

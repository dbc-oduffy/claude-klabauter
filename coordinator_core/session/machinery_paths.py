"""The machinery root: one owner for paths a dozen modules were each
spelling out for themselves.

WHAT THIS IS. `<repo_root>/.coordinator-local/` -- the gitignored root every
coordinator-machinery bucket lives under (the next-move ledger, the
obligations intake, the Group EM send log, the review trail, ceremony
records, dispatch briefs, plan sidecars, caches, and the moved-not-killed
ledgers) -- and the accessors that name each bucket under it. Nothing here
reads, writes, or interprets any bucket's contents; this module owns only
WHERE they are and WHO may name a directory or file under the root.

WHY IT EXISTS. `group_em.send_pass`, `group_em.obligations` and
`hooks.watchdog_undischarged_next_move` each carried their own
`_session_share_dir` and their own `_LEDGER_FILENAME = "next-move-ledger.jsonl"`
-- the same directory join and the same string, retyped three times, with
`obligations` additionally reaching into `send_pass`'s underscore-private
namespace to borrow one of them. Two failure modes, neither of which any test
covered: a typo corrected in one copy and not the others silently splits the
producers and the readers onto different files, and a private symbol used
across a module boundary can be changed by an edit to its own module that has
no reason to look for foreign callers. Twenty-one live sites built paths this
way; this module is their single owner.

WHY `session/` AND NOT `group_em/`. The dependency arrow. `group_em` and
`hooks` both import `session`; `session` imports neither. Homing this in
`group_em` would have made a hook depend on the Group EM package for a path
join, which is the coupling this consolidation exists to remove rather than
relocate.

WHY ONE MODULE, NOT TWO. A module named for one bucket (`subagent_share.py`)
owning every bucket's path was rejected on naming honesty -- until the
integration pass found that C2 already pays to rewrite this module's body
and repoint all 21 importers, making the rename to `machinery_paths.py` free
inside work already budgeted. There is no second module; `subagent_share.py`
stays only as a back-compat re-export shim for the wave gap (see that
module's own docstring).

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


def machinery_root(repo_root: str) -> str:
    """`<repo_root>/.coordinator-local` -- the gitignored root every
    coordinator-machinery bucket lives under.

    Repo-root relative, never under the git dir, never resolved from
    `$HOME` or any single-machine absolute path -- see
    `hooks.watchdog_undischarged_next_move`'s module docstring § LEDGER
    LOCATION for the reasoning behind repo-root-relative machinery paths.
    """
    return os.path.join(repo_root, ".coordinator-local")


def share_dir(repo_root: str, session_id: str) -> str:
    """`<machinery_root>/subagent-share/<session_id>`."""
    return os.path.join(machinery_root(repo_root), "subagent-share", session_id)


def ledger_path(repo_root: str, session_id: str) -> str:
    """This session's next-move ledger."""
    return os.path.join(share_dir(repo_root, session_id), LEDGER_FILENAME)


def intake_path(repo_root: str, session_id: str) -> str:
    """This session's obligations intake."""
    return os.path.join(share_dir(repo_root, session_id), INTAKE_FILENAME)


def send_log_path(repo_root: str, session_id: str) -> str:
    """The Group EM send log for the session that made the offers."""
    return os.path.join(share_dir(repo_root, session_id), SEND_LOG_FILENAME)


def review_trail_dir(repo_root: str) -> str:
    """`<machinery_root>/review-trail` -- the relocated review-trail bucket."""
    return os.path.join(machinery_root(repo_root), "review-trail")


def ceremony_dir(repo_root: str) -> str:
    """`<machinery_root>/ceremony` -- the relocated ceremony bucket."""
    return os.path.join(machinery_root(repo_root), "ceremony")


def dispatch_briefs_dir(repo_root: str) -> str:
    """`<machinery_root>/dispatch-briefs` -- the relocated dispatch-briefs bucket."""
    return os.path.join(machinery_root(repo_root), "dispatch-briefs")


def plan_sidecars_dir(repo_root: str) -> str:
    """`<machinery_root>/plan-sidecars` -- the relocated plan-sidecars bucket."""
    return os.path.join(machinery_root(repo_root), "plan-sidecars")


def cache_dir(repo_root: str) -> str:
    """`<machinery_root>/cache` -- the relocated cache bucket."""
    return os.path.join(machinery_root(repo_root), "cache")


def orientation_cache_path(repo_root: str) -> str:
    """`<machinery_root>/orientation_cache.md` -- the relocated orientation cache."""
    return os.path.join(machinery_root(repo_root), "orientation_cache.md")


def cockpit_emission_path(repo_root: str) -> str:
    """`<machinery_root>/cockpit-emission.json` -- the relocated cockpit emission spool."""
    return os.path.join(machinery_root(repo_root), "cockpit-emission.json")


def ledgers_dir(repo_root: str) -> str:
    """`<machinery_root>/ledgers` -- moved, not killed (PM Adjudication 4):
    tracked-content ledgers relocated alongside the machinery buckets but
    excluded from the history rewrite and retention cap C6/C7/C11/C12 apply
    to the rest of the root.
    """
    return os.path.join(machinery_root(repo_root), "ledgers")


def kill_ledger_path(repo_root: str) -> str:
    """`<machinery_root>/kill-ledger.md` -- moved, not killed."""
    return os.path.join(machinery_root(repo_root), "kill-ledger.md")


def memo_outbox_sent_ledger_path(repo_root: str) -> str:
    """`<machinery_root>/memo-outbox/sent-ledger.jsonl` -- moved, not killed."""
    return os.path.join(machinery_root(repo_root), "memo-outbox", "sent-ledger.jsonl")

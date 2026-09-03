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
from functools import lru_cache
from typing import Any, Pattern

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


def share_root(repo_root: str) -> str:
    """`<machinery_root>/subagent-share` -- the parent every per-session share
    directory hangs off.

    Exists because callers that enumerate sessions (a reaper, a citation
    guard) need the bucket itself, not one session's directory, and were
    otherwise rebuilding the join by hand against a stale `state/` literal --
    the exact drift this module owns.
    """
    return os.path.join(machinery_root(repo_root), "subagent-share")


def share_dir(repo_root: str, session_id: str) -> str:
    """`<machinery_root>/subagent-share/<session_id>`."""
    return os.path.join(share_root(repo_root), session_id)


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


@lru_cache(maxsize=None)
def subagent_share_leaf_pattern() -> Pattern[str]:
    """Compiled pattern matching a sidecar LEAF under either machinery root,
    capturing `root`, `session` and `leaf` as named groups.

    Same convention as `subagent_share_id_pattern` -- either root, `/` and
    `\\` alike -- differing only in what it captures: that accessor stops at
    the session-id segment, this one requires a leaf basename after it and
    hands back all three parts. The write guards need the leaf (they parse
    `<label>.<agent_id>.md` out of it) and the root (their denial message
    echoes the path the caller actually wrote, not a hardcoded one), which
    is why they could not consume the id accessor and hand-rolled their own
    single-root regexes instead -- and were silently retired by the
    relocation when they did. The convention lives here so a bucket move is
    one edit, not a sweep of every guard that happens to spell it.

    `root` is captured, not merely accepted: a caller that echoes a path
    back to an operator must name the root that operator actually used, and
    both roots are live (pre-relocation paths persist in committed
    citations and archived records).
    """
    return re.compile(
        r"(?:^|[/\\])(?P<root>state|\.coordinator-local)[/\\]subagent-share"
        r"[/\\](?P<session>[^/\\]+)[/\\](?P<leaf>[^/\\]+)$"
    )


@lru_cache(maxsize=None)
def subagent_share_id_pattern() -> Pattern[str]:
    """Compiled pattern matching the `subagent-share` bucket under EITHER
    root (`state/` or `.coordinator-local/`), accepting `/` and `\\` alike
    regardless of host OS, and capturing as group(1) the session-id segment
    that follows the bucket.

    Either machinery root is accepted. The bucket moved from
    `state/subagent-share/` to `.coordinator-local/subagent-share/`
    (docs/plans/2026-09-02-state-keeps-the-work-not-the-machinery.md); a
    pattern pinned to the old root matches no live sidecar, and a reader
    keyed on it then returns "no owner" for every one of them -- silently,
    because an unmatched path is indistinguishable from an artifact that is
    simply not a sidecar. Both spellings stay accepted rather than
    swapping: pre-relocation paths persist in committed citations and
    archived records, and a reader that refuses them re-breaks the corpus
    the relocation left readable.

    Distinct from `record_homes.home_pattern`: that accessor deliberately
    has no capture group (membership only), while every caller here exists
    to recover the session id that follows the bucket, so this pattern
    captures it as group(1) rather than being called for its match alone.

    Tradeoff (inherited, not rediscovered): a genuine POSIX filename
    containing a literal backslash byte is split on that byte too -- the
    subagent-share id universe (machine-authored session ids) never
    contains one.
    """
    return re.compile(
        r"(?:^|[/\\])(?:state|\.coordinator-local)[/\\]subagent-share[/\\]([^/\\]+)(?:[/\\]|$)"
    )

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

#: The one spelling of the machinery root's leaf name. `machinery_root()`
#: joins this onto `repo_root`; every other repo-relative constant that
#: needs to spell the root (e.g. `MEMO_OUTBOX_RELDIR` below) builds off this
#: same literal rather than respelling it.
_MACHINERY_ROOT_LEAF = ".coordinator-local"

_SAFE_SID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_session_id(session_id: Any) -> bool:
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
    return os.path.join(repo_root, _MACHINERY_ROOT_LEAF)


#: The one spelling of the share bucket's leaf name. `share_root()` joins it
#: onto `machinery_root()`; `SHARE_RELDIR` builds the repo-relative spelling
#: off the same two literals, so this module holds one spelling of each.
SHARE_LEAF = "subagent-share"

#: Repo-relative, POSIX-separated spelling of the CURRENT share root, for the
#: declaration sites an absolute-path accessor structurally cannot serve --
#: same role as `MEMO_OUTBOX_RELDIR`.
SHARE_RELDIR = "/".join([_MACHINERY_ROOT_LEAF, SHARE_LEAF])


def share_root(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), SHARE_LEAF)


def share_dir(repo_root: str, session_id: str) -> str:
    return os.path.join(share_root(repo_root), session_id)


def machinery_path_prefixes() -> tuple:
    """Every repo-relative path PREFIX a name-based excluder (a corpus
    census/walk) must skip to exclude coordinator machinery, current and
    legacy roots alike.

    Decided NOT to gravestone: the premise "the legacy root is retired"
    does not hold -- coordinator-content-repo's `coordinator/hooks/scripts/_plan_path_bridge.py`
    module and several reviewer-sidecar writers there still cite and match
    live `state/subagent-share/<session>/...` paths, so a census keyed on
    `.coordinator-local` alone would silently miss every legacy-root
    artifact. Re-verify against both repos before ever dropping this leg;
    do not gravestone on the strength of a single grep.

    Anchored at a full path segment, never a name token -- same selector
    discipline as `fleet_machinery_sweep.select_machinery_paths` (a
    caller matches these as PREFIXES of a repo-relative candidate, not a
    substring). Returns a fixed tuple of bare relative strings rather than
    a `repo_root`-joined path: a name-based excluder tests a
    repo-relative candidate, it does not resolve a filesystem path -- same
    role as `SHARE_RELDIR`/`LEGACY_SHARE_RELDIR` above.
    """
    return (_MACHINERY_ROOT_LEAF, LEGACY_SHARE_RELDIR, LEGACY_MEMO_OUTBOX_RELDIR)


#: Repo-relative, POSIX-separated spelling of the RETIRED share root. Same
#: role as `LEGACY_MEMO_OUTBOX_RELDIR`: the declaration sites that need a
#: bare relative string (op `MUTATES` lists, guard prefix tuples) cannot use
#: an accessor that takes a `repo_root`.
LEGACY_SHARE_RELDIR = "state/subagent-share"

#: REMOVAL TRIGGER for every dual-root share read below, named because the
#: relocation shipped without one and a reader repointed at only the new
#: root reports the entire pre-move corpus as absent.
#:
#: Drop the legacy leg -- `legacy_share_root`, `legacy_share_dir`,
#: `LEGACY_SHARE_RELDIR`, and the second element of `share_roots`/`share_dirs`
#: -- when `state/subagent-share/` in this repo is gone (reaped by
#: `coordinator/bin/reap-stale-subagent-sidecars.py`, which walks both roots
#: for exactly this reason) and no session provisioned before the relocation
#: republish is still live. Checkable in one command:
#:   `test -d state/subagent-share || echo drop-the-leg`


def legacy_share_root(repo_root: str) -> str:
    return os.path.join(repo_root, *LEGACY_SHARE_RELDIR.split("/"))


def legacy_share_dir(repo_root: str, session_id: str) -> str:
    return os.path.join(legacy_share_root(repo_root), session_id)


def share_roots(repo_root: str) -> list:
    return [share_root(repo_root), legacy_share_root(repo_root)]


def share_dirs(repo_root: str, session_id: str) -> list:
    return [os.path.join(root, session_id) for root in share_roots(repo_root)]


def ledger_path(repo_root: str, session_id: str) -> str:
    return os.path.join(share_dir(repo_root, session_id), LEDGER_FILENAME)


def intake_path(repo_root: str, session_id: str) -> str:
    return os.path.join(share_dir(repo_root, session_id), INTAKE_FILENAME)


def send_log_path(repo_root: str, session_id: str) -> str:
    return os.path.join(share_dir(repo_root, session_id), SEND_LOG_FILENAME)


def review_trail_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "review-trail")


def ceremony_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "ceremony")


def dispatch_briefs_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "dispatch-briefs")


def plan_sidecars_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "plan-sidecars")


def cache_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "cache")


def cockpit_emission_path(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "cockpit-emission.json")


def ledgers_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "ledgers")


def kill_ledger_path(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), "kill-ledger.md")


#: The memo-outbox leaf under `machinery_root()`. Not a second spelling of
#: the machinery root itself -- `MEMO_OUTBOX_RELDIR` below is built from this
#: leaf plus `_MACHINERY_ROOT_LEAF`, the SAME literal `machinery_root()`
#: joins onto `repo_root`, so there is exactly one spelling of
#: `.coordinator-local` in this module, not two.
MEMO_OUTBOX_LEAF = "memo-outbox"

#: Repo-relative, POSIX-separated spellings of the two outbox roots, for the
#: declaration sites an absolute-path accessor structurally cannot serve: op
#: `MUTATES` lists, `_SUPERSEDES_ANCHORS`, and the `_LEGACY_*_REL` tuples.
#: Those sites need a bare relative string, not a path built from a
#: caller-supplied `repo_root`, so before these constants existed the literal
#: was respelled by hand at five declaration sites and the eventual removal of
#: dual-root support would have been five separate edits to find
#: (coordinator:overengineering-reviewer, 2026-09-03). Built from
#: `_MACHINERY_ROOT_LEAF`, the same literal `machinery_root()` uses, so this
#: constant and `memo_outbox_dir` cannot drift onto two spellings of the
#: machinery root.
MEMO_OUTBOX_RELDIR = "/".join([_MACHINERY_ROOT_LEAF, MEMO_OUTBOX_LEAF])
LEGACY_MEMO_OUTBOX_RELDIR = "state/memo-outbox"

#: REMOVAL TRIGGER for every dual-root outbox read, named because the
#: dual-root branch shipped without one and "temporary" fallbacks that name no
#: end condition are how a migration window becomes permanent.
#:
#: Drop the legacy leg -- `legacy_memo_outbox_dir`, `legacy_memo_outbox_sent_dir`,
#: `LEGACY_MEMO_OUTBOX_RELDIR`, `memo_draft.merged_outbox_drafts`' second glob,
#: and `_SUPERSEDES_ANCHORS`' legacy entry -- when BOTH hold:
#:
#:   1. `state/memo-outbox/` in this repo contains no `status: draft` memo.
#:      Drafts staged before the 2026-09-03 repoint are the only live content
#:      the fallback exists to drain; `sent/` history is not a reason to keep
#:      a READ path, since nothing resolves a sent memo by outbox lookup.
#:   2. The 984 already-tracked files are untracked and `.gitignore` lists
#:      this bucket alongside its siblings. That is PM-gated (a `git rm
#:      --cached` deletes peers' live queued drafts on their next pull), so
#:      this condition is not the engine's to clear unilaterally.
#:
#: Condition 1 is checkable in one command:
#:   `grep -l "^status: draft" state/memo-outbox/*.md`
#: An empty result with condition 2 satisfied means delete the leg, not keep
#: it "just in case" -- a fallback nothing can reach is dead code.


def memo_outbox_dir(repo_root: str) -> str:
    return os.path.join(machinery_root(repo_root), MEMO_OUTBOX_LEAF)


def memo_outbox_sent_dir(repo_root: str) -> str:
    return os.path.join(memo_outbox_dir(repo_root), "sent")


def memo_outbox_sent_ledger_path(repo_root: str) -> str:
    return os.path.join(memo_outbox_dir(repo_root), "sent-ledger.jsonl")


def legacy_memo_outbox_dir(repo_root: str) -> str:
    return os.path.join(repo_root, *LEGACY_MEMO_OUTBOX_RELDIR.split("/"))


def legacy_memo_outbox_sent_dir(repo_root: str) -> str:
    return os.path.join(legacy_memo_outbox_dir(repo_root), "sent")


@lru_cache(maxsize=None)
def subagent_share_leaf_pattern() -> Pattern[str]:
    return re.compile(
        r"(?:^|[/\\])(?P<root>state|\.coordinator-local)[/\\]subagent-share"
        r"[/\\](?P<session>[^/\\]+)[/\\](?P<leaf>[^/\\]+)$"
    )


@lru_cache(maxsize=None)
def subagent_share_id_pattern() -> Pattern[str]:
    return re.compile(
        r"(?:^|[/\\])(?:state|\.coordinator-local)[/\\]subagent-share[/\\]([^/\\]+)(?:[/\\]|$)"
    )

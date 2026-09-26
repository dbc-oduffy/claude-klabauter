"""coordinator_core.bash_guards._write_bump_stand_down -- the ONE
environment stand-down the three write-confinement bumps share.

Purpose: answer, in one place, whether cross-repo write gating is a real rule
on THIS host or an impossible ask -- and, when it is an impossible ask, carry
the durable record that keeps the downgrade honest. Consumed by all three
write-confinement surfaces:

  - `bash_guards/bump_foreign_repo_write.py`   (C4, Bash, foreign repo)
  - `bash_guards/bump_outside_repo_write.py`   (C5, Bash, outside any repo)
  - `write_guards/bump_out_of_repo_tool_write.py` (C7, Write/Edit/MultiEdit/
    NotebookEdit)

WHY THIS MODULE EXISTS RATHER THAN THREE COPIES. The mechanism was authored
in C4 alone (measured 2026-09-05) and never ported, so for the whole of that
window the Bash foreign-repo surface stood down on a managed remote container
while the tool surface hard-denied the IDENTICAL write. An agent could
`git commit` into the granted sibling but not `Edit` a file in it. That is
not a narrow gap: the tool surface is the one a well-meaning agent reaches
for first, and C7's own `CLASS = "hard-deny"` makes its version of the
refusal the least passable of the three. Every one of these modules already
carries a docstring section warning that an independently-derived second copy
of a shared predicate is the failure mode -- `_write_bump_applicability`
exists for exactly that reason. This module is that rule applied to the
stand-down itself: import and call, never re-derive.

WHICH CAPABILITY, AND WHY NOT THE OBVIOUS ONE. `fleet_present`, not
`ephemeral_host`.

These bumps ask one question -- *is this somebody else's tree?* -- and the
answer turns on whether there is a fleet around this session at all, not on
whether the filesystem survives the session. `ephemeral_host` is an input to
`fleet_present` (see `environment._probe_fleet_present`), so keying on it
directly would both duplicate that probe's composition and bind a boundary
question to a storage-durability fact. A durable self-hosted runner that
sets the managed-remote markers is the case that distinguishes them, and
`environment.py`'s own evidence string names it.

THE CASE THIS EXISTS FOR, restated because the shape is easy to lose. On a
managed remote container the session is handed a closed set of repos by
explicit grant, and it is the only writer on the box. The "foreign" repo is
one this same session was given. Denying the write there protects no other
team's tree -- there is no other team on this machine -- while the
alternative the doctrine offers (send a cross-repo memo) has no reader
either. So the guard forbids the only correct move while the incorrect one
stays available, which inverts the north star it was built to serve. And a
grant is not a boundary: the session was handed these repos together, so
treating a write between them as a trespass misreads the grant.

IT STANDS DOWN, IT DOES NOT STOP CARING. The stand-down is a DOWNGRADE to
after-the-fact warning, never a deletion. Cloud sessions really should work
one repo at a time -- a session that mixes several repos in one commit stream
produces a history no single repo's reviewers can follow, and that cost is
real whether or not a peer EM exists. So the advisory still says so
(`stand_down_reason`), and the durable record still lands
(`log_environment_stand_down`). What changes is that the write proceeds.

FAIL OPEN, LIKE EVERYTHING ELSE IN THIS FAMILY. An unimportable or unhappy
capability layer returns `None` from `environment_stands_the_bump_down`, and
each guard behaves exactly as it did before this module existed. A failed
audit write never alters a guard's allow/deny decision.

Negative-spec:
  - Does NOT decide whether a bump APPLIES (that is
    `_write_bump_applicability.bump_applies`) or whether a marker clears it
    (`_write_bump_marker`). It answers only "is this rule coherent on this
    host at all", and is consulted AFTER a guard has otherwise resolved that
    it would bump.
  - Does NOT return a hook envelope. Returning a non-`None` envelope from a
    stand-down path claims the dispatcher's slot and silently skips every
    guard registered after it -- the regression `_stand_down_notice`'s own
    docstring in C4 records. `stand_down_notice` prints and returns `None`;
    "stand down" means this guard declines to object and every other guard
    still runs.
  - Does NOT key on `ephemeral_host` -- see "WHICH CAPABILITY" above.
  - Does NOT re-derive the overrides-log path. `_override_log_path` owns the
    never-mint-a-phantom-session rule; one copy of it is the point.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.bash_guards._override_log_path import _override_log_path

GENERATES = []


def environment_stands_the_bump_down(env: Optional[Any] = None):
    try:
        from coordinator_core.environment import capability

        fleet = capability("fleet_present", env=env)
    except Exception:
        return None
    return None if fleet.value else fleet


def stand_down_reason(
    guard_label: str, target_repo: str, session_repo: str, evidence: str
) -> str:
    return (
        "%s: allowed a write into %s from a session anchored at %s.\n"
        "Cross-repo write gating does not apply on this host: %s. "
        "The repos this session holds were granted together, so a write "
        "between them is not a trespass, and there is no peer EM to memo.\n"
        "Standing preference, unchanged: a cloud session should carry ONE "
        "repo's work at a time. Mixing repos in one commit stream produces a "
        "history no single repo's reviewers can follow."
        % (guard_label, target_repo or "<unresolved>", session_repo or "<unresolved>", evidence)
    )


def stand_down_notice(reason: str) -> None:
    """Print the stand-down notice to stderr and return NOTHING.

    RETURNING AN ENVELOPE HERE IS A REGRESSION -- `dispatch`'s chain loop is
    `if out is not None: ... return out`, so any non-`None` value CLAIMS THE
    SLOT and ends evaluation, silently skipping every guard registered after
    this one (`validate-commit` among them, which composes real denies on its
    normal path). The deny this replaces short-circuited identically, which
    is exactly why such a change looks safe: a deny makes the skipped guards
    moot, an allow does not.

    Two further reasons an envelope buys nothing it appears to. The warm rung
    (`ops/warm_guard_evaluate._verdict_from_envelope`) collapses every
    non-deny dict to NO_OBJECTION, so a `systemMessage` never leaves the
    process on the transport serving nearly all PreToolUse events. And
    `systemMessage` is the OPERATOR channel (`warm/hook_http.py` states the
    split) while an agent reads only a nested
    `hookSpecificOutput.additionalContext`.

    So the notice goes to stderr, which the cold rung surfaces, and the
    DURABLE record is the `overrides.log` line.
    """
    print(reason, file=sys.stderr)


def log_environment_stand_down(
    git_root: Optional[str],
    session_id: str,
    target_repo: str,
    evidence: str,
    *,
    marker: str,
    sink_basename: str,
) -> None:
    """Append one audit line recording that a bump stood down.

    THIS IS NOT OPTIONAL BOOKKEEPING, it is what keeps the downgrade honest.
    A block leaves evidence by stopping the world; a warning scrolls past in
    a transcript nobody re-reads. Without a durable trace, "permissive and
    warn" degrades to "permissive" and the boundary stops existing rather
    than becoming advisory -- strictly worse than the deny it replaces.

    WHICH IS WHY IT WRITES TWICE ON AN EPHEMERAL HOST. The `.git`-resident
    overrides.log follows the convention peer guards already use
    (`commit_tripwires._log_pathspec_divergence_override`,
    `check_blanket_git_add`) and is the right home on a workstation. But
    `.git` is never tracked and never pushed, and the dominant reason these
    guards stand down at all is an ephemeral host -- whose own evidence says
    the filesystem does not survive the session. The safeguard would be
    destroyed with the container precisely where it was claimed to matter.

    So when `durable_repo` reports a git remote, a second copy lands in a
    TRACKED path a push can carry off the box.

    `marker` is the audit line's verdict token (e.g.
    `STAND-DOWN-FOREIGN-REPO-WRITE`) and `sink_basename` the tracked log's
    filename -- both per-surface, so three surfaces standing down are three
    distinguishable records rather than one ambiguous stream.
    """
    try:
        if not git_root:
            return
        override_log = _override_log_path(git_root, session_id)
        if override_log is None:
            return
        line = "%s | %s | %s | %s | %s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            session_id or "no-session",
            marker,
            target_repo[:80],
            evidence[:160],
        )
        with open(override_log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
        _mirror_to_durable_sink(Path(git_root), line, sink_basename)
    except OSError as exc:
        print(
            "write-bump stand-down: failed to write audit log: %s" % exc,
            file=sys.stderr,
        )


def _mirror_to_durable_sink(git_root: Path, line: str, sink_basename: str) -> None:
    try:
        from coordinator_core.environment import capability

        if not capability("durable_repo").value:
            return
        sink = git_root / "state" / "stand-downs"
        sink.mkdir(parents=True, exist_ok=True)
        with open(sink / sink_basename, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except Exception:
        return

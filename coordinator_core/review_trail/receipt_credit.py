"""
coordinator_core.review_trail.receipt_credit — the SECOND credit source for
review coverage: a commit is credited when the session that authored it
carries a counting reviewer sidecar receipt stamped no earlier than the
commit itself.

Purpose: the reviewed-set store (`coordinator_core.review_trail.reviewed_set`)
is fed exclusively by `state/review-trail/*.json` records folded in at write
time. That corpus is frozen — `review_trail.write`'s in-process wiring was
removed 2026-08-23 by PM ruling and stays removed (DR-372, DR-374;
`coordinator_core/cartography/tests/test_op_edges.py` carries the removal
note), and no production call site resolves the op today. Reviews now land on
the reviewer's own sidecar under `state/subagent-share/<session_id>/`, as the
`review_receipt:` frontmatter block `subagent_sandbox.provision_report
._splice_review_receipt` stamps at dispatch.

The consequence, measured in this clone 2026-08-28: the store held 3550 SHAs
and its newest covered commit was 486 commits behind HEAD; NONE of the most
recent 400 commits were members. Every reader of the store therefore returned
a confident "uncovered" for all recent work — not an error, not an
`indeterminate`, a clean negative. `coordinator_core.coverage`'s
`indeterminate` flag cannot catch this: it has exactly one setter, an
`except Exception` around a handoff read, so it reports a read that FAILED. A
frozen corpus raises nothing. This is a read that succeeds and returns empty,
which is the direction every guard here was built blind to.

THE JOIN, and why it needs a clock.

A receipt block carries exactly four fields — `session_id`, `agent_id`,
`agent_type`, `stamped_at` (`provision_report._receipt_block`). It names no
SHA and no range, so it cannot populate a SHA-keyed set on its own. What
bridges the two is the `Session-Id:` commit trailer, present on 196 of the
last 200 commits here: the receipt says a reviewer ran for session S, and the
trailer says this commit belongs to session S.

That join alone is NOT sufficient, and shipping it alone would be worse than
the stuck negative it replaces — an "uncovered" nobody trusts costs a
redundant review, a wrong "covered" costs the review itself. Measured over
400 commits / 51 sessions: crediting every commit whose session holds any
receipt credits 95 commits, of which 40 (42%) were authored AFTER that
review had already finished. A reviewer dispatched at T cannot have read a
commit that did not exist at T.

So credit requires `commit_date <= stamped_at`. Under that rule the same
sample credits 43, drops those 40, and leaves 300 uncovered because their
session holds no receipt at all — the instrument still says "no" in both
directions, which is the only reason its "yes" is worth anything.

DELIBERATELY NOT CREDITED: the 12 commits in the remaining sample that were
authored after the receipt was stamped but before the sidecar's last write —
"committed while the review was still running". A file mtime is not evidence
a reviewer read a commit, it is not a semantic field, and it is trivially
perturbed by any later touch of the sidecar. Ambiguity favors more review
(the same direction `gate_dimension_review` already takes on an unresolvable
range), so these stay uncovered.

WHY THE RECEIPT BLOCK AND NOT THE DISPOSITIONS BLOCK. `## Integrator
Dispositions` is the other candidate surface and it is the wrong one, for two
independent reasons.

The first reason below
described `ops/append_integrator_dispositions.py` as it stood before commit
1b44e2138c and is now stale prose, corrected here rather than left to drift:
`already_dispositioned=True` no longer means the call declined to write a
second block — it now means a block was already there, and the call appends
a NEW block naming the one it supersedes (the last block is the operative
one). So a finding escalated and later resolved is no longer permanently
stuck reading `escalated-ask` the way the bug-backlog entry below describes;
a re-dispositioning integrator run now updates it. The conclusion of this
section is unchanged by that fix — see the SECOND reason, which stands on its
own: the close ceremony still permits an EM to apply a reviewer's findings
itself rather than dispatching a `review-integrator`, which still produces no
dispositions block at all while the review demonstrably happened, and the
`review_receipt:` block is still present in both cases. (Original filing:
`state/bug-backlog/2026-08-27-the-sole-review-receipt-cannot-record-that-an-
escalation-was-answered.yaml` — its own root cause is fixed; this module's
choice of surface does not depend on it having been.) Do not "improve" this
module by repointing it at the dispositions block; the second reason alone
is still the same defect in a new place, not a cleanup.

Relationship to `workstream_complete._compute_review_receipt_gate`: that gate
reads the same block to answer a DIFFERENT question — "did a review run for
THIS session inside its baton claim window", one session, no commits. This
module answers "which of THESE commits are covered", many sessions, and needs
the per-commit clock comparison that gate has no use for. The two overlap on
the counting-receipt predicate only; see `state/improvement-queue/` for the
filed de-duplication.

Cost: zero added subprocesses. The commit date and `Session-Id` trailer ride
along in a `git log` the caller already spawns, and the receipt read is a
directory listing plus a frontmatter parse per DISTINCT session. Measured
62.5ms process time over 1000 commits / 105 sessions, against DR-344's 500ms
brightline. `parse_frontmatter` (29.1ms to import) is imported lazily inside
the lookup rather than at module scope, so a caller whose commits are all
already credited by the store pays none of it.

THE UNIT IS WEAKER THAN THE CRITERION IT SERVES, and this is a chosen limit
rather than an oversight. Ruled by claude-klabauter-ba 2026-08-28, who owns the
merge gate this feeds: land it, and write the gap down where the next reader
will hit it.

A receipt certifies a SESSION. The merge gate's own prime exit criterion asks
for "a review record NAMING IT" — a commit. A session receipt names no commit.
The ordering rule above closes the forward half of that gap (a review cannot
have read a commit that did not yet exist) and leaves the backward half open:
because credit requires only `commit_date <= stamped_at`, ONE receipt late in
a session credits EVERY earlier commit in that session, however many there
are and whatever they touched. Nothing here bounds that fan-out.

So this reader answers "was this session reviewed, before or at the moment
this commit existed?" and is being used to answer "was this commit reviewed?".
Those coincide for a session that was reviewed once at its end with a handful
of commits behind it, and diverge as the commit count between receipts grows.
It is accepted as an interim because the alternative in place was a store
that credited nothing at all, and moving from "refuses everything" to
"discriminates" forecloses none of the eventual fix. It is NOT the end state.
Closing it needs a receipt that carries a range — which is a change to what
`provision_report._receipt_block` stamps, not a change to this reader.

Negative-spec:
    - Does NOT certify a commit; it certifies the session that authored one.
      See the paragraph above for the backward fan-out this leaves open. Do
      not read a credit from this module as "a reviewer read this commit".
    - Does NOT write anything. No store, no fold, no sidecar mutation. This
      is a read-side credit source; `reviewed_set.py` remains the only
      writer of the resident store, and nothing here folds into it.
    - Does NOT spawn a subprocess. If a caller needs commit dates or
      trailers it must widen its own existing `git log`; this module takes
      them as arguments. A future edit that shells out here reintroduces
      exactly the per-call git cost the resident store was built to remove.
    - Does NOT credit a commit whose session id is absent, unparseable, or
      names a session with no counting receipt. Absence is never credit.
    - Does NOT accept a bare receipt on its own once its session holds a
      `review_completion:` block anywhere: a receipt is stamped at DISPATCH,
      before the reviewer runs, so a session with any completion evidence
      requires the SAME sidecar (or its `agent_id: ''` findings twin, same
      session + receipt `agent_type`) to also carry a session-matching
      `review_completion:` block, plus authored content in one of the two.
      In a session with no completion evidence at all (the writer never ran
      there) a filled body is still sufficient on its own -- the fallback
      `_compute_review_receipt_gate` also applies.
      Residual: the twin join is by (session, receipt `agent_type`), not by
      `agent_id` -- no surface here stamps one for the findings twin -- so
      two same-type reviewers in one session, one silent and one that wrote
      the twin, both count. See plan section "Risks and named residuals",
      "The twin join is by type, not by id."
    - Does NOT treat `integrator_receipt:` as review evidence. That block
      records that findings were applied, which is a separate fact; a
      review whose findings needed no application is still a review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from coordinator_core.session.machinery_paths import (
    machinery_root as _machinery_root,
    share_roots as _share_roots,
)

#: The frontmatter key `provision_report._splice_review_receipt` stamps. The
#: integrator's counterpart (`integrator_receipt`) is deliberately not read —
#: see the module docstring's negative-spec.
_RECEIPT_KEY = "review_receipt"

#: A `Session-Id:` trailer value must look like a session id before it is used
#: as a directory name. This is the only thing standing between a malformed
#: trailer and a path join, so it is deliberately strict: the trailer is
#: commit-message text, which is author-controlled.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _parse_timestamp(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 receipt/commit timestamp to an aware datetime, or
    None when it is absent or unparseable.

    A naive timestamp is read as UTC — the receipt writer
    (`provision_report`) stamps UTC, and treating a naive value as local time
    would shift the comparison by the host's offset and silently credit or
    drop commits near the boundary. Returning None on a bad parse is the
    conservative direction: a receipt whose clock cannot be read credits
    nothing, rather than credits everything.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _bare_agent_type(agent_type: object) -> Optional[str]:
    """`agent_type` namespace-stripped (`coordinator:code-reviewer` ->
    `code-reviewer`), or None if `agent_type` is not a string. Dispatch
    writes the namespaced form; the vocabulary and the twin-type join both
    key on the bare name."""
    if not isinstance(agent_type, str):
        return None
    return agent_type.rpartition(":")[2] if ":" in agent_type else agent_type


@dataclass(frozen=True)
class _SessionSummary:
    """The completion-writer state for one session, computed once across
    every extant share-root directory -- see `receipt_credited_shas`. Never
    per-directory: a twin under one root must be able to lend content to a
    run-report under the other, and `session_has_completion` must not
    disagree between a session's pre- and post-relocation halves."""

    session_has_completion: bool
    #: Bare (namespace-stripped) receipt `agent_type`s for which a
    #: session-matching, `agent_id: ''`, completion-less, filled findings
    #: twin sidecar exists (the shape `provision-sidecar.py` writes).
    filled_twin_types: frozenset


def _has_own_completion(frontmatter: Dict, session_id: str) -> bool:
    """True iff `frontmatter` carries a `review_completion:` dict whose
    `session_id` matches `session_id`."""
    completion = frontmatter.get("review_completion")
    return isinstance(completion, dict) and completion.get("session_id") == session_id


def _parsed_session_sidecars(
    share_dirs: Iterable[Path], session_id: str
) -> List[Tuple[Dict, str]]:
    """Every `(frontmatter, full_text)` pair this session's sidecars parse
    to, across every share-root directory in `share_dirs`. One read pass,
    reused by both the session summary and the per-directory stamp
    collection below.

    Never raises. An unreadable sidecar, undecodable bytes, or a
    frontmatter block that will not parse is skipped, not fatal -- same
    contract as the rest of this module."""
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    parsed_sidecars: List[Tuple[Dict, str]] = []
    for share_dir in share_dirs:
        session_dir = share_dir / session_id
        if not session_dir.is_dir():
            continue
        for sidecar in sorted(session_dir.glob("*.md")):
            try:
                text = sidecar.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                parsed = parse_frontmatter(text)
            except Exception:
                continue
            frontmatter = parsed.get("frontmatter")
            if not isinstance(frontmatter, dict):
                continue
            parsed_sidecars.append((frontmatter, text))
    return parsed_sidecars


def _compute_session_summary(
    parsed_sidecars: List[Tuple[Dict, str]], session_id: str
) -> _SessionSummary:
    """The session summary the module docstring names: whether ANY sidecar
    carries a session-matching `review_completion` block, plus the set of
    receipt `agent_type`s for which a filled, completion-less, `agent_id: ''`
    findings twin exists for this session (`provision-sidecar.py`'s shape,
    census row 2)."""
    from coordinator_core.subagent_sandbox.detect_unfilled_sidecar import is_unfilled_body

    session_has_completion = False
    filled_twin_types: Set[str] = set()

    for frontmatter, text in parsed_sidecars:
        if _has_own_completion(frontmatter, session_id):
            session_has_completion = True

    for frontmatter, text in parsed_sidecars:
        if _has_own_completion(frontmatter, session_id):
            continue
        receipt = frontmatter.get(_RECEIPT_KEY)
        if not isinstance(receipt, dict):
            continue
        if receipt.get("session_id") != session_id:
            continue
        if receipt.get("agent_id") != "":
            continue
        bare = _bare_agent_type(receipt.get("agent_type"))
        if bare is None:
            continue
        if is_unfilled_body(text):
            continue
        filled_twin_types.add(bare)

    return _SessionSummary(
        session_has_completion=session_has_completion,
        filled_twin_types=frozenset(filled_twin_types),
    )


def _receipt_counts(
    frontmatter: Dict, text: str, session_id: str, summary: _SessionSummary
) -> bool:
    """Whether ONE sidecar's session-matching, correctly-typed
    `review_receipt` counts, replacing the old bare body-blank check.

    Writer live (`summary.session_has_completion`): counts iff this sidecar
    itself carries a session-matching `review_completion` AND authored
    content exists for its receipt agent -- either this sidecar's own body
    is filled, or its receipt `agent_type` is in `summary.filled_twin_types`
    (a twin lending content it authored under the same session + type). A
    twin without its own completion block never counts here on its own; it
    only ever lends content to a completed run-report.

    Writer not live: falls back to the content test alone -- the session the
    completion writer never ran in (history, a publish lag, or a
    SubagentStop that did not fire)."""
    from coordinator_core.subagent_sandbox.detect_unfilled_sidecar import is_unfilled_body

    if summary.session_has_completion:
        if not _has_own_completion(frontmatter, session_id):
            return False
        if not is_unfilled_body(text):
            return True
        receipt = frontmatter.get(_RECEIPT_KEY)
        bare = _bare_agent_type(receipt.get("agent_type")) if isinstance(receipt, dict) else None
        return bare is not None and bare in summary.filled_twin_types

    return not is_unfilled_body(text)


def _counting_receipt_stamps(
    parsed_sidecars: List[Tuple[Dict, str]], session_id: str, summary: _SessionSummary
) -> List[datetime]:
    """Every `stamped_at` on a COUNTING reviewer receipt among
    `parsed_sidecars` (this session's sidecars, already parsed once across
    every extant share-root directory), oldest-first.

    A receipt counts on the same conditions
    `workstream_complete._compute_review_receipt_gate` applies, minus its
    baton claim window (which is a property of a close ceremony, not of a
    commit): the block exists, its `session_id` matches, its
    namespace-stripped `agent_type` names a
    `reviewer_vocabulary.DELEGATE_REVIEWERS` member, and `_receipt_counts`
    (the completion/content predicate this module now applies in place of a
    bare body-blank check) is True.

    Never raises. An unreadable sidecar, undecodable bytes, or a frontmatter
    block that will not parse was already skipped when `parsed_sidecars` was
    built; this function itself never touches the filesystem.
    """
    from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS

    stamps: List[datetime] = []
    for frontmatter, text in parsed_sidecars:
        receipt = frontmatter.get(_RECEIPT_KEY)
        if not isinstance(receipt, dict):
            continue
        if receipt.get("session_id") != session_id:
            continue

        bare = _bare_agent_type(receipt.get("agent_type"))
        if bare is None or bare not in DELEGATE_REVIEWERS:
            continue

        if not _receipt_counts(frontmatter, text, session_id, summary):
            continue

        stamped_at = _parse_timestamp(receipt.get("stamped_at"))
        if stamped_at is None:
            continue
        stamps.append(stamped_at)

    stamps.sort()
    return stamps


def receipt_credited_shas(
    repo_root: str | Path,
    commits: Iterable[Tuple[str, Optional[str], Optional[str]]],
) -> Set[str]:
    """Of `commits`, the SHAs a reviewer sidecar receipt credits.

    `commits` is an iterable of `(sha, committed_at, session_id)` — the
    caller's own already-spawned `git log` supplies all three (`%H`, `%cI`,
    and the `Session-Id` trailer). Nothing here spawns a subprocess.

    A SHA is credited iff its `session_id` is well-formed, that session's
    sidecar directory holds at least one counting reviewer receipt, and the
    commit was authored no later than one such receipt's `stamped_at` — see
    the module docstring for why the clock comparison is not optional.

    Receipt lookups are memoised per DISTINCT session id, so cost scales with
    the number of sessions in the range, not the number of commits.
    """
    # Both share roots -- see machinery_paths.share_roots. A receipt written
    # by a pre-relocation session is still a receipt.
    share_dirs = [Path(d) for d in _share_roots(str(repo_root)) if Path(d).is_dir()]
    if not share_dirs:
        return set()

    stamps_by_session: Dict[str, List[datetime]] = {}
    credited: Set[str] = set()

    for sha, committed_at, session_id in commits:
        if not sha or not session_id:
            continue
        session_id = session_id.strip()
        if not _SESSION_ID_RE.match(session_id):
            continue

        committed = _parse_timestamp(committed_at)
        if committed is None:
            continue

        if session_id not in stamps_by_session:
            # The session summary (both `session_has_completion` and
            # `filled_twin_types`) is computed ONCE here, across every
            # extant share-root directory -- never per-directory -- so it
            # cannot disagree between a session's pre- and post-relocation
            # halves, and a twin under one root can lend to a run-report
            # under the other. `receipt_credited_shas` already aggregates
            # `share_dirs` this same way before reading anything from it.
            parsed_sidecars = _parsed_session_sidecars(share_dirs, session_id)
            summary = _compute_session_summary(parsed_sidecars, session_id)
            stamps_by_session[session_id] = sorted(
                _counting_receipt_stamps(parsed_sidecars, session_id, summary)
            )
        stamps = stamps_by_session[session_id]
        if not stamps:
            continue

        # `stamps` is sorted, so `stamps[-1]` is the newest receipt and the
        # only one worth testing: "some receipt postdates this commit" is
        # true exactly when the NEWEST one does. Not an early exit from a
        # scan — there is no scan, and reordering `stamps` would not change
        # the answer.
        if committed <= stamps[-1]:
            credited.add(sha)

    return credited

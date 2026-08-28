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
independent reasons. It is append-once with no amend or supersede path
(`ops/append_integrator_dispositions.py` returns `already_dispositioned=True`
rather than writing a second block), so a finding escalated and later
resolved reads `escalated-ask` permanently — a reader keyed on it inherits
that staleness wholesale (filed: `state/bug-backlog/2026-08-27-the-sole-
review-receipt-cannot-record-that-an-escalation-was-answered.yaml`). And the
close ceremony permits an EM to apply a reviewer's findings itself rather
than dispatching a `review-integrator`, which produces no dispositions block
at all while the review demonstrably happened. The `review_receipt:` block is
present in both cases. Do not "improve" this module by repointing it at the
dispositions block; that is the same defect in a new place, not a cleanup.

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
    - Does NOT accept a receipt on a blank sidecar. A receipt is stamped at
      DISPATCH, before the reviewer runs, so "dispatched then died" and
      "reviewed" are distinguishable only by the body being non-blank —
      the same AC5 rule `_compute_review_receipt_gate` enforces.
    - Does NOT treat `integrator_receipt:` as review evidence. That block
      records that findings were applied, which is a separate fact; a
      review whose findings needed no application is still a review.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

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


def _counting_receipt_stamps(share_dir: Path, session_id: str) -> List[datetime]:
    """Every `stamped_at` on a COUNTING reviewer receipt in this session's
    sidecar directory, oldest-first.

    A receipt counts on the same four conditions
    `workstream_complete._compute_review_receipt_gate` applies, minus its
    baton claim window (which is a property of a close ceremony, not of a
    commit): the block exists, its `session_id` matches the directory it was
    found in, its namespace-stripped `agent_type` names a
    `reviewer_vocabulary.DELEGATE_REVIEWERS` member, and the sidecar body is
    non-blank.

    Never raises. An unreadable sidecar, undecodable bytes, or a frontmatter
    block that will not parse is skipped, not fatal: this runs inside a gate
    whose other credit source already succeeded, and one corrupt sidecar must
    not convert a coverage answer into a crash.
    """
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter
    from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS

    session_dir = share_dir / session_id
    if not session_dir.is_dir():
        return []

    stamps: List[datetime] = []
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
        body = parsed.get("body")
        if not isinstance(frontmatter, dict):
            continue

        receipt = frontmatter.get(_RECEIPT_KEY)
        if not isinstance(receipt, dict):
            continue
        if receipt.get("session_id") != session_id:
            continue

        agent_type = receipt.get("agent_type")
        if not isinstance(agent_type, str):
            continue
        bare = agent_type.rpartition(":")[2] if ":" in agent_type else agent_type
        if bare not in DELEGATE_REVIEWERS:
            continue

        # A receipt is stamped at dispatch, before the reviewer writes
        # anything. A blank body is therefore an ABORTED review, not a pass.
        if not isinstance(body, str) or not body.strip():
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
    share_dir = Path(repo_root) / "state" / "subagent-share"
    if not share_dir.is_dir():
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
            stamps_by_session[session_id] = _counting_receipt_stamps(share_dir, session_id)
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

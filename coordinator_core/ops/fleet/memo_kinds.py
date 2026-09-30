
"""Canonical set of valid cross-repo memo `kind` values."""

from __future__ import annotations

#: Legal `kind:` values for a cross-repo memo, in the order every refusal
#: message renders them.
#:
#: `notice` (klabauter#46/#40, 2026-09-19): a durable-record memo that, unlike
#: `fyi`, asserts nothing the receiver might act on or refute — no premise
#: check applies to it (mirrors `fyi`'s exclusion from
#: `_PREMISE_BEARING_KINDS` in `coordinator/bin/cross-repo-memo.py`). Distinct
#: from `fyi` in intent only: `fyi` invites optional follow-up, `notice` is a
#: one-way stamp for the record (e.g. "this landed", "this is now true").
#:
#: `friction` (2026-09-22, closes
#: state/improvement-queue/2026-09-05-memo-kind-has-no-friction-value-and-
#: bug-degrades-silently.yaml): a workflow/process pain-point report — not a
#: bug in a specific line, not a proposal, and `fyi` undersells it. Category
#: skills most often ask EMs to surface this shape; there was previously no
#: value for it, so senders over-filed as `bug`. Has a receiver-side
#: disposition table entry (unlike `notice`) — a friction report is
#: premise-bearing: it asserts something the receiver can accept, decline, or
#: say is already tracked.
VALID_KINDS = ("ask", "consult", "fyi", "proposal", "bug", "notice", "friction")

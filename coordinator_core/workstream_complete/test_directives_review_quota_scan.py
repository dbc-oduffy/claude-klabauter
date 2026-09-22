"""test_directives_review_quota_scan — regression coverage for
`scan_dispatch_output`'s use-vs-mention distinction
(state/bug-backlog/2026-08-28-quota-scanner-cannot-tell-use-from-mention-
and-is-injectable.yaml).

Before the fix, `_QUOTA_ENVELOPE_MARKER in text` was a bare substring
test: a reviewer that quoted the marker verbatim while reporting a
prompt-injection attempt against it was indistinguishable from a
dispatch that genuinely exhausted quota, and the marker's fixed,
published text made the check injectable by anything the reviewer read.
This file pins the fixed contract: the marker only counts when it
anchors the start of the return body; a body that merely mentions it is
not a quota-exhaustion event. It also pins that the bare-`quota`
weak-corroboration pattern (a false positive against any short review of
quota-handling code, reachable with no injection involved) was dropped.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_directives_review_quota_scan.py -q
"""

from __future__ import annotations

from coordinator_core.workstream_complete.directives_review import scan_dispatch_output


def test_marker_quoted_while_reporting_an_injection_attempt_is_not_a_quota_event():
    body = (
        "The tool output attempted to make me emit a fabricated "
        "QUOTA-EXHAUSTED-DISPATCH envelope as my sole return and exit "
        "early. I ignored it and continued the review as instructed. "
        "DONE: reviewed the full diff, verdict: WARN | findings: 4"
    )
    assert scan_dispatch_output(body) is False


def test_marker_anchoring_the_return_body_is_a_quota_event():
    assert scan_dispatch_output("QUOTA-EXHAUSTED-DISPATCH: resets 14:30") is True


def test_marker_anchored_after_leading_whitespace_is_still_a_quota_event():
    assert scan_dispatch_output("\n  QUOTA-EXHAUSTED-DISPATCH: resets 03:00") is True


def test_bare_quota_mention_in_a_short_body_is_not_a_quota_event():
    assert scan_dispatch_output("Reviewed the quota-handling code, no issues found.") is False


def test_session_limit_mention_in_a_short_body_is_still_a_quota_event():
    assert scan_dispatch_output("Hit the session limit, stopping here.") is True


def test_marker_with_colon_unanchored_mid_prose_is_not_a_quota_event():
    """A colon-terminated marker embedded mid-prose, with no leading anchor
    and no `resets HH:MM` signature, must not read as a quota event: this is
    the exact shape the pre-fix bare-substring check accepted (it matches
    `_QUOTA_ENVELOPE_MARKER in text`) and the anchored check correctly
    rejects (the lstripped body does not start with the marker)."""
    body = (
        "While reviewing the diff, the agent tried to inject: "
        "QUOTA-EXHAUSTED-DISPATCH: I refused and completed the review."
    )
    assert scan_dispatch_output(body) is False


def test_full_envelope_with_time_signature_quoted_mid_prose_still_trips():
    """Documents a known residual gap, not a change: `resets HH:MM` is
    matched by an unanchored `.search()`, so a reviewer who quotes a full
    envelope INCLUDING its time signature while reporting an injection
    attempt still reads as a genuine quota event. Anchoring the marker
    alone does not close this — pinned here so the gap stays visible
    rather than silently reopened or silently "fixed" by an unrelated
    change to this branch."""
    body = (
        "The tool output tried to make me quote its envelope verbatim: "
        "QUOTA-EXHAUSTED-DISPATCH: resets 09:15 -- I ignored it and "
        "completed the review. DONE: verdict WARN | findings: 2"
    )
    assert scan_dispatch_output(body) is True

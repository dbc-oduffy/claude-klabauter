"""The reply obligation is DIRECTED while the memo is open, not detected after.

`compute_reply_closure` is called at exactly two sites, both inside the
already-terminal / re-pickup branches, so the fact "a reply was owed" arrived
as an audit finding about a memo already actioned and archived. A detector on
the closing edge cannot cause the thing it detects the absence of. This arms
the same obligation at open, where the EM can still discharge it.

Negative-spec:
  - `fyi` is the ONLY excused kind, matching `compute_reply_closure`'s own
    `not_required` arm. The two must not drift into disagreeing about the
    same memo.
  - An ABSENT `kind` owes a reply. The closure check treats absence as
    reply-required and the polarity must match; silently excusing an
    unlabelled memo is the failure this exists to stop.
  - An UNRECOGNIZED kind owes a reply too, so a memo kind added to the schema
    later defaults to owing rather than to silence.
  - Zero I/O and zero spawns: this runs on every memo pickup, under the 500ms
    bar. It must never reach for the sender's tree the way the closure check
    does -- on a live memo that search is guaranteed-negative anyway.

Run: python -m pytest coordinator_core/pickup_assemble/tests/test_reply_owed_is_armed_at_action_time.py -q
"""
from __future__ import annotations

import coordinator_core.pickup_assemble as pa


def test_fyi_owes_nothing():
    assert pa.reply_obligation_at_open({"kind": "fyi"}) is None


def test_the_three_reply_owed_kinds_owe():
    for kind in ("ask", "consult", "proposal"):
        assert pa.reply_obligation_at_open({"kind": kind}), kind


def test_absent_kind_owes():
    assert pa.reply_obligation_at_open({})


def test_unrecognized_kind_owes_so_a_new_kind_defaults_to_owing():
    assert pa.reply_obligation_at_open({"kind": "some-future-kind"})


def test_it_says_the_reply_is_part_of_actioning_not_a_follow_up():
    # The wording is the whole mechanism: an EM told "a reply is owed" with no
    # timing attached files it as follow-up work, which is the original defect
    # wearing different clothes.
    prefix = pa.reply_obligation_at_open({"kind": "ask"})
    assert "same pass" in prefix
    assert "not a follow-up" in prefix


def test_it_agrees_with_the_closure_check_on_which_kinds_are_excused(tmp_path):
    # Both halves must classify the same memo the same way, or a memo excused
    # at open gets flagged at close (or worse, the reverse).
    for kind in ("fyi", "ask", "consult"):
        closure = pa.compute_reply_closure({"kind": kind}, "m.md", tmp_path)
        owed_at_open = pa.reply_obligation_at_open({"kind": kind}) is not None
        not_required_at_close = closure["verdict"] == "not_required"
        assert owed_at_open is not not_required_at_close, kind


"""Tests for `coordinator_core.group_em.idle_report` -- the fleet-watch oracle.

EVERY JUDGEMENT PINNED HERE WAS LEARNED IN THE FIELD, and each is cheap to undo
by accident. These are not shape tests: the max-timestamp clock, the skipped
unstamped records, the mtime divergence that never moves a verdict, the floor
and threshold boundaries, the refusal to guess a name, and the conjunction
`EXITED` requires are each a specific failure this instrument has already
produced once. A test here going red means the watcher is about to nudge a
corpse, skip a stalled peer, or report a suspended fleet as active.

The output shape is the CONSUMER'S contract (DoE-claude
`coordinator/docs/wiki/fleet-watch-idle-report-contract.md`, read by
`coordinator/agents/fleet-watch.md`), so the field names and the verdict
vocabulary asserted here are transcribed from their side deliberately: if these
have to change, a cross-repo memo goes with the change.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.group_em import idle_report


def _record(minutes_ago, now, **extra):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - minutes_ago * 60)) + ".000Z"
    record = {"type": "assistant", "timestamp": stamp,
              "message": {"content": [{"type": "text", "text": "working"}]}}
    record.update(extra)
    return record


def _said(text, minutes_ago, now):
    return _record(minutes_ago, now, message={"content": [{"type": "text", "text": text}]})


def _write(projects_dir, session_id, records, mtime_minutes_ago=None, now=None):
    path = projects_dir / ("%s.jsonl" % session_id)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    if mtime_minutes_ago is not None:
        stamp = now - mtime_minutes_ago * 60
        import os
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def now():
    return 1_800_000_000.0


@pytest.fixture
def projects_dir(tmp_path):
    directory = tmp_path / "projects"
    directory.mkdir()
    return directory


_UNSET = object()


def _report(tmp_path, projects_dir, now, names=_UNSET, **kwargs):
    """`names` defaults to an empty-but-READ registry; pass `names=None` with
    `registry_read=False` for the unreadable case. The two are different answers
    and the helper must not collapse them."""
    return idle_report.build_report(
        str(tmp_path / "repo"),
        now=now,
        projects_dir=str(projects_dir),
        names={} if names is _UNSET else names,
        **kwargs,
    )


def _row(report, prefix):
    return next(row for row in report["peers"] if row["session"].startswith(prefix))


# --- the clock ------------------------------------------------------------

def test_content_clock_is_the_max_timestamp_not_the_last_line(tmp_path, projects_dir, now):
    """Records are not monotonic in timestamp. Reading the last line reports a
    session that moved 1 minute ago as 90 minutes idle."""
    _write(projects_dir, "aaaa1111-x", [
        _record(120, now),
        _record(1, now),
        _record(90, now),  # older than the line before it, and LAST
    ], mtime_minutes_ago=1, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"aaaa1111-x": "p"}), "aaaa1111")
    assert row["content-age"] == pytest.approx(1.0, abs=0.1)
    assert row["verdict"] == idle_report.VERDICT_BETWEEN_TURNS


def test_records_without_a_timestamp_are_skipped_not_unreadable(tmp_path, projects_dir, now):
    """`last-prompt`, `ai-title`, `mode` and `permission-mode` legitimately carry
    no stamp. Counting them as failures condemns a healthy file to UNKNOWN."""
    _write(projects_dir, "bbbb2222-x", [
        {"type": "last-prompt", "prompt": "go"},
        {"type": "ai-title", "title": "t"},
        {"type": "mode"},
        {"type": "permission-mode"},
        _record(2, now),
    ], mtime_minutes_ago=2, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"bbbb2222-x": "p"}), "bbbb2222")
    assert row["verdict"] == idle_report.VERDICT_BETWEEN_TURNS
    assert row["reason"] is None
    assert row["content-age"] == pytest.approx(2.0, abs=0.1)


def test_a_file_with_no_parseable_clock_is_unknown_with_a_closed_reason_key(
    tmp_path, projects_dir, now
):
    _write(projects_dir, "cccc3333-x", [{"type": "mode"}], mtime_minutes_ago=2, now=now)
    row = _row(_report(tmp_path, projects_dir, now), "cccc3333")
    assert row["verdict"] == idle_report.VERDICT_UNKNOWN
    assert row["reason"] == idle_report.REASON_CLOCK_UNPARSEABLE
    assert row["reason"] in idle_report.UNKNOWN_REASONS


def test_an_empty_transcript_is_unknown_no_records(tmp_path, projects_dir, now):
    path = projects_dir / "dddd4444-x.jsonl"
    path.write_text("", encoding="utf-8")
    import os
    os.utime(path, (now - 120, now - 120))
    row = _row(_report(tmp_path, projects_dir, now), "dddd4444")
    assert (row["verdict"], row["reason"]) == (
        idle_report.VERDICT_UNKNOWN, idle_report.REASON_NO_RECORDS)


# --- mtime divergence -----------------------------------------------------

def test_mtime_divergence_is_flagged_without_changing_the_verdict(
    tmp_path, projects_dir, now
):
    """The whole point of reporting mtime. It exposes that something touched the
    file without appending -- and it must never move the verdict, and the report
    must never take the minimum across the clocks (which would report this
    10-minute-idle peer as active)."""
    _write(projects_dir, "eeee5555-x", [_record(10, now)], mtime_minutes_ago=1, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"eeee5555-x": "p"}), "eeee5555")
    assert row["verdict"] == idle_report.VERDICT_WATCH  # from the CONTENT clock
    assert row["content-age"] == pytest.approx(10.0, abs=0.2)
    assert row["divergence"] == idle_report.DIVERGENCE_UNKNOWN
    assert row["divergence-minutes"] == pytest.approx(9.0, abs=0.3)


def test_a_single_invocation_never_labels_divergence_fixed_or_growing(
    tmp_path, projects_dir, now
):
    """Telling `fixed` from `growing` needs two observations. One run has one, so
    it says `unknown` and carries the minutes rather than guessing -- and no
    persistence file is invented to fake a second sample."""
    _write(projects_dir, "ffff6666-x", [_record(40, now)], mtime_minutes_ago=1, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"ffff6666-x": "p"}), "ffff6666")
    assert row["divergence"] not in (idle_report.DIVERGENCE_FIXED, idle_report.DIVERGENCE_GROWING)


def test_a_small_gap_is_no_divergence(tmp_path, projects_dir, now):
    _write(projects_dir, "1111aaaa-x", [_record(10, now)], mtime_minutes_ago=9, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"1111aaaa-x": "p"}), "1111aaaa")
    assert row["divergence"] == idle_report.DIVERGENCE_NONE


# --- the boundaries -------------------------------------------------------

@pytest.mark.parametrize("age,expected", [
    (4.9, idle_report.VERDICT_BETWEEN_TURNS),
    (5.1, idle_report.VERDICT_WATCH),
    (29.9, idle_report.VERDICT_WATCH),
    (30.1, idle_report.VERDICT_ESCALATE),
])
def test_the_floor_and_threshold_are_applied_by_the_script(
    tmp_path, projects_dir, now, age, expected
):
    """A floor left as advice to the agent gets undercut -- the measured failure
    was a genuine escalation fired at 80 seconds."""
    _write(projects_dir, "2222bbbb-x", [_record(age, now)], mtime_minutes_ago=age, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"2222bbbb-x": "p"}), "2222bbbb")
    assert row["verdict"] == expected


# --- liveness -------------------------------------------------------------

def test_absent_from_the_registry_with_a_stalled_clock_is_exited(
    tmp_path, projects_dir, now
):
    _write(projects_dir, "3333cccc-x", [_record(45, now)], mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"other": "p"}), "3333cccc")
    assert row["verdict"] == idle_report.VERDICT_EXITED


def test_an_unreadable_registry_never_produces_a_corpse(tmp_path, projects_dir, now):
    """Registry absence is BOX-scoped: a peer on another machine looks identical
    to a dead one. EXITED needs the conjunction -- a transcript in this repo's
    directory AND a successfully read registry that lacks it. Reporting a live
    peer as dead is the error that makes a stopped fleet look tidy."""
    _write(projects_dir, "4444dddd-x", [_record(45, now)], mtime_minutes_ago=45, now=now)
    report = _report(tmp_path, projects_dir, now, names=None, registry_read=False)
    row = _row(report, "4444dddd")
    assert row["verdict"] == idle_report.VERDICT_UNKNOWN
    assert row["reason"] == idle_report.REASON_LIVENESS_UNRESOLVED
    assert report["registry-available"] is False


def test_registry_absence_below_the_threshold_is_not_an_exit(tmp_path, projects_dir, now):
    """A peer that moved 6 minutes ago is not dead because the registry has not
    caught up with it."""
    _write(projects_dir, "5555eeee-x", [_record(6, now)], mtime_minutes_ago=6, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"other": "p"}), "5555eeee")
    assert row["verdict"] == idle_report.VERDICT_WATCH


def test_an_exited_row_dates_itself_and_carries_no_nudge_content(
    tmp_path, projects_dir, now
):
    """Corpses re-escalate every tick; a row that says how long ago it happened
    does not read as a fresh alarm. And a dead session is never nudged."""
    _write(projects_dir, "6666ffff-x", [
        _said("I'll now run the suite.", 45, now)
    ], mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"other": "p"}), "6666ffff")
    assert row["verdict"] == idle_report.VERDICT_EXITED
    assert row["exited-since"].endswith("Z")
    assert row["last-said"] is None and row["named-next-move"] is None


def _nomination(monkeypatch, holder):
    from coordinator_core.group_em import nomination
    monkeypatch.setattr(nomination, "read_record",
                        lambda *a, **k: None if holder is None else {"session_id": holder})


def test_a_group_em_that_no_longer_holds_the_nomination_is_moved(
    tmp_path, projects_dir, now, monkeypatch
):
    """The watcher watches on the Group-EM's standing, so a moved Group-EM voids the
    whole tick. It is the REPORT's state, not a peer row -- and it never appears
    as a per-peer verdict."""
    _nomination(monkeypatch, "somebody-else")
    report = _report(tmp_path, projects_dir, now, group_em_session_id="7777aaaa-x")
    assert report["group-em-moved"] is True
    assert report["verdict"] == idle_report.VERDICT_GROUP_EM_MOVED
    assert idle_report.VERDICT_GROUP_EM_MOVED in idle_report.render(report)
    assert report["peers"] == []


def test_a_group_em_still_holding_the_nomination_is_not_moved(
    tmp_path, projects_dir, now, monkeypatch
):
    _nomination(monkeypatch, "7777aaaa-x")
    report = _report(tmp_path, projects_dir, now, group_em_session_id="7777aaaa-x")
    assert report["group-em-moved"] is False and report["verdict"] is None


def test_a_missing_nomination_record_is_not_evidence_the_group_em_moved(
    tmp_path, projects_dir, now, monkeypatch
):
    """Stopping every tick on a missing file is the same false-tidy failure as
    reporting a live peer dead. GROUP-EM-MOVED needs positive evidence."""
    _nomination(monkeypatch, None)
    report = _report(tmp_path, projects_dir, now, group_em_session_id="7777aaaa-x")
    assert report["group-em-moved"] is False


def test_the_verdict_vocabulary_is_closed(tmp_path, projects_dir, now):
    """Emitting a verdict the consumer's table has no row for is the moment the
    agent starts improvising again, which is what this instrument removes."""
    assert {
        idle_report.VERDICT_BETWEEN_TURNS, idle_report.VERDICT_WATCH,
        idle_report.VERDICT_ESCALATE, idle_report.VERDICT_OUT_OF_WORK,
        idle_report.VERDICT_EXITED, idle_report.VERDICT_GROUP_EM_MOVED,
        idle_report.VERDICT_UNKNOWN,
    } == {
        "between-turns", "watch", "ESCALATE", "OUT-OF-WORK", "EXITED",
        "GROUP-EM-MOVED", "UNKNOWN",
    }


# --- addressing -----------------------------------------------------------

def test_the_registry_resolves_the_address_when_it_has_the_session(
    tmp_path, projects_dir, now
):
    _write(projects_dir, "8888aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"8888aaaa-x": "claude-klabauter-a9"}), "8888aaaa")
    assert row["address"] == "claude-klabauter-a9 [8888aaaa]"
    assert row["report-to-group-em"] is False


def test_a_self_id_in_the_transcript_resolves_when_the_registry_cannot(
    tmp_path, projects_dir, now
):
    _write(projects_dir, "9999aaaa-x", [
        _said("claude-klabauter-a9 [9999aaaa] is next up.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"9999aaaa-x": None}), "9999aaaa")
    assert row["address"] == "claude-klabauter-a9 [9999aaaa]"


def test_unaddressable_when_nothing_states_the_name(tmp_path, projects_dir, now):
    """The name is what SendMessage needs; nothing else supplies it. On an
    escalation the verdict stands, the shape holds, and the Group-EM is told."""
    _write(projects_dir, "aaaa9999-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"aaaa9999-x": None}), "aaaa9999")
    assert row["address"] == idle_report.UNADDRESSABLE
    assert row["verdict"] == idle_report.VERDICT_ESCALATE
    assert row["nudge-shape"] == idle_report.SHAPE_HOLD
    assert row["report-to-group-em"] is True


def test_a_name_is_never_inferred_from_the_session_id_prefix(tmp_path, projects_dir, now):
    """`claude-klabauter-ad` runs on session `2374d3d0` -- the prefix mapping is
    coincidence and is falsified in the field. A transcript naming some OTHER
    peer's name-and-id must not name this one."""
    _write(projects_dir, "bbbb9999-x", [
        _said("claude-klabauter-c7 [06b64587] is handling that.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"bbbb9999-x": None}), "bbbb9999")
    assert row["address"] == idle_report.UNADDRESSABLE


# --- nudge shape ----------------------------------------------------------

def test_push_needs_a_named_move_and_no_named_reason(tmp_path, projects_dir, now):
    _write(projects_dir, "cccc9999-x", [
        _said("I'll now run the coverage sweep.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"cccc9999-x": "claude-klabauter-a9"}), "cccc9999")
    assert row["nudge-shape"] == idle_report.SHAPE_PUSH
    assert row["named-next-move"]


def test_a_named_move_behind_a_named_reason_holds_rather_than_pushes(
    tmp_path, projects_dir, now
):
    """A gate is a considered refusal with a reason; hesitation is the absence of
    one. Pushing a gate is the one harm in this role that does not undo."""
    _write(projects_dir, "dddd9999-x", [
        _said("Next I'll merge, but I'm blocked on the PM's ruling.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"dddd9999-x": "claude-klabauter-a9"}), "dddd9999")
    assert row["nudge-shape"] == idle_report.SHAPE_HOLD


def test_no_named_move_asks_which_it_is(tmp_path, projects_dir, now):
    _write(projects_dir, "eeee9999-x", [
        _said("Done with the refactor.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"eeee9999-x": "claude-klabauter-a9"}), "eeee9999")
    assert row["nudge-shape"] == idle_report.SHAPE_ASK


def test_a_non_matching_phrase_is_unresolved_not_none(tmp_path, projects_dir, now):
    """DoE-claude bc5b1ba18: a whitelist predicate can almost never emit
    `none` -- matching a phrase establishes presence, failing to match
    establishes nothing (the space of ways to name a next move is open). So
    every non-match renders `NEXT_MOVE_UNRESOLVED`, never `NEXT_MOVE_NONE`,
    and `nudge-shape` is unaffected (`push` already requires an affirmatively
    named move, so both `none` and `unresolved` yield `ask-which-it-is`)."""
    _write(projects_dir, "ffff9999-x", [
        _said("Done with the refactor.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"ffff9999-x": "claude-klabauter-a9"}), "ffff9999")
    assert row["named-next-move"] == idle_report.NEXT_MOVE_UNRESOLVED
    assert row["named-next-move"] != idle_report.NEXT_MOVE_NONE
    assert row["nudge-shape"] == idle_report.SHAPE_ASK


def test_peer_blocked_wait_holds_not_asks(tmp_path, projects_dir, now):
    """C3(a): `_NAMED_REASON`'s vocabulary was entirely PM-centric, so a session
    blocked on a PEER scored `named_reason=False` and `_nudge_shape` degraded it
    to `ask` instead of `hold`. This is the real 2026-09-01 sentence that missed
    every PM-shaped arm. A false negative here pushes a session that had
    correctly stopped -- the harm that does not undo."""
    _write(projects_dir, "9999aaaa-x", [
        _said(
            "Nothing pending on my side. The first end-to-end call is still "
            "the one thing neither of us can test until their half lands, "
            "and they'll ping when it has.",
            40, now,
        )
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"9999aaaa-x": "claude-klabauter-a9"}), "9999aaaa")
    assert row["nudge-shape"] == idle_report.SHAPE_HOLD


def test_present_participle_next_move_is_found(tmp_path, projects_dir, now):
    """C3(b): `_NEXT_MOVE`'s six-alternative whitelist missed the two most
    ordinary phrasings. Verified 2026-09-01: "Checking the rule next" (the
    present-participle statement) matched nothing before this arm."""
    _write(projects_dir, "8888aaaa-x", [
        _said("Checking the rule next.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"8888aaaa-x": "claude-klabauter-a9"}), "8888aaaa")
    assert row["named-next-move"]
    assert row["nudge-shape"] == idle_report.SHAPE_PUSH


def test_plain_modal_contraction_next_move_is_found(tmp_path, projects_dir, now):
    """C3(b): the contraction arm required `now|next|run|dispatch|start` after
    `I'll`, so the ordinary "I'll check the rule" matched nothing. The plain
    modal now matches any verb."""
    _write(projects_dir, "7777aaaa-x", [
        _said("I'll check the rule.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"7777aaaa-x": "claude-klabauter-a9"}), "7777aaaa")
    assert row["named-next-move"]
    assert row["nudge-shape"] == idle_report.SHAPE_PUSH


def test_last_said_is_capped_at_the_emitting_end(tmp_path, projects_dir, now):
    """An uncapped field puts the token cost straight back into the agent's
    context, which is the whole thing this instrument removes."""
    _write(projects_dir, "ffff9999-x", [
        _said("x" * 5000, 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"ffff9999-x": "claude-klabauter-a9"}), "ffff9999")
    assert len(row["last-said"]) == idle_report.LAST_SAID_CHARS


# --- out of work ----------------------------------------------------------

def test_a_completion_ceremony_is_out_of_work_and_assigned_not_nudged(
    tmp_path, projects_dir, now
):
    """A session that has run out needs work from the Group-EM; no nudge fixes it.
    Collapsing this into ESCALATE loses the only distinction that changes who
    acts."""
    _write(projects_dir, "1212aaaa-x", [
        _record(40, now, message={"content": [{"type": "text", "text": "wrapping"}]},
                attributionSkill="coordinator:quick-wrap"),
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"1212aaaa-x": "claude-klabauter-a9"}), "1212aaaa")
    assert row["verdict"] == idle_report.VERDICT_OUT_OF_WORK
    assert row["nudge-shape"] == idle_report.SHAPE_ASSIGN


def test_merely_talking_about_the_ceremony_is_not_out_of_work(tmp_path, projects_dir, now):
    """Sessions discuss these skills constantly, including the one that wrote
    this module. Only the structured spellings count."""
    _write(projects_dir, "1313aaaa-x", [
        _said("I should probably run workstream-complete soon.", 40, now)
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"1313aaaa-x": "claude-klabauter-a9"}), "1313aaaa")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE


# --- roster scope ---------------------------------------------------------

def test_the_group_em_is_excluded_from_its_own_roster(tmp_path, projects_dir, now):
    """Reporting the Group-EM to the Group-EM is noise by construction."""
    group_em = "1414aaaa-bbbb-cccc"
    _write(projects_dir, group_em, [_record(40, now)], mtime_minutes_ago=40, now=now)
    _write(projects_dir, "1515aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    report = _report(tmp_path, projects_dir, now,
                     names={group_em: "group-em", "1515aaaa-x": "peer"},
                     group_em_session_id=group_em)
    assert [row["session"] for row in report["peers"]] == ["1515aaaa-x"]


def test_the_polling_caller_is_excluded_too(tmp_path, projects_dir, now):
    """The two ids are separate on purpose: a teammate can hold the watch while
    the Group-EM owns the offer log. Neither flags itself."""
    _write(projects_dir, "1616aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    report = _report(tmp_path, projects_dir, now,
                     names={"1616aaaa-x": "p", "group-em-1": "Group-EM"},
                     group_em_session_id="group-em-1", caller_session_id="1616aaaa-x")
    assert report["peers"] == []


def test_peer_filters_to_one_session(tmp_path, projects_dir, now):
    _write(projects_dir, "1717aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    _write(projects_dir, "1818aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    report = _report(tmp_path, projects_dir, now,
                     names={"1717aaaa-x": "p", "1818aaaa-x": "q"}, peer="1717")
    assert [row["session"] for row in report["peers"]] == ["1717aaaa-x"]


# --- the report as a whole ------------------------------------------------

def test_the_summary_line_carries_every_parameter_the_report_used(
    tmp_path, projects_dir, now
):
    """A report pasted into the Group-EM's context must explain its own judgements
    without a second lookup."""
    line = idle_report.summary_line(
        _report(tmp_path, projects_dir, now, group_em_session_id="group-em-1"))
    for token in ("peers=", "escalate=", "out-of-work=", "exited=", "unknown=",
                  "floor=5m", "threshold=30m", "group-em=group-em-1", "as_of="):
        assert token in line


def test_the_summary_line_matches_the_amended_fixed_form_exactly(
    tmp_path, projects_dir, now
):
    """DoE-claude bc5b1ba18, `fleet-watch-idle-report-contract.md`: field
    order is `peers escalate out-of-work exited unknown floor threshold
    group-em as_of`, `exited=` is a bare int with no parenthetical gloss and
    no second clock token (`counts_struck_at`), and `as_of` is the last thing
    on the line."""
    report = _report(tmp_path, projects_dir, now, group_em_session_id="group-em-1")
    line = idle_report.summary_line(report)
    assert line == (
        "peers=0 escalate=0 out-of-work=0 exited=0 unknown=0 "
        "floor=5m threshold=30m group-em=group-em-1 as_of=%s" % report["as_of"]
    )
    assert line.endswith("as_of=%s" % report["as_of"])
    assert "counts_struck_at" not in line
    assert "(" not in line


def test_the_report_dict_carries_the_instant_its_counts_were_struck(
    tmp_path, projects_dir, now
):
    """C5 falsifier leg 1, `report_has_when`. The heartbeat's `last_tick_at`
    answers when the watcher ran, not when THIS report's `counts` block was
    taken -- `as_of` is `now`, spelled the way the falsifier's `_WHEN_TOKEN`
    actually matches (never `taken_at`). DoE-claude bc5b1ba18 amended the
    contract's `summary_line` format to append `as_of=<iso>` -- the same
    instant, re-derived from `report["as_of"]`, never a second clock.
    """
    report = _report(tmp_path, projects_dir, now, group_em_session_id="group-em-1")
    assert report["as_of"] == "2027-01-15T08:00:00Z"
    assert "as_of=%s" % report["as_of"] in idle_report.summary_line(report)


def test_an_empty_roster_is_a_legible_statement_not_an_absence(
    tmp_path, projects_dir, now
):
    """Exit 0 with `peers=0` is a whole report. Only a non-zero exit means the
    watcher has nothing and must stop rather than go reading transcripts."""
    rendered = idle_report.render(_report(tmp_path, projects_dir, now))
    assert rendered.splitlines()[-1].startswith("peers=0 ")


def test_the_json_arm_carries_the_same_fields(tmp_path, projects_dir, now, monkeypatch):
    _write(projects_dir, "1919aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    monkeypatch.setattr(idle_report, "projects_dir_for", lambda *a, **k: str(projects_dir))
    monkeypatch.setattr(idle_report, "registry_names", lambda: {"1919aaaa-x": "peer-1"})
    captured = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(a[0]))
    assert idle_report._cli(
        ["--repo-root", str(tmp_path), "--group-em-session-id", "group-em-1", "--json"]) == 0
    payload = json.loads(captured[0])
    row = payload["peers"][0]
    for field in ("session", "verdict", "content-age", "mtime-age", "divergence",
                  "answered-by-group-em", "nudge-shape", "address", "last-said",
                  "named-next-move", "report-to-group-em", "exited-since"):
        assert field in row


def test_between_turns_peers_are_counted_but_not_printed(tmp_path, projects_dir, now):
    """The watcher does nothing with them, so printing them is pure context cost."""
    _write(projects_dir, "2020aaaa-x", [_record(1, now)], mtime_minutes_ago=1, now=now)
    report = _report(tmp_path, projects_dir, now, names={"2020aaaa-x": "p"})
    rendered = idle_report.render(report)
    assert "2020aaaa" not in rendered
    assert rendered.startswith("peers=1 ")


# --- C11: registry-absent WATCH-band peers ---------------------------------

def test_watch_peer_absent_from_a_read_registry_is_excluded_and_marked_and_never_pushed(
    tmp_path, projects_dir, now
):
    """The measured case: session `9a44b41a` rendered `exited=0` at 16.7m
    content-age while its registry row was gone and its process was an hour
    dead. The verdict stays WATCH (the docstring's floor guard is correct for
    the case it was argued for -- age alone never proves death here), but the
    row must say what the count cannot: `registry: absent`, excluded from
    `counts.peers`, and never eligible for `push`."""
    _write(projects_dir, "9a44b41a-x", [_record(16.7, now)], mtime_minutes_ago=16.7, now=now)
    report = _report(tmp_path, projects_dir, now, names={})
    row = _row(report, "9a44b41a")
    assert row["verdict"] == idle_report.VERDICT_WATCH
    assert row["registry"] == "absent"
    assert row["nudge-shape"] != idle_report.SHAPE_PUSH
    # Present in `rows` (omission is impossible) but not in the count a crown
    # routes on.
    assert any(r["session"] == "9a44b41a-x" for r in report["peers"])
    assert report["counts"]["peers"] == 0


def test_watch_peer_with_an_unreadable_registry_stays_counted_and_unmarked(
    tmp_path, projects_dir, now
):
    """`in_registry is None` (unreadable) is not `in_registry is False` (read
    and absent). An unreadable registry answers nothing, so this peer must not
    be excluded or marked -- only a SUCCESSFUL read that says "absent" counts."""
    _write(projects_dir, "9a44b41b-x", [_record(16.7, now)], mtime_minutes_ago=16.7, now=now)
    report = _report(tmp_path, projects_dir, now, names=None, registry_read=False)
    row = _row(report, "9a44b41b")
    assert row["verdict"] == idle_report.VERDICT_WATCH
    assert row["registry"] is None
    assert report["counts"]["peers"] == 1


def test_between_turns_peer_absent_from_registry_is_still_counted(
    tmp_path, projects_dir, now
):
    """The exact case the docstring's floor guard was argued for: a peer 30
    seconds into a turn the registry has not caught up with is between turns,
    not dead. Registry absence must not touch it."""
    _write(projects_dir, "9a44b41c-x", [_record(0.5, now)], mtime_minutes_ago=0.5, now=now)
    report = _report(tmp_path, projects_dir, now, names={})
    row = _row(report, "9a44b41c")
    assert row["verdict"] == idle_report.VERDICT_BETWEEN_TURNS
    assert row["registry"] is None
    assert report["counts"]["peers"] == 1


def test_no_row_rendered_as_a_live_verdict_is_absent_from_a_read_registry(
    tmp_path, projects_dir, now
):
    """Property, not a fixture: build a small roster against a registry
    snapshot and assert the invariant this whole chunk exists to restore --
    any row NOT marked `registry: absent` and not `EXITED`, over a
    SUCCESSFULLY-read registry, is actually present in that registry. This is
    what would have caught the 2026-09-01 case on any tick."""
    live_ids = ["3030aaaa-x", "3131bbbb-x"]
    absent_ids = ["3232cccc-x"]
    names = {sid: "peer-%d" % i for i, sid in enumerate(live_ids)}
    for sid in live_ids + absent_ids:
        _write(projects_dir, sid, [_record(16.7, now)], mtime_minutes_ago=16.7, now=now)
    report = _report(tmp_path, projects_dir, now, names=names)
    for row in report["peers"]:
        live_verdict = row["verdict"] not in (
            idle_report.VERDICT_EXITED, idle_report.VERDICT_UNKNOWN,
        )
        if live_verdict and row["registry"] != "absent":
            assert row["session"] in names, (
                "row %r rendered a live verdict but is absent from the "
                "registry snapshot" % row["session"]
            )


def test_the_projects_directory_is_derived_from_the_repo_root(tmp_path):
    """Hardcoding it is a watcher that silently reports the wrong fleet."""
    resolved = idle_report.projects_dir_for("X:/some-repo", home=str(tmp_path))
    assert resolved.endswith("X--some-repo")
    assert idle_report.projects_dir_for("X:\\some-repo", home=str(tmp_path)) == resolved


# --- the EXITED derivation order ------------------------------------------

def test_an_observed_exit_outranks_every_inference(tmp_path, projects_dir, now):
    """The harness reporting what it saw beats anything derived from a file's
    timestamps. This peer is live in the registry and one minute idle; the
    observed transition still wins."""
    _write(projects_dir, "2121aaaa-x", [_record(1, now)], mtime_minutes_ago=1, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"2121aaaa-x": "peer-1"},
                       observed_exits=frozenset({"2121aaaa-x"})), "2121aaaa")
    assert row["verdict"] == idle_report.VERDICT_EXITED


def test_the_clocks_contribute_nothing_to_the_exit_derivation(tmp_path, projects_dir, now):
    """A stalled content clock establishes idleness, never death. Registry-present
    and 45 minutes silent is ESCALATE, not EXITED."""
    _write(projects_dir, "2222cccc-x", [_record(45, now)], mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"2222cccc-x": "claude-klabauter-a9"}), "2222cccc")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE


def test_divergence_never_moves_a_verdict_in_either_direction(tmp_path, projects_dir, now):
    """Tested and refuted in the field: of three confirmed corpses one showed
    0.0m divergence, one 1.4m, one 7.4m, while live peers showed 0.0-0.5m. A
    large gap is neither necessary nor sufficient for an exit, so it decides
    nothing -- these two peers differ only in mtime and share a verdict."""
    _write(projects_dir, "2323aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    _write(projects_dir, "2424aaaa-x", [_record(40, now)], mtime_minutes_ago=1, now=now)
    report = _report(tmp_path, projects_dir, now,
                     names={"2323aaaa-x": "p", "2424aaaa-x": "q"})
    quiet, diverging = _row(report, "2323aaaa"), _row(report, "2424aaaa")
    assert diverging["divergence"] == idle_report.DIVERGENCE_UNKNOWN
    assert quiet["divergence"] == idle_report.DIVERGENCE_NONE
    assert quiet["verdict"] == diverging["verdict"] == idle_report.VERDICT_ESCALATE


def test_the_unknown_reason_set_admits_no_confidence_claim(tmp_path, projects_dir, now):
    """A "probably terminated" key invites the agent to decide how probable,
    which is the improvisation this design removes."""
    assert idle_report.UNKNOWN_REASONS == frozenset({
        "liveness-unresolved", "transcript-unreadable", "no-records", "clock-unparseable",
        "out-of-work-undetected", "suppression-unavailable",
        # A key, added for a genuinely new case as the module's docstring
        # sanctions: "cannot act" is not a confidence claim about "will not".
        "rate-limited",
    })


# --- the downgrade rule ---------------------------------------------------

def test_a_missing_offer_log_downgrades_to_reporting_not_to_sending(
    tmp_path, projects_dir, now, monkeypatch
):
    """Losing suppression must not become "nudge everyone again". The peers the
    watcher would have nudged come back as UNKNOWN, which routes to report-it."""
    _write(projects_dir, "2525aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    monkeypatch.setattr(idle_report, "_read_group_em_log", lambda *a, **k: ([], False))
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"2525aaaa-x": "claude-klabauter-a9"},
                       group_em_session_id="group-em-1"), "2525aaaa")
    assert row["verdict"] == idle_report.VERDICT_UNKNOWN
    assert row["reason"] == idle_report.REASON_SUPPRESSION_UNAVAILABLE
    assert row["nudge-shape"] == idle_report.SHAPE_HOLD


def test_an_empty_offer_log_is_an_answer_not_a_failure(tmp_path, projects_dir, now):
    """"This Group-EM has offered nobody" is a real answer and must not downgrade."""
    _write(projects_dir, "2626aaaa-x", [_said("I'll now merge.", 40, now)],
           mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now,
                       names={"2626aaaa-x": "claude-klabauter-a9"},
                       group_em_session_id="group-em-1"), "2626aaaa")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE
    assert row["nudge-shape"] == idle_report.SHAPE_PUSH


def test_group_em_moved_emits_no_peer_rows_at_all(tmp_path, projects_dir, now, monkeypatch):
    """The rows would describe a fleet this watcher no longer has standing over,
    and a row that is present is a row something acts on."""
    _write(projects_dir, "2727aaaa-x", [_record(40, now)], mtime_minutes_ago=40, now=now)
    _nomination(monkeypatch, "somebody-else")
    report = _report(tmp_path, projects_dir, now, group_em_session_id="2828aaaa-x")
    assert report["peers"] == []
    assert report["verdict"] == idle_report.VERDICT_GROUP_EM_MOVED


def test_the_nudge_shape_set_is_closed(tmp_path, projects_dir, now):
    """A shape the watcher cannot place makes it SEND wrongly -- the
    highest-stakes improvisation available to it."""
    assert idle_report.NUDGE_SHAPES == {"push", "ask-which-it-is", "assign", "hold"}


def test_every_emitted_shape_and_verdict_is_in_its_closed_set(tmp_path, projects_dir, now):
    for age in (1, 6, 40):
        _write(projects_dir, "29%02daaaa-x" % age, [_said("I'll now merge.", age, now)],
               mtime_minutes_ago=age, now=now)
    report = _report(tmp_path, projects_dir, now, names={})
    verdicts = {
        idle_report.VERDICT_BETWEEN_TURNS, idle_report.VERDICT_WATCH,
        idle_report.VERDICT_ESCALATE, idle_report.VERDICT_OUT_OF_WORK,
        idle_report.VERDICT_EXITED, idle_report.VERDICT_GROUP_EM_MOVED,
        idle_report.VERDICT_UNKNOWN,
    }
    for row in report["peers"]:
        assert row["verdict"] in verdicts
        assert row["nudge-shape"] in idle_report.NUDGE_SHAPES
        assert row["reason"] is None or row["reason"] in idle_report.UNKNOWN_REASONS


# --- the refusal, as distinct from the stall ------------------------------
#
# Six ESCALATE verdicts landed in one tick on 2026-09-01 and all six were
# false: a shared limit window had stopped every peer's clock at once. The
# fix is a CHECK, not a threshold -- the tick's one real stall (`c7`, 88
# minutes, closed quick-wrap, a named next move) sat inside the same band, so
# any threshold that suppressed the five suppressed it too. These tests pin
# the discriminator, not the suppression.


def _refusal(minutes_ago, now, resets_in_minutes=180):
    """A harness refusal record, in the shape the harness actually writes it."""
    record = _said("You've hit your session limit \u00b7 resets 7:30pm (Europe/London)",
                   minutes_ago, now)
    record["quotaLimits"] = {
        "status": "rejected",
        "resetsAt": int(now + resets_in_minutes * 60),
        "rateLimitType": "five_hour",
    }
    record["error"] = "rate_limit"
    record["apiErrorStatus"] = 429
    return record


def test_a_refused_peer_is_unknown_and_never_escalate(tmp_path, projects_dir, now):
    """A peer whose clock stopped because the harness refused every request it
    made has not stalled -- it cannot act. Escalating it fans a nudge into a
    session structurally incapable of reading it, and a limit window is
    fleet-wide, so it does that to every peer at once."""
    _write(projects_dir, "3030aaaa-x", [_record(90, now), _refusal(45, now)],
           mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"3030aaaa-x": "p"}), "3030aaaa")
    assert row["verdict"] == idle_report.VERDICT_UNKNOWN
    assert row["reason"] == idle_report.REASON_RATE_LIMITED
    assert row["reason"] in idle_report.UNKNOWN_REASONS
    # Toward REPORTING, never toward SENDING.
    assert row["nudge-shape"] == idle_report.SHAPE_HOLD


def test_a_real_stall_in_the_same_band_still_escalates(tmp_path, projects_dir, now):
    """THE DISCRIMINATOR. `c7`'s shape from the 2026-09-01 tick: 88 minutes, a
    closed quick-wrap, a named next move, and no refusal anywhere. Five
    suppressed, this one surviving -- a fix that quiets this has made the
    oracle quieter and less useful, which is the failure a raised threshold
    would have produced."""
    _write(projects_dir, "3131aaaa-x", [_record(120, now), _refusal(50, now)],
           mtime_minutes_ago=50, now=now)
    _write(projects_dir, "3232aaaa-x", [
        _said("Both reviews in and integrated. Quick-wrap closed.", 90, now),
        _said("I'll now fold the scoped review's verdict in when it lands.", 88, now),
    ], mtime_minutes_ago=88, now=now)
    report = _report(tmp_path, projects_dir, now,
                     names={"3131aaaa-x": "p", "3232aaaa-x": "q"})
    assert _row(report, "3131aaaa")["verdict"] == idle_report.VERDICT_UNKNOWN
    stall = _row(report, "3232aaaa")
    assert stall["verdict"] == idle_report.VERDICT_ESCALATE
    assert stall["nudge-shape"] == idle_report.SHAPE_PUSH


def test_a_lifted_window_escalates_again_without_anything_lifting_it(
    tmp_path, projects_dir, now
):
    """`resetsAt` is the freshness token: past it, the refusal no longer
    explains the silence and the verdict comes back on its own. Nothing
    persists a mute and nothing has to remember to clear one."""
    _write(projects_dir, "3333aaaa-x", [_refusal(45, now, resets_in_minutes=-10)],
           mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"3333aaaa-x": "p"}), "3333aaaa")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE


def test_a_refusal_the_session_has_moved_past_does_not_suppress(
    tmp_path, projects_dir, now
):
    """`resetsAt` ALONE over-suppresses, measured: the 2026-09-01 refusals
    nominally ran to 18:30Z and every peer resumed at 16:42Z. A session still
    being refused keeps retrying, so its refusal records stay level with its
    clock; one that got through and then genuinely stalled has left the
    refusal behind, and that stall is a real one."""
    _write(projects_dir, "3434aaaa-x", [
        _refusal(100, now, resets_in_minutes=180),
        _said("Back in. I'll now run the scoped tests.", 40, now),
    ], mtime_minutes_ago=40, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"3434aaaa-x": "p"}), "3434aaaa")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE


def test_talking_about_a_rate_limit_is_not_being_rate_limited(
    tmp_path, projects_dir, now
):
    """The same discipline `_is_out_of_work` keeps: structured spellings only.
    Sessions discuss rate limits constantly -- the session that wrote this fix
    quoted the sentence dozens of times -- and prose is not evidence."""
    _write(projects_dir, "3535aaaa-x", [
        _said("Three peers had a last-said reading \"You've hit your session limit "
              "\u00b7 resets 7:30pm (Europe/London)\" and quotaLimits status rejected.",
              50, now),
    ], mtime_minutes_ago=50, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"3535aaaa-x": "p"}), "3535aaaa")
    assert row["verdict"] == idle_report.VERDICT_ESCALATE


def test_a_refused_peer_the_registry_has_forgotten_is_still_exited(
    tmp_path, projects_dir, now
):
    """A refusal explains a stopped clock, never a missing process. Downgrading
    a corpse to UNKNOWN would put it back on the roster the `EXITED` verdict
    exists to date and retire."""
    _write(projects_dir, "3636aaaa-x", [_refusal(45, now)], mtime_minutes_ago=45, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"other": "p"}), "3636aaaa")
    assert row["verdict"] == idle_report.VERDICT_EXITED


def test_a_refused_peer_below_the_threshold_is_untouched(tmp_path, projects_dir, now):
    """The check sits over `ESCALATE` alone. A peer refused two minutes ago is
    between turns, exactly as it was before -- the refusal changes no verdict
    the watcher was never going to act on."""
    _write(projects_dir, "3737aaaa-x", [_refusal(2, now)], mtime_minutes_ago=2, now=now)
    row = _row(_report(tmp_path, projects_dir, now, names={"3737aaaa-x": "p"}), "3737aaaa")
    assert row["verdict"] == idle_report.VERDICT_BETWEEN_TURNS
    assert row["reason"] is None

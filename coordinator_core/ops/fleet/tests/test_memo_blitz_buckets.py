"""
Tests for coordinator_core.ops.fleet.memo_blitz_buckets — the mechanical half of
an inbox blitz.

Exercised against a FIXTURE inbox (the op's `inbox_dir` param) rather than a
real repo, so the corpus shape under test is pinned rather than whatever the
machine's inbox happens to hold on the day.

Covered:
  - param validation (dry_run required + must be true; positive-int thresholds;
    bool rejected as a threshold since bool is an int subclass)
  - bucketing: fyi / dominant-correspondent / rest, with fyi winning over
    dominant (the fyi sweep's re-judgement is the point of that bucket)
  - dominant-correspondent resolution, including the two floors that suppress it
  - supersession CANDIDATES: declared (`supersedes:`) and inferred
    (same-sender / later-date / shared cited locus) — and that neither is
    reported as a confirmation
  - `space:` preference over the inferred fallback, with `space_declared`
    distinguishing the two
  - both escalation legs, independently
  - terminal-status memos excluded from the open pile
  - malformed inbox files counted in `unreadable`, not silently swallowed
  - no-write proof

Spec backlink: cross-repo/inbox/2026-07-28-example-retrieval-repo-em-inbox-blitz-proven-pattern.md;
  DoE state/handoffs/2026-07-28-fold-inbox-blitz-into-workday-start-as-a.md
"""

from __future__ import annotations

import datetime
from pathlib import Path

from coordinator_core.ops.fleet.memo_blitz_buckets import (
    _MODE,
    _build_candidates,
    _memo_blitz_buckets,
    _validate_params,
)


def _write_memo(
    inbox: Path,
    name: str,
    *,
    sender: str = "claude-klabauter-em",
    kind: str = "ask",
    status: str = "open",
    created: str = "2026-07-20",
    space: str | None = None,
    supersedes: str | list[str] | None = None,
    in_reply_to: str | None = None,
    body: str = "Some body text.\n",
    title: str = "A memo",
) -> Path:
    lines = [
        "---",
        f'title: "{title}"',
        f'from: "{sender}"',
        'to: "doe-claude-em"',
        f"created: {created}",
        f"status: {status}",
        "delivery_mode: receiver-repo",
        f'kind: "{kind}"',
    ]
    if space is not None:
        lines.append(f'space: "{space}"')
    if isinstance(supersedes, str):
        lines.append(f'supersedes: "{supersedes}"')
    elif isinstance(supersedes, list):
        lines.append("supersedes:")
        lines.extend(f'  - "{entry}"' for entry in supersedes)
    if in_reply_to is not None:
        lines.append(f'in_reply_to: "{in_reply_to}"')
    lines.append("---")
    path = inbox / name
    path.write_text("\n".join(lines) + "\n\n" + body, encoding="utf-8")
    return path


def _by_kind(candidates: list[dict], kind: str) -> list[dict]:
    return [c for c in candidates if c["kind"] == kind]


def _summary(candidates: list[dict]) -> dict:
    return _by_kind(candidates, "bucket_summary")[0]


def _trigger(candidates: list[dict]) -> dict:
    return _by_kind(candidates, "trigger")[0]


TODAY = datetime.date(2026, 7, 28)


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

class TestValidateParams:
    def test_missing_dry_run_is_setup_error(self):
        result = _validate_params({})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1
        assert result["mode"] == _MODE

    def test_dry_run_false_is_setup_error(self):
        result = _validate_params({"dry_run": False})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_defaults_applied(self):
        dry_run, open_threshold, age_days, inbox_dir = _validate_params({"dry_run": True})
        assert dry_run is True
        assert open_threshold == 10
        assert age_days == 7
        assert inbox_dir is None

    def test_bool_threshold_rejected(self):
        # bool is an int subclass — `open_threshold: true` must not silently
        # mean 1.
        result = _validate_params({"dry_run": True, "open_threshold": True})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_non_positive_threshold_rejected(self):
        result = _validate_params({"dry_run": True, "age_days_threshold": 0})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

class TestBucketing:
    def _dominant_inbox(self, tmp_path: Path) -> Path:
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(6):
            _write_memo(inbox, f"2026-07-2{i % 8}-claude-klabauter-em-thread-{i}.md")
        _write_memo(inbox, "2026-07-21-example-retrieval-repo-em-other.md", sender="example-retrieval-repo-em")
        _write_memo(
            inbox, "2026-07-22-cockpit-em-status.md", sender="cockpit-em", kind="fyi",
        )
        return inbox

    def test_dominant_correspondent_resolved_and_bucketed(self, tmp_path):
        inbox = self._dominant_inbox(tmp_path)
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        summary = _summary(candidates)
        assert summary["dominant_sender"] == "claude-klabauter-em"
        assert summary["dominant_count"] == 6
        assert summary["bucket_counts"]["dominant"] == 6
        assert summary["bucket_counts"]["rest"] == 1
        assert summary["bucket_counts"]["fyi"] == 1

    def test_fyi_wins_over_dominant(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(5):
            _write_memo(inbox, f"2026-07-2{i}-claude-klabauter-em-ask-{i}.md")
        _write_memo(inbox, "2026-07-26-claude-klabauter-em-note.md", kind="fyi")
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        buckets = {c["id"]: c["bucket"] for c in _by_kind(candidates, "bucket")}
        # Same (dominant) sender, but kind fyi — must land in the fyi sweep, or
        # the re-judgement that surfaced a break-class defect never happens.
        assert buckets["2026-07-26-claude-klabauter-em-note.md"] == "fyi"
        assert buckets["2026-07-20-claude-klabauter-em-ask-0.md"] == "dominant"

    def test_no_dominant_below_min_open_floor(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-one.md", sender="a-em")
        _write_memo(inbox, "2026-07-21-a-em-two.md", sender="a-em")
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        assert _summary(candidates)["dominant_sender"] is None
        assert all(c["bucket"] == "rest" for c in _by_kind(candidates, "bucket"))

    def test_no_dominant_when_pile_is_evenly_spread(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i, sender in enumerate(["a-em", "b-em", "c-em", "d-em", "e-em", "f-em"]):
            _write_memo(inbox, f"2026-07-2{i}-{sender}-x.md", sender=sender)
        assert _summary(_build_candidates(inbox, 10, 7, TODAY))["dominant_sender"] is None

    def test_terminal_status_memos_excluded(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-open.md", sender="a-em")
        _write_memo(inbox, "2026-07-20-a-em-done.md", sender="a-em", status="actioned")
        _write_memo(inbox, "2026-07-20-a-em-closed.md", sender="a-em", status="closed")
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        assert _summary(candidates)["open_count"] == 1

    def test_unreadable_files_counted_not_swallowed(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-ok.md", sender="a-em")
        (inbox / "README.md").write_text("no frontmatter here\n", encoding="utf-8")
        summary = _summary(_build_candidates(inbox, 10, 7, TODAY))
        assert summary["open_count"] == 1
        assert summary["unreadable"] == ["README.md"]

    def test_non_utf8_file_counted_unreadable_not_fatal(self, tmp_path):
        # Review: code-reviewer F1 — a binary/non-UTF-8 file must land in
        # unreadable[] rather than raising UnicodeDecodeError out of the
        # whole sweep.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-ok.md", sender="a-em")
        (inbox / "binary.md").write_bytes(b"\xff\xfe\x00\x01not utf-8\x80\x81")
        summary = _summary(_build_candidates(inbox, 10, 7, TODAY))
        assert summary["open_count"] == 1
        assert summary["unreadable"] == ["binary.md"]

    def test_all_terminal_status_inbox_distinct_from_empty(self, tmp_path):
        # Review: code-reviewer test-quality note — an all-terminal-status
        # inbox and a genuinely empty inbox degrade through the same
        # open_count == 0 path; assert both explicitly so a future change
        # that special-cases one doesn't silently break the other.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-done.md", sender="a-em", status="actioned")
        _write_memo(inbox, "2026-07-20-a-em-closed.md", sender="a-em", status="closed")
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        summary = _summary(candidates)
        assert summary["open_count"] == 0
        assert summary["unreadable"] == []
        assert _trigger(candidates)["fires"] is False


# ---------------------------------------------------------------------------
# space:
# ---------------------------------------------------------------------------

class TestSpace:
    def test_declared_space_preferred_and_flagged(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-one.md", sender="a-em", space="gate-migration")
        _write_memo(inbox, "2026-07-21-a-em-two.md", sender="a-em")
        buckets = {c["id"]: c for c in _by_kind(_build_candidates(inbox, 10, 7, TODAY), "bucket")}
        declared = buckets["2026-07-20-a-em-one.md"]
        assert declared["space"] == "gate-migration"
        assert declared["space_declared"] is True
        inferred = buckets["2026-07-21-a-em-two.md"]
        # Fallback is the topic slug with the date prefix stripped — a weaker
        # key, and explicitly marked as this op's guess.
        assert inferred["space"] == "a-em-two"
        assert inferred["space_declared"] is False

    def test_spaces_declared_counted_in_summary(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-one.md", sender="a-em", space="s")
        _write_memo(inbox, "2026-07-21-a-em-two.md", sender="a-em", space="s")
        _write_memo(inbox, "2026-07-22-a-em-three.md", sender="a-em")
        assert _summary(_build_candidates(inbox, 10, 7, TODAY))["spaces_declared"] == 2


# ---------------------------------------------------------------------------
# Supersession candidates
# ---------------------------------------------------------------------------

class TestSupersessionCandidates:
    def test_self_declared_superseding_it_form_detected(self, tmp_path):
        # AC2 form 1 — a memo citing the older memo's basename directly and
        # announcing the supersession with the verb "superseding".
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The original ask.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="See 2026-07-20-a-em-old.md. Superseding it with this memo.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "self-declared"
        assert cands[0]["newer"] == "2026-07-25-a-em-new.md"
        assert cands[0]["older"] == "2026-07-20-a-em-old.md"
        assert cands[0]["advisory"] is False
        assert "CANDIDATE" in cands[0]["note"]

    def test_self_declared_authoritative_disagree_form_detected(self, tmp_path):
        # AC2 form 2 — syntactically unalike from form 1: no "supersed*" verb
        # at all, a precedence claim instead, paired via the generic
        # "previous memo" reference rather than a basename citation.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The original ask.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body=(
                "This updates the previous memo — read this one as "
                "authoritative where the two disagree.\n"
            ),
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "self-declared"
        assert cands[0]["newer"] == "2026-07-25-a-em-new.md"
        assert cands[0]["older"] == "2026-07-20-a-em-old.md"

    def test_authoritative_disagree_unrelated_clause_is_not_a_candidate(self, tmp_path):
        # Review: code-reviewer F1 — a same-line "authoritative ... disagree"
        # pair that shares no "where" clause link is an unrelated coincidence,
        # not a precedence claim, and must not fire even when the memo also
        # cites a legitimate sibling basename elsewhere in the body.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The original ask.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body=(
                "See 2026-07-20-a-em-old.md for background. Our position "
                "remains authoritative, though regional offices disagree on "
                "this point.\n"
            ),
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_bare_superseding_with_no_memo_reference_is_not_a_candidate(self, tmp_path):
        # AC5 — the phrase alone, with no reference to another memo, must not
        # emit a candidate.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The original ask.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="We are superseding our old process going forward.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_declared_beats_prose_when_the_sender_filled_the_field(self, tmp_path):
        # AC3 — when all three bases could apply to the same pair, the pair is
        # emitted exactly once. The winning basis is `declared`, not
        # `self-declared`: `supersedes:` is the sender's own structured answer
        # to the question the prose pass infers, so prose does not overrule it.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="see foo.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            supersedes="2026-07-20-a-em-old.md",
            body="See 2026-07-20-a-em-old.md. Superseding it, and see foo.py too.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "declared"
        assert cands[0]["newer"] == "2026-07-25-a-em-new.md"
        assert cands[0]["older"] == "2026-07-20-a-em-old.md"

    def test_prose_never_retargets_a_memo_that_declared_supersedes(self, tmp_path):
        # Regression, DoE-claude 2026-08-30: a memo declaring `supersedes: A`
        # whose prose also trips the phrase pattern used to be paired against
        # a DIFFERENT memo B by the self-declared pass, which ran first and
        # claimed the pair at the top-ranked basis. The declared target is the
        # only one that may be emitted for such a memo.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-18-a-em-real-target.md", sender="a-em",
            created="2026-07-18", body="The contract.\n",
        )
        _write_memo(
            inbox, "2026-07-22-a-em-decoy.md", sender="a-em",
            created="2026-07-22", body="Something else entirely.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            supersedes="cross-repo/inbox/2026-07-18-a-em-real-target.md",
            body="Superseding 2026-07-22-a-em-decoy.md as I read it.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        by_newer = [c for c in cands if c["newer"] == "2026-07-25-a-em-new.md"]
        assert [(c["older"], c["basis"]) for c in by_newer] == [
            ("2026-07-18-a-em-real-target.md", "declared")
        ]

    def test_declared_supersedes_resolves_through_a_path_form_reference(self, tmp_path):
        # Regression, DoE-claude 2026-08-30: `supersedes:` is authored with
        # every root the corpus admits. A reference carrying any directory
        # prefix used to miss the basename-keyed index entirely, dropping the
        # declaration — and with it the basis that outranks a prose guess.
        for i, reference in enumerate((
            "cross-repo/inbox/2026-07-20-a-em-old.md",
            "state/memo-outbox/sent/2026-07-20-a-em-old.md",
            # abs-path-ok: fixture data, not a citation — a foreign-host
            # absolute root is one of the four shapes `supersedes:` is
            # authored in across the live corpus, and is the case the
            # basename normalization exists to survive.
            "/Users/x/repo/cross-repo/inbox/2026-07-20-a-em-old.md",
            "2026-07-20-a-em-old.md",
        )):
            inbox = tmp_path / f"inbox-{i}"
            inbox.mkdir()
            _write_memo(
                inbox, "2026-07-20-a-em-old.md", sender="a-em",
                created="2026-07-20", body="The original.\n",
            )
            _write_memo(
                inbox, "2026-07-25-a-em-new.md", sender="a-em",
                created="2026-07-25", supersedes=reference,
                body="A follow-up with no supersession prose.\n",
            )
            cands = _by_kind(
                _build_candidates(inbox, 10, 7, TODAY), "supersession_candidate"
            )
            assert [(c["basis"], c["older"]) for c in cands] == [
                ("declared", "2026-07-20-a-em-old.md")
            ], reference

    def test_in_reply_to_resolves_its_own_thread_not_the_nearest_date(self, tmp_path):
        # Regression, DoE-claude 2026-08-30: `in_reply_to` was matched as a
        # bare token in prose and then resolved by date proximity, so a memo
        # naming thread A was paired with whatever same-sender memo happened
        # to be nearest in time. The frontmatter value is the answer.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-10-a-em-thread.md", sender="a-em",
            created="2026-07-10", body="The thread being answered.\n",
        )
        _write_memo(
            inbox, "2026-07-24-a-em-nearest.md", sender="a-em",
            created="2026-07-24", body="An unrelated concurrent thread.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            in_reply_to="2026-07-10-a-em-thread.md",
            body="Superseding the earlier memo on this.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        self_declared = [c for c in cands if c["basis"] == "self-declared"]
        assert [(c["newer"], c["older"]) for c in self_declared] == [
            ("2026-07-25-a-em-new.md", "2026-07-10-a-em-thread.md")
        ]

    def test_unresolvable_in_reply_to_emits_nothing_rather_than_a_substitute(self, tmp_path):
        # The declared thread is archived or was never received here. Its
        # absence is an answer — there is no open pair to offer — and must
        # NOT fall through to the nearest-dated same-sender memo.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-24-a-em-nearest.md", sender="a-em",
            created="2026-07-24", body="An unrelated concurrent thread.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            in_reply_to="2026-07-01-a-em-already-archived.md",
            body="Superseding the earlier memo on this.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert [c for c in cands if c["basis"] == "self-declared"] == []

    def test_in_reply_to_across_senders_is_not_a_supersession(self, tmp_path):
        # A reply names the memo it answers, routinely across senders. That is
        # a correspondence edge — nobody supersedes someone else's memo.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-10-b-em-their-ask.md", sender="b-em",
            created="2026-07-10", body="Their ask.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            in_reply_to="2026-07-10-b-em-their-ask.md",
            body="Superseding the earlier memo on this.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_ambiguous_basename_citation_is_skipped(self, tmp_path):
        # Precision-over-recall: a memo whose body cites TWO different
        # same-sender older memos by basename gives no unambiguous single
        # older memo to pair with, and is skipped rather than guessed.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-18-a-em-one.md", sender="a-em", created="2026-07-18",
            body="Ask one.\n",
        )
        _write_memo(
            inbox, "2026-07-19-a-em-two.md", sender="a-em", created="2026-07-19",
            body="Ask two.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body=(
                "Superseding both 2026-07-18-a-em-one.md and "
                "2026-07-19-a-em-two.md with updated terms.\n"
            ),
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_generic_reference_resolves_to_nearest_earlier_same_sender(self, tmp_path):
        # No basename citation at all, so the generic "previous memo" phrase
        # resolves to the single nearest earlier same-sender memo — this is
        # deterministic nearest-date resolution, not a guess among ties.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-18-a-em-one.md", sender="a-em", created="2026-07-18",
            body="Ask one.\n",
        )
        _write_memo(
            inbox, "2026-07-19-a-em-two.md", sender="a-em", created="2026-07-19",
            body="Ask two.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="Superseding the previous memo, updated terms apply.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "self-declared"
        assert cands[0]["older"] == "2026-07-19-a-em-two.md"

    def test_bare_slug_citation_yields_no_candidate(self, tmp_path):
        # Replay of the 2026-08-20 escalate run's worked example. The memo
        # names its target by bare topic-slug, which carries no file extension
        # and so never reaches `loci`; the generic phrase "that memo" then
        # used to resolve by date adjacency to an unrelated same-sender memo.
        # A slug reference IS a citation: precision-over-recall means no
        # candidate at all rather than a nearest-dated stand-in.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-08-01-a-em-distill-log-parser-discards-every-run.md",
            sender="a-em", created="2026-08-01", body="The parser drops runs.\n",
        )
        _write_memo(
            inbox, "2026-08-05-a-em-anchor-gate-green-manifest-live.md",
            sender="a-em", created="2026-08-05", body="Dead doc anchors, fyi.\n",
        )
        _write_memo(
            inbox, "2026-08-06-a-em-log-correction-the-defect-is-ours.md",
            sender="a-em", created="2026-08-06",
            body=(
                "Supersedes distill-log-parser-discards-every-run, sent last "
                "week. Do not action that memo.\n"
            ),
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert [c for c in cands if c["basis"] == "self-declared"] == []

    def test_declared_basis_carries_advisory_false(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-old.md", sender="a-em")
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em",
            supersedes="2026-07-20-a-em-old.md",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands[0]["advisory"] is False

    def test_same_sender_same_locus_carries_advisory_true(self, tmp_path):
        # AC4 — the locus basis is explicitly marked advisory, distinguishing
        # it from the two declaration bases.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="see rare_locus.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="see rare_locus.py again\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "same-sender-same-locus"
        assert cands[0]["advisory"] is True

    def test_declared_supersession_emitted_as_candidate(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-old.md", sender="a-em")
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em",
            supersedes="2026-07-20-a-em-old.md",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "declared"
        assert cands[0]["newer"] == "2026-07-25-a-em-new.md"
        assert cands[0]["older"] == "2026-07-20-a-em-old.md"
        # Never reported as settled — the op emits candidates, full stop.
        assert "CANDIDATE" in cands[0]["note"]

    def test_declared_list_form_emits_one_candidate_per_reference(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-one.md", sender="a-em")
        _write_memo(inbox, "2026-07-21-a-em-two.md", sender="a-em")
        _write_memo(
            inbox, "2026-07-25-a-em-three.md", sender="a-em",
            supersedes=["2026-07-20-a-em-one.md", "2026-07-21-a-em-two.md"],
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        declared = [c for c in cands if c["basis"] == "declared"]
        assert {c["older"] for c in declared} == {
            "2026-07-20-a-em-one.md", "2026-07-21-a-em-two.md",
        }

    def test_declared_reference_outside_open_pile_is_not_offered(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em",
            supersedes="2026-06-01-a-em-already-archived.md",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_same_sender_same_locus_inferred(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The bug is in coordinator_core/ops/fleet/memo_send.py today.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="Correction: ops/fleet/memo_send.py was already fixed.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "same-sender-same-locus"
        assert cands[0]["newer"] == "2026-07-25-a-em-new.md"
        assert cands[0]["shared_loci"] == ["memo_send.py"]

    def test_different_senders_same_locus_not_a_candidate(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-x.md", sender="a-em", created="2026-07-20",
            body="see foo.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-b-em-y.md", sender="b-em", created="2026-07-25",
            body="see foo.py\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_same_day_pair_not_a_candidate(self, tmp_path):
        # Neither is "later"; a same-day pair carries no supersession direction.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-x.md", sender="a-em", body="see foo.py\n")
        _write_memo(inbox, "2026-07-20-a-em-y.md", sender="a-em", body="see foo.py\n")
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_declared_pair_not_duplicated_by_the_inferred_pass(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="see foo.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            supersedes="2026-07-20-a-em-old.md", body="see foo.py\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "declared"


    def test_declared_direction_wins_over_disagreeing_inferred_dates(self, tmp_path):
        # Review: code-reviewer F2 — a same-sender pair whose supersedes:
        # claim disagrees with created-date ordering must emit exactly one
        # candidate, with the declared basis and the declared direction, not
        # two candidates with inverted newer/older claims.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-25-a-em-actually-older.md", sender="a-em",
            created="2026-07-25", body="see foo.py\n",
        )
        _write_memo(
            inbox, "2026-07-20-a-em-actually-newer.md", sender="a-em",
            created="2026-07-20", body="see foo.py\n",
            supersedes="2026-07-25-a-em-actually-older.md",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert len(cands) == 1
        assert cands[0]["basis"] == "declared"
        assert cands[0]["newer"] == "2026-07-20-a-em-actually-newer.md"
        assert cands[0]["older"] == "2026-07-25-a-em-actually-older.md"

    def test_unknown_date_memo_not_paired_in_inferred_pass(self, tmp_path):
        # Review: code-reviewer F3 — a same-sender memo with no resolvable
        # date must not be synthesized as "the older" side of a candidate
        # against a dated memo via the datetime.date.min sentinel.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        dated = inbox / "2026-07-25-a-em-dated.md"
        dated.write_text(
            '---\ntitle: "t"\nfrom: "a-em"\nto: "doe-claude-em"\n'
            'created: 2026-07-25\nstatus: open\ndelivery_mode: receiver-repo\n'
            'kind: "ask"\n---\n\nsee foo.py\n',
            encoding="utf-8",
        )
        undated = inbox / "no-date-prefix-a-em-file.md"
        undated.write_text(
            '---\ntitle: "t"\nfrom: "a-em"\nto: "doe-claude-em"\n'
            'status: open\ndelivery_mode: receiver-repo\nkind: "ask"\n---\n\n'
            'see foo.py\n',
            encoding="utf-8",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_url_locus_collision_not_a_candidate(self, tmp_path):
        # Review: code-reviewer F6 — two memos citing different URLs that
        # happen to share a trailing path segment must not collapse to a
        # shared "locus" via basename normalization.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-x.md", sender="a-em", created="2026-07-20",
            body="see https://example.com/v1/config.yaml\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-y.md", sender="a-em", created="2026-07-25",
            body="see https://other.example.org/v2/config.yaml\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_bare_filename_locus_still_matches_without_url(self, tmp_path):
        # Guards against an over-broad URL-stripping fix that also eats bare
        # filename citations — recall on `memo_send.py`-style citations must
        # survive the F6 fix.
        cands = _by_kind(
            _build_candidates(self._same_sender_locus_inbox(tmp_path), 10, 7, TODAY),
            "supersession_candidate",
        )
        assert len(cands) == 1
        assert cands[0]["shared_loci"] == ["memo_send.py"]

    @staticmethod
    def _same_sender_locus_inbox(tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="The bug is in coordinator_core/ops/fleet/memo_send.py today.\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="Correction: ops/fleet/memo_send.py was already fixed.\n",
        )
        return inbox

    def test_small_inbox_floor_still_governs(self, tmp_path):
        # Review: code-reviewer F4 — small-inbox behaviour must be identical
        # to before the corpus-scaled fix: the absolute floor (3) governs.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(4):
            _write_memo(
                inbox, f"2026-07-2{i}-a-em-noise{i}.md", sender="a-em",
                created=f"2026-07-2{i}", body="see SKILL.md\n",
            )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_large_inbox_locus_cited_4_times_still_pairs(self, tmp_path):
        # Review: code-reviewer F4 — 4 citations out of 100+ open memos is a
        # strong discriminating signal the bare floor (3) would wrongly
        # suppress; the share-scaled cutoff must still pass it through.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(100):
            _write_memo(
                inbox, f"2026-07-01-a-em-filler{i:03d}.md", sender=f"sender-{i}",
                created="2026-07-01", body="no shared locus here\n",
            )
        for i in range(2):
            _write_memo(
                inbox, f"2026-07-2{i}-b-em-shared{i}.md", sender="b-em",
                created=f"2026-07-2{i}", body="see rare_locus.py\n",
            )
        _write_memo(
            inbox, "2026-07-22-b-em-shared2.md", sender="b-em",
            created="2026-07-22", body="see rare_locus.py\n",
        )
        _write_memo(
            inbox, "2026-07-23-b-em-shared3.md", sender="b-em",
            created="2026-07-23", body="see rare_locus.py\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        shared = [c for c in cands if c["shared_loci"] == ["rare_locus.py"]]
        assert len(shared) >= 1

    def test_ubiquitous_locus_is_not_a_candidate_signal(self, tmp_path):
        # `SKILL.md`-class names are cited by everything and carry no thread
        # signal — pairing on them manufactures candidates an EM would reject.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(5):
            _write_memo(
                inbox, f"2026-07-2{i}-a-em-x{i}.md", sender="a-em",
                created=f"2026-07-2{i}", body="see coordinator/skills/x/SKILL.md\n",
            )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert cands == []

    def test_narrowly_cited_locus_still_pairs(self, tmp_path):
        # Same corpus shape, but the shared locus is cited by only the pair —
        # the frequency cut must not swallow the signal it exists to sharpen.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(4):
            _write_memo(
                inbox, f"2026-07-2{i}-a-em-noise{i}.md", sender="a-em",
                created=f"2026-07-2{i}", body="see SKILL.md\n",
            )
        _write_memo(
            inbox, "2026-07-24-a-em-old.md", sender="a-em", created="2026-07-24",
            body="bug in memo_blitz_buckets.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            body="correction on memo_blitz_buckets.py\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert [c["shared_loci"] for c in cands] == [["memo_blitz_buckets.py"]]

    def test_pairs_per_locus_bound_caps_fanout_at_boundary_cutoff(self, tmp_path):
        # state/audits/2026-08-12-supersession-candidate-pair-blowup.md — a
        # locus sitting exactly at `_discriminating_locus_cutoff` still
        # contributes up to C(cutoff, 2) pairs; `_MAX_PAIRS_PER_LOCUS` (3)
        # must cap that fanout regardless. Corpus sized to 61 open memos so
        # the SHARE-scaled cutoff (ceil(0.05 * 61) == 4) governs, and the
        # shared locus is cited by exactly 4 same-sender memos — the
        # boundary case (a locus AT the cutoff, not comfortably under it).
        # Uncapped this would emit C(4, 2) == 6 pairs; capped it must emit
        # at most `_MAX_PAIRS_PER_LOCUS` == 3.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(57):
            _write_memo(
                inbox, f"2026-07-01-filler{i:03d}-em-x.md", sender=f"filler{i:03d}-em",
                created="2026-07-01", body="no shared locus here\n",
            )
        for i in range(4):
            _write_memo(
                inbox, f"2026-07-2{i}-b-em-shared{i}.md", sender="b-em",
                created=f"2026-07-2{i}", body="see shared_file.py\n",
            )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        shared = [c for c in cands if c["shared_loci"] == ["shared_file.py"]]
        assert len(shared) == 3

    def test_declaration_bases_still_emit_when_inferred_basis_is_capped(self, tmp_path):
        # Regression for the pairs-per-locus fix: `self-declared` and
        # `declared` must be completely unaffected by the inferred-basis
        # pair cap, even in a corpus where the locus-pair cap is actively
        # suppressing `same-sender-same-locus` candidates.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(57):
            _write_memo(
                inbox, f"2026-07-01-filler{i:03d}-em-x.md", sender=f"filler{i:03d}-em",
                created="2026-07-01", body="no shared locus here\n",
            )
        for i in range(4):
            _write_memo(
                inbox, f"2026-07-2{i}-b-em-shared{i}.md", sender="b-em",
                created=f"2026-07-2{i}", body="see shared_file.py\n",
            )
        _write_memo(inbox, "2026-07-20-a-em-declared-old.md", sender="a-em")
        _write_memo(
            inbox, "2026-07-21-a-em-declared-new.md", sender="a-em",
            supersedes="2026-07-20-a-em-declared-old.md",
        )
        _write_memo(
            inbox, "2026-07-22-c-em-self-old.md", sender="c-em", created="2026-07-22",
            body="Original process notes.\n",
        )
        _write_memo(
            inbox, "2026-07-23-c-em-self-new.md", sender="c-em", created="2026-07-23",
            body="Superseding it: 2026-07-22-c-em-self-old.md is retired.\n",
        )
        cands = _by_kind(_build_candidates(inbox, 10, 7, TODAY), "supersession_candidate")
        assert any(c["basis"] == "declared" for c in cands)
        assert any(c["basis"] == "self-declared" for c in cands)
        inferred = [c for c in cands if c["basis"] == "same-sender-same-locus"]
        assert len(inferred) == 3


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

class TestTrigger:
    def test_neither_leg_trips_on_a_small_fresh_inbox(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-27-a-em-x.md", sender="a-em", created="2026-07-27")
        trig = _trigger(_build_candidates(inbox, 10, 7, TODAY))
        assert trig["fires"] is False
        assert trig["count_leg_tripped"] is False
        assert trig["age_leg_tripped"] is False

    def test_count_leg_alone_fires(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        for i in range(11):
            _write_memo(
                inbox, f"2026-07-27-a-em-x{i}.md", sender="a-em", created="2026-07-27",
            )
        trig = _trigger(_build_candidates(inbox, 10, 7, TODAY))
        assert trig["count_leg_tripped"] is True
        assert trig["age_leg_tripped"] is False
        assert trig["fires"] is True

    def test_age_leg_alone_fires(self, tmp_path):
        # The load-bearing leg: one memo, 16 days old — example-retrieval-repo's actual
        # failure shape, which no count threshold would ever have caught.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-12-a-em-x.md", sender="a-em", created="2026-07-12")
        trig = _trigger(_build_candidates(inbox, 10, 7, TODAY))
        assert trig["count_leg_tripped"] is False
        assert trig["age_leg_tripped"] is True
        assert trig["fires"] is True
        assert trig["oldest_open_age_days"] == 16

    def test_thresholds_are_tunable(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-12-a-em-x.md", sender="a-em", created="2026-07-12")
        assert _trigger(_build_candidates(inbox, 10, 30, TODAY))["fires"] is False

    def test_missing_created_falls_back_to_filename_date(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        path = inbox / "2026-07-12-a-em-x.md"
        path.write_text(
            '---\ntitle: "t"\nfrom: "a-em"\nto: "doe-claude-em"\n'
            'status: open\ndelivery_mode: receiver-repo\nkind: "ask"\n---\n\nbody\n',
            encoding="utf-8",
        )
        trig = _trigger(_build_candidates(inbox, 10, 7, TODAY))
        # Without the filename fallback this memo would be ageless and drag the
        # oldest-open figure toward "nothing is old here."
        assert trig["oldest_open_age_days"] == 16

    def test_malformed_created_falls_back_to_filename_date(self, tmp_path):
        # Review: code-reviewer test-quality note — only "missing created" was
        # tested before; a present-but-unparseable value must exercise the
        # same _created_date ValueError-catch fallback to the filename prefix.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        path = inbox / "2026-07-12-a-em-x.md"
        path.write_text(
            '---\ntitle: "t"\nfrom: "a-em"\nto: "doe-claude-em"\n'
            'created: not-a-date\nstatus: open\ndelivery_mode: receiver-repo\n'
            'kind: "ask"\n---\n\nbody\n',
            encoding="utf-8",
        )
        trig = _trigger(_build_candidates(inbox, 10, 7, TODAY))
        assert trig["oldest_open_age_days"] == 16

    def test_empty_inbox_is_not_an_error(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        candidates = _build_candidates(inbox, 10, 7, TODAY)
        assert _summary(candidates)["open_count"] == 0
        assert _trigger(candidates)["fires"] is False

    def test_absent_inbox_dir_is_not_an_error(self, tmp_path):
        candidates = _build_candidates(tmp_path / "nope", 10, 7, TODAY)
        assert _summary(candidates)["open_count"] == 0


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class TestHandler:
    def test_handler_returns_dry_run_envelope(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-x.md", sender="a-em")
        result = _memo_blitz_buckets({"dry_run": True, "inbox_dir": str(inbox)})
        assert result["exit_code"] == 0
        assert result["dry_run"] is True
        assert result["mode"] == _MODE
        assert result["acted"] == [] and result["failed"] == []
        kinds = {c["kind"] for c in result["candidates"]}
        assert {"bucket", "bucket_summary", "trigger"} <= kinds

    def test_handler_without_repo_root_or_inbox_dir_fails_loud(self):
        # The frozen fleet envelope carries no reason field — the exit_code:1
        # setup-error shape IS the signal (the reason is logged daemon-side).
        result = _memo_blitz_buckets({"dry_run": True})
        assert result["exit_code"] == 1
        assert result["mode"] == _MODE
        assert result["candidates"] == []

    def test_no_basis_ever_auto_applies(self, tmp_path):
        # AC6 — regression guard: every supersession basis (self-declared,
        # declared, same-sender-same-locus) remains an OFFER. This op has no
        # act mode at all (dry_run:false is rejected outright, see
        # TestValidateParams), and no candidate carries an "applied"/"acted"
        # field of any kind — a future edit must not quietly add one.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(
            inbox, "2026-07-20-a-em-old.md", sender="a-em", created="2026-07-20",
            body="see foo.py\n",
        )
        _write_memo(
            inbox, "2026-07-25-a-em-new.md", sender="a-em", created="2026-07-25",
            supersedes="2026-07-20-a-em-old.md",
            body="See 2026-07-20-a-em-old.md. Superseding it, and see foo.py too.\n",
        )
        result = _memo_blitz_buckets({"dry_run": True, "inbox_dir": str(inbox)})
        assert result["dry_run"] is True
        assert result["acted"] == []
        for candidate in result["candidates"]:
            if candidate["kind"] == "supersession_candidate":
                assert "applied" not in candidate
                assert "acted" not in candidate

    def test_handler_writes_nothing(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        _write_memo(inbox, "2026-07-20-a-em-x.md", sender="a-em")
        before = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        _memo_blitz_buckets({"dry_run": True, "inbox_dir": str(inbox)})
        after = {p: p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file()}
        assert before == after

"""Unit coverage for `coordinator_core.bash_guards._message_size` (C2).

Spec backlink: pln-runtime-measured-message-size--0669ac,
chunk C2. Three things this module owns and must prove correct in
isolation, before any corpus (C3) or gate (C5) is built on top of it:

  (a) the prose/exempt-span split (cue-window-anchored backtick and
      indented-block extraction, per the pinned 7-step algorithm);
  (b) the `operator_override_note` tail subtracted BY IDENTITY (call the
      same builder, subtract the exact returned substring);
  (c) the speaker predicate (`prose_bytes > 0`) returning `False` on a
      synthetic zero-prose non-`None` envelope -- the exact silent-shim
      trap `dispatch._resolve_suppressed_envelope` reconstitutes on
      non-Windows hosts (plan's own "single most important correction").
"""

from __future__ import annotations

from coordinator_core.bash_guards import _message_size as msz
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards.dispatch import GuardBand


def _envelope(*, additional_context: str = None, permission_reason: str = None, updated_input: dict = None) -> dict:
    hso: dict = {"hookEventName": "PreToolUse"}
    if additional_context is not None:
        hso["additionalContext"] = additional_context
    if permission_reason is not None:
        hso["permissionDecisionReason"] = permission_reason
    if updated_input is not None:
        hso["updatedInput"] = updated_input
    return {"hookSpecificOutput": hso}


class TestMessageProseCap:
    def test_cap_is_220_not_440(self):
        assert msz.MESSAGE_PROSE_CAP_BYTES == 220


class TestNoneEnvelope:
    """A non-firing guard's `GuardEntry.fn()` return -- `None` -- must
    degrade to a clean zero-byte, non-speaker measurement, not raise."""

    def test_none_envelope_is_zero_bytes_and_not_speaker(self):
        result = msz.measure_envelope(None)
        assert result.total_bytes == 0
        assert result.prose_bytes == 0
        assert result.exempt_bytes == 0
        assert result.tail_bytes == 0
        assert result.is_speaker is False
        assert result.over_cap is False


class TestSpeakerPredicateIsProseBytesNotEnvelopeIdentity:
    """The plan's "single most important correction": speaker = prose_bytes
    > 0, NOT "envelope is non-None". `dispatch._resolve_suppressed_envelope`
    manufactures exactly this shape on non-Windows hosts -- a non-`None`
    envelope carrying only `updatedInput` (a rewrite leg) and no
    `additionalContext`/`permissionDecisionReason` at all."""

    def test_zero_prose_non_none_envelope_is_not_a_speaker(self):
        envelope = _envelope(updated_input={"command": "git status"})
        assert envelope is not None  # sanity: the predicate under test is NOT this
        result = msz.measure_envelope(envelope)
        assert result.total_bytes == 0
        assert result.prose_bytes == 0
        assert result.is_speaker is False

    def test_updated_input_command_never_counted_as_prose(self):
        long_command = "x" * 5000
        envelope = _envelope(
            additional_context="short",
            updated_input={"command": long_command},
        )
        result = msz.measure_envelope(envelope)
        # If `command` leaked into the measured text, total_bytes would be
        # in the thousands; it must reflect only `additionalContext`.
        assert result.total_bytes == len("short".encode("utf-8"))

    def test_nonempty_prose_is_a_speaker(self):
        envelope = _envelope(additional_context="Advisory: do the other thing.")
        result = msz.measure_envelope(envelope)
        assert result.is_speaker is True
        assert result.prose_bytes > 0


class TestProseExemptSplit:
    def test_plain_prose_with_no_cue_window_is_all_prose(self):
        text = "This command is denied for a structural reason with no offered alternative."
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes == 0
        assert result.total_bytes == len(text.encode("utf-8"))
        assert result.prose_bytes == result.total_bytes

    def test_backtick_command_inside_cue_window_is_exempted(self):
        text = "This shape is denied. Use instead: `git status --short`."
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes > 0
        assert result.prose_bytes < result.total_bytes
        assert result.prose_bytes == result.total_bytes - result.exempt_bytes

    def test_backtick_outside_any_cue_window_is_not_exempted(self):
        # The cue word "instead" never appears, so `_cue_windows` yields no
        # window at all -- a stray backtick in ordinary prose (e.g. naming
        # a guard's own shape) must not be exempted.
        text = "This command was classified as a `destructive-rm` shape."
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_indented_block_after_cue_word_is_exempted(self):
        # Matches the real shape shipped guards use (e.g.
        # guard_plumbing_and_loops.py, guard_multiprobe_banner.py): cue word
        # immediately followed by indented lines, no intervening blank line
        # (a blank line right after the cue word truncates the
        # `_cue_windows` span to nothing -- see the overlapping-spans test
        # below for the same shape).
        text = "Use instead:\n  git status --short\n  git diff --stat\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes > 0
        assert result.prose_bytes < result.total_bytes

    def test_overlapping_backtick_and_indented_spans_are_not_double_subtracted(self):
        # A backtick span that sits inside an indented line, following a
        # cue word, in the same window -- the union must not subtract the
        # overlapping bytes twice, which would drive prose_bytes negative
        # (caught below by the floor-at-zero assertion) or simply wrong.
        text = "Use instead:\n  `git status --short`\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes <= result.total_bytes
        assert result.prose_bytes >= 0

    def test_diagnostic_prefixed_indented_line_inside_cue_window_is_not_exempted(self):
        # Review: coordinator:code-reviewer (Finding 1, guard-message-size-
        # discipline) -- a `Detected:` line is the "what was denied" half
        # of the duty-of-care contract, not an offered alternative, even
        # though it sits indented inside the same no-blank-line run as the
        # real alternative below it. It must count as prose regardless of
        # cue-window placement.
        text = (
            "Use instead:\n"
            "  git status --short\n"
            "  Detected: rm -rf -- no test file, directory, or node-id scope\n"
        )
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        detected_line = "  Detected: rm -rf -- no test file, directory, or node-id scope\n"
        assert result.exempt_bytes < result.total_bytes
        assert result.prose_bytes >= len(detected_line.encode("utf-8"))
        # The genuine alternative on the preceding line is still exempted.
        assert result.exempt_bytes > 0


class TestTailSubtractionByIdentity:
    """`operator_override_note`'s 2026-08-11 second reshape (same-day,
    guard-messages-point-to-docs-never-name plan) made its output
    independent of `env_var`/`reason_placeholder` -- a single fixed string
    every guard's tail either carries verbatim or doesn't. `_tail_bytes`
    no longer takes an `override_env_var` argument (nor does
    `measure_envelope`); this class was rewritten to match, replacing the
    old per-guard-argument identity tests (which asserted a WRONG env var
    did not match, and a mismatched `reason_placeholder` did not match --
    both premises this reshape retires, since there is no longer a
    per-call-site value to be wrong about)."""

    def test_tail_is_subtracted_by_identity(self):
        tail = operator_override_note(
            "COORDINATOR_ALLOW_TEST_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        text = "Advisory prose sentence. " + tail
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.tail_bytes == len(tail.encode("utf-8"))
        assert result.prose_bytes == result.total_bytes - result.exempt_bytes - result.tail_bytes
        assert result.prose_bytes < result.total_bytes

    def test_tail_is_identical_regardless_of_env_var_or_reason_placeholder(self):
        """The direct regression for the reshape: every call to the builder
        renders the SAME string now, so the tail subtracted is the same
        regardless of which env var (or reason_placeholder) a guard's call
        site happens to pass."""
        flag_tail = operator_override_note(
            "COORDINATOR_ALLOW_TEST_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        other_flag_tail = operator_override_note(
            "COORDINATOR_ALLOW_SOME_OTHER_GUARD", payload={"session_id": "sess-c1d-em"}
        )
        reason_tail = operator_override_note(
            "COORDINATOR_QUEUE_PUNT",
            payload={"session_id": "sess-c1d-em"},
            reason_placeholder="not now, doing X",
        )
        assert flag_tail == other_flag_tail == reason_tail

    def test_no_override_note_leaves_tail_bytes_zero(self):
        envelope = _envelope(additional_context="Advisory prose sentence, no override offered.")
        result = msz.measure_envelope(envelope)
        assert result.tail_bytes == 0


class TestBandResolution:
    def test_guard_band_enum_resolves_to_its_value(self):
        result = msz.measure_envelope(None, band=GuardBand.ADVISORY_REWRITE)
        assert result.band == "advisory-rewrite"

    def test_proxy_band_is_namespaced_and_distinct_from_guard_band_values(self):
        band = msz.proxy_band("write_guards")
        assert band == "directory:write_guards"
        assert band not in {b.value for b in GuardBand}

    def test_no_band_resolves_to_none(self):
        result = msz.measure_envelope(None)
        assert result.band is None


class TestFoundDataVsAuthoredProse:
    """`data_bytes`: found data (paths a guard is echoing back) charged
    separately from authored prose. See `_message_size` module docstring
    "FOUND DATA vs. AUTHORED PROSE" for the full abuse-resistance argument
    this class exists to prove."""

    def test_path_list_after_colon_is_charged_as_data_not_prose(self):
        text = (
            "BLOCKED: 'git checkout .' discards 1 uncommitted file(s):\n"
            "  state/x.json (load-bearing)\n"
            "\nDid you mean to scope it?\n"
            "  git checkout -- <your-paths>\n"
        )
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.data_bytes > 0
        assert result.prose_bytes == result.total_bytes - result.exempt_bytes - result.tail_bytes - result.data_bytes

    def test_data_bytes_flat_across_short_and_long_path(self):
        short_text = (
            "BLOCKED: 'git checkout .' discards 1 uncommitted file(s):\n"
            "  state/x.json (load-bearing)\n"
        )
        long_text = (
            "BLOCKED: 'git checkout .' discards 1 uncommitted file(s):\n"
            "  coordinator_core/bash_guards/tests/a-realistically-named-artifact.json (load-bearing)\n"
        )
        short_result = msz.measure_envelope(_envelope(additional_context=short_text))
        long_result = msz.measure_envelope(_envelope(additional_context=long_text))
        assert short_result.prose_bytes == long_result.prose_bytes

    def test_indented_prose_paragraph_without_path_tokens_stays_prose(self):
        # Anti-gaming: a paragraph of ordinary prose, indented under a
        # colon exactly like a genuine data block, but carrying no
        # path-like token on any line -- must NOT be reclassified as data.
        # An author cannot duck the cap merely by indenting sentences.
        text = (
            "Rationale:\n"
            "  This line explains why the command is denied for a purely\n"
            "  structural reason that has nothing to do with any path.\n"
            "  This third line keeps the paragraph going without ever\n"
            "  naming a file or directory anywhere in its own text.\n"
        )
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_one_prose_line_smuggled_into_path_block_disqualifies_whole_block(self):
        # A block where every line but one is a real path -- the single
        # non-path line must disqualify the WHOLE block back to prose,
        # not just itself, so an author cannot smuggle authored prose in
        # alongside genuine data and have it ride along uncharged.
        text = (
            "BLOCKED: discards 2 uncommitted file(s):\n"
            "  state/x.json (load-bearing)\n"
            "  this line has no path token at all\n"
        )
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    # -----------------------------------------------------------------
    # The four cases from the reopened review, reproduced directly. An
    # earlier revision keyed the per-line check off "line contains a
    # slash" (`_PATH_TOKEN_RE`), which passed the "prose, no slash" case
    # below but failed both cases that actually mattered: an author who
    # drops a slash into every line of a sentence ducked the cap entirely
    # (slash presence was ALSO what qualified the line, so it could never
    # disqualify anything), and a genuine path list with one slash-
    # bearing prose line smuggled in was never disqualified either. The
    # word-count-based `_is_path_entry_line` replaces it; these four
    # cases are the regression test for exactly that failure mode.
    # -----------------------------------------------------------------

    def test_legit_path_list_is_charged_as_data(self):
        text = (
            "BLOCKED: refusing 2 paths:\n"
            "  state/a.json (load-bearing)\n"
            "  state/b.json (peer-claimed by s1)\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes > 0
        assert result.prose_bytes < result.total_bytes

    def test_ordinary_prose_with_no_slash_stays_prose(self):
        text = (
            "BLOCKED: this is bad:\n"
            "  because the tree is shared and other people are working in it right now\n"
            "  and you would destroy their work without any way to get it back at all\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_prose_paragraph_with_slashes_in_every_line_stays_prose(self):
        # ABUSE case 1: an author pads every line of a sentence with a
        # slash-bearing word/word token specifically to duck the cap. Old
        # slash-presence check absorbed all 229 bytes of this as data;
        # word count must reject it -- each line is many words, not one
        # token.
        text = (
            "BLOCKED: this is bad:\n"
            "  the shared/tree carries every session's work and a sweep takes all of it\n"
            "  which means your/peer loses everything they had not yet committed anywhere\n"
            "  and there is no/recovery once the command completes so please do not do it\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_smuggled_prose_line_with_slash_disqualifies_whole_block(self):
        # ABUSE case 2: a genuine path entry plus one long editorial
        # sentence that happens to carry a slash token. The slash no
        # longer buys the smuggled line a pass -- it is still many words,
        # not one token, so it disqualifies the whole block.
        text = (
            "BLOCKED: refusing:\n"
            "  state/a.json (load-bearing)\n"
            "  here is a long editorial sentence with a slash/token smuggled into it ok\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_reason_with_comma_still_qualifies_as_path_entry(self):
        # The real `check_destructive_git_revert` call site joins two
        # reasons with a comma ("load-bearing, peer-claimed by <sid>") --
        # comma must not be treated as disqualifying sentence punctuation
        # the way `.`/`!`/`?`/`;` are.
        text = (
            "BLOCKED: refusing 1 path:\n"
            "  state/a.json (load-bearing, peer-claimed by s1)\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes > 0

    def test_reason_with_sentence_punctuation_disqualifies_the_line(self):
        text = (
            "BLOCKED: refusing 1 path:\n"
            "  state/a.json (this one really should not go. trust me)\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_data_block_overlapping_cue_window_is_not_double_counted(self):
        # The offered-alternative command block after "Use instead:" is
        # `_exempt_span_bytes`'s territory; even if it happened to carry a
        # path-like token, `data_bytes` must not also claim those bytes.
        text = "Use instead:\n  git checkout -- state/x.json\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes > 0
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes - result.exempt_bytes


class TestOverCap:
    def test_under_cap_prose_is_not_over_cap(self):
        envelope = _envelope(additional_context="short advisory")
        result = msz.measure_envelope(envelope)
        assert result.over_cap is False

    def test_over_cap_prose_is_flagged(self):
        envelope = _envelope(additional_context="x" * (msz.MESSAGE_PROSE_CAP_BYTES + 1))
        result = msz.measure_envelope(envelope)
        assert result.over_cap is True

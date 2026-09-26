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
      non-Windows hosts (plan's own "single most important correction");
  (d) relayed prose subtracted BY IDENTITY the same way (b) is -- a
      document another plane owns, carried verbatim, is not bytes this
      repo's author chose (`TestRelayedProse`).
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

    def test_none_envelope_is_zero_bytes_and_not_speaker(self):
        result = msz.measure_envelope(None)
        assert result.total_bytes == 0
        assert result.prose_bytes == 0
        assert result.exempt_bytes == 0
        assert result.tail_bytes == 0
        assert result.is_speaker is False
        assert result.over_cap is False


class TestSpeakerPredicateIsProseBytesNotEnvelopeIdentity:

    def test_zero_prose_non_none_envelope_is_not_a_speaker(self):
        envelope = _envelope(updated_input={"command": "git status"})
        assert envelope is not None
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
        text = "This command was classified as a `destructive-rm` shape."
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_indented_block_after_cue_word_is_exempted(self):
        text = "Use instead:\n  git status --short\n  git diff --stat\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes > 0
        assert result.prose_bytes < result.total_bytes

    def test_overlapping_backtick_and_indented_spans_are_not_double_subtracted(self):
        text = "Use instead:\n  `git status --short`\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes <= result.total_bytes
        assert result.prose_bytes >= 0

    def test_diagnostic_prefixed_indented_line_inside_cue_window_is_not_exempted(self):
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
        assert result.exempt_bytes > 0


class TestTailSubtractionByIdentity:

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
        text = (
            "BLOCKED: discards 2 uncommitted file(s):\n"
            "  state/x.json (load-bearing)\n"
            "  this line has no path token at all\n"
        )
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    # slash" (`_PATH_TOKEN_RE`), which passed the "prose, no slash" case

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
        text = (
            "BLOCKED: refusing:\n"
            "  state/a.json (load-bearing)\n"
            "  here is a long editorial sentence with a slash/token smuggled into it ok\n"
        )
        result = msz.measure_envelope(_envelope(additional_context=text))
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_reason_with_comma_still_qualifies_as_path_entry(self):
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
        text = "Use instead:\n  git checkout -- state/x.json\n"
        envelope = _envelope(additional_context=text)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes > 0
        assert result.data_bytes == 0
        assert result.prose_bytes == result.total_bytes - result.exempt_bytes


class TestRelayedProse:
    """`relayed_bytes`: a fixed document this repo neither authors nor can
    edit, carried verbatim inside a message, subtracted BY IDENTITY -- see
    the module docstring's "RELAYED PROSE vs. AUTHORED PROSE".

    Every test below pins the identity source through
    `_RELAYED_ROLE_APPEND_CACHE` rather than depending on whether this host
    happens to have a coordinator-claude plugin checkout: the class's
    behaviour is the thing under test, not the fleet's install state. The
    live-resolution path itself is covered by
    `test_live_resolution_fails_open_to_zero_on_an_unresolvable_host`
    below, and end-to-end by the corpus's own
    `cater_subagent_start/fire-missing-provisioning` cell.
    """

    _RELAYED = "## Your Role\n\nYou are a dispatched worker, not the EM.\nRun `x` and report back.\n"

    def _pin(self, monkeypatch, text: str) -> None:
        monkeypatch.setattr(msz, "_RELAYED_ROLE_APPEND", text)

    def test_relayed_document_is_not_charged_as_prose(self, monkeypatch):
        self._pin(monkeypatch, self._RELAYED)
        own = "Provisioning missed. Say so in your report."
        envelope = _envelope(additional_context=own + "\n\n" + self._RELAYED)
        result = msz.measure_envelope(envelope)
        assert result.relayed_bytes == len(self._RELAYED.encode("utf-8"))
        assert result.prose_bytes < len(self._RELAYED.encode("utf-8"))
        assert result.prose_bytes == (
            result.total_bytes
            - result.relayed_bytes
            - result.exempt_bytes
            - result.tail_bytes
            - result.data_bytes
        )

    def test_a_message_carrying_only_relayed_prose_still_speaks(self, monkeypatch):
        self._pin(monkeypatch, self._RELAYED)
        envelope = _envelope(additional_context="Missed.\n\n" + self._RELAYED)
        result = msz.measure_envelope(envelope)
        assert result.is_speaker is True
        assert 0 < result.prose_bytes < 50

    def test_absent_relayed_document_subtracts_nothing(self, monkeypatch):
        self._pin(monkeypatch, self._RELAYED)
        envelope = _envelope(additional_context="An ordinary advisory with no relayed document.")
        result = msz.measure_envelope(envelope)
        assert result.relayed_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_empty_identity_source_subtracts_nothing(self, monkeypatch):
        self._pin(monkeypatch, "")
        envelope = _envelope(additional_context="An ordinary advisory.")
        result = msz.measure_envelope(envelope)
        assert result.relayed_bytes == 0
        assert result.prose_bytes == result.total_bytes

    def test_paraphrase_of_the_relayed_document_is_still_charged(self, monkeypatch):
        """ABUSE RESISTANCE: the class is keyed by IDENTITY, not by
        resemblance. Near-identical text an author wrote themselves is
        prose, and stays charged."""
        self._pin(monkeypatch, self._RELAYED)
        near = self._RELAYED.replace("dispatched worker", "dispatched agent")
        envelope = _envelope(additional_context=near)
        result = msz.measure_envelope(envelope)
        assert result.relayed_bytes == 0
        assert result.prose_bytes > 0

    def test_a_second_copy_is_charged_as_prose(self, monkeypatch):
        self._pin(monkeypatch, self._RELAYED)
        envelope = _envelope(additional_context=self._RELAYED + self._RELAYED)
        result = msz.measure_envelope(envelope)
        assert result.relayed_bytes == len(self._RELAYED.encode("utf-8"))
        assert result.prose_bytes > 0

    def test_relayed_and_exempt_never_double_subtract(self, monkeypatch):
        self._pin(monkeypatch, self._RELAYED)
        envelope = _envelope(additional_context=self._RELAYED)
        result = msz.measure_envelope(envelope)
        assert result.exempt_bytes == 0
        assert result.relayed_bytes + result.prose_bytes == result.total_bytes

    def test_resolution_fails_open_to_empty_when_the_loader_raises(self, monkeypatch):
        from coordinator_core.hooks import cater_subagent_start

        def _boom() -> str:
            raise OSError("no coordinator-claude checkout on this host")

        monkeypatch.setattr(cater_subagent_start, "_load_role_append", _boom)
        assert msz._resolve_relayed_role_append() == ""

    def test_resolution_reads_the_real_artifact_not_a_copy(self, monkeypatch):
        from coordinator_core.hooks import cater_subagent_start

        monkeypatch.setattr(
            cater_subagent_start, "_load_role_append", lambda: "SENTINEL ROLE TEXT"
        )
        assert msz._resolve_relayed_role_append() == "SENTINEL ROLE TEXT"

    def test_identity_is_resolved_at_import_not_on_first_measurement(self):
        """THE REGRESSION THIS CLASS EXISTS TO PREVENT, and the reason
        `_RELAYED_ROLE_APPEND` is a module constant rather than a lazily
        filled cache.

        The loader resolves through `claude_config_dir()`, which reads
        `CLAUDE_CONFIG_DIR` -- and `guard_message_corpus.py` monkeypatches
        that variable to a scratch dir while a row fires. A cache filled on
        first use therefore latches `""` whenever the first measurement in
        the process lands inside one of those patched windows, and every
        later cell silently charges the relayed document as prose again.
        The first version of this code did exactly that: it measured
        correctly running two files and wrongly running the directory,
        because row order decided it.

        Resolving at import removes the window rather than narrowing it --
        pytest imports test modules during collection, before any fixture
        can patch anything. This test pins the SHAPE (a module-level
        constant, already resolved) rather than a value, so it stays
        meaningful on a host with no snippet at all.
        """
        assert isinstance(msz._RELAYED_ROLE_APPEND, str)
        assert not hasattr(msz, "_RELAYED_ROLE_APPEND_CACHE"), (
            "the lazily-filled cache is back; it latches whatever "
            "CLAUDE_CONFIG_DIR said at the first measurement"
        )

    def test_a_patched_config_dir_cannot_change_what_is_recognized(self, monkeypatch):
        """The same regression from the caller's side: patching
        `CLAUDE_CONFIG_DIR` mid-run (what the corpus fixtures do) must not
        change this module's answer, because the answer was already
        resolved."""
        before = msz._relayed_role_append_text()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/nonexistent-scratch-root")
        assert msz._relayed_role_append_text() == before


class TestOverCap:
    def test_under_cap_prose_is_not_over_cap(self):
        envelope = _envelope(additional_context="short advisory")
        result = msz.measure_envelope(envelope)
        assert result.over_cap is False

    def test_over_cap_prose_is_flagged(self):
        envelope = _envelope(additional_context="x" * (msz.MESSAGE_PROSE_CAP_BYTES + 1))
        result = msz.measure_envelope(envelope)
        assert result.over_cap is True

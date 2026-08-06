"""Tests for coordinator_core.bash_guards.guard_grep_via_bash (BX-6).

NARROWED 2026-07-30 (worklist row H11, docs/plans/2026-07-30-os-aware-guard-
advisory-defaults.md): this guard no longer denies on any platform, and no
longer advises on every composed/untranslatable match either -- see the
module's own docstring "GNU-ONLY PORTABILITY CHECK" section. Coverage:
  (a) substitutable residue (single-segment, recognized-short-flag
      grep-family invocation) -- now SILENT (``check()`` returns ``None``):
      `grep-via-bash-rewrite` (`dispatch_checks.check_grep_via_bash_rewrite`),
      registered EARLIER in `dispatch.py`'s guard chain and sharing this
      module's own classification/parsing helpers, already claims every
      command in this set as an `ADVISORY_REWRITE` -- this guard's own
      platform-conditioned deny/advise for the same set was provably
      unreachable in production (0 denies on either platform across the
      full corpus) and was removed.
  (b) composed pipelines/chains with a REAL two-segment `|` partial rewrite
      -- still advisory on every platform, never a deny anywhere, leading
      with the real alternative (design-as-offers).
  (c) composed pipelines/chains/untranslatable-flag commands with NEITHER a
      partial rewrite NOR a genuine GNU-only construct (`-P`/`-z`/a long
      option other than `--include`/`--exclude`) -- now SILENT: no
      actionable alternative exists, so the honest output is silence, not
      the ~2.2KB advisory this guard used to render unconditionally.
  (d) a genuine GNU-only construct (`-P`/`-z`/a non-`--include`/`--exclude`
      long option), with no partial rewrite available -- still advisory,
      naming the real GNU/BSD divergence (`_has_gnu_only_construct`).
  (e) precedence: a command that is simultaneously GREP_VIA_BASH and
      another shape (the plan's own canonical composite example) still
      fires THIS guard's classification, naming grep-via-bash, when it
      also qualifies for (b) or (d) above.
  (f) escape hatch: COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD=1 suppresses
      the guard entirely.
  (g) non-matches: a non-Bash tool, an empty command, and a command with
      no grep-family invocation at all all return None (silent allow).

Pure Python -- no shell spawns, no filesystem writes, no real platform
dependency.

Spec backlink: coordinator_core/bash_guards/guard_grep_via_bash.py
"""

from __future__ import annotations

import inspect

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.bash_guards import guard_grep_via_bash as guard


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
    }


def _result(cmd, **kwargs):
    return guard.check(_payload(cmd), **kwargs)


def _envelope(cmd, **kwargs):
    result = _result(cmd, **kwargs)
    assert result is not None, f"expected a non-None envelope for: {cmd!r}"
    return result["hookSpecificOutput"]


# ---------------------------------------------------------------------------
# (a) Substitutable residue -- now silent. `grep-via-bash-rewrite` already
# claims this set earlier in the chain; this guard's own deny/advise for it
# was provably unreachable and has been removed (H11(a)).
# ---------------------------------------------------------------------------


class TestSubstitutableResidueIsSilent:
    def test_windows_no_longer_denies(self):
        assert _result("grep -rn TODO src/", host_is_windows=True) is None

    def test_macos_no_longer_advises(self):
        assert _result("grep -rn TODO src/", host_is_windows=False) is None

    def test_case_insensitive_and_filenames_only_flags_also_silent(self):
        for cmd in ("grep -il TODO src/", "grep -rc TODO src/"):
            assert _result(cmd, host_is_windows=True) is None, cmd

    def test_egrep_fgrep_rg_all_silent(self):
        for binary in ("egrep", "fgrep", "rg"):
            cmd = "%s -rn TODO src/" % binary
            assert _result(cmd, host_is_windows=True) is None, cmd

    def test_still_shares_the_rewrite_target_bx16_would_emit(self):
        """Sanity check that this guard's own substitutability test still
        agrees with BX-16's rewrite check on what counts as substitutable
        -- even though this guard no longer speaks about it, a drift here
        would mean `_substitutable_rewrite` silently stopped matching
        `check_grep_via_bash_rewrite`'s own reach."""
        cmd = "grep -rn TODO src/"
        assert dispatch_checks.check_grep_via_bash_rewrite(cmd) is not None


# ---------------------------------------------------------------------------
# (b) Composed pipelines with a real two-segment `|` partial rewrite --
# still advisory, on every platform, never a deny.
# ---------------------------------------------------------------------------


class TestPartialRewriteStillAdvises:
    @staticmethod
    def _assert_advisory_both_platforms(cmd):
        for host_is_windows in (True, False):
            out = _envelope(cmd, host_is_windows=host_is_windows)
            assert out["permissionDecision"] == "allow", (cmd, host_is_windows)
            assert "additionalContext" in out
            assert "permissionDecisionReason" not in out

    def test_two_segment_pipe_offers_a_real_partial_rewrite(self):
        cmd = "grep -rn TODO src/ | wc -l"
        self._assert_advisory_both_platforms(cmd)
        out = _envelope(cmd, host_is_windows=True)
        ctx = out["additionalContext"]
        expected_grep_rewrite = dispatch_checks._bt_grep_python_rewrite(
            dispatch_checks._bt_grep_flags_and_operands(["grep", "-rn", "TODO", "src/"])
        )
        assert expected_grep_rewrite in ctx
        assert "wc -l" in ctx
        assert "one-fewer-fork" in ctx

    def test_two_stage_and_filter_edge_case_also_gets_partial_rewrite(self):
        """The pinned edge case `grep -l X | xargs grep -l Y` (two segments,
        first substitutable) qualifies for the partial rewrite -- piping
        into `xargs` verbatim is still correct, since `xargs` only cares
        about the bytes on its stdin. Also confirm BX-16's own rewrite
        offers nothing for this shape."""
        cmd = "grep -l X . | xargs grep -l Y"
        self._assert_advisory_both_platforms(cmd)
        assert dispatch_checks.check_grep_via_bash_rewrite(cmd) is None
        out = _envelope(cmd, host_is_windows=True)
        assert "xargs grep -l Y" in out["additionalContext"]

    def test_ordinary_downstream_flag_value_still_gets_partial_rewrite(self):
        cmd = "grep -rn TODO src/ | head -n 5"
        out = _envelope(cmd, host_is_windows=True)
        ctx = out["additionalContext"]
        assert "head -n 5" in ctx
        assert "one-fewer-fork" in ctx

    def test_message_leads_with_the_alternative(self):
        """design-as-offers: the alternative leads, not the violation."""
        out = _envelope("grep -rn TODO src/ | wc -l", host_is_windows=True)
        ctx = out["additionalContext"]
        assert ctx.index("one-fewer-fork replacement") < ctx.index("Else, Explore")

    def test_message_names_the_subagent_fallback(self):
        out = _envelope("grep -rn TODO src/ | wc -l", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "Explore or general-purpose subagent" in ctx

    def test_message_does_not_explain_why_the_in_process_answerer_declined(self):
        """The answerer's chain position is true and unusable: the reader
        cannot re-target it or reorder registration, so the explanation was
        cut (2026-07-30 message-prose trim) to the docstring. This asserts
        the negative so it cannot creep back into the reader's context."""
        ctx = _envelope("grep -rn TODO src/ | wc -l", host_is_windows=True)[
            "additionalContext"
        ]
        assert "registered ahead of this guard" not in ctx
        assert "already evaluated this exact invocation" not in ctx

    def test_message_names_its_own_override_route(self):
        """A code-review finding (M17, 2026-07-30): H11(b) had opted this guard's advisory
        out of `operator_override_note` entirely, leaving `_OVERRIDE_ENV_VAR`
        functional but undiscoverable from the message text. Restored as
        the same one-line SSOT pointer every sibling guard appends -- not
        the old ~1.1KB inline boilerplate, which stays gone."""
        out = _envelope("grep -rn TODO src/ | wc -l", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "blanket-disarm marker" not in ctx
        assert "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD" in ctx


# ---------------------------------------------------------------------------
# (c) No partial rewrite AND no genuine GNU-only construct -- silent.
# ---------------------------------------------------------------------------


class TestNoActionableAlternativeIsSilent:
    def test_piped_in_composed_is_silent(self):
        assert _result("cat file.txt | grep TODO", host_is_windows=True) is None

    def test_semicolon_chained_is_silent(self):
        assert _result("echo hi; grep -rn TODO src/", host_is_windows=True) is None

    def test_and_chained_is_silent(self):
        assert _result("grep -rn TODO src/ && echo done", host_is_windows=True) is None

    def test_three_segment_pipe_is_silent(self):
        assert _result("grep -rn TODO src/ | sort | wc -l", host_is_windows=True) is None

    def test_unrecognized_short_flag_is_silent(self):
        # -A (context lines) is not GNU-only -- it exists on BSD grep too.
        assert _result("grep -A3 TODO src/x.py", host_is_windows=True) is None

    def test_untranslatable_regex_dialect_is_silent(self):
        # BRE alternation is untranslatable to BX-16's rewrite, but is not
        # itself a GNU-only construct on this guard's narrow check.
        assert _result(r'grep -n "a\|b" file', host_is_windows=True) is None

    def test_downstream_expansion_declines_the_partial_rewrite_and_stays_silent(self):
        """A downstream token needing shell expansion (`$VAR`, a glob, a
        leading `~`, a backtick, redirection, brace expansion) makes
        `_partial_pipe_rewrite` decline -- with no GNU-only construct
        either, the whole command is now silent (previously advisory-only
        prose naming no action)."""
        for cmd in (
            "grep -rn TODO src/ | head -n $LINES",
            "grep -rln TODO src/ | xargs grep -l *.py",
            "grep -rn TODO src/ | tee ~/out.txt",
            "grep -rn TODO src/ | xargs echo `date`",
            "grep -rn TODO src/ | wc -l > out.txt",
            "grep -rn TODO src/ | tee out.{txt,bak}",
        ):
            assert _result(cmd, host_is_windows=True) is None, cmd

    def test_untranslatable_upstream_in_a_pipe_is_silent(self):
        cmd = "grep -A3 TODO src/x.py | wc -l"
        assert _result(cmd, host_is_windows=True) is None


# ---------------------------------------------------------------------------
# (d) A genuine GNU-only construct (no partial rewrite available) --
# still advisory, naming the real GNU/BSD divergence.
# ---------------------------------------------------------------------------


class TestGnuOnlyConstructStillAdvises:
    def test_dash_capital_p_on_single_segment_advises(self):
        # -P is not in `_GREP_SUBSTITUTABLE_SHORT_FLAGS`, so this is
        # untranslatable-reason, and -P is genuinely GNU-only.
        out = _envelope("grep -Pn TODO src/", host_is_windows=True)
        assert out["permissionDecision"] == "allow"
        assert "GNU-only" in out["additionalContext"]

    def test_dash_z_advises(self):
        out = _envelope("grep -rnz TODO src/", host_is_windows=True)
        assert "GNU-only" in out["additionalContext"]

    def test_long_option_on_the_denylist_advises(self):
        # `--group-separator` is on the C2 denylist (absent on BSD grep) --
        # unlike the deleted allowlist shape, this asserts a DENYLISTED
        # long option fires, not merely "not on a two-entry allowlist".
        out = _envelope("grep --group-separator=== TODO src/", host_is_windows=True)
        assert "GNU-only" in out["additionalContext"]

    def test_include_and_exclude_do_not_count_as_gnu_only(self):
        # Portable long options -- exist on BSD grep too (H11(c) evidence:
        # 8.36% of this guard's prior firing set, explicitly excluded).
        assert _result("grep -rn TODO --include=*.py src/", host_is_windows=True) is None

    def test_exclude_dir_does_not_count_as_gnu_only(self):
        # AC7: `--exclude-dir` is BSD-supported -- the allowlist shape this
        # denylist replaced misclassified it as GNU-only purely for not
        # being `--include`/`--exclude`. Must not fire.
        assert (
            _result("grep -rn TODO --exclude-dir=.git src/", host_is_windows=True)
            is None
        )

    def test_version_does_not_count_as_gnu_only(self):
        # AC7: `--version` is BSD-supported. Must not fire.
        assert _result("grep --version", host_is_windows=True) is None

    def test_color_does_not_count_as_gnu_only(self):
        # `--color` exists on BSD grep too -- was a false positive under
        # the deleted allowlist shape (fired purely for not being
        # `--include`/`--exclude`), corrected by the C2 denylist.
        assert _result("grep --color TODO src/", host_is_windows=True) is None

    def test_dash_o_and_dash_r_do_not_count_as_gnu_only(self):
        # `-o`/`-r` exist on BSD grep too -- semantic divergence, not a
        # portability hazard this guard should flag. `-o` alone is outside
        # `_GREP_SUBSTITUTABLE_SHORT_FLAGS`, so this is untranslatable-
        # reason (not chained), same gate as the rest of this class.
        assert _result("grep -ro TODO src/", host_is_windows=True) is None

    def test_dash_capital_z_advises(self):
        # `-Z` is a semantic collision (BSD: force zgrep behavior; GNU:
        # NUL-terminator), not just absence -- distinct denylist entry from
        # `-z`.
        out = _envelope("grep -rnZ TODO src/", host_is_windows=True)
        assert "GNU-only" in out["additionalContext"]

    def test_dash_capital_t_advises(self):
        out = _envelope("grep -rnT TODO src/", host_is_windows=True)
        assert "GNU-only" in out["additionalContext"]

    def test_gnu_only_construct_in_a_chained_command_still_advises(self):
        """The GNU-only check scans every grep-family segment of a chained
        command, not only a lone single-segment one -- `_REASON_CHAINED`
        short-circuits before flags are ever inspected, so this must be a
        genuinely separate scan."""
        out = _envelope("grep -Pn TODO src/ && echo done", host_is_windows=True)
        assert "GNU-only" in out["additionalContext"]

    def test_message_names_the_subagent_fallback(self):
        out = _envelope("grep -Pn TODO src/", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "Explore or general-purpose subagent" in ctx

    def test_message_names_its_own_override_route(self):
        """See the sibling assertion in TestComposedAdvisoryWithoutRewrite
        for the full rationale (a code-review finding, M17, 2026-07-30)."""
        out = _envelope("grep -Pn TODO src/", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "blanket-disarm marker" not in ctx
        assert "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD" in ctx


# ---------------------------------------------------------------------------
# (e) Precedence: GREP_VIA_BASH outranks every other shape, so this guard's
# classification (and, when it fires, its message) names grep, not banner
# or plumbing.
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_composite_banner_plus_grep_plus_head_is_silent_but_correctly_classified(self):
        # The plan's own canonical composite example: a banner-marked echo,
        # a piped grep, and a piped head all in one command. Composed
        # (piped both in and out), no partial rewrite (3+ segments), no
        # GNU-only construct -- silent under the new gate. Confirmed via
        # the classifier directly, since a silent `check()` result carries
        # no message to assert the shape name against.
        from coordinator_core.bash_guards._shape_classifier import (
            Shape as _Shape,
            classify_command as _classify_command,
        )

        cmd = 'echo "=== status ==="; cat log.txt | grep -i error | head'
        assert _result(cmd, host_is_windows=True) is None
        classification = _classify_command(cmd)
        assert classification.has_shape(_Shape.GREP_VIA_BASH)
        assert classification.primary.shape is _Shape.GREP_VIA_BASH

    def test_composite_with_a_gnu_only_flag_still_names_grep_via_bash(self):
        cmd = 'echo "=== status ==="; cat log.txt | grep -P error | head'
        out = _envelope(cmd, host_is_windows=True)
        ctx = out["additionalContext"]
        assert "grep-via-bash" in ctx
        assert "grep -P error" in ctx


# ---------------------------------------------------------------------------
# (d.1) Denylist membership rationale -- C2, 2026-08-01. Mechanical check
# that every DENYLISTED short flag and long option carries its own
# one-line rationale comment, not merely a bare literal in the frozenset.
# A future addition to either denylist with no accompanying rationale
# fails this gate rather than silently landing undocumented.
# ---------------------------------------------------------------------------


class TestGnuOnlyDenylistRationaleDocumented:
    @staticmethod
    def _source_around(const_name):
        """Return the source lines spanning from the comment block
        immediately preceding ``const_name``'s assignment through the
        assignment itself, so a rationale comment attached to that
        specific constant (not some unrelated constant elsewhere in the
        module) is what gets checked."""
        source_lines = inspect.getsource(guard).splitlines()
        assign_idx = next(
            i
            for i, line in enumerate(source_lines)
            if line.startswith("%s = " % const_name)
            or line.startswith("%s = frozenset" % const_name)
        )
        start = assign_idx
        while start > 0 and source_lines[start - 1].lstrip().startswith("#:"):
            start -= 1
        return "\n".join(source_lines[start : assign_idx + 1])

    def test_every_short_flag_entry_has_a_rationale_comment(self):
        block = self._source_around("_GNU_ONLY_SHORT_FLAGS")
        for flag in guard._GNU_ONLY_SHORT_FLAGS:
            assert ("`%s`" % flag) in block or ("`-%s`" % flag) in block, (
                flag,
                block,
            )

    def test_every_long_opt_entry_has_a_rationale_comment(self):
        block = self._source_around("_GNU_ONLY_LONG_OPTS")
        for opt in guard._GNU_ONLY_LONG_OPTS:
            assert ("`%s`" % opt) in block, (opt, block)

    def test_denylist_constants_are_frozensets(self):
        assert isinstance(guard._GNU_ONLY_SHORT_FLAGS, frozenset)
        assert isinstance(guard._GNU_ONLY_LONG_OPTS, frozenset)

    def test_derivation_date_and_platform_recorded(self):
        block = self._source_around("_GNU_ONLY_LONG_OPTS")
        assert "2026-08-01" in block
        assert "macOS 26.5" in block


# ---------------------------------------------------------------------------
# (f) Escape hatch
# ---------------------------------------------------------------------------


class TestOverrideEnvVar:
    def test_override_suppresses_partial_rewrite_advisory(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "1")
        assert _result("grep -rn TODO src/ | wc -l", host_is_windows=True) is None

    def test_override_suppresses_gnu_only_advisory(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "1")
        assert _result("grep -Pn TODO src/", host_is_windows=True) is None

    def test_wrong_value_does_not_suppress(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "yes")
        result = _result("grep -Pn TODO src/", host_is_windows=True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# (g) Non-matches
# ---------------------------------------------------------------------------


class TestNonMatches:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
        assert guard.check(payload) is None

    def test_empty_command_allows(self):
        assert _result("") is None

    def test_no_grep_family_binary_allows(self):
        assert _result("ls -la") is None

    def test_missing_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash"}) is None

    def test_unparseable_command_allows(self):
        # Unterminated quote -- tokenize_full_command returns None, so this
        # guard has nothing to classify and must not deny on that alone.
        assert _result("grep 'unterminated") is None

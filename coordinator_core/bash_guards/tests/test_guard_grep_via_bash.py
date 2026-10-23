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
  (b) the two-segment `|` partial rewrite (M10R: deleted -- it swaps the
      grep process for an interpreter process, a spawn delta of zero) --
      asserted ABSENT.
  (c) composed pipelines/chains/untranslatable-flag commands without a
      genuine GNU-only construct (`-P`/`-z`/a long
      option other than `--include`/`--exclude`) -- SILENT: no
      actionable alternative exists, so the honest output is silence, not
      the ~2.2KB advisory this guard used to render unconditionally.
  (d) a genuine GNU-only construct (`-P`/`-z`/a non-`--include`/`--exclude`
      long option) -- still advisory,
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

from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY
from coordinator_core.bash_guards import _dc_rewrites, dispatch_checks
from coordinator_core.bash_guards import guard_grep_via_bash as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _payload(command):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
    }


def _result(cmd, **kwargs):
    return _enveloped(guard.check)(_payload(cmd), **kwargs)


def _envelope(cmd, **kwargs):
    result = _result(cmd, **kwargs)
    assert result is not None, f"expected a non-None envelope for: {cmd!r}"
    return result["hookSpecificOutput"]


class TestRewriteScriptCompilesAndFallsBack:
    def test_windows_path_and_backslash_regex_compile(self):
        import shlex

        for cmd in (
            r"grep -n 'foo' 'C:\Users\me\new'",
            r"grep -n 'a\.b' 'C:\t\n'",
            "grep -n 'a.b' /tmp/x/src",
        ):
            rewrite, _ = guard._substitutable_rewrite(shlex.split(cmd))
            assert rewrite is not None, cmd
            compile(shlex.split(rewrite)[-1], "x", "exec")

    def test_rewrite_error_falls_back_to_original(self, monkeypatch):
        import shlex

        def boom(parsed):
            raise ValueError("boom")

        monkeypatch.setattr(guard, "_grep_python_rewrite", boom)
        rewrite, reason = guard._substitutable_rewrite(shlex.split("grep -n foo src"))
        assert rewrite is None and reason == guard._REASON_UNTRANSLATABLE

    def test_standalone_rewrite_that_will_not_compile_keeps_the_original(self, monkeypatch):
        cmd = "grep -n foo src"
        assert _enveloped(_dc_rewrites.check_grep_via_bash_rewrite)(cmd) is not None
        monkeypatch.setattr(
            _dc_rewrites, "_bt_grep_python_rewrite", lambda parsed: "python3 -c 'if ('"
        )
        assert _enveloped(_dc_rewrites.check_grep_via_bash_rewrite)(cmd) is None


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
        assert _enveloped(dispatch_checks.check_grep_via_bash_rewrite)(cmd) is not None


class TestPartialRewriteIsDeleted:
    def test_two_segment_pipe_offer_is_absent(self):
        for cmd in (
            "grep -rn TODO src/ | wc -l",
            "grep -l X . | xargs grep -l Y",
            "grep -rn TODO src/ | head -n 5",
        ):
            for host_is_windows in (True, False):
                assert _result(cmd, host_is_windows=host_is_windows) is None, cmd

    def test_partial_pipe_rewrite_symbols_are_gone(self):
        assert not hasattr(guard, "_partial_pipe_rewrite")
        assert not hasattr(guard, "_downstream_token_needs_expansion")
        assert not hasattr(guard, "_DOWNSTREAM_SAFE_TOKEN_RE")
        assert "one-fewer-fork" not in inspect.getsource(guard._composed_advisory)

    def test_gnu_only_pipe_advisory_carries_no_replacement_offer(self):
        out = _envelope("grep -Pn TODO src/ | wc -l", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "GNU-only" in ctx
        assert "one-fewer-fork" not in ctx
        assert "python3 -c" not in ctx
        assert "Explore or general-purpose subagent" in ctx


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
        assert _result("grep -A3 TODO src/x.py", host_is_windows=True) is None

    def test_untranslatable_regex_dialect_is_silent(self):
        assert _result(r'grep -n "a\|b" file', host_is_windows=True) is None

    def test_downstream_expansion_declines_the_partial_rewrite_and_stays_silent(self):
        """A downstream token needing shell expansion (`$VAR`, a glob, a
        leading `~`, a backtick, redirection, brace expansion) makes
        a pipe-downstream, with no GNU-only construct, is silent."""
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
        assert _result("grep -rn TODO --include=*.py src/", host_is_windows=True) is None

    def test_exclude_dir_does_not_count_as_gnu_only(self):
        assert (
            _result("grep -rn TODO --exclude-dir=.git src/", host_is_windows=True)
            is None
        )

    def test_version_does_not_count_as_gnu_only(self):
        assert _result("grep --version", host_is_windows=True) is None

    def test_color_does_not_count_as_gnu_only(self):
        assert _result("grep --color TODO src/", host_is_windows=True) is None

    def test_dash_o_and_dash_r_do_not_count_as_gnu_only(self):
        # `-o`/`-r` exist on BSD grep too -- semantic divergence, not a
        # portability hazard this guard should flag. `-o` alone is outside
        # `_GREP_SUBSTITUTABLE_SHORT_FLAGS`, so this is untranslatable-
        # reason (not chained), same gate as the rest of this class.
        assert _result("grep -ro TODO src/", host_is_windows=True) is None

    def test_dash_capital_z_advises(self):
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

    def test_message_points_to_the_override_route_doc(self):
        """See the sibling assertion (renamed the same way, same day) in
        TestComposedAdvisoryWithoutRewrite for the full rationale (a
        code-review finding, M17, 2026-07-30, updated 2026-08-11 for the
        guard-messages-point-to-docs-never-name plan's C2 render change)."""
        out = _envelope("grep -Pn TODO src/", host_is_windows=True)
        ctx = out["additionalContext"]
        assert "blanket-disarm marker" not in ctx
        assert "COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD" not in ctx
        # RETARGETED 2026-08-30 (DR-290 form 1 -> form 2): the message
        assert OVERRIDE_KEYS_DOC_DISPLAY in ctx


# ---------------------------------------------------------------------------
# (e) Precedence: GREP_VIA_BASH outranks every other shape, so this guard's
# classification (and, when it fires, its message) names grep, not banner
# or plumbing.
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_composite_banner_plus_grep_plus_head_is_silent_but_correctly_classified(self):
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


class TestOverrideEnvVar:
    def test_override_suppresses_gnu_only_pipe_advisory(self, monkeypatch):
        assert _result("grep -Pn TODO src/ | wc -l", host_is_windows=True) is not None
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "1")
        assert _result("grep -Pn TODO src/ | wc -l", host_is_windows=True) is None

    def test_override_suppresses_gnu_only_advisory(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "1")
        assert _result("grep -Pn TODO src/", host_is_windows=True) is None

    def test_wrong_value_does_not_suppress(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD", "yes")
        result = _result("grep -Pn TODO src/", host_is_windows=True)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestNonMatches:
    def test_non_bash_tool_allows(self):
        payload = {"tool_name": "Read", "tool_input": {"file_path": "x"}}
        assert _enveloped(guard.check)(payload) is None

    def test_empty_command_allows(self):
        assert _result("") is None

    def test_no_grep_family_binary_allows(self):
        assert _result("ls -la") is None

    def test_missing_tool_input_allows(self):
        assert _enveloped(guard.check)({"tool_name": "Bash"}) is None

    def test_unparseable_command_allows(self):
        assert _result("grep 'unterminated") is None


class TestPowerShellDialect:
    """Row 12, docs/reference/guard-dialect-coverage.md: `Select-String`/
    `sls` carry no PowerShell alias for grep, and this guard's rewrite
    payload is POSIX-grep-flag-shaped, so it cannot translate cleanly --
    the guard must declare SILENT rather than return a false clean for a
    Select-String/sls invocation it can see but not safely offer a rewrite
    for. A PowerShell command with no grep-family invocation at all stays a
    genuine, undeclared clean."""

    def _ps_payload(self, command):
        return {
            "tool_name": "PowerShell",
            "tool_input": {"command": command},
            "session_id": "sess1",
            "cwd": None,
        }

    def test_select_string_declares_silent_not_false_clean(self):
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = _enveloped(guard.check)(self._ps_payload("Select-String -Pattern TODO -Path ."))
        assert result is None
        assert was_silent("guard_grep_via_bash", silences)

    def test_sls_alias_declares_silent(self):
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = _enveloped(guard.check)(self._ps_payload("sls TODO ."))
        assert result is None
        assert was_silent("guard_grep_via_bash", silences)

    def test_non_grep_powershell_command_is_a_genuine_clean(self):
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = _enveloped(guard.check)(self._ps_payload("Get-ChildItem -Path ."))
        assert result is None
        assert not was_silent("guard_grep_via_bash", silences)


class TestPowerShellTokenizesOnce:
    """`_check_powershell` tokenizes once, heredoc-stripped, and hands the
    tokens to `classify_command`; the decline check and the classification
    agree on the heredoc policy."""

    _PS = {"tool_name": "PowerShell", "session_id": "sess1", "cwd": None}

    def _ps(self, command):
        return {**self._PS, "tool_input": {"command": command}}

    def test_one_tokenize_per_grep_recognized_call(self, monkeypatch):
        from coordinator_core.bash_guards import _shape_classifier as sc

        calls = []
        real = sc.tokenize_command

        def spy(*args, **kwargs):
            calls.append(args[0])
            return real(*args, **kwargs)

        monkeypatch.setattr(sc, "tokenize_command", spy)
        _enveloped(guard.check)(self._ps("grep -rnP TODO src"))
        assert len(calls) == 1

    def test_heredoc_body_does_not_trigger_select_string_decline(self):
        # The raw (unstripped) tokenize fails on this command; the stripped
        # one sees only `grep -rnP TODO src <<EOF`. The heredoc-stripped
        # policy is chosen for both the decline check and the
        # classification, so the GNU-only advisory fires.
        cmd = "grep -rnP TODO src <<EOF\ndata | Select-String x\nEOF"
        result = _enveloped(guard.check)(self._ps(cmd))
        assert result is not None
        assert "GNU-only" in result["hookSpecificOutput"]["additionalContext"]

    def test_real_select_string_still_declines(self):
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        with collecting() as silences:
            result = _enveloped(guard.check)(self._ps("Select-String -Pattern TODO -Path ."))
        assert result is None
        assert was_silent("guard_grep_via_bash", silences)

    def test_heredoc_body_select_string_is_data_not_a_decline(self):
        """A Select-String inside a heredoc body is stdin data, so the grep
        invocation is judged on its own and is not declared SILENT."""
        from coordinator_core.bash_guards._verdict import collecting, was_silent

        cmd = "grep -rn TODO src <<EOF\ndata | Select-String x\nEOF"
        with collecting() as silences:
            _enveloped(guard.check)(self._ps(cmd))
        assert not was_silent("guard_grep_via_bash", silences)


class TestClassifyCommandPreTokenizedInput:
    def test_tokens_kwarg_matches_self_tokenized_classification(self):
        from coordinator_core.bash_guards._dialect import Dialect
        from coordinator_core.bash_guards import _shape_classifier as sc

        cmd = "grep -rn TODO src <<EOF\nbody | x\nEOF"
        tokens = sc.tokenize_for_classification(cmd, Dialect.BASH)
        assert tokens == ["grep", "-rn", "TODO", "src", "<<EOF"]
        assert sc.classify_command(cmd, tokens=tokens) == sc.classify_command(cmd)

    def test_tokens_kwarg_skips_tokenize(self, monkeypatch):
        from coordinator_core.bash_guards import _shape_classifier as sc

        monkeypatch.setattr(
            sc, "tokenize_command", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
        )
        out = sc.classify_command("ignored", tokens=["grep", "-rn", "x", "."])
        assert out.tokens == ["grep", "-rn", "x", "."]

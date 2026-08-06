"""Pins the denial-of-service bound on
``coordinator_core.bash_guards._command_tokenizer`` -- see
``_MAX_TOKENIZABLE_COMMAND_CHARS``'s own comment for the measured quadratic
this bounds.

WHAT WENT WRONG (found 2026-08-05 by staff-eng review, pre-existing, not a
regression): ``shlex`` tokenization is quadratic in the length of the
LONGEST SINGLE TOKEN. The driver is ``shlex.shlex.read_token``'s
``self.token += nextchar``: CPython's in-place string-append optimization
requires the target's refcount to be 1, and an INSTANCE-ATTRIBUTE target is
referenced by both the evaluation stack and ``self.__dict__``, so every
character copies the whole accumulated token. A double-quoted argument is
ONE token however long it is, so ``git commit -m "<3.2 MB message>"`` --
an ordinary, entirely legitimate command shape -- stalled a PreToolUse hook
for ~105 seconds. ``punctuation_chars`` was the review's initial suspect and
is NOT the cause; the same curve is present without it.

THE FIX IS A CEILING THAT DENIES, AND THAT IS THE POINT. Past the ceiling
the tokenizer declines the work and returns its existing UNPARSEABLE signal
(``None`` / a single ``UNRESOLVED`` ``ResolvedCommand``). It does not
attempt a cheaper parse, and it does not guess. For a security guard,
converting a two-minute hang into a deny is the correct trade: the
fail-closed path already exists and is already exercised by every caller in
this package for unterminated quotes.

NEGATIVE SPEC: none of these tests may be "fixed" by raising
``_MAX_TOKENIZABLE_COMMAND_CHARS``. The constant is a security property, not
a tuning knob -- raising it re-opens the hang on the hook hot path. A test
here failing because a real command legitimately exceeds the ceiling is a
signal to shrink the command, not the bound.
"""

from __future__ import annotations

import time

from coordinator_core.bash_guards import _command_tokenizer
from coordinator_core.bash_guards._command_tokenizer import (
    ResolutionConfidence,
    resolve_command_positions,
    tokenize_full_command,
)

CEILING = _command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS


def _pad_to(prefix: str, suffix: str, total: int) -> str:
    """Build a command of exactly `total` characters shaped
    `<prefix><filler><suffix>` -- the worst case for the quadratic, since the
    filler is one unbroken token."""
    filler = "A" * (total - len(prefix) - len(suffix))
    cmd = prefix + filler + suffix
    assert len(cmd) == total, (len(cmd), total)
    return cmd


class TestCeilingBoundary:
    """The ceiling is inclusive: exactly-at-ceiling still tokenizes, so the
    boundary cannot silently drift by one and start denying commands the
    measurement said were fine."""

    def test_exactly_at_ceiling_still_tokenizes(self) -> None:
        cmd = _pad_to("echo ", "", CEILING)
        tokens = tokenize_full_command(cmd)
        assert tokens is not None
        assert tokens[0] == "echo"

    def test_one_character_past_ceiling_returns_none(self) -> None:
        cmd = _pad_to("echo ", "", CEILING + 1)
        assert tokenize_full_command(cmd) is None

    def test_ordinary_command_is_nowhere_near_the_ceiling(self) -> None:
        """Headroom check. The longest command-shaped string literal in this
        package's own test corpus at the time the ceiling was chosen was
        8,020 characters; the ceiling sits ~8x above it."""
        assert CEILING >= 8 * 8_020


class TestOverCeilingFailsClosed:
    """Over-ceiling input must land on the SAME fail-closed signal an
    unterminated quote already lands on -- no new fail-direction, no new
    caller contract."""

    def test_tokenize_returns_the_unparseable_signal(self) -> None:
        over = _pad_to("git commit -m '", "'", CEILING + 1)
        unterminated = "git commit -m 'never closed"
        assert tokenize_full_command(over) is tokenize_full_command(unterminated) is None

    def test_resolve_returns_single_unresolved_entry(self) -> None:
        over = _pad_to("git commit -m '", "'", CEILING + 1)
        resolved = resolve_command_positions(over)
        assert len(resolved) == 1
        assert resolved[0].confidence is ResolutionConfidence.UNRESOLVED
        assert resolved[0].tokens == [over]
        assert resolved[0].raw_tokens == [over]

    def test_resolve_short_circuits_before_the_prescan_walks(self) -> None:
        """`resolve_command_positions` gates ahead of `_strip_heredocs` and
        `_extract_command_substitutions`, which are linear passes over the
        whole text with per-substitution recursion on top. Gating only inside
        `tokenize_full_command` would leave those running on a multi-megabyte
        command."""
        over = "cat <<EOF; $(echo x); " + "A" * (CEILING + 1) + "\nEOF\n"
        resolved = resolve_command_positions(over)
        assert len(resolved) == 1, (
            "a substitution was still recursed into past the ceiling"
        )
        assert resolved[0].confidence is ResolutionConfidence.UNRESOLVED

    def test_guard_still_denies_an_over_ceiling_bare_commit(self, monkeypatch) -> None:
        """End-to-end: the fail-closed claim is checked THROUGH a guard's
        public verdict, not asserted about the tokenizer in isolation.

        Identity-seam wiring mirrors `test_block_subagent_commit.py`'s own
        `_subagent` helper -- the guard resolves subagent identity off a
        git-root back-pointer, so an unwired payload allows everything and
        would make this assertion vacuous."""
        from coordinator_core.bash_guards import block_subagent_commit as guard

        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
        monkeypatch.setattr(
            guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
        )
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type",
            lambda git_root, agent_id: "coordinator:executor",
        )
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": _pad_to('git commit -a -m "', '"', CEILING + 1)},
            "session_id": "sess1",
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
        }
        assert guard.check(payload) is not None, (
            "over-ceiling commit became MORE permissive"
        )

    def test_the_same_command_under_the_ceiling_also_denies(self, monkeypatch) -> None:
        """Control for the test above: proves the seam is actually wired and
        the deny is not an artifact of a payload the guard ignores."""
        from coordinator_core.bash_guards import block_subagent_commit as guard

        monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
        monkeypatch.setattr(
            guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123"
        )
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type",
            lambda git_root, agent_id: "coordinator:executor",
        )
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -a -m "x"'},
            "session_id": "sess1",
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": "coordinator:executor",
        }
        assert guard.check(payload) is not None


class TestDosBoundIsActuallyBinding:
    """The bound has to hold in wall-clock terms on the hook hot path, or it
    is decoration. Thresholds are deliberately loose (10x the measured
    figures) so this is a DoS regression detector, not a benchmark that
    flakes on a loaded CI box."""

    def test_multi_megabyte_command_returns_promptly(self) -> None:
        cmd = _pad_to("git commit -m '", "'", 3_200_000)
        start = time.perf_counter()
        assert tokenize_full_command(cmd) is None
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, (
            "3.2 MB command took %.3fs -- the DoS bound is not binding "
            "(pre-fix this shape took ~105s)" % elapsed
        )

    def test_multi_megabyte_command_resolves_promptly(self) -> None:
        cmd = _pad_to("git commit -m '", "'", 3_200_000)
        start = time.perf_counter()
        resolve_command_positions(cmd)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, "resolve path took %.3fs" % elapsed

    def test_at_ceiling_worst_case_stays_within_budget(self) -> None:
        """The ceiling was chosen as the largest power-of-two size at which
        the WORST-CASE shape (whole command in one token) keeps a full guard
        dispatch under a second. This pins the tokenizer's own share of that
        budget."""
        cmd = _pad_to("git commit -m '", "'", CEILING)
        start = time.perf_counter()
        assert tokenize_full_command(cmd) is not None
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, "at-ceiling worst case took %.3fs" % elapsed


class TestBelowCeilingIsUntouched:
    """Zero verdict changes below the threshold. The ceiling branch is a
    single length comparison ahead of otherwise-identical code, so this is
    true by construction -- pinned anyway, because "by construction" is what
    every silent drift was before it drifted."""

    def test_representative_shapes_tokenize_unchanged(self) -> None:
        cases = {
            "git commit -m 'subject line'": ["git", "commit", "-m", "subject line"],
            "cd /tmp && git status": ["cd", "/tmp", "&&", "git", "status"],
            'foo -- a.py 2>&1': ["foo", "--", "a.py", "2>&1"],
            "a & >b c": ["a", "&", ">b", "c"],
            "echo hi &>/dev/null": ["echo", "hi", "&>/dev/null"],
            "echo one\necho two": ["echo", "one", ";", "echo", "two"],
        }
        for cmd, expected in cases.items():
            assert tokenize_full_command(cmd) == expected, cmd

    def test_large_but_under_ceiling_command_still_classified(self) -> None:
        """A many-small-tokens command near the ceiling is LINEAR and must
        still be fully tokenized -- the ceiling is a size bound, never a
        "give up when it looks big" heuristic."""
        cmd = ("echo a b c; " * (CEILING // 12))[: CEILING - 1]
        tokens = tokenize_full_command(cmd)
        assert tokens is not None
        assert tokens.count("echo") > 1_000


class TestEveryDirectShlexSiteInheritsTheCeiling:
    """The ceiling on `tokenize_full_command` alone left the DoS OPEN: a full
    `dispatch.evaluate_payload_json` at 3.2 MB still took ~364 s afterwards,
    because ~16 call sites in this package call `shlex.split` DIRECTLY and
    never route through the shared tokenizer.

    Each site below is pinned as a pair -- at-ceiling behaves normally,
    past-ceiling takes that site's OWN pre-existing unparseable branch. The
    pairing is the point: it is what makes "no new fail-direction" a measured
    claim rather than an asserted one.

    NEGATIVE SPEC: a new direct `shlex.split`/`shlex.shlex` call site in
    `coordinator_core/bash_guards/` must call
    `_command_tokenizer.exceeds_tokenizable_ceiling` first, and must be added
    here. `test_no_unguarded_direct_shlex_site_is_added` below fails when one
    is not.
    """

    def test_shell_c_unwrap_payloads(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        under = "bash -c 'git commit --no-verify'"
        assert dc._shell_c_unwrap_payloads(under) == ["git commit --no-verify"]
        over = _pad_to("bash -c 'echo ", "'", CEILING + 1)
        assert dc._shell_c_unwrap_payloads(over) == []

    def test_line_has_shell_in_command_position(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        assert dc._line_has_shell_in_command_position("echo hi") is False
        over = _pad_to("echo ", "", CEILING + 1)
        assert dc._line_has_shell_in_command_position(over) is True

    def test_classify_heredoc_intro(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        over = _pad_to("cat ", "", CEILING + 1)
        assert dc._classify_heredoc_intro(over, over) in ("shell", "unknown"), (
            "both are the ALWAYS-VISIBLE fail-closed classes; only `prose` and "
            "`scriptable` can suppress a heredoc body, and neither may be "
            "reached past the ceiling"
        )

    def test_seg_excluding_freetext_operands(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        assert dc._seg_excluding_freetext_operands("git push -m subject") != "git push -m subject"
        over = _pad_to("git push -m ", "", CEILING + 1)
        assert dc._seg_excluding_freetext_operands(over) == over

    def test_seg_resolved_git_subcommand(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        assert dc._seg_resolved_git_subcommand("git push origin main") == "push"
        over = _pad_to("git push ", "", CEILING + 1)
        assert dc._seg_resolved_git_subcommand(over) is None

    def test_seg_confirmed_not_git_invocation(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        over = _pad_to("rm -f ", "", CEILING + 1)
        assert dc._seg_confirmed_not_git_invocation(over) is False

    def test_seg_has_git_bypass_flag(self) -> None:
        from coordinator_core.bash_guards import dispatch_checks as dc

        over = _pad_to("git commit --no-verify ", "", CEILING + 1)
        assert dc._seg_has_git_bypass_flag(over) is True, (
            "the over-ceiling branch must still run the raw over-inclusive scan"
        )

    def test_git_subcommand_and_remaining_for_segment(self) -> None:
        from coordinator_core.bash_guards import block_subagent_destructive_action as bda

        subcmd, parse_ok, _rest = bda._git_subcommand_and_remaining_for_segment(
            "git push origin main"
        )
        assert (subcmd, parse_ok) == ("push", True)
        over = _pad_to("git push ", "", CEILING + 1)
        assert bda._git_subcommand_and_remaining_for_segment(over) == (None, False, [])

    def test_extract_commit_trailing_pathspecs(self) -> None:
        from coordinator_core.bash_guards import commit_tripwires as tw

        assert tw._extract_commit_trailing_pathspecs("git commit -m x -- a.py") == ["a.py"]
        over = _pad_to("git commit -m ", " -- a.py", CEILING + 1)
        assert tw._extract_commit_trailing_pathspecs(over) is None

    def test_check_test_suite_invocation_tokens(self) -> None:
        from coordinator_core.bash_guards import check_test_suite_invocation as cts

        assert cts._tokens("pytest 'a b'") == ["pytest", "a b"]
        over = _pad_to("pytest ", "", CEILING + 1)
        assert cts._tokens(over) == over.split()

    def test_span_is_single_shell_token(self) -> None:
        from coordinator_core.bash_guards import guard_offer_invoke_params_stdin as gi

        under = "python3 -m coordinator_core.invoke ping '{\"a\": 1}'"
        assert gi._span_is_single_shell_token(under, '{"a": 1}') == (
            gi._CROSS_CHECK_CONFIRMED
        )
        over = _pad_to("python3 -m coordinator_core.invoke ping '{\"a\": \"", "\"}'", CEILING + 1)
        assert gi._span_is_single_shell_token(over, "x") == gi._CROSS_CHECK_TOO_LARGE

    def test_alternative_liveness_backtick_span(self) -> None:
        from coordinator_core.bash_guards import _alternative_liveness as alt

        over = _pad_to("git status ", "", CEILING + 1)
        alt._classify_backtick_span(over)  # must return, not hang

    def test_shell_c_payload_past_ceiling_returns_promptly_through_check(self) -> None:
        """`check_test_suite_invocation._strip_command_prefix`'s `sh -c`
        recursion re-split its payload with an UNGUARDED `shlex.split`, on the
        false premise that `_tokens` bounds it. `_tokens`'s over-ceiling
        fallback is `segment.split()` -- whitespace-only -- so a payload with
        no internal whitespace arrives here at full length and pays the full
        quadratic. Measured at HEAD through `check()` itself: 200 KB -> 0.81 s,
        800 KB -> 15.4 s, on the raw-`tool_input` dispatch hot path.

        Routed through `check()` rather than the private helper on purpose:
        the claim under test is reachability from a Bash payload, which a
        direct helper call would assume rather than demonstrate."""
        from coordinator_core.bash_guards import check_test_suite_invocation as cts

        cmd = _pad_to("bash -c 'pytest.", "'", 800_000)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "agent_id": "agent-under-test",
        }
        start = time.perf_counter()
        cts.check(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, (
            "800 KB `bash -c '<no-whitespace payload>'` took %.3fs through "
            "check() -- the ceiling is not binding on the `sh -c` re-split "
            "(pre-fix this shape took ~15.4s, and 200 KB took ~0.81s)" % elapsed
        )

    def test_shell_c_payload_past_ceiling_keeps_the_runner_visible(self) -> None:
        """Fail direction, pinned. The sibling `ValueError` branch here is
        `break`, which is fail-OPEN for detection -- it leaves the interpreter
        in command position and nothing classifies. Routing the ceiling there
        would have flipped a live DENY to an allow for the one over-ceiling
        shape whose basename IS a runner, so the ceiling branch quote-strips
        and whitespace-splits (linear, and a no-op on a token that by
        construction holds no whitespace) instead of giving up."""
        from coordinator_core.bash_guards import check_test_suite_invocation as cts

        cmd = "bash -c '" + ("A" * (CEILING + 1)) + "/pytest'"
        start = time.perf_counter()
        result = cts.check({
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "agent_id": "agent-under-test",
        })
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, "took %.3fs" % elapsed
        assert isinstance(result, dict), "over-ceiling runner basename must still deny"
        assert (
            result["hookSpecificOutput"]["permissionDecision"] == "deny"
        ), result

    def test_no_unguarded_direct_shlex_site_is_added(self) -> None:
        """Structural pin. Walks the package's own AST for `shlex.split` /
        `shlex.shlex` calls and requires each enclosing function to mention
        `exceeds_tokenizable_ceiling`, so a NEW site cannot quietly
        reintroduce the hang.

        The exemption list is deliberately spelled out with a reason each,
        not a wildcard -- an addition to it is a review event."""
        import ast
        import pathlib

        exempt = {
            # Defines the ceiling; the comparison itself lives here.
            ("_command_tokenizer.py", "tokenize_full_command"),
            # `check_test_suite_invocation._strip_command_prefix` was exempted
            # here on the claim that a token of a ceilinged `_tokens()` result
            # cannot exceed the ceiling. That claim was FALSE -- `_tokens`'s
            # over-ceiling fallback is `segment.split()`, which bounds the
            # segment and not the token -- and the exemption was worse than the
            # missing guard because it asserted safety. It now carries a real
            # ceiling check; see `test_shell_c_payload_past_ceiling_...` below.
        }

        root = pathlib.Path(__file__).resolve().parents[1]
        unguarded = []
        sources = [
            p
            for p in sorted(root.rglob("*.py"))
            if "tests" not in p.parts and "__pycache__" not in p.parts
        ]
        assert len(sources) > 30, "package walk found almost nothing -- the glob is wrong"
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            funcs = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("split", "shlex")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "shlex"
                ):
                    continue
                enclosing = [
                    f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
                ]
                enclosing.sort(key=lambda f: (f.end_lineno or f.lineno) - f.lineno)
                name = enclosing[0].name if enclosing else "<module>"
                if (path.name, name) in exempt:
                    continue
                body = ast.dump(enclosing[0]) if enclosing else ""
                if "exceeds_tokenizable_ceiling" not in body:
                    unguarded.append("%s::%s" % (path.name, name))

        assert not unguarded, (
            "direct shlex call site(s) with no ceiling check -- each one "
            "re-opens the multi-second hook hang for whatever it parses: %s"
            % ", ".join(sorted(set(unguarded)))
        )


class TestSentinelRemovalOverCeilingDenies:
    """The removal guard's `tokens is None` branch returned ADVISORY (which
    renders as `permissionDecision: allow`), so padding a command past the
    ceiling bought an allow from the guard the padding defeats. Over-ceiling
    + a raw-text sentinel mention now DENIES; every other unparseable cause
    keeps the module's documented ADVISORY posture."""

    def test_over_ceiling_mentioning_the_sentinel_denies(self) -> None:
        from coordinator_core.bash_guards import _sentinel_removal_guard as srg

        det = srg.SentinelRemovalDetector(".dev-repo-marker")
        cmd = _pad_to("rm .dev-repo-marker '", "'", CEILING + 1)
        verdict, reason, reason_class = det.evaluate(cmd)
        assert verdict == srg.VERDICT_DENY, (verdict, reason)
        assert reason == srg._REASON_OVER_CEILING % ".dev-repo-marker"
        assert ".dev-repo-marker" in reason, (
            "the deny text must name the sentinel, like every sibling reason "
            "in that module -- a static reason string drops it"
        )

    def test_below_ceiling_unparseable_keeps_the_advisory_posture(self) -> None:
        from coordinator_core.bash_guards import _sentinel_removal_guard as srg

        det = srg.SentinelRemovalDetector(".dev-repo-marker")
        verdict, _reason, _cls = det.evaluate("rm .dev-repo-marker 'never closed")
        assert verdict == srg.VERDICT_ADVISORY, (
            "the ceiling fix must not tighten the ordinary unterminated-quote "
            "shape -- that is a below-ceiling verdict move"
        )

    def test_over_ceiling_not_mentioning_the_sentinel_still_allows(self) -> None:
        from coordinator_core.bash_guards import _sentinel_removal_guard as srg

        det = srg.SentinelRemovalDetector(".dev-repo-marker")
        verdict, _reason, _cls = det.evaluate(_pad_to("echo '", "'", CEILING + 1))
        assert verdict == srg.VERDICT_ALLOW, (
            "over-ceiling alone must not deny -- this guard is not a "
            "command-length policy"
        )

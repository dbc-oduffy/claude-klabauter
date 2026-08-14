"""Tests for coordinator_core.bash_guards._shape_classifier.

Three failure classes this pins, per DoE
``docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md`` C2's own test
requirement:

1. **Quoted-metacharacter correctness** (``TestQuotedMetacharacters``) --
   a `;`/`|`/`&` INSIDE a quoted argument must not be treated as a real
   command separator. This is "the case regex gets wrong" the plan chunk
   names explicitly: a naive ``re.split(r"[;&|]")`` over the raw command
   text would fracture a single ``grep -n "a;b" file`` into bogus
   segments. This module never does that -- it classifies over
   ``tokenize_full_command``'s already-quote-aware segmentation.

2. **No drive-letter-adjacent logic to trip** (``TestNoDriveLetterLogic``)
   -- this module contains no ``[A-Za-z]:`` -style matcher at all (the
   precedent bug: ``[A-Za-z]:[/\\]`` matching the ``s:`` in ``https://``,
   fixed in ``guard-settings-json-write.py:105``), so a URL inside a
   command cannot mis-trip anything here. Asserted by classifying commands
   containing ``https://`` and confirming only the expected shape(s) fire.

3. **Precedence over simultaneous matches** (``TestPrecedence``) -- the
   plan's own canonical example, a single command that is at once a
   grep-via-Bash, a multi-probe banner, and head/tail plumbing, must
   resolve to the documented precedence winner
   (``GREP_VIA_BASH > MULTI_PROBE_BANNER > HEAD_TAIL_PLUMBING > FOR_LOOP >
   WHILE_READ_LOOP > FIND_EXEC_XARGS``) with the other two shapes present,
   in order, in ``.residue`` -- never an arbitrary pick and never a dropped
   match.

``TestWhileReadLoop`` covers the sixth shape added by
``docs/plans/2026-08-10-the-one-fan-out-shape-the-classifier-nev.md``: the
four live spellings (AC-3), the Anti-scope negatives (a quoted mention, a
heredoc body, ``while true`` with no ``read``, a fragment missing
``do``/``done``), and the FOR_LOOP-primary/while-read-residue precedence
case.
"""

from __future__ import annotations

from coordinator_core.bash_guards._shape_classifier import (
    Shape,
    SHAPE_PRECEDENCE,
    classify_command,
)


class TestQuotedMetacharacters:
    def test_semicolon_inside_quoted_grep_pattern_stays_one_segment(self) -> None:
        result = classify_command('grep -n "a;b" file.txt')
        assert result.tokens is not None
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH
        # The quoted `;` must not have fractured this into extra segments
        # for the banner detector to (wrongly) see.
        assert not result.has_shape(Shape.MULTI_PROBE_BANNER)

    def test_pipe_inside_quoted_grep_alternation_is_not_a_pipe_segment(self) -> None:
        result = classify_command('grep -E "(A|B)" file.txt')
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH
        # A real pipe segment (e.g. `| head`) would also fire head/tail
        # plumbing; the quoted `|` here must not be mistaken for one.
        assert not result.has_shape(Shape.HEAD_TAIL_PLUMBING)

    def test_quoted_for_loop_keywords_as_data_do_not_classify_as_for_loop(
        self,
    ) -> None:
        result = classify_command('echo "for x in y; do z; done"')
        assert not result.has_shape(Shape.FOR_LOOP)

    def test_ampersand_inside_quoted_argument_is_not_a_background_separator(
        self,
    ) -> None:
        result = classify_command('grep -n "foo & bar" file.txt')
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH
        assert result.tokens is not None
        # One segment only: the quoted `&` did not split the command.
        assert result.tokens.count("&") == 0


class TestNoDriveLetterLogic:
    def test_https_url_in_grep_pattern_classifies_as_plain_grep(self) -> None:
        result = classify_command('grep -i "https://example.com" access.log')
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH

    def test_https_url_piped_through_grep_does_not_add_spurious_shapes(self) -> None:
        result = classify_command(
            "curl -s https://example.com/data.json | grep -i error"
        )
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_windows_path_with_colon_in_argument_does_not_trip_a_shape(self) -> None:
        result = classify_command('grep -n "C:\\Users\\x\\file.txt" notes.txt')
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)


class TestPrecedence:
    def test_canonical_triple_overlap_resolves_grep_first(self) -> None:
        # The plan's own canonical example
        # (state/plan-sidecars/2026-07-28-bash-tax-negative-space.md:69):
        # a banner echo, feeding a pipeline that greps and heads.
        cmd = 'echo "=== git status ==="; git status | grep -i modified | head'
        result = classify_command(cmd)

        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH

        residue_shapes = [m.shape for m in result.residue]
        assert residue_shapes == [Shape.MULTI_PROBE_BANNER, Shape.HEAD_TAIL_PLUMBING]

        assert result.matched_shapes == (
            Shape.GREP_VIA_BASH,
            Shape.MULTI_PROBE_BANNER,
            Shape.HEAD_TAIL_PLUMBING,
        )

    def test_find_exec_and_grep_both_present_as_separate_segments_grep_wins(
        self,
    ) -> None:
        cmd = "grep -rn TODO src/; find . -name '*.log' -exec rm {} \\;"
        result = classify_command(cmd)
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH
        assert result.has_shape(Shape.FIND_EXEC_XARGS)
        assert result.residue[0].shape is Shape.FIND_EXEC_XARGS

    def test_xargs_invoked_grep_is_find_exec_xargs_not_grep_via_bash(self) -> None:
        # `grep` here is xargs's ARGUMENT, not a top-level invoked segment
        # (tokens[0] of that segment is `xargs`) -- this is the find-
        # exec/xargs shape (3.5% of the corpus), a distinct habit from a
        # directly-invoked `grep` segment, and the classifier must not
        # conflate the two.
        result = classify_command("find . -type f | xargs grep -l TODO")
        assert result.matched_shapes == (Shape.FIND_EXEC_XARGS,)

    def test_precedence_tuple_matches_documented_order(self) -> None:
        assert SHAPE_PRECEDENCE == (
            Shape.GREP_VIA_BASH,
            Shape.MULTI_PROBE_BANNER,
            Shape.HEAD_TAIL_PLUMBING,
            Shape.FOR_LOOP,
            Shape.WHILE_READ_LOOP,
            Shape.FIND_EXEC_XARGS,
        )

    def test_for_loop_and_while_read_both_present_for_loop_wins(self) -> None:
        cmd = (
            'for f in *.py; do wc -l "$f"; done; '
            "cat items.txt | while read x; do echo \"$x\"; done"
        )
        result = classify_command(cmd)
        assert result.primary is not None
        assert result.primary.shape is Shape.FOR_LOOP
        assert result.residue[0].shape is Shape.WHILE_READ_LOOP

    def test_matches_are_always_in_precedence_order_never_arbitrary(self) -> None:
        cmd = 'echo "=== probe one ==="; echo "=== probe two ==="; find . -exec cat {} \\;'
        result = classify_command(cmd)
        ranks = [SHAPE_PRECEDENCE.index(m.shape) for m in result.matches]
        assert ranks == sorted(ranks)


class TestEachShapeInIsolation:
    def test_grep_via_bash_bare(self) -> None:
        result = classify_command("grep -rn TODO src/")
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_grep_family_alias_ripgrep(self) -> None:
        result = classify_command("rg -n TODO src/")
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_grep_exe_windows_launcher_suffix_still_matches(self) -> None:
        result = classify_command("grep.exe -n TODO src\\file.txt")
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_multi_probe_banner_needs_at_least_three_segments(self) -> None:
        # Banner label plus exactly one probe -- NOT the multi-probe shape
        # (see _MIN_BANNER_SEGMENTS docstring).
        result = classify_command('echo "=== status ==="; git status')
        assert not result.has_shape(Shape.MULTI_PROBE_BANNER)

    def test_multi_probe_banner_three_or_more_segments(self) -> None:
        result = classify_command(
            'echo "=== probes ==="; git status; git log -1; git branch'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

    def test_head_tail_plumbing_requires_a_pipe(self) -> None:
        # A bare, un-piped head is an ordinary bounded read, not plumbing.
        result = classify_command("head -n 20 file.txt")
        assert not result.has_shape(Shape.HEAD_TAIL_PLUMBING)

    def test_head_tail_plumbing_piped(self) -> None:
        result = classify_command("git log --oneline | tail -50")
        assert result.matched_shapes == (Shape.HEAD_TAIL_PLUMBING,)

    def test_for_loop_basic(self) -> None:
        result = classify_command('for f in *.py; do wc -l "$f"; done')
        assert result.matched_shapes == (Shape.FOR_LOOP,)

    def test_for_loop_requires_do_and_done_not_just_leading_for(self) -> None:
        # "for" as a bare unquoted first token with no do/done is not a
        # complete loop signature.
        result = classify_command("for f in *.py")
        assert not result.has_shape(Shape.FOR_LOOP)

    def test_find_exec(self) -> None:
        result = classify_command('find . -name "*.log" -exec rm {} \\;')
        assert result.matched_shapes == (Shape.FIND_EXEC_XARGS,)

    def test_bare_xargs_pipe(self) -> None:
        result = classify_command("cat files.txt | xargs rm")
        assert result.matched_shapes == (Shape.FIND_EXEC_XARGS,)

    def test_no_shape_matches_plain_command(self) -> None:
        result = classify_command("git status")
        assert result.matched_shapes == ()
        assert result.primary is None
        assert result.residue == ()


class TestHeredocBodyIsNotShapeMaterial:
    """A heredoc body is stdin DATA, never shell command text. Once the
    shared tokenizer started treating a bare newline as a segment boundary
    (2026-07-30, closing the multi-line-command bypass), a heredoc body
    written via `cat <<EOF ... EOF` would fragment at every line break
    unless heredoc bodies are stripped before classification -- and prose
    merely describing one of these six shapes would then classify as one.
    `classify_command` strips heredoc bodies first (mirroring
    `block_worktree_creation.check()`), so none of these are matched."""

    def test_heredoc_prose_mentioning_find_exec_is_not_a_match(self) -> None:
        cmd = (
            "cat <<'EOF' > notes.md\n"
            "example: find . -name \"*.log\" -exec rm {} \\;\n"
            "EOF"
        )
        result = classify_command(cmd)
        assert not result.has_shape(Shape.FIND_EXEC_XARGS)

    def test_heredoc_prose_mentioning_a_for_loop_is_not_a_match(self) -> None:
        cmd = (
            "cat <<'EOF' > notes.md\n"
            "for f in *.py; do wc -l \"$f\"; done\n"
            "EOF"
        )
        result = classify_command(cmd)
        assert not result.has_shape(Shape.FOR_LOOP)

    def test_heredoc_prose_mentioning_grep_is_not_a_match(self) -> None:
        cmd = (
            "cat <<'EOF' > notes.md\n"
            "grep -rn TODO src/\n"
            "EOF"
        )
        result = classify_command(cmd)
        assert not result.has_shape(Shape.GREP_VIA_BASH)

    def test_real_invocation_after_heredoc_still_matches(self) -> None:
        """The heredoc-stripping fix must not swallow a genuine invocation
        that follows the heredoc on its own line."""
        cmd = (
            "cat <<'EOF' > notes.md\n"
            "some prose\n"
            "EOF\n"
            "grep -rn TODO src/"
        )
        result = classify_command(cmd)
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)


class TestWhileReadLoop:
    """The sixth shape (AC-1/AC-2/AC-3/AC-7). Positives cover the four live
    spellings; negatives cover each Anti-scope bullet."""

    def test_pipe_fed(self) -> None:
        result = classify_command('cat f | while read x; do echo "$x"; done')
        assert result.matched_shapes == (Shape.WHILE_READ_LOOP,)

    def test_safe_idiom_with_ifs_assignment_between_while_and_read(self) -> None:
        # The canonical safe spelling puts an assignment between `while`
        # and `read` -- the adjacency trap named in the plan's Anti-scope.
        result = classify_command('while IFS= read -r x; do echo "$x"; done')
        assert result.matched_shapes == (Shape.WHILE_READ_LOOP,)

    def test_redirect_fed(self) -> None:
        result = classify_command('while read x; do echo "$x"; done < f')
        assert result.matched_shapes == (Shape.WHILE_READ_LOOP,)

    def test_process_substitution_fed(self) -> None:
        result = classify_command(
            'while read x; do echo "$x"; done < <(git diff --name-only)'
        )
        assert result.matched_shapes == (Shape.WHILE_READ_LOOP,)

    def test_quoted_mention_does_not_classify(self) -> None:
        result = classify_command('echo "while read f; do x; done"')
        assert not result.has_shape(Shape.WHILE_READ_LOOP)

    def test_heredoc_body_mentioning_while_read_is_not_a_match(self) -> None:
        cmd = (
            "cat <<'EOF' > notes.md\n"
            "while read x; do echo \"$x\"; done\n"
            "EOF"
        )
        result = classify_command(cmd)
        assert not result.has_shape(Shape.WHILE_READ_LOOP)

    def test_while_true_with_no_read_is_a_poll_loop_not_while_read(self) -> None:
        result = classify_command("while true; do sleep 1; done")
        assert not result.has_shape(Shape.WHILE_READ_LOOP)

    def test_while_read_fragment_with_no_do_done_does_not_classify(self) -> None:
        result = classify_command("while read x")
        assert not result.has_shape(Shape.WHILE_READ_LOOP)


class TestUnparseableCommand:
    def test_unterminated_quote_returns_none_tokens_and_no_matches(self) -> None:
        result = classify_command('grep -n "unterminated')
        assert result.tokens is None
        assert result.matches == ()
        assert result.primary is None

    def test_trailing_backslash_returns_none_tokens(self) -> None:
        result = classify_command("grep -n foo file.txt \\")
        assert result.tokens is None
        assert result.matches == ()

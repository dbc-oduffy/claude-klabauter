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

from coordinator_core.bash_guards import _command_tokenizer
from coordinator_core.bash_guards._dialect import Dialect
from coordinator_core.bash_guards._shape_classifier import (
    Shape,
    SHAPE_PRECEDENCE,
    classify_command,
)
from coordinator_core.bash_guards._verdict import collecting


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
            Shape.PIPELINE_FOREACH_OBJECT,
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


class TestMultiProbeBannerIsSemanticNotJustFormatting:
    """``state/audits/2026-08-14-boot-payload-baseline.md`` § "The
    false-positive matcher": the pre-fix predicate was pure ``echo`` +
    ``===`` + 3-segments SHAPE detection, with no check that the other
    segments actually re-derive a harness-known session fact. These pin
    the true positive still firing and the audit's confirmed false
    positive no longer firing.
    """

    def test_true_positive_labelled_git_reprobe_still_fires(self) -> None:
        result = classify_command(
            'echo "=== git status ==="; git status; git log'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

    def test_confirmed_false_positive_labelled_measurement_does_not_fire(
        self,
    ) -> None:
        # From the baseline audit verbatim: a legitimate labelled
        # multi-file measurement, not a session-fact re-derivation.
        result = classify_command(
            'echo "=== EM snippets ==="; wc -c a.md b.md; ls x'
        )
        assert not result.has_shape(Shape.MULTI_PROBE_BANNER)

    def test_mixed_probe_and_non_probe_segments_does_not_fire(self) -> None:
        # Not PURELY the re-derive-known-facts shape -- stays silent
        # rather than misnaming a mixed command.
        result = classify_command('echo "=== x ==="; git status; wc -l a')
        assert not result.has_shape(Shape.MULTI_PROBE_BANNER)

    def test_canonical_pipeline_probe_with_plumbing_still_fires_as_residue(
        self,
    ) -> None:
        # The plan's own canonical overlap example: a SINGLE probe
        # (`git status`) piped through grep/head plumbing -- the pipe
        # continuations must not be treated as additional non-probe
        # segments that disqualify the banner match.
        cmd = 'echo "=== git status ==="; git status | grep -i modified | head'
        result = classify_command(cmd)
        assert result.has_shape(Shape.MULTI_PROBE_BANNER)

    def test_probe_family_all_five_binaries_fire(self) -> None:
        result = classify_command(
            'echo "=== facts ==="; pwd; whoami; date; uname'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

    def test_sudo_prefixed_probe_still_fires(self) -> None:
        # Review: coordinator:code-reviewer (Finding 2) -- a `sudo`-wrapped
        # probe is still a genuine session-fact re-derivation.
        result = classify_command(
            'echo "=== facts ==="; sudo git status; pwd; whoami'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

    def test_env_var_prefixed_probe_still_fires(self) -> None:
        result = classify_command(
            'echo "=== facts ==="; env FOO=bar git status; pwd; whoami'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

    def test_env_multiple_assignments_prefixed_probe_still_fires(self) -> None:
        result = classify_command(
            'echo "=== facts ==="; env FOO=bar BAZ=qux git status; pwd; whoami'
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.MULTI_PROBE_BANNER

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


class TestPowerShellShapeSet:
    """C2 of pln-the-shape-classifier-reaches-a-e743e5 -- the six-shape
    POWERSHELL table entry (D2). AC6 (the four blind-spawn shapes classify),
    AC7 (in-process cmdlets are a hard-gate negative), AC8 (WHILE_READ_LOOP
    stays absent), AC12 (the three reused binary-identity detectors
    regression-tested under `dialect=POWERSHELL` explicitly, not just via
    the bash-default path).
    """

    # -- AC6: the four blind-spawn shapes classify --------------------

    def test_foreach_object_block_calling_a_process_matches(self) -> None:
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { python3 script.py $_.FullName }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_percent_alias_block_calling_a_process_matches(self) -> None:
        result = classify_command(
            "Get-ChildItem -Recurse | % { git log -1 $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_foreach_statement_matches_for_loop(self) -> None:
        result = classify_command(
            "foreach ($f in $files) { git log -1 $f }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.FOR_LOOP,)

    def test_write_host_banner_sequence_matches(self) -> None:
        result = classify_command(
            "Write-Host '=== status ==='; git status; git log -1; pwd",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.MULTI_PROBE_BANNER,)

    def test_write_output_banner_vocabulary_also_matches(self) -> None:
        result = classify_command(
            "Write-Output '=== status ==='; git status; git log -1; pwd",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.MULTI_PROBE_BANNER,)

    def test_echo_alias_of_write_output_is_banner_vocabulary(self) -> None:
        # PowerShell ships `echo` as a live ALIAS of `Write-Output`, so an
        # echo-led probe sequence spawns per probe exactly as its
        # `Write-Host` spelling does. Omitting the alias left a shape the
        # PowerShell leg could not see -- i.e. the escape hatch this whole
        # dialect leg exists to close, reachable by spelling the banner
        # `echo` instead of `Write-Host`.
        result = classify_command(
            'echo "=== facts ==="; pwd; whoami; git status',
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.MULTI_PROBE_BANNER,)

    def test_printf_is_not_powershell_banner_vocabulary(self) -> None:
        # The negative half of the alias reasoning: PowerShell ships no
        # `printf` alias, so admitting it would match a name that cannot
        # run. Only `echo` crosses over from bash's banner vocabulary.
        result = classify_command(
            'printf "=== facts ==="; pwd; whoami; git status',
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    # -- AC7: in-process cmdlets are NOT members (D3) -- hard gate -----

    def test_select_string_does_not_match(self) -> None:
        result = classify_command(
            "Select-String -Pattern TODO -Path src/*.py",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_get_content_select_object_first_does_not_match(self) -> None:
        result = classify_command(
            "Get-Content foo.txt | Select-Object -First 20",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_get_item_does_not_match(self) -> None:
        result = classify_command("Get-Item .", dialect=Dialect.POWERSHELL)
        assert result.matched_shapes == ()

    def test_bare_write_host_no_probe_sequence_does_not_match(self) -> None:
        # A lone banner call is not banner-shaped -- the shape requires N
        # probe segments (`_MIN_BANNER_SEGMENTS`).
        result = classify_command("Write-Host 'hello'", dialect=Dialect.POWERSHELL)
        assert result.matched_shapes == ()

    def test_bare_write_host_banner_marker_no_probe_sequence_does_not_match(
        self,
    ) -> None:
        result = classify_command(
            "Write-Host '=== section ==='", dialect=Dialect.POWERSHELL
        )
        assert result.matched_shapes == ()

    def test_bare_write_output_does_not_match(self) -> None:
        result = classify_command("Write-Output 'x'", dialect=Dialect.POWERSHELL)
        assert result.matched_shapes == ()

    def test_foreach_object_block_pure_property_access_does_not_match(self) -> None:
        # The block-content check (D2/D3): pure member/property access
        # spawns nothing and must not match, even though the outer segment
        # shape (ForEach-Object piped a block) looks identical to the
        # true-positive case.
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { $_.Name }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    # -- Finding 2 fix: in-process ForEach-Object block content must not
    # -- false-positive as PIPELINE_FOREACH_OBJECT (D3) -----------------

    def test_foreach_object_block_write_host_only_does_not_match(self) -> None:
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { Write-Host $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_foreach_object_block_select_object_first_does_not_match(self) -> None:
        result = classify_command(
            "Get-ChildItem | ForEach-Object { Select-Object -First 1 }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_percent_block_write_output_alias_does_not_match(self) -> None:
        result = classify_command(
            "Get-ChildItem | % { Write-Output $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_percent_block_get_content_does_not_match(self) -> None:
        result = classify_command(
            "Get-ChildItem | % { Get-Content $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    def test_foreach_object_block_native_call_still_matches(self) -> None:
        # Regression: the AC6 true positive must not be swept up by the
        # in-process exclusion.
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { python3 script.py $_.FullName }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_percent_block_native_call_still_matches(self) -> None:
        result = classify_command(
            "Get-ChildItem -Recurse | % { git log -1 $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_percent_block_hyphenated_native_executable_still_matches(self) -> None:
        # docker-compose is not an approved-verb cmdlet, despite the
        # hyphen -- anchored on the approved-verb list, not "contains a
        # hyphen".
        result = classify_command(
            "Get-ChildItem | % { docker-compose up $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_percent_block_start_process_still_matches(self) -> None:
        # Start-/Invoke- are the deliberate carve-out: they genuinely
        # spawn, so they stay native calls despite the Verb-Noun shape.
        result = classify_command(
            "Get-ChildItem | % { Start-Process python3 $_ }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

    def test_foreach_object_block_pure_property_access_still_does_not_match(
        self,
    ) -> None:
        # Existing negative, must not regress under the new discriminator.
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { $_.Name }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.matched_shapes == ()

    # -- AC8: WHILE_READ_LOOP absent, with a stated reason --------------

    def test_while_read_loop_has_no_powershell_detector(self) -> None:
        # PowerShell has no `while read` idiom -- WHILE_READ_LOOP is not a
        # member of the POWERSHELL table entry at all (see the table's own
        # comment). A pwsh-flavoured attempt at the bash spelling must not
        # accidentally match either.
        result = classify_command(
            "while ($true) { $x = Read-Host; git log -1 $x }",
            dialect=Dialect.POWERSHELL,
        )
        assert not result.has_shape(Shape.WHILE_READ_LOOP)

    # -- AC12: the three reused binary-identity detectors, regression- ---
    # -- tested explicitly under dialect=POWERSHELL ----------------------

    def test_grep_via_bash_detector_reused_unchanged_under_powershell_dialect(
        self,
    ) -> None:
        result = classify_command("grep -rn TODO src/", dialect=Dialect.POWERSHELL)
        assert result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_find_exec_detector_reused_unchanged_under_powershell_dialect(
        self,
    ) -> None:
        result = classify_command(
            "find . -name '*.log' -exec rm {} ;", dialect=Dialect.POWERSHELL
        )
        assert result.matched_shapes == (Shape.FIND_EXEC_XARGS,)

    def test_head_tail_plumbing_detector_reused_unchanged_under_powershell_dialect(
        self,
    ) -> None:
        result = classify_command(
            "Get-Content foo.txt | head -20", dialect=Dialect.POWERSHELL
        )
        assert result.matched_shapes == (Shape.HEAD_TAIL_PLUMBING,)

    # -- precedence: the new member seats correctly ----------------------

    def test_pipeline_foreach_object_seats_immediately_after_for_loop(self) -> None:
        assert SHAPE_PRECEDENCE.index(Shape.PIPELINE_FOREACH_OBJECT) == (
            SHAPE_PRECEDENCE.index(Shape.FOR_LOOP) + 1
        )
        assert SHAPE_PRECEDENCE.index(Shape.PIPELINE_FOREACH_OBJECT) < (
            SHAPE_PRECEDENCE.index(Shape.WHILE_READ_LOOP)
        )

    def test_grep_outranks_pipeline_foreach_object_on_powershell_leg(self) -> None:
        # A single command that is both grep-via-Bash and pipeline-foreach-
        # object shaped resolves to the precedence winner, with the other
        # surfacing in residue -- same overlap discipline as the bash leg.
        result = classify_command(
            "grep -rn TODO src/; Get-ChildItem *.py | ForEach-Object "
            "{ python3 script.py $_.FullName }",
            dialect=Dialect.POWERSHELL,
        )
        assert result.primary is not None
        assert result.primary.shape is Shape.GREP_VIA_BASH
        assert result.has_shape(Shape.PIPELINE_FOREACH_OBJECT)
        assert result.residue[0].shape is Shape.PIPELINE_FOREACH_OBJECT


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


class TestDialectParameter:
    """AC1/AC2/AC3/AC5 -- the keyword-only `dialect` parameter (D1) and the
    dialect-indexed detector table (D4) landed in C1. Every case in every
    OTHER test class above already covers AC2 (byte-for-byte bash behaviour
    with no `dialect=` argument at all, i.e. the pre-existing bash suite
    passing unmodified); this class adds the cases specific to the new
    parameter's own contract.
    """

    def test_default_dialect_is_bash(self) -> None:
        # AC1: no `dialect=` argument at all defaults to `Dialect.BASH`.
        default_result = classify_command("grep -rn TODO src/")
        explicit_result = classify_command(
            "grep -rn TODO src/", dialect=Dialect.BASH
        )
        assert default_result.matched_shapes == explicit_result.matched_shapes
        assert default_result.matched_shapes == (Shape.GREP_VIA_BASH,)

    def test_bash_default_byte_for_byte_on_canonical_overlap_case(self) -> None:
        # AC2: the default-`dialect` path and an explicit `dialect=BASH`
        # path must be indistinguishable, even on the plan's own canonical
        # triple-overlap command.
        cmd = 'echo "=== git status ==="; git status | grep -i modified | head'
        default_result = classify_command(cmd)
        explicit_result = classify_command(cmd, dialect=Dialect.BASH)
        assert default_result.tokens == explicit_result.tokens
        assert default_result.matches == explicit_result.matches

    def test_explicit_none_dialect_is_silent_not_bash_fallback(self) -> None:
        # AC3: an explicit `dialect=None` returns an empty classification
        # with `tokens=None` -- never the bash-default result the same
        # command text would produce.
        bash_result = classify_command("grep -rn TODO src/")
        assert bash_result.primary is not None  # sanity: this text IS a match on bash

        none_result = classify_command("grep -rn TODO src/", dialect=None)
        assert none_result.tokens is None
        assert none_result.matches == ()
        assert none_result.primary is None

    def test_explicit_none_dialect_records_silent(self) -> None:
        # AC3: the SILENT declaration is real and test-observable via the
        # existing `_verdict.collecting()` mechanism.
        with collecting() as silences:
            result = classify_command("grep -rn TODO src/", dialect=None)
        assert result.tokens is None
        assert len(silences) == 1
        assert "None" in silences[0].reason

    def test_powershell_dialect_never_calls_posix_tokenizer(self, monkeypatch) -> None:
        # AC5: PowerShell text must never reach `tokenize_full_command`
        # (the `shlex(posix=True)` tokenizer) -- spy on it directly.
        def _fail_if_called(*args, **kwargs):
            raise AssertionError(
                "tokenize_full_command must not be called under "
                "dialect=Dialect.POWERSHELL"
            )

        monkeypatch.setattr(
            _command_tokenizer, "tokenize_full_command", _fail_if_called
        )
        result = classify_command(
            "Get-ChildItem *.py | ForEach-Object { git log -1 $_.FullName }",
            dialect=Dialect.POWERSHELL,
        )
        # C2 fills the POWERSHELL table entry -- this now classifies for
        # real (PIPELINE_FOREACH_OBJECT), which is itself further proof the
        # posix tokenizer was never invoked to produce it.
        assert result.matched_shapes == (Shape.PIPELINE_FOREACH_OBJECT,)

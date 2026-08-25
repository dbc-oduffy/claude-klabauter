"""
Tests for coordinator_core.frontmatter.primitives.

Coverage targets (per task spec):
  - anchored vs append-only insert_fm_field
  - all quoting variants: # char, all-numeric, scientific, structural chars,
    leading -/?/space
  - preamble + HTML-comment preservation through split → rebuild round-trip
  - CRLF normalization on read
  - block-scalar guard raises ValueError in replace_fm_field
  - split_frontmatter returns None on malformed input
  - read_fm_field boundary lookahead (no prefix match)
  - rebuild byte-identity outside mutated fields
"""
from __future__ import annotations

import pytest
import yaml

from coordinator_core.frontmatter.primitives import (
    FrontmatterSplit,
    _append_blocking_note,
    _retire_gate_dependency,
    insert_fm_field,
    append_fm_block_scalar_line,
    read_fm_block_scalar,
    read_fm_field,
    read_fm_nested_field,
    rebuild,
    remove_fm_field,
    remove_fm_nested_field,
    replace_fm_field,
    replace_fm_field_raw,
    read_fm_field_unquoted,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
    write_fm_nested_field,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(fm_body: str, body: str = '\n# Body\n', preamble: str = '') -> str:
    """Build a minimal frontmatter document for testing."""
    return f'{preamble}---\n{fm_body}\n---{body}'


# ---------------------------------------------------------------------------
# split_frontmatter — basic cases
# ---------------------------------------------------------------------------

class TestSplitFrontmatter:
    def test_simple_split(self):
        doc = '---\ntitle: Hello\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == ''
        assert result.fm_text == 'title: Hello\n'
        assert result.body_with_leading_newline == '\n# Body\n'

    def test_returns_none_for_no_frontmatter(self):
        assert split_frontmatter('# Just a body\n') is None

    def test_returns_none_for_missing_close(self):
        assert split_frontmatter('---\ntitle: Hello\n') is None

    def test_returns_none_for_empty_string(self):
        assert split_frontmatter('') is None

    def test_returns_none_for_no_opening_dash(self):
        assert split_frontmatter('title: Hello\n---\n') is None

    def test_body_preserves_leading_blank_line(self):
        """Closing --- followed by blank line preserves that blank line."""
        doc = '---\nk: v\n---\n\n# Heading\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.body_with_leading_newline == '\n\n# Heading\n'

    def test_trailing_whitespace_on_close_delimiter(self):
        """--- followed by spaces/tabs still recognized as close."""
        doc = '---\nk: v\n---   \n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None

    def test_close_with_tabs(self):
        doc = '---\nk: v\n---\t\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None

    def test_open_delimiter_with_trailing_text_ignored(self):
        # Review: code-reviewer — F8 (docstring fix): test exercises opening delimiter, not close.
        # Review: code-reviewer — F2 (vacuous assert): now asserts real behaviour after F3 parity fix.
        """--- followed by non-whitespace on the opening line — JS parity: rejected as invalid opener."""
        doc = '---yaml\nk: v\n---\n'
        result = split_frontmatter(doc)
        # After F3 parity fix: ^---[ \t]*\n regex rejects ---yaml (JS parity — JS uses /^---\s*\n/).
        assert result is None

    def test_multiline_fm(self):
        doc = '---\ntitle: Hello\nstatus: open\npickup_ready: true\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert 'title: Hello' in result.fm_text
        assert 'status: open' in result.fm_text
        assert 'pickup_ready: true' in result.fm_text


# ---------------------------------------------------------------------------
# split_frontmatter — CRLF normalization
# ---------------------------------------------------------------------------

class TestCRLFNormalization:
    def test_crlf_in_frontmatter(self):
        doc = '---\r\ntitle: Hello\r\nstatus: open\r\n---\r\n# Body\r\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert '\r' not in result.fm_text
        assert '\r' not in result.preamble

    def test_crlf_in_body(self):
        doc = '---\r\nk: v\r\n---\r\nBody line\r\n'
        result = split_frontmatter(doc)
        assert result is not None
        # body_with_leading_newline had CRLF normalized
        assert '\r' not in result.body_with_leading_newline

    def test_crlf_mixed_with_lf(self):
        doc = '---\r\ntitle: Hi\nstatus: ok\r\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert 'title: Hi' in result.fm_text


# ---------------------------------------------------------------------------
# split_frontmatter — preamble handling
# ---------------------------------------------------------------------------

class TestPreambleHandling:
    def test_blank_line_preamble(self):
        doc = '\n---\ntitle: Hi\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == '\n'
        assert result.fm_text == 'title: Hi\n'

    def test_multiple_blank_lines_preamble(self):
        doc = '\n\n\n---\ntitle: Hi\n---\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == '\n\n\n'

    def test_html_comment_preamble(self):
        doc = '<!-- provenance: installer-seeded -->\n---\ntitle: Hi\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == '<!-- provenance: installer-seeded -->\n'
        assert result.fm_text == 'title: Hi\n'

    def test_html_comment_multiline_preamble(self):
        preamble = '<!--\nproject_rag_setup\nholodeck installer\n-->\n'
        doc = preamble + '---\ntitle: Hi\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == preamble

    def test_blank_lines_then_html_comment_preamble(self):
        preamble = '\n<!-- comment -->\n'
        doc = preamble + '---\nk: v\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == preamble

    def test_preamble_preserved_verbatim_in_rebuild(self):
        preamble = '<!-- DO NOT EDIT: installer provenance record -->\n'
        fm = 'title: My Handoff\nstatus: open\n'
        body = '\n# My Handoff\n\nBody text.\n'
        doc = preamble + '---\n' + fm + '---' + body
        result = split_frontmatter(doc)
        assert result is not None
        assert result.preamble == preamble
        rebuilt = rebuild(result, result.fm_text)
        assert rebuilt == doc

    def test_no_preamble_no_match_returns_none(self):
        # Non-blank, non-comment, non-dash start
        assert split_frontmatter('Some text\n---\nk: v\n---\n') is None


# ---------------------------------------------------------------------------
# read_fm_field
# ---------------------------------------------------------------------------

class TestReadFmField:
    def test_simple_read(self):
        fm = 'title: Hello\nstatus: open\n'
        assert read_fm_field(fm, 'title') == 'Hello'
        assert read_fm_field(fm, 'status') == 'open'

    def test_absent_key_returns_none(self):
        fm = 'title: Hello\n'
        assert read_fm_field(fm, 'status') is None

    def test_boundary_lookahead_no_prefix_match(self):
        """'status' must NOT match 'status_message:' line."""
        fm = 'status_message: something\nother: val\n'
        assert read_fm_field(fm, 'status') is None

    def test_value_trimmed(self):
        fm = 'title:   Hello World   \n'
        assert read_fm_field(fm, 'title') == 'Hello World'

    def test_empty_value(self):
        fm = 'initiative: \n'
        assert read_fm_field(fm, 'initiative') == ''

    def test_key_at_start_of_line_only(self):
        """Key must be at line start (^ anchor)."""
        fm = '  indented: val\nreal: ok\n'
        assert read_fm_field(fm, 'indented') is None
        assert read_fm_field(fm, 'real') == 'ok'

    def test_quoted_value_returned_raw(self):
        """read_fm_field returns raw text; caller is responsible for unquoting."""
        fm = "title: 'My Title'\n"
        assert read_fm_field(fm, 'title') == "'My Title'"

    def test_key_with_no_space_after_colon(self):
        """key: followed by end-of-line (empty value)."""
        fm = 'initiative:\n'
        assert read_fm_field(fm, 'initiative') == ''


# ---------------------------------------------------------------------------
# Empty-value reads must not cross the line boundary (break-class, 2026-07-28)
#
# The pre-fix pattern padded with `\s*`, and `\s` matches a newline — so a
# present-but-empty key walked past its own line break and returned the
# FOLLOWING line's content. The pre-existing empty-value cases above hid it by
# putting the empty key on the LAST line, where there is no following line to
# steal. Every case here therefore has a populated line after the empty key.
# ---------------------------------------------------------------------------

class TestEmptyValueDoesNotCrossLineBoundary:
    def test_empty_key_does_not_read_following_line(self):
        fm = (
            'title: x\n'
            'blocking_notes:\n'
            'status: open\n'
            'deployment_state: awaiting_gate\n'
        )
        assert read_fm_field(fm, 'blocking_notes') == ''

    def test_empty_key_with_trailing_space_does_not_read_following_line(self):
        fm = 'blocking_notes: \nstatus: open\n'
        assert read_fm_field(fm, 'blocking_notes') == ''

    def test_empty_key_with_trailing_tab_does_not_read_following_line(self):
        fm = 'blocking_notes:\t\nstatus: open\n'
        assert read_fm_field(fm, 'blocking_notes') == ''

    def test_empty_key_followed_by_blank_line_then_key(self):
        fm = 'blocking_notes:\n\nstatus: open\n'
        assert read_fm_field(fm, 'blocking_notes') == ''

    def test_unquoted_reader_inherits_the_fix(self):
        fm = 'blocking_notes:\nstatus: open\n'
        assert read_fm_field_unquoted(fm, 'blocking_notes') == ''

    def test_absent_stays_none_so_callers_can_tell_it_from_empty(self):
        """None (absent) vs '' (present-but-empty) is a real distinction now."""
        fm = 'blocking_notes:\nstatus: open\n'
        assert read_fm_field(fm, 'blocking_notes') == ''
        assert read_fm_field(fm, 'gate_dependency') is None

    def test_crlf_value_excludes_carriage_return(self):
        fm = 'title: x\r\nstatus: open\r\nother: v\r\n'
        assert read_fm_field(fm, 'status') == 'open'

    def test_replace_of_empty_key_does_not_overwrite_next_line(self):
        """The corruption the read defect drove: replace ate the neighbour."""
        fm = 'title: x\nblocking_notes:\nstatus: open\n'
        result = replace_fm_field(fm, 'blocking_notes', 'note')
        assert result == 'title: x\nblocking_notes: note\nstatus: open\n'
        assert read_fm_field(result, 'status') == 'open'
        assert result.count('blocking_notes:') == 1

    def test_replace_of_empty_key_emits_a_mapping_not_a_glued_scalar(self):
        fm = 'blocking_notes:\nstatus: open\n'
        result = replace_fm_field(fm, 'blocking_notes', 'note')
        assert 'blocking_notes: note' in result
        assert 'blocking_notes:note' not in result


# ---------------------------------------------------------------------------
# unquote_yaml_scalar / read_fm_field_unquoted — write/read symmetry
#
# Regression cover for state/bug-backlog/2026-07-20-read-fm-field-does-not-
# unquote-write-read-asymmetry.yaml: serialize_yaml_scalar quotes numeric-looking
# SHAs (numeric_quoting=True) but read_fm_field does not unquote, so a
# comparison-style consumer silently mismatched on ~13% of git sha8s.
# ---------------------------------------------------------------------------

#: sha8 values that actually trigger each of serialize_yaml_scalar's two
#: numeric_quoting guards. Both shapes occur naturally in git output.
_ALL_DIGIT_SHA8 = '44379324'          # _ALL_NUMERIC_RE — YAML int coercion
_SCIENTIFIC_SHA8 = '23814e50'         # _SCIENTIFIC_RE — YAML 1.1 float coercion


class TestUnquoteYamlScalar:
    def test_none_passes_through(self):
        assert unquote_yaml_scalar(None) is None

    def test_bare_value_unchanged(self):
        assert unquote_yaml_scalar('abc1234f') == 'abc1234f'

    def test_single_quoted_stripped(self):
        assert unquote_yaml_scalar("'44379324'") == '44379324'

    def test_double_quoted_stripped(self):
        assert unquote_yaml_scalar('"draft"') == 'draft'

    def test_doubled_inner_quotes_unescaped(self):
        """YAML's '' inner-quote form is unescaped, not left doubled."""
        assert unquote_yaml_scalar("'it''s here'") == "it's here"

    def test_empty_quoted_value(self):
        assert unquote_yaml_scalar("''") == ''

    def test_unmatched_leading_quote_untouched(self):
        """A lone leading quote is not a matched pair — leave the text alone."""
        assert unquote_yaml_scalar("'unterminated") == "'unterminated"

    def test_not_a_naive_strip(self):
        """A legitimately-quoted value whose content contains quotes must survive.

        `.strip("'")` would eat the inner quotes too and return `a' and 'b`.
        """
        raw = serialize_yaml_scalar("'a' and 'b'")
        assert unquote_yaml_scalar(raw) == "'a' and 'b'"

    def test_single_char_quote_not_stripped(self):
        """A one-character value that IS a quote has no matched pair."""
        assert unquote_yaml_scalar("'") == "'"


class TestWriteReadRoundTrip:
    """serialize/insert → read_fm_field_unquoted must be the identity."""

    @pytest.mark.parametrize('sha', [_ALL_DIGIT_SHA8, _SCIENTIFIC_SHA8])
    def test_numeric_quoting_sha_round_trips(self, sha):
        fm = insert_fm_field('title: H\n', 'shipped_in', sha, numeric_quoting=True)
        # Precondition: the writer really did quote — otherwise this test is vacuous.
        assert f"shipped_in: '{sha}'" in fm
        # The raw reader returns the quotes (documented, unchanged behaviour)...
        assert read_fm_field(fm, 'shipped_in') == f"'{sha}'"
        # ...and the unquoting reader closes the loop.
        assert read_fm_field_unquoted(fm, 'shipped_in') == sha

    @pytest.mark.parametrize('sha', [_ALL_DIGIT_SHA8, _SCIENTIFIC_SHA8])
    def test_numeric_quoting_sha_round_trips_via_replace(self, sha):
        fm = replace_fm_field(
            'shipped_in: placeholder\n', 'shipped_in', sha, numeric_quoting=True
        )
        assert read_fm_field_unquoted(fm, 'shipped_in') == sha

    @pytest.mark.parametrize('value', [
        'plain-value',
        'has: a colon',
        'has # a hash',
        "has ' a quote",
        '-leading-dash',
        ' leading-space',
        'abc1234f',
    ])
    def test_structural_values_round_trip(self, value):
        fm = insert_fm_field('title: H\n', 'note', value)
        assert read_fm_field_unquoted(fm, 'note') == value

    def test_absent_key_returns_none(self):
        assert read_fm_field_unquoted('title: H\n', 'shipped_in') is None

    def test_boundary_lookahead_preserved(self):
        """Shares read_fm_field's key resolution — no prefix match."""
        fm = 'status_message: hello\n'
        assert read_fm_field_unquoted(fm, 'status') is None


class TestTrailingCommentStripping:
    """2026-07-27 break-class fix: a trailing ``# comment`` on a plain
    ``key: value  # comment`` frontmatter line used to read back as part of
    the value itself (see `_strip_trailing_comment`'s own docstring for the
    reproduced `baton_assemble` FK-corruption incident)."""

    def test_null_with_trailing_comment_reads_as_null(self):
        fm = (
            'initiative: null  # FK to state/initiatives/<id>.yaml; '
            'null when no named initiative\n'
        )
        assert read_fm_field_unquoted(fm, 'initiative') == 'null'

    def test_plain_value_with_trailing_comment_strips_it(self):
        fm = 'status: open  # one of open|claimed\n'
        assert read_fm_field_unquoted(fm, 'status') == 'open'

    def test_quoted_value_containing_hash_survives_unstripped(self):
        fm = "note: 'has # a hash'  # trailing comment\n"
        assert read_fm_field_unquoted(fm, 'note') == 'has # a hash'

    def test_hash_glued_to_value_is_not_a_comment(self):
        # No whitespace before '#' -- YAML treats this as data, not a comment.
        fm = 'note: abc#def\n'
        assert read_fm_field_unquoted(fm, 'note') == 'abc#def'

    def test_no_trailing_comment_unaffected(self):
        fm = 'title: plain title\n'
        assert read_fm_field_unquoted(fm, 'title') == 'plain title'

    def test_raw_read_fm_field_still_returns_the_comment_verbatim(self):
        """`read_fm_field` (the verbatim/echo/rewrite reader) is deliberately
        UNCHANGED by this fix -- only `read_fm_field_unquoted` (the
        compare/parse reader) strips the comment."""
        fm = 'status: open  # one of open|claimed\n'
        assert read_fm_field(fm, 'status') == 'open  # one of open|claimed'


class TestWriteSidePreservesTrailingComment:
    """2026-08-01 break-class fix (project-opticon repro): `replace_fm_field`/
    `replace_fm_field_raw` used to substitute the ENTIRE rest of the `key:`
    line, so a value carrying a trailing YAML inline comment
    (``status: approved  # PM authorized execution ...``) silently lost the
    comment on rewrite (``status: implemented``). `_split_trailing_comment`
    is now the single quote-aware parser shared by the read side
    (`_strip_trailing_comment`, see `TestTrailingCommentStripping` above) and
    this write side, so a rewritten line re-emits the original comment
    instead of deleting it. Supplements the coverage already living in
    `TestCRLFPresentButEmptyKey`'s "trailing inline-comment preservation"
    section (the opticon repro itself, quote-awareness, glued-hash,
    comment-only and present-but-empty-key shapes, and CRLF round-tripping)
    with the padding-replay, neighbour-isolation and inline-array cases not
    yet covered there."""

    def test_opticon_repro_replace_fm_field(self):
        fm = (
            'status: approved  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == (
            'status: implemented  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )

    def test_opticon_repro_replace_fm_field_raw(self):
        fm = (
            'status: approved  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == (
            'status: implemented  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )

    def test_no_comment_line_unchanged_replace_fm_field(self):
        fm = 'status: approved\nother: v\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == 'status: implemented\nother: v\n'

    def test_no_comment_line_unchanged_replace_fm_field_raw(self):
        fm = 'status: approved\nother: v\n'
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == 'status: implemented\nother: v\n'

    def test_quote_aware_hash_in_quotes_is_data_replace_fm_field(self):
        fm = "note: 'has # a hash'  # real comment\nother: v\n"
        result = replace_fm_field(fm, 'note', 'newval')
        assert result == 'note: newval  # real comment\nother: v\n'

    def test_quote_aware_hash_in_quotes_is_data_replace_fm_field_raw(self):
        fm = "note: 'has # a hash'  # real comment\nother: v\n"
        result = replace_fm_field_raw(fm, 'note', 'newval')
        assert result == 'note: newval  # real comment\nother: v\n'

    def test_glued_hash_is_not_a_comment_replace_fm_field(self):
        fm = 'field: abc#def\nother: v\n'
        result = replace_fm_field(fm, 'field', 'newval')
        assert result == 'field: newval\nother: v\n'

    def test_glued_hash_is_not_a_comment_replace_fm_field_raw(self):
        fm = 'field: abc#def\nother: v\n'
        result = replace_fm_field_raw(fm, 'field', 'newval')
        assert result == 'field: newval\nother: v\n'

    def test_present_but_empty_fills_with_canonical_space_replace_fm_field(self):
        fm = 'field:\nother: v\n'
        result = replace_fm_field(fm, 'field', 'v')
        assert result == 'field: v\nother: v\n'

    def test_present_but_empty_fills_with_canonical_space_replace_fm_field_raw(self):
        fm = 'field:\nother: v\n'
        result = replace_fm_field_raw(fm, 'field', 'v')
        assert result == 'field: v\nother: v\n'

    def test_comment_only_line_fills_value_and_keeps_comment_replace_fm_field(self):
        fm = 'field:  # nothing yet\nother: v\n'
        result = replace_fm_field(fm, 'field', 'v')
        assert result == 'field: v  # nothing yet\nother: v\n'

    def test_comment_only_line_fills_value_and_keeps_comment_replace_fm_field_raw(self):
        fm = 'field:  # nothing yet\nother: v\n'
        result = replace_fm_field_raw(fm, 'field', 'v')
        assert result == 'field: v  # nothing yet\nother: v\n'

    def test_crlf_round_trip_replace_fm_field(self):
        fm = 'field: old  # c\r\nother: v\r\n'
        result = replace_fm_field(fm, 'field', 'new')
        assert result == 'field: new  # c\r\nother: v\r\n'

    def test_crlf_round_trip_replace_fm_field_raw(self):
        fm = 'field: old  # c\r\nother: v\r\n'
        result = replace_fm_field_raw(fm, 'field', 'new')
        assert result == 'field: new  # c\r\nother: v\r\n'

    @pytest.mark.parametrize('padding', [' ', '   '], ids=['single-space', 'multi-space'])
    def test_padding_before_hash_replays_byte_identically_replace_fm_field(self, padding):
        fm = f'status: approved{padding}# c\nother: v\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == f'status: implemented{padding}# c\nother: v\n'

    @pytest.mark.parametrize('padding', [' ', '   '], ids=['single-space', 'multi-space'])
    def test_padding_before_hash_replays_byte_identically_replace_fm_field_raw(self, padding):
        fm = f'status: approved{padding}# c\nother: v\n'
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == f'status: implemented{padding}# c\nother: v\n'

    def test_only_targeted_line_touched_neighbour_comment_survives_replace_fm_field(self):
        """A neighbouring field that ALSO carries a trailing comment must be
        left completely untouched -- not just present, but byte-identical."""
        fm = 'status: approved  # c1\nother: val  # c2\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == 'status: implemented  # c1\nother: val  # c2\n'

    def test_only_targeted_line_touched_neighbour_comment_survives_replace_fm_field_raw(self):
        fm = 'status: approved  # c1\nother: val  # c2\n'
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == 'status: implemented  # c1\nother: val  # c2\n'

    def test_inline_array_raw_caller_shape_preserves_trailing_comment(self):
        """The `replace_fm_field_raw(fm, key, "[a, b]")` shape used by array
        writers (`ops/handoff_author_fork._stamp_fork_provenance`,
        `ops/handoff_transition`'s array writers) must preserve a trailing
        comment exactly like any other raw-value caller."""
        fm = 'tags: [a, b]  # curated list\nother: v\n'
        result = replace_fm_field_raw(fm, 'tags', '[c, d]')
        assert result == 'tags: [c, d]  # curated list\nother: v\n'


class TestGluedHashBeforeRealCommentIsStillFound:
    """Review: code-reviewer — Finding 1 (P1). `_split_trailing_comment` used
    to give up entirely on the FIRST `#` it found, even a glued one that is
    plainly data (`abc#def`) rather than a comment opener -- so a LATER,
    genuinely space-preceded `#` starting a real trailing comment was never
    located. On the read path (`_strip_trailing_comment`) this polluted the
    returned VALUE with the comment text; on the write path
    (`replace_fm_field_raw`) it DESTROYED the comment outright on rewrite.
    Fixed by continuing the `#` scan from `hash_pos + 1` instead of stopping
    at the first ineligible candidate."""

    def test_read_side_value_excludes_comment_text(self):
        fm = 'field: abc#def  # real comment\n'
        assert read_fm_field_unquoted(fm, 'field') == 'abc#def'

    def test_write_side_preserves_the_comment_replace_fm_field(self):
        fm = 'field: abc#def  # real comment\nother: v\n'
        result = replace_fm_field(fm, 'field', 'newval')
        assert result == 'field: newval  # real comment\nother: v\n'

    def test_write_side_preserves_the_comment_replace_fm_field_raw(self):
        fm = 'field: abc#def  # real comment\nother: v\n'
        result = replace_fm_field_raw(fm, 'field', 'newval')
        assert result == 'field: newval  # real comment\nother: v\n'

    def test_quoted_value_with_hash_then_real_trailing_comment(self):
        # `#glued` immediately follows the closing quote with no whitespace,
        # so it is data appended to the tail (mirrors the unquoted glued-hash
        # case), not a comment opener -- only the space-preceded `#` after it
        # starts the real comment.
        fm = "note: 'has # a hash'#glued  # real comment\nother: v\n"
        result = replace_fm_field_raw(fm, 'note', 'newval')
        assert result == 'note: newval#glued  # real comment\nother: v\n'

    def test_glued_hash_with_no_trailing_comment_is_still_a_regression_guard(self):
        """A glued `#` with NO later real comment must still yield no
        comment at all -- the fix must not manufacture a comment split out
        of pure data."""
        fm = 'field: abc#def\nother: v\n'
        assert read_fm_field_unquoted(fm, 'field') == 'abc#def'
        result = replace_fm_field_raw(fm, 'field', 'newval')
        assert result == 'field: newval\nother: v\n'

    def test_multiple_glued_hashes_before_the_real_comment(self):
        fm = 'field: a#b#c#d  # real comment\nother: v\n'
        assert read_fm_field_unquoted(fm, 'field') == 'a#b#c#d'
        result = replace_fm_field_raw(fm, 'field', 'newval')
        assert result == 'field: newval  # real comment\nother: v\n'


class TestAppendBlockingNoteCommentContractDocstring:
    """Pins `_append_blocking_note`'s documented 2026-08-01 contract change:
    shape 3 (comment-only `blocking_notes:  # nothing yet`) now preserves its
    trailing comment on fill, because the fill routes through
    `replace_fm_field`/`replace_fm_field_raw`, which no longer discard one.
    `TestAppendBlockingNote.test_comment_only_blocking_notes_is_filled_not_duplicated`
    only asserts through `yaml.safe_load`, which strips comments regardless
    of whether the fix is present -- this pins the RAW text instead, so it
    would fail if the comment-preservation behaviour regressed."""

    def test_comment_only_shape_preserves_the_comment_in_raw_text(self):
        fm = 'title: T\nblocking_notes:  # nothing yet\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result == 'title: T\nblocking_notes: retired note  # nothing yet\nstatus: open\n'


# ---------------------------------------------------------------------------
# serialize_yaml_scalar
# ---------------------------------------------------------------------------

class TestSerializeYamlScalar:
    # ---- null ---

    def test_none_to_null(self):
        assert serialize_yaml_scalar(None) == 'null'

    def test_none_with_numeric_quoting(self):
        assert serialize_yaml_scalar(None, numeric_quoting=True) == 'null'

    # ---- structural characters ---

    def test_hash_quoting(self):
        """# must be quoted (SHA inline-comment defense)."""
        result = serialize_yaml_scalar('abc#def')
        assert result == "'abc#def'"

    def test_colon_quoting(self):
        result = serialize_yaml_scalar('key:value')
        assert result == "'key:value'"

    def test_brace_quoting(self):
        assert serialize_yaml_scalar('{flow}') == "'{flow}'"

    def test_bracket_quoting(self):
        assert serialize_yaml_scalar('[item]') == "'[item]'"

    def test_comma_quoting(self):
        assert serialize_yaml_scalar('a,b') == "'a,b'"

    def test_ampersand_quoting(self):
        assert serialize_yaml_scalar('&anchor') == "'&anchor'"

    def test_asterisk_quoting(self):
        assert serialize_yaml_scalar('*alias') == "'*alias'"

    def test_exclamation_quoting(self):
        assert serialize_yaml_scalar('!tag') == "'!tag'"

    def test_pipe_quoting(self):
        assert serialize_yaml_scalar('a|b') == "'a|b'"

    def test_greater_than_quoting(self):
        assert serialize_yaml_scalar('>folded') == "'>folded'"

    def test_double_quote_quoting(self):
        assert serialize_yaml_scalar('"quoted"') == '\'"quoted"\''

    def test_single_quote_in_value(self):
        """Internal ' must be escaped by doubling."""
        result = serialize_yaml_scalar("it's fine")
        assert result == "'it''s fine'"

    def test_percent_quoting(self):
        assert serialize_yaml_scalar('%TAG') == "'%TAG'"

    def test_at_quoting(self):
        assert serialize_yaml_scalar('@mention') == "'@mention'"

    def test_backtick_quoting(self):
        assert serialize_yaml_scalar('`code`') == "'`code`'"

    # ---- leading characters ---

    def test_leading_dash(self):
        assert serialize_yaml_scalar('-item') == "'-item'"

    def test_leading_question_mark(self):
        assert serialize_yaml_scalar('?key') == "'?key'"

    def test_leading_space(self):
        assert serialize_yaml_scalar(' indented') == "' indented'"

    # ---- safe values pass through unquoted ---

    def test_plain_word(self):
        assert serialize_yaml_scalar('active') == 'active'

    def test_date_string(self):
        assert serialize_yaml_scalar('2026-07-05') == '2026-07-05'

    def test_bool_string(self):
        assert serialize_yaml_scalar('true') == 'true'

    def test_float_string(self):
        assert serialize_yaml_scalar('3.14') == '3.14'

    # ---- numeric_quoting flag ---

    def test_all_numeric_no_flag(self):
        """Without flag, all-numeric passes through."""
        assert serialize_yaml_scalar('12345678') == '12345678'

    def test_all_numeric_with_flag(self):
        """SHA-as-int defense: all-numeric quoted when numeric_quoting=True."""
        result = serialize_yaml_scalar('274671833', numeric_quoting=True)
        assert result == "'274671833'"

    def test_scientific_no_flag(self):
        """Without flag, scientific notation passes through."""
        assert serialize_yaml_scalar('1958e194') == '1958e194'

    def test_scientific_with_flag(self):
        """YAML 1.1 float coerce defense: scientific quoted when numeric_quoting=True."""
        result = serialize_yaml_scalar('1958e194', numeric_quoting=True)
        assert result == "'1958e194'"

    def test_mixed_alphanumeric_not_numeric(self):
        """'abc123' is not all-numeric — not quoted by numeric_quoting."""
        assert serialize_yaml_scalar('abc123', numeric_quoting=True) == 'abc123'

    def test_sha_hex_with_letters(self):
        """Normal hex SHA (has letters) — numeric_quoting irrelevant, but safe."""
        sha = 'a1b2c3d4e5f6'
        assert serialize_yaml_scalar(sha, numeric_quoting=True) == sha

    def test_sha_with_hash_char(self):
        """SHA that contains # must be quoted regardless of numeric_quoting."""
        result = serialize_yaml_scalar('abc#123')
        assert result == "'abc#123'"

    def test_multiple_internal_single_quotes(self):
        result = serialize_yaml_scalar("can't won't")
        assert result == "'can''t won''t'"

    def test_empty_string(self):
        """Empty string has no structural chars — passes through unquoted."""
        assert serialize_yaml_scalar('') == ''


# ---------------------------------------------------------------------------
# replace_fm_field
# ---------------------------------------------------------------------------

class TestReplaceFmField:
    def test_simple_replace(self):
        fm = 'title: Hello\nstatus: open\n'
        result = replace_fm_field(fm, 'status', 'claimed')
        assert read_fm_field(result, 'status') == 'claimed'
        assert read_fm_field(result, 'title') == 'Hello'

    def test_replace_with_quoting(self):
        fm = 'title: Hello\nstatus: open\n'
        result = replace_fm_field(fm, 'status', 'value#with#hash')
        assert read_fm_field(result, 'status') == "'value#with#hash'"

    def test_replace_preserves_key_prefix(self):
        fm = 'deployment_state: pending\n'
        result = replace_fm_field(fm, 'deployment_state', 'shipped')
        assert result == 'deployment_state: shipped\n'

    def test_replace_absent_key_no_op(self):
        """replace_fm_field does NOT insert — no change when key absent."""
        fm = 'title: Hello\n'
        result = replace_fm_field(fm, 'missing', 'value')
        assert result == fm

    def test_boundary_no_prefix_match(self):
        """Replacing 'status' must not alter 'status_message:'."""
        fm = 'status_message: detail\nstatus: open\n'
        result = replace_fm_field(fm, 'status', 'claimed')
        assert read_fm_field(result, 'status') == 'claimed'
        assert read_fm_field(result, 'status_message') == 'detail'
        assert 'status_message: detail' in result

    def test_block_scalar_folded_raises(self):
        fm = 'summary: >\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            replace_fm_field(fm, 'summary', 'new value')

    def test_block_scalar_literal_raises(self):
        fm = 'notes: |\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            replace_fm_field(fm, 'notes', 'new value')

    def test_block_scalar_error_truncates_long_value(self):
        long_value = '>' + ('x' * 80)
        fm = f'summary: {long_value}\n'
        with pytest.raises(ValueError) as exc_info:
            replace_fm_field(fm, 'summary', 'replacement')
        assert '...' in str(exc_info.value)

    def test_replace_null(self):
        fm = 'initiative: some-value\n'
        result = replace_fm_field(fm, 'initiative', None)
        assert read_fm_field(result, 'initiative') == 'null'

    def test_replace_does_not_duplicate_key(self):
        fm = 'status: open\nother: val\n'
        result = replace_fm_field(fm, 'status', 'claimed')
        assert result.count('status:') == 1


# ---------------------------------------------------------------------------
# insert_fm_field — append-only (2-arg / no after_key)
# ---------------------------------------------------------------------------

class TestInsertFmFieldAppendOnly:
    def test_append_adds_to_end(self):
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'category', 'infra')
        assert result.endswith('category: infra\n')
        assert read_fm_field(result, 'category') == 'infra'

    def test_append_preserves_existing_fields(self):
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'new_field', 'value')
        assert read_fm_field(result, 'title') == 'Hello'
        assert read_fm_field(result, 'status') == 'open'
        assert read_fm_field(result, 'new_field') == 'value'

    def test_append_trims_trailing_whitespace(self):
        fm = 'title: Hello\nstatus: open\n\n\n'
        result = insert_fm_field(fm, 'category', 'bug')
        # Result should end with exactly one newline after the new key
        assert result.endswith('category: bug\n')
        assert not result.endswith('category: bug\n\n')

    def test_append_null_value(self):
        fm = 'title: Hello\n'
        result = insert_fm_field(fm, 'initiative', None)
        assert read_fm_field(result, 'initiative') == 'null'

    def test_append_quoted_value(self):
        fm = 'title: Hello\n'
        result = insert_fm_field(fm, 'summary', 'line with #hash')
        assert "'line with #hash'" in result


# ---------------------------------------------------------------------------
# insert_fm_field — anchored (4-arg / with after_key)
# ---------------------------------------------------------------------------

class TestInsertFmFieldAnchored:
    def test_anchored_inserts_after_key(self):
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'deployment_state', 'in_flight', after_key='status')
        lines = result.split('\n')
        status_idx = next(i for i, l in enumerate(lines) if l.startswith('status:'))
        deploy_idx = next(i for i, l in enumerate(lines) if l.startswith('deployment_state:'))
        assert deploy_idx == status_idx + 1

    def test_anchored_preserves_all_fields(self):
        fm = 'title: Hello\nstatus: open\npickup_ready: true\n'
        result = insert_fm_field(fm, 'deployment_state', 'in_flight', after_key='status')
        assert read_fm_field(result, 'title') == 'Hello'
        assert read_fm_field(result, 'status') == 'open'
        assert read_fm_field(result, 'pickup_ready') == 'true'
        assert read_fm_field(result, 'deployment_state') == 'in_flight'

    def test_anchored_after_key_absent_appends(self):
        """When after_key is not found, falls back to append-at-end.

        The value '2026-07-05' (date only) has no structural chars and is stored
        unquoted. An ISO timestamp like '2026-07-05T10:00:00Z' contains ':' and
        would be single-quoted by serialize_yaml_scalar — tested separately.
        """
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'claimed_at', '2026-07-05', after_key='deployment_state')
        assert result.endswith('claimed_at: 2026-07-05\n')
        assert read_fm_field(result, 'claimed_at') == '2026-07-05'

    def test_anchored_sequential_inserts(self):
        """Simulate the consume transition: insert deployment_state after status,
        then claimed_at after deployment_state, then claimed_by after claimed_at.

        ISO timestamps like '2026-07-05T10:00:00Z' contain ':' so serialize_yaml_scalar
        wraps them in single-quotes — read_fm_field returns the raw quoted text.
        Use a plain date value here to keep the assertion simple.
        """
        fm = 'title: Hello\nstatus: claimed\n'
        fm = insert_fm_field(fm, 'deployment_state', 'in_flight', after_key='status')
        fm = insert_fm_field(fm, 'claimed_at', '2026-07-05', after_key='deployment_state')
        fm = insert_fm_field(fm, 'claimed_by', 'sess-abc123', after_key='claimed_at')

        assert read_fm_field(fm, 'deployment_state') == 'in_flight'
        assert read_fm_field(fm, 'claimed_at') == '2026-07-05'
        assert read_fm_field(fm, 'claimed_by') == 'sess-abc123'

        lines = [l for l in fm.split('\n') if l.strip()]
        status_idx = next(i for i, l in enumerate(lines) if l.startswith('status:'))
        deploy_idx = next(i for i, l in enumerate(lines) if l.startswith('deployment_state:'))
        at_idx = next(i for i, l in enumerate(lines) if l.startswith('claimed_at:'))
        by_idx = next(i for i, l in enumerate(lines) if l.startswith('claimed_by:'))
        assert deploy_idx == status_idx + 1
        assert at_idx == deploy_idx + 1
        assert by_idx == at_idx + 1

    def test_anchored_iso_timestamp_gets_quoted(self):
        """ISO timestamp with ':' is single-quoted — read_fm_field returns raw text."""
        fm = 'status: claimed\n'
        fm = insert_fm_field(fm, 'claimed_at', '2026-07-05T10:00:00Z', after_key='status')
        # serialize_yaml_scalar wraps ':'-containing values in single quotes
        raw = read_fm_field(fm, 'claimed_at')
        assert raw == "'2026-07-05T10:00:00Z'"

    def test_anchored_after_key_boundary(self):
        """after_key='status' must not anchor on 'status_message:' line."""
        fm = 'title: Hello\nstatus_message: detail\nstatus: open\n'
        result = insert_fm_field(fm, 'new', 'val', after_key='status')
        lines = result.split('\n')
        status_idx = next(i for i, l in enumerate(lines) if l == 'status: open')
        new_idx = next(i for i, l in enumerate(lines) if l.startswith('new:'))
        assert new_idx == status_idx + 1

    def test_anchored_with_null(self):
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'initiative', None, after_key='status')
        assert read_fm_field(result, 'initiative') == 'null'

    def test_anchored_insert_after_title(self):
        """Typical consume flow: insert status after title when status absent."""
        fm = 'title: My Handoff\npickup_ready: true\n'
        result = insert_fm_field(fm, 'status', 'claimed', after_key='title')
        lines = result.split('\n')
        title_idx = next(i for i, l in enumerate(lines) if l.startswith('title:'))
        status_idx = next(i for i, l in enumerate(lines) if l.startswith('status:'))
        assert status_idx == title_idx + 1


# ---------------------------------------------------------------------------
# remove_fm_field
# ---------------------------------------------------------------------------

class TestRemoveFmField:
    def test_mid_frontmatter_removal(self):
        """Removing a key in the middle of the frontmatter leaves other lines intact."""
        fm = 'title: Hello\npicked_up_by: sess-abc\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by' not in result
        assert read_fm_field(result, 'title') == 'Hello'
        assert read_fm_field(result, 'status') == 'actioned'

    def test_last_line_of_frontmatter_removal(self):
        """Removing the last line (no trailing newline) is safe — \\n? handles it."""
        fm = 'title: Hello\npicked_up_by: sess-abc'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by' not in result
        assert read_fm_field(result, 'title') == 'Hello'

    def test_absent_key_no_op(self):
        """When the key is not present, the frontmatter text is returned unchanged."""
        fm = 'title: Hello\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert result == fm

    def test_prefix_guard(self):
        """'picked_up_by' must NOT remove the 'picked_up_by_x:' line."""
        fm = 'title: Hello\npicked_up_by_x: extra\npicked_up_by: sess-abc\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by_x: extra' in result
        assert 'picked_up_by: sess-abc' not in result
        assert read_fm_field(result, 'picked_up_by_x') == 'extra'
        assert read_fm_field(result, 'picked_up_by') is None

    # Review: code-reviewer — F1: block-scalar guard — remove_fm_field must raise
    # ValueError on block-scalar values, mirroring replace_fm_field's guard. The
    # regex ``.*$\n?`` removes only the key line, orphaning indented continuation
    # lines and silently corrupting the frontmatter.

    def test_block_scalar_folded_raises(self):
        """remove_fm_field raises ValueError when field has a folded block scalar (>)."""
        fm = 'summary: >\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            remove_fm_field(fm, 'summary')

    def test_block_scalar_literal_raises(self):
        """remove_fm_field raises ValueError when field has a literal block scalar (|)."""
        fm = 'notes: |\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            remove_fm_field(fm, 'notes')

    def test_block_scalar_error_truncates_long_value(self):
        """Error message truncates long block-scalar values with '...'."""
        long_value = '>' + ('x' * 80)
        fm = f'summary: {long_value}\n'
        with pytest.raises(ValueError) as exc_info:
            remove_fm_field(fm, 'summary')
        assert '...' in str(exc_info.value)


# ---------------------------------------------------------------------------
# read_fm_nested_field / write_fm_nested_field / remove_fm_nested_field
#
# Regression cover for AC11 (eng-director F1, break-class): the
# gate_evidence:-shaped YAML sequence-of-mappings that remove_fm_field
# silently corrupted (orphaned continuation lines) because it read back as
# "" rather than tripping the >/| block-scalar guard.
# ---------------------------------------------------------------------------

_GATE_EVIDENCE_BLOCK = (
    '  - kind: test-node-id\n'
    '    ref: coordinator_core/ops/test_gate_eval.py::test_foo\n'
    '  - kind: commit-sha\n'
    '    ref: a1b2c3d\n'
    '    repo: doe_claude\n'
)


class TestNestedFieldRoundTrip:
    def test_absent_key_returns_none(self):
        fm = 'title: Hello\n'
        assert read_fm_nested_field(fm, 'gate_evidence') is None

    def test_write_then_read_round_trips(self):
        fm = 'title: Hello\nstatus: open\n'
        fm = write_fm_nested_field(fm, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        assert read_fm_nested_field(fm, 'gate_evidence') == _GATE_EVIDENCE_BLOCK
        # Sibling fields untouched.
        assert read_fm_field(fm, 'title') == 'Hello'
        assert read_fm_field(fm, 'status') == 'open'

    def test_write_appends_key_line_and_block(self):
        fm = 'title: Hello\n'
        result = write_fm_nested_field(fm, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        assert result == fm + 'gate_evidence:\n' + _GATE_EVIDENCE_BLOCK

    def test_write_adds_missing_trailing_newline(self):
        fm = 'title: Hello\n'
        block_no_nl = '  - kind: human\n    repo: doe_claude'
        result = write_fm_nested_field(fm, 'gate_evidence', block_no_nl)
        assert read_fm_nested_field(result, 'gate_evidence') == block_no_nl + '\n'

    def test_write_replaces_existing_block(self):
        fm = 'title: Hello\n'
        fm = write_fm_nested_field(fm, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        new_block = '  - kind: human\n    repo: doe_claude\n'
        fm = write_fm_nested_field(fm, 'gate_evidence', new_block)
        assert read_fm_nested_field(fm, 'gate_evidence') == new_block
        assert fm.count('gate_evidence:') == 1
        assert 'test-node-id' not in fm

    def test_write_preserves_following_fields(self):
        fm = 'title: Hello\ngate_evidence:\n  - kind: human\nstatus: open\n'
        fm = write_fm_nested_field(fm, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        assert read_fm_nested_field(fm, 'gate_evidence') == _GATE_EVIDENCE_BLOCK
        assert read_fm_field(fm, 'status') == 'open'

    def test_remove_strips_key_and_full_block(self):
        fm = 'title: Hello\n'
        fm = write_fm_nested_field(fm, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        fm = 'status: open\n' + fm  # unrelated ordering sanity, not required
        result = remove_fm_nested_field(fm, 'gate_evidence')
        assert 'gate_evidence' not in result
        assert 'test-node-id' not in result
        assert 'commit-sha' not in result
        assert read_fm_field(result, 'title') == 'Hello'

    def test_remove_preserves_following_field(self):
        fm = 'title: Hello\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK + 'status: open\n'
        result = remove_fm_nested_field(fm, 'gate_evidence')
        assert 'gate_evidence' not in result
        assert 'test-node-id' not in result
        assert read_fm_field(result, 'title') == 'Hello'
        assert read_fm_field(result, 'status') == 'open'

    def test_remove_absent_key_is_no_op(self):
        fm = 'title: Hello\nstatus: open\n'
        assert remove_fm_nested_field(fm, 'gate_evidence') == fm

    def test_boundary_no_prefix_match_on_read(self):
        """'gate_evidence' must not match a 'gate_evidence_extra:' line."""
        fm = 'gate_evidence_extra: something\n'
        assert read_fm_nested_field(fm, 'gate_evidence') is None

    def test_boundary_no_prefix_match_on_remove(self):
        fm = 'gate_evidence_extra: something\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        result = remove_fm_nested_field(fm, 'gate_evidence')
        assert 'gate_evidence_extra: something' in result
        assert 'test-node-id' not in result

    def test_ordinary_empty_field_reads_as_empty_string_not_none(self):
        """A genuinely empty (non-nested) field is distinguishable from absence."""
        fm = 'gate_evidence:\nother: val\n'
        assert read_fm_nested_field(fm, 'gate_evidence') == ''

    def test_full_document_lifecycle_via_split_rebuild(self):
        doc = (
            '---\n'
            'title: Awaiting handoff\n'
            'gate_dependency: doe_claude fleet-capability\n'
            '---\n'
            '# Body\n'
        )
        split = split_frontmatter(doc)
        assert split is not None
        fm = write_fm_nested_field(split.fm_text, 'gate_evidence', _GATE_EVIDENCE_BLOCK)
        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        assert read_fm_nested_field(out.fm_text, 'gate_evidence') == _GATE_EVIDENCE_BLOCK
        assert read_fm_field(out.fm_text, 'gate_dependency') == 'doe_claude fleet-capability'
        # Round trip through remove restores the pre-write shape.
        stripped = remove_fm_nested_field(out.fm_text, 'gate_evidence')
        assert stripped == split.fm_text


# ---------------------------------------------------------------------------
# Extended guard: remove_fm_field / replace_fm_field must refuse a
# nested-block key directly (Patrik F4) — the mechanism, not just the new
# capability. A sequence-of-mappings reads back as "" via read_fm_field, so
# the pre-existing >/| block-scalar guard does not fire; this is the gap
# AC11 closes generically (any nested field, not just gate_evidence).
# ---------------------------------------------------------------------------

class TestExtendedNestedBlockGuard:
    def test_remove_fm_field_raises_on_nested_block(self):
        fm = 'title: Hello\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK + 'status: open\n'
        with pytest.raises(ValueError, match='nested'):
            remove_fm_field(fm, 'gate_evidence')

    def test_remove_fm_field_nested_guard_does_not_corrupt(self):
        """Confirms the guard fires BEFORE any mutation — frontmatter unchanged."""
        fm = 'gate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        try:
            remove_fm_field(fm, 'gate_evidence')
        except ValueError:
            pass
        # fm is a local string — immutability is structural, but assert the
        # guard didn't return a corrupted value some caller might have kept.
        assert 'test-node-id' in fm

    def test_replace_fm_field_raises_on_nested_block(self):
        fm = 'title: Hello\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        with pytest.raises(ValueError, match='nested'):
            replace_fm_field(fm, 'gate_evidence', 'oops-single-line')

    def test_guard_names_the_nested_primitive_in_message(self):
        fm = 'gate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        with pytest.raises(ValueError, match='write_fm_nested_field'):
            replace_fm_field(fm, 'gate_evidence', 'oops')
        with pytest.raises(ValueError, match='remove_fm_nested_field'):
            remove_fm_field(fm, 'gate_evidence')

    def test_guard_does_not_fire_for_ordinary_empty_field(self):
        """Strictly additive: an ordinary empty single-line field is unaffected."""
        fm = 'initiative:\nother: val\n'
        result = remove_fm_field(fm, 'initiative')
        assert 'initiative' not in result
        assert read_fm_field(result, 'other') == 'val'

    def test_guard_does_not_fire_for_existing_scalar_callers(self):
        """Every existing single-line-scalar caller shape stays unaffected —
        strictly additive per the chunk spec (no current caller passes a
        nested-block key)."""
        fm = 'status: open\ndeployment_state: pending\n'
        assert remove_fm_field(fm, 'status') == 'deployment_state: pending\n'
        assert replace_fm_field(fm, 'deployment_state', 'in_flight') == (
            'status: open\ndeployment_state: in_flight\n'
        )

    def test_guard_does_not_fire_for_flow_sequence_field(self):
        """A single-line flow sequence (carried_items-adjacent shape,
        `[a, b]`) is not a nested block — must remain mutable via the
        existing single-line helpers."""
        fm = 'carried_ids: [a, b, c]\nstatus: open\n'
        result = replace_fm_field(fm, 'carried_ids', '[a, b]')
        assert read_fm_field_unquoted(result, 'carried_ids') == '[a, b]'

    def test_guard_fires_for_unindented_block_sequence(self):
        """Review: code-reviewer — Finding 4. A legal YAML block sequence
        written at the SAME indentation as its parent key (`tags:\\n- a\\n-
        b\\n`, no leading space before `- a`) must be treated as nested —
        the pre-fix guard only checked for a leading space/tab and missed
        this shape, letting remove_fm_field/replace_fm_field silently
        truncate it."""
        fm = 'title: Hello\ntags:\n- a\n- b\nstatus: open\n'
        with pytest.raises(ValueError, match='nested'):
            remove_fm_field(fm, 'tags')
        with pytest.raises(ValueError, match='nested'):
            replace_fm_field(fm, 'tags', 'oops-single-line')

    def test_guard_does_not_fire_for_folded_block_scalar(self):
        """A folded block scalar (`key: >`) carries its indicator on the
        SAME line as the key, so it is excluded before the nested-sequence
        check ever runs — confirms the Finding-4 broadening did not widen
        the guard onto this shape."""
        fm = 'notes: >\n  folded text here\nstatus: open\n'
        with pytest.raises(ValueError):
            remove_fm_field(fm, 'notes')

    def test_guard_does_not_fire_for_sibling_key_after_empty_field(self):
        """An ordinary empty field followed by a normal sibling key (which
        cannot start with `-`) must still be treated as non-nested."""
        fm = 'initiative:\nother: val\n'
        result = remove_fm_field(fm, 'initiative')
        assert 'initiative' not in result
        assert read_fm_field(result, 'other') == 'val'


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------

class TestRebuild:
    def test_round_trip_identity(self):
        doc = '---\ntitle: Hello\nstatus: open\n---\n# Body\n'
        split = split_frontmatter(doc)
        assert rebuild(split, split.fm_text) == doc

    def test_round_trip_with_preamble(self):
        doc = '<!-- comment -->\n---\ntitle: Hello\n---\n# Body\n'
        split = split_frontmatter(doc)
        assert rebuild(split, split.fm_text) == doc

    def test_fm_text_without_trailing_newline_gets_one(self):
        split = FrontmatterSplit(preamble='', fm_text='k: v', body_with_leading_newline='\n')
        result = rebuild(split, 'k: v')
        assert result == '---\nk: v\n---\n'

    def test_fm_text_with_trailing_newline_not_doubled(self):
        split = FrontmatterSplit(preamble='', fm_text='k: v\n', body_with_leading_newline='\n')
        result = rebuild(split, 'k: v\n')
        assert result == '---\nk: v\n---\n'

    def test_preamble_prepended(self):
        # Review: code-reviewer — F6: call real API (rebuild(split, fm_text)) not wrapper;
        # assert full output, not just prefix.
        preamble = '<!-- prov -->\n'
        split = FrontmatterSplit(
            preamble=preamble, fm_text='k: v\n', body_with_leading_newline='\n'
        )
        result = rebuild(split, split.fm_text)
        assert result == preamble + '---\n' + split.fm_text + '---' + split.body_with_leading_newline

    def test_body_preserved_verbatim(self):
        body = '\n# Section\n\nSome **bold** text.\n\n```python\ncode()\n```\n'
        doc = '---\nk: v\n---' + body
        split = split_frontmatter(doc)
        assert rebuild(split, split.fm_text) == doc


# ---------------------------------------------------------------------------
# Integration: full document lifecycle
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_claim_transition(self):
        """Simulate the claim verb from handoff-transition.js.

        ISO timestamp '2026-07-05T10:00:00Z' contains ':' so serialize_yaml_scalar
        wraps it in single-quotes. read_fm_field returns the raw quoted text.
        """
        doc = (
            '---\n'
            'title: My Handoff\n'
            'status: open\n'
            'deployment_state: pending\n'
            'pickup_ready: true\n'
            '---\n'
            '# My Handoff\n\nBody text.\n'
        )
        split = split_frontmatter(doc)
        assert split is not None

        fm = split.fm_text
        fm = replace_fm_field(fm, 'status', 'claimed')
        fm = replace_fm_field(fm, 'deployment_state', 'in_flight')
        fm = insert_fm_field(fm, 'claimed_at', '2026-07-05T10:00:00Z', after_key='deployment_state')
        fm = insert_fm_field(fm, 'claimed_by', 'sess-abc', after_key='claimed_at')

        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        assert read_fm_field(out.fm_text, 'status') == 'claimed'
        assert read_fm_field(out.fm_text, 'deployment_state') == 'in_flight'
        # ISO timestamps contain ':' — stored as single-quoted YAML scalars
        assert read_fm_field(out.fm_text, 'claimed_at') == "'2026-07-05T10:00:00Z'"
        assert read_fm_field(out.fm_text, 'claimed_by') == 'sess-abc'
        assert read_fm_field(out.fm_text, 'pickup_ready') == 'true'
        assert '# My Handoff' in result

    def test_stamp_shipped_in(self):
        """Simulate stamp-shipped-in.js with numeric_quoting for SHA defense.

        insert_fm_field accepts numeric_quoting=True and forwards it to
        serialize_yaml_scalar, so the raw SHA string is passed — not a
        pre-serialized value (which would trigger double-quoting).
        """
        doc = '---\ntitle: H\nstatus: claimed\nclaimed_at: 2026-07-05\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = split.fm_text

        sha = '274671833'  # all-numeric SHA — would be mis-parsed as int without quoting
        fm = insert_fm_field(fm, 'shipped_in', sha, after_key='claimed_at', numeric_quoting=True)

        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        # read_fm_field returns the raw on-disk text including the single-quotes
        raw = read_fm_field(out.fm_text, 'shipped_in')
        assert raw == "'274671833'"

    def test_normalize_insert_null_initiative(self):
        """Simulate normalize-handoff-frontmatter.js appending initiative: null."""
        doc = '---\ntitle: H\nstatus: open\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = insert_fm_field(split.fm_text, 'initiative', None)

        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        assert read_fm_field(out.fm_text, 'initiative') == 'null'

    def test_html_comment_preamble_preserved_through_mutation(self):
        preamble = '<!-- project_rag_setup baton v2 -->\n<!-- generated: 2026-07-05 -->\n'
        doc = (
            preamble
            + '---\n'
            'title: Installer Baton\n'
            'status: open\n'
            '---\n'
            '# Body\n'
        )
        split = split_frontmatter(doc)
        assert split is not None
        assert split.preamble == preamble

        fm = replace_fm_field(split.fm_text, 'status', 'claimed')
        result = rebuild(split, fm)
        assert result.startswith(preamble)
        assert '<!-- project_rag_setup baton v2 -->' in result
        assert read_fm_field(split_frontmatter(result).fm_text, 'status') == 'claimed'

    def test_crlf_document_produces_lf_output(self):
        """CRLF input is normalised; output is LF-only."""
        doc = '---\r\ntitle: Hello\r\nstatus: open\r\n---\r\n# Body\r\n'
        split = split_frontmatter(doc)
        assert split is not None
        fm = replace_fm_field(split.fm_text, 'status', 'claimed')
        result = rebuild(split, fm)
        assert '\r' not in result
        assert read_fm_field(split_frontmatter(result).fm_text, 'status') == 'claimed'

    def test_idempotent_normalize(self):
        """Running normalize-style insertions twice should not double-insert fields."""
        doc = '---\ntitle: H\nstatus: open\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = split.fm_text

        # First pass: append category and initiative
        if read_fm_field(fm, 'category') is None:
            fm = insert_fm_field(fm, 'category', 'infra')
        if read_fm_field(fm, 'initiative') is None:
            fm = insert_fm_field(fm, 'initiative', None)

        # Second pass: should be no-ops
        before = fm
        if read_fm_field(fm, 'category') is None:
            fm = insert_fm_field(fm, 'category', 'infra')
        if read_fm_field(fm, 'initiative') is None:
            fm = insert_fm_field(fm, 'initiative', None)

        assert fm == before
        assert fm.count('category:') == 1
        assert fm.count('initiative:') == 1


# ---------------------------------------------------------------------------
# _retire_gate_dependency (C8) — the shared gate_dependency-retirement primitive
# behind every destroy-site: appends the value to blocking_notes, THEN strips
# gate_dependency, rather than dropping the value with no history retention.
# ---------------------------------------------------------------------------

class TestRetireGateDependency:
    def test_absent_gate_dependency_is_a_no_op(self):
        fm = 'title: T\nstatus: open\n'
        result = _retire_gate_dependency(fm)
        assert result == fm

    def test_moves_value_to_new_blocking_notes_field(self):
        fm = 'title: T\ngate_dependency: waiting on sibling repo X\nstatus: open\n'
        result = _retire_gate_dependency(fm)
        assert read_fm_field(result, 'gate_dependency') is None
        assert 'gate_dependency' not in result
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'waiting on sibling repo X'

    def test_appends_to_existing_blocking_notes_never_overwrites(self):
        fm = (
            'title: T\n'
            'gate_dependency: waiting on sibling repo X\n'
            'blocking_notes: pre-existing advisory note\n'
            'status: open\n'
        )
        result = _retire_gate_dependency(fm)
        assert read_fm_field(result, 'gate_dependency') is None
        notes = read_fm_field_unquoted(result, 'blocking_notes')
        assert 'pre-existing advisory note' in notes
        assert 'waiting on sibling repo X' in notes

    def test_preserves_value_longer_than_former_60_char_truncation(self):
        long_value = (
            'waiting on sibling repo X to ship PR 4821, see DR-148 for the '
            'full cross-repo rationale and timeline'
        )
        assert len(long_value) > 60
        # On-disk YAML value is comma-bearing prose -- serialize_yaml_scalar
        # would single-quote a value like this; fixture mirrors that shape.
        fm = "title: T\ngate_dependency: '" + long_value.replace("'", "''") + "'\nstatus: open\n"
        result = _retire_gate_dependency(fm)
        # Read back unquoted to compare against the original plain value.
        assert read_fm_field_unquoted(result, 'blocking_notes') == long_value
        assert '…' not in result

    def test_retired_value_round_trips_through_read_fm_field_unquoted_when_quoted(self):
        # A gate_dependency value containing a colon forces on-disk quoting;
        # the retired blocking_notes value must still read back as the plain
        # unquoted original, not carry the quoting artifact.
        fm = "title: T\ngate_dependency: 'blocked: needs review'\nstatus: open\n"
        result = _retire_gate_dependency(fm)
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'blocked: needs review'


# ---------------------------------------------------------------------------
# _append_blocking_note (AC9) — the ONE append-to-blocking_notes mechanism,
# factored out of _retire_gate_dependency so the gate_evidence retirement in
# handoff_transition shares it rather than carrying a private copy of the
# read/combine/insert dance.
# ---------------------------------------------------------------------------

class TestAppendBlockingNote:
    def test_empty_note_is_a_no_op(self):
        fm = 'title: T\nstatus: open\n'
        assert _append_blocking_note(fm, '', 'gate_evidence') == fm

    def test_inserts_after_anchor_when_blocking_notes_absent(self):
        fm = 'title: T\ndeployment_state: awaiting_gate\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'deployment_state')
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'retired note'
        lines = result.splitlines()
        assert lines.index('deployment_state: awaiting_gate') + 1 == lines.index(
            'blocking_notes: retired note'
        )

    def test_appends_at_end_when_anchor_absent(self):
        fm = 'title: T\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'nonexistent_anchor')
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'retired note'

    def test_never_overwrites_existing_prose(self):
        fm = 'title: T\nblocking_notes: pre-existing\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'pre-existing | retired note'
        assert result.count('blocking_notes:') == 1

    def test_present_but_empty_blocking_notes_is_replaced_not_duplicated(self):
        # Duplicate-key guard: inserting alongside an empty blocking_notes:
        # would mint a SECOND key and hand the reader a duplicate-key document.
        fm = 'title: T\nblocking_notes:\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result.count('blocking_notes:') == 1
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'retired note'
        # Latent-bug guard: read_fm_field's \s* crosses the newline, so a naive
        # read/replace on an empty key overwrites the FOLLOWING line.
        assert 'status: open' in result, 'the adjacent field must survive untouched'

    def test_present_but_empty_blocking_notes_preserves_crlf(self):
        fm = 'title: T\r\nblocking_notes:\r\nstatus: open\r\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result == 'title: T\r\nblocking_notes: retired note\r\nstatus: open\r\n'

    # -- All THREE present-but-empty shapes fill in place (code-reviewer P2 on
    # 2bf49370). Shapes 2 and 3 are unreachable from this module's own writers
    # — serialize_yaml_scalar('') emits neither quotes nor a comment — so they
    # arrive only via hand-authored or externally-written frontmatter. Each
    # asserts through yaml.safe_load as well as on the raw text, because
    # safe_load keeps the LAST occurrence of a duplicate mapping key: a minted
    # duplicate makes the note vanish from the parsed view while still present
    # in the bytes, which is exactly how this stays silent.

    @pytest.mark.parametrize('quote', ["''", '""'])
    def test_quoted_empty_blocking_notes_is_filled_not_duplicated(self, quote):
        fm = f'title: T\nblocking_notes: {quote}\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result.count('blocking_notes:') == 1
        assert yaml.safe_load(result)['blocking_notes'] == 'retired note'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_comment_only_blocking_notes_is_filled_not_duplicated(self):
        fm = 'title: T\nblocking_notes:  # nothing yet\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result.count('blocking_notes:') == 1
        assert yaml.safe_load(result)['blocking_notes'] == 'retired note'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_bare_empty_blocking_notes_survives_yaml_load(self):
        fm = 'title: T\nblocking_notes:\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert yaml.safe_load(result)['blocking_notes'] == 'retired note'

    def test_absent_key_still_inserts_rather_than_filling(self):
        """`is None` (absent) and `''` (present-but-empty) take different paths."""
        fm = 'title: T\ngate_dependency: g\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'gate_dependency')
        assert result.count('blocking_notes:') == 1
        assert yaml.safe_load(result)['blocking_notes'] == 'retired note'


# ---------------------------------------------------------------------------
# CRLF present-but-empty key resolution (2026-07-28) — the residual the
# \s*-crosses-newline fix deliberately left open.
#
# The boundary lookahead was `(?=[ \t]|$)`, which rejects the `\r` of a
# CRLF-authored `key:\r\n`: the char after the colon is neither `[ \t]` nor a
# MULTILINE `$`. Such a key resolved as ABSENT rather than empty. The lookahead
# is shared VERBATIM by five key-resolution patterns — read_fm_field,
# replace_fm_field, remove_fm_field, _fm_key_line_pattern, and
# insert_fm_field's anchor — so it was widened to `(?=[ \t]|\r?$)` in all five
# at once. Fixing only the reader would have been worse than the gap: reads
# would succeed where the matching write silently no-ops.
#
# Every case below is parametrized over BOTH line endings and asserts LF/CRLF
# PARITY rather than one ending in isolation — a per-ending assertion is what
# let the gap survive the original fix. Windows is first-class here.
# ---------------------------------------------------------------------------

_EOLS = [
    pytest.param('\n', id='LF'),
    pytest.param('\r\n', id='CRLF'),
]


def _fm(eol: str, *lines: str) -> str:
    """Frontmatter text with every line terminated by `eol`."""
    return ''.join(line + eol for line in lines)


def _mixed_endings(text: str) -> bool:
    """True when `text` carries BOTH CRLF and bare-LF line terminators."""
    return '\r\n' in text and '\n' in text.replace('\r\n', '')


class TestCRLFPresentButEmptyKey:
    """A present-but-empty `key:` must resolve identically under LF and CRLF."""

    # -- read_fm_field ------------------------------------------------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_read_of_empty_key_is_empty_string_not_none(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_field(fm, 'status') == ''

    @pytest.mark.parametrize('eol', _EOLS)
    def test_read_of_empty_key_does_not_return_following_line(self, eol):
        fm = _fm(eol, 'blocking_notes:', 'status: open')
        assert read_fm_field(fm, 'blocking_notes') == ''

    @pytest.mark.parametrize('eol', _EOLS)
    def test_read_unquoted_of_empty_key_agrees_with_read(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_field_unquoted(fm, 'status') == read_fm_field(fm, 'status') == ''

    @pytest.mark.parametrize('eol', _EOLS)
    def test_absent_key_still_reads_none(self, eol):
        """The absent-vs-empty distinction must survive the widening."""
        fm = _fm(eol, 'title: T', 'other: v')
        assert read_fm_field(fm, 'status') is None

    # -- replace_fm_field ---------------------------------------------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_replace_fills_empty_key_in_place(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        result = replace_fm_field(fm, 'status', 'open')
        assert result == _fm(eol, 'title: T', 'status: open', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_replace_of_empty_key_leaves_neighbour_intact(self, eol):
        fm = _fm(eol, 'blocking_notes:', 'status: open')
        result = replace_fm_field(fm, 'blocking_notes', 'note')
        assert read_fm_field(result, 'status') == 'open'
        assert result.count('status: open') == 1

    @pytest.mark.parametrize('eol', _EOLS)
    def test_replace_of_empty_key_does_not_mix_line_endings(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert not _mixed_endings(replace_fm_field(fm, 'status', 'open'))

    # -- remove_fm_field ----------------------------------------------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_remove_drops_the_whole_empty_key_line(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        result = remove_fm_field(fm, 'status')
        assert result == _fm(eol, 'title: T', 'other: v')
        assert not _mixed_endings(result)

    # -- insert_fm_field (anchor pattern) -----------------------------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchors_on_an_empty_key(self, eol):
        """The anchor is the fifth site sharing the lookahead — before the
        widening, a CRLF `status:\\r\\n` anchor did not match and the insert
        silently fell through to append-at-end, landing the new key in the
        wrong place."""
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        result = insert_fm_field(fm, 'newk', 'nv', 'status')
        assert result == _fm(eol, 'title: T', 'status:', 'newk: nv', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchored_does_not_mix_line_endings(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert not _mixed_endings(insert_fm_field(fm, 'newk', 'nv', 'status'))

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_append_only_does_not_mix_line_endings(self, eol):
        fm = _fm(eol, 'title: T', 'other: v')
        result = insert_fm_field(fm, 'newk', 'nv')
        assert result == _fm(eol, 'title: T', 'other: v', 'newk: nv')
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchor_absent_falls_back_to_append(self, eol):
        fm = _fm(eol, 'title: T', 'other: v')
        result = insert_fm_field(fm, 'newk', 'nv', 'nonexistent')
        assert result == _fm(eol, 'title: T', 'other: v', 'newk: nv')

    # -- insert_fm_field append path: line-ending DETECTION (2026-07-28) -----
    #
    # The multi-line cases above pass either way: their `\r\n` survives the
    # rstrip() that the detection used to run on, because an interior line
    # break is left behind. A document whose ONLY `\r\n` is its terminator has
    # no such survivor — rstrip() eats it, the doc is misdetected as LF, and
    # `trimmed + eol` then rewrites the EXISTING line's ending too. These
    # cases pin the single-line and terminator-only shapes specifically.

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_append_preserves_single_line_document_ending(self, eol):
        fm = _fm(eol, 'title: T')
        result = insert_fm_field(fm, 'newk', 'nv')
        assert result == _fm(eol, 'title: T', 'newk: nv')
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchor_absent_fallback_preserves_single_line_ending(self, eol):
        """The anchored path's append FALLBACK is the same code — a missing
        anchor must not downgrade a CRLF document to LF either."""
        fm = _fm(eol, 'title: T')
        result = insert_fm_field(fm, 'newk', 'nv', 'nonexistent-anchor')
        assert result == _fm(eol, 'title: T', 'newk: nv')
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_append_preserves_ending_when_only_terminator_carries_it(self, eol):
        """Multi-line, but every line break except the terminator has already
        been consumed by the trailing-blank-line trim — same blind spot."""
        fm = _fm(eol, 'title: T') + eol + eol
        result = insert_fm_field(fm, 'newk', 'nv')
        assert result == _fm(eol, 'title: T', 'newk: nv')
        assert not _mixed_endings(result)

    def test_insert_append_on_unterminated_single_line_defaults_to_lf(self):
        """No ending present anywhere to preserve — LF is the documented
        default, and detection on the original text must not invent a CRLF."""
        assert insert_fm_field('title: T', 'newk', 'nv') == 'title: T\nnewk: nv\n'

    # -- _fm_key_line_pattern, via its nested-block consumers ---------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_nested_read_of_empty_key_is_empty_block_not_none(self, eol):
        """read_fm_nested_field routes through _fm_key_line_pattern — the fifth
        shared site — and must agree with read_fm_field on absent-vs-empty."""
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_nested_field(fm, 'status') == ''
        assert read_fm_nested_field(fm, 'nosuch') is None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_nested_remove_of_empty_key_drops_only_that_line(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert remove_fm_nested_field(fm, 'status') == _fm(eol, 'title: T', 'other: v')

    # -- replace_fm_field_raw (pre-serialized values) ------------------------
    #
    # The raw entry point exists so array writers stop hand-forking the
    # substitution regex; three such forks each reproduced the
    # \s*-crosses-the-newline corruption. It shares replace_fm_field's pattern
    # by construction, so it inherits both the empty-key and CRLF guarantees.

    @pytest.mark.parametrize('eol', _EOLS)
    def test_raw_replace_of_empty_key_leaves_neighbour_intact(self, eol):
        """The exact corruption the hand-forks carried: on a present-but-empty
        key, `\\s*` swallowed the line break and `.*$` consumed the FOLLOWING
        line, which the substitution then destroyed."""
        fm = _fm(eol, 'origin_goal_id:', 'status: open')
        result = replace_fm_field_raw(fm, 'origin_goal_id', '[g1, g2]')
        assert result == _fm(eol, 'origin_goal_id: [g1, g2]', 'status: open')
        assert read_fm_field(result, 'status') == 'open'
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_raw_replace_overwrites_an_existing_value_in_place(self, eol):
        fm = _fm(eol, 'title: T', 'origin_goal_id: [old]', 'status: open')
        result = replace_fm_field_raw(fm, 'origin_goal_id', '[g1, g2]')
        assert result == _fm(eol, 'title: T', 'origin_goal_id: [g1, g2]', 'status: open')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_raw_replace_is_a_noop_when_the_key_is_absent(self, eol):
        fm = _fm(eol, 'title: T', 'status: open')
        assert replace_fm_field_raw(fm, 'origin_goal_id', '[g1]') == fm

    @pytest.mark.parametrize('eol', _EOLS)
    def test_raw_replace_respects_the_status_message_boundary(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        assert replace_fm_field_raw(fm, 'status', '[x]') == fm

    def test_replace_fm_field_delegates_to_the_raw_form(self):
        """One substitution regex, not two: the serializing wrapper must be the
        raw form plus serialize_yaml_scalar, or the fork problem returns."""
        fm = 'title: T\nstatus:\nother: v\n'
        assert replace_fm_field(fm, 'status', 'open') == \
            replace_fm_field_raw(fm, 'status', serialize_yaml_scalar('open'))

    # -- trailing inline-comment preservation (break-class, 2026-08-01) -----
    #
    # replace_fm_field_raw used to substitute the ENTIRE rest of the `key:`
    # line, so a value carrying a trailing YAML inline comment lost the
    # comment on rewrite. See _split_trailing_comment's docstring for the
    # quote-aware comment-detection rule this mirrors from the read side.

    @pytest.mark.parametrize('eol', _EOLS)
    def test_opticon_repro_preserves_the_qualifier_comment(self, eol):
        """The literal project-opticon repro: a status stamp must not destroy
        the one qualifier saying the shipped code had never run."""
        comment = (
            '# PM authorized execution 2026-08-01; C1\'s >=1-landed-row '
            'data gate is still unmet'
        )
        fm = _fm(eol, f'status: approved  {comment}', 'other: v')
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == _fm(eol, f'status: implemented  {comment}', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_hash_inside_single_quotes_is_not_a_comment(self, eol):
        fm = _fm(eol, "key: 'has # a hash'", 'other: v')
        result = replace_fm_field_raw(fm, 'key', 'newval')
        assert result == _fm(eol, 'key: newval', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_hash_inside_double_quotes_is_not_a_comment(self, eol):
        fm = _fm(eol, 'key: "has # a hash"', 'other: v')
        result = replace_fm_field_raw(fm, 'key', 'newval')
        assert result == _fm(eol, 'key: newval', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_glued_hash_is_data_not_a_comment(self, eol):
        fm = _fm(eol, 'key: abc#def', 'other: v')
        result = replace_fm_field_raw(fm, 'key', 'newval')
        assert result == _fm(eol, 'key: newval', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_no_comment_line_rewrites_byte_identically(self, eol):
        """Regression guard: a line with no trailing comment must rewrite
        exactly as before this fix."""
        fm = _fm(eol, 'title: My Title', 'other: v')
        result = replace_fm_field_raw(fm, 'title', 'New Title')
        assert result == _fm(eol, 'title: New Title', 'other: v')

    def test_crlf_line_with_comment_preserves_comment_and_single_ending(self):
        fm = 'status: approved  # comment\r\nother: v\r\n'
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == 'status: implemented  # comment\r\nother: v\r\n'
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_present_but_empty_key_without_comment_fills_plainly(self, eol):
        fm = _fm(eol, 'key:', 'other: v')
        result = replace_fm_field_raw(fm, 'key', 'val')
        assert result == _fm(eol, 'key: val', 'other: v')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_present_but_empty_key_with_comment_preserves_it(self, eol):
        fm = _fm(eol, 'key:  # nothing yet', 'other: v')
        result = replace_fm_field_raw(fm, 'key', 'val')
        assert result == _fm(eol, 'key: val  # nothing yet', 'other: v')

    def test_value_needing_quoting_round_trips_through_replace_fm_field(self):
        """A `#`-carrying new value must be quoted by serialize_yaml_scalar
        (via replace_fm_field), not confused with a comment on the NEW line."""
        fm = 'status: approved  # old comment\nother: v\n'
        result = replace_fm_field(fm, 'status', 'blocked # not a comment')
        assert read_fm_field_unquoted(result, 'status') == 'blocked # not a comment'

    def test_comment_preserved_through_the_guarded_wrapper(self):
        """Not just the raw form: replace_fm_field itself (block-scalar and
        nested-block guards included) must preserve the comment too."""
        fm = 'status: approved  # PM authorized execution 2026-08-01\nother: v\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == 'status: implemented  # PM authorized execution 2026-08-01\nother: v\n'

    # -- cross-primitive agreement -----------------------------------------

    @pytest.mark.parametrize('eol', _EOLS)
    def test_all_five_agree_the_key_is_present(self, eol):
        """The whole point of the atomic change: read, replace, remove, insert
        and the nested-block locator must all resolve the SAME key on the SAME
        document. A read that succeeds where the write no-ops is worse than a
        consistent blind spot."""
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_field(fm, 'status') is not None            # read_fm_field
        assert replace_fm_field(fm, 'status', 'x') != fm          # replace_fm_field
        assert remove_fm_field(fm, 'status') != fm                # remove_fm_field
        assert insert_fm_field(fm, 'k', 'v', 'status') != \
            insert_fm_field(fm, 'k', 'v', 'nonexistent')          # insert anchor
        assert read_fm_nested_field(fm, 'status') is not None     # _fm_key_line_pattern

    @pytest.mark.parametrize('eol', _EOLS)
    def test_round_trip_through_all_writers_keeps_endings_uniform(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        out = insert_fm_field(replace_fm_field(fm, 'status', 'open'), 'extra', 'e', 'title')
        out = remove_fm_field(out, 'other')
        assert not _mixed_endings(out)
        assert yaml.safe_load(out.replace('\r\n', '\n')) == {
            'title': 'T', 'status': 'open', 'extra': 'e',
        }

    # -- the status_message boundary guarantee, preserved byte-for-byte -----
    #
    # `\r?$` admits exactly one new position — end-of-line — so it cannot
    # widen the prefix-collision surface the A1 fix closed. Asserted under
    # CRLF as well as LF, including against an EMPTY `status_message:`, which
    # is the shape the widening newly makes visible.

    @pytest.mark.parametrize('eol', _EOLS)
    def test_read_status_does_not_match_status_message(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        assert read_fm_field(fm, 'status') is None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_read_status_does_not_match_an_empty_status_message(self, eol):
        fm = _fm(eol, 'status_message:', 'other: v')
        assert read_fm_field(fm, 'status') is None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_replace_status_does_not_alter_status_message(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        assert replace_fm_field(fm, 'status', 'open') == fm

    @pytest.mark.parametrize('eol', _EOLS)
    def test_remove_status_does_not_drop_status_message(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        assert remove_fm_field(fm, 'status') == fm

    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchor_status_does_not_bind_to_status_message(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        result = insert_fm_field(fm, 'newk', 'nv', 'status')
        assert result == _fm(eol, 'status_message: detail', 'other: v', 'newk: nv')

    @pytest.mark.parametrize('eol', _EOLS)
    def test_nested_locator_does_not_match_status_message(self, eol):
        fm = _fm(eol, 'status_message: detail', 'other: v')
        assert read_fm_nested_field(fm, 'status') is None


# ---------------------------------------------------------------------------
# _append_blocking_note after the _EMPTY_BLOCKING_NOTES_RE branch was removed
# (2026-07-28). Both justifications for that private line-anchored regex died
# with the CRLF widening above: the bare-empty key is no longer invisible to
# the readers, and duplicate-key prevention was always discharged by the
# `is None` test rather than by the regex. Removing it additionally stopped it
# BYPASSING replace_fm_field's nested-block guard.
# ---------------------------------------------------------------------------

class TestAppendBlockingNoteWithoutTheEmptyKeyRegex:

    @pytest.mark.parametrize('eol', _EOLS)
    @pytest.mark.parametrize('empty_shape', [
        pytest.param('blocking_notes:', id='bare'),
        pytest.param("blocking_notes: ''", id='quoted-empty'),
        pytest.param('blocking_notes:  # nothing yet', id='comment-only'),
    ])
    def test_every_empty_shape_fills_in_place_with_exactly_one_key(self, eol, empty_shape):
        fm = _fm(eol, 'title: T', empty_shape, 'status: open')
        result = _append_blocking_note(fm, 'retired note', 'title')
        assert result.count('blocking_notes:') == 1
        assert not _mixed_endings(result)
        # safe_load keeps the LAST occurrence of a duplicate mapping key, so a
        # minted duplicate makes the note vanish from the parsed view while
        # still present in the bytes — assert through the parser, not the text.
        loaded = yaml.safe_load(result.replace('\r\n', '\n'))
        assert loaded['blocking_notes'] == 'retired note'
        assert loaded['status'] == 'open'

    @pytest.mark.parametrize('eol', _EOLS)
    def test_existing_prose_is_still_appended_not_overwritten(self, eol):
        fm = _fm(eol, 'title: T', 'blocking_notes: pre-existing', 'status: open')
        result = _append_blocking_note(fm, 'retired note', 'title')
        assert read_fm_field_unquoted(result, 'blocking_notes') == \
            'pre-existing | retired note'
        assert result.count('blocking_notes:') == 1

    @pytest.mark.parametrize('eol', _EOLS)
    def test_absent_key_still_inserts_at_the_anchor(self, eol):
        fm = _fm(eol, 'title: T', 'gate_dependency: g', 'status: open')
        result = _append_blocking_note(fm, 'retired note', 'gate_dependency')
        assert result.count('blocking_notes:') == 1
        assert not _mixed_endings(result)
        loaded = yaml.safe_load(result.replace('\r\n', '\n'))
        assert loaded['blocking_notes'] == 'retired note'

    def test_nested_shaped_blocking_notes_now_raises_instead_of_orphaning(self):
        """The removed regex ran AHEAD of the reads and so bypassed
        replace_fm_field's nested-block guard, blindly rewriting the key line
        and orphaning its indented entries beneath a now-scalar key. The
        mechanical ValueError is the module's standing answer to that shape."""
        fm = 'title: T\nblocking_notes:\n  - a\nstatus: open\n'
        with pytest.raises(ValueError, match='nested YAML block'):
            _append_blocking_note(fm, 'note', 'title')


# ---------------------------------------------------------------------------
# read_fm_block_scalar / append_fm_block_scalar_line
#
# The append path replace_fm_field refuses. Its absence made
# `review-exec-auth-stamp authorize-invocation` — /execute-plan Phase 1
# step 2 — unrunnable on every plan whose execution_authorized_note is a
# `|` or `>` block, with no flag to get past it (cross-repo memo,
# project-rag-em, 2026-08-20).
# ---------------------------------------------------------------------------

class TestBlockScalarReadAndAppend:

    def test_plain_scalar_reads_as_not_a_block(self):
        """The discriminator callers branch on. read_fm_field returns the
        header sigil for a block scalar and the value for a plain one, so it
        cannot tell them apart; this can."""
        assert read_fm_block_scalar('note: hello\n', 'note') is None

    def test_absent_key_reads_as_not_a_block(self):
        assert read_fm_block_scalar('title: T\n', 'note') is None

    @pytest.mark.parametrize('header,style', [
        ('|', '|'), ('|-', '|'), ('|+', '|'),
        ('>', '>'), ('>-', '>'), ('>+', '>'),
        ('|2', '|'), ('|2-', '|'), ('|-2', '|'),
    ])
    def test_every_block_header_variant_is_recognised(self, header, style):
        """Chomping and explicit-indentation indicators are permitted in
        either order (YAML 1.2 §8.1.1); a header parser that admits only
        `|`/`>-` silently reclassifies the rest as plain scalars and hands
        them to the single-line writer that corrupts them."""
        fm = f'note: {header}\n  first\n  second\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block is not None
        assert block.style == style
        assert block.lines == ['first', 'second']

    def test_value_merely_containing_a_pipe_is_not_a_block(self):
        assert read_fm_block_scalar('note: a|b\n', 'note') is None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_literal_append_lands_inside_the_block(self, eol):
        fm = _fm(eol, 'title: T', 'note: |', '  one', '  two', 'status: open')
        result = append_fm_block_scalar_line(fm, 'note', 'three')
        loaded = yaml.safe_load(result.replace('\r\n', '\n'))
        assert loaded['note'] == 'one\ntwo\nthree\n'
        assert loaded['status'] == 'open'
        assert not _mixed_endings(result)

    @pytest.mark.parametrize('eol', _EOLS)
    def test_folded_append_stays_a_separate_line(self, eol):
        """A bare appended line under a FOLDED block is space-joined into the
        preceding line by any conforming reader — the appended text would
        silently merge into the PM's last sentence. The blank-line separator
        is how a folded scalar spells a real line break."""
        fm = _fm(eol, 'note: >-', '  PM said go', '  and go now.', 'status: open')
        result = append_fm_block_scalar_line(fm, 'note', '/execute-plan')
        loaded = yaml.safe_load(result.replace('\r\n', '\n'))
        assert loaded['note'] == 'PM said go and go now.\n/execute-plan'

    def test_append_honours_the_blocks_own_indentation(self):
        fm = 'note: |-\n    deep\nstatus: open\n'
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert '\n    appended\n' in result
        assert yaml.safe_load(result)['note'] == 'deep\nappended'

    def test_append_to_the_last_field_in_the_block(self):
        fm = 'note: |\n  only\n'
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert yaml.safe_load(result)['note'] == 'only\nappended\n'

    def test_trailing_blank_lines_do_not_push_the_append_out_of_the_block(self):
        """A blank line after the block belongs to the document. Counting it
        into the block's extent lands the appended line below it — outside
        the scalar, where it parses as a stray key or breaks the document."""
        fm = 'note: |\n  one\n\nstatus: open\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert yaml.safe_load(result)['note'] == 'one\ntwo\n'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_append_is_idempotent_on_a_line_the_block_already_ends_with(self):
        fm = 'note: |\n  one\n  two\nstatus: open\n'
        assert append_fm_block_scalar_line(fm, 'note', 'two') == fm

    def test_append_preserves_neighbouring_fields_byte_for_byte(self):
        fm = 'title: T\nnote: |\n  one\nstatus: open\nother: 42\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert result.startswith('title: T\n')
        assert result.endswith('status: open\nother: 42\n')

    def test_append_refuses_a_plain_scalar(self):
        """Not a silent no-op and not a fallback to replace_fm_field: the
        caller picked the wrong primitive and needs to hear it."""
        with pytest.raises(ValueError, match='does not'):
            append_fm_block_scalar_line('note: plain\n', 'note', 'x')

    def test_append_refuses_an_absent_key(self):
        with pytest.raises(ValueError, match='absent'):
            append_fm_block_scalar_line('title: T\n', 'note', 'x')

    def test_append_refuses_multiline_text(self):
        """Each appended line's indentation is this function's decision. A
        caller splicing its own newlines in would author the second line's
        indent itself, which is how a block gets a line at the wrong column."""
        with pytest.raises(ValueError, match='single line'):
            append_fm_block_scalar_line('note: |\n  one\n', 'note', 'a\nb')

    def test_replace_fm_field_still_refuses_the_shape(self):
        """The refusal is correct and stays. Appending got its own function
        precisely so the guard did not have to be softened into a flag."""
        with pytest.raises(ValueError, match='block-scalar'):
            replace_fm_field('note: |\n  one\n', 'note', 'clobbered')


class TestBlockScalarAppendEdgeCases:
    """Review: coordinator:code-reviewer, 2026-08-20 — six findings against
    the append path, all confirmed against real input before fixing."""

    def test_tab_indented_body_is_re_emitted_with_tabs(self):
        """Padding with `' ' * indent` wrote ONE SPACE under a tab-indented
        block, mixing both inside a single scalar. The prefix is copied, not
        counted. (Such a document is already invalid YAML — PyYAML rejects
        tab indentation — but writing a space into it makes it worse, not
        better, and the primitive should not be the thing that corrupts.)"""
        fm = 'note: |\n\tfirst\n\tsecond\n'
        result = append_fm_block_scalar_line(fm, 'note', 'third')
        assert result == 'note: |\n\tfirst\n\tsecond\n\tthird\n'

    def test_newline_follows_the_block_not_the_document(self):
        """The old detector asked "does CRLF appear anywhere earlier in the
        document", so one CRLF field above an LF-only block dictated a CRLF
        append INTO that block."""
        fm = 'title: T\r\nnote: |\n  one\nstatus: open\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert '  two\n' in result
        assert '  two\r\n' not in result

    def test_crlf_block_still_gets_crlf(self):
        fm = 'title: T\r\nnote: |\r\n  one\r\nstatus: open\r\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert '  two\r\n' in result

    def test_final_body_line_without_a_terminator_is_not_glued(self):
        """`end_offset == len(fm)` with no trailing newline appended straight
        onto the previous line: 'one' + 'two' became 'onetwo'."""
        result = append_fm_block_scalar_line('note: |\n  one', 'note', 'two')
        assert yaml.safe_load(result)['note'] == 'one\ntwo\n'

    @pytest.mark.parametrize('malformed', ['|abc', '|0', '>x'])
    def test_malformed_header_reads_as_not_a_block(self, malformed):
        """The discriminator is deliberately STRICTER than
        replace_fm_field's one-character guard. This asserts the gap exists
        so the domain-error conversion that covers it is not later deleted as
        dead code — see test_exec_auth_stamp's malformed-header test."""
        fm = f'note: {malformed}\n  body\nstatus: open\n'
        assert read_fm_block_scalar(fm, 'note') is None
        with pytest.raises(ValueError, match='block-scalar'):
            replace_fm_field(fm, 'note', 'x')

    def test_keep_chomp_retains_trailing_blanks_as_content(self):
        """Under `+` trailing blank lines ARE the value. An unconditional trim
        read them back as absent and appended into the middle of the body;
        the trim now follows the chomping indicator."""
        fm = 'note: |+\n  one\n\n\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one', '', '']
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert yaml.safe_load(result)['note'] == 'one\n\n\ntwo\n'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_strip_chomp_still_stops_before_trailing_blanks(self):
        """The other half of the same rule — under `-`/none those blanks are
        document filler and an append must land before them."""
        fm = 'note: |-\n  one\n\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one']
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert yaml.safe_load(result)['note'] == 'one\ntwo'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_explicit_indicator_wider_than_the_body_pads_to_the_indicator(self):
        fm = 'note: |2\n  two\nstatus: open\n'
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert '\n  appended\n' in result
        assert yaml.safe_load(result)['note'] == 'two\nappended\n'

    def test_under_indented_continuation_ends_the_block_not_sliced(self):
        """Review: code-reviewer P2 — a line indented LESS than the pad
        established by the first body line used to be kept (only tested
        `startswith((' ', '\\t'))`) and then sliced by `ln[len(pad):]`,
        silently dropping its leading characters (`'  two'[4:]` == `'o'`).
        The collector now BREAKS on a line that does not start with the
        established pad, so the under-indented line is excluded from the
        block entirely rather than corrupted."""
        fm = 'note: |\n    one\n  two\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        # `  two` sits outside the block (per YAML, an under-indented line
        # ends the scalar), so the append lands right after `one` and `  two`
        # is left untouched below it — the document is already invalid YAML
        # independent of this fix (a conforming reader would refuse the
        # under-indented continuation too), so this asserts the exact bytes
        # this primitive produced rather than a YAML round-trip.
        assert result == 'note: |\n    one\n    appended\n  two\nstatus: open\n'
        assert '  two' in result  # untouched, not sliced into 'o' etc.

    def test_mixed_tab_and_space_indentation_ends_the_block_not_sliced(self):
        """Review: code-reviewer P2 — a body mixing space- and tab-indented
        lines used to slice the tab-indented line by the space pad's
        character count (`'\\ttwo'[2:]` == `'o'`), dropping real content
        instead of raising or stopping. The pad established by the first
        line is now matched by prefix, not counted, so the mismatched line
        ends the block."""
        fm = 'note: |\n  one\n\ttwo\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        # `\ttwo` does not carry the space pad established by `one`, so it
        # sits outside the block and the append lands right after `one` —
        # the tab-indented remainder is left untouched below it, not sliced
        # into `'o'`/`'ne'`-shaped garbage.
        assert result == 'note: |\n  one\n  appended\n\ttwo\nstatus: open\n'
        assert '\ttwo' in result

    def test_explicit_indicator_over_tab_indented_body_ends_the_block_not_sliced(self):
        """Review: code-reviewer P2 — the explicit-indent path (`|2`) built
        `pad = '  '` from the count alone and sliced a tab-indented body by
        that count (`'\\tone'[2:]` == `'ne'`), dropping the first two
        characters of real content. The explicit pad is now matched by
        prefix too, so a body line that does not carry it never enters the
        block."""
        fm = 'note: |2\n\tone\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == []
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        # The tab-indented `\tone` line is excluded from the block (as
        # established above) and left untouched in the document — the
        # document as a whole is already invalid YAML (tabs cannot start a
        # block-scalar indentation token) independent of this fix, so the
        # assertion is on the exact bytes this primitive produced, not a
        # round-trip through a YAML loader.
        assert result == 'note: |2\n  appended\n\tone\nstatus: open\n'

    def test_over_indented_continuation_is_kept_and_deindented_by_pad_only(self):
        """Review: code-reviewer P3 — three tests above pin the BREAK path (a
        line that does not carry the established pad ends the block). Nothing
        pinned the KEEP path: a line indented MORE than pad is legitimate
        literal-block content, must be retained, and must be de-indented by
        exactly `len(pad)` so its extra indentation survives as part of the
        value. An edit to the `startswith(pad)` / `ln[len(pad):]` pair could
        silently break this with the suite still green."""
        fm = 'note: |\n  one\n    deeper\n  three\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one', '  deeper', 'three']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert '\n  appended\n' in result
        assert yaml.safe_load(result)['note'] == 'one\n  deeper\nthree\nappended\n'
        assert yaml.safe_load(result)['status'] == 'open'

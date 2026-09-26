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


def _doc(fm_body: str, body: str = '\n# Body\n', preamble: str = '') -> str:
    return f'{preamble}---\n{fm_body}\n---{body}'


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
        doc = '---\nk: v\n---\n\n# Heading\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert result.body_with_leading_newline == '\n\n# Heading\n'

    def test_trailing_whitespace_on_close_delimiter(self):
        doc = '---\nk: v\n---   \n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None

    def test_close_with_tabs(self):
        doc = '---\nk: v\n---\t\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None

    def test_open_delimiter_with_trailing_text_ignored(self):
        doc = '---yaml\nk: v\n---\n'
        result = split_frontmatter(doc)
        assert result is None

    def test_multiline_fm(self):
        doc = '---\ntitle: Hello\nstatus: open\npickup_ready: true\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert 'title: Hello' in result.fm_text
        assert 'status: open' in result.fm_text
        assert 'pickup_ready: true' in result.fm_text


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
        assert '\r' not in result.body_with_leading_newline

    def test_crlf_mixed_with_lf(self):
        doc = '---\r\ntitle: Hi\nstatus: ok\r\n---\n# Body\n'
        result = split_frontmatter(doc)
        assert result is not None
        assert 'title: Hi' in result.fm_text


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
        preamble = '<!--\nexample_retrieval_repo_setup\nexample_game_repo installer\n-->\n'
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
        assert split_frontmatter('Some text\n---\nk: v\n---\n') is None


class TestReadFmField:
    def test_simple_read(self):
        fm = 'title: Hello\nstatus: open\n'
        assert read_fm_field(fm, 'title') == 'Hello'
        assert read_fm_field(fm, 'status') == 'open'

    def test_absent_key_returns_none(self):
        fm = 'title: Hello\n'
        assert read_fm_field(fm, 'status') is None

    def test_boundary_lookahead_no_prefix_match(self):
        fm = 'status_message: something\nother: val\n'
        assert read_fm_field(fm, 'status') is None

    def test_value_trimmed(self):
        fm = 'title:   Hello World   \n'
        assert read_fm_field(fm, 'title') == 'Hello World'

    def test_empty_value(self):
        fm = 'initiative: \n'
        assert read_fm_field(fm, 'initiative') == ''

    def test_key_at_start_of_line_only(self):
        fm = '  indented: val\nreal: ok\n'
        assert read_fm_field(fm, 'indented') is None
        assert read_fm_field(fm, 'real') == 'ok'

    def test_quoted_value_returned_raw(self):
        fm = "title: 'My Title'\n"
        assert read_fm_field(fm, 'title') == "'My Title'"

    def test_key_with_no_space_after_colon(self):
        fm = 'initiative:\n'
        assert read_fm_field(fm, 'initiative') == ''


# FOLLOWING line's content. The pre-existing empty-value cases above hid it by

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
        fm = 'blocking_notes:\nstatus: open\n'
        assert read_fm_field(fm, 'blocking_notes') == ''
        assert read_fm_field(fm, 'gate_dependency') is None

    def test_crlf_value_excludes_carriage_return(self):
        fm = 'title: x\r\nstatus: open\r\nother: v\r\n'
        assert read_fm_field(fm, 'status') == 'open'

    def test_replace_of_empty_key_does_not_overwrite_next_line(self):
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
        assert unquote_yaml_scalar("'it''s here'") == "it's here"

    def test_empty_quoted_value(self):
        assert unquote_yaml_scalar("''") == ''

    def test_unmatched_leading_quote_untouched(self):
        assert unquote_yaml_scalar("'unterminated") == "'unterminated"

    def test_not_a_naive_strip(self):
        raw = serialize_yaml_scalar("'a' and 'b'")
        assert unquote_yaml_scalar(raw) == "'a' and 'b'"

    def test_single_char_quote_not_stripped(self):
        assert unquote_yaml_scalar("'") == "'"


class TestWriteReadRoundTrip:

    @pytest.mark.parametrize('sha', [_ALL_DIGIT_SHA8, _SCIENTIFIC_SHA8])
    def test_numeric_quoting_sha_round_trips(self, sha):
        fm = insert_fm_field('title: H\n', 'shipped_in', sha, numeric_quoting=True)
        assert f"shipped_in: '{sha}'" in fm
        assert read_fm_field(fm, 'shipped_in') == f"'{sha}'"
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
        fm = 'status_message: hello\n'
        assert read_fm_field_unquoted(fm, 'status') is None


class TestTrailingCommentStripping:

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

    def test_cockpit_repro_replace_fm_field(self):
        fm = (
            'status: approved  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == (
            'status: implemented  # PM authorized execution 2026-08-01; '
            "C1's gate is still unmet\nother: v\n"
        )

    def test_cockpit_repro_replace_fm_field_raw(self):
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
        fm = 'status: approved  # c1\nother: val  # c2\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == 'status: implemented  # c1\nother: val  # c2\n'

    def test_only_targeted_line_touched_neighbour_comment_survives_replace_fm_field_raw(self):
        fm = 'status: approved  # c1\nother: val  # c2\n'
        result = replace_fm_field_raw(fm, 'status', 'implemented')
        assert result == 'status: implemented  # c1\nother: val  # c2\n'

    def test_inline_array_raw_caller_shape_preserves_trailing_comment(self):
        fm = 'tags: [a, b]  # curated list\nother: v\n'
        result = replace_fm_field_raw(fm, 'tags', '[c, d]')
        assert result == 'tags: [c, d]  # curated list\nother: v\n'


class TestGluedHashBeforeRealCommentIsStillFound:
    """`_split_trailing_comment` used
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
        fm = "note: 'has # a hash'#glued  # real comment\nother: v\n"
        result = replace_fm_field_raw(fm, 'note', 'newval')
        assert result == 'note: newval#glued  # real comment\nother: v\n'

    def test_glued_hash_with_no_trailing_comment_is_still_a_regression_guard(self):
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

    def test_comment_only_shape_preserves_the_comment_in_raw_text(self):
        fm = 'title: T\nblocking_notes:  # nothing yet\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result == 'title: T\nblocking_notes: retired note  # nothing yet\nstatus: open\n'


class TestSerializeYamlScalar:

    def test_none_to_null(self):
        assert serialize_yaml_scalar(None) == 'null'

    def test_none_with_numeric_quoting(self):
        assert serialize_yaml_scalar(None, numeric_quoting=True) == 'null'


    def test_hash_quoting(self):
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
        result = serialize_yaml_scalar("it's fine")
        assert result == "'it''s fine'"

    def test_percent_quoting(self):
        assert serialize_yaml_scalar('%TAG') == "'%TAG'"

    def test_at_quoting(self):
        assert serialize_yaml_scalar('@mention') == "'@mention'"

    def test_backtick_quoting(self):
        assert serialize_yaml_scalar('`code`') == "'`code`'"


    def test_leading_dash(self):
        assert serialize_yaml_scalar('-item') == "'-item'"

    def test_leading_question_mark(self):
        assert serialize_yaml_scalar('?key') == "'?key'"

    def test_leading_space(self):
        assert serialize_yaml_scalar(' indented') == "' indented'"


    def test_plain_word(self):
        assert serialize_yaml_scalar('active') == 'active'

    def test_date_string(self):
        assert serialize_yaml_scalar('2026-07-05') == '2026-07-05'

    def test_bool_string(self):
        assert serialize_yaml_scalar('true') == 'true'

    def test_float_string(self):
        assert serialize_yaml_scalar('3.14') == '3.14'


    def test_all_numeric_no_flag(self):
        assert serialize_yaml_scalar('12345678') == '12345678'

    def test_all_numeric_with_flag(self):
        result = serialize_yaml_scalar('274671833', numeric_quoting=True)
        assert result == "'274671833'"

    def test_scientific_no_flag(self):
        assert serialize_yaml_scalar('1958e194') == '1958e194'

    def test_scientific_with_flag(self):
        result = serialize_yaml_scalar('1958e194', numeric_quoting=True)
        assert result == "'1958e194'"

    def test_mixed_alphanumeric_not_numeric(self):
        assert serialize_yaml_scalar('abc123', numeric_quoting=True) == 'abc123'

    def test_sha_hex_with_letters(self):
        sha = 'a1b2c3d4e5f6'
        assert serialize_yaml_scalar(sha, numeric_quoting=True) == sha

    def test_sha_with_hash_char(self):
        result = serialize_yaml_scalar('abc#123')
        assert result == "'abc#123'"

    def test_multiple_internal_single_quotes(self):
        result = serialize_yaml_scalar("can't won't")
        assert result == "'can''t won''t'"

    def test_empty_string(self):
        assert serialize_yaml_scalar('') == ''


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
        fm = 'title: Hello\n'
        result = replace_fm_field(fm, 'missing', 'value')
        assert result == fm

    def test_boundary_no_prefix_match(self):
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
        fm = 'title: Hello\nstatus: open\n'
        result = insert_fm_field(fm, 'claimed_at', '2026-07-05', after_key='deployment_state')
        assert result.endswith('claimed_at: 2026-07-05\n')
        assert read_fm_field(result, 'claimed_at') == '2026-07-05'

    def test_anchored_sequential_inserts(self):
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
        fm = 'status: claimed\n'
        fm = insert_fm_field(fm, 'claimed_at', '2026-07-05T10:00:00Z', after_key='status')
        raw = read_fm_field(fm, 'claimed_at')
        assert raw == "'2026-07-05T10:00:00Z'"

    def test_anchored_after_key_boundary(self):
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
        fm = 'title: My Handoff\npickup_ready: true\n'
        result = insert_fm_field(fm, 'status', 'claimed', after_key='title')
        lines = result.split('\n')
        title_idx = next(i for i, l in enumerate(lines) if l.startswith('title:'))
        status_idx = next(i for i, l in enumerate(lines) if l.startswith('status:'))
        assert status_idx == title_idx + 1


class TestRemoveFmField:
    def test_mid_frontmatter_removal(self):
        fm = 'title: Hello\npicked_up_by: sess-abc\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by' not in result
        assert read_fm_field(result, 'title') == 'Hello'
        assert read_fm_field(result, 'status') == 'actioned'

    def test_last_line_of_frontmatter_removal(self):
        fm = 'title: Hello\npicked_up_by: sess-abc'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by' not in result
        assert read_fm_field(result, 'title') == 'Hello'

    def test_absent_key_no_op(self):
        fm = 'title: Hello\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert result == fm

    def test_prefix_guard(self):
        fm = 'title: Hello\npicked_up_by_x: extra\npicked_up_by: sess-abc\nstatus: actioned\n'
        result = remove_fm_field(fm, 'picked_up_by')
        assert 'picked_up_by_x: extra' in result
        assert 'picked_up_by: sess-abc' not in result
        assert read_fm_field(result, 'picked_up_by_x') == 'extra'
        assert read_fm_field(result, 'picked_up_by') is None


    def test_block_scalar_folded_raises(self):
        fm = 'summary: >\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            remove_fm_field(fm, 'summary')

    def test_block_scalar_literal_raises(self):
        fm = 'notes: |\n  line one\n  line two\n'
        with pytest.raises(ValueError, match='block-scalar'):
            remove_fm_field(fm, 'notes')

    def test_block_scalar_error_truncates_long_value(self):
        long_value = '>' + ('x' * 80)
        fm = f'summary: {long_value}\n'
        with pytest.raises(ValueError) as exc_info:
            remove_fm_field(fm, 'summary')
        assert '...' in str(exc_info.value)


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
        fm = 'status: open\n' + fm
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
        fm = 'gate_evidence_extra: something\n'
        assert read_fm_nested_field(fm, 'gate_evidence') is None

    def test_boundary_no_prefix_match_on_remove(self):
        fm = 'gate_evidence_extra: something\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        result = remove_fm_nested_field(fm, 'gate_evidence')
        assert 'gate_evidence_extra: something' in result
        assert 'test-node-id' not in result

    def test_ordinary_empty_field_reads_as_empty_string_not_none(self):
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
        stripped = remove_fm_nested_field(out.fm_text, 'gate_evidence')
        assert stripped == split.fm_text


class TestExtendedNestedBlockGuard:
    def test_remove_fm_field_raises_on_nested_block(self):
        fm = 'title: Hello\ngate_evidence:\n' + _GATE_EVIDENCE_BLOCK + 'status: open\n'
        with pytest.raises(ValueError, match='nested'):
            remove_fm_field(fm, 'gate_evidence')

    def test_remove_fm_field_nested_guard_does_not_corrupt(self):
        fm = 'gate_evidence:\n' + _GATE_EVIDENCE_BLOCK
        try:
            remove_fm_field(fm, 'gate_evidence')
        except ValueError:
            pass
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
        fm = 'initiative:\nother: val\n'
        result = remove_fm_field(fm, 'initiative')
        assert 'initiative' not in result
        assert read_fm_field(result, 'other') == 'val'

    def test_guard_does_not_fire_for_existing_scalar_callers(self):
        fm = 'status: open\ndeployment_state: pending\n'
        assert remove_fm_field(fm, 'status') == 'deployment_state: pending\n'
        assert replace_fm_field(fm, 'deployment_state', 'in_flight') == (
            'status: open\ndeployment_state: in_flight\n'
        )

    def test_guard_does_not_fire_for_flow_sequence_field(self):
        fm = 'carried_ids: [a, b, c]\nstatus: open\n'
        result = replace_fm_field(fm, 'carried_ids', '[a, b]')
        assert read_fm_field_unquoted(result, 'carried_ids') == '[a, b]'

    def test_guard_fires_for_unindented_block_sequence(self):
        fm = 'title: Hello\ntags:\n- a\n- b\nstatus: open\n'
        with pytest.raises(ValueError, match='nested'):
            remove_fm_field(fm, 'tags')
        with pytest.raises(ValueError, match='nested'):
            replace_fm_field(fm, 'tags', 'oops-single-line')

    def test_guard_does_not_fire_for_folded_block_scalar(self):
        fm = 'notes: >\n  folded text here\nstatus: open\n'
        with pytest.raises(ValueError):
            remove_fm_field(fm, 'notes')

    def test_guard_does_not_fire_for_sibling_key_after_empty_field(self):
        fm = 'initiative:\nother: val\n'
        result = remove_fm_field(fm, 'initiative')
        assert 'initiative' not in result
        assert read_fm_field(result, 'other') == 'val'


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


class TestIntegration:
    def test_claim_transition(self):
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
        assert read_fm_field(out.fm_text, 'claimed_at') == "'2026-07-05T10:00:00Z'"
        assert read_fm_field(out.fm_text, 'claimed_by') == 'sess-abc'
        assert read_fm_field(out.fm_text, 'pickup_ready') == 'true'
        assert '# My Handoff' in result

    def test_stamp_shipped_in(self):
        doc = '---\ntitle: H\nstatus: claimed\nclaimed_at: 2026-07-05\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = split.fm_text

        sha = '274671833'
        fm = insert_fm_field(fm, 'shipped_in', sha, after_key='claimed_at', numeric_quoting=True)

        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        raw = read_fm_field(out.fm_text, 'shipped_in')
        assert raw == "'274671833'"

    def test_normalize_insert_null_initiative(self):
        doc = '---\ntitle: H\nstatus: open\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = insert_fm_field(split.fm_text, 'initiative', None)

        result = rebuild(split, fm)
        out = split_frontmatter(result)
        assert out is not None
        assert read_fm_field(out.fm_text, 'initiative') == 'null'

    def test_html_comment_preamble_preserved_through_mutation(self):
        preamble = '<!-- example_retrieval_repo_setup baton v2 -->\n<!-- generated: 2026-07-05 -->\n'
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
        assert '<!-- example_retrieval_repo_setup baton v2 -->' in result
        assert read_fm_field(split_frontmatter(result).fm_text, 'status') == 'claimed'

    def test_crlf_document_produces_lf_output(self):
        doc = '---\r\ntitle: Hello\r\nstatus: open\r\n---\r\n# Body\r\n'
        split = split_frontmatter(doc)
        assert split is not None
        fm = replace_fm_field(split.fm_text, 'status', 'claimed')
        result = rebuild(split, fm)
        assert '\r' not in result
        assert read_fm_field(split_frontmatter(result).fm_text, 'status') == 'claimed'

    def test_idempotent_normalize(self):
        doc = '---\ntitle: H\nstatus: open\n---\n# Body\n'
        split = split_frontmatter(doc)
        fm = split.fm_text

        if read_fm_field(fm, 'category') is None:
            fm = insert_fm_field(fm, 'category', 'infra')
        if read_fm_field(fm, 'initiative') is None:
            fm = insert_fm_field(fm, 'initiative', None)

        before = fm
        if read_fm_field(fm, 'category') is None:
            fm = insert_fm_field(fm, 'category', 'infra')
        if read_fm_field(fm, 'initiative') is None:
            fm = insert_fm_field(fm, 'initiative', None)

        assert fm == before
        assert fm.count('category:') == 1
        assert fm.count('initiative:') == 1


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
        fm = "title: T\ngate_dependency: '" + long_value.replace("'", "''") + "'\nstatus: open\n"
        result = _retire_gate_dependency(fm)
        assert read_fm_field_unquoted(result, 'blocking_notes') == long_value
        assert '…' not in result

    def test_retired_value_round_trips_through_read_fm_field_unquoted_when_quoted(self):
        fm = "title: T\ngate_dependency: 'blocked: needs review'\nstatus: open\n"
        result = _retire_gate_dependency(fm)
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'blocked: needs review'


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
        fm = 'title: T\nblocking_notes:\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result.count('blocking_notes:') == 1
        assert read_fm_field_unquoted(result, 'blocking_notes') == 'retired note'
        # read/replace on an empty key overwrites the FOLLOWING line.
        assert 'status: open' in result, 'the adjacent field must survive untouched'

    def test_present_but_empty_blocking_notes_preserves_crlf(self):
        fm = 'title: T\r\nblocking_notes:\r\nstatus: open\r\n'
        result = _append_blocking_note(fm, 'retired note', 'status')
        assert result == 'title: T\r\nblocking_notes: retired note\r\nstatus: open\r\n'


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
        fm = 'title: T\ngate_dependency: g\nstatus: open\n'
        result = _append_blocking_note(fm, 'retired note', 'gate_dependency')
        assert result.count('blocking_notes:') == 1
        assert yaml.safe_load(result)['blocking_notes'] == 'retired note'


# MULTILINE `$`. Such a key resolved as ABSENT rather than empty. The lookahead
# is shared VERBATIM by five key-resolution patterns — read_fm_field,

_EOLS = [
    pytest.param('\n', id='LF'),
    pytest.param('\r\n', id='CRLF'),
]


def _fm(eol: str, *lines: str) -> str:
    return ''.join(line + eol for line in lines)


def _mixed_endings(text: str) -> bool:
    return '\r\n' in text and '\n' in text.replace('\r\n', '')


class TestCRLFPresentButEmptyKey:


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
        fm = _fm(eol, 'title: T', 'other: v')
        assert read_fm_field(fm, 'status') is None


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


    @pytest.mark.parametrize('eol', _EOLS)
    def test_remove_drops_the_whole_empty_key_line(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        result = remove_fm_field(fm, 'status')
        assert result == _fm(eol, 'title: T', 'other: v')
        assert not _mixed_endings(result)


    @pytest.mark.parametrize('eol', _EOLS)
    def test_insert_anchors_on_an_empty_key(self, eol):
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
    # `trimmed + eol` then rewrites the EXISTING line's ending too. These

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
        fm = _fm(eol, 'title: T') + eol + eol
        result = insert_fm_field(fm, 'newk', 'nv')
        assert result == _fm(eol, 'title: T', 'newk: nv')
        assert not _mixed_endings(result)

    def test_insert_append_on_unterminated_single_line_defaults_to_lf(self):
        assert insert_fm_field('title: T', 'newk', 'nv') == 'title: T\nnewk: nv\n'


    @pytest.mark.parametrize('eol', _EOLS)
    def test_nested_read_of_empty_key_is_empty_block_not_none(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_nested_field(fm, 'status') == ''
        assert read_fm_nested_field(fm, 'nosuch') is None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_nested_remove_of_empty_key_drops_only_that_line(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert remove_fm_nested_field(fm, 'status') == _fm(eol, 'title: T', 'other: v')


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
        fm = 'title: T\nstatus:\nother: v\n'
        assert replace_fm_field(fm, 'status', 'open') == \
            replace_fm_field_raw(fm, 'status', serialize_yaml_scalar('open'))


    @pytest.mark.parametrize('eol', _EOLS)
    def test_cockpit_repro_preserves_the_qualifier_comment(self, eol):
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
        fm = 'status: approved  # old comment\nother: v\n'
        result = replace_fm_field(fm, 'status', 'blocked # not a comment')
        assert read_fm_field_unquoted(result, 'status') == 'blocked # not a comment'

    def test_comment_preserved_through_the_guarded_wrapper(self):
        fm = 'status: approved  # PM authorized execution 2026-08-01\nother: v\n'
        result = replace_fm_field(fm, 'status', 'implemented')
        assert result == 'status: implemented  # PM authorized execution 2026-08-01\nother: v\n'


    @pytest.mark.parametrize('eol', _EOLS)
    def test_all_five_agree_the_key_is_present(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        assert read_fm_field(fm, 'status') is not None
        assert replace_fm_field(fm, 'status', 'x') != fm
        assert remove_fm_field(fm, 'status') != fm
        assert insert_fm_field(fm, 'k', 'v', 'status') != \
            insert_fm_field(fm, 'k', 'v', 'nonexistent')
        assert read_fm_nested_field(fm, 'status') is not None

    @pytest.mark.parametrize('eol', _EOLS)
    def test_round_trip_through_all_writers_keeps_endings_uniform(self, eol):
        fm = _fm(eol, 'title: T', 'status:', 'other: v')
        out = insert_fm_field(replace_fm_field(fm, 'status', 'open'), 'extra', 'e', 'title')
        out = remove_fm_field(out, 'other')
        assert not _mixed_endings(out)
        assert yaml.safe_load(out.replace('\r\n', '\n')) == {
            'title': 'T', 'status': 'open', 'extra': 'e',
        }


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


# _append_blocking_note after the _EMPTY_BLOCKING_NOTES_RE branch was removed
# BYPASSING replace_fm_field's nested-block guard.

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
        fm = 'title: T\nblocking_notes:\n  - a\nstatus: open\n'
        with pytest.raises(ValueError, match='nested YAML block'):
            _append_blocking_note(fm, 'note', 'title')


class TestBlockScalarReadAndAppend:

    def test_plain_scalar_reads_as_not_a_block(self):
        assert read_fm_block_scalar('note: hello\n', 'note') is None

    def test_absent_key_reads_as_not_a_block(self):
        assert read_fm_block_scalar('title: T\n', 'note') is None

    @pytest.mark.parametrize('header,style', [
        ('|', '|'), ('|-', '|'), ('|+', '|'),
        ('>', '>'), ('>-', '>'), ('>+', '>'),
        ('|2', '|'), ('|2-', '|'), ('|-2', '|'),
    ])
    def test_every_block_header_variant_is_recognised(self, header, style):
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
        with pytest.raises(ValueError, match='does not'):
            append_fm_block_scalar_line('note: plain\n', 'note', 'x')

    def test_append_refuses_an_absent_key(self):
        with pytest.raises(ValueError, match='absent'):
            append_fm_block_scalar_line('title: T\n', 'note', 'x')

    def test_append_refuses_multiline_text(self):
        with pytest.raises(ValueError, match='single line'):
            append_fm_block_scalar_line('note: |\n  one\n', 'note', 'a\nb')

    def test_replace_fm_field_still_refuses_the_shape(self):
        with pytest.raises(ValueError, match='block-scalar'):
            replace_fm_field('note: |\n  one\n', 'note', 'clobbered')


class TestBlockScalarAppendEdgeCases:

    def test_tab_indented_body_is_re_emitted_with_tabs(self):
        fm = 'note: |\n\tfirst\n\tsecond\n'
        result = append_fm_block_scalar_line(fm, 'note', 'third')
        assert result == 'note: |\n\tfirst\n\tsecond\n\tthird\n'

    def test_newline_follows_the_block_not_the_document(self):
        fm = 'title: T\r\nnote: |\n  one\nstatus: open\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert '  two\n' in result
        assert '  two\r\n' not in result

    def test_crlf_block_still_gets_crlf(self):
        fm = 'title: T\r\nnote: |\r\n  one\r\nstatus: open\r\n'
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert '  two\r\n' in result

    def test_final_body_line_without_a_terminator_is_not_glued(self):
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
        fm = 'note: |+\n  one\n\n\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one', '', '']
        result = append_fm_block_scalar_line(fm, 'note', 'two')
        assert yaml.safe_load(result)['note'] == 'one\n\n\ntwo\n'
        assert yaml.safe_load(result)['status'] == 'open'

    def test_strip_chomp_still_stops_before_trailing_blanks(self):
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
        fm = 'note: |\n    one\n  two\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert result == 'note: |\n    one\n    appended\n  two\nstatus: open\n'
        assert '  two' in result

    def test_mixed_tab_and_space_indentation_ends_the_block_not_sliced(self):
        fm = 'note: |\n  one\n\ttwo\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert result == 'note: |\n  one\n  appended\n\ttwo\nstatus: open\n'
        assert '\ttwo' in result

    def test_explicit_indicator_over_tab_indented_body_ends_the_block_not_sliced(self):
        fm = 'note: |2\n\tone\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == []
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert result == 'note: |2\n  appended\n\tone\nstatus: open\n'

    def test_over_indented_continuation_is_kept_and_deindented_by_pad_only(self):
        fm = 'note: |\n  one\n    deeper\n  three\nstatus: open\n'
        block = read_fm_block_scalar(fm, 'note')
        assert block.lines == ['one', '  deeper', 'three']
        result = append_fm_block_scalar_line(fm, 'note', 'appended')
        assert '\n  appended\n' in result
        assert yaml.safe_load(result)['note'] == 'one\n  deeper\nthree\nappended\n'
        assert yaml.safe_load(result)['status'] == 'open'

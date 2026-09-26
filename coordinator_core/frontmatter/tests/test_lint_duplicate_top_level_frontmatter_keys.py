
from __future__ import annotations

from coordinator_core.frontmatter.schema_validate import parse_frontmatter, parse_yaml

_VALID_HANDOFF_FM = """---
title: "Duplicate key probe"
created: 2026-08-11
branch: "work/probe"
status: open
predecessor: none
kind: session-handoff
handoff_phase: continuation
deployment_state: ready_to_fire
category: infra
summary: "Duplicate top-level key probe"
pickup_ready: true
deliverable_id: null
initiative: null
---

# Duplicate key probe

Body.
"""


class TestParseYamlDuplicateTopLevelKeys:

    def test_no_duplicates_collector_stays_empty(self):
        dup_keys: list[str] = []
        parse_yaml('title: x\nstatus: open\n', dup_keys=dup_keys)
        assert dup_keys == []

    def test_duplicate_top_level_key_is_collected_once(self):
        dup_keys: list[str] = []
        value = parse_yaml(
            'title: x\norigin_plan_id: pln-real-07b788\nstatus: open\norigin_plan_id: null\n',
            dup_keys=dup_keys,
        )
        # last-key-wins is UNCHANGED — the null occurrence still wins the value.
        assert value['origin_plan_id'] is None
        assert dup_keys == ['origin_plan_id']

    def test_three_occurrences_collected_on_each_repeat(self):
        dup_keys: list[str] = []
        parse_yaml('a: 1\na: 2\na: 3\n', dup_keys=dup_keys)
        assert dup_keys == ['a', 'a']

    def test_two_distinct_duplicated_keys_both_collected(self):
        dup_keys: list[str] = []
        parse_yaml('a: 1\nb: x\na: 2\nb: y\n', dup_keys=dup_keys)
        assert dup_keys == ['a', 'b']

    def test_default_collector_is_none_and_is_a_no_op(self):
        assert parse_yaml('title: x\ntitle: y\n') == {'title': 'y'}

    def test_nested_mapping_duplicate_key_is_out_of_scope(self):
        dup_keys: list[str] = []
        value = parse_yaml('outer:\n  a: 1\n  a: 2\ntitle: x\n', dup_keys=dup_keys)
        assert value == {'outer': {'a': 2}, 'title': 'x'}
        assert dup_keys == []

    def test_list_item_mapping_duplicate_key_is_out_of_scope(self):
        dup_keys: list[str] = []
        value = parse_yaml('probes:\n- a: 1\n  a: 2\n', dup_keys=dup_keys)
        assert value == {'probes': [{'a': 2}]}
        assert dup_keys == []


class TestParseFrontmatterDuplicateTopLevelKeys:

    def test_duplicate_key_reported_alongside_last_wins_value(self):
        doc = (
            '---\n'
            'handoff_id: hnd-probe-abc123\n'
            'origin_plan_id: pln-argv-fidelity-at-the-windows-l-07b788\n'
            'origin_plan_id: null\n'
            '---\n'
            'body\n'
        )
        parsed = parse_frontmatter(doc)
        assert parsed['frontmatter']['origin_plan_id'] is None
        assert parsed['duplicate_keys'] == ['origin_plan_id']

    def test_three_occurrences_dedupe_to_one_entry(self):
        doc = '---\na: 1\na: 2\na: 3\n---\nbody\n'
        parsed = parse_frontmatter(doc)
        assert parsed['duplicate_keys'] == ['a']

    def test_no_duplicates_key_present_and_empty(self):
        parsed = parse_frontmatter(_VALID_HANDOFF_FM)
        assert parsed['frontmatter'] is not None
        assert parsed.get('duplicate_keys') == []

    def test_no_frontmatter_block_has_no_duplicate_keys_field_crash(self):
        parsed = parse_frontmatter('no frontmatter here\n')
        assert parsed['frontmatter'] is None
        assert (parsed.get('duplicate_keys') or []) == []


class TestLintSingleFileReportsDuplicateTopLevelKey:

    def test_duplicate_key_on_an_otherwise_valid_handoff_is_a_violation(self, tmp_path, capsys):
        from coordinator_core.frontmatter import schema_validate

        doc = _VALID_HANDOFF_FM.replace(
            'initiative: null\n',
            'initiative: null\norigin_plan_id: pln-real-07b788\norigin_plan_id: null\n',
        )
        target = tmp_path / 'dup-key-handoff.md'
        target.write_text(doc, encoding='utf-8')

        rc = schema_validate._run_single_file_check(str(tmp_path), str(target), False)

        out = capsys.readouterr().err
        assert rc == 1, out
        assert 'valid' not in out
        assert "duplicate top-level key 'origin_plan_id'" in out

    def test_no_duplicate_key_still_reports_valid(self, tmp_path, capsys):
        from coordinator_core.frontmatter import schema_validate

        target = tmp_path / 'clean-handoff.md'
        target.write_text(_VALID_HANDOFF_FM, encoding='utf-8')

        rc = schema_validate._run_single_file_check(str(tmp_path), str(target), False)

        out = capsys.readouterr().out
        assert rc == 0, capsys.readouterr()
        assert 'valid' in out

    def test_duplicate_key_reported_even_when_no_schema_matches_the_path(self, tmp_path, capsys):
        from coordinator_core.frontmatter import schema_validate

        doc = (
            '---\n'
            'kind: totally-unregistered-kind\n'
            'title: x\n'
            'title: y\n'
            '---\n'
            'body\n'
        )
        scratch = tmp_path / 'unmatched'
        scratch.mkdir()
        target = scratch / 'not-a-known-shape.md'
        target.write_text(doc, encoding='utf-8')

        rc = schema_validate._run_single_file_check(str(tmp_path), str(target), False)

        out = capsys.readouterr().err
        assert rc == 1, out
        assert "duplicate top-level key 'title'" in out

"""credit_gate.zero_path_prefix_credit: a prefix row that landed nothing while its prefix holds dirty files."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import credit_gate

DIRTY = {"docs/decisions/a.md", "docs/decisions/b.md", "docs/decisions-old/c.md", "src/x.py"}


def test_zero_landed_prefix_row_lists_dirty_files_under_prefix():
    out = credit_gate.zero_path_prefix_credit({"C1": ["docs/decisions/"]}, {"C1": []}, DIRTY)
    assert out == {"C1": ["docs/decisions/a.md", "docs/decisions/b.md"]}


def test_prefix_without_trailing_slash_still_matches_whole_segments_only():
    out = credit_gate.zero_path_prefix_credit({"C1": ["docs/decisions"]}, {}, DIRTY)
    assert out == {"C1": ["docs/decisions/a.md", "docs/decisions/b.md"]}


def test_row_with_landed_paths_is_not_listed():
    assert credit_gate.zero_path_prefix_credit(
        {"C1": ["docs/decisions/"]}, {"C1": ["docs/decisions/a.md"]}, DIRTY
    ) == {}


def test_row_without_prefixes_is_not_listed():
    assert credit_gate.zero_path_prefix_credit({"C1": []}, {"C1": []}, DIRTY) == {}


def test_clean_prefix_is_not_listed():
    assert credit_gate.zero_path_prefix_credit({"C1": ["docs/other/"]}, {"C1": []}, DIRTY) == {}


def test_refusal_constant():
    assert credit_gate.REFUSED_CREDITED_PREFIX_ZERO_PATHS == "credited-prefix-row-zero-paths"

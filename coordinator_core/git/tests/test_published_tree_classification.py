"""Spawn-free unit tests for `coordinator_core.git.published_tree_
classification`, plus the parity test pinning its duplicated constants
against the generator's own (AC10)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from coordinator_core.git import published_tree_classification as ptc

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PORTABLE_TWO_ROWS = (
    "claude-klabauter|mirror|publish-mirror:claude_klabauter|coordinator_core|"
    "coordinator_core||foo.py,bar,!baz\n"
    "claude-klabauter-coordinator-bin|mirror|publish-mirror:claude_klabauter|"
    "coordinator/bin|coordinator/bin||setup.py\n"
)

_DECLARATIONS_ONE_DENY = """
rows:
  claude-klabauter:
    deny:
      - quux
      - name: also-denied
"""


def test_touched_published_names_empty_when_nothing_touched():
    assert ptc.touched_published_names(["docs/reference/x.md", "setup/y.yaml"]) == {}


def test_touched_published_names_finds_top_level_name_under_each_subdir():
    touched = ptc.touched_published_names(
        [
            "coordinator_core/newmod.py",
            "coordinator_core/ops/thing.py",
            "coordinator/bin/newcli.py",
            "unrelated/file.py",
        ]
    )
    assert touched == {
        "claude-klabauter": frozenset({"newmod.py", "ops"}),
        "claude-klabauter-coordinator-bin": frozenset({"newcli.py"}),
    }


def test_touched_published_names_ignores_bare_subdir_entry():
    # a path exactly equal to the subdir itself carries no top-level name
    assert ptc.touched_published_names(["coordinator_core"]) == {}


def test_field7_inclusions_splits_and_drops_empty_and_bang_entries():
    inclusions = ptc.field7_inclusions(_PORTABLE_TWO_ROWS, "claude-klabauter")
    assert inclusions == frozenset({"foo.py", "bar"})


def test_field7_inclusions_empty_for_absent_row():
    assert ptc.field7_inclusions(_PORTABLE_TWO_ROWS, "no-such-row") == frozenset()


def test_deny_names_reads_str_and_mapping_entries():
    assert ptc.deny_names(_DECLARATIONS_ONE_DENY, "claude-klabauter") == frozenset(
        {"quux", "also-denied"}
    )


def test_deny_names_empty_for_absent_row():
    assert ptc.deny_names(_DECLARATIONS_ONE_DENY, "no-such-row") == frozenset()


def test_unclassified_refuses_touched_name_not_in_field7_or_deny():
    touched = {"claude-klabauter": frozenset({"newmod.py"})}
    reads = {ptc.PORTABLE_PATH: _PORTABLE_TWO_ROWS, ptc.DECLARATIONS_PATH: _DECLARATIONS_ONE_DENY}
    result = ptc.unclassified(touched, reads.get)
    assert result == [("claude-klabauter", "newmod.py")]


def test_unclassified_allows_name_present_in_field7():
    touched = {"claude-klabauter": frozenset({"foo.py"})}
    reads = {ptc.PORTABLE_PATH: _PORTABLE_TWO_ROWS, ptc.DECLARATIONS_PATH: _DECLARATIONS_ONE_DENY}
    result = ptc.unclassified(touched, reads.get)
    assert result == []


def test_unclassified_allows_name_present_in_deny():
    touched = {"claude-klabauter": frozenset({"quux"})}
    reads = {ptc.PORTABLE_PATH: _PORTABLE_TWO_ROWS, ptc.DECLARATIONS_PATH: _DECLARATIONS_ONE_DENY}
    result = ptc.unclassified(touched, reads.get)
    assert result == []


def test_unclassified_does_not_read_declarations_when_field7_covers_everything():
    calls = []

    def read_landing(rel):
        calls.append(rel)
        if rel == ptc.PORTABLE_PATH:
            return _PORTABLE_TWO_ROWS
        raise AssertionError(f"deny yaml should not be read: {rel}")

    touched = {"claude-klabauter": frozenset({"foo.py"})}
    result = ptc.unclassified(touched, read_landing)
    assert result == []
    assert calls == [ptc.PORTABLE_PATH]


def test_unclassified_no_refusal_when_landing_portable_missing():
    touched = {"claude-klabauter": frozenset({"newmod.py"})}
    result = ptc.unclassified(touched, lambda rel: None)
    assert result == []


def test_unclassified_no_refusal_when_landing_declarations_missing():
    touched = {"claude-klabauter": frozenset({"newmod.py"})}
    reads = {ptc.PORTABLE_PATH: _PORTABLE_TWO_ROWS}
    result = ptc.unclassified(touched, reads.get)
    assert result == []


def test_rows_and_allowlist_field_parity_with_generator():
    """AC10: load the generator by file path (no spawn) and assert its
    `_ROWS` / `_ALLOWLIST_FIELD` equal this module's duplicates."""
    generator_path = _REPO_ROOT / "coordinator" / "bin" / "publish-allowlist-generate.py"
    spec = importlib.util.spec_from_file_location(
        "publish_allowlist_generate_parity", generator_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert tuple(module._ROWS) == ptc._ROWS
    assert module._ALLOWLIST_FIELD == ptc._ALLOWLIST_FIELD

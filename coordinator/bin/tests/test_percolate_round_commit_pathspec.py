"""test_percolate_round_commit_pathspec — pins the publish-round rename
defect (docs/plans/2026-08-13-the-publish-round-commits-the-names-it-a.md
§ Tasks C3, Acceptance Criteria AC6): a percolate-renamed artifact must be
committed under its POST-transform name, never its pre-transform one.

Unit-level only, per the plan's own Anti-scope ("do not re-run a real
publish to test") — `_extract_change_lines` / `_build_commit_pathspec` are
exercised directly against captured stdout shapes and a rename manifest
drawn from the real 2026-08-13 `fd801b1` failure
(`coordinator_core/claude_klabauter_root.py` -> `coordinator_core/claude_klabauter_root.py`),
never via a subprocess or a real percolate round.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_commit_pathspec.py -q
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_commit_pathspec", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()

_OLD = "coordinator_core/claude_klabauter_root.py"
_NEW = "coordinator_core/claude_klabauter_root.py"


def test_extract_change_lines_parses_rename_tag_separately():
    stdout_text = (
        "  UPDATE: coordinator_core/other.py\n"
        f"  RENAME: {_OLD} -> {_NEW}\n"
        "  NEW:    coordinator_core/brand_new.py\n"
    )
    changes, renames = _mod._extract_change_lines(stdout_text)
    assert changes == [
        ("UPDATE", "coordinator_core/other.py"),
        ("NEW", "coordinator_core/brand_new.py"),
    ]
    assert renames == [(_OLD, _NEW)]


def test_pathspec_contains_post_transform_name_not_pre_transform(tmp_path):
    """AC6: a pathspec built from a change-line set plus a rename manifest
    contains the post-transform names and none of the pre-transform ones."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", _OLD)]
    rename_pairs = [(_OLD, _NEW)]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    expected = str(dest / _NEW)
    unexpected = str(dest / _OLD)
    assert expected in pathspec
    assert unexpected not in pathspec


def test_mutation_check_rename_resolution_disabled_goes_red(tmp_path):
    """Mutation-verify (C3 body): stub the rename resolution to a no-op and
    confirm this pins the defect — a pathspec built WITHOUT the rename
    resolution still contains the pre-transform path, which is exactly the
    real `fd801b1` failure shape (declined_paths naming the pre-rename
    path)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", _OLD)]

    # No rename_pairs forwarded — the pre-fix call shape.
    pathspec_without_fix = _mod._build_commit_pathspec(str(dest), change_lines)
    assert str(dest / _OLD) in pathspec_without_fix
    assert str(dest / _NEW) not in pathspec_without_fix


def test_none_manifest_is_byte_identical_to_today(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "coordinator_core/a.py"),
        ("UPDATE", "coordinator_core/b.py"),
        ("REMOVE", "coordinator_core/c.py"),
    ]
    with_none = _mod._build_commit_pathspec(str(dest), change_lines, None)
    without_arg = _mod._build_commit_pathspec(str(dest), change_lines)
    with_empty = _mod._build_commit_pathspec(str(dest), change_lines, [])
    assert with_none == without_arg == with_empty
    assert with_none == [str(dest / rel) for _tag, rel in change_lines]


def test_rename_target_already_a_change_line_not_duplicated(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", _OLD), ("NEW", _NEW)]
    rename_pairs = [(_OLD, _NEW)]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    assert pathspec.count(str(dest / _NEW)) == 1
    assert str(dest / _OLD) not in pathspec


def test_dotdot_path_rejected_after_rename_resolution(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "safe/inside.py")]
    rename_pairs = [("safe/inside.py", "../../outside.py")]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    assert pathspec == []


def test_block_prefix_top_level_unaffected():
    """C4/AC8: a change line outside any `--- <subdir> ---` block is
    returned exactly as printed, matching today's behaviour."""
    stdout_text = "  UPDATE: wire_paths.py\n"
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [("UPDATE", "wire_paths.py")]


def test_block_prefix_one_level():
    stdout_text = (
        "  UPDATE: wire_paths.py\n"
        "  --- authz ---\n"
        "    UPDATE: classification.py\n"
        "    UPDATE: tests/test_authz_contract.py\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [
        ("UPDATE", "wire_paths.py"),
        ("UPDATE", "authz/classification.py"),
        ("UPDATE", "authz/tests/test_authz_contract.py"),
    ]


def test_block_prefix_two_levels():
    stdout_text = (
        "  --- authz ---\n"
        "    --- tests ---\n"
        "      UPDATE: test_authz_contract.py\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [("UPDATE", "authz/tests/test_authz_contract.py")]


def test_block_prefix_dedent_back_out():
    """A block closes when a header at a shallower-or-equal indent follows
    it, and also when a bare change line dedents back to the enclosing
    indent with no closing header in between (the real-run "flat audit"
    shape)."""
    stdout_text = (
        "  --- authz ---\n"
        "    UPDATE: classification.py\n"
        "  --- backlog_grind_assemble ---\n"
        "    UPDATE: apply.py\n"
        "  UPDATE: archive_stamp.py\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [
        ("UPDATE", "authz/classification.py"),
        ("UPDATE", "backlog_grind_assemble/apply.py"),
        ("UPDATE", "archive_stamp.py"),
    ]


def test_block_prefix_annotation_stripped_before_join():
    stdout_text = (
        "  --- authz ---\n"
        "    REMOVE: gone.py (not in source)\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [("REMOVE", "authz/gone.py")]


def test_block_prefix_rename_applied_to_already_prefixed_path():
    """A rename manifest is dest-relative and fully qualified, so it must
    match against the block-PREFIXED change-line path, not a bare
    basename (§ C4 body: prefix resolves first, then the rename map)."""
    stdout_text = (
        "  --- authz ---\n"
        "    UPDATE: classification.py\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    assert changes == [("UPDATE", "authz/classification.py")]

    rename_pairs = [("authz/classification.py", "authz/authz_classification.py")]
    dest = "/repo/dest"
    pathspec = _mod._build_commit_pathspec(dest, changes, rename_pairs)
    assert pathspec == [str(Path(dest) / "authz/authz_classification.py")]


def test_block_prefix_mutation_check_disabling_it_goes_red():
    """Mutation-verify (C4 body): a parser that drops the indent/header
    tracking (i.e. today's pre-fix `_CHANGE_LINE_RE` behaviour) loses the
    block prefix — this asserts the CURRENT parser does NOT exhibit that
    regression, would fail if prefix tracking were disabled."""
    stdout_text = (
        "  --- authz ---\n"
        "    UPDATE: classification.py\n"
    )
    changes, _renames = _mod._extract_change_lines(stdout_text)
    # A stub with prefix tracking disabled would yield ("UPDATE",
    # "classification.py") — bare, no "authz/" prefix. Confirm this test
    # would go red under that regression.
    unprefixed = [("UPDATE", "classification.py")]
    assert changes != unprefixed
    assert changes == [("UPDATE", "authz/classification.py")]


def test_row_attribution_two_rows_each_under_own_dest(tmp_path):
    """C5/AC9: a single `percolate-round.py <target>` invocation's real-run
    stdout spans MANY `publish.py` rows, each printing its own `  Target:
    <dest>` line. Every row's change lines must be committed under THAT
    row's dest, not the single dest resolved for the invoked target
    (measured real-run residual: 641 of C4's 689 absent entries were
    cross-row mis-attribution, dominated by 516 from `coordinator/bin`)."""
    dest_a = tmp_path / "coordinator_core"
    dest_b = tmp_path / "coordinator" / "bin"
    dest_a.mkdir(parents=True)
    dest_b.mkdir(parents=True)

    stdout_text = (
        "=== claude-klabauter (mirror) ===\n"
        f"  Target: {dest_a}\n"
        "  UPDATE: claude_klabauter_root.py\n"
        "=== claude-klabauter-coordinator-bin (mirror) ===\n"
        f"  Target: {dest_b}\n"
        "  UPDATE: bootstrap_discovery.py\n"
    )

    pathspec_seen: dict = {}
    for row_dest, row_stdout in _mod._split_stdout_by_row_dest(stdout_text, str(dest_a)):
        row_changes, row_renames = _mod._extract_change_lines(row_stdout)
        for entry in _mod._build_commit_pathspec(row_dest, row_changes, row_renames):
            pathspec_seen.setdefault(entry, None)
    pathspec = list(pathspec_seen.keys())

    assert str(dest_a / "claude_klabauter_root.py") in pathspec
    assert str(dest_b / "bootstrap_discovery.py") in pathspec
    # The cross-row-attribution bug this pins: row 2's file must NOT land
    # under row 1's dest.
    assert str(dest_a / "bootstrap_discovery.py") not in pathspec


def test_row_attribution_mutation_check_pinning_fallback_dest_goes_red(tmp_path):
    """Mutation-verify (C5 body): if every row were attributed to the
    fallback dest instead of its own reported `Target:` line — the pre-fix
    shape — row 2's file lands under row 1's dest, reproducing the real
    cross-row residual. Confirms this test would go red under that
    regression."""
    dest_a = tmp_path / "coordinator_core"
    dest_b = tmp_path / "coordinator" / "bin"
    dest_a.mkdir(parents=True)
    dest_b.mkdir(parents=True)

    stdout_text = (
        "=== claude-klabauter (mirror) ===\n"
        f"  Target: {dest_a}\n"
        "  UPDATE: claude_klabauter_root.py\n"
        "=== claude-klabauter-coordinator-bin (mirror) ===\n"
        f"  Target: {dest_b}\n"
        "  UPDATE: bootstrap_discovery.py\n"
    )

    # Pre-fix shape: every change line resolved against the single fallback
    # dest, ignoring the per-row `Target:` line entirely.
    changes, renames = _mod._extract_change_lines(stdout_text)
    mutated_pathspec = _mod._build_commit_pathspec(str(dest_a), changes, renames)

    assert str(dest_a / "bootstrap_discovery.py") in mutated_pathspec
    assert str(dest_b / "bootstrap_discovery.py") not in mutated_pathspec


def test_no_pathspec_element_names_a_directory():
    """AC7: every entry `_build_commit_pathspec` emits names a specific
    file, never a directory — the function trusts its inputs to already be
    file-shaped (upstream report lines never name a directory, § module
    docstring), so post-rename resolution must preserve that invariant
    rather than introduce a bare directory path."""
    dest = "/repo/dest"
    change_lines = [
        ("NEW", "a.py"),
        ("REMOVE", _OLD),
    ]
    rename_pairs = [(_OLD, _NEW)]

    pathspec = _mod._build_commit_pathspec(dest, change_lines, rename_pairs)

    for entry in pathspec:
        assert not entry.endswith("/") and not entry.endswith("\\")
        assert Path(entry).suffix, f"pathspec entry looks directory-shaped: {entry!r}"

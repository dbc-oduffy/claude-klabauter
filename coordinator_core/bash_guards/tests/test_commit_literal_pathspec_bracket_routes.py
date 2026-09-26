"""`:(literal)<path>` is one named operand in both commit guards.

B8 (`docs/plans/2026-09-26-inbox-blitz-part-b-engine-defects.md`): the two
independent sweeping-pathspec predicates --
`block_subagent_commit._pathspec_element_is_sweeping` and
`dispatch_checks._bt_commit_scope_operand_is_sweeping` -- both treated ANY
leading `:` as a sweeping magic-pathspec form, with no carve-out for
`:(literal)<path>`. Git's own `:(literal)` form promises `<path>` is never
glob-expanded, so a bracket in the path (e.g. a Next.js dynamic-route
directory, `src/app/[id]/page.tsx`) reads as a glob and denies a correct,
narrowly-scoped commit of a path that may not even exist on disk anymore
(the deleted-file case).

Both predicates now share ONE carve-out predicate,
`_helpers.literal_pathspec_inner_path`: exactly `:(literal)` plus a
non-empty path is unwrapped and resolved as that path with the glob test
skipped; a bare `:(literal)` and every other `:`-magic form (`:/`,
`:(top)`, `:(glob)`, ...) stays sweeping.

Non-spawning by construction: no git process is created.
"""

from __future__ import annotations

from coordinator_core.bash_guards import block_subagent_commit as bsc
from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards.block_subagent_commit import (
    _literal_pathspec_inner_path as literal_pathspec_inner_path,
)


# --- shared predicate -------------------------------------------------------


def test_literal_pathspec_inner_path_unwraps_non_empty_path():
    assert (
        literal_pathspec_inner_path(":(literal)src/app/[id]/page.tsx")
        == "src/app/[id]/page.tsx"
    )


def test_literal_pathspec_inner_path_bare_form_is_none():
    assert literal_pathspec_inner_path(":(literal)") is None


def test_literal_pathspec_inner_path_other_magic_is_none():
    assert literal_pathspec_inner_path(":/") is None
    assert literal_pathspec_inner_path(":(top)") is None
    assert literal_pathspec_inner_path(":(glob)src/**") is None


# --- block_subagent_commit._pathspec_element_is_sweeping --------------------


def test_bracket_pathspec_on_existing_file_not_sweeping(tmp_path):
    (tmp_path / "src" / "app" / "[id]").mkdir(parents=True)
    (tmp_path / "src" / "app" / "[id]" / "page.tsx").write_text("x")
    assert not bsc._pathspec_element_is_sweeping(
        "src/app/[id]/page.tsx", str(tmp_path)
    )


def test_literal_bracket_pathspec_for_deleted_file_not_sweeping(tmp_path):
    # No file exists at this path at all -- the whole point of `:(literal)`
    # is that git never globs it, so it must not need to exist on disk.
    assert not bsc._pathspec_element_is_sweeping(
        ":(literal)src/app/[id]/page.tsx", str(tmp_path)
    )


def test_bare_literal_magic_stays_sweeping(tmp_path):
    assert bsc._pathspec_element_is_sweeping(":(literal)", str(tmp_path))


def test_other_colon_magic_stays_sweeping(tmp_path):
    assert bsc._pathspec_element_is_sweeping(":/", str(tmp_path))
    assert bsc._pathspec_element_is_sweeping(":(top)", str(tmp_path))
    assert bsc._pathspec_element_is_sweeping(":(glob)src/**", str(tmp_path))


def test_plain_glob_pathspec_still_sweeping(tmp_path):
    assert bsc._pathspec_element_is_sweeping("src/*.ts", str(tmp_path))


def test_literal_pathspec_naming_repo_root_still_sweeping(tmp_path):
    assert bsc._pathspec_element_is_sweeping(":(literal).", str(tmp_path))


# --- dispatch_checks._bt_commit_scope_operand_is_sweeping -------------------


def test_dc_bracket_pathspec_on_existing_file_not_sweeping(tmp_path):
    (tmp_path / "src" / "app" / "[id]").mkdir(parents=True)
    (tmp_path / "src" / "app" / "[id]" / "page.tsx").write_text("x")
    assert not dc._bt_commit_scope_operand_is_sweeping(
        "src/app/[id]/page.tsx", str(tmp_path)
    )


def test_dc_literal_bracket_pathspec_for_deleted_file_not_sweeping(tmp_path):
    assert not dc._bt_commit_scope_operand_is_sweeping(
        ":(literal)src/app/[id]/page.tsx", str(tmp_path)
    )


def test_dc_bare_literal_magic_stays_sweeping(tmp_path):
    assert dc._bt_commit_scope_operand_is_sweeping(":(literal)", str(tmp_path))


def test_dc_other_colon_magic_stays_sweeping(tmp_path):
    assert dc._bt_commit_scope_operand_is_sweeping(":/", str(tmp_path))
    assert dc._bt_commit_scope_operand_is_sweeping(":(top)", str(tmp_path))
    assert dc._bt_commit_scope_operand_is_sweeping(":(glob)src/**", str(tmp_path))


def test_dc_plain_glob_pathspec_still_sweeping(tmp_path):
    assert dc._bt_commit_scope_operand_is_sweeping("src/*.ts", str(tmp_path))


def test_dc_literal_pathspec_naming_a_real_directory_still_sweeping(tmp_path):
    (tmp_path / "state" / "lessons").mkdir(parents=True)
    assert dc._bt_commit_scope_operand_is_sweeping(
        ":(literal)state/lessons", str(tmp_path)
    )

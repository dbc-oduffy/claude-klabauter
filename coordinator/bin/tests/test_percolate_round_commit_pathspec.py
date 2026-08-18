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
    assert renames == [(_OLD, _NEW, "file")]


def test_pathspec_contains_post_transform_name_not_pre_transform(tmp_path):
    """AC6: a pathspec built from a change-line set plus a rename manifest
    contains the post-transform names and none of the pre-transform ones."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", _OLD)]
    rename_pairs = [(_OLD, _NEW, "file")]

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
    rename_pairs = [(_OLD, _NEW, "file")]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    assert pathspec.count(str(dest / _NEW)) == 1
    assert str(dest / _OLD) not in pathspec


def test_dotdot_path_rejected_after_rename_resolution(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "safe/inside.py")]
    rename_pairs = [("safe/inside.py", "../../outside.py", "file")]

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

    rename_pairs = [("authz/classification.py", "authz/authz_classification.py", "file")]
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


def test_extract_change_lines_parses_directory_rename_tag():
    """publish.py `_report_rename_manifest` (2026-08-14 fix) tags a
    directory-kind RENAME line with a `RENAME[directory]:` PREFIX marker
    (not a trailing suffix on the path -- a suffix is a position a real
    `new_path` could occupy); `_extract_change_lines` must surface that as
    `kind='directory'` so `_build_commit_pathspec` can resolve it via
    `RenameManifest.resolve()` (prefix match), not an exact-key lookup."""
    stdout_text = "  RENAME[directory]: resolve-claude-klabauter -> resolve-claude-klabauter\n"
    _changes, renames = _mod._extract_change_lines(stdout_text)
    assert renames == [("resolve-claude-klabauter", "resolve-claude-klabauter", "directory")]


def test_extract_change_lines_does_not_misparse_path_ending_in_bracket_tag_text():
    """Review: coordinator:code-reviewer P2(a) -- a file-kind rename whose
    `new_path` literally ends with the substring ` [directory]` must NOT be
    misparsed as a directory-kind entry now that the tag is a prefix on the
    keyword, not a suffix on the path (the failure mode the trailing-suffix
    wire format was vulnerable to)."""
    stdout_text = "  RENAME: old/thing -> new/thing [directory]\n"
    _changes, renames = _mod._extract_change_lines(stdout_text)
    assert renames == [("old/thing", "new/thing [directory]", "file")]


def test_directory_rename_resolves_every_path_beneath_it(tmp_path):
    """AC1 (docs/plans/2026-08-14-publishing-runs-itself.md § C1): a
    directory rename in the rename manifest resolves every change-line path
    beneath it to its post-rename target -- the defect that made
    `declined_paths` permanently non-empty on every klabauter round. Uses
    `RenameManifest.resolve()` under the hood, never a hand-rolled
    single-pass prefix resolver."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("UPDATE", "resolve-claude-klabauter/a.py"),
        ("NEW", "resolve-claude-klabauter/nested/b.py"),
        ("REMOVE", "resolve-claude-klabauter"),
    ]
    rename_pairs = [("resolve-claude-klabauter", "resolve-claude-klabauter", "directory")]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    assert str(dest / "resolve-claude-klabauter/a.py") in pathspec
    assert str(dest / "resolve-claude-klabauter/nested/b.py") in pathspec
    assert str(dest / "resolve-claude-klabauter") in pathspec
    # None of the pre-rename paths survive resolution.
    assert str(dest / "resolve-claude-klabauter/a.py") not in pathspec
    assert str(dest / "resolve-claude-klabauter/nested/b.py") not in pathspec
    assert str(dest / "resolve-claude-klabauter") not in pathspec


def test_directory_rename_composed_with_file_rename(tmp_path):
    """`RenameManifest.resolve()`'s own reason for existing (§ that method's
    docstring): a file whose containing directory is renamed AND whose own
    basename is renamed needs BOTH records applied in sequence. A
    single-pass prefix resolver would return a path with the new directory
    but the stale basename -- exactly the declined-path symptom this chunk
    exists to eliminate in the composed case."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("UPDATE", "resolve-claude-klabauter/claude_klabauter_root.py")]
    rename_pairs = [
        ("resolve-claude-klabauter", "resolve-claude-klabauter", "directory"),
        (
            "resolve-claude-klabauter/claude_klabauter_root.py",
            "resolve-claude-klabauter/claude_klabauter_root.py",
            "file",
        ),
    ]

    pathspec = _mod._build_commit_pathspec(str(dest), change_lines, rename_pairs)

    assert (
        str(dest / "resolve-claude-klabauter/claude_klabauter_root.py") in pathspec
    )
    assert str(dest / "resolve-claude-klabauter/claude_klabauter_root.py") not in pathspec
    assert (
        str(dest / "resolve-claude-klabauter/claude_klabauter_root.py") not in pathspec
    )


def test_rename_cycle_surfaces_loudly_not_as_declined_path():
    """docs debt 2026-08-14-renamemanifest-resolve-resolves-a-rename: an
    A->B / B->A rename cycle must make `_build_commit_pathspec` raise
    `RenameCycleError`, never silently drop the entry into the containment
    check's "dropped from commit pathspec" path (which is reserved for a
    legitimately-resolved path that lands outside `dest`)."""
    dest = "/repo/dest"
    change_lines = [("UPDATE", "a.py")]
    rename_pairs = [("a.py", "b.py", "file"), ("b.py", "a.py", "file")]

    with pytest.raises(_mod._RenameCycleError):
        _mod._build_commit_pathspec(dest, change_lines, rename_pairs)


# ---------------------------------------------------------------------------
# Pathspec pre-filtering (docs/plans/2026-08-14-the-publish-round-commits-
# the-names-it-a.md follow-up): the 100-declined-path deadlock. Fix 2 drops
# two knowable-before-committing benign-decline classes from the derived
# pathspec so `scoped-git-commit` is never asked to land a path that cannot.
# `_run` is monkeypatched at module scope (never a real subprocess) per this
# file's own Anti-scope ("do not re-run a real publish to test").
# ---------------------------------------------------------------------------


def test_gitignored_path_dropped_from_pathspec(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("NEW", "__pycache__/foo.pyc")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._build_commit_pathspec(str(dest), change_lines)
    assert pathspec == []


def test_already_absent_deletion_intent_dropped_from_pathspec(tmp_path, monkeypatch):
    """A `DELETE`/`REMOVE` tag for a path absent from both dest's worktree
    and its index has nothing left to commit -- the desired end state
    (absent) already holds."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "gone-already.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._build_commit_pathspec(str(dest), change_lines)
    assert pathspec == []


def test_real_add_update_delete_still_appears_in_pathspec(tmp_path, monkeypatch):
    """A genuine deletion (still index-tracked, only worktree-removed by the
    real publish run) must NOT be filtered -- only the class of deletion
    whose path is absent from BOTH worktree and index is dropped."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "added.md"),
        ("UPDATE", "changed.md"),
        ("REMOVE", "still-tracked.sh"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "still-tracked.sh\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._build_commit_pathspec(str(dest), change_lines)
    assert pathspec == [
        str(dest / "added.md"),
        str(dest / "changed.md"),
        str(dest / "still-tracked.sh"),
    ]


def test_filter_summary_printed_to_stderr(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [
        ("NEW", "__pycache__/foo.pyc"),
        ("REMOVE", "gone-already.sh"),
        ("NEW", "kept.md"),
    ]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(
                cmd, 0, "__pycache__/foo.pyc\0", ""
            )
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._build_commit_pathspec(str(dest), change_lines)
    assert pathspec == [str(dest / "kept.md")]
    err = capsys.readouterr().err
    assert "filtered 2 path(s)" in err
    assert "1 gitignored" in err
    assert "1 deletion-intent" in err


def test_pathspec_filter_fails_open_on_undeterminable_dest_state(tmp_path, monkeypatch):
    """A path this filter cannot actually verify (probe failure, e.g. `dest`
    is not a git repo in this stub) is left in the pathspec -- a real change
    that should land is the failure mode this filter must never cause, so
    an uncertain case surfaces through `scoped-git-commit`'s own decline
    instead of being silently dropped here."""
    dest = tmp_path / "dest"
    dest.mkdir()
    change_lines = [("REMOVE", "maybe-gone.sh")]

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        if "ls-files" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 128, "", "fatal: not a git repository")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    pathspec = _mod._build_commit_pathspec(str(dest), change_lines)
    assert pathspec == [str(dest / "maybe-gone.sh")]


def test_check_ignore_result_outside_rel_paths_raises_hard(tmp_path, monkeypatch):
    """`check-ignore --stdin` can only ever echo back a member of what it was
    fed once `rel_paths` and its output share one canonical (POSIX) form --
    a returned path absent from `rel_paths` means that invariant broke, and
    this must fail LOUD (hard raise) rather than silently filtering nothing:
    the corrupting direction is a narrowed filter missing a real gitignored
    path and committing gitignored content into the mirror."""
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {str(dest / "tracked.md"): ("NEW", "tracked.md")}

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "not-in-rel-paths.md\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    with pytest.raises(ValueError, match="check-ignore reported"):
        _mod._filter_commit_pathspec(dest, str(dest), seen)


def test_check_ignore_result_subset_of_rel_paths_does_not_raise(tmp_path, monkeypatch):
    """Ordinary legitimate input -- every path `check-ignore` reports as
    ignored is drawn from what it was asked about -- must never trip the
    invariant-break raise; only a genuine mismatch does (§ the sibling
    hard-raise test above)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    seen = {
        str(dest / "ignored.pyc"): ("NEW", "ignored.pyc"),
        str(dest / "kept.md"): ("NEW", "kept.md"),
    }

    def _fake_run(cmd, **kwargs):
        if "check-ignore" in cmd:
            return _mod.subprocess.CompletedProcess(cmd, 0, "ignored.pyc\0", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod, "_run", _fake_run)
    kept = _mod._filter_commit_pathspec(dest, str(dest), seen)
    assert kept == [str(dest / "kept.md")]


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
    rename_pairs = [(_OLD, _NEW, "file")]

    pathspec = _mod._build_commit_pathspec(dest, change_lines, rename_pairs)

    for entry in pathspec:
        assert not entry.endswith("/") and not entry.endswith("\\")
        assert Path(entry).suffix, f"pathspec entry looks directory-shaped: {entry!r}"


# ---------------------------------------------------------------------------
# Real-shape regression (docs/plans/2026-08-14-the-publish-round-commits-
# the-names-it-a.md follow-up, the 83-decline defect): a REAL git repo, with
# `dest` a `dest_subdir` beneath the actual `scoped-git-commit --repo` root
# (§ `_build_commit_pathspec`'s `repo_root` docstring) -- the shape
# `test_already_absent_deletion_intent_dropped_from_pathspec` above did NOT
# exercise (flat `tmp_path/"dest"`, mocked `_run`, no outer repo root at
# all). A REAL subprocess `git` is used deliberately here (never mocked) --
# the defect is in exactly what a real `git ls-files`/`git diff --cached`
# reports for a `dest_subdir`, which a hand-rolled `_fake_run` cannot stand
# in for without begging the question.
# ---------------------------------------------------------------------------


import subprocess as _subprocess

_NO_WINDOW = {"creationflags": getattr(_subprocess, "CREATE_NO_WINDOW", 0)}


def _git_run(args, **kwargs):
    return _subprocess.run(args, capture_output=True, text=True, **_NO_WINDOW, **kwargs)


def _init_real_repo(repo_root: Path) -> None:
    _git_run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "init"],
        cwd=str(repo_root), check=True,
    )


def test_already_absent_deletion_intent_dropped_from_pathspec_real_repo_subdir(tmp_path):
    """Real-shape fidelity fix: `dest` is a `dest_subdir` under a real repo
    ROOT that never itself received the removed file (never existed at
    dest, matching one of the two `_dest_path_exists` "already gone"
    causes) -- `not _dest_path_exists(...)` must still resolve to True (drop
    it) when probed via a real `git`, not just a mocked one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    dest_subdir.mkdir()
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "--allow-empty", "-m", "second"],
        cwd=str(repo_root), check=True,
    )

    change_lines = [("REMOVE", "ops/ceremony/tests/never_existed.py")]
    pathspec = _mod._build_commit_pathspec(str(dest_subdir), change_lines)
    assert pathspec == []


def test_unstaged_worktree_deletion_kept_but_repo_root_relative(tmp_path):
    """The other `_dest_path_exists` truth (still index-tracked, only
    worktree-removed): `_filter_commit_pathspec` must still KEEP it (§
    `test_real_add_update_delete_still_appears_in_pathspec` above, semantics
    unchanged by this fix) -- but with `repo_root` given, the kept entry
    must be `repo_root`-relative, not absolute under `dest_subdir`.

    Why this matters (the actual 83-decline root cause, not the filter's
    own drop/keep call): `scoped_git_commit.commit_pipeline.explicit_stage`
    classifies an unstaged deletion via `git_native.ls_files_deleted`, which
    runs with `cwd=worktree_root` (the `--repo` value, i.e. this test's
    `repo_root`) and reports matches CWD-relative. An absolute pathspec
    entry can never equality-match that CWD-relative name -- this is
    reproduced directly below via the same real-git call `explicit_stage`
    depends on, without touching `commit_pipeline.py` itself."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core"
    tests_dir = dest_subdir / "ops" / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_claim_cli_remedy_invocations.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    # The real publish swap: physically removed, never staged (§ `publish.py
    # ::_swap_publish_staging_into_dest` -- a filesystem-level rename, never
    # a `git rm`).
    target_file.unlink()

    change_lines = [("REMOVE", "ops/ceremony/tests/test_claim_cli_remedy_invocations.py")]
    pathspec = _mod._build_commit_pathspec(
        str(dest_subdir), change_lines, repo_root=str(repo_root)
    )
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_claim_cli_remedy_invocations.py"]

    # Reproduces the actual downstream classification `explicit_stage` runs
    # (`git_native.ls_files_deleted`) -- confirms the entry this call
    # produces is the form that probe will actually recognize.
    result = _git_run(["git", "-C", str(repo_root), "ls-files", "--deleted", "--", *pathspec])
    assert result.stdout.strip() == pathspec[0]

    # Pins the actual regression: an ABSOLUTE pathspec entry still scopes
    # `git ls-files --deleted` to the right file (git accepts an absolute
    # pathspec argument fine), but the reported match is ALWAYS CWD-relative
    # -- never byte-equal to the absolute input that named it. This is
    # exactly why `commit_pipeline.explicit_stage`'s `p in worktree_deleted`
    # containment check (comparing its caller's own pathspec string against
    # this CWD-relative output set) can never succeed for an absolute `p`,
    # regardless of whether the file is genuinely, unambiguously deleted.
    absolute_form = str(target_file)
    result_absolute = _git_run(
        ["git", "-C", str(repo_root), "ls-files", "--deleted", "--", absolute_form]
    )
    assert result_absolute.stdout.strip() == pathspec[0]
    assert result_absolute.stdout.strip() != absolute_form


def test_repo_root_relative_pathspec_uses_forward_slashes(tmp_path):
    """Review: coordinatorcode-reviewer-c58be590 -- `os.path.relpath` emits
    OS-native separators (backslash on Windows), which never byte-match
    git's own always-forward-slash CWD-relative output. Pins the expected
    string explicitly (never derived from `os.sep`) so this holds on any
    host, not just a Windows one."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)
    dest_subdir = repo_root / "coordinator_core" / "ops"
    tests_dir = dest_subdir / "ceremony" / "tests"
    tests_dir.mkdir(parents=True)
    target_file = tests_dir / "test_nested_deletion.py"
    target_file.write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )
    target_file.unlink()

    change_lines = [("REMOVE", "ceremony/tests/test_nested_deletion.py")]
    pathspec = _mod._build_commit_pathspec(
        str(dest_subdir), change_lines, repo_root=str(repo_root)
    )
    assert pathspec == ["coordinator_core/ops/ceremony/tests/test_nested_deletion.py"]
    assert "\\" not in pathspec[0]


def test_sibling_row_subtree_resolves_without_dotdot(tmp_path):
    """Review: coordinatorcode-reviewer-c58be590 (live-round follow-up) --
    a real multi-row round's per-row loop calls `_build_commit_pathspec`
    once per row, each row's `dest` a DIFFERENT subtree of the same
    worktree (e.g. one row's dest is `<root>/coordinator_core`, another's
    is `<root>/coordinator/bin`). Passing the actual worktree `<root>` as
    `repo_root` (§ `_resolve_repo_root`) must resolve every row's entries
    relative to that shared root, never walking above it with `..`."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _init_real_repo(repo_root)

    row_a_dest = repo_root / "coordinator_core"
    row_a_dest.mkdir()
    row_b_dest = repo_root / "coordinator" / "bin"
    row_b_dest.mkdir(parents=True)
    (row_a_dest / "existing.py").write_text("x\n")
    (row_b_dest / "existing-tool").write_text("x\n")
    _git_run(["git", "add", "-A"], cwd=str(repo_root), check=True)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q",
         "-m", "seed"],
        cwd=str(repo_root), check=True,
    )

    row_a_changes = [("NEW", "new-file.py")]
    row_b_changes = [("NEW", "new-tool")]

    pathspec_a = _mod._build_commit_pathspec(
        str(row_a_dest), row_a_changes, repo_root=str(repo_root)
    )
    pathspec_b = _mod._build_commit_pathspec(
        str(row_b_dest), row_b_changes, repo_root=str(repo_root)
    )
    combined = pathspec_a + pathspec_b

    assert combined == ["coordinator_core/new-file.py", "coordinator/bin/new-tool"]
    for entry in combined:
        assert ".." not in entry.split("/"), entry

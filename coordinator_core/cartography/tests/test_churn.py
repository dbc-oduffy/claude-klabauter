"""
coordinator_core.cartography.tests.test_churn

Tests for coordinator_core.cartography.churn.compute_emergent_set (pure
primitive) and coordinator_core.ops.cartography_churn (the "cartography.churn"
op wrapper that derives its inputs from git).

Import guard: coordinator_core.ops.cartography_churn MUST be imported at
module load time so @register_op("cartography.churn") fires and populates
_REGISTRY.

Coverage:
  Pure primitive (coordinator_core.cartography.churn):
    (a) registry assertion — op name present in _REGISTRY after import
    (b) collation-safety   — emergent set is correct under a churned/catalogued
                              input pair whose lexical sort order is NOT the
                              same as its collation order (proves the diff is
                              a hash-set operation, not a comm/sort-dependent
                              one; mitigation (a))
    (c) source-dir prefilter — docs/ and tasks/ paths in the raw emergent set
                              are excluded from `emergent`, surfaced in
                              `excluded_by_prefilter` instead (mitigation (c))
    (d) prefilter is prefix-aware — a path like "my-docs/x.md" is NOT excluded
                              by an excluded_dirs entry of "docs/" (false-
                              positive-prefix guard)
    (e) deleted-at-HEAD (unit-level) — a path present in churned_all/absent
                              from catalogued but ALSO absent from
                              head_present is excluded from `emergent` and
                              surfaced in `deleted_at_head` (mitigation (b))
    (f) empty-inputs        — empty churned_all/catalogued/head_present yields
                              an empty ChurnResult, no exception

  History-shaped git fixture (op-level, the Staff Engineer Finding 2 — REQUIRED shape):
    (g) deleted-at-HEAD (history-shaped) — a seeded repo where path X is added
                              in an early commit and DELETED before HEAD, and
                              path Y is added and SURVIVES to HEAD. Asserts X
                              is NOT in the emergent set and Y IS. A
                              static-tree fixture (only current `git ls-files`
                              state, no create-then-delete history) would pass
                              this assertion vacuously — this fixture
                              deliberately carries commit history so the
                              HEAD cross-check path is actually exercised.
    (h) op-level source-dir prefilter — static-tree fixture is sufficient here
                              (no history dependency) per the AC6 fixture-shape
                              note.
    (i) op-level missing params → {"error": ...} for target_root/since/system_dirs
    (j) fail-closed subprocess-failure path (Finding 1, 2026-07-12-codereview-
                              slicecartography-substrate-b-wave) — _git_name_only/
                              _git_ls_files return [] (never raise) on any
                              subprocess.run exception or non-zero returncode;
                              the op-level payload stays well-formed rather than
                              propagating the exception.

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C3 (cartography.churn); AC6 fixture-shape note (the Staff Engineer Finding 2).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.cartography_churn  # noqa: F401 — fires @register_op

from coordinator_core.cartography.churn import ChurnResult, compute_emergent_set
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.cartography_churn import _cartography_churn

_OP_NAME = "cartography.churn"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.cartography_churn @register_op did not fire"
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=True,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root (mirrors
    test_handoff_stamp._make_git_repo's convention)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "churn-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Churn Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


# ---------------------------------------------------------------------------
# Pure primitive tests
# ---------------------------------------------------------------------------

def test_registry_assertion():
    assert _OP_NAME in _REGISTRY


def test_collation_unsafe_ordering_is_handled_correctly():
    """Inputs whose lexical (Python default) sort order differs from a naive
    locale-sensitive collation still produce the correct hash-set diff —
    proves the diff is set-arithmetic, not comm/sort-dependent (mitigation (a))."""
    # Mixed-case + punctuation ordering is a classic collation footgun:
    # under some locales "Z_file.py" sorts before "a_file.py"; under "C" it's
    # the reverse. A comm-based diff on mismatched sort would silently drop
    # or duplicate entries; a hash-set diff is invariant to input order.
    churned_all = ["Z_file.py", "a_file.py", "m-file.py", "new_thing.py"]
    catalogued = ["Z_file.py", "m-file.py", "a_file.py"]
    head_present = churned_all  # all still exist at HEAD
    result = compute_emergent_set(churned_all, catalogued, head_present, excluded_dirs=())
    assert result.emergent == ["new_thing.py"]


def test_source_dir_prefilter_excludes_docs_and_tasks():
    churned_all = ["docs/plans/x.md", "tasks/foo/todo.md", "src/real_change.py"]
    catalogued: list[str] = []
    head_present = churned_all
    result = compute_emergent_set(
        churned_all, catalogued, head_present, excluded_dirs=("docs/", "tasks/", "archive/")
    )
    assert result.emergent == ["src/real_change.py"]
    assert set(result.excluded_by_prefilter) == {"docs/plans/x.md", "tasks/foo/todo.md"}


def test_prefilter_is_prefix_aware_not_substring():
    """A path like 'my-docs/x.md' must NOT be excluded by excluded_dirs=("docs/",)
    — the prefilter is path-segment aware, not a raw substring/startswith test."""
    churned_all = ["my-docs/x.md", "docs/y.md"]
    catalogued: list[str] = []
    head_present = churned_all
    result = compute_emergent_set(
        churned_all, catalogued, head_present, excluded_dirs=("docs/",)
    )
    assert "my-docs/x.md" in result.emergent
    assert "docs/y.md" in result.excluded_by_prefilter
    assert "docs/y.md" not in result.emergent


def test_deleted_at_head_unit_level_excluded_from_emergent():
    churned_all = ["src/gone.py", "src/still_here.py"]
    catalogued: list[str] = []
    head_present = ["src/still_here.py"]  # src/gone.py no longer at HEAD
    result = compute_emergent_set(churned_all, catalogued, head_present, excluded_dirs=())
    assert result.emergent == ["src/still_here.py"]
    assert result.deleted_at_head == ["src/gone.py"]


def test_empty_inputs_yield_empty_result_no_exception():
    result = compute_emergent_set([], [], [], excluded_dirs=())
    assert result == ChurnResult(emergent=[], excluded_by_prefilter=[], deleted_at_head=[])


# ---------------------------------------------------------------------------
# History-shaped git fixture — the Staff Engineer Finding 2 (op-level, REQUIRED shape)
# ---------------------------------------------------------------------------

def test_op_deleted_at_head_requires_history_shaped_fixture(tmp_path):
    """CRITICAL (the Staff Engineer Finding 2): the deleted-at-HEAD mitigation can only be
    proven with a HISTORY-shaped fixture — a repo where a path is added in an
    early commit and then deleted before HEAD. A static-tree fixture (only
    current `git ls-files` state) cannot exercise the "deleted between the
    diff window and HEAD" cross-check at all, so this test deliberately
    creates the create-then-delete history:

      commit 1: add src/deleted_path.py + src/survivor.py
      commit 2: delete src/deleted_path.py

    `since` is set to before commit 1 so both paths appear in the git log
    diff window. Asserts:
      - src/deleted_path.py is NOT in emergent (correctly filtered — it is a
        deletion record, not uncatalogued architecture)
      - src/survivor.py IS in emergent (it is uncatalogued AND survives to HEAD)
    """
    repo = _make_git_repo(tmp_path)

    src = repo / "src"
    src.mkdir()
    (src / "deleted_path.py").write_text("x = 1\n", encoding="utf-8")
    (src / "survivor.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add deleted_path.py and survivor.py")

    (src / "deleted_path.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete deleted_path.py")

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                # No catalogued system dirs contain src/ — both paths are
                # "uncatalogued" from the catalogued-diff's perspective.
                "system_dirs": ["nonexistent_system_dir"],
                "excluded_dirs": [],
            },
            repo_root=repo,
        )
    )

    assert "src/deleted_path.py" not in result["emergent"]
    assert "src/deleted_path.py" in result["deleted_at_head"]
    assert "src/survivor.py" in result["emergent"]


# ---------------------------------------------------------------------------
# Op-level tests — static-tree fixtures sufficient (AC6 fixture-shape note)
# ---------------------------------------------------------------------------

def test_op_source_dir_prefilter_static_tree(tmp_path):
    repo = _make_git_repo(tmp_path)

    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("z = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add docs + src")

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                "system_dirs": ["nonexistent_system_dir"],
                # C9: excluded_dirs no longer carries an implicit default —
                # explicit here so this prefilter assertion still exercises
                # the prefilter path rather than the no-filter default.
                "excluded_dirs": ["docs/", "tasks/", "archive/"],
            },
            repo_root=repo,
        )
    )

    assert "src/real.py" in result["emergent"]
    assert "docs/notes.md" not in result["emergent"]
    assert "docs/notes.md" in result["excluded_by_prefilter"]


def test_op_churn_ratio_and_catalogued_count(tmp_path):
    """A5: churn_ratio = len(catalogued churn diff) / catalogued_count, where
    catalogued_count is the number of files tracked under system_dirs at
    HEAD (NOT the diff-scoped churn list) — proves the op returns the ratio
    and its denominator, not a pre-applied threshold verdict."""
    repo = _make_git_repo(tmp_path)

    src = repo / "src"
    src.mkdir()
    (src / "a.py").write_text("a = 1\n", encoding="utf-8")
    (src / "b.py").write_text("b = 1\n", encoding="utf-8")
    (src / "c.py").write_text("c = 1\n", encoding="utf-8")
    (src / "d.py").write_text("d = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add 4 files under src/")

    # Since-window starts strictly AFTER the initial commit, so only the
    # subsequent touch of a.py counts as "churned" — catalogued_count (4)
    # still reflects everything tracked under src/ at HEAD, not just what
    # fell inside the diff window. Second commit's date is forced 1 hour
    # later (explicit committer date) so a same-second `--since` boundary
    # can't fold both commits into the window.
    since = "2050-01-01"

    (src / "a.py").write_text("a = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    later_env = dict(os.environ)
    later_env["GIT_AUTHOR_DATE"] = "2099-01-01T00:00:00+00:00"
    later_env["GIT_COMMITTER_DATE"] = "2099-01-01T00:00:00+00:00"
    subprocess.run(
        ["git", "commit", "-m", "touch a.py"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=later_env,
    )

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": since,
                "system_dirs": ["src"],
            },
            repo_root=repo,
        )
    )

    assert result["catalogued_count"] == 4
    assert result["churn_ratio"] == 1 / 4  # only a.py inside the since-window


def test_op_churn_ratio_bounded_with_deletion_and_rename(tmp_path):
    """Eng-director ruling (2026-08-06, churn_ratio finding): the ratio's
    numerator (raw `--name-only` diff) and denominator (`git ls-files` at
    HEAD) previously drew from different populations — a deleted or renamed
    path inflated the numerator without inflating the denominator, so the
    ratio could exceed 1.0 (measured 1.063 against this repo pre-fix). This
    fixture reproduces that exact shape: within the since-window, one file is
    deleted and another is renamed (git log --name-only records BOTH the
    old and new path for a rename), while only two files survive at HEAD.
    Asserts the ratio stays within [0.0, 1.0] and is computed from the
    HEAD-intersected population, not the raw diff count."""
    repo = _make_git_repo(tmp_path)

    src = repo / "src"
    src.mkdir()
    (src / "a.py").write_text("a = 1\n", encoding="utf-8")
    (src / "b.py").write_text("b = 1\n", encoding="utf-8")
    (src / "old_name.py").write_text("c = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add 3 files under src/")

    since = "2050-01-01"

    later_env = dict(os.environ)
    later_env["GIT_AUTHOR_DATE"] = "2099-01-01T00:00:00+00:00"
    later_env["GIT_COMMITTER_DATE"] = "2099-01-01T00:00:00+00:00"

    # Delete b.py and rename old_name.py -> new_name.py: both operations
    # touch paths that raw --name-only records but that are NOT (fully, or
    # under their old name) present in `git ls-files` at HEAD.
    (src / "b.py").unlink()
    (src / "old_name.py").rename(src / "new_name.py")
    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "commit", "-m", "delete b.py, rename old_name.py -> new_name.py"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=later_env,
    )

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": since,
                "system_dirs": ["src"],
            },
            repo_root=repo,
        )
    )

    # HEAD now tracks a.py + new_name.py only (2 files) — the deletion and
    # rename's old-path record must not inflate the numerator past 2.
    assert result["catalogued_count"] == 2
    assert 0.0 <= result["churn_ratio"] <= 1.0
    assert result["churn_ratio"] == 1 / 2


def test_op_missing_target_root_returns_error():
    result = _run(_cartography_churn({}, repo_root=None))
    assert "error" in result


def test_op_missing_since_returns_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = _run(
        _cartography_churn({"target_root": str(repo)}, repo_root=repo)
    )
    assert "error" in result


def test_op_missing_system_dirs_returns_error(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = _run(
        _cartography_churn(
            {"target_root": str(repo), "since": "1970-01-01"}, repo_root=repo
        )
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Fail-closed subprocess-failure path (Finding 1, 2026-07-12-codereview-
# slicecartography-substrate-b-wave) — _git_name_only/_git_ls_files return []
# on ANY subprocess failure (docstring: "worst case an all-tree diff is
# misreported as fully emergent... never a crash"). Previously untested: every
# other op-level test uses a real, successfully-running git repo. Monkeypatch
# subprocess.run to raise on every invocation, forcing all three git-shelling
# calls (_git_name_only x2, _git_ls_files x1) down their except-Exception ->
# [] branch, and assert the op still returns a well-formed payload rather
# than propagating the exception.
# ---------------------------------------------------------------------------


def test_op_subprocess_failure_is_fail_closed_not_a_crash(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)

    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("z = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add src/real.py")

    import coordinator_core.ops.cartography_churn as churn_op_mod

    def _always_raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "git", timeout=1)

    monkeypatch.setattr(churn_op_mod.subprocess, "run", _always_raise)

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                "system_dirs": ["src"],
            },
            repo_root=repo,
        )
    )

    # Well-formed payload — never an exception, never None-shaped fields.
    # Per-field, not equality: the reply is now a superset (C3's additive
    # atlas fields), so this pins "fail-closed, well-formed" rather than
    # "exactly five keys" (EM ruling, chunk C4).
    assert "error" not in result
    assert result["emergent"] == []
    assert result["excluded_by_prefilter"] == []
    assert result["deleted_at_head"] == []
    assert result["churn_ratio"] == 0.0
    assert result["catalogued_count"] == 0
    # This fixture has no docs/architecture/ at all — the fail-closed atlas
    # behaviour the fixture actually exercises (AC8).
    assert "atlas_unreadable" in result
    assert result["atlas_unreadable"]["reason"]
    assert result["uncatalogued"] == []
    assert result["drifted_systems"] == []
    assert result["last_mapped"] is None
    assert result["catalogued_source_count"] == 0


def test_git_name_only_returns_empty_list_on_subprocess_exception(tmp_path, monkeypatch):
    """Unit-level: _git_name_only itself returns [] (not None, not raised) when
    subprocess.run raises — the "worst case is over-inclusion, never a crash"
    contract the module docstring claims."""
    import coordinator_core.ops.cartography_churn as churn_op_mod

    def _always_raise(*args, **kwargs):
        raise RuntimeError("simulated git binary missing")

    monkeypatch.setattr(churn_op_mod.subprocess, "run", _always_raise)

    assert churn_op_mod._git_name_only(tmp_path, "1970-01-01") == []


def test_git_ls_files_returns_empty_list_on_subprocess_exception(tmp_path, monkeypatch):
    """Unit-level: _git_ls_files itself returns [] (not None, not raised) when
    subprocess.run raises."""
    import coordinator_core.ops.cartography_churn as churn_op_mod

    def _always_raise(*args, **kwargs):
        raise RuntimeError("simulated git binary missing")

    monkeypatch.setattr(churn_op_mod.subprocess, "run", _always_raise)

    assert churn_op_mod._git_ls_files(tmp_path) == []


def test_git_name_only_returns_empty_list_on_nonzero_returncode(tmp_path, monkeypatch):
    """Non-zero returncode (e.g. not a git repo) is a distinct failure path from
    a raised exception — both must return [] (fail-closed), not raise."""
    import coordinator_core.ops.cartography_churn as churn_op_mod

    class _FakeCompletedProcess:
        returncode = 128
        stdout = ""

    def _fake_run(*args, **kwargs):
        return _FakeCompletedProcess()

    monkeypatch.setattr(churn_op_mod.subprocess, "run", _fake_run)

    assert churn_op_mod._git_name_only(tmp_path, "1970-01-01") == []
    assert churn_op_mod._git_ls_files(tmp_path) == []


# ---------------------------------------------------------------------------
# C9 — excluded_dirs no longer carries a tree-specific default; the caller
# supplies the set (docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md,
# AC10).
# ---------------------------------------------------------------------------


def test_op_excluded_dirs_omitted_applies_no_prefilter(tmp_path):
    """A caller that omits excluded_dirs gets NO prefiltering — the retired
    DEFAULT_EXCLUDED_DIRS ("docs/", "tasks/", "archive/") no longer applies
    implicitly. docs/notes.md must now surface in `emergent`, not
    `excluded_by_prefilter` — the mirror-image assertion of
    test_op_source_dir_prefilter_static_tree, which pins the same fixture
    shape WITH an explicit excluded_dirs."""
    repo = _make_git_repo(tmp_path)

    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("z = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add docs + src")

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                "system_dirs": ["nonexistent_system_dir"],
                # excluded_dirs deliberately omitted.
            },
            repo_root=repo,
        )
    )

    assert "src/real.py" in result["emergent"]
    assert "docs/notes.md" in result["emergent"]
    assert result["excluded_by_prefilter"] == []


def test_op_excluded_dirs_non_list_value_applies_no_prefilter(tmp_path):
    """A non-list excluded_dirs (e.g. a caller-supplied string, which is not
    the wire contract) falls back to no prefiltering — same posture as
    omitted, never the retired default."""
    repo = _make_git_repo(tmp_path)

    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add docs")

    result = _run(
        _cartography_churn(
            {
                "target_root": str(repo),
                "since": "1970-01-01",
                "system_dirs": ["nonexistent_system_dir"],
                "excluded_dirs": "docs/",
            },
            repo_root=repo,
        )
    )

    assert "docs/notes.md" in result["emergent"]
    assert result["excluded_by_prefilter"] == []

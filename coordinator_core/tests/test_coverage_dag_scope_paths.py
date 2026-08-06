"""
coordinator_core.tests.test_coverage_dag_scope_paths — DAG-mode scope_paths
parity with flat mode (coordinator_core/coverage.py's run_coverage_gate).

Prior to this fix, `run_coverage_gate`'s DAG branch (from_handoff provided)
silently ignored `scope_paths` -- it was applied only in flat mode, via
`git rev-list --no-merges <range> -- scope_paths`. `_filter_shas_by_scope_paths`
closes that gap by narrowing the DAG-derived chain_set through git's own
pathspec matcher (`git log --no-walk --format=%H <shas>... -- <scope_paths>`),
applied as a post-filter over the already-derived SHAs rather than at
derivation time.

These tests monkeypatch `cov._derive_dag_chain_set` to return a fixed
`_DagChainResult` over a small real git history, so the scope-filter logic
under test runs against git's real pathspec engine without needing to build
the full handoff-DAG / archival.reverse_membership machinery that
`_derive_dag_chain_set` itself depends on -- that machinery is exercised by
test_coverage_dag_chain_set_cross_branch.py and is not the subject here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _write_and_commit(repo: Path, rel_paths: List[str], message: str) -> str:
    """Write placeholder content to each rel_path (appending if it already
    exists), stage + commit them all in one commit, and return the new SHA.
    """
    for rel in rel_paths:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
        _git(["add", rel], repo)
    _git(["commit", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture()
def scoped_repo(tmp_path: Path):
    """A small real repo with four commits spanning in-scope, out-of-scope,
    directory-prefix, and mixed-path shapes, plus a fake (never-read) handoff
    path -- _derive_dag_chain_set is monkeypatched per-test, so the handoff
    file itself never needs to exist or be well-formed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    c_out = _write_and_commit(repo, ["docs/readme.md"], "out-of-scope commit")
    c_dir = _write_and_commit(repo, ["tests/unit/test_a.py"], "dir-prefix commit")
    c_in = _write_and_commit(repo, ["src/foo.py"], "in-scope commit")
    c_mixed = _write_and_commit(
        repo, ["src/bar.py", "docs/other.md"], "mixed in+out commit"
    )

    fake_handoff = str((repo / "state" / "handoffs" / "closing.md"))
    return repo, fake_handoff, {
        "out": c_out,
        "dir": c_dir,
        "in": c_in,
        "mixed": c_mixed,
    }


def _patch_dag_chain_set(monkeypatch, shas: List[str]) -> None:
    def _fake(from_handoff, repo_root, closing_session_id=""):
        return cov._DagChainResult(shas=list(shas), indeterminate=False, notes=[])

    monkeypatch.setattr(cov, "_derive_dag_chain_set", _fake)


def test_dag_scope_paths_excludes_out_of_scope_commits(scoped_repo, monkeypatch) -> None:
    """(1) A commit touching only an out-of-scope path is excluded from
    chain_set when scope_paths narrows to a different subtree."""
    repo, fake_handoff, shas = scoped_repo
    _patch_dag_chain_set(monkeypatch, [shas["out"], shas["in"]])

    result = cov.run_coverage_gate(
        from_handoff=fake_handoff,
        scope_paths=["src/"],
        repo_root=str(repo),
    )

    # No review-trail records exist in this repo, so every in-chain commit
    # that survives scoping shows up as uncovered -- that's how we observe
    # which SHAs made it through the filter.
    assert set(result.uncovered_shas) == {shas["in"]}, result.notes


def test_dag_scope_paths_empty_is_a_no_op(scoped_repo, monkeypatch) -> None:
    """(2) scope_paths empty/None must be byte-identical to no filtering --
    every DAG-derived SHA stays in chain_set."""
    repo, fake_handoff, shas = scoped_repo
    all_shas = [shas["out"], shas["dir"], shas["in"], shas["mixed"]]

    _patch_dag_chain_set(monkeypatch, all_shas)
    result_none = cov.run_coverage_gate(
        from_handoff=fake_handoff, scope_paths=None, repo_root=str(repo)
    )

    _patch_dag_chain_set(monkeypatch, all_shas)
    result_empty = cov.run_coverage_gate(
        from_handoff=fake_handoff, scope_paths=[], repo_root=str(repo)
    )

    assert set(result_none.uncovered_shas) == set(all_shas)
    assert set(result_empty.uncovered_shas) == set(all_shas)
    assert result_none.verdict_line == result_empty.verdict_line


def test_dag_scope_paths_directory_prefix_matches_nested_file(
    scoped_repo, monkeypatch
) -> None:
    """(3) A directory-prefix pathspec ("tests/") must match a commit that
    touches a file nested beneath it (tests/unit/test_a.py) -- the case a
    naive Python startswith() would get right but a naive glob would not,
    and proof this uses git's real pathspec matcher rather than a hand-rolled
    prefix check."""
    repo, fake_handoff, shas = scoped_repo
    _patch_dag_chain_set(monkeypatch, [shas["out"], shas["dir"]])

    result = cov.run_coverage_gate(
        from_handoff=fake_handoff,
        scope_paths=["tests/"],
        repo_root=str(repo),
    )

    assert set(result.uncovered_shas) == {shas["dir"]}, result.notes


def test_dag_scope_paths_mixed_commit_is_retained(scoped_repo, monkeypatch) -> None:
    """(4) A commit touching BOTH an in-scope and an out-of-scope path is
    retained -- touching any scoped path is sufficient, matching flat mode's
    `git rev-list -- scope_paths` semantics."""
    repo, fake_handoff, shas = scoped_repo
    _patch_dag_chain_set(monkeypatch, [shas["out"], shas["mixed"]])

    result = cov.run_coverage_gate(
        from_handoff=fake_handoff,
        scope_paths=["src/"],
        repo_root=str(repo),
    )

    assert set(result.uncovered_shas) == {shas["mixed"]}, result.notes


def test_filter_shas_by_scope_paths_fails_closed_on_git_error(tmp_path: Path) -> None:
    """A git failure (unresolvable SHA) must return (None, note) -- never an
    unfiltered fallback and never a silently-empty result."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_and_commit(repo, ["src/foo.py"], "seed commit")

    bogus_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    filtered, note = cov._filter_shas_by_scope_paths([bogus_sha], ["src/"], str(repo))

    assert filtered is None
    assert note is not None and "scope_paths filter failed" in note

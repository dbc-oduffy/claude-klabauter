"""Oracles for exemptions whose claim is about GIT'S OWN ARGUMENT SURFACE.

"`git config --unset` accepts exactly one key" is not a fact about this repo, and no AST pass can
decide it. It is a fact about the git binary, and the only honest way to hold it is to ask git.

TIER, stated plainly because it is a real difference in claim strength: these spawn a real binary,
so the spawn ratchet requires `spawns_process`, which puts them on the cadence tier rather than
the fast one. A claim here is therefore re-measured at cadence gates, not on every commit -- the
argparse oracles in `test_sibling_cli_single_record.py` cost 0.14s and run per-commit. Both beat a
comment that is never re-read; they do not beat it equally, and `_ORACLE_CLAIMS` records which
tier each claim carries so nobody has to infer it.

Every fixture builds its own repository under `tmp_path`. Never the shared tree: this box runs
50-70 concurrent sessions, and a probe that mutated the working tree would corrupt a peer's state
to answer a question about git.
"""

from __future__ import annotations

import subprocess

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture()
def repo(tmp_path):
    """A throwaway repository with one commit."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    _git(tmp_path, "config", "user.email", "oracle@example.invalid")
    _git(tmp_path, "config", "user.name", "oracle")
    (tmp_path / "f1").write_text("one\n", encoding="utf-8")
    (tmp_path / "f2").write_text("two\n", encoding="utf-8")
    _git(tmp_path, "add", "f1", "f2")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_git_config_unset_takes_exactly_one_key(repo):
    """`install/uninstall_legs::uninstall_reverse_git_config_group -> config_unset`.

    The exemption says one `--unset` per key is the floor. Note the asymmetry that a wave-4
    reviewer got wrong in the other direction: the READ side batches fine (`--get-regexp`,
    `--list`), and the exemption for `configure_git::main -> _git_config_get` was refuted on
    exactly that ground. Only the WRITE side is single-key."""
    _git(repo, "config", "--local", "a.one", "1")
    _git(repo, "config", "--local", "a.two", "2")

    result = _git(repo, "config", "--local", "--unset", "a.one", "a.two")

    assert result.returncode != 0, (
        "`git config --unset` now accepts two keys in one invocation. The exemption at "
        "`uninstall_legs::uninstall_reverse_git_config_group` is no longer true -- batch the "
        "unsets and delete its `_ORACLE_CLAIMS` entry."
    )
    still_set = _git(repo, "config", "--local", "--get", "a.two")
    assert still_set.stdout.strip() == "2", (
        "the two-key `--unset` had a partial effect, which is a different and worse finding "
        "than it being refused -- re-read this call site before trusting either behaviour."
    )


def test_git_config_set_takes_exactly_one_pair(repo):
    """`ops/configure_git::main -> _git_config_set`."""
    result = _git(repo, "config", "--local", "b.one", "1", "b.two", "2")

    assert result.returncode != 0, (
        "`git config <k> <v> <k> <v>` now sets two pairs in one invocation -- the exemption at "
        "`configure_git::main -> _git_config_set` no longer holds."
    )
    assert _git(repo, "config", "--local", "--get", "b.two").returncode != 0, (
        "the multi-pair form partially applied; re-read the call site rather than assuming "
        "either the old or the new behaviour."
    )


def test_git_rm_is_atomic_across_its_pathspec(repo):
    """`ops/fleet/_common::rm_and_commit -> create_subprocess_exec`.

    This exemption is subtler than "no batch form exists" -- `git rm` takes N pathspecs happily.
    The claim is that batching would DESTROY a property the caller depends on: one dirty path
    aborts the whole invocation, so a sidecar modified out from under a reap would take every
    other path down with it. The function's contract is to retain that file and delete the rest,
    which a batched call cannot express."""
    (repo / "f2").write_text("modified\n", encoding="utf-8")

    result = _git(repo, "rm", "--", "f1", "f2")

    assert result.returncode != 0, (
        "`git rm` no longer refuses a pathspec containing a dirty file. The per-item isolation "
        "exemption at `fleet/_common::rm_and_commit` rests on that refusal."
    )
    tracked = _git(repo, "ls-files").stdout.split()
    assert "f1" in tracked, (
        "`git rm` deleted the clean path while refusing overall -- it is no longer atomic "
        "across its pathspec, which changes what a batched call would cost."
    )


def test_git_rev_list_exclusions_are_global(repo):
    """The `no-primitive-MEASURED-wrong` family: `coverage::_reviewed_via_graph_walk`,
    `review_coverage_core::build_reviewed_set` / `build_segments`,
    `plan_suggest_completion_steps::_plans_with_review_trail_coverage`,
    `review_trail_readjudication_report::compute_readjudication_report`.

    `git rev-list A..B C..D` does NOT mean the union of two ranges -- it means `B D ^A ^C`, so
    for two adjacent ranges the `^C` cancels the `C` that `A..B` contributed and the batched
    form silently NARROWS the result. A batched version of this shipped once and was reverted.

    `TestOwnFrozenDiffShas::test_ranges_resolve_independently` already pins this for one call
    site; this generalises it to the primitive itself, so the whole family rests on a measured
    fact rather than on that one test's reputation."""
    shas = []
    for i in range(4):
        (repo / f"c{i}").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", f"c{i}")
        _git(repo, "commit", "-qm", f"c{i}")
        shas.append(_git(repo, "rev-parse", "HEAD").stdout.strip())

    first = _git(repo, "rev-list", f"{shas[0]}..{shas[1]}").stdout.split()
    second = _git(repo, "rev-list", f"{shas[2]}..{shas[3]}").stdout.split()
    union = set(first) | set(second)

    batched = set(_git(repo, "rev-list", f"{shas[0]}..{shas[1]}", f"{shas[2]}..{shas[3]}").stdout.split())

    assert batched != union, (
        "`git rev-list` now expresses a UNION of ranges in one invocation. The whole "
        "`no-primitive-MEASURED-wrong` family exists because it did not -- re-read every one of "
        "those call sites, because batching them is now correct and they should be fixed."
    )
    assert batched < union, (
        "batched rev-list differs from the union but is not a strict subset -- it returned "
        "commits neither independent range did. That is a different failure from the recorded "
        "narrowing and must be understood before any of these sites are touched."
    )

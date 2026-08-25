"""
coordinator_core.ops.ceremony.tests.test_empty_private_index_refusal

Regression pin for the CEREMONY half of the 2026-08-18 branch-collapse
incident. The first guard landed on the fleet seams
(`ops/fleet/_common.py :: _empty_private_index_breach`) because
`fleet.archive_actioned_memos` produced the observed bad commit
`fbfbd061d`, which committed git's canonical EMPTY TREE
`4b825dc642cb6eb9a060e54bf8d69288fbee4904` and deleted all 26,264 tracked
files on an already-pushed shared branch.

That guard did NOT cover this module. `git_native.py`'s two
`commit-tree` seams -- `_commit_scoped_private_index` and
`commit_authored_content` -- are the path every `pickup-assemble apply` on
the box runs through, and project-makima-7a reported the collapse
reproducing there, not only through fleet. Same shape, same blast radius,
different caller.

Mechanism (verified on git 2.55.0.windows.4, not inferred): `git write-tree`
against a MISSING `GIT_INDEX_FILE` returns the empty tree with rc=0 and
empty stderr, so every `.ok` check upstream is blind to it; a ZERO-BYTE
index instead fails loud (rc=128). An index that vanishes AFTER a successful
`read-tree HEAD` seed is therefore undetectable except by inspecting the
tree object itself.

Both seams are pathspec-less by design -- the private index IS the commit
scope -- so a lost index there commits the empty tree rather than committing
nothing. The refusal is trigger-independent: what removed the index is still
open, and the guard holds regardless.
"""

from coordinator_core.ops.ceremony.git_native import (
    EMPTY_TREE_SHA,
    _empty_private_index_refusal,
)


def test_empty_tree_sha_is_gits_canonical_value():
    assert EMPTY_TREE_SHA == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_empty_tree_is_refused_with_a_failed_result():
    refusal = _empty_private_index_refusal(
        EMPTY_TREE_SHA, root="X:/project-makima", caller="_commit_scoped_private_index"
    )
    assert refusal is not None
    assert not refusal.ok
    assert refusal.returncode != 0
    assert "empty-private-index" in refusal.stderr
    assert EMPTY_TREE_SHA in refusal.stderr
    assert "_commit_scoped_private_index" in refusal.stderr


def test_a_real_tree_sha_passes_through():
    assert (
        _empty_private_index_refusal(
            "6be1de8a7a3b7396b84d857ca61adb932be6264d",
            root="X:/project-makima",
            caller="commit_authored_content",
        )
        is None
    )


def test_both_commit_tree_seams_call_the_refusal():
    """The guard is only worth anything if it is WIRED, not merely defined.

    Pins the call site count rather than the behaviour of each seam: driving
    a real vanished-index commit through `commit_scoped` would require
    racing the `finally` unlink the trigger investigation is still open on.
    """
    import inspect

    from coordinator_core.ops.ceremony import git_native

    src = inspect.getsource(git_native)
    assert src.count("_empty_private_index_refusal(") == 4  # 1 def + 3 call sites
    for caller in ("_commit_scoped_private_index", "commit_authored_content"):
        assert 'caller="%s"' % caller in src

"""`commit_paths(..., detect_rollback=True)` -- AC-P2-3.

Mirrors `test_rollback_check.py`'s in-process history-building pattern:
`commit_paths` itself (zero-spawn) builds fixture history; only repo
`init`/`config` spawn real `git`. `StagedRollbackRefused` is a
`CommitRefused` subclass, so every existing `except (CommitRefused,
FilterUnsupported)` call site needs no edit -- this file also pins that the
two agent routes (`commit_v2`, `safe_commit_offer`) pass
`detect_rollback=True`.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused, StagedRollbackRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _commit(repo, path, content, msg, **kwargs):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content, encoding="utf-8", newline="\n")
    return gcommit.commit_paths(repo, [path], msg, **kwargs)


def _head_tree_paths_unchanged(repo, before_sha):
    # No ref move, no new commit object reachable beyond `before_sha`.
    out = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return out == before_sha


def test_refuses_depth_2_rollback(tmp_path):
    """v0 -> v1 -> v2 -> a commit whose staged bytes restore v0's exact
    blob at depth 2: refused, and nothing landed."""
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")
    _commit(repo, "p.txt", "v2\n", "v2")
    before = gcommit.head_sha(repo)

    with pytest.raises(StagedRollbackRefused) as exc_info:
        _commit(repo, "p.txt", "v0\n", "revert p", detect_rollback=True)

    assert "p.txt" in str(exc_info.value)
    assert "2" in str(exc_info.value)
    assert gcommit.head_sha(repo) == before
    assert _head_tree_paths_unchanged(repo, before)


def test_no_tree_or_commit_object_written_on_refusal(tmp_path):
    """No ref move, and HEAD's tree is unchanged -- the refusal fires
    before `_rewrite_head_spine`/the commit object write."""
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")
    _commit(repo, "p.txt", "v2\n", "v2")
    before_head = gcommit.head_sha(repo)
    before_tree = gcommit.head_tree_sha(repo)

    with pytest.raises(StagedRollbackRefused):
        _commit(repo, "p.txt", "v0\n", "revert p", detect_rollback=True)

    assert gcommit.head_sha(repo) == before_head
    assert gcommit.head_tree_sha(repo) == before_tree


def test_declared_reverts_lands_the_commit(tmp_path):
    """`declared_reverts=[p]` excludes it from the candidate set -- the
    same rollback lands rather than refusing."""
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")

    outcome = _commit(
        repo,
        "p.txt",
        "v0\n",
        "revert p, declared",
        detect_rollback=True,
        declared_reverts=["p.txt"],
    )
    assert outcome.sha
    assert (repo / "p.txt").read_text(encoding="utf-8") == "v0\n"


def test_detect_rollback_false_default_lands_unchanged(tmp_path):
    """`detect_rollback=False` (the default) never raises, even over the
    identical planted rollback."""
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")
    _commit(repo, "p.txt", "v1\n", "v1")

    outcome = _commit(repo, "p.txt", "v0\n", "revert p, default off")
    assert outcome.sha


def test_clean_edit_not_refused(tmp_path):
    """An ordinary edit that never restores an older exact blob is
    unaffected by `detect_rollback=True`."""
    repo = _repo(tmp_path)
    _commit(repo, "p.txt", "v0\n", "v0")

    outcome = _commit(repo, "p.txt", "v1\n", "ordinary edit", detect_rollback=True)
    assert outcome.sha


def test_breadth_3_refused(tmp_path):
    """Three distinct paths each restoring an older version at any depth
    refuses on breadth alone (K-016's rule, via `rollback_check.refusal`)."""
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("a0\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("b0\n", encoding="utf-8", newline="\n")
    (repo / "c.txt").write_text("c0\n", encoding="utf-8", newline="\n")
    gcommit.commit_paths(repo, ["a.txt", "b.txt", "c.txt"], "seed abc")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("b1\n", encoding="utf-8", newline="\n")
    (repo / "c.txt").write_text("c1\n", encoding="utf-8", newline="\n")
    gcommit.commit_paths(repo, ["a.txt", "b.txt", "c.txt"], "abc v1")

    (repo / "a.txt").write_text("a0\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").write_text("b0\n", encoding="utf-8", newline="\n")
    (repo / "c.txt").write_text("c0\n", encoding="utf-8", newline="\n")
    with pytest.raises(StagedRollbackRefused):
        gcommit.commit_paths(
            repo, ["a.txt", "b.txt", "c.txt"], "revert all three", detect_rollback=True
        )


def test_is_a_commit_refused_subclass(tmp_path):
    """`StagedRollbackRefused` is a `CommitRefused`, so every existing
    `except (CommitRefused, FilterUnsupported)` call site catches it
    without an edit."""
    assert issubclass(StagedRollbackRefused, CommitRefused)


def test_reused_content_across_a_burst_is_a_real_rollback_not_a_bug(tmp_path):
    """Regression for the `test_commit_v2_process_time_gate.py` C6
    gate-corruption-guard failure (2026-09-24): a harness that rewrites a
    path through the SAME sequence of exact byte values more than once
    (`"harness rev {i}"` reset to `i=0` at the top of every burst) is
    indistinguishable, blob-for-blob, from operator reverts and correctly
    earns `StagedRollbackRefused` for most of the second burst -- this is
    `rollback_check.find_exact_blob_rollbacks` (K-016) working as designed,
    not a `commit_paths` defect. A monotonic value that never repeats a
    prior blob is what a real caller's successive edits look like, and it
    is never refused."""
    repo = _repo(tmp_path)
    for i in range(10):
        _commit(repo, "p.txt", f"harness rev {i}\n", f"burst1 rev {i}", detect_rollback=True)

    refused = 0
    for i in range(10):
        try:
            _commit(repo, "p.txt", f"harness rev {i}\n", f"burst2 rev {i}", detect_rollback=True)
        except StagedRollbackRefused:
            refused += 1
    assert refused > 0, (
        "reusing burst1's exact byte sequence in burst2 should trip the "
        "anti-rollback guard at least once -- if this assertion fails the "
        "guard regressed, not the harness"
    )

    # The fix: a strictly monotonic value (never repeats a prior blob)
    # never trips the guard, however many bursts run.
    seq = iter(range(10, 10_000))
    for _burst in range(3):
        for _ in range(10):
            n = next(seq)
            _commit(repo, "p.txt", f"harness rev {n}\n", f"unique rev {n}", detect_rollback=True)


def test_move_of_a_path_absent_at_depth_2_is_not_a_rollback(tmp_path):
    """Archiving a file created two commits ago deletes it at the old path,
    which alone reads as restoring depth-2 absence; its blob landing at the
    new path in the same commit makes it a move, never a refusal."""
    repo = _repo(tmp_path)
    _commit(repo, "live/s.yaml", "status: routed\n", "create")
    _commit(repo, "live/s.yaml", "status: shipped\n", "ship")
    (repo / "archive").mkdir()
    (repo / "live/s.yaml").replace(repo / "archive/s.yaml")

    outcome = gcommit.commit_paths(
        repo, ["archive/s.yaml"], "archive s", deleted_paths=["live/s.yaml"],
        detect_rollback=True,
    )
    assert outcome.sha


def test_deletion_without_a_matching_add_still_refuses(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "live/s.yaml", "status: routed\n", "create")
    _commit(repo, "live/s.yaml", "status: shipped\n", "ship")
    (repo / "live/s.yaml").unlink()
    before = gcommit.head_sha(repo)

    with pytest.raises(StagedRollbackRefused):
        gcommit.commit_paths(
            repo, [], "delete live/s.yaml", deleted_paths=["live/s.yaml"],
            detect_rollback=True,
        )
    assert gcommit.head_sha(repo) == before


def test_commit_v2_pin_passes_detect_rollback(tmp_path):
    """Pin: `ceremony.commit_v2`'s `commit_paths` call always passes
    `detect_rollback=True`."""
    import inspect

    from coordinator_core.ops.ceremony import commit_v2

    src = inspect.getsource(commit_v2)
    assert "detect_rollback=True" in src


def test_safe_commit_offer_pin_passes_detect_rollback(tmp_path):
    """Pin: `safe_commit_offer`'s `commit_paths` call always passes
    `detect_rollback=True`."""
    import inspect

    from coordinator_core.ops.session import safe_commit_offer

    src = inspect.getsource(safe_commit_offer)
    assert "detect_rollback=True" in src

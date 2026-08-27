"""
coordinator_core.ops.fleet.tests.test_common_tree_build

C2 (docs/dispatch-briefs/2026-08-26-the-archival-commit-helper-computes-its-
own-tree/C2.md): `archive_and_commit` no longer stages through a private
`GIT_INDEX_FILE` (`git read-tree`/`git add`/`git write-tree`/`git commit-
tree`/`git update-ref`) -- it assembles its `{path: (mode, sha) | _ABSENT}`
tree-delta directly, in process, and lands it via `ops.ceremony.git_native.
_commit_via_head_spine`. Two things that dance used to give away almost for
free are the ones this module pins directly against real git:

1. AC-5 -- repathing a HEAD-tracked blob to its new archive destination
   PRESERVES the mode HEAD recorded for it, including `100755`. A `git add`
   staging pass under `core.filemode=false` would silently normalise that
   bit away; the new build reads it straight from HEAD's own tree spine
   instead (`read_tree_spine`), so it never goes through `git add` at all.
2. AC-7 (re-sited) -- `_empty_private_index_breach` refused a pathspec-less
   commit whose PRIVATE INDEX resolved to git's empty tree. This build has
   no private index left to go missing, so `_assembled_commit_is_noop`
   (`_common.py`) re-sites the same guarantee to this mechanism's own shape:
   a commit whose assembled tree-delta already matches HEAD's tree is
   refused before it is ever handed to `_commit_via_head_spine`.

RE-TARGETED, 2026-08-26 (C1, docs/plans/2026-08-26-the-archival-seam-stops-
asking-git-at-all.md): the disk/HEAD drift gate is retired by PM ruling (see
`_common.py :: archive_and_commit`'s docstring for the ruling text), and
`head_entry[1]` -- `read_tree_spine`'s own blob sha, previously discarded --
now supplies the blob sha for every `restage_src=False` move directly,
spawn-free. `git hash-object -w --stdin-paths` is still spawned, but ONLY
over a `restage_src=True` subset, and only when that subset is non-empty.
This module adds coverage for that split:

3. AC-1 -- an all-`restage_src=False` batch issues ZERO `git hash-object`
   spawns; a batch with a `restage_src=True` move present issues exactly ONE,
   scoped to that subset.
4. AC-4 -- a `restage_src=True` move's committed blob is the FRESH,
   filter-correct hash of dst's on-disk content (a CRLF-bearing file's
   committed blob matches `git hash-object`'s own answer), never
   `head_entry[1]`.
5. The `head_entry is None` refusal (a `restage_src=False` src with no HEAD
   tree entry) lands in `failed[]` as `untracked-at-head`, not defaulted to
   an invented mode/sha and not routed through a spawn to recover.

Real-git spawn is load-bearing for the exec-bit case: a mode assertion
against actual `git ls-tree` output is the only way to prove `git`
core.filemode=false (or a filesystem that carries no executable bit, e.g.
NTFS) cannot silently launder the exec bit away -- a mock could not
reproduce the exact hazard AC-5 exists to close. `popup-intentional-last-
resort` — test-only real-git spawn, mirrors this suite's own governed
`real_git.py`-adjacent pattern (see test_archive_and_commit_envelope_
contract.py's identical marker/rationale).

Negative-spec:
  - Does NOT re-test archive_and_commit's envelope shape (already covered by
    test_archive_and_commit_envelope_contract.py) -- this module is scoped to
    the tree-build mechanism only. The disk/HEAD drift refusal this module's
    predecessor excluded no longer exists anywhere (C1 retired it outright,
    by PM ruling) -- there is no substitute refusal to cover here.
  - Does NOT assert on `_commit_via_head_spine`'s own internals -- those are
    covered by ops/ceremony/tests/test_git_native.py and friends; this
    module only proves _common.py's caller-side assembly is correct.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.ops.ceremony.git_native import _ABSENT
from coordinator_core.ops.fleet import _common as _common_mod
from coordinator_core.ops.fleet._common import (
    Move,
    _assembled_commit_is_noop,
    archive_and_commit,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn; see module
    # docstring for why a mock cannot stand in for this assertion.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def test_archive_and_commit_preserves_exec_bit_from_head(tmp_path: Path) -> None:
    """AC-10: a `100755` HEAD entry lands at dst as `100755`, not `100644`."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-01-exec.md"
    src.write_text("---\nstatus: claimed\n---\n\nBody.\n", encoding="utf-8")

    _git(["add", "-A"], root)
    # Force the exec bit into the index/HEAD regardless of the host
    # filesystem's own permission bits (NTFS carries none) -- this is the
    # authoritative way to make git itself believe this blob is 100755.
    _git(["update-index", "--chmod=+x", "state/handoffs/2026-08-01-exec.md"], root)
    _git(["commit", "-q", "-m", "seed: executable handoff"], root)

    head_mode = _git(
        ["ls-tree", "HEAD", "state/handoffs/2026-08-01-exec.md"], root
    ).stdout.split()[0]
    assert head_mode == "100755", "test setup did not actually record an exec bit"

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(src=src, dst=dst, candidate_id="state/handoffs/2026-08-01-exec.md")

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)")
    )
    assert failed == []
    assert acted == [{"id": move.candidate_id, "archived": True}]

    dst_mode = _git(
        ["ls-tree", "HEAD", "archive/handoffs/2026-08/2026-08-01-exec.md"], root
    ).stdout.split()[0]
    assert dst_mode == "100755", (
        "archive_and_commit's new assembled-dict build lost the exec bit "
        "HEAD recorded for src -- see _common.py archive_and_commit's "
        "AC-10 mode-preservation comment"
    )


def test_archive_and_commit_restage_src_content_is_current_disk_not_head(
    tmp_path: Path,
) -> None:
    """A restage_src=True move commits dst's CURRENT on-disk bytes, never
    HEAD's stale blob for src -- the new build hashes dst fresh via
    `git hash-object`, it does not reuse HEAD's sha the way it reuses HEAD's
    mode."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-01-stamped.md"
    src.write_text("---\nstatus: claimed\n---\n\nBody.\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: handoff"], root)

    # Author fresh content on disk, uncommitted -- exactly what a
    # restage_src=True caller (e.g. a terminality stamp) does immediately
    # before queuing the archival move.
    fresh_content = "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nBody.\n"
    src.write_text(fresh_content, encoding="utf-8")

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(
        src=src, dst=dst,
        candidate_id="state/handoffs/2026-08-01-stamped.md",
        restage_src=True,
    )

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)")
    )
    assert failed == []
    assert acted == [{"id": move.candidate_id, "archived": True}]

    landed = _git(
        ["show", "HEAD:archive/handoffs/2026-08/2026-08-01-stamped.md"], root
    ).stdout
    assert landed == fresh_content, (
        "archive_and_commit committed stale content -- dst's blob must come "
        "from a fresh hash of current on-disk bytes, never HEAD's own blob "
        "for src (see the restage_src docstring note in _common.py)"
    )


def test_assembled_commit_is_noop_true_when_assembled_matches_head(
    tmp_path: Path,
) -> None:
    """AC-7 re-siting: an `assembled` dict whose every entry already matches
    HEAD's tree is detected as a no-op, spawn-free."""
    root = tmp_path / "repo"
    _init_repo(root)

    tracked = root / "tracked.txt"
    tracked.write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)

    head_mode_sha = _git(["ls-tree", "HEAD", "tracked.txt"], root).stdout.split()
    mode = int(head_mode_sha[0], 8)
    sha = head_mode_sha[2]

    # The guard TAKES a spine rather than reading one (2026-08-27) -- its
    # caller walks HEAD once for src union dst and hands the result down, so
    # a single walk here covers every path the four cases below assert on.
    # Built with the real `read_tree_spine` rather than a hand-shaped dict:
    # the guard's contract is "whatever read_tree_spine returns", and a fixture
    # that cannot express the real shape is not coverage of it.
    spine = read_tree_spine(root, ["tracked.txt", "never-tracked.txt"])
    assert spine is not None

    # Byte-identical to what HEAD already records -- no real change.
    noop_assembled = {"tracked.txt": (mode, sha)}
    assert _assembled_commit_is_noop(spine, noop_assembled) is True

    # A genuinely different sha for the same path IS a real change.
    real_change_assembled = {"tracked.txt": (mode, "0" * 40)}
    assert _assembled_commit_is_noop(spine, real_change_assembled) is False

    # A deletion of a path that does not exist in HEAD is also a no-op.
    absent_noop = {"never-tracked.txt": _ABSENT}
    assert _assembled_commit_is_noop(spine, absent_noop) is True

    # A deletion of a path HEAD DOES track is a real change.
    real_deletion = {"tracked.txt": _ABSENT}
    assert _assembled_commit_is_noop(spine, real_deletion) is False


def _patch_counting_hash_object(monkeypatch):
    """Counts calls to `_hash_object_stdin_paths` -- the ONE git spawn left
    anywhere in `archive_and_commit`'s build, and only for a `restage_src=
    True` subset (C1). Mirrors test_archive_and_commit_batched_drift_and_
    restage.py's identical helper."""
    orig = _common_mod._hash_object_stdin_paths
    calls = {"n": 0, "argv_lens": []}

    def _counting(paths, *args, **kwargs):
        calls["n"] += 1
        calls["argv_lens"].append(len(paths))
        return orig(paths, *args, **kwargs)

    monkeypatch.setattr(_common_mod, "_hash_object_stdin_paths", _counting)
    return calls


def test_all_restage_src_false_batch_spawns_zero_hash_object_calls(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-1: a batch made entirely of restage_src=False (the common archival
    shape) issues ZERO `git hash-object` spawns -- head_entry[1] supplies
    every blob sha directly, spawn-free."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    srcs = []
    for i in range(1, 4):
        p = handoffs / f"2026-08-{i:02d}-clean{i}.md"
        p.write_text(f"---\nstatus: claimed\n---\n\n{i}.\n", encoding="utf-8")
        srcs.append(p)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: three handoffs"], root)

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=s, dst=_dst(s), candidate_id=f"state/handoffs/{s.name}")
        for s in srcs
    ]

    calls = _patch_counting_hash_object(monkeypatch)
    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 3 shipped handoff(s)")
    )

    assert failed == []
    assert {a["id"] for a in acted} == {m.candidate_id for m in moves}
    assert calls["n"] == 0, (
        f"an all-restage_src=False batch must spawn zero hash-object calls; got {calls['n']}"
    )
    for s in srcs:
        assert not s.exists()
        assert _dst(s).exists()


def test_mixed_batch_spawns_hash_object_once_scoped_to_restage_subset(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-1: a batch with both restage_src=False and restage_src=True moves
    issues exactly ONE hash-object spawn, covering only the restage_src=True
    dst -- never the restage_src=False ones."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    plain = handoffs / "2026-08-01-plain.md"
    plain.write_text("---\nstatus: claimed\n---\n\nplain.\n", encoding="utf-8")
    stamped = handoffs / "2026-08-02-stamped.md"
    stamped.write_text("---\nstatus: claimed\n---\n\nstamped.\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: two handoffs"], root)

    # Fresh, uncommitted content -- what a restage_src=True caller authors
    # immediately before queuing the move.
    stamped.write_text(
        "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nstamped.\n",
        encoding="utf-8",
    )

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=plain, dst=_dst(plain), candidate_id="state/handoffs/2026-08-01-plain.md"),
        Move(
            src=stamped, dst=_dst(stamped),
            candidate_id="state/handoffs/2026-08-02-stamped.md",
            restage_src=True,
        ),
    ]

    calls = _patch_counting_hash_object(monkeypatch)
    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 2 shipped handoff(s)")
    )

    assert failed == []
    assert {a["id"] for a in acted} == {m.candidate_id for m in moves}
    assert calls["n"] == 1, f"exactly one hash-object spawn expected for the restage_src=True subset; got {calls['n']}"
    assert calls["argv_lens"] == [1], (
        f"hash-object must be scoped to the restage_src=True subset only (1 path); got {calls['argv_lens']}"
    )


def test_restage_src_true_committed_blob_matches_hash_object_on_crlf_content(
    tmp_path: Path,
) -> None:
    """AC-4: a restage_src=True move's committed blob is the FRESH,
    filter-correct hash of dst's on-disk content -- a CRLF-bearing file's
    committed blob equals `git hash-object`'s own answer for that content,
    with no in-process hashing anywhere in the diff."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-01-crlf.md"
    src.write_bytes(b"---\r\nstatus: claimed\r\n---\r\n\r\nBody.\r\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: crlf handoff"], root)

    fresh_content = b"---\r\nstatus: claimed\r\ndeployment_state: shipped\r\n---\r\n\r\nBody.\r\n"
    src.write_bytes(fresh_content)

    # git's own answer for this exact content, filter-correct (respects
    # core.autocrlf/.gitattributes the same way `git add`/`git commit` would).
    expected_sha = _git(["hash-object", "--path", str(src.relative_to(root)), str(src)], root).stdout.strip()

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(
        src=src, dst=dst,
        candidate_id="state/handoffs/2026-08-01-crlf.md",
        restage_src=True,
    )

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)")
    )
    assert failed == []
    assert acted == [{"id": move.candidate_id, "archived": True}]

    landed_sha = _git(
        ["rev-parse", "HEAD:archive/handoffs/2026-08/2026-08-01-crlf.md"], root
    ).stdout.strip()
    assert landed_sha == expected_sha, (
        "restage_src=True's committed blob must match git hash-object's own "
        "answer for the fresh content -- see AC-4"
    )


def test_restage_src_false_untracked_at_head_src_lands_in_failed(
    tmp_path: Path,
) -> None:
    """`head_entry is None` for a restage_src=False move is a refusal, not a
    default: an untracked-at-HEAD src fails with a named reason, its
    os.replace is reversed, and a sibling clean move is unaffected."""
    root = tmp_path / "repo"
    _init_repo(root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    clean = handoffs / "2026-08-01-clean.md"
    clean.write_text("---\nstatus: claimed\n---\n\nclean.\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: clean handoff"], root)

    # Untracked at HEAD -- written to disk but never staged or committed.
    untracked = handoffs / "2026-08-02-untracked.md"
    untracked.write_text("---\nstatus: claimed\n---\n\nuntracked.\n", encoding="utf-8")

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=clean, dst=_dst(clean), candidate_id="state/handoffs/2026-08-01-clean.md"),
        Move(src=untracked, dst=_dst(untracked), candidate_id="state/handoffs/2026-08-02-untracked.md"),
    ]

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive mixed batch")
    )

    assert {a["id"] for a in acted} == {moves[0].candidate_id}
    assert len(failed) == 1
    assert failed[0]["id"] == moves[1].candidate_id
    assert "untracked-at-head" in failed[0]["reason"]

    # The untracked move's os.replace was reversed -- src restored, dst gone.
    assert untracked.exists()
    assert not _dst(untracked).exists()
    # The clean move landed normally.
    assert not clean.exists()
    assert _dst(clean).exists()

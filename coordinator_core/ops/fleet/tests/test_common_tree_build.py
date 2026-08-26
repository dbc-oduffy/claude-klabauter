"""
coordinator_core.ops.fleet.tests.test_common_tree_build

C2 (docs/dispatch-briefs/2026-08-26-the-archival-commit-helper-computes-its-
own-tree/C2.md): `archive_and_commit` no longer stages through a private
`GIT_INDEX_FILE` (`git read-tree`/`git add`/`git write-tree`/`git commit-
tree`/`git update-ref`) -- it assembles its `{path: (mode, sha) | _ABSENT}`
tree-delta directly, in process, and lands it via `ops.ceremony.git_native.
_commit_via_head_spine`. Two things that dance used to give away almost for
free are the ones this module pins directly against real git:

1. AC-10 -- repathing a HEAD-tracked blob to its new archive destination
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

Real-git spawn is load-bearing for the exec-bit case: a mode assertion
against actual `git ls-tree` output is the only way to prove `git`
core.filemode=false (or a filesystem that carries no executable bit, e.g.
NTFS) cannot silently launder the exec bit away -- a mock could not
reproduce the exact hazard AC-10 exists to close. `popup-intentional-last-
resort` — test-only real-git spawn, mirrors this suite's own governed
`real_git.py`-adjacent pattern (see test_archive_and_commit_envelope_
contract.py's identical marker/rationale).

Negative-spec:
  - Does NOT re-test archive_and_commit's envelope shape (already covered by
    test_archive_and_commit_envelope_contract.py) or its disk/HEAD drift
    refusal (test_archive_and_commit_disk_head_drift.py) -- this module is
    scoped to the NEW tree-build mechanism only.
  - Does NOT assert on `_commit_via_head_spine`'s own internals -- those are
    covered by ops/ceremony/tests/test_git_native.py and friends; this
    module only proves _common.py's caller-side assembly is correct.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.git_native import _ABSENT
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

    # Byte-identical to what HEAD already records -- no real change.
    noop_assembled = {"tracked.txt": (mode, sha)}
    assert _assembled_commit_is_noop(root, noop_assembled) is True

    # A genuinely different sha for the same path IS a real change.
    real_change_assembled = {"tracked.txt": (mode, "0" * 40)}
    assert _assembled_commit_is_noop(root, real_change_assembled) is False

    # A deletion of a path that does not exist in HEAD is also a no-op.
    absent_noop = {"never-tracked.txt": _ABSENT}
    assert _assembled_commit_is_noop(root, absent_noop) is True

    # A deletion of a path HEAD DOES track is a real change.
    real_deletion = {"tracked.txt": _ABSENT}
    assert _assembled_commit_is_noop(root, real_deletion) is False

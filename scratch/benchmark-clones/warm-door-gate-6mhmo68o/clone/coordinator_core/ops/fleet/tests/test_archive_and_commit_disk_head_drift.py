"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_disk_head_drift

Was: reproduces the b3e61bd00 archive-gate bypass, where `git mv` against a
`git read-tree HEAD`-seeded PRIVATE INDEX re-keyed whichever blob the index
held for src (src's LAST-COMMITTED content) rather than src's current
on-disk content -- so a candidate whose on-disk frontmatter had drifted
from HEAD (an uncommitted stamp write) could pass a caller's disk-based
terminality re-verify while `git mv` silently committed the STALE HEAD
content to the archive destination. The old fix was a dedicated
`git diff --name-only` guard that refused any such move outright.

Then (F-5 swap, 2026-08-21, docs/plans/2026-08-20-the-close-ceremony-stops-
paying-for-the-join.md C5): `git mv` was replaced by `os.replace` -- an
in-process rename that always moves whatever bytes are CURRENTLY on disk at
src, never a stale index-held blob -- and the drift guard was dropped
entirely on the reasoning that os.replace closed b3e61bd00 structurally.
That reasoning was half right: os.replace genuinely retires the STALE-BLOB
hazard (there is no private-index blob left to re-key stale content from),
but the guard was doing a SECOND, undocumented job -- refusing to archive a
candidate whose disk content diverges from HEAD but was never staged or
committed. Dropping the guard reopened THAT hole: os.replace has no notion
of "is this committed", so an uncommitted `deployment_state: shipped` write
from a buggy op or a concurrent peer session relocates into the archive the
instant the (disk-reading) terminality predicate passes -- an
archive-gate-bypass reached through a different mechanism than b3e61bd00's,
but the same class.

Then (C5-REPAIR, 2026-08-21): the batched disk/HEAD drift check was
RESTORED as a refusal gate for restage_src=False moves (the default).

Now (PM ruling, 2026-08-26, cffa6e99f): that refusal gate is RETIRED
outright, not merely re-sited. archival moves may assume a terminal
handoff's disk content matches HEAD, because nothing in this fleet stamps
a record complete and leaves the stamp uncommitted -- the drift the gate
was refusing does not arise in practice, so the refusal was pure cost with
no corresponding incident of its own. The one real incident this module's
own history traces to, b3e61bd00, was the STALE-BLOB mechanism -- `git
mv` re-keying a private index's stale HEAD-seeded blob for src -- which
`os.replace` retired structurally (F-5, 2026-08-21), before the drift gate
was ever restored. The archive-gate-bypass job the drift gate did after
that (refusing an uncommitted-but-drifted disk write) never itself had an
incident behind it; the PM ruling above retires it on that basis.
`test_drifted_src_is_refused_by_default` below is retired accordingly,
not deleted -- see its own docstring. The `restage_src=True` sibling is
unaffected either way: it was always exempt from the drift gate and
remains a plain "fresh disk content survives os.replace" positive case.

This module spawns real git (mirrors the governed model in
coordinator_core/ops/ceremony/tests/fixtures/real_git.py) but is
deliberately its OWN small, scoped module (not an import of that fixture,
which is reserved for the commit-mechanism selector's own tests) so this
real-git usage stays visible in the diff, per the standing test-cull
ruling (state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md) that
real-git fixtures must not go ambient again. Two throwaway repos, two
tests -- one per Move.restage_src value.

Spec backlink: archive-gate bypass investigation, commit b3e61bd00
("fleet: archive 7 shipped handoff(s)", Session-Id
1e7c7cb6-206d-4e07-901f-d9160018b233).

Negative-spec: do NOT restore the drift refusal on the strength of this
module's own history -- the PM ruling above (2026-08-26) is doctrine, not
a suggestion this module happens to embody; the gate is retired, not
merely quiet. `test_drifted_src_is_refused_by_default` below now records
that ruling rather than pinning the old refusal.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern; no console window
    # risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def _seed_drifted_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    """Init a throwaway repo with a committed handoff, then drift its disk
    content (uncommitted deployment_state:shipped stamp) without staging or
    committing it. Returns (root, src, drifted_content)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-06-roadmap-sedge-03.md"

    # Committed (HEAD) content: in_flight, no shipped_in.
    head_content = (
        "---\n"
        "status: claimed\n"
        "deployment_state: in_flight\n"
        "---\n\nBody.\n"
    )
    src.write_text(head_content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: in_flight handoff"], root)
    head_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    # Uncommitted disk-only stamp: deployment_state:shipped + a resolvable
    # shipped_in -- never staged, never committed. This is the drift the
    # restored guard refuses for restage_src=False, and the drift
    # restage_src=True asserts it authored on purpose.
    drifted_content = (
        "---\n"
        "status: claimed\n"
        f"deployment_state: shipped\nshipped_in: {head_sha}\n"
        "---\n\nBody.\n"
    )
    src.write_text(drifted_content, encoding="utf-8")

    assert src.read_text(encoding="utf-8") != head_content, "fixture sanity: disk must differ from HEAD"
    return root, src, drifted_content


def test_drifted_src_is_refused_by_default(tmp_path: Path):
    """RETIRED refusal, recorded as retired (PM ruling, 2026-08-26, see
    module docstring) -- this test used to assert a drifted restage_src=False
    candidate is REFUSED. That refusal gate was deleted outright in
    cffa6e99f: archival moves may assume a terminal handoff's disk content
    matches HEAD, because nothing in this fleet stamps a record complete and
    leaves the stamp uncommitted. The one real incident behind this module
    (b3e61bd00) was the stale-blob mechanism, which `os.replace` retired
    structurally (F-5, 2026-08-21) -- the archive-gate-bypass refusal this
    test pinned never had an incident of its own behind it.

    Recorded here as an explicit assertion of the NEW behaviour (drift no
    longer refuses a restage_src=False move) rather than a silent deletion,
    so a future reader sees the ruling was applied on purpose."""
    root, src, drifted_content = _seed_drifted_repo(tmp_path)

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(src=src, dst=dst, candidate_id="state/handoffs/2026-08-06-roadmap-sedge-03.md")

    acted, failed = _run(archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)"))

    assert failed == [], f"drift no longer refuses a restage_src=False move: {failed}"
    assert acted == [{"id": move.candidate_id, "archived": True}]

    # The archived file carries the drifted disk content — os.replace moved
    # whatever was on disk at src, and no gate intervened.
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == drifted_content


def test_drifted_src_with_restage_src_is_archived_with_its_fresh_on_disk_content(tmp_path: Path):
    """restage_src=True: the caller asserts it authored src's current
    on-disk content on purpose (e.g. a terminality stamp just written), so
    the drift refusal is exempt and the move proceeds via os.replace. The
    archived content at dst is the FRESH (drifted) disk content, not stale
    HEAD content -- there is no private-index blob left to re-key stale
    content from under os.replace."""
    root, src, drifted_content = _seed_drifted_repo(tmp_path)

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    move = Move(
        src=src, dst=dst,
        candidate_id="state/handoffs/2026-08-06-roadmap-sedge-03.md",
        restage_src=True,
    )

    acted, failed = _run(archive_and_commit(worktree_root=root, moves=[move], subject="fleet: archive 1 shipped handoff(s)"))

    assert failed == [], f"restage_src=True drifted move must succeed under os.replace: {failed}"
    assert acted == [{"id": move.candidate_id, "archived": True}]

    # The archived file carries the FRESH (drifted) content, not stale HEAD.
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == drifted_content

    status = _git(["status", "--porcelain"], root).stdout
    assert status.strip() == "", f"archival commit must leave a clean tree: {status!r}"

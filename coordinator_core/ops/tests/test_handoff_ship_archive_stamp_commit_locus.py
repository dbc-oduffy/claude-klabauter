"""
coordinator_core.ops.tests.test_handoff_ship_archive_stamp_commit_locus

ORIGIN (kept as paper trail — do not delete): this file began life as a SPIKE
that executed, rather than read, `handoff_ship_archive.py`'s module docstring
claim (lines ~22-25 at the time):

    "the ship stamp is NOT committed separately: archive_and_commit's
    main-index resync stages the current on-disk content of the archive
    destination (which carries the shipped stamp), so the stamp lands in
    the archival commit"

That claim was SELF-CONTRADICTORY on its face: `archive_and_commit`'s
main-index resync (`_common.py`'s post-commit `git update-index --add --
dst` block) runs AFTER the private-index archival commit, so it cannot put
anything INTO that already-made commit — it can only stage content into the
SEPARATE, SHARED main index, where it sits as residue until some later,
unrelated commit absorbs it.

SPIKE FINDING (at that time, driving the REAL op against a scratch git
repo, HEAD before the C3 fix): the module docstring's claim was WRONG. The
deployment_state:shipped stamp did NOT land in the archival commit — it was
written to disk (uncommitted) by handoff_transition._ship's locked_rmw
call, strictly BEFORE archive_and_commit ran, and archive_and_commit's
private index (seeded via `git read-tree HEAD`) held src's stale,
LAST-COMMITTED blob because `archive_shipped_handoffs.py`'s Move() call
site did not pass `restage_src=True`. This was the EXACT residue shape Arm
B of test_index_residue_reproduction.py demonstrates for a generic
dirty-src Move — `handoff_ship_archive`'s ship-then-archive composite was
an unlabelled instance of that same dirty-src gap.

FIX LANDED (C3, this working tree): `handoff_ship_archive.py`'s call into
`_handle_act` now passes `restage_src=True` at its one call site. That
targeted `git add -- <src>` (private index only), run immediately before
`git mv` for this handoff, picks up the ship stamp from src's current
on-disk content, so the stamp now lands IN the archival commit itself —
closing the residue gap this file used to pin. The tests below now assert
the FIXED behaviour (AC9): no more residue, no more absorption-by-bystander
misattribution. Kept in the same file (flipped, not deleted) because it is
the evidence trail for a module docstring that once documented a leak as a
feature — see `handoff_ship_archive.py`'s own updated docstring for the
current, correct claim.

Spec backlinks:
  - Docstring under test: coordinator_core/ops/handoff_ship_archive.py:22-35
  - Mechanism: coordinator_core/ops/fleet/_common.py archive_and_commit
    (private HEAD-seeded index commit, then separate main-index resync)
  - Dirty-src residue shape (generic, non-handoff-specific reproduction,
    pre-restage_src baseline): coordinator_core/ops/fleet/tests/
    test_index_residue_reproduction.py::test_arm_b_engine_dirty_src_stays_unstaged
    (renamed from test_arm_b_engine_dirty_src_reproduces when C1 inverted it)
  - restage_src opt-in this composite now uses:
    coordinator_core/ops/fleet/_common.py Move.restage_src /
    coordinator_core/ops/handoff_ship_archive.py (_archive_shipped_act call,
    restage_src=True)
  - Graceful-partial branch (AC4): coordinator_core/ops/handoff_ship_archive.py
    "Graceful partial outcome" docstring section (shipped_in neither supplied
    nor present -> stamp written, archival skipped, mutation left uncommitted)

Negative-spec:
  - Does NOT edit any production file — observation only, current HEAD.
  - Does NOT monkeypatch or mock any git call, retry constant, or lock —
    every assertion below is against a REAL scratch git repo driven
    through the real `handoff.ship_and_archive` op end-to-end.
  - Does NOT re-test the 2026-08-01/02 retry-exhaustion residue mechanism
    (a different, already-fixed bug with a different residue shape) —
    this file's coverage is a FULLY successful, first-attempt resync/archive
    path, same as Arm B, plus the graceful-partial (no shipped_in) branch.
"""

from __future__ import annotations

import asyncio
import subprocess

import coordinator_core.ops.handoff_ship_archive  # noqa: F401 — fires @register_op

from coordinator_core.ops.handoff_ship_archive import _handler
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter


def _run(coro):
    """Run an async coroutine synchronously — mirrors sibling test files' helper."""
    return asyncio.run(coro)


def _head_sha(repo) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo.root),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo.root),
        capture_output=True,
        text=True,
        check=True,
    )


def _archived_dst_rel(repo, name: str) -> str:
    """Return the single archive/handoffs/YYYY-MM/<name> repo-relative path.

    Fails loudly (not a soft skip) if the archival did not produce exactly
    one candidate — a locus test that silently no-ops on the wrong count
    would mask its own precondition failure as a false negative.
    """
    matches = [
        p for p in (repo.root / "archive" / "handoffs").rglob("*.md") if p.name == name
    ]
    assert len(matches) == 1, f"expected exactly one archived {name}, got {matches!r}"
    return str(matches[0].relative_to(repo.root))


# ---------------------------------------------------------------------------
# (1) + (2) — where the stamp lands, and index/worktree cleanliness after
# ---------------------------------------------------------------------------


def test_ship_stamp_is_staged_main_index_residue_not_in_archival_commit(handoff_repo):
    """Drives the REAL `handoff.ship_and_archive` op end-to-end (shipped_in
    already present, mirroring test_handoff_ship_archive.py's happy path).

    PRE-C3 this test pinned the REFUTED reading of the module docstring's
    claim: the archival commit's own content at dst carried the STALE,
    pre-ship blob, and the shipped stamp sat as an uncommitted, staged
    residue in the shared main index (`M  <dst>` in `git status
    --porcelain`) rather than landing in the archival commit.

    POST-C3 (`_handle_act` called with `restage_src=True` at this module's
    one call site — see handoff_ship_archive.py's updated docstring): the
    targeted `git add -- <src>` immediately before `git mv` picks up the
    ship stamp from src's current on-disk content, so the archival commit's
    OWN blob at dst now carries `deployment_state: shipped`, and the
    working tree/index is left CLEAN — no more residue, AC9 satisfied.
    """
    name = "2026-07-12-stamp-locus.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight", shipped_in=sha,
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))
    assert result["exit_code"] == 0, result
    assert result["archived"] is True

    dst_rel = _archived_dst_rel(handoff_repo, name)

    # (1) The archival commit's OWN content at dst now carries the SHIPPED
    # stamp — restage_src=True's targeted `git add -- src` (immediately
    # before `git mv`) rehashed src's current on-disk content, which
    # already carried Step 2's ship stamp, into the private index BEFORE
    # the archival commit was made. AC9: the stamp is IN the archival
    # commit, not staged residue against it.
    head_dst_content = _git(handoff_repo, "show", f"HEAD:{dst_rel}").stdout
    head_split = split_frontmatter(head_dst_content)
    assert head_split is not None
    assert read_fm_field(head_split.fm_text, "deployment_state") == "shipped", (
        f"archival commit's own content at {dst_rel} was expected to carry the "
        f"shipped deployment_state (C3's restage_src=True fix); got: "
        f"{head_dst_content!r}"
    )

    # (2) Working tree/index is CLEAN after a successful ship-and-archive
    # call — no leftover staged modification at dst, no residue.
    porcelain = _git(handoff_repo, "status", "--porcelain").stdout.strip()
    assert porcelain == "", (
        f"expected a clean working tree/index after ship-and-archive; "
        f"got {porcelain!r}"
    )


# ---------------------------------------------------------------------------
# (3) — no residue survives to be absorbed by an unrelated subsequent commit
# ---------------------------------------------------------------------------


def test_staged_stamp_residue_absorbed_by_unrelated_bare_commit(handoff_repo):
    """PRE-C3 this test pinned the same shape as
    test_index_residue_reproduction.py's test_arm_b_engine_dirty_src_stays_unstaged
    (then named test_arm_b_engine_dirty_src_reproduces):
    an UNRELATED bare `git commit` (no pathspec) absorbed the ship stamp
    residue left behind by ship-and-archive, misattributing it under its own
    subject line.

    POST-C3, restage_src=True closes the residue gap entirely (see sibling
    test above), so there is nothing left for a bystander commit to absorb.
    This test now asserts NON-absorption: an unrelated bare commit made
    after a successful ship-and-archive call touches ONLY its own file —
    HEAD movement / `--name-status` semantics are asserted directly (never
    string membership on a `--name-status` line, which can render as
    `R100<TAB>old<TAB>new` for a rename and defeat a naive substring check —
    same false-green hazard flagged in the sibling C2 chunk).
    """
    name = "2026-07-12-stamp-locus-absorbed.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    sha = _head_sha(handoff_repo)
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight", shipped_in=sha,
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))
    assert result["exit_code"] == 0, result
    assert result["archived"] is True

    dst_rel = _archived_dst_rel(handoff_repo, name)
    ship_and_archive_head = _head_sha(handoff_repo)

    # No residue at dst to begin with — the archival commit itself already
    # carries the shipped stamp (sibling test above).
    porcelain_before = _git(handoff_repo, "status", "--porcelain").stdout.strip()
    assert porcelain_before == "", (
        f"expected a clean tree before the bystander commit; got {porcelain_before!r}"
    )

    # An unrelated file, staged AFTER the ship-and-archive call, standing in
    # for a bystander session's/janitor's own in-flight work.
    unrelated = handoff_repo.root / "unrelated.txt"
    unrelated.write_text("unrelated bystander content\n", encoding="utf-8")
    _git(handoff_repo, "add", "unrelated.txt")

    porcelain_staged = _git(handoff_repo, "status", "--porcelain").stdout.strip().splitlines()
    assert porcelain_staged == ["A  unrelated.txt"], (
        f"expected only the bystander file staged; got {porcelain_staged!r}"
    )

    # Bare commit — no pathspec.
    _git(handoff_repo, "-c", "commit.gpgsign=false", "commit", "-m", "unrelated bystander commit")

    name_status = _git(
        handoff_repo, "show", "--name-status", "--format=", "HEAD",
    ).stdout.strip().splitlines()
    touched = {line.split("\t", 1)[1] for line in name_status if line.strip()}
    assert touched == {"unrelated.txt"}, (
        f"expected the bystander commit to touch ONLY its own file, not "
        f"{dst_rel!r} (no residue left to absorb post-C3); got {name_status!r}"
    )

    # HEAD's parent is still the ship-and-archive op's own archival commit —
    # the bystander commit is a distinct, single-parent step forward, not a
    # merge/amend that folded ship-and-archive's own commit into itself.
    parent_sha = _git(handoff_repo, "rev-parse", "HEAD^").stdout.strip()
    assert parent_sha == ship_and_archive_head

    # dst's content is unchanged by the bystander commit — still the
    # archival commit's own shipped stamp.
    dst_content = _git(handoff_repo, "show", f"HEAD:{dst_rel}").stdout
    dst_split = split_frontmatter(dst_content)
    assert read_fm_field(dst_split.fm_text, "deployment_state") == "shipped"

    assert _git(handoff_repo, "status", "--porcelain").stdout.strip() == ""


# ---------------------------------------------------------------------------
# (4) — graceful-partial branch: stamp written, archival skipped, uncommitted
# ---------------------------------------------------------------------------


def test_graceful_partial_stamp_uncommitted_no_archival_commit(handoff_repo):
    """AC4's only discharge: shipped_in neither supplied (no `sha` param)
    nor already present on the handoff -> `handoff.ship_and_archive` still
    stamps `deployment_state: shipped` (Bug-2 closed) while archival is
    SKIPPED with reason `terminality-drift: ...`, and — unlike the archived
    branch covered by the sibling tests above, where restage_src=True folds
    the stamp into the archival commit — this branch makes NO archival
    commit at all, so the stamp mutation is left UNCOMMITTED in the working
    tree, pending a later archival pass once a shipped_in lands. See
    handoff_ship_archive.py's "Graceful partial outcome" docstring section.
    """
    name = "2026-07-12-stamp-locus-graceful.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    pre_call_head = _head_sha(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}"},
        handoff_repo.common_dir,
    ))
    assert result["exit_code"] == 0, result
    assert result["shipped"] is True
    assert result["archived"] is False
    assert result["archive_skip_reason"] is not None
    assert "terminality-drift" in result["archive_skip_reason"]

    # No archival commit was made: HEAD is unchanged from before the call.
    assert _head_sha(handoff_repo) == pre_call_head

    # The frontmatter mutation is unstaged/uncommitted in the working tree
    # (locked_rmw is a pure filesystem write, no git add/commit of its own).
    #
    # NOTE: porcelain's leading status column can itself be a literal space
    # (" M" = worktree-modified, not staged) — `.strip()` on the whole
    # stdout blob would eat that leading space and corrupt the single-line
    # case, so this reads raw lines via `.splitlines()` without a
    # whole-string `.strip()` first.
    src_rel = f"state/handoffs/{name}"
    porcelain = _git(handoff_repo, "status", "--porcelain").stdout.rstrip("\n").splitlines()
    assert porcelain == [f" M {src_rel}"], (
        f"expected exactly one unstaged modification at {src_rel}; got {porcelain!r}"
    )

    # HEAD's own content at the handoff path is still the pre-ship blob —
    # the ship stamp never landed in a commit this call.
    head_content = _git(handoff_repo, "show", f"HEAD:{src_rel}").stdout
    head_split = split_frontmatter(head_content)
    assert read_fm_field(head_split.fm_text, "deployment_state") == "in_flight"

    # The on-disk (working tree) content DOES carry the shipped stamp.
    disk_content = (handoff_repo.root / "state" / "handoffs" / name).read_text(
        encoding="utf-8"
    )
    disk_split = split_frontmatter(disk_content)
    assert read_fm_field(disk_split.fm_text, "deployment_state") == "shipped"

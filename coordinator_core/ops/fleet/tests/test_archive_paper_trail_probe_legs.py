"""
Tests for coordinator_core.ops.fleet.archive_paper_trail — the six D2/staging probe legs,
promoted to regression tests.

Purpose: state/audits/2026-08-05-d2-paper-trail-untracked-workdir-probe.md and
state/audits/2026-08-05-staging-is-insufficient-and-the-staleness-hazard-is-real.md ran
six legs against throwaway repos to establish the untracked-src defect, C8's degraded-exit
signal, C9's no-residue postcondition, and the C1 commit-timing/index-resync fix. Those
audits are a RECORD of what was executed once, not a gate that runs again — this module is
the gate. Each leg below reproduces its audit counterpart against the shared `fleet_repo`
fixture and asserts the same postconditions the audit observed by hand.

C1 (`--cacheinfo` main-index resync), C8 (`_ARCHIVE_DEGRADED = 4` CLI trampoline exit +
additive `failed` key), and C9 (no-destination-residue on total failure) are read-only
dependencies here — this module asserts against their landed behaviour, it does not
re-implement or re-verify their internals directly.

Coverage:
  leg 1 — untracked src (the live shape), dry_run:false, run through the ACTUAL
          `coordinator/bin/archive-paper-trail.py` CLI trampoline (not just the handler)
          so AC11 (rc == _ARCHIVE_DEGRADED, reason on stderr) is genuinely exercised,
          not merely assumed from the handler's `failed` key. AC12: destination directory
          must not exist afterward (residue is an empty dir, not merely "non-empty" — the
          assertion here is absence, not emptiness).
  leg 2 — tracked src control: must archive cleanly, HEAD advances, tree clean. Isolates
          trackedness as the one variable the other legs differ on.
  leg 3 — fail (untracked) -> belatedly track+commit -> retry: the retry must succeed
          (AC14), not wedge behind C9's now-absent residue.
  leg 4 — fail (untracked) -> operator hand-deletes the workdir -> re-query: must not
          report a phantom archive (AC13) — no directory matching the topic_slug glob may
          exist, and the reported `dest` must not exist on disk.
  leg 5 — dry_run:true preview over an untracked src. Recorded, not fixed: the preview path
          never inspects git-tracked state (its `if not src.exists()` / `if dest.exists()`
          gates run entirely off disk presence), so it cannot discriminate the tracked vs.
          untracked shapes this module's other legs distinguish. See the leg's own docstring
          for why this is accepted rather than asserted-away.
  leg D — tracked src, edited on disk after commit but before archival (the scenario the
          wave-1 commit-before-archive ordering constraint protects against): the archived
          blob at HEAD must be the COMMITTED content, the later on-disk edit must survive
          at dst as an UNSTAGED worktree modification (porcelain ` M dst`, distinct from
          leg 2's clean tree), and exactly one new commit (the archival commit) must land —
          no bare commit absorbing the edit.

Harness: asyncio.run() in sync test fns (mirrors test_archive_paper_trail.py — no
pytest-asyncio dependency). Handler called directly with repo_root=fleet_repo.common_dir
for legs 2-5/D. Leg 1 additionally drives the real CLI module
(coordinator/bin/archive-paper-trail.py), loaded by path via importlib (mirrors
coordinator/bin/test_sweep_actioned_memos.py's `_load_module` pattern), with
`cc_invoke.route` monkeypatched to call the real `_handler` in-process rather than
spawning `coordinator_core.invoke` as a subprocess — this keeps the CLI's own argv
parsing, JSON stdout contract, and exit-code branch genuinely exercised without a daemon.

Spec backlinks:
  - state/audits/2026-08-05-d2-paper-trail-untracked-workdir-probe.md (legs 1, 2, 3, 4, 5)
  - state/audits/2026-08-05-staging-is-insufficient-and-the-staleness-hazard-is-real.md
    (leg D)
  - coordinator/bin/archive-paper-trail.py (_ARCHIVE_DEGRADED, exit-code contract)
  - coordinator_core/ops/fleet/archive_paper_trail.py (_handler)
  - coordinator_core/ops/fleet/_common.py (archive_and_commit — C1 cacheinfo resync, C9
    created-dir cleanup)

Negative-spec:
  - Does NOT restructure or duplicate coverage from test_archive_paper_trail.py (C8's own
    verification file) — that file's dry_run/idempotency/collision/param-validation
    coverage is untouched here.
  - Does NOT assert on message-string formatting for branch discrimination (the "false
    green" warning from the dispatch brief: leg 3's second call returns the identical
    triple to an mv failure) — every leg asserts on semantics: HEAD movement, `git
    ls-files`/`git show` content, file-inventory presence/absence, and porcelain state.
  - Leg 5 does NOT assert the preview now discriminates tracked state — it does not, and
    this module records that rather than silently accepting an unchanged assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

# ---- Import guard: fires @register_op side-effect for fleet.archive_paper_trail. ----
import coordinator_core.ops.fleet.archive_paper_trail  # noqa: F401

from coordinator_core.ops.fleet.archive_paper_trail import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _today() -> str:
    return date.today().isoformat()


def _seed_workdir(fleet_repo, run_id: str, filename: str = "notes.md", content: str = "paper trail content\n") -> Path:
    """Create + commit docs/research/{run_id}-workdir/{filename} in fleet_repo (tracked)."""
    workdir = fleet_repo.root / "docs" / "research" / f"{run_id}-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / filename).write_text(content, encoding="utf-8")
    fleet_repo._git("add", str(workdir))
    fleet_repo._git("commit", "-m", f"seed workdir {run_id}")
    return workdir


def _seed_untracked_workdir(fleet_repo, run_id: str, filename: str = "notes.md", content: str = "untracked content\n") -> Path:
    """Create docs/research/{run_id}-workdir/{filename} WITHOUT staging or committing —
    the live staff-session shape (D2 audit § 1)."""
    workdir = fleet_repo.root / "docs" / "research" / f"{run_id}-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / filename).write_text(content, encoding="utf-8")
    return workdir


def _dest_dir(fleet_repo, topic_slug: str) -> Path:
    return fleet_repo.root / "docs" / "research" / "archive" / f"{_today()}-{topic_slug}"


# ---------------------------------------------------------------------------
# Leg 1 — untracked src, dry_run:false, driven through the CLI trampoline.
# ---------------------------------------------------------------------------

def _load_cli_module():
    """Import coordinator/bin/archive-paper-trail.py as a fresh module object.

    Mirrors coordinator/bin/test_sweep_actioned_memos.py's `_load_module` pattern —
    hyphenated filename, loaded by path via importlib.util rather than a normal import
    statement.

    # Review: coordinator:code-reviewer — deliberately omits sys.modules[spec.name]
    # registration, matching coordinator/bin/test_sweep_actioned_memos.py's own
    # `_load_module` (confirmed by reading it: it does not register into
    # sys.modules either). Safe because the CLI script under test has no relative
    # imports; revisit if that ever changes or a second concurrent load is added.
    """
    repo_root = Path(__file__).resolve().parents[4]
    cli_path = repo_root / "coordinator" / "bin" / "archive-paper-trail.py"
    spec = importlib.util.spec_from_file_location("archive_paper_trail_cli_under_test", cli_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_cli(mod, argv, fleet_repo, monkeypatch):
    """Run mod.main(argv) with stdout/stderr captured; cc_invoke.route is monkeypatched
    to call the real _handler in-process (no coordinator_core.invoke subprocess/daemon).

    # Review: coordinator:code-reviewer — monkeypatch.setattr guarantees restoration
    # even if the harness raises before a hand-rolled finally is reached.
    """

    def fake_route(op, params, repo_root, legacy_fn):
        assert op == "fleet.archive_paper_trail"
        return _run(_handler(params, repo_root=fleet_repo.common_dir))

    monkeypatch.setattr(mod.cc_invoke, "route", fake_route)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_leg1_untracked_src_degrades_with_no_destination_residue(fleet_repo, monkeypatch):
    """AC11: rc == _ARCHIVE_DEGRADED and the failure reason reaches stderr.
    AC12: no destination residue after total failure — the destination dir must not
    EXIST, not merely be empty (an empty dir is exactly the pre-C9 residue defect)."""
    mod = _load_cli_module()
    run_id = "2026-08-05-leg1"
    topic_slug = "leg1-untracked-topic"
    workdir = _seed_untracked_workdir(fleet_repo, run_id)

    argv = ["--run-id", run_id, "--topic-slug", topic_slug, "--dry-run", "false", str(fleet_repo.root)]
    rc, out, err = _run_cli(mod, argv, fleet_repo, monkeypatch)

    assert rc == mod._ARCHIVE_DEGRADED
    assert "archive degraded" in err
    assert "not under version control" in err

    dest = _dest_dir(fleet_repo, topic_slug)
    assert not dest.exists(), "AC12: destination directory must not exist after total failure"
    assert workdir.exists(), "src is left untouched on total failure (refuse-before-mutate)"


# ---------------------------------------------------------------------------
# Leg 2 — tracked src control: must stay green.
# ---------------------------------------------------------------------------

def test_leg2_tracked_src_control_stays_green(fleet_repo):
    """The contrast leg — proves trackedness is the single differing variable between
    leg 1 (untracked, fails) and this leg (tracked, succeeds)."""
    run_id = "2026-08-05-leg2"
    topic_slug = "leg2-tracked-topic"
    workdir = _seed_workdir(fleet_repo, run_id)

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True
    assert result["already_archived"] is False
    assert "failed" not in result

    dest_rel = f"docs/research/archive/{_today()}-{topic_slug}"
    assert not workdir.exists()
    assert fleet_repo.path_exists(f"{dest_rel}/notes.md")
    assert fleet_repo.git_status_clean(), "leg 2 is the ONLY leg where a clean tree is expected"


# ---------------------------------------------------------------------------
# Leg 3 — fail (untracked) -> belatedly track+commit -> retry.
# ---------------------------------------------------------------------------

def test_leg3_fail_then_track_then_retry_succeeds(fleet_repo):
    """AC14: the retry succeeds, not wedged behind stale residue.

    False-green warning (dispatch brief): the retry's return triple alone cannot be
    trusted to discriminate a genuine success from a repeat of the collision branch —
    both legs assert on HEAD movement / ls-files / archived file presence, never on
    the return value shape alone.
    """
    run_id = "2026-08-05-leg3"
    topic_slug = "leg3-retry-topic"
    workdir = _seed_untracked_workdir(fleet_repo, run_id)

    head_before = fleet_repo.git_log_subject()

    first = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))
    assert first["archived"] is False
    assert first.get("failed")

    # HEAD did not move on the failed attempt.
    assert fleet_repo.git_log_subject() == head_before

    # Belatedly track + commit the workdir (the fix the audit's leg 3 exercised).
    fleet_repo._git("add", str(workdir))
    fleet_repo._git("commit", "-m", f"belatedly track paper trail {run_id}")

    second = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert second["archived"] is True, "retry must succeed now that src is tracked and no residue wedges it"
    assert second.get("already_archived") is False

    dest_rel = f"docs/research/archive/{_today()}-{topic_slug}"
    ls_files = subprocess.run(
        ["git", "ls-files", "--", dest_rel],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().splitlines()
    assert f"{dest_rel}/notes.md" in ls_files
    assert not workdir.exists()
    assert fleet_repo.git_status_clean()


# ---------------------------------------------------------------------------
# Leg 4 — fail (untracked) -> operator hand-deletes the workdir -> re-query.
# ---------------------------------------------------------------------------

def test_leg4_hand_deleted_workdir_reports_no_phantom_archive(fleet_repo):
    """AC13: no false already_archived signal pointing at an empty/phantom archive dir.

    The handler's "src is gone" branch unconditionally reports already_archived:True
    (that is the deliberate DEC-7 idempotency contract for a GENUINE prior success —
    see archive_paper_trail.py's module docstring). What must NOT happen is that report
    resolving to a directory that was never actually populated: pre-C9, a failed run's
    empty residue dir satisfied `_find_existing_archive`'s glob and was handed back as
    `dest`, letting a caller believe a real archive exists at that path when it holds
    zero files. Post-C9, the failed run leaves no residue at all, so this leg asserts the
    reported dest does not exist on disk and no directory anywhere matches the
    topic_slug glob.
    """
    run_id = "2026-08-05-leg4"
    topic_slug = "leg4-handdelete-topic"
    workdir = _seed_untracked_workdir(fleet_repo, run_id)

    first = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))
    assert first["archived"] is False
    assert not (fleet_repo.root / first["dest"]).exists()

    # Operator hand-deletes the (still-untracked, still-failed) workdir.
    shutil.rmtree(workdir)

    second = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert second["already_archived"] is True
    dest_path = fleet_repo.root / second["dest"]
    assert not dest_path.exists(), (
        "AC13: the reported dest must not point at a phantom/empty archive dir -- "
        "zero files were ever archived for this topic_slug"
    )

    archive_dir = fleet_repo.root / "docs" / "research" / "archive"
    matches = list(archive_dir.glob(f"*-{topic_slug}")) if archive_dir.is_dir() else []
    assert matches == [], "no directory anywhere should match the topic_slug glob -- nothing was ever archived"


# ---------------------------------------------------------------------------
# Leg 5 — dry_run:true preview over an untracked src (recorded, not fixed).
# ---------------------------------------------------------------------------

def test_leg5_dry_run_preview_does_not_discriminate_tracked_state(fleet_repo):
    """Leg 5 is a RECORDED non-fix, not an assertion of correct behaviour.

    `_handler`'s dry_run branch is reached only after `if not src.exists()` and
    `if dest.exists()` — both pure on-disk-presence checks. Neither touches git's index,
    so the preview cannot tell an untracked src (which WILL fail on the corresponding
    dry_run:false act) from a tracked one (which will succeed) — the preview is
    byte-identical either way. This is the exact finding in
    state/audits/2026-08-05-d2-paper-trail-untracked-workdir-probe.md § "Leg 5": "There
    is no pre-flight signal available to the caller." Accepted because: dry_run's whole
    contract is "mutate nothing", and the cheapest way to add tracked-state discrimination
    (an extra `git ls-files` probe) would be a NEW mechanism this plan's C1/C8/C9 scope
    never asked for — out of scope for this promotion pass. This test exists so a future
    change to the dry_run branch that accidentally starts discriminating (or a change that
    silently regresses leg 2/leg 3's fix) is forced to touch this assertion deliberately
    rather than drift unnoticed.
    """
    topic_slug = "leg5-preview-topic"

    untracked_run_id = "2026-08-05-leg5-untracked"
    _seed_untracked_workdir(fleet_repo, untracked_run_id)
    preview_untracked = _run(_handler(
        {"run_id": untracked_run_id, "topic_slug": topic_slug, "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    tracked_run_id = "2026-08-05-leg5-tracked"
    _seed_workdir(fleet_repo, tracked_run_id)
    preview_tracked = _run(_handler(
        {"run_id": tracked_run_id, "topic_slug": topic_slug, "dry_run": True},
        repo_root=fleet_repo.common_dir,
    ))

    assert preview_untracked == preview_tracked, (
        "recorded non-fix: the dry_run preview is byte-identical for tracked and "
        "untracked src -- see this test's docstring"
    )
    assert preview_untracked == {
        "archived": False,
        "already_archived": False,
        "dest": f"docs/research/archive/{_today()}-{topic_slug}",
    }


# ---------------------------------------------------------------------------
# Leg D — tracked src, edited on disk after commit but before archival.
# ---------------------------------------------------------------------------

def test_legD_tracked_then_edited_before_archival_preserves_committed_blob(fleet_repo):
    """The wave-1 commit-before-archive ordering-constraint regression proof
    (state/audits/2026-08-05-staging-is-insufficient-and-the-staleness-hazard-is-real.md
    § leg D). The archived blob at HEAD must be the COMMITTED content; the later on-disk
    edit must survive at dst as an UNSTAGED worktree modification (porcelain ` M dst`) —
    NOT a clean tree (clean-tree is leg 2's control only)."""
    run_id = "2026-08-05-legD"
    topic_slug = "legD-staleness-topic"
    committed_content = "committed content\n"
    edited_content = "EDITED AFTER COMMIT -- must not be archived\n"

    workdir = _seed_workdir(fleet_repo, run_id, content=committed_content)
    src_file = workdir / "notes.md"

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()

    # Edit on disk AFTER commit, BEFORE archival — the exact hazard window.
    src_file.write_text(edited_content, encoding="utf-8")

    result = _run(_handler(
        {"run_id": run_id, "topic_slug": topic_slug, "dry_run": False},
        repo_root=fleet_repo.common_dir,
    ))

    assert result["archived"] is True
    assert "failed" not in result, "no per-file git-mv failure -- and hence no index_resync_failed path -- on this leg"

    dest_rel = f"docs/research/archive/{_today()}-{topic_slug}"
    dst_rel_file = f"{dest_rel}/notes.md"

    committed_blob = subprocess.run(
        ["git", "show", f"HEAD:{dst_rel_file}"],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode()
    assert committed_blob == committed_content, "HEAD must carry the COMMITTED blob, not the later on-disk edit"

    on_disk = (fleet_repo.root / dst_rel_file).read_text(encoding="utf-8")
    assert on_disk == edited_content, "the later on-disk edit must survive at dst"

    porcelain = subprocess.run(
        ["git", "status", "--porcelain", "--", dst_rel_file],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode()
    assert porcelain == f" M {dst_rel_file}\n", (
        "dst must show as an UNSTAGED worktree modification -- not a clean tree "
        "(clean-tree belongs only to leg 2's clean-src control), and not staged "
        "(a staged M would mean the edit got absorbed into the index)"
    )

    # Exactly one new commit landed (the archival commit) -- no bare commit
    # absorbed the on-disk edit.
    new_commit_count = subprocess.run(
        ["git", "rev-list", "--count", f"{head_before}..HEAD"],
        cwd=str(fleet_repo.root), capture_output=True, check=True,
    ).stdout.decode().strip()
    assert new_commit_count == "1"
    subject = fleet_repo.git_log_subject()
    assert subject == f"fleet: archive paper-trail {run_id} -> {_today()}-{topic_slug} [fleet.archive_paper_trail]"

    assert not workdir.exists()
    assert fleet_repo.path_exists(f"{dest_rel}/notes.md")

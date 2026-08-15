"""coordinator/bin/tests/test_publish_swap_preserves_dest_git.py — regression
guard for the C1 fix (`_swap_publish_staging_into_dest` reordered so `.git`
rides `prior_backup` instead of being pre-moved into the doomed staging
tree). See `docs/plans/2026-08-10-publish-swap-git-in-a-doomed-directory.md`
and the spike verdict at
`docs/research/spike-verdicts/2026-08-10-publish-swap-git-in-doomed-directory.md`
this file promotes from throwaway scaffolding.

Every arm builds a REAL git repo as the destination (`git init` + one
commit, HEAD captured) and drives the REAL `_swap_publish_staging_into_dest`
— never a stub of the swap function itself. Failures are injected by
monkeypatching `os.rename` for the one call site each arm targets, which is
the only way to prove something about the ACTUAL rename sequence rather
than about a test double standing in for it (see
`test_publish_swap_failure_report_honesty.py`'s own docstring, which
names this exact gap).

Assertions resolve HEAD from the on-disk `.git` (`publish._git_head`), never
merely check that a `.git` path exists — a directory that exists but no
longer resolves HEAD is still a destroyed repo.

Run: python -m pytest coordinator/bin/tests/test_publish_swap_preserves_dest_git.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

import pytest


# Declared, not excused: this file spawns real processes because the behaviour under
# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
# not the route for a new file -- test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_swap_preserves_dest_git_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------
def _git(args: "list[str]", cwd: Path) -> None:
    """Runs `git <args>` in `cwd` with identity/signing pinned via `-c`
    flags on the invocation itself — never repo-local or global config —
    so these fixtures never depend on (or mutate) this machine's global git
    config, per the C2 dispatch brief."""
    # popup-safe-env-suppressed
    subprocess.run(
        [
            "git",
            "-c", "user.email=publish-swap-test@example.invalid",
            "-c", "user.name=publish-swap-test",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            *args,
        ],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_git_repo(path: Path, *, seed_name: str = "seed.txt") -> str:
    """`git init` + one commit in `path`; returns the resulting HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    (path / seed_name).write_text("seed\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)
    head = publish._git_head(path)
    assert head, f"fixture setup failed: {path} has no resolvable HEAD"
    return head


def _install_rename_trap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_when: Callable[[Path, Path], bool],
    message_for: Optional[Callable[[Path, Path], str]] = None,
) -> None:
    """Patches `os.rename` so any call whose `(src, dst)` satisfies
    `fail_when` raises `PermissionError` instead of renaming; every other
    call passes through to the real `os.rename` unchanged. This is the
    injection mechanism every arm below uses to fail a SPECIFIC rename
    inside the real `_swap_publish_staging_into_dest`, rather than stubbing
    the function out."""
    real_rename = os.rename

    def fake_rename(src, dst, *a, **k):
        src_path, dst_path = Path(src), Path(dst)
        if fail_when(src_path, dst_path):
            msg = (
                message_for(src_path, dst_path)
                if message_for is not None
                else f"simulated rename failure: {src_path} -> {dst_path}"
            )
            raise PermissionError(13, msg)
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", fake_rename)


def _stranded_prior_dir(dest_dir: Path) -> Path:
    """Materializes a `.prior` directory left behind by an EARLIER,
    incomplete swap of `dest_dir` — same naming convention
    `_swap_publish_staging_into_dest` itself uses
    (`_create_publish_staging_dir`'s `tempfile.mkdtemp` prefix, then
    renamed with the `.prior` suffix), holding its own real `.git`."""
    stray = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    prior = stray.with_name(stray.name + ".prior")
    stray.rename(prior)
    _init_git_repo(prior, seed_name="stranded-seed.txt")
    return prior


# ---------------------------------------------------------------------------
# arm a (AC2) — PermissionError on the staging->dest rename (site 2).
# ---------------------------------------------------------------------------
def test_arm_a_staging_to_dest_rename_failure_preserves_git(tmp_path, monkeypatch):
    # dest_dir must NOT hold its own `.git` here: this arm pins the
    # WHOLE-TREE path (`_swap_publish_staging_into_dest`'s non-root branch),
    # which is only reachable when `(dest_dir / ".git")` is absent (§ that
    # function's root-dest detection docstring) — a `dest_dir` with its own
    # `.git` now takes the root branch instead, an entirely different code
    # path with no `os.rename(staging_dir, dest_dir)` call site to trap.
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "subdir"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)
    original_head = publish._git_head(repo_root)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: s == staging_dir and d == dest_dir,
    )

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (repo_root / ".git").exists()
    assert publish._git_head(repo_root) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# arm b (AC2) — raise on the dest->prior_backup rename (site 1).
# ---------------------------------------------------------------------------
def test_arm_b_dest_to_prior_backup_rename_failure_preserves_git(tmp_path, monkeypatch):
    # dest_dir must NOT hold its own `.git` here — see arm a's comment on
    # why (this pins the whole-tree path, only reachable for a `dest_dir`
    # with no `.git` of its own).
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "subdir"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)
    original_head = publish._git_head(repo_root)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    prior_backup = staging_dir.with_name(staging_dir.name + ".prior")

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: s == dest_dir and d == prior_backup,
    )

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (repo_root / ".git").exists()
    assert publish._git_head(repo_root) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"
    # site 1 never completed — nothing was ever renamed aside.
    assert not prior_backup.exists()


# ---------------------------------------------------------------------------
# arm c (AC2) — recovery rename (site 2's own restore) also fails.
# ---------------------------------------------------------------------------
def test_arm_c_recovery_rename_also_fails_git_still_recoverable(tmp_path, monkeypatch):
    # dest_dir must NOT hold its own `.git` here — see arm a's comment.
    # `prior_backup` therefore never carries a `.git` for THIS row shape
    # either (that only happens for a root-dest row, § arm f) — this arm's
    # remaining value is "original content is still fully recoverable from
    # `prior_backup` when both renames fail", which holds independent of
    # `.git`.
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "subdir"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")
    prior_backup = staging_dir.with_name(staging_dir.name + ".prior")

    def message_for(s: Path, d: Path) -> str:
        return f"simulated failure moving {s} to {d}, prior_backup={prior_backup}"

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: (s == staging_dir and d == dest_dir)
        or (s == prior_backup and d == dest_dir),
        message_for=message_for,
    )

    with pytest.raises(PermissionError) as excinfo:
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    # The error names prior_backup — an operator reading it can find the data.
    assert str(prior_backup) in str(excinfo.value)
    # The original content is recoverable: both renames failed, so nothing
    # actually moved — prior_backup still holds it under its aside-name.
    assert (prior_backup / "payload.txt").read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# arm d (AC4) — abort before the swap is entered: destination untouched.
# ---------------------------------------------------------------------------
def test_arm_d_abort_before_swap_leaves_destination_untouched(tmp_path, monkeypatch):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    original_head = _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    target = publish.ResolvedTarget(
        name="arm-d-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )

    def failing_post_rsync(*a, **k):
        raise publish.EngineUnavailableError("simulated engine unavailable before swap")

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", failing_post_rsync)
    swap_calls: list = []
    monkeypatch.setattr(
        publish,
        "_swap_publish_staging_into_dest",
        lambda *a, **k: swap_calls.append((a, k)),
    )

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    # The swap was never even attempted.
    assert swap_calls == []
    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"
    assert totals.synced == 0
    # The staging tree the guard-before-mutate seam created must not leak.
    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []


# ---------------------------------------------------------------------------
# arm e (AC3) — happy path: .git + HEAD survive, content updated.
# ---------------------------------------------------------------------------
def test_arm_e_happy_path_preserves_git_and_updates_content(tmp_path):
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "new content\n"
    assert not staging_dir.exists()
    prior_backup = staging_dir.with_name(staging_dir.name + ".prior")
    assert not prior_backup.exists()


# ---------------------------------------------------------------------------
# arm f (AC2, AC5b) — raise on the prior_backup/.git -> dest/.git rename
# (site 3, the only genuinely new failure state C1 introduces). Driven
# through the real `process_target`, not the swap function alone, because
# assertion (iv) is about the ROW'S REPORT (`process_target`'s recording
# behaviour on `PublishSwapPartial`), not the swap function in isolation.
# ---------------------------------------------------------------------------
# arm f is now a root-dest UNREACHABILITY pin, not a rehome-failure pin: a
# `dest_dir` holding its own `.git` (this arm's exact shape) is precisely
# the "root-dest" case `_swap_publish_staging_into_dest`'s new detection
# routes to `_swap_publish_staging_into_dest_root`, which never touches
# `.git` at all (§ that function's docstring). The `os.rename(..., dest_dir
# / ".git")` site 3 this arm used to trap can therefore never fire for this
# shape anymore, and `PublishSwapPartial`'s `.git`-rehome raise is
# unreachable for it — this arm now proves that directly (trap installed,
# asserted never triggered) instead of asserting the failure it induces.
# ---------------------------------------------------------------------------
def test_arm_f_git_rehome_failure_reports_content_change_honestly(tmp_path, monkeypatch):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    original_bytes = b"original payload bytes\n"
    payload_path = dest_dir / "widget.py"
    payload_path.write_bytes(original_bytes)
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    target = publish.ResolvedTarget(
        name="arm-f-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )

    fixed_bytes = b"published payload bytes\n"

    def fake_post_rsync(engine_ctx, store_path, sync_target, effective_source_dir, visited_sink=None):
        staged_path = sync_target.dest_dir / "widget.py"
        staged_path.write_bytes(fixed_bytes)
        if visited_sink is not None:
            visited_sink.add(staged_path)
        return None

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", fake_post_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)

    # Would have trapped the trailing .git rehome (old site 3) — kept to
    # prove it is never called for this shape anymore, not to induce a
    # failure.
    rehome_attempts: list = []

    def message_for(s: Path, d: Path) -> str:
        rehome_attempts.append((s, d))
        return f"should be unreachable: {s} -> {d}"

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: d == dest_dir / ".git",
        message_for=message_for,
    )

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})
    visited_files_sink: set = set()
    published_dest_dirs_sink: set = set()

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        visited_files_sink=visited_files_sink,
        published_dest_dirs_sink=published_dest_dirs_sink,
        out=out,
    )

    # The old .git-rehome rename site was never reached: the root branch
    # never renames anything onto `dest_dir / ".git"`.
    assert rehome_attempts == []

    # `.git` was never even renamed aside — it is untouched throughout, and
    # HEAD still resolves to the pre-swap SHA (there is nothing new to
    # commit; this row's own swap never touches `.git`).
    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head

    # Content still lands correctly.
    assert payload_path.read_bytes() == fixed_bytes
    assert totals.synced == 1
    report = out.getvalue()
    assert "UPDATE:" in report
    assert "widget.py" in report
    assert visited_files_sink == {dest_dir / "widget.py"}
    assert published_dest_dirs_sink == {dest_dir}


# ---------------------------------------------------------------------------
# arm g (AC3) — dest_subdir shaped destination with no .git of its own:
# happy path, step 3 a no-op. Regression pin for the shape 7 of 8 real
# publish rows take.
# ---------------------------------------------------------------------------
def test_arm_g_dest_subdir_without_own_git_step3_is_noop(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "subdir"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    assert not (dest_dir / ".git").exists()

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert not (dest_dir / ".git").exists()
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "new content\n"
    # The repo root's own .git is entirely untouched by a subdir-scoped swap.
    assert (repo_root / ".git").exists()
    assert publish._is_git_repo(repo_root)
    prior_backup = staging_dir.with_name(staging_dir.name + ".prior")
    assert not prior_backup.exists()
    assert not staging_dir.exists()


# ---------------------------------------------------------------------------
# arm h (AC5c) — the stranded-.prior refuse check. dest_dir must NOT hold
# its own `.git` (root-dest shape skips this guard entirely — it never
# reaches the check, § `_swap_publish_staging_into_dest`'s docstring); a
# `dest_subdir` shape is the only one that still exercises it.
# ---------------------------------------------------------------------------
def test_arm_h_stranded_prior_refuses_before_any_rename(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "subdir"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)
    original_head = publish._git_head(dest_dir)

    stranded_prior = _stranded_prior_dir(dest_dir)
    stranded_head = publish._git_head(stranded_prior)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    with pytest.raises(publish.PublishSwapPartial) as excinfo:
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    exc = excinfo.value
    assert exc.content_swapped is False

    # (i) nothing touched dest_dir or the fresh staging_dir.
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"
    assert publish._git_head(stranded_prior) == stranded_head
    assert staging_dir.exists()

    # (ii) the message names both dest_dir and the stranded .prior path.
    assert str(dest_dir) in str(exc)
    assert str(stranded_prior) in str(exc)


def test_arm_h_ensure_dest_ready_degraded_existing_dest_refuses(tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "stray.txt").write_text("stray content\n", encoding="utf-8")
    # No .git of its own, and no .git ancestor above it (tmp_path is not a repo).

    target = publish.ResolvedTarget(
        name="degraded-row",
        mode="manifest",
        source_dir=tmp_path / "source",
        dest_dir=dest_dir,
    )
    totals = publish.RunTotals()
    out = io.StringIO()

    assert publish._ensure_dest_ready(target, totals, out=out) is False


def test_arm_h_ensure_dest_ready_virgin_subdir_bootstraps(tmp_path):
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "mirror" / "not-yet-created"

    target = publish.ResolvedTarget(
        name="virgin-row",
        mode="manifest",
        source_dir=tmp_path / "source",
        dest_dir=dest_dir,
    )
    totals = publish.RunTotals()
    out = io.StringIO()

    assert publish._ensure_dest_ready(target, totals, out=out) is True
    assert dest_dir.is_dir()


# ---------------------------------------------------------------------------
# arm i — staging-directory leak regression (docs: publish leaves
# `.<name>.publish-staging-*` behind in the destination repo after a fully
# successful run). Two shapes: (1) the happy path through `process_target`
# leaves nothing behind, already covered structurally by arm e/g at the
# `_swap_publish_staging_into_dest` level — this arm pins it at the
# `process_target` level, where the real leak was observed. (2) a row that
# raises mid-flight (post-staging) must not leak its staging dir either.
# ---------------------------------------------------------------------------
def test_arm_i_successful_row_leaves_no_staging_dir_via_process_target(tmp_path, monkeypatch):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)

    target = publish.ResolvedTarget(
        name="arm-i-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )
    monkeypatch.setattr(
        publish,
        "dispatch_percolate_post_rsync",
        lambda engine_ctx, store_path, sync_target, effective_source_dir, visited_sink=None: None,
    )
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    assert totals.processed == 1
    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []


def test_arm_i_row_that_raises_after_staging_leaves_no_staging_dir(tmp_path, monkeypatch):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)

    target = publish.ResolvedTarget(
        name="arm-i-raise-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )

    def failing_post_rsync(*a, **k):
        raise publish.EngineUnavailableError("simulated engine unavailable after staging")

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", failing_post_rsync)

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    assert totals.processed == 0
    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []


def test_arm_i_copytree_failure_during_staging_creation_leaves_no_orphan(tmp_path, monkeypatch):
    """Regression for the real defect: `_create_publish_staging_dir` used to
    `mkdtemp` then `copytree` with no cleanup of its own -- a raise from
    `copytree` propagated past the `staging_dir = ...` assignment in
    `process_target`, so the caller's local stayed `None` and the `finally`
    block's `_discard_publish_staging_dir(None)` was a no-op, permanently
    orphaning the already-created directory. This is the actual mechanism
    that stranded `.bin.publish-staging-9z8ye65a` /
    `.coordinator_core.publish-staging-o32a9q31` in the real mirror."""
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)

    def failing_copytree(*a, **k):
        raise OSError("simulated copytree failure mid-copy")

    monkeypatch.setattr(publish.shutil, "copytree", failing_copytree)

    with pytest.raises(OSError):
        publish._create_publish_staging_dir(dest_dir)

    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []


# ---------------------------------------------------------------------------
# arm j — `_sweep_stale_publish_staging_dirs` (orphan-staging-dir cleanup).
# See docs/wiki/machine-load-norm.md for the concurrency context: a `kill
# -9`, a machine reboot, or a session killed mid-publish leaves one of these
# behind, and this box runs 50-70 concurrent sessions, so a live sibling
# publish's own staging dir must never be swept.
# ---------------------------------------------------------------------------
def test_arm_j_stale_staging_dir_is_removed(tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    stale = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    (stale / "leftover.txt").write_text("orphaned\n", encoding="utf-8")
    old_time = publish.time.time() - 7200  # 2 hours ago, past the 1h default threshold
    os.utime(stale, (old_time, old_time))

    totals = publish.RunTotals()
    out = io.StringIO()
    publish._sweep_stale_publish_staging_dirs(dest_dir, totals, out=out)

    assert not stale.exists()
    assert totals.warnings == 0


def test_arm_j_recent_staging_dir_is_left_alone(tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    live = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    (live / "in-progress.txt").write_text("still being written\n", encoding="utf-8")

    totals = publish.RunTotals()
    out = io.StringIO()
    publish._sweep_stale_publish_staging_dirs(dest_dir, totals, out=out)

    assert live.exists()
    assert (live / "in-progress.txt").read_text(encoding="utf-8") == "still being written\n"


def test_arm_j_partially_copied_orphan_is_removed(tmp_path):
    """Reproduces the actual old-bug orphan shape, not just a clean
    `tempfile.mkdtemp` fixture: `_create_publish_staging_dir` used to leave
    a directory `mkdtemp`'d but only PARTWAY through `copytree` when the
    copy itself raised (§ `test_arm_i_copytree_failure_during_staging_creation_leaves_no_orphan`,
    the fix for the create-path). This arm proves the SWEEP independently
    clears that exact partially-populated shape once it ages past the
    threshold, rather than only proving it matches a directory freshly
    minted and left empty."""
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    (dest_dir / "nested").mkdir()
    (dest_dir / "nested" / "more.txt").write_text("more\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)

    orphan = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    # Simulate a `copytree` that raised after copying one file but before
    # reaching the rest -- the exact partial shape the old bug left behind,
    # not an empty `mkdtemp` result.
    (orphan / "payload.txt").write_text("original\n", encoding="utf-8")

    old_time = publish.time.time() - 7200  # 2 hours ago, past the 1h default threshold
    os.utime(orphan, (old_time, old_time))

    totals = publish.RunTotals()
    out = io.StringIO()
    publish._sweep_stale_publish_staging_dirs(dest_dir, totals, out=out)

    assert not orphan.exists()
    assert totals.warnings == 0
    # The real destination and its .git are untouched by the sweep.
    assert (dest_dir / ".git").exists()
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"


def test_arm_j_non_matching_directory_is_untouched(tmp_path):
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    # Not this tool's naming shape at all -- the blast-radius test.
    unrelated = dest_dir.parent / "some-other-directory"
    unrelated.mkdir()
    (unrelated / "payload.txt").write_text("not ours\n", encoding="utf-8")
    old_time = publish.time.time() - 7200
    os.utime(unrelated, (old_time, old_time))

    # Also confirm a `.prior` directory (a DIFFERENT lifecycle) is untouched
    # even when old and matching the staging prefix otherwise.
    prior_shaped = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}.publish-staging-", dir=str(dest_dir.parent))
    )
    prior = prior_shaped.with_name(prior_shaped.name + ".prior")
    prior_shaped.rename(prior)
    os.utime(prior, (old_time, old_time))

    totals = publish.RunTotals()
    out = io.StringIO()
    publish._sweep_stale_publish_staging_dirs(dest_dir, totals, out=out)

    assert unrelated.exists()
    assert (unrelated / "payload.txt").read_text(encoding="utf-8") == "not ours\n"
    assert prior.exists()


# ---------------------------------------------------------------------------
# arm h (glob-metachar regression) — a `dest_dir.name` containing `[`/`]`
# must not let the `.prior` sweep's glob under- or over-match against a
# look-alike sibling. Mirrors `_sweep_stale_publish_staging_dirs`'s own
# glob-metachar coverage, but for `_swap_publish_staging_into_dest`'s
# stranded-`.prior` guard (the second interpolation site, § dispatch brief).
# ---------------------------------------------------------------------------
def test_arm_h_stranded_prior_glob_metachar_dest_name_still_matched(tmp_path):
    # dest_dir must NOT hold its own `.git` — see the plain arm h above for
    # why (root-dest skips this guard entirely).
    repo_root = tmp_path / "repo"
    _init_git_repo(repo_root)
    dest_dir = repo_root / "app[1]"
    dest_dir.mkdir(parents=True)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], repo_root)
    _git(["commit", "-q", "-m", "payload"], repo_root)
    original_head = publish._git_head(dest_dir)

    # This dest's own stranded `.prior`, minted with the literal bracketed
    # name -- an unescaped `[1]` in the glob would be read as a one-char
    # class matching "1", NOT as literal brackets, so this would go
    # UNDER-matched (missed) by a regressed, unescaped pattern.
    stranded_prior = _stranded_prior_dir(dest_dir)
    stranded_head = publish._git_head(stranded_prior)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    with pytest.raises(publish.PublishSwapPartial) as excinfo:
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    exc = excinfo.value
    assert exc.content_swapped is False
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"
    assert publish._git_head(stranded_prior) == stranded_head
    assert staging_dir.exists()
    assert str(dest_dir) in str(exc)
    assert str(stranded_prior) in str(exc)


def test_arm_h_stranded_prior_glob_metachar_dest_name_ignores_lookalike_sibling(tmp_path):
    dest_dir = tmp_path / "app[1]"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    # A look-alike sibling that an UNESCAPED `.app[1].publish-staging-*.prior`
    # pattern would over-match: `[1]` read as a one-char class matches the
    # single literal character "1", so this bracket-free `.app1...` name
    # would falsely satisfy a regressed pattern for `app[1]`'s own sweep.
    lookalike = Path(
        tempfile.mkdtemp(prefix=".app1.publish-staging-", dir=str(dest_dir.parent))
    )
    lookalike_prior = lookalike.with_name(lookalike.name + ".prior")
    lookalike.rename(lookalike_prior)
    _init_git_repo(lookalike_prior, seed_name="lookalike-seed.txt")
    lookalike_head = publish._git_head(lookalike_prior)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    # No refuse: the look-alike is not this dest's own stranded `.prior`.
    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "new content\n"
    # The look-alike sibling is completely untouched by this dest's swap.
    assert lookalike_prior.exists()
    assert publish._git_head(lookalike_prior) == lookalike_head


def test_arm_h_ensure_dest_ready_absent_no_git_ancestor_refuses(tmp_path):
    dest_dir = tmp_path / "nowhere" / "dest"

    target = publish.ResolvedTarget(
        name="unresolved-row",
        mode="manifest",
        source_dir=tmp_path / "source",
        dest_dir=dest_dir,
    )
    totals = publish.RunTotals()
    out = io.StringIO()

    assert publish._ensure_dest_ready(target, totals, out=out) is False
    assert not dest_dir.exists()


# ---------------------------------------------------------------------------
# arm k (root-dest branch) — `_swap_publish_staging_into_dest_root` coverage.
# `dest_dir` IS the repo root (`.git` directly inside it): files + a
# directory swap correctly, `dest_dir` itself is never renamed, `.git` and
# an unrelated sibling directory survive untouched, and a stale top-level
# file absent from staging is removed the same as the whole-tree path would
# have discarded it.
# ---------------------------------------------------------------------------
def test_arm_k_root_dest_swaps_files_and_dirs_never_renames_dest_dir(tmp_path):
    dest_dir = tmp_path / "repo"
    _init_git_repo(dest_dir)
    (dest_dir / "CHANGELOG.md").write_text("old\n", encoding="utf-8")
    managed_dir = dest_dir / "managed"
    managed_dir.mkdir()
    (managed_dir / "a.txt").write_text("old-a\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "seed"], dest_dir)
    dest_dir_inode_marker = dest_dir  # identity check below is path-based

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "CHANGELOG.md").write_text("new\n", encoding="utf-8")
    (staging_dir / "managed" / "a.txt").write_text("new-a\n", encoding="utf-8")

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert dest_dir == dest_dir_inode_marker and dest_dir.is_dir()
    assert (dest_dir / "CHANGELOG.md").read_text(encoding="utf-8") == "new\n"
    assert (dest_dir / "managed" / "a.txt").read_text(encoding="utf-8") == "new-a\n"
    assert not staging_dir.exists()


def test_arm_k_root_dest_preserves_git_and_unrelated_sibling(tmp_path):
    dest_dir = tmp_path / "repo"
    _init_git_repo(dest_dir)
    sibling_dir = dest_dir / "coordinator_core"
    sibling_dir.mkdir()
    (sibling_dir / "keep.py").write_text("unrelated\n", encoding="utf-8")
    (dest_dir / "CHANGELOG.md").write_text("old\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "seed"], dest_dir)
    original_head = publish._git_head(dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "CHANGELOG.md").write_text("new\n", encoding="utf-8")

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head
    # The unrelated sibling directory (another row's own output) is
    # byte-identical in staging (an untouched copy, § _dir_trees_equal) and
    # is therefore left alone, not renamed-aside.
    assert (sibling_dir / "keep.py").read_text(encoding="utf-8") == "unrelated\n"
    assert (dest_dir / "CHANGELOG.md").read_text(encoding="utf-8") == "new\n"


def test_arm_k_root_dest_removes_stale_top_level_file_absent_from_staging(tmp_path):
    dest_dir = tmp_path / "repo"
    _init_git_repo(dest_dir)
    (dest_dir / "AGENTS.md").write_text("stale\n", encoding="utf-8")
    (dest_dir / "CHANGELOG.md").write_text("kept\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "seed"], dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    # Simulates sync_flat_mirror's own "not in source" deletion phase, which
    # already ran directly against this staging copy before the swap.
    (staging_dir / "AGENTS.md").unlink()

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert not (dest_dir / "AGENTS.md").exists()
    assert (dest_dir / "CHANGELOG.md").read_text(encoding="utf-8") == "kept\n"
    assert (dest_dir / ".git").exists()


def test_arm_k_root_dest_changed_directory_is_swapped(tmp_path):
    dest_dir = tmp_path / "repo"
    _init_git_repo(dest_dir)
    managed_dir = dest_dir / "managed"
    managed_dir.mkdir()
    (managed_dir / "a.txt").write_text("old-a\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "seed"], dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    # A fresh copy is byte-identical to its source (copy2 preserves mtime).
    assert publish._dir_trees_equal(staging_dir / "managed", managed_dir)
    (staging_dir / "managed" / "a.txt").write_text("new-a\n", encoding="utf-8")
    assert not publish._dir_trees_equal(staging_dir / "managed", managed_dir)

    publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (managed_dir / "a.txt").read_text(encoding="utf-8") == "new-a\n"
    assert (dest_dir / ".git").exists()

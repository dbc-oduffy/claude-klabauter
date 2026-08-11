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
pytestmark = [pytest.mark.spawns_process]


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
    dest_dir = tmp_path / "dest"
    original_head = _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    (staging_dir / "payload.txt").write_text("new content\n", encoding="utf-8")

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: s == staging_dir and d == dest_dir,
    )

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# arm b (AC2) — raise on the dest->prior_backup rename (site 1).
# ---------------------------------------------------------------------------
def test_arm_b_dest_to_prior_backup_rename_failure_preserves_git(tmp_path, monkeypatch):
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    staging_dir = publish._create_publish_staging_dir(dest_dir)
    prior_backup = staging_dir.with_name(staging_dir.name + ".prior")

    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: s == dest_dir and d == prior_backup,
    )

    with pytest.raises(PermissionError):
        publish._swap_publish_staging_into_dest(dest_dir, staging_dir)

    assert (dest_dir / ".git").exists()
    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"
    # site 1 never completed — nothing was ever renamed aside.
    assert not prior_backup.exists()


# ---------------------------------------------------------------------------
# arm c (AC2) — recovery rename (site 2's own restore) also fails.
# ---------------------------------------------------------------------------
def test_arm_c_recovery_rename_also_fails_git_still_recoverable(tmp_path, monkeypatch):
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

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
    # `.git` (and the rest of the original content) is recoverable: both
    # renames failed, so nothing actually moved — prior_backup still holds
    # the complete original repo under its aside-name.
    assert (prior_backup / ".git").exists()
    assert publish._git_head(prior_backup) == original_head
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

    # Only the trailing .git rehome (site 3) fails — sites 1 and 2 run for
    # real, so the content swap genuinely lands before this injected failure.
    _install_rename_trap(
        monkeypatch,
        fail_when=lambda s, d: d == dest_dir / ".git",
    )

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})
    visited_files_sink: set = set()
    published_dest_dirs_sink: set = set()

    with pytest.raises(publish.PublishSwapPartial) as excinfo:
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

    exc = excinfo.value
    prior_backup = exc.prior_backup
    assert exc.content_swapped is True

    # (i) .git still resolves HEAD to the pre-swap SHA, from inside prior_backup.
    assert (prior_backup / ".git").exists()
    assert publish._git_head(prior_backup) == original_head

    # (ii) the raised error names prior_backup's concrete path.
    assert str(prior_backup) in str(exc)

    # (iii) prior_backup was not rmtree'd.
    assert prior_backup.is_dir()

    # (iv) the row's report discloses the content change (AC5b): content DID
    # land at dest_dir even though .git is stranded — dest_dir/.git is
    # therefore absent (the rehome that would have put it there is exactly
    # what failed).
    assert not (dest_dir / ".git").exists()
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
# arm h (AC5c) — the stranded-.prior refuse check.
# ---------------------------------------------------------------------------
def test_arm_h_stranded_prior_refuses_before_any_rename(tmp_path):
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
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

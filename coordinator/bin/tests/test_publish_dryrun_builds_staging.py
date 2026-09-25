"""coordinator/bin/tests/test_publish_dryrun_builds_staging.py — P079-C1
regression guard (`docs/plans/2026-09-11-publish-build-verify-swap-one-staging-pa.md`
chunk C1).

Before this fix, `process_target`'s dry-run leg was a hard `if dry_run: ...
skip ... ` branch that never created a staging tree, never dispatched
`dispatch_percolate_pre_rsync`/`dispatch_standalone_guards`, and never ran
the post_rsync/inject/pre_ci percolate phases — the stated reason being "the
engine has no non-mutating preview mode ... dispatching it under --dry-run
would actually mutate the destination tree". That reasoning is retired by
routing dry-run through the SAME staging-tree code path a real run uses
(`sync_target.dest_dir` is a throwaway copy in both modes): a dry run now
runs every percolate-engine phase against the staging copy and discards it,
never touching the real destination. Only the swap of that staging copy
into the real destination stays real-run-only (§ P079-C2).

This file drives the REAL `process_target` (never a stub of the staging
fork itself), with the percolate-engine dispatch functions monkeypatched to
lightweight fakes (mirrors `test_publish_swap_preserves_dest_git.py`'s own
`fake_post_rsync` pattern) so the run reaches the staging-tree creation and
post-transform write without needing a real percolate engine wired up.

Run: python -m pytest coordinator/bin/tests/test_publish_dryrun_builds_staging.py -x -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dryrun_builds_staging_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(args: "list[str]", cwd: Path) -> None:
    # popup-safe-env-suppressed
    subprocess.run(
        [
            "git",
            "-c", "user.email=publish-dryrun-staging-test@example.invalid",
            "-c", "user.name=publish-dryrun-staging-test",
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
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    (path / seed_name).write_text("seed\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "seed"], path)
    head = publish._git_head(path)
    assert head, f"fixture setup failed: {path} has no resolvable HEAD"
    return head


def _wire_common_fakes(monkeypatch, *, src_dir: Path, pre_rsync_calls: list, guard_calls: list):
    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(
        publish,
        "dispatch_percolate_pre_rsync",
        lambda *a, **k: pre_rsync_calls.append(a),
    )
    monkeypatch.setattr(
        publish,
        "dispatch_standalone_guards",
        lambda *a, **k: guard_calls.append(a),
    )
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_preswap_function_gate", lambda *a, **k: True)


def test_dry_run_builds_staging_with_post_transform_bytes_and_leaves_dest_byte_identical(
    tmp_path, monkeypatch
):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    payload_path = dest_dir / "widget.py"
    original_bytes = b"original payload bytes\n"
    payload_path.write_bytes(original_bytes)
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    # Untracked residue too — byte-identity must hold for it as well, not
    # only tracked git content.
    (dest_dir / "untracked.txt").write_text("untracked residue\n", encoding="utf-8")

    target = publish.ResolvedTarget(
        name="p079c1-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    pre_rsync_calls: list = []
    guard_calls: list = []
    _wire_common_fakes(monkeypatch, src_dir=src_dir, pre_rsync_calls=pre_rsync_calls, guard_calls=guard_calls)

    fixed_bytes = b"published payload bytes\n"
    staging_dirs_seen: list = []
    staged_bytes_seen: list = []

    def fake_post_rsync(
        engine_ctx,
        store_path,
        sync_target,
        effective_source_dir,
        *,
        visited_sink=None,
        sync_changed_paths=None,
    ):
        staging_dirs_seen.append(sync_target.dest_dir)
        staged_path = sync_target.dest_dir / "widget.py"
        staged_path.write_bytes(fixed_bytes)
        # Read back immediately, before the staging tree can be reclaimed —
        # this is the "the staging tree holds the post-transform bytes"
        # assertion; the tree itself is gone by the time `process_target`
        # returns under dry-run (§ the reclaim assertions below), so the
        # content check has to happen here, in-flight.
        staged_bytes_seen.append(staged_path.read_bytes())
        if visited_sink is not None:
            visited_sink.add(staged_path)
        return None, None

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", fake_post_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)

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
        dry_run=True,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    # The pre_rsync/standalone-guard percolate phases now dispatch under
    # dry-run too (P079-C1's whole point) — the pre-fix behaviour never
    # called either.
    assert len(pre_rsync_calls) == 1
    assert len(guard_calls) == 1

    # A staging tree was created and reached post_rsync, and it holds the
    # post-transform bytes `fake_post_rsync` wrote into it.
    assert len(staging_dirs_seen) == 1
    staging_dir = staging_dirs_seen[0]
    assert staging_dir != dest_dir
    assert staged_bytes_seen == [fixed_bytes]

    # The swap into the real destination was never attempted under dry-run.
    assert swap_calls == []

    # The real destination is byte-identical before and after — tracked
    # paths AND untracked residue.
    assert publish._git_head(dest_dir) == original_head
    assert payload_path.read_bytes() == original_bytes
    assert (dest_dir / "untracked.txt").read_text(encoding="utf-8") == "untracked residue\n"

    # No staging tree survives the call.
    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []
    assert not staging_dir.exists()


def test_dry_run_staging_tree_reclaimed_on_exception_path(tmp_path, monkeypatch):
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)
    (dest_dir / "payload.txt").write_text("original\n", encoding="utf-8")
    _git(["add", "."], dest_dir)
    _git(["commit", "-q", "-m", "payload"], dest_dir)
    original_head = publish._git_head(dest_dir)

    target = publish.ResolvedTarget(
        name="p079c1-raise-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    pre_rsync_calls: list = []
    guard_calls: list = []
    _wire_common_fakes(monkeypatch, src_dir=src_dir, pre_rsync_calls=pre_rsync_calls, guard_calls=guard_calls)

    def failing_post_rsync(*a, **k):
        raise publish.EngineUnavailableError("simulated engine unavailable during dry-run staging")

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
        dry_run=True,
        engine_ctx=engine_ctx,
        percolate_store_path=tmp_path / "store.yaml",
        out=out,
    )

    # pre_rsync/standalone-guards still ran before the failure below them.
    assert len(pre_rsync_calls) == 1
    assert len(guard_calls) == 1
    assert swap_calls == []

    assert publish._git_head(dest_dir) == original_head
    assert (dest_dir / "payload.txt").read_text(encoding="utf-8") == "original\n"

    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []


def test_dry_run_engine_unavailable_still_refuses_row(tmp_path, monkeypatch):
    # AC15 fail-closed (§ C1 body: "Engine-unavailable stays a row refusal
    # in both modes — unchanged"): before this fix, `if dry_run:` short-
    # circuited ahead of the engine-availability check entirely, so a
    # dry-run row with no engine never refused. It must refuse now, exactly
    # as a real run does.
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    dest_dir = tmp_path / "dest"
    _init_git_repo(dest_dir)

    target = publish.ResolvedTarget(
        name="p079c1-engine-unavailable-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dest_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )

    totals = publish.RunTotals()
    out = io.StringIO()
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=None, store=None)

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=True,
        identity=None,
        dry_run=True,
        engine_ctx=engine_ctx,
        percolate_store_path=None,
        out=out,
    )

    assert totals.processed == 0
    assert "percolate engine unavailable" in err.getvalue()
    leaked_staging = list(dest_dir.parent.glob(f".{dest_dir.name}.publish-staging-*"))
    assert leaked_staging == []

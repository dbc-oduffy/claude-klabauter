"""coordinator/bin/tests/test_publish_row_isolation.py — regression test for
the mid-first-row publish-run death mechanism: `process_target` can raise
`SystemExit` from deep inside a dispatched sync (`publish_sync.py`'s
orphan-sweep top-level-presence FATAL, `raise SystemExit(3)`). `SystemExit`
is a `BaseException`, not an `Exception` — before this fix, an uncaught raise
there unwound straight out of `main()`'s whole target loop, silently killing
every row after the one that raised: no traceback, no "Done." summary,
whatever raw exit code the raise carried.

This test drives `main()`'s REAL per-row loop (not a hand-rolled stand-in for
it) with `process_target` faked to raise `SystemExit(3)` on one specific row
and succeed normally on the others — modeling the actual failure, not a
happy path. A prior test in this area faked BOTH `load_targets` and
`process_target` so thoroughly it never exercised the loop's own row-to-row
control flow, and passed while the live path was broken; this test fakes
`process_target`'s BODY only, leaving the loop, the try/except around each
row dispatch, and the final summary/exit-code computation exactly as `main()`
runs them.

Run: python -m pytest coordinator/bin/tests/test_publish_row_isolation.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# `_init_git_repo` shells out to real `git` subprocesses (init/config/add/
# commit) to build a fixture repo for `held_lock`'s `git_common_dir()`
# resolution — real process spawn at function scope, not a mock.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_git_repo(root: Path) -> None:
    """Minimal git init so `held_lock`'s `git_common_dir()` resolution succeeds.

    Part B (state/handoffs/2026-08-07-percolate-performance-delta-sweep.md)
    added a per-destination advisory lock keyed via `git_common_dir(dest_dir)`
    to `main()`'s non-dry-run path — every real destination is a git repo
    (the spec's own precondition), so the fixture must model that now, not
    just an arbitrary tmp directory.
    """

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "publish-row-isolation-test@claude-klabauter.test")
    _git("config", "user.name", "Publish Row Isolation Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_row_isolation_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_NAMES = ["row-a", "row-b", "row-c", "row-d"]
# row-b is the one that raises SystemExit(3), matching the real
# top-level-presence FATAL's `raise SystemExit(3)` in publish_sync.py.
_FAILING_ROW = "row-b"


def _wire_common_fakes(monkeypatch, tmp_path, *, rows_reached: list):
    """Stub every precondition `main()` runs before the target loop so the
    test exercises real row-loop control flow without needing a real
    percolate store, identity file, or publish_sync module on disk."""

    def fake_row(name: str) -> str:
        src = tmp_path / f"src-{name}"
        dst = tmp_path / f"dst-{name}"
        src.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)
        _init_git_repo(dst)
        return f"{name}|mirror|{src}|{dst}"

    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter=None: [
            fake_row(n) for n in _ROW_NAMES
        ]
    )
    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            # `dispatch_end_of_run_function_gate` calls this unconditionally
            # for every reached repo root (§ C4C brief) — this fixture's
            # dest dirs contain nothing but `.gitkeep`, so a parse-clean,
            # zero-file sweep result is the honest answer, not a stand-in.
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            # `dispatch_end_of_run_entrypoint_gate` calls this unconditionally
            # too (§ chunk C3). This fixture's dest dirs ship no entrypoints
            # at all, so the honest answer is an empty tuple — the real
            # `enumerate_gate_entrypoints` would return the same for a repo
            # containing only `.gitkeep`, and an empty result short-circuits
            # the gate loop (`if not entrypoints: continue`) without needing
            # to fake `run_entrypoint_gate`/`derive_worker_cap`/
            # `mktcache_gate_env` as well.
            return ()

    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda path: publish.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        # Models the real failure mechanism: one row's dispatched sync
        # raises SystemExit directly (a BaseException, not an Exception),
        # exactly like publish_sync.py's orphan-sweep top-level-presence
        # FATAL. Every other row completes normally.
        rows_reached.append(target.name)
        if target.name == _FAILING_ROW:
            raise SystemExit(3)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)


def test_one_row_systemexit_does_not_kill_remaining_rows(monkeypatch, tmp_path):
    """The regression this closes: before the fix, `row-b` raising
    `SystemExit(3)` unwound `main()`'s whole loop, so `row-c`/`row-d` were
    NEVER reached — a test asserting only `totals.processed` (or exit code
    alone) cannot distinguish "processed 4, 1 failed" from "processed 1, then
    died," which is exactly the gap that let this ship. Assert on the SET of
    rows the loop actually reached instead."""
    rows_reached: list = []
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    rc = publish.main([",".join(_ROW_NAMES)])

    # The loop must have REACHED every requested row, not stopped at row-b.
    assert rows_reached == _ROW_NAMES

    # Property 2 (task spec): never exit 0 having processed fewer rows than
    # requested — one row failed, so the run-level exit code must be non-zero.
    assert rc != 0


def test_failed_row_reported_separately_from_succeeded_rows(monkeypatch, tmp_path, capsys):
    """Property 1 (task spec): a row that fails must not silently take the
    remaining rows with it — each row's outcome is reported, and the final
    summary states which rows succeeded and which did not."""
    rows_reached: list = []
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    publish.main([",".join(_ROW_NAMES)])
    captured = capsys.readouterr()

    combined = captured.out + captured.err
    assert "Rows succeeded: 3/4" in combined
    assert "row-a" in combined and "row-c" in combined and "row-d" in combined
    assert "Rows FAILED" in combined and "row-b" in combined


def test_all_rows_succeed_reports_full_count_and_zero_exit(monkeypatch, tmp_path):
    """Sanity counterpart — no failure means every requested row is reported
    succeeded and the run exits 0, so the new row-isolation path doesn't
    regress the happy path."""
    rows_reached: list = []
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    def fake_process_target_all_ok(target, setup_dir, totals, **kwargs):
        rows_reached.append(target.name)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target_all_ok)

    rc = publish.main([",".join(_ROW_NAMES)])

    assert rows_reached == _ROW_NAMES
    assert rc == 0

"""coordinator/bin/tests/test_publish_swap_failure_report_honesty.py —
regression test for the staged-publish honest-report-vs-swap ordering
defect (2026-08-08 break-class fix, `process_target`).

Mechanism: `process_target` runs a staged publish row's sync + content-
transform sweep against a destination-adjacent `staging_dir`
(`_create_publish_staging_dir`), computes the row's authoritative
`NEW:`/`UPDATE:`/`REMOVE:` report (`_report_published_diff`) by comparing
`staging_dir` (the fully-synced-and-transformed candidate) against
`target.dest_dir` (the untouched prior tree), and only THEN calls
`_swap_publish_staging_into_dest` to actually mutate the real destination.

Before this fix, the report printed straight to the real `out` and folded
its counts into the real `totals` BEFORE the swap ran. A swap that then
raised (a partial `os.rename` failure — the accepted-residual-risk case
that function's own docstring names, e.g. a transient external handle on
one nested path) left the real destination unchanged while the
already-printed `UPDATE:` line and the `totals.synced` increment were
never retracted. The caller's per-row isolation (`main`'s
`except (SystemExit, Exception)`) marks the row FAILED and prints a FATAL
line, but that does not un-print output already written — an operator
scanning stdout for `UPDATE:` lines (not cross-referencing every FATAL on
stderr) sees a truthful-looking report for a file the run never wrote.

This test asserts against the FILESYSTEM (the real destination file's
bytes after the call) and against the `out`/`totals` the row actually
produced — never against the presence/absence of a report line alone,
since a test that only checks "was UPDATE printed" cannot distinguish an
honest report from a dishonestly early one. It fails with the bug
present: before the fix, `dest_file` bytes are unchanged (proving no
write landed) while `out` still contains `UPDATE:` and `totals.synced`
is 1 — a contradiction this test pins as `should never happen`.

Run: python -m pytest coordinator/bin/tests/test_publish_swap_failure_report_honesty.py -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_swap_failure_report_honesty_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_swap_failure_leaves_destination_untouched_and_report_unclaimed(monkeypatch, tmp_path):
    src_dir = tmp_path / "source"
    dst_dir = tmp_path / "dest"
    src_dir.mkdir()
    dst_dir.mkdir()

    original_dest_bytes = b"self.claude-klabauter = _RealEngineClaudeKlabauter()\n"
    dest_file = dst_dir / "test_function_gate_wiring.py"
    dest_file.write_bytes(original_dest_bytes)

    target = publish.ResolvedTarget(
        name="fake-row",
        mode="manifest",
        source_dir=src_dir,
        dest_dir=dst_dir,
    )

    monkeypatch.setattr(
        publish,
        "run_pre_sync_gates",
        lambda *a, **k: publish.GateResult(proceed=True, source_dir=src_dir),
    )
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)

    # Sync leg is a no-op — the staging seed (copied from `dst_dir`) is left
    # as-is; the fixed content below is written directly to simulate what
    # the real content-transform sweep would have produced.
    monkeypatch.setattr(
        publish, "sync_manifest", lambda src, dst, totals, dry_run, out: True
    )

    fixed_bytes = b"self.engine_claude_klabauter = _RealEngineClaudeKlabauter()\n"

    def fake_post_rsync(engine_ctx, store_path, sync_target, effective_source_dir, visited_sink=None):
        (sync_target.dest_dir / "test_function_gate_wiring.py").write_bytes(fixed_bytes)
        return None

    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", fake_post_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)

    def failing_swap(dest_dir, staging_dir):
        raise OSError("simulated transient lock on swap")

    monkeypatch.setattr(publish, "_swap_publish_staging_into_dest", failing_swap)

    totals = publish.RunTotals()
    out = io.StringIO()
    engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=object(), store={})

    with pytest.raises(OSError):
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

    # The real destination must be byte-for-byte unchanged — the swap never
    # landed, so the run never actually wrote this file.
    assert dest_file.read_bytes() == original_dest_bytes

    report = out.getvalue()
    assert "UPDATE:" not in report
    assert "test_function_gate_wiring.py" not in report
    assert totals.synced == 0

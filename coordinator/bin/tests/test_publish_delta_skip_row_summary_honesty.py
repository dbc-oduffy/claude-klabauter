"""coordinator/bin/tests/test_publish_delta_skip_row_summary_honesty.py —
regression test for state/bug-backlog/2026-08-10-a-delta-skipped-row-reports-
rows-succeed-8806cf839322.yaml.

Mechanism: a `--delta` row that `delta_row_unchanged()` proves unchanged
prints an explicit "--delta: ... — skipping." line and `continue`s past
`process_target` entirely — correct, a skip is not a failure, and the run
still exits 0. But before this fix, the end-of-run summary line ("Rows
succeeded: N/M") counted that row in neither `succeeded_row_names` nor
`failed_row_names`, so the summary block alone ("Rows succeeded: 0/1", exit
0) was byte-identical to the shape of the exit-code defect
`test_publish_skipped_row_not_counted_succeeded.py` pins (a gate-declined row
silently miscounted as neither processed nor flagged). This test drives
`main()`'s REAL per-row loop — same harness shape as that sibling file — with
`delta_row_unchanged` faked to model a proven-unchanged row, and asserts the
summary line now names the skip explicitly and the run still exits 0.

Run: python -m pytest coordinator/bin/tests/test_publish_delta_skip_row_summary_honesty.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_git_repo(root: Path) -> None:
    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "publish-delta-skip-honesty-test@claude-klabauter.test")
    _git("config", "user.name", "Publish Delta Skip Honesty Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_delta_skip_honesty_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_NAMES = ["row-a", "row-b", "row-c"]
# row-b models a row `delta_row_unchanged` proves unchanged since its last
# recorded `--delta` publish — the whole-row skip path (main()'s row loop,
# just above `process_target` dispatch), never reached by `process_target`.
_SKIPPED_ROW = "row-b"


def _wire_common_fakes(monkeypatch, tmp_path, *, rows_reached: list):
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
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
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
    monkeypatch.setattr(
        publish, "compute_delta_invalidation_signature", lambda store_path, engine_ctx: "fixed-sig"
    )
    monkeypatch.setattr(
        publish,
        "delta_row_unchanged",
        lambda setup_dir, target, signature, round_pinned_shas: target.name == _SKIPPED_ROW,
    )

    def fake_process_target(target, setup_dir, totals, **kwargs):
        # Must never be reached for `_SKIPPED_ROW` — the delta whole-row skip
        # happens in `main()`'s loop before `process_target` dispatch.
        assert target.name != _SKIPPED_ROW
        rows_reached.append(target.name)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)


def test_delta_skipped_row_reported_separately_and_not_as_succeeded(monkeypatch, tmp_path, capsys):
    """The fix this pins: a `--delta` whole-row skip must not collapse the
    summary block into the same "Rows succeeded: N/M", exit-0 shape the
    now-fixed `coordinator-publish` exit-code defect had. The skip must be
    named in the summary line itself, distinct from both succeeded and
    failed."""
    rows_reached: list = []
    _wire_common_fakes(monkeypatch, tmp_path, rows_reached=rows_reached)

    rc = publish.main([",".join(_ROW_NAMES), "--delta"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rows_reached == ["row-a", "row-c"]
    assert "--delta: unchanged since last publish" in combined
    summary_line = combined.split("Rows succeeded:")[1].split("\n")[0]
    assert "2/3" in summary_line
    assert "1 skipped, unchanged" in summary_line
    # The skip must never be counted toward "Rows FAILED" either.
    assert "Rows FAILED" not in combined
    # A skip is not a failure: the run still exits clean.
    assert rc == 0

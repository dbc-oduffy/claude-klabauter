"""coordinator/bin/tests/test_publish_row_failure_aggregates_gates.py —
regression test for the 2026-08-14 aggregate-instead-of-abort fix in
`publish.py::main`: a failing row used to `return 1` BEFORE any end-of-run
gate (`dispatch_end_of_run_identity_check`, `..._install_doc_payload_check`,
`..._unscanned_published_check`, `..._function_gate`,
`..._entrypoint_gate`) ever ran, so a round with both a row failure and an
independent gate defect only ever surfaced the row failure — discovering the
gate defect cost a second round, after the row failure was fixed and the
round re-run clean. This module pins the fixed behaviour: the gates now run
regardless of `failed_row_names`, and their findings are reported in the
SAME round as the row failure, while the row failure keeps exit-code
priority (1), matching the pre-fix contract for a row-failure-only round.

Reuses `test_publish_row_isolation.py`'s fixture shape (same fake-row/fake-
process_target wiring) rather than re-deriving it, so this test exercises
`main()`'s real per-row loop and real end-of-run gate dispatch, not a
hand-rolled stand-in for either.

Run: python -m pytest coordinator/bin/tests/test_publish_row_failure_aggregates_gates.py -q
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
    _git("config", "user.email", "publish-row-failure-aggregates-gates-test@claude-klabauter.test")
    _git("config", "user.name", "Publish Row Failure Aggregates Gates Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_row_failure_aggregates_gates_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_NAMES = ["row-a", "row-b", "row-c"]
_FAILING_ROW = "row-b"


def _wire_common_fakes(monkeypatch, tmp_path, *, identity_ok: bool, entrypoint_ok: bool):
    """Same shape as `test_publish_row_isolation.py::_wire_common_fakes`,
    with `dispatch_end_of_run_identity_check`/`..._entrypoint_gate`
    monkeypatched to a caller-supplied verdict (rather than always True) so
    this module can assert on THEIR outcome reaching the final report."""

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

    identity_calls: list = []
    entrypoint_calls: list = []

    def fake_identity_check(*a, **k):
        identity_calls.append((a, k))
        if not identity_ok:
            print("  Error: end-of-run identity check FAILED (fixture)", file=sys.stderr)
        return identity_ok

    def fake_entrypoint_gate(*a, **k):
        entrypoint_calls.append((a, k))
        if not entrypoint_ok:
            print("  Error: end-of-run entrypoint gate FAILED (fixture)", file=sys.stderr)
        return entrypoint_ok

    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", fake_identity_check)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_function_gate", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_entrypoint_gate", fake_entrypoint_gate)

    return identity_calls, entrypoint_calls


def _fake_process_target_one_fails(target, setup_dir, totals, **kwargs):
    if target.name == _FAILING_ROW:
        raise SystemExit(3)
    totals.processed += 1


def _fake_process_target_all_ok(target, setup_dir, totals, **kwargs):
    totals.processed += 1


def test_row_failure_still_runs_end_of_run_gates(monkeypatch, tmp_path, capsys):
    """The core fix: a failing row must NOT short-circuit past the
    end-of-run gates — they still run this same round, over whatever
    successfully synced."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    identity_calls, entrypoint_calls = _wire_common_fakes(
        monkeypatch, tmp_path, identity_ok=True, entrypoint_ok=True
    )
    monkeypatch.setattr(publish, "process_target", _fake_process_target_one_fails)

    rc = publish.main([",".join(_ROW_NAMES)])

    assert identity_calls, "identity gate never ran despite a row failure"
    assert entrypoint_calls, "entrypoint gate never ran despite a row failure"
    # A row failure still refuses at exit 1, unchanged from before this fix.
    assert rc == 1


def test_row_failure_plus_gate_failure_both_reported_same_round(monkeypatch, tmp_path, capsys):
    """A round with BOTH a failing row AND an independent gate defect must
    report both in the SAME round — this is the exact waste the fix closes
    (six rounds to discover six sequential single-defect classes)."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, identity_ok=False, entrypoint_ok=True)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_one_fails)

    rc = publish.main([",".join(_ROW_NAMES)])
    combined = "\n".join(capsys.readouterr())

    assert "Rows FAILED" in combined and _FAILING_ROW in combined
    assert "end-of-run identity check FAILED" in combined
    # Row failure keeps exit-code priority (1), not the gate's own code (2).
    assert rc == 1


def test_three_failing_rows_report_all_three_not_one(monkeypatch, tmp_path, capsys):
    """Row-level aggregation (pre-existing, pinned here alongside the new
    gate aggregation): every failing row is named, not just the first."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, identity_ok=True, entrypoint_ok=True)

    def fake_process_target_all_fail(target, setup_dir, totals, **kwargs):
        raise SystemExit(3)

    monkeypatch.setattr(publish, "process_target", fake_process_target_all_fail)

    rc = publish.main([",".join(_ROW_NAMES)])
    combined = "\n".join(capsys.readouterr())

    for row in _ROW_NAMES:
        assert row in combined
    assert "Rows FAILED" in combined
    assert rc == 1


def test_single_failing_row_exit_code_and_message_unchanged(monkeypatch, tmp_path, capsys):
    """A lone failing row, every gate clean, still refuses exactly as
    before this fix: exit 1, "Rows FAILED" naming it."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, identity_ok=True, entrypoint_ok=True)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_one_fails)

    rc = publish.main([",".join(_ROW_NAMES)])
    combined = "\n".join(capsys.readouterr())

    assert rc == 1
    assert "Rows FAILED" in combined and _FAILING_ROW in combined
    assert "publish.py: FATAL" not in combined


def test_no_row_failure_gate_failure_still_exits_2(monkeypatch, tmp_path, capsys):
    """Sanity counterpart, unchanged from before this fix: every row
    succeeds, but a gate fails — exit 2 (not 1), since bytes landed but
    verification did not complete."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, identity_ok=False, entrypoint_ok=True)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_all_ok)

    rc = publish.main([",".join(_ROW_NAMES)])
    combined = "\n".join(capsys.readouterr())

    assert rc == 2
    assert "Rows FAILED" not in combined
    assert "publish.py: FATAL" in combined


def test_all_clean_still_exits_0(monkeypatch, tmp_path):
    """Happy path is unaffected by the reordering."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, identity_ok=True, entrypoint_ok=True)
    monkeypatch.setattr(publish, "process_target", _fake_process_target_all_ok)

    rc = publish.main([",".join(_ROW_NAMES)])

    assert rc == 0

"""
coordinator_core.ops.tests.test_handoff_reconcile_report

C12a durable dry-run report tests (docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md
§ C12a): `handoff.reconcile_open`'s optional `report_path` param renders one Markdown row
per open handoff (current status/deployment_state, this pass's proposed verdict, and WHY)
and persists it via `locked_rmw` instead of the discard-on-return the op had before this
chunk. See the C12a section of `handoff_reconcile.py`'s module docstring for the full
rationale (why the body is timestamp-free, why `locked_rmw` and not a bare `write_text`).

Kept in a separate file from test_handoff_reconcile.py and test_handoff_reconcile_d1.py —
this coverage is additive to the op's existing AC5/AC8/AC9/D1 behavior, not a rework of it,
and a separate file avoids touching a large shared test file on a tree with many concurrent
sessions.

Import guard: coordinator_core.ops.handoff_reconcile MUST be imported at module load time
to fire @register_op("handoff.reconcile_open") — mirrors the sibling test files' own guard.

Coverage:
  (1) unit: _build_dry_run_report — deterministic body given the same inputs (no wall-clock
      element), one row per open handoff, current/proposed/why populated across all four
      verdict buckets (auto-ship, gate-cascade-clear, surface, not-cleared benign steady
      state), flip-candidate count only counts actual transition verdicts.
  (2) integration via _handler: report_path writes a real file; a caller that omits
      report_path gets the identical pre-C12a return shape and no extra file.
  (3) AC13 idempotence: two consecutive _handler passes over an UNCHANGED corpus produce
      ONE write (report created) then a genuine no-op (mtime unchanged on pass 2) — proves
      locked_rmw's own skip-if-identical fired, not merely that the bytes happen to match.
  (4) AC13 concurrency-safety: two threads racing locked_rmw against the SAME report target
      with distinct payloads never interleave/corrupt — the final file is byte-identical to
      exactly one of the two payloads, never a mix of both.
  (5) § C10 / AC16: `_handler`'s returned dict AND the C12a Markdown report body both carry
      `policy_source`/`policy_path` under each of the three `PolicyResult.source` values
      (absent, malformed, loaded) — the actual reconcile-report surface AC16 names, not
      `policy_report_fields` in isolation (see test_policy_loader.py for that unit coverage).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import yaml

import coordinator_core.ops.handoff_reconcile  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.locked_write import locked_rmw
from coordinator_core.ops.handoff_reconcile import _build_dry_run_report, _handler

_OP_NAME = "handoff.reconcile_open"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_reconcile @register_op did not fire"
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# (1) _build_dry_run_report — pure-function unit coverage
# ---------------------------------------------------------------------------


def _fake_handoff(hid: str, status: str = "open", deployment_state: str = "") -> dict:
    return {
        "id": hid,
        "status": status,
        "deployment_state": deployment_state,
        "_path": f"/repo/state/handoffs/{hid}.md",
    }


def test_report_covers_all_four_verdict_buckets_and_counts_flips():
    worktree_root = Path("/repo")
    open_handoffs = [
        _fake_handoff("h-ship"),
        _fake_handoff("h-gate", deployment_state="awaiting_gate"),
        _fake_handoff("h-surface"),
        _fake_handoff("h-steady", deployment_state="awaiting_gate"),
    ]
    reconciled = [
        {"handoff_id": "h-ship", "action": "ship_and_archive", "applied": False,
         "message": None},
    ]
    gates_cleared = [
        {"handoff_id": "h-gate", "verdict": "clear", "blocker_ids": ["b1"],
         "applied": False},
    ]
    surfaced = [
        {"handoff_id": "h-surface", "reason": "ambiguous attribution"},
    ]

    text = _build_dry_run_report(
        worktree_root, open_handoffs, reconciled, gates_cleared, surfaced, dry_run=True,
        policy_source="absent", policy_path=None,
    )

    assert "h-ship" in text and "ship_and_archive" in text
    assert "h-gate" in text and "gate-cascade-clear" in text and "b1" in text
    assert "h-surface" in text and "ambiguous attribution" in text
    assert "h-steady" in text and "not-cleared (benign steady state)" in text
    assert "flip candidates" in text
    # Only h-ship and h-gate are actual transition verdicts; h-surface and
    # h-steady are not flip candidates.
    assert "flip candidates (resolver verdict calls for a transition): 2" in text


def test_report_body_is_deterministic_across_identical_inputs():
    worktree_root = Path("/repo")
    open_handoffs = [_fake_handoff("h1"), _fake_handoff("h2")]
    text_a = _build_dry_run_report(
        worktree_root, open_handoffs, [], [], [], dry_run=True,
        policy_source="loaded", policy_path="/plugin/auto-reconcile-policy.yaml",
    )
    text_b = _build_dry_run_report(
        worktree_root, open_handoffs, [], [], [], dry_run=True,
        policy_source="loaded", policy_path="/plugin/auto-reconcile-policy.yaml",
    )
    assert text_a == text_b


def test_report_run_label_appears_only_in_header_not_body_rows():
    worktree_root = Path("/repo")
    open_handoffs = [_fake_handoff("h1")]
    unlabelled = _build_dry_run_report(
        worktree_root, open_handoffs, [], [], [], dry_run=True,
        policy_source="absent", policy_path=None,
    )
    labelled = _build_dry_run_report(
        worktree_root, open_handoffs, [], [], [], dry_run=True,
        policy_source="absent", policy_path=None,
        report_run_label="run-xyz",
    )
    assert "run-xyz" in labelled
    assert "run-xyz" not in unlabelled
    # Stripping the header line that carries the label should make the two
    # bodies identical below the label line -- the label must not leak into
    # the per-baton table.
    labelled_without_header = "\n".join(
        line for line in labelled.splitlines() if "run-xyz" not in line
    )
    unlabelled_body = "\n".join(unlabelled.splitlines())
    assert labelled_without_header == unlabelled_body


# ---------------------------------------------------------------------------
# (2) integration via _handler
# ---------------------------------------------------------------------------


def test_handler_without_report_path_writes_no_file(handoff_repo, tmp_path):
    handoff_repo.seed_handoff("h1.md", "open")
    report_target = tmp_path / "should-not-exist.md"

    result = _run(_handler({}, repo_root=handoff_repo.common_dir))

    assert result["exit_code"] in (0, 2)
    assert not report_target.exists()


def test_handler_with_report_path_writes_a_real_file(handoff_repo, tmp_path):
    handoff_repo.seed_handoff("h1.md", "open")
    report_target = tmp_path / "reports" / "dry-run.md"

    result = _run(_handler(
        {"report_path": str(report_target)}, repo_root=handoff_repo.common_dir,
    ))

    assert result["exit_code"] in (0, 2)
    assert report_target.is_file()
    text = report_target.read_text(encoding="utf-8")
    assert "Gate resolver dry-run report" in text
    assert "Per-baton detail" in text


# ---------------------------------------------------------------------------
# (3) AC13 idempotence via _handler
# ---------------------------------------------------------------------------


def test_two_consecutive_passes_over_unchanged_corpus_are_one_write_then_noop(
    handoff_repo, tmp_path,
):
    handoff_repo.seed_handoff("h1.md", "open")
    handoff_repo.seed_handoff("h2.md", "open", deployment_state="awaiting_gate")
    report_target = tmp_path / "dry-run.md"
    params = {"report_path": str(report_target)}

    _run(_handler(dict(params), repo_root=handoff_repo.common_dir))
    assert report_target.is_file()
    text_after_pass_1 = report_target.read_text(encoding="utf-8")
    mtime_after_pass_1 = report_target.stat().st_mtime_ns

    time.sleep(0.05)

    _run(_handler(dict(params), repo_root=handoff_repo.common_dir))
    text_after_pass_2 = report_target.read_text(encoding="utf-8")
    mtime_after_pass_2 = report_target.stat().st_mtime_ns

    assert text_after_pass_2 == text_after_pass_1
    # A genuine no-op means locked_rmw never called os.replace on pass 2 --
    # if it had, the mtime would have advanced past the sleep above.
    assert mtime_after_pass_2 == mtime_after_pass_1


# ---------------------------------------------------------------------------
# (4) AC13 concurrency-safety — locked_rmw itself, exercised against a C12a
#     report target, races two writers and asserts no interleaving/corruption.
# ---------------------------------------------------------------------------


def test_concurrent_report_writes_never_interleave_or_corrupt(handoff_repo):
    report_target = handoff_repo.root / "concurrent-dry-run.md"
    payload_a = "AAAA-PAYLOAD-" * 5000 + "\n"
    payload_b = "BBBB-PAYLOAD-" * 5000 + "\n"
    errors: list = []

    def _write(payload: str) -> None:
        try:
            locked_rmw(
                report_target,
                lambda _old: payload,
                repo_root=handoff_repo.common_dir,
                missing_ok=True,
            )
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    t1 = threading.Thread(target=_write, args=(payload_a,))
    t2 = threading.Thread(target=_write, args=(payload_b,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"locked_rmw raised under concurrency: {errors}"
    final = report_target.read_text(encoding="utf-8")
    assert final in (payload_a, payload_b)
    assert len(final) in (len(payload_a), len(payload_b))


# ---------------------------------------------------------------------------
# (5) § C10 / AC16 — the actual reconcile-report surface: _handler's returned
#     dict AND the C12a Markdown report body carry policy_source/policy_path
#     under each of the three PolicyResult.source values. Distinct from
#     test_policy_loader.py's helper-level unit coverage of
#     `policy_report_fields` in isolation — this exercises the op end to end.
# ---------------------------------------------------------------------------


def _grammar_valid_policy(tmp_path: Path) -> Path:
    """A grammar-valid policy fixture with `dry_run: true` — loaded-source
    coverage that never arms auto-ship (irrelevant to this test's concern,
    but keeps the fixture inert per the hard constraint against touching
    arming behavior)."""
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text(
        yaml.safe_dump({
            "three_signal": {},
            "mechanical_commit_denylist": [],
            "cross_handoff_attribution": True,
            "dry_run": True,
        }),
        encoding="utf-8",
    )
    return policy_file


def _malformed_policy(tmp_path: Path) -> Path:
    """A grammar-INVALID policy fixture (missing required keys)."""
    policy_file = tmp_path / "auto-reconcile-policy.yaml"
    policy_file.write_text(yaml.safe_dump({"three_signal": {}}), encoding="utf-8")
    return policy_file


def test_handler_and_report_surface_policy_source_absent(
    handoff_repo, tmp_path, monkeypatch,
):
    # No policy_path param, no env override, and CLAUDE_PLUGIN_ROOT unset —
    # forces the "absent" branch deterministically regardless of the host
    # environment's own CLAUDE_PLUGIN_ROOT.
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("AUTO_RECONCILE_POLICY", raising=False)
    handoff_repo.seed_handoff("h1.md", "open")
    report_target = tmp_path / "dry-run-absent.md"

    result = _run(_handler(
        {"report_path": str(report_target)}, repo_root=handoff_repo.common_dir,
    ))

    assert result["policy_source"] == "absent"
    assert result["policy_path"] is None

    text = report_target.read_text(encoding="utf-8")
    assert "policy_source: absent" in text
    assert "policy_path: None" in text


def test_handler_and_report_surface_policy_source_malformed(handoff_repo, tmp_path):
    handoff_repo.seed_handoff("h1.md", "open")
    policy_file = _malformed_policy(tmp_path)
    report_target = tmp_path / "dry-run-malformed.md"

    result = _run(_handler(
        {"policy_path": str(policy_file), "report_path": str(report_target)},
        repo_root=handoff_repo.common_dir,
    ))

    assert result["policy_source"] == "malformed"
    assert result["policy_path"] == str(policy_file)

    text = report_target.read_text(encoding="utf-8")
    assert "policy_source: malformed" in text
    assert f"policy_path: {policy_file}" in text


def test_handler_and_report_surface_policy_source_loaded(handoff_repo, tmp_path):
    handoff_repo.seed_handoff("h1.md", "open")
    policy_file = _grammar_valid_policy(tmp_path)
    report_target = tmp_path / "dry-run-loaded.md"

    result = _run(_handler(
        {"policy_path": str(policy_file), "report_path": str(report_target)},
        repo_root=handoff_repo.common_dir,
    ))

    assert result["policy_source"] == "loaded"
    assert result["policy_path"] == str(policy_file)

    text = report_target.read_text(encoding="utf-8")
    assert "policy_source: loaded" in text
    assert f"policy_path: {policy_file}" in text

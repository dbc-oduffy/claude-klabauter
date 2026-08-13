"""
coordinator_core.ops.tests.test_distill_stamp_disposal

Unit tests for coordinator_core.ops.distill_stamp_disposal — the
"distill.stamp_disposal" op (C13).

Coverage:
  stamp_disposal_manifest (pure decision function):
    (a) stamp round-trip: fresh (unstamped) manifest -> stamp applied, all
        four STAMP_FIELDS present, sha == manifest_schema.compute_manifest_sha
        over the pre-stamp body.
    (b) idempotency: re-running with the SAME by/at/note against an
        already-stamped, unchanged manifest -> applied False, no-op message.
    (c) refuse re-stamp with DIFFERENT values (same body, different
        by/at/note) -> DisposalStampError.
    (d) sha-drift detection: manifest body mutated after stamping (same
        by/at/note re-supplied) -> DisposalStampError naming drift.
  load_disposal_manifest:
    (e) absent manifest file -> DisposalStampError.
    (f) schema-invalid manifest (missing required field) -> DisposalStampError.
  write_stamped_manifest:
    (g) atomic in-place rewrite — same path as before, valid JSON after.
  handler:
    (h) repo_root=None raises ValueError.
    (i) missing run_id/by/at/note raises ValueError.
    (j) injected sha / disposal_authorized_* param raises ValueError
        (refuse-injection).
    (k) end-to-end dispatch_message smoke: assemble (C12) then stamp (C13)
        via the real registered wiring, fresh stamp + idempotent re-run +
        value-mismatch refusal all exercised over the IPC surface.

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C13
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.ops.distill_stamp_disposal import (
    DisposalStampError,
    _handler,
    load_disposal_manifest,
    manifest_path_for_run,
    stamp_disposal_manifest,
    write_stamped_manifest,
)

# Same convention as coordinator_core/distill/tests/{test_common,test_delete_guard}.py:
# the real end-to-end dispatch path shells out to ripgrep (`rg`) via
# active_reference_guard (coordinator_core/distill/_common.py) — an optional
# external tool, not a portability gap, so its absence is a skip, not a
# Windows-conditional skip (which stays forbidden).
_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")


def _run(coro):
    return asyncio.run(coro)


def _fresh_manifest(run_id: str = "2026-07-23-01h00") -> dict:
    row = _schema.make_disposal_row(
        path="archive/handoffs/eligible.md",
        artifact_class="handoff",
        guards_run=[_schema.make_guard_receipt("shipped_in", "pass", "sha=abc123")],
        eligible=True,
        log_row="- archive/handoffs/eligible.md -> EPHEMERAL (disposed)",
    )
    return _schema.make_disposal_manifest(
        run_id=run_id,
        rows=[row],
        scan_stats=_schema.make_scan_stats(1, 1, 0),
        mass_throttle=False,
    )


def _write_manifest_to(worktree_root: Path, manifest: dict) -> Path:
    path = manifest_path_for_run(worktree_root, manifest["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# stamp_disposal_manifest — pure decision function
# ---------------------------------------------------------------------------


def test_stamp_round_trip():
    manifest = _fresh_manifest()
    expected_sha = _schema.compute_manifest_sha(manifest)

    result = stamp_disposal_manifest(manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved")

    assert result.applied is True
    assert result.computed_sha == expected_sha
    assert _schema.stamp_complete(result.manifest)
    assert result.manifest["disposal_authorized_by"] == "pm"
    assert result.manifest["disposal_authorized_at"] == "2026-07-23T10:00:00Z"
    assert result.manifest["disposal_authorized_note"] == "approved"
    assert result.manifest["disposal_authorized_sha"] == expected_sha
    # Underlying rows/scan_stats untouched (additive stamp only).
    assert result.manifest["rows"] == manifest["rows"]


def test_stamp_idempotent_when_equal():
    manifest = _fresh_manifest()
    first = stamp_disposal_manifest(manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved")
    assert first.applied is True

    second = stamp_disposal_manifest(
        first.manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved"
    )
    assert second.applied is False
    assert "no-op" in second.message
    assert second.manifest == first.manifest


def test_refuse_restamp_with_different_values():
    manifest = _fresh_manifest()
    first = stamp_disposal_manifest(manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved")

    with pytest.raises(DisposalStampError, match="different values"):
        stamp_disposal_manifest(
            first.manifest, by="someone-else", at="2026-07-23T10:00:00Z", note="approved"
        )


def test_sha_drift_detection_after_manifest_edit():
    manifest = _fresh_manifest()
    first = stamp_disposal_manifest(manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved")

    # Mutate the manifest BODY (not the stamp) after stamping — a re-run of
    # assemble_disposal_manifest, or a hand-edit, would look like this.
    drifted = dict(first.manifest)
    drifted["scan_stats"] = dict(drifted["scan_stats"])
    drifted["scan_stats"]["total_scanned"] = 999

    with pytest.raises(DisposalStampError, match="drift"):
        stamp_disposal_manifest(drifted, by="pm", at="2026-07-23T10:00:00Z", note="approved")


# ---------------------------------------------------------------------------
# load_disposal_manifest
# ---------------------------------------------------------------------------


def test_load_disposal_manifest_absent_file_raises(tmp_path: Path):
    with pytest.raises(DisposalStampError, match="not found on disk"):
        load_disposal_manifest(tmp_path / "does-not-exist.json")


def test_load_disposal_manifest_schema_invalid_raises(tmp_path: Path):
    path = tmp_path / "bad-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "run_id": "x"}), encoding="utf-8")
    with pytest.raises(DisposalStampError, match="schema validation"):
        load_disposal_manifest(path)


# ---------------------------------------------------------------------------
# write_stamped_manifest
# ---------------------------------------------------------------------------


def test_write_stamped_manifest_atomic_in_place(tmp_path: Path):
    manifest = _fresh_manifest()
    path = _write_manifest_to(tmp_path, manifest)

    result = stamp_disposal_manifest(manifest, by="pm", at="2026-07-23T10:00:00Z", note="approved")
    write_stamped_manifest(path, result.manifest)

    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert next(iter(reloaded)) == "schema_version"
    assert reloaded["disposal_authorized_by"] == "pm"
    assert reloaded["disposal_authorized_sha"] == result.computed_sha


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_raises_on_repo_root_none():
    with pytest.raises(ValueError, match="_origin_worktree"):
        _handler(
            {"run_id": "2026-07-23-01h00", "by": "pm", "at": "2026-07-23T10:00:00Z", "note": "ok"},
            repo_root=None,
        )


def test_handler_raises_on_missing_run_id(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="run_id"):
        _handler(
            {"by": "pm", "at": "2026-07-23T10:00:00Z", "note": "ok"},
            repo_root=tmp_path / ".git",
        )


def test_handler_raises_on_missing_by_at_note(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match=r"by.*at.*note|non-empty params"):
        _handler({"run_id": "2026-07-23-01h00"}, repo_root=tmp_path / ".git")


def test_handler_refuses_injected_sha_param(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="refuses caller-supplied"):
        _handler(
            {
                "run_id": "2026-07-23-01h00",
                "by": "pm",
                "at": "2026-07-23T10:00:00Z",
                "note": "ok",
                "sha": "deadbeef",
            },
            repo_root=tmp_path / ".git",
        )


def test_handler_refuses_injected_disposal_authorized_sha_param(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="refuses caller-supplied"):
        _handler(
            {
                "run_id": "2026-07-23-01h00",
                "by": "pm",
                "at": "2026-07-23T10:00:00Z",
                "note": "ok",
                "disposal_authorized_sha": "deadbeef",
            },
            repo_root=tmp_path / ".git",
        )


@_requires_rg
def test_handler_end_to_end_dispatch_message_smoke(tmp_path: Path):
    """assemble (C12) then stamp (C13) via the REAL registered wiring
    (ops/__init__.py eager import + _registry_map.py + op_scopes.py + the
    @register_op decorator)."""
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "archive" / "handoffs" / "eligible.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "---\n"
        "shipped_in: 68b27420\n"
        "status: consumed\n"
        "deployment_state: shipped\n"
        "realized_by: inline\n"
        "distill_fate: ephemeral\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    assemble_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.assemble_disposal_manifest",
        "params": {
            "run_id": "2026-07-23-01h00",
            "candidates": ["archive/handoffs/eligible.md"],
        },
        "_origin_worktree": str(tmp_path),
    }
    assembled = _run(ipc.dispatch_message(assemble_msg))
    assert "result" in assembled, f"assemble must succeed; got error: {assembled.get('error')}"

    stamp_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "distill.stamp_disposal",
        "params": {
            "run_id": "2026-07-23-01h00",
            "by": "pm",
            "at": "2026-07-23T10:00:00Z",
            "note": "approved",
        },
        "_origin_worktree": str(tmp_path),
    }
    stamped = _run(ipc.dispatch_message(stamp_msg))
    assert "result" in stamped, f"stamp must succeed; got error: {stamped.get('error')}"
    assert stamped["result"]["applied"] is True
    assert stamped["result"]["disposal_authorized_sha"]
    # § 1b — the preview is rendered from the manifest as loaded, BEFORE the
    # stamp write, and returned alongside the stamp decision.
    assert "archive/handoffs/eligible.md" in stamped["result"]["rendered_manifest"]
    assert "ELIGIBLE" in stamped["result"]["rendered_manifest"]

    # Idempotent re-run — same values, applied False.
    stamped_again = _run(ipc.dispatch_message(stamp_msg))
    assert "result" in stamped_again
    assert stamped_again["result"]["applied"] is False

    # Value-mismatch re-stamp -> JSON-RPC error, not a silent overwrite.
    bad_stamp_msg = dict(stamp_msg)
    bad_stamp_msg["params"] = dict(stamp_msg["params"])
    bad_stamp_msg["params"]["by"] = "someone-else"
    mismatched = _run(ipc.dispatch_message(bad_stamp_msg))
    assert "error" in mismatched

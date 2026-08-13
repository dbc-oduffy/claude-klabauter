"""Regression coverage for unconditional emit() — no producer-side hold/sentinel.

The runtime emit-hold (``cockpit-revendor-pending-*`` sentinel -> ``EmitHeldError``) was
removed entirely; reader-first forward-compatibility is now a consumer (example-retrieval-repo)
responsibility, not a producer-side gate. This file retains the default-path write
regression proof and the contract-declared backlog_history integration proof, both now
exercised WITHOUT any sentinel involved.

Spec backlink: pln-producer-emit-hold-removal-rea-48bd64 § C1
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.emit import validate
from coordinator_core.ops.emit.envelope import emit
from coordinator_core.ops.emit.context import EmitContext


# ---------------------------------------------------------------------------
# Minimal EmitContext factory
# ---------------------------------------------------------------------------

def _make_ctx(central_state_root: Path, repo_root: Path) -> EmitContext:
    """Return a minimal EmitContext with the given state roots.

    Volatile fields (git_sha, observed_at, hostname) are non-load-bearing for these
    write-path tests — only central_state_root and repo_root are exercised here.
    """
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=central_state_root,
        git_branch="test-branch",
        git_sha="0000000000000000000000000000000000000000",
        git_sha_short="00000000",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


class TestDefaultPathEmitWritesArtifact:
    """Baseline: default-path emit() (no ``out``) writes cockpit-emission.json unconditionally."""

    def test_emit_writes_canonical_output(self, tmp_path: Path) -> None:
        """emit(ctx) with no out override writes the canonical artifact and returns ok: True."""
        canonical = tmp_path / "cockpit-emission.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        result = emit(ctx)

        assert result["ok"] is True
        assert canonical.exists(), "cockpit-emission.json must be written to central_state_root"
        assert canonical.stat().st_size > 0

        data = json.loads(canonical.read_text())
        assert "schema_version" in data


class TestContractDeclaredBacklogHistoryEmitSucceeds:
    """Integration proof: contract-declared backlog_history populates series on the write path.

    Regression guard for the backlog_history gate (docs/plans/2026-07-05-backlog-history-emit-gate-decouple.md)
    now exercised with no sentinel in the picture — emit() has no hold to bypass.
    """

    def test_contract_declared_emit_succeeds_with_real_series(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """contract_declares_backlog_history() True -> emit() proceeds with real series data."""
        monkeypatch.setattr(validate, "contract_declares_backlog_history", lambda: True)
        shard = tmp_path / "backlog-snapshots.test-machine.jsonl"
        shard.write_text(
            '{"repo":"owner/repo","date":"2026-07-05","bug":3,"improvement":2,"lessons":1}\n',
            encoding="utf-8",
        )
        out_file = tmp_path / "out.json"
        ctx = _make_ctx(central_state_root=tmp_path, repo_root=tmp_path)

        result = emit(ctx, out=out_file)
        assert result["ok"] is True

        data = json.loads(out_file.read_text())
        assert len(data["backlog_history"]["series"]) > 0, (
            "backlog_history.series must be non-empty when contract declares the block "
            "and shards are present"
        )

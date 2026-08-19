"""
coordinator_core.ops.session.tests.test_boot_sweep_phase_spans

Tests for C5 (docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-
on-every-commit.md § C5): session.boot_sweep's per-family cost attribution
via `op_latency.record_composition_span`, emitted at boot_sweep's existing
phase boundaries.

Coverage:
  - An empty-repo boot_sweep run still writes one `kind="composition"` row
    per family — consumed_handoffs, stranded_supersede, terminal_plans,
    shipped_handoffs, actioned_memos — each named `"boot_sweep.<family>"`,
    each carrying a non-negative elapsed_secs and an invocation_count.
  - `session.sweep_consumed_handoffs` (the standalone op that calls
    `boot_sweep._sweep_consumed_handoffs` directly) emits the SAME
    `boot_sweep.consumed_handoffs` span name — the shared-leg premise this
    chunk's body requires ("this chunk's spans cover both call paths").
  - A per-item failure surfaces as `outcome: "partial_mutation"` on that
    family's row, not `"success"`.

Spec backlinks:
  - docs/plans/2026-08-19-the-engine-stops-paying-a-network-push-on-every-
    commit.md § C5 (AC5)
  - coordinator_core/ops/session/boot_sweep.py (_emit_family_span,
    _sweep_consumed_handoffs, _handler)
  - coordinator_core/telemetry/op_latency.py :: record_composition_span

Negative-spec:
  - Does NOT assert against the real repo's on-disk sink — routes through a
    tmp git-common-dir stand-in, mirroring
    coordinator_core/tests/test_composition_budget_fleet_default.py's `_sink`
    fixture.
  - Does NOT re-test boot_sweep's own archival correctness (result envelope
    shape, per-item disposition) — that is test_boot_sweep.py's remit; this
    file asserts only the span rows the archival machinery now also emits.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 — fires @register_op

from coordinator_core.ops.session.boot_sweep import _handler

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FAMILY_NAMES = (
    "boot_sweep.consumed_handoffs",
    "boot_sweep.stranded_supersede",
    "boot_sweep.terminal_plans",
    "boot_sweep.shipped_handoffs",
    "boot_sweep.actioned_memos",
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def boot_repo(tmp_path) -> Path:
    """Minimal git repo with the directory skeleton boot_sweep's four
    archival families need — mirrors test_boot_sweep.py's own `boot_repo`
    fixture (kept self-contained here rather than importing across test
    files, per that file's own module docstring negative-spec on coupling).
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "boot-sweep-span-test@claude-klabauter.test")
    _git("config", "user.name", "Boot Sweep Span Test")
    _git("config", "commit.gpgsign", "false")

    dirs = [
        "docs/plans",
        "state/handoffs",
        "archive/specs",
        "archive/handoffs",
        "cross-repo/inbox",
        "cross-repo/archive",
    ]
    for d in dirs:
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=str(repo_root),
        capture_output=True,
        check=True,
    )
    common_dir = Path(result.stdout.decode().strip()).resolve()
    return common_dir


def _sink_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _read_rows(sink_path: Path) -> list[dict]:
    if not sink_path.exists():
        return []
    with open(sink_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _composition_rows(sink_path: Path) -> list[dict]:
    return [r for r in _read_rows(sink_path) if r.get("kind") == "composition"]


class TestBootSweepPerFamilySpans:
    def test_empty_repo_emits_one_span_per_family(self, boot_repo):
        result = _run(_handler({}, repo_root=boot_repo))
        assert result["exit_code"] == 0

        rows = _composition_rows(_sink_path(boot_repo))
        names = [r["name"] for r in rows]

        for family in _FAMILY_NAMES:
            assert family in names, (
                f"expected a {family!r} composition row; got names={names!r}"
            )

    def test_each_span_row_carries_elapsed_and_invocation_count(self, boot_repo):
        _run(_handler({}, repo_root=boot_repo))

        rows = _composition_rows(_sink_path(boot_repo))
        by_name = {r["name"]: r for r in rows}

        for family in _FAMILY_NAMES:
            row = by_name[family]
            assert isinstance(row["elapsed_secs"], (int, float))
            assert row["elapsed_secs"] >= 0.0
            assert isinstance(row["invocation_count"], int)
            assert row["invocation_count"] >= 0
            assert row["outcome"] in ("success", "partial_mutation")
            assert isinstance(row["t_start"], (int, float))
            assert row["t_start"] > 0.0

    def test_empty_repo_families_report_success(self, boot_repo):
        _run(_handler({}, repo_root=boot_repo))

        rows = _composition_rows(_sink_path(boot_repo))
        by_name = {r["name"]: r for r in rows}

        for family in _FAMILY_NAMES:
            assert by_name[family]["outcome"] == "success"


class TestSweepConsumedHandoffsSharesTheLeg:
    """AC5 / chunk body: session.sweep_consumed_handoffs calls
    boot_sweep._sweep_consumed_handoffs DIRECTLY, so it must emit the SAME
    boot_sweep.consumed_handoffs span name this chunk instruments — the
    shared-leg premise, not a separate row.
    """

    def test_standalone_op_emits_the_same_family_span_name(self, boot_repo):
        from coordinator_core.ops.session.sweep_consumed_handoffs import (
            _handler as _sweep_handler,
        )

        _run(_sweep_handler({}, repo_root=boot_repo))

        rows = _composition_rows(_sink_path(boot_repo))
        names = [r["name"] for r in rows]
        assert "boot_sweep.consumed_handoffs" in names, (
            "session.sweep_consumed_handoffs must emit the same "
            f"boot_sweep.consumed_handoffs span name; got names={names!r}"
        )
        # stranded_supersede is part of the same shared leg (see
        # _sweep_consumed_handoffs' own C5 span accounting) — the standalone
        # op must not silently skip it either.
        assert "boot_sweep.stranded_supersede" in names

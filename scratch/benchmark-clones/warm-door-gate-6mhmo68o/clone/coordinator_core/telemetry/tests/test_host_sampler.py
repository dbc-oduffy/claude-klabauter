"""
coordinator_core.telemetry.tests.test_host_sampler -- coverage for
coordinator_core.telemetry.host_sampler.

Purpose: exercises the sink-write contract, disable kill switch, fail-open
behaviour, row shape, and the per-sample cost ratchet -- see that module's
own negative-spec and "Cadence arithmetic" for the guarantees under test.

Spec backlink: state/audits/2026-08-15-fleet-degradation-forensics.md
               state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.telemetry import host_sampler


def _init_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_sample_once_writes_one_row(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path)
    row = host_sampler.sample_once(repo_root=repo_root)
    assert row is not None

    sink = tmp_path / ".git" / "coordinator-sessions" / "logs" / "host-samples.jsonl"
    assert sink.exists()
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["ts"] == pytest.approx(row["ts"])


def test_row_shape_has_required_fields(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path)
    row = host_sampler.sample_once(repo_root=repo_root)
    assert row is not None
    for field in (
        "ts",
        "cpu_pct",
        "mem_used_mb",
        "mem_avail_mb",
        "mem_total_mb",
        "proc_count",
        "claude_proc_count",
        "python_proc_count",
        "sample_cost_ms",
    ):
        assert field in row


def test_disable_env_short_circuits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _init_repo(tmp_path)
    monkeypatch.setenv("COORDINATOR_HOST_SAMPLER_DISABLE", "1")
    row = host_sampler.sample_once(repo_root=repo_root)
    assert row is None

    sink = tmp_path / ".git" / "coordinator-sessions" / "logs" / "host-samples.jsonl"
    assert not sink.exists()


def test_none_repo_root_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No .git anywhere reachable from repo_root -- git_common_dir should
    # fail closed and sample_once must swallow it, never raise.
    monkeypatch.chdir(tmp_path)
    row = host_sampler.sample_once(repo_root=tmp_path)
    assert row is None


def test_multiple_samples_append_multiple_rows(tmp_path: Path) -> None:
    repo_root = _init_repo(tmp_path)
    host_sampler.sample_once(repo_root=repo_root)
    host_sampler.sample_once(repo_root=repo_root)
    host_sampler.sample_once(repo_root=repo_root)

    sink = tmp_path / ".git" / "coordinator-sessions" / "logs" / "host-samples.jsonl"
    lines = sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3


def test_sample_cost_ratchet(tmp_path: Path) -> None:
    """Enforcement half of the budget: one sample's own cost must stay under
    the module's high-water mark -- mirrors
    coordinator_core.tests.test_ipc_per_request_state
    .test_op_timeout_overrides_never_ratchet_upward's ratchet shape. A
    sample that cannot fit under its bound is a design that needs to shrink,
    not a bound that needs to grow.
    """
    repo_root = _init_repo(tmp_path)
    row = host_sampler.sample_once(repo_root=repo_root)
    assert row is not None
    assert row["sample_cost_ms"] <= host_sampler._MAX_SAMPLE_COST_MS, (
        f"host_sampler.sample_once cost {row['sample_cost_ms']}ms exceeds its "
        f"{host_sampler._MAX_SAMPLE_COST_MS}ms high-water mark -- shrink the "
        f"sampler, don't raise the ratchet."
    )


def test_sink_override_arg_skips_git_resolution(tmp_path: Path) -> None:
    """No ``.git`` anywhere near ``repo_root`` and no ``repo_root`` passed --
    the override must still work, writing exactly to the given path."""
    override = tmp_path / "override.jsonl"
    row = host_sampler.sample_once(sink_override=override)
    assert row is not None
    lines = override.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["ts"] == pytest.approx(row["ts"])


def test_sink_override_env_var_takes_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "env-override.jsonl"
    monkeypatch.setenv("COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE", str(override))
    row = host_sampler.sample_once()
    assert row is not None
    assert override.exists()
    lines = override.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_sink_override_arg_wins_over_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_override = tmp_path / "env-override.jsonl"
    arg_override = tmp_path / "arg-override.jsonl"
    monkeypatch.setenv("COORDINATOR_HOST_SAMPLER_SINK_OVERRIDE", str(env_override))
    row = host_sampler.sample_once(sink_override=arg_override)
    assert row is not None
    assert arg_override.exists()
    assert not env_override.exists()


def test_sample_once_fails_closed_on_collection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _init_repo(tmp_path)

    def _boom():
        raise RuntimeError("collection exploded")

    monkeypatch.setattr(host_sampler, "_collect_row", _boom)
    row = host_sampler.sample_once(repo_root=repo_root)
    assert row is None

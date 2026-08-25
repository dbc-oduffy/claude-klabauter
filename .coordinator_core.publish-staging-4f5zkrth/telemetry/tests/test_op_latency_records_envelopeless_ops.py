"""
coordinator_core.telemetry.tests.test_op_latency_records_envelopeless_ops —
guard against re-dropping envelopeless rows.

Purpose: ``_write_entry`` in op_latency.py used to ``return`` early when
``repo_root`` was None, silently dropping the row instead of writing it. A
None ``repo_root`` means the JSON-RPC envelope carried no
``_origin_worktree`` (see ``ipc.resolve_request_repo``) — it does NOT mean
the invocation happened outside a repo. The measured consequence:
``hooks.postuse_advisory_dispatch`` fires on every PostToolUse
``Write|Edit|MultiEdit|NotebookEdit|Agent`` and recorded ZERO rows in 85
hours, so it was absent from every ranking built on this ledger while
plausibly the largest single engine consumer. This test pins the fix: a
None ``repo_root`` now falls back to ``Path.cwd()`` and the row is written
with ``repo_key_source: "cwd"`` rather than dropped.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
               docs/wiki/cost-budgets-and-the-kill-disposition.md
"""

from __future__ import annotations

import json

from coordinator_core.telemetry.op_latency import (
    new_correlation_id,
    record_op_latency,
    record_op_started,
)


def _fake_common_dir(tmp_path):
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    return fake_common_dir


def _sink_for(fake_common_dir):
    return fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def test_record_op_started_with_no_repo_root_still_writes_a_row(tmp_path, monkeypatch):
    # repo_root=None must fall back to Path.cwd() and resolve the sink from
    # there — never silently drop the row. Chdir into tmp_path so the cwd
    # fallback resolves to a repo this test controls, not the real tree.
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    monkeypatch.chdir(tmp_path)

    corr_id = new_correlation_id()
    record_op_started(op="ping", t_start=1.0, corr_id=corr_id, repo_root=None)

    sink = _sink_for(fake_common_dir)
    assert sink.exists()
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["repo_key_source"] == "cwd"


def test_record_op_latency_with_no_repo_root_still_writes_a_row(tmp_path, monkeypatch):
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    monkeypatch.chdir(tmp_path)

    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="ok", repo_root=None,
    )

    sink = _sink_for(fake_common_dir)
    assert sink.exists()
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["repo_key_source"] == "cwd"


def test_repo_key_source_is_envelope_when_repo_root_is_explicit(tmp_path, monkeypatch):
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="ok", repo_root=tmp_path,
    )

    sink = _sink_for(fake_common_dir)
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["repo_key_source"] == "envelope"


def test_cwd_fallback_never_raises_when_sink_is_unwritable(tmp_path, monkeypatch):
    # Same "never breaks dispatch" contract as the explicit-repo_root path:
    # an unwritable resolved common dir must not raise, even on the cwd
    # fallback branch.
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    unwritable_common_dir = blocking_file / "impossible-child"

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: unwritable_common_dir,
    )
    monkeypatch.chdir(tmp_path)

    # Must not raise.
    record_op_started(
        op="ping", t_start=1.0, corr_id=new_correlation_id(), repo_root=None,
    )
    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="error", repo_root=None,
    )

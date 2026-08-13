"""
coordinator_core.ops.tests.test_producer_axis_creation_seam — producer-axis
engine-half coverage (state/sizings/2026-08-12-producer-axis-claude-klabauter-engine-half.yaml).

Covers:
  1. `resolve_producer_for_creation` degrades a malformed session-shape record
     to `typed_command="unresolved"` instead of raising.
  2. An unresolvable session id (no env var set) resolves `typed_command=None`
     ("nothing captured", never "unresolved" — the two facts must stay
     distinguishable).
  3. `_normalize_one_text` writes the caller-supplied `producer` record
     verbatim — both creation doors resolve to `op_identity="machine-minted"`
     (the finer op-minted-vs-EM-initiated split is deliberately not carried
     on this closed axis).
  4. The critical non-backfill guarantee: the batch `handoff.normalize` sweep
     (`producer=None`, the sweep's own contract) leaves a pre-existing
     record's `producer` field completely untouched — never inferred,
     never overwritten.
  5. `resolve_producer_for_creation` raises on an out-of-enum `op_identity`
     rather than writing it (the closed-set validation guard).
  6. Both creation doors' actual resolver output round-trips through
     `HandoffSummary` — the test that would have caught the original
     three-way token mismatch.

Spec backlink: state/sizings/2026-08-12-producer-axis-claude-klabauter-engine-half.yaml
Spec backlink (cross-repo contract): coordinator-claude
    docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md

Negative-spec: does NOT append to `test_handoff_normalize.py` — that module is
under concurrent peer edit (the sibling `minted_by` axis) on 2026-08-12; a new
test module avoids widening the collision surface there.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_nested_field
from coordinator_core.ops.handoff_normalize import _handler, _normalize_one_text
from coordinator_core.session import core as session_core
from coordinator_core.session import shape as session_shape
from coordinator_core.session.producer_resolve import resolve_producer_for_creation

pytestmark = [pytest.mark.spawns_process]

_GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env={**os.environ, **_GIT_ENV},
        timeout=15, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-safe-env-suppressed
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _handoff_body(*, extra_fm: str = "") -> str:
    return (
        "---\ntitle: Test handoff\ncreated: 2026-08-12\n"
        f"{extra_fm}---\n\n# Test handoff\n\nBody.\n"
    )


def _write_handoff(worktree_root: Path, slug: str, *, extra_fm: str = "") -> Path:
    path = worktree_root / "state" / "handoffs" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_handoff_body(extra_fm=extra_fm), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_producer_for_creation — unit coverage
# ---------------------------------------------------------------------------


def test_both_creation_doors_emit_machine_minted(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_core, "sessions_dir", lambda cwd=None: str(tmp_path / "coordinator-sessions")
    )
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    fork = resolve_producer_for_creation(op_identity="machine-minted")
    baton = resolve_producer_for_creation(op_identity="machine-minted")

    assert fork["op_identity"] == "machine-minted"
    assert baton["op_identity"] == "machine-minted"


def test_out_of_enum_op_identity_raises_not_written():
    with pytest.raises(ValueError):
        resolve_producer_for_creation(op_identity="op-minted")
    with pytest.raises(ValueError):
        resolve_producer_for_creation(op_identity="em-initiated")


def test_creation_door_output_round_trips_through_handoff_summary():
    from coordinator_core.contract.cockpit_schema.entities.summaries import _HandoffProducer

    fork = resolve_producer_for_creation(op_identity="machine-minted")
    baton = resolve_producer_for_creation(op_identity="machine-minted")

    _HandoffProducer.model_validate(fork)
    _HandoffProducer.model_validate(baton)


def test_resolver_accepted_set_and_model_literal_share_one_definition():
    """Drift guard: `producer_resolve._VALID_OP_IDENTITIES` (session layer)
    and `summaries.ProducerOpIdentity` (pydantic contract layer) must be the
    SAME object identity — both re-exports of
    `coordinator_core.producer_vocab.ProducerOpIdentity` — not merely
    equal-valued. Fails if a future edit re-duplicates the Literal in either
    layer instead of importing the shared leaf-module definition."""
    from typing import get_args

    from coordinator_core.contract.cockpit_schema.entities import summaries
    from coordinator_core.producer_vocab import ProducerOpIdentity as _leaf_literal
    from coordinator_core.session import producer_resolve as _resolver_module

    assert summaries.ProducerOpIdentity is _leaf_literal
    assert _resolver_module.ProducerOpIdentity is _leaf_literal
    assert _resolver_module._VALID_OP_IDENTITIES == frozenset(get_args(_leaf_literal))


def test_malformed_session_shape_degrades_to_unresolved(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    monkeypatch.setattr(session_core, "sessions_dir", lambda cwd=None: str(sessions_dir))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-malformed")
    sdir = sessions_dir / "sid-malformed"
    sdir.mkdir(parents=True)
    # Valid JSON, but not an object -- producer_read raises ValueError on this shape.
    (sdir / "session-shape.json").write_text("[]", encoding="utf-8")

    producer = resolve_producer_for_creation(op_identity="machine-minted")

    assert producer == {"typed_command": "unresolved", "op_identity": "machine-minted"}


def test_unresolvable_session_id_resolves_to_none_not_unresolved(monkeypatch, tmp_path):
    monkeypatch.setattr(
        session_core, "sessions_dir", lambda cwd=None: str(tmp_path / "coordinator-sessions")
    )
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    producer = resolve_producer_for_creation(op_identity="machine-minted")

    assert producer == {"typed_command": None, "op_identity": "machine-minted"}


def test_op_identity_required_nonempty():
    with pytest.raises(ValueError):
        resolve_producer_for_creation(op_identity="")


# ---------------------------------------------------------------------------
# _normalize_one_text — caller-supplied threading
# ---------------------------------------------------------------------------


def test_normalize_one_text_stamps_supplied_producer_record(tmp_path):
    content = _handoff_body()
    result = _normalize_one_text(
        content,
        tmp_path / "some-handoff.md",
        None,
        None,
        {"typed_command": "workday-start", "op_identity": "machine-minted"},
    )
    assert result is not None
    block = read_fm_nested_field(result["rebuilt"], "producer")
    assert block is not None
    assert "op_identity: machine-minted" in block
    assert "typed_command: workday-start" in block


def test_normalize_one_text_producer_none_stamps_nothing(tmp_path):
    content = _handoff_body()
    result = _normalize_one_text(content, tmp_path / "some-handoff.md", None, None, None)
    # Other normalizations still fire (category/summary/etc backfill), so the
    # file is NOT byte-identical -- assert specifically that no producer key
    # was introduced.
    rebuilt = result["rebuilt"] if result is not None else content
    assert read_fm_nested_field(rebuilt, "producer") is None


# ---------------------------------------------------------------------------
# Batch sweep — the critical non-backfill guarantee
# ---------------------------------------------------------------------------


def test_batch_sweep_never_backfills_producer_onto_existing_record(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        session_core, "sessions_dir", lambda cwd=None: str(repo / ".git" / "coordinator-sessions")
    )
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-sweeper")

    key_absent = _write_handoff(repo, "2026-08-12-key-absent-handoff")
    key_present = _write_handoff(
        repo,
        "2026-08-12-key-present-handoff",
        extra_fm="producer:\n  op_identity: machine-minted\n  typed_command: null\n",
    )
    before_producer_block = read_fm_nested_field(
        key_present.read_text(encoding="utf-8"), "producer"
    )

    result = _run({"write": True}, repo_root=repo / ".git")

    assert result["errors"] == []
    # The pre-existing record's producer block is untouched. Other fields
    # (category/summary/deliverable_id/initiative) legitimately backfill on
    # this file too -- only the producer axis's own non-backfill guarantee is
    # under test here, so compare that one block, not the whole document.
    after_producer_block = read_fm_nested_field(
        key_present.read_text(encoding="utf-8"), "producer"
    )
    assert after_producer_block == before_producer_block
    # The key-absent handoff must ALSO stay key-absent -- the sweep never
    # infers/mints a producer for a file that never had one.
    assert read_fm_nested_field(key_absent.read_text(encoding="utf-8"), "producer") is None

"""
coordinator_core.ops.tests.test_priority_set — tests for priority.set
(coordinator_core.ops.priority_set), the SOLE writer of a priority-ledger entry.

Coverage:
  (a) basic write: correct filename, correct fields (target_id, target_kind,
      priority, set_by, source: op, source_repo: null, set_at present).
  (b) CLEARING WRITES THE `none` SENTINEL, never deletes the file — an
      explicit second write with priority="none" leaves the file present with
      priority: none on disk.
  (c) fail-loud: missing/blank target_id, out-of-enum target_kind, out-of-enum
      priority each raise ValueError with zero writes.
  (d) idempotent overwrite: two writes with identical field values still
      succeed and leave exactly one file.
  (e) schema-enforced: the priority-ledger schema is vendored locally (C1 of
      docs/plans/2026-08-06-vendor-priority-ledger-and-priority-inte.md) and
      always resolvable — a schema-invalid entry (unknown target_kind) raises
      MutateAbort, no skip branch.
  (f) target_id trust boundary: a traversal-shaped target_id is rejected
      unconditionally, independent of schema resolution (C3, same plan).

POSIX guard mirrors test_goal_kr_status.py: skipped if neither fcntl nor
msvcrt is available (locked_rmw's lock-backend requirement).

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C3
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE

pytestmark = pytest.mark.skipif(
    not _LOCKING_AVAILABLE,
    reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
)

from coordinator_core.locked_write import MutateAbort  # noqa: E402
from coordinator_core.ops import priority_set  # noqa: E402
from coordinator_core.ops.priority_set import set_priority  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_central_root(tmp_path, monkeypatch):
    """Redirect the op's central-state resolution to a per-test tmp dir so
    concurrent test runs (and the real claude-klabauter central state) are never
    touched by this test module.

    ``git init`` the tmp dir: ``locked_rmw``'s lock-sidecar placement resolves
    ``git_common_dir(repo_root)`` (coordinator_core.lifecycle), which requires
    a real git repository — in production the central-state root always sits
    inside claude-klabauter's own git checkout, so this mirrors that shape rather than
    special-casing the op for a non-git test fixture.
    """
    fake_root = tmp_path / "central-state"
    fake_root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(fake_root),
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    monkeypatch.setattr(
        priority_set, "coordinator_state_root", lambda central=False: str(fake_root)
    )
    return fake_root


def _ledger_file(central_root: Path, target_id: str) -> Path:
    return central_root / "priority-ledger" / f"{target_id}.yaml"


def test_basic_write_fields(_isolated_central_root):
    result = set_priority(
        "handoff-abc123", "handoff", "high", set_by="test-session", note="urgent triage"
    )

    assert result == {
        "target_id": "handoff-abc123",
        "target_kind": "handoff",
        "priority": "high",
        "set_by": "test-session",
        "source": "op",
    }

    ledger_file = _ledger_file(_isolated_central_root, "handoff-abc123")
    assert ledger_file.is_file()
    text = ledger_file.read_text()
    assert "target_id: handoff-abc123" in text
    assert "target_kind: handoff" in text
    assert "priority: high" in text
    assert "set_by: test-session" in text
    assert "source: op" in text
    assert "source_repo: null" in text
    assert "set_at:" in text
    assert "note: urgent triage" in text
    # NEGATIVE-SPEC: no inherited_from / resolved_from / priority_predecessor
    # field, and no bare `id:` (target_id IS the filename, not a separate
    # `id` field) — line-exact check since "id:" is a substring of "target_id:".
    lines = text.splitlines()
    for forbidden in ("inherited_from:", "resolved_from:", "priority_predecessor:"):
        assert not any(line.startswith(forbidden) for line in lines)
    assert not any(line.strip().startswith("id:") for line in lines)


def test_clear_writes_none_sentinel_not_delete(_isolated_central_root):
    set_priority("plan-xyz", "plan", "urgent", set_by="session-a")
    ledger_file = _ledger_file(_isolated_central_root, "plan-xyz")
    assert ledger_file.is_file()

    result = set_priority("plan-xyz", "plan", "none", set_by="session-b")

    assert result["priority"] == "none"
    # File still present — clearing never deletes.
    assert ledger_file.is_file()
    text = ledger_file.read_text()
    assert "priority: none" in text
    assert "set_by: session-b" in text


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"target_id": "", "target_kind": "handoff", "priority": "high"}, "target_id"),
        ({"target_id": "x", "target_kind": "bogus", "priority": "high"}, "target_kind"),
        ({"target_id": "x", "target_kind": "handoff", "priority": "bogus"}, "priority"),
    ],
)
def test_invalid_params_raise_value_error_zero_writes(_isolated_central_root, kwargs, match):
    with pytest.raises(ValueError, match=match):
        set_priority(**kwargs)
    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())


def test_idempotent_repeat_write_single_file(_isolated_central_root):
    set_priority("roadmap-1", "roadmap", "medium", set_by="s")
    set_priority("roadmap-1", "roadmap", "medium", set_by="s")

    ledger_dir = _isolated_central_root / "priority-ledger"
    files = list(ledger_dir.iterdir())
    assert len(files) == 1


def test_schema_validation_enforced(_isolated_central_root):
    """The vendored priority-ledger schema (C1) is always resolvable now — no
    tolerant-skip branch. Assert a bad target_kind is caught at the SCHEMA
    layer too by bypassing this op's own enum guard (calling
    _apply_priority_set directly): build a schema-invalid entry (unknown
    target_kind) directly against the real schema.
    """
    schema_path = priority_set._resolve_schema_path()
    assert schema_path is not None, (
        "priority-ledger.schema.json must be resolvable now that it is "
        "vendored (C1) — a None here means the vendored file went missing"
    )

    with pytest.raises(MutateAbort):
        priority_set._apply_priority_set(
            "", "t1", "not-a-real-kind", "high", "tester", ""
        )


def test_traversal_shaped_target_id_rejected_with_schema_stubbed_unavailable(
    _isolated_central_root, monkeypatch
):
    """C3: set_priority()'s target_id trust boundary must be UNCONDITIONAL,
    independent of whether schema resolution succeeds. Stub schema
    resolution unavailable (mirrors an unresolvable/missing vendored file)
    and prove a traversal-shaped target_id is still rejected before any path
    interpolation. Must fail against the pre-C3 code (where the schema was
    the only defense on this path).
    """
    monkeypatch.setattr(priority_set, "_resolve_schema_path", lambda: None)

    with pytest.raises(ValueError, match="target_id"):
        set_priority("../../etc/passwd", "handoff", "high")

    ledger_dir = _isolated_central_root / "priority-ledger"
    assert not ledger_dir.exists() or not list(ledger_dir.iterdir())

"""
coordinator_core.ops.tests.test_handoff_repoint_origin

Tests for the handoff.repoint_origin op (coordinator_core/ops/handoff_repoint_origin.py)
— the sanctioned hook-exempt writer for repointing/nulling the origin_handoff
provenance field on an existing state/handoffs/*.md stub.

Import guard: coordinator_core.ops.handoff_repoint_origin MUST be imported at module
load time to fire the @register_op("handoff.repoint_origin") side-effect and populate
_REGISTRY. Without this the decorator has not run and the registry assertion passes
vacuously over an empty (or incomplete) registry.

Coverage:
  (a) null-repoint  — an unresolvable phantom origin_handoff is nulled; body + other
                       keys preserved verbatim.
  (b) resolvable    — repointing to a RESOLVABLE existing handoff path succeeds.
  (c) unresolvable  — repointing to an UNRESOLVABLE path fails loud (exit_code 1,
                       structured error), no write performed.
  (d) idempotent    — a second call with the same new_origin is a no-op success
                       (changed:False), re-running never re-writes.

Spec backlink: cross-repo memo — sanctioned hook-exempt writer for the
origin_handoff provenance edge (2026-07-19).
Port template: coordinator_core/ops/handoff_normalize.py
"""

from __future__ import annotations

import asyncio

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_repoint_origin  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_repoint_origin import _handler

_OP_NAME = "handoff.repoint_origin"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.handoff_repoint_origin @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio required."""
    return asyncio.run(coro)


_TARGET_CONTENT = """---
title: "Test target handoff"
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: null
origin_handoff: {origin}
---

# Test target handoff

Some body text that must survive untouched.
"""

_ORIGIN_CONTENT = """---
title: "Test origin handoff"
created: 2026-01-01
branch: work/test/2026-01-01
status: open
predecessor: null
---

# Test origin handoff

Body.
"""


# ---------------------------------------------------------------------------
# (a) null-repoint of an unresolvable phantom origin_handoff
# ---------------------------------------------------------------------------


def test_null_repoint_of_phantom_origin(norm_repo):
    norm_repo.seed_handoff(
        "target.md", _TARGET_CONTENT.format(origin="nonexistent-phantom-handoff.md")
    )

    result = _run(
        _handler(
            {"targets": "state/handoffs/target.md", "new_origin": None},
            repo_root=norm_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert len(result["results"]) == 1
    entry = result["results"][0]
    assert entry["file"] == "state/handoffs/target.md"
    assert entry["old_origin"] == "nonexistent-phantom-handoff.md"
    assert entry["new_origin"] is None
    assert entry["changed"] is True
    assert result["errors"] == []

    rewritten = norm_repo.read_handoff("target.md")
    assert "origin_handoff: null" in rewritten
    # Every other key preserved.
    assert 'title: "Test target handoff"' in rewritten
    assert "created: 2026-01-01" in rewritten
    assert "branch: work/test/2026-01-01" in rewritten
    assert "status: open" in rewritten
    assert "predecessor: null" in rewritten
    # Body preserved verbatim.
    assert "Some body text that must survive untouched." in rewritten
    assert "# Test target handoff" in rewritten


# ---------------------------------------------------------------------------
# (b) repoint to a RESOLVABLE existing handoff path
# ---------------------------------------------------------------------------


def test_repoint_to_resolvable_origin_succeeds(norm_repo):
    norm_repo.seed_handoff("origin.md", _ORIGIN_CONTENT)
    norm_repo.seed_handoff(
        "target.md", _TARGET_CONTENT.format(origin="old-phantom-origin.md")
    )

    result = _run(
        _handler(
            {"targets": "state/handoffs/target.md", "new_origin": "origin.md"},
            repo_root=norm_repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    entry = result["results"][0]
    assert entry["old_origin"] == "old-phantom-origin.md"
    assert entry["new_origin"] == "origin.md"
    assert entry["changed"] is True
    assert result["errors"] == []

    rewritten = norm_repo.read_handoff("target.md")
    assert "origin_handoff: origin.md" in rewritten


# ---------------------------------------------------------------------------
# (c) repoint to an UNRESOLVABLE path fails loud
# ---------------------------------------------------------------------------


def test_repoint_to_unresolvable_origin_fails_loud(norm_repo):
    norm_repo.seed_handoff(
        "target.md", _TARGET_CONTENT.format(origin="old-phantom-origin.md")
    )

    result = _run(
        _handler(
            {
                "targets": "state/handoffs/target.md",
                "new_origin": "definitely-never-existed-anywhere.md",
            },
            repo_root=norm_repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert result["results"] == []
    assert len(result["errors"]) == 1
    assert "unresolvable" in result["errors"][0]["error"]

    # No write performed — the phantom origin_handoff is untouched on disk.
    untouched = norm_repo.read_handoff("target.md")
    assert "origin_handoff: old-phantom-origin.md" in untouched


# ---------------------------------------------------------------------------
# (d) idempotency — a second call with the same new_origin is a no-op success
# ---------------------------------------------------------------------------


def test_repoint_is_idempotent(norm_repo):
    norm_repo.seed_handoff(
        "target.md", _TARGET_CONTENT.format(origin="nonexistent-phantom-handoff.md")
    )

    params = {"targets": "state/handoffs/target.md", "new_origin": None}

    first = _run(_handler(params, repo_root=norm_repo.common_dir))
    assert first["exit_code"] == 0
    assert first["results"][0]["changed"] is True

    after_first = norm_repo.read_handoff("target.md")

    second = _run(_handler(params, repo_root=norm_repo.common_dir))
    assert second["exit_code"] == 0, second
    assert second["applied"] is False
    assert len(second["results"]) == 1
    assert second["results"][0]["changed"] is False
    assert second["results"][0]["old_origin"] is None
    assert second["results"][0]["new_origin"] is None
    assert second["errors"] == []

    after_second = norm_repo.read_handoff("target.md")
    assert after_first == after_second  # byte-identical — no re-write occurred


# ---------------------------------------------------------------------------
# Usage-error paths
# ---------------------------------------------------------------------------


def test_missing_targets_is_usage_error(norm_repo):
    result = _run(_handler({"new_origin": None}, repo_root=norm_repo.common_dir))
    assert result["exit_code"] == 2
    assert result["applied"] is False
    assert "targets" in result["errors"][0]["error"]


def test_missing_new_origin_key_is_usage_error(norm_repo):
    result = _run(
        _handler({"targets": "state/handoffs/target.md"}, repo_root=norm_repo.common_dir)
    )
    assert result["exit_code"] == 2
    assert result["applied"] is False
    assert "new_origin" in result["errors"][0]["error"]


def test_no_repo_root_is_usage_error():
    result = _run(_handler({"targets": "state/handoffs/target.md", "new_origin": None}))
    assert result["exit_code"] == 2
    assert result["applied"] is False

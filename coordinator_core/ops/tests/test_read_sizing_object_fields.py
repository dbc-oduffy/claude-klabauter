"""
coordinator_core.ops.tests.test_read_sizing_object_fields

Unit tests for coordinator_core.ops.read_sizing_object_fields (C6, hard-
requirement partial 1: "fenceless sizing read").

Coverage:
  (a) FAILS-THE-WRONG-WAY-FIRST regression: `read_frontmatter_field` over a
      bare-YAML (no `---` fence) sizing-object cannot reach a nested/array
      field — pins the defect this op exists to close.
  (b) `_read_sizing_object_fields` — happy path over a whole-document YAML
      fixture carrying all four fields; absent-field -> None; non-mapping
      parse -> ValueError; unreadable path -> ValueError.
  (c) import-guard + registry — "sizing.read_object_fields" registered after
      import.
  (d) op handler — missing sizing_path raises ValueError; path escaping
      state/sizings//archive/sizings raises ValueError; happy path reads
      through repo_root/worktree resolution.

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C6.md
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Declared, not excused: `_init_worktree` spawns a real `git init` because the
# op handler resolves `repo_root` via `main_worktree_root`, itself a real git
# query (`git rev-parse`-shaped) — no fixture stands in for that resolution.
# Mirrors test_sizing_ship.py's/test_sizing_decline.py's identical rationale.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.read_sizing_object_fields  # noqa: F401 — fires @register_op

import coordinator_core.ipc as ipc
from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.ops.read_sizing_object_fields import (
    _handler,
    _read_sizing_object_fields,
)
from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

_OP_NAME = "sizing.read_object_fields"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.read_sizing_object_fields @register_op did not fire"
)

_SIZING_YAML = """schema: sizing-object
intent: Do the thing, verbatim.
appetite: medium
estimate:
  tshirt: M
  provisional: false
scout_evidence:
  - kind: mention-count
    finding: "12 mentions"
route: plan
detents: []
fork: null
xl_exit: null
status: sized
premise:
  provenance: unrecorded
"""


# ---------------------------------------------------------------------------
# (a) fails-the-wrong-way-first: read_frontmatter_field cannot serve
# ---------------------------------------------------------------------------


def test_read_frontmatter_field_cannot_reach_nested_estimate(tmp_path):
    """Pins the defect this op exists to close: a line-based `^field:`
    scalar reader over a fenceless sizing-object returns only the raw
    (empty) suffix of the `estimate:` key line — never the nested
    `{tshirt, provisional}` mapping beneath it.
    """
    sizing_path = tmp_path / "a-sizing.yaml"
    sizing_path.write_text(_SIZING_YAML, encoding="utf-8")

    raw = read_frontmatter_field(str(sizing_path), "estimate")

    assert raw == "", (
        "read_frontmatter_field unexpectedly returned nested content for "
        "'estimate' — if this now fails, the fenceless-sizing-read gap this "
        "op exists to close may have closed itself; re-check before editing "
        "this pin."
    )


# ---------------------------------------------------------------------------
# (b) _read_sizing_object_fields
# ---------------------------------------------------------------------------


def test_read_sizing_object_fields_happy_path(tmp_path):
    sizing_path = tmp_path / "a-sizing.yaml"
    sizing_path.write_text(_SIZING_YAML, encoding="utf-8")

    fields = _read_sizing_object_fields(str(sizing_path))

    assert fields["intent"] == "Do the thing, verbatim."
    assert fields["appetite"] == "medium"
    assert fields["estimate"] == {"tshirt": "M", "provisional": False}
    assert fields["scout_evidence"] == [{"kind": "mention-count", "finding": "12 mentions"}]


def test_read_sizing_object_fields_absent_fields_are_none(tmp_path):
    sizing_path = tmp_path / "minimal-sizing.yaml"
    sizing_path.write_text("schema: sizing-object\nintent: Minimal ask.\n", encoding="utf-8")

    fields = _read_sizing_object_fields(str(sizing_path))

    assert fields["intent"] == "Minimal ask."
    assert fields["appetite"] is None
    assert fields["estimate"] is None
    assert fields["scout_evidence"] is None


def test_read_sizing_object_fields_non_mapping_raises(tmp_path):
    sizing_path = tmp_path / "list-shaped.yaml"
    sizing_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _read_sizing_object_fields(str(sizing_path))


def test_read_sizing_object_fields_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        _read_sizing_object_fields(str(tmp_path / "does-not-exist.yaml"))


# ---------------------------------------------------------------------------
# (d) op handler
# ---------------------------------------------------------------------------


def test_handler_missing_sizing_path_raises_value_error():
    with pytest.raises(ValueError):
        _handler({}, repo_root=Path("."))


def test_handler_missing_repo_root_raises_value_error():
    with pytest.raises(ValueError):
        _handler({"sizing_path": "state/sizings/x.yaml"}, repo_root=None)


def test_handler_escaping_path_raises_value_error(tmp_path):
    _init_worktree(tmp_path)
    with pytest.raises(ValueError):
        _handler({"sizing_path": "../outside.yaml"}, repo_root=tmp_path)


def test_handler_happy_path(tmp_path):
    worktree = _init_worktree(tmp_path)
    sizings_dir = worktree / "state" / "sizings"
    sizings_dir.mkdir(parents=True)
    sizing_path = sizings_dir / "a-sizing.yaml"
    sizing_path.write_text(_SIZING_YAML, encoding="utf-8")

    result = _handler(
        {"sizing_path": "state/sizings/a-sizing.yaml"}, repo_root=worktree
    )

    assert result["sizing_path"] == "state/sizings/a-sizing.yaml"
    assert result["intent"] == "Do the thing, verbatim."
    assert result["estimate"] == {"tshirt": "M", "provisional": False}


# ---------------------------------------------------------------------------
# (e) real dispatch path — through ipc.dispatch_message, not a direct
# `_handler(...)` call. B1 (code review, slice B): `sizing.read_object_fields`
# was absent from `op_scopes.py::_OP_KEY_SCOPE`, so every real JSON-RPC
# dispatch resolved `scope = "none"` and reached the handler with
# `repo_root=None` — 100% failure in production despite every test above
# (which all call `_handler(...)` directly with an explicit `repo_root=`)
# passing green. Mirrors `test_artifact_emit_scope_touch.py`'s dispatch-path
# pattern (drive the REAL registered handler end-to-end through
# `dispatch_message`, not a synthetic stand-in) — the only in-repo precedent
# found for a scope-table regression test of this shape.
# ---------------------------------------------------------------------------


def test_dispatch_message_resolves_repo_root_and_returns_fields(tmp_path):
    assert "sizing.read_object_fields" in ipc._REGISTRY, (
        "import guard: sizing.read_object_fields not registered"
    )
    worktree = _init_worktree(tmp_path)
    sizings_dir = worktree / "state" / "sizings"
    sizings_dir.mkdir(parents=True)
    sizing_path = sizings_dir / "a-sizing.yaml"
    sizing_path.write_text(_SIZING_YAML, encoding="utf-8")

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sizing.read_object_fields",
        "params": {"sizing_path": "state/sizings/a-sizing.yaml"},
        "_origin_worktree": str(worktree),
    }
    reply = asyncio.run(dispatch_message(msg))

    assert "error" not in reply, reply
    assert reply["result"]["sizing_path"] == "state/sizings/a-sizing.yaml"
    assert reply["result"]["intent"] == "Do the thing, verbatim."
    assert reply["result"]["estimate"] == {"tshirt": "M", "provisional": False}


def _init_worktree(tmp_path: Path) -> Path:
    """Minimal git worktree so `main_worktree_root` resolves without a real repo."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    worktree = tmp_path / "repo"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(worktree),
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    return worktree

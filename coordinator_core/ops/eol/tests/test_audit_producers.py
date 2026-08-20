"""coordinator_core.ops.eol.tests.test_audit_producers -- op tests for
"eol.audit_producers".

House convention (a)-(e), per this plan's C6 body:
  (a) registration -- the op lands in the live _REGISTRY on import.
  (b) negative paths -- missing target_root -> ValueError; a propagating
      PathEscapeError is not swallowed by the handler.
  (c) end-to-end round trip through the registered handler, against a
      fixture tree carrying one violation of EACH direction the predicate
      checks (a bare `open(..., "w")` with no newline= pin, and a
      `Path.write_text` call -- both flagged) plus one dirty^ file that
      must be skipped: `_is_test_file` exclusion (`test_*.py` never
      scanned, mirroring the source ratchet's own scope).
      (^ "dirty" here means excluded-by-design, not git-dirty -- this op
      spawns no subprocess and reads no git state at all; the excluded
      leg the fixture must still carry is the test-file exclusion.)
  (d) a real dispatch_message() command-type smoke -- the _OP_KEY_SCOPE
      keying path in-process handler tests do not exercise. A missing/wrong
      op_scopes.py entry would raise ValueError here demanding
      `_origin_worktree`, since this call deliberately omits that envelope
      field -- exactly the shape a sibling repo's caller sends.
  (e) classification assertion -- eol.audit_producers is OpClass.COMPUTE_ONLY
      -- the DR-208 affirmation granted at review per C5 (a read: rglob /
      read_text / ast.parse, no subprocess and no open-for-write).

No subprocess anywhere in this op (pure pathlib/ast) -- this module carries
no spawns_process/cadence marker, unlike its census/repair siblings.

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C4, C6
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Import guard -- MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.eol.audit_producers  # noqa: F401 -- fires @register_op

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.ops.eol.audit_producers import _eol_audit_producers

_OP_NAME = "eol.audit_producers"


def _new_fixture_tree(tmp_path):
    """One production offender via bare `open(..., "w")` with no newline=,
    one production offender via `Path.write_text(...)` with no newline=,
    and one test-file (`test_*.py`) carrying the identical offending shape
    that `_is_test_file` must exclude from scanning entirely."""
    d = tmp_path / "repo"
    d.mkdir()
    (d / "producer_open.py").write_text(
        "def write_it(path):\n"
        "    with open(path, 'w') as fh:\n"
        "        fh.write('hi')\n",
        encoding="utf-8",
    )
    (d / "producer_write_text.py").write_text(
        "from pathlib import Path\n"
        "def write_it(path):\n"
        "    Path(path).write_text('hi')\n",
        encoding="utf-8",
    )
    (d / "test_producer_open.py").write_text(
        "def write_it(path):\n"
        "    with open(path, 'w') as fh:\n"
        "        fh.write('hi')\n",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_registration():
    assert _OP_NAME in _REGISTRY, (
        f"import guard failed: {_OP_NAME!r} not in _REGISTRY -- "
        "coordinator_core.ops.eol.audit_producers @register_op did not fire"
    )


# ---------------------------------------------------------------------------
# (b) negative paths
# ---------------------------------------------------------------------------


def test_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        _eol_audit_producers({})


def test_path_escape_error_propagates_uncaught(monkeypatch):
    import coordinator_core.ops.eol.audit_producers as mod

    def boom(target_root, path):
        raise PathEscapeError("forced escape")

    monkeypatch.setattr(mod, "path_guard", boom)

    with pytest.raises(PathEscapeError):
        _eol_audit_producers({"target_root": "whatever"})


# ---------------------------------------------------------------------------
# (c) end-to-end round trip
# ---------------------------------------------------------------------------


def test_end_to_end_flags_both_call_shapes_and_excludes_test_files(tmp_path):
    d = _new_fixture_tree(tmp_path)

    result = _eol_audit_producers({"target_root": str(d)})

    assert result["offender_count"] == 2
    assert any(o.startswith("producer_open.py:") for o in result["offenders"])
    assert any(o.startswith("producer_write_text.py:") for o in result["offenders"])
    assert not any(o.startswith("test_producer_open.py:") for o in result["offenders"])
    # 2 production sources scanned; the test_*.py file is excluded entirely.
    assert result["scanned"] == 2


# ---------------------------------------------------------------------------
# (d) dispatch_message wire smoke -- the _OP_KEY_SCOPE keying path
# ---------------------------------------------------------------------------


def test_dispatch_message_wire_smoke_without_origin_worktree(tmp_path):
    """No `_origin_worktree` envelope field -- the sibling-repo caller
    shape. Proves op_scopes.py keys eol.audit_producers "none": a wrong
    scope entry (e.g. "show_top") would raise here demanding
    _origin_worktree, a defect the in-process handler tests above cannot
    see."""
    d = _new_fixture_tree(tmp_path)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP_NAME,
        "params": {"target_root": str(d)},
    }
    response = asyncio.run(dispatch_message(msg))

    assert "error" not in response, response.get("error")
    result = response["result"]
    assert result["offender_count"] == 2


# ---------------------------------------------------------------------------
# (e) classification
# ---------------------------------------------------------------------------


def test_classified_compute_only():
    assert OP_CLASSIFICATION[_OP_NAME] is OpClass.COMPUTE_ONLY

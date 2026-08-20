"""coordinator_core.ops.eol.tests.test_repair -- op tests for "eol.repair".

House convention (a)-(e), per this plan's C6 body:
  (a) registration -- the op lands in the live _REGISTRY on import.
  (b) negative paths -- missing target_root -> ValueError; a propagating
      PathEscapeError is not swallowed by the handler.
  (c) end-to-end round trip through the registered handler, against a
      fixture repo carrying one violation of EACH direction (both repaired,
      both directions, AC5) plus one dirty file that must be SKIPPED
      (reason "dirty") -- dry-run by default, then a real mutate:true pass
      that writes normalized bytes to disk.
  (d) a real dispatch_message() command-type smoke -- the _OP_KEY_SCOPE
      keying path in-process handler tests do not exercise. A missing/wrong
      op_scopes.py entry would raise ValueError here demanding
      `_origin_worktree`, since this call deliberately omits that envelope
      field -- exactly the shape a sibling repo's caller sends.
  (e) classification assertion -- eol.repair is OpClass.MUTATING
      unambiguously (writes disk bytes when mutate:true).

Spawns real `git` against a fixture repo -- tiered off the per-commit path
per this repo's spawn ratchet (coordinator_core/tests/test_no_new_spawning_tests.py).

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C3, C6
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guard -- MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.eol.repair  # noqa: F401 -- fires @register_op

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.ops.eol.repair import _eol_repair
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "eol.repair"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )


def _new_fixture_repo(tmp_path):
    """Same shape as eol.census's fixture (test_census.py) -- a.txt
    (declared lf, holds CRLF) and b.txt (declared crlf, holds LF-only) are
    clean, provably EOL-only violations (index-blob-equals-LF-normalized
    holds for both, since nothing but line endings differs from what was
    committed); c.txt carries a further uncommitted edit on top of its own
    committed violation, so `eol.repair` must skip it with reason "dirty".
    """
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "core.autocrlf", "false")
    (d / ".gitattributes").write_text(
        "a.txt eol=lf\nb.txt eol=crlf\nc.txt eol=lf\n", newline="\n"
    )
    _git(d, "add", ".gitattributes")
    _git(d, "commit", "-qm", "attrs")

    with open(d / "a.txt", "wb") as fh:
        fh.write(b"alpha\r\nbeta\r\n")
    with open(d / "b.txt", "wb") as fh:
        fh.write(b"gamma\ndelta\n")
    with open(d / "c.txt", "wb") as fh:
        fh.write(b"epsilon\r\nzeta\r\n")
    _git(d, "add", "a.txt", "b.txt", "c.txt")
    _git(d, "commit", "-qm", "violations")

    # Leave c.txt dirty -- a further uncommitted edit, still violating.
    with open(d / "c.txt", "wb") as fh:
        fh.write(b"epsilon\r\nzeta\r\nETA\r\n")

    return d


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_registration():
    assert _OP_NAME in _REGISTRY, (
        f"import guard failed: {_OP_NAME!r} not in _REGISTRY -- "
        "coordinator_core.ops.eol.repair @register_op did not fire"
    )


# ---------------------------------------------------------------------------
# (b) negative paths
# ---------------------------------------------------------------------------


def test_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        _eol_repair({})


def test_path_escape_error_propagates_uncaught(monkeypatch):
    import coordinator_core.ops.eol.repair as mod

    def boom(target_root, path):
        raise PathEscapeError("forced escape")

    monkeypatch.setattr(mod, "path_guard", boom)

    with pytest.raises(PathEscapeError):
        _eol_repair({"target_root": "whatever"})


# ---------------------------------------------------------------------------
# (c) end-to-end round trip -- dry-run, then mutate:true
# ---------------------------------------------------------------------------


def test_dry_run_reports_both_directions_and_skips_dirty(tmp_path):
    d = _new_fixture_repo(tmp_path)

    result = _eol_repair({"target_root": str(d)})

    assert result["dry_run"] is True
    repaired_by_path = {r["path"]: r for r in result["repaired"]}
    assert repaired_by_path["a.txt"]["declared"] == "lf"
    assert repaired_by_path["b.txt"]["declared"] == "crlf"
    assert result["repaired_count"] == 2

    skipped_by_path = {s["path"]: s for s in result["skipped"]}
    assert skipped_by_path["c.txt"]["reason"] == "dirty"
    assert result["skipped_count"] == 1
    assert result["violation_count"] == 3

    # Dry-run must not touch disk.
    assert (d / "a.txt").read_bytes() == b"alpha\r\nbeta\r\n"
    assert (d / "b.txt").read_bytes() == b"gamma\ndelta\n"


def test_mutate_true_writes_normalized_bytes_both_directions(tmp_path):
    d = _new_fixture_repo(tmp_path)

    result = _eol_repair({"target_root": str(d)})  # dry-run first, unaffected by the mutate call below
    assert result["dry_run"] is True

    mutated = _eol_repair({"target_root": str(d), "mutate": True})

    assert mutated["dry_run"] is False
    assert mutated["repaired_count"] == 2
    assert (d / "a.txt").read_bytes() == b"alpha\nbeta\n"
    assert (d / "b.txt").read_bytes() == b"gamma\r\ndelta\r\n"
    # c.txt (dirty, skipped) is untouched.
    assert (d / "c.txt").read_bytes() == b"epsilon\r\nzeta\r\nETA\r\n"


# ---------------------------------------------------------------------------
# (d) dispatch_message wire smoke -- the _OP_KEY_SCOPE keying path
# ---------------------------------------------------------------------------


def test_dispatch_message_wire_smoke_without_origin_worktree(tmp_path):
    """No `_origin_worktree` envelope field -- the sibling-repo caller
    shape. Proves op_scopes.py keys eol.repair "none": a wrong scope entry
    (e.g. "show_top") would raise here demanding _origin_worktree, a defect
    the in-process handler tests above cannot see."""
    d = _new_fixture_repo(tmp_path)
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP_NAME,
        "params": {"target_root": str(d)},
    }
    response = asyncio.run(dispatch_message(msg))

    assert "error" not in response, response.get("error")
    result = response["result"]
    assert result["dry_run"] is True
    assert result["repaired_count"] == 2


# ---------------------------------------------------------------------------
# (e) classification
# ---------------------------------------------------------------------------


def test_classified_mutating():
    assert OP_CLASSIFICATION[_OP_NAME] is OpClass.MUTATING

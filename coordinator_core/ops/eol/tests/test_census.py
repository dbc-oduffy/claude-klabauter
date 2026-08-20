"""coordinator_core.ops.eol.tests.test_census -- op tests for "eol.census".

House convention (a)-(e), per this plan's C6 body:
  (a) registration -- the op lands in the live _REGISTRY on import.
  (b) negative paths -- missing target_root -> ValueError; a propagating
      PathEscapeError is not swallowed by the handler.
  (c) end-to-end round trip through the registered handler, against a
      fixture repo carrying one violation of EACH direction plus one dirty
      file that must be reported (flagged dirty, never dropped -- census
      reports dirty violations, repair is what skips them, C3).
  (d) a real dispatch_message() command-type smoke -- the _OP_KEY_SCOPE
      keying path in-process handler tests do not exercise. A missing/wrong
      op_scopes.py entry (e.g. "show_top"/"common_dir" instead of "none")
      would raise ValueError here demanding `_origin_worktree`, since this
      call deliberately omits that envelope field -- exactly the shape a
      sibling repo's caller sends.
  (e) classification assertion -- eol.census is OpClass.MUTATING (DR-208
      affirmation left to reviewer per C5, not elevated to COMPUTE_ONLY here).

Spawns real `git` against a fixture repo -- tiered off the per-commit path
per this repo's spawn ratchet (coordinator_core/tests/test_no_new_spawning_tests.py).

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C2, C6
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Import guard -- MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.eol.census  # noqa: F401 -- fires @register_op

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.ops.eol.census import _eol_census
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "eol.census"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )


def _new_fixture_repo(tmp_path):
    """A committed tree carrying one clean violation of EACH direction
    (a.txt declared lf holding CRLF, b.txt declared crlf holding LF-only)
    plus one file (c.txt) left dirty after its own committed violation --
    the "must be skipped" file this plan's repair op (C3) excludes, that
    census must still REPORT with dirty=True (AC1: every tracked path).
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
        "coordinator_core.ops.eol.census @register_op did not fire"
    )


# ---------------------------------------------------------------------------
# (b) negative paths
# ---------------------------------------------------------------------------


def test_missing_target_root_raises_value_error():
    with pytest.raises(ValueError, match="target_root"):
        _eol_census({})


def test_path_escape_error_propagates_uncaught(monkeypatch):
    import coordinator_core.ops.eol.census as mod

    def boom(target_root, path):
        raise PathEscapeError("forced escape")

    monkeypatch.setattr(mod, "path_guard", boom)

    with pytest.raises(PathEscapeError):
        _eol_census({"target_root": "whatever"})


# ---------------------------------------------------------------------------
# (c) end-to-end round trip
# ---------------------------------------------------------------------------


def test_end_to_end_bidirectional_violations_and_dirty_flag(tmp_path):
    d = _new_fixture_repo(tmp_path)

    result = _eol_census({"target_root": str(d)})

    by_path = {v["path"]: v for v in result["violations"]}
    assert by_path["a.txt"] == {
        "path": "a.txt",
        "declared": "lf",
        "found": "crlf-present",
        "dirty": False,
    }
    assert by_path["b.txt"] == {
        "path": "b.txt",
        "declared": "crlf",
        "found": "lf-only",
        "dirty": False,
    }
    assert by_path["c.txt"]["dirty"] is True
    assert by_path["c.txt"]["declared"] == "lf"

    assert result["violation_count"] == 3
    assert result["dirty_violation_count"] == 1
    assert result["tracked_count"] == 4  # .gitattributes, a.txt, b.txt, c.txt
    assert result["declaration_coverage"] == {
        "unspecified_count": 1,  # .gitattributes carries no eol declaration
        "declared_count": 3,
    }


# ---------------------------------------------------------------------------
# (d) dispatch_message wire smoke -- the _OP_KEY_SCOPE keying path
# ---------------------------------------------------------------------------


def test_dispatch_message_wire_smoke_without_origin_worktree(tmp_path):
    """No `_origin_worktree` envelope field -- the sibling-repo caller
    shape. Proves op_scopes.py keys eol.census "none": a wrong scope entry
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
    assert result["violation_count"] == 3


# ---------------------------------------------------------------------------
# (e) classification
# ---------------------------------------------------------------------------


def test_classified_mutating():
    assert OP_CLASSIFICATION[_OP_NAME] is OpClass.MUTATING

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


# ---------------------------------------------------------------------------
# F1 -- binary corruption under a wildcard eol=crlf declaration (CRITICAL)
# ---------------------------------------------------------------------------


def test_wildcard_eol_crlf_binary_content_untouched_after_mutate(tmp_path):
    """Regression for the critical review finding: a wildcard
    `* text=auto eol=crlf` declaration made `check-attr eol` answer `crlf`
    for a binary path, and `_normalize_for_index_check` is a no-op on
    content with no CRLF -- so the index-blob-equality refusal was vacuous
    for this exact case, and `mutate: true` would insert `\\r` before every
    `\\n` in the binary. `eol.census`'s NUL-byte guard must keep this path
    out of the violation set entirely, so `eol.repair` never even sees it
    as a candidate."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "core.autocrlf", "false")
    (d / ".gitattributes").write_bytes(b"* text=auto eol=crlf\n")
    _git(d, "add", ".gitattributes")
    _git(d, "commit", "-qm", "attrs")

    binary_content = b"\x00\x01\x02fake\nbinary\x00content\n"
    (d / "image.bin").write_bytes(binary_content)
    _git(d, "add", "image.bin")
    _git(d, "commit", "-qm", "binary")

    result = _eol_repair({"target_root": str(d), "mutate": True})

    assert "image.bin" not in {r["path"] for r in result["repaired"]}
    assert "image.bin" not in {s["path"] for s in result["skipped"]}
    assert (d / "image.bin").read_bytes() == binary_content


# ---------------------------------------------------------------------------
# F2 -- read-then-write race
# ---------------------------------------------------------------------------


def test_concurrent_edit_between_read_and_write_is_not_reverted(tmp_path, monkeypatch):
    """A peer writes to a candidate's path in the window between this op's
    initial read and its final disk write (simulated here as the window
    around `_index_blobs`, which is where the real op spawns and awaits a
    git subprocess). The op must skip with `changed-under-us` rather than
    silently reverting the peer's edit to its own stale normalization."""
    import coordinator_core.ops.eol.repair as mod

    d = _new_fixture_repo(tmp_path)
    peer_content = b"alpha\r\nbeta\r\nPEER-EDIT\r\n"
    real_index_blobs = mod._index_blobs

    def racing_index_blobs(root, rels):
        result = real_index_blobs(root, rels)
        # Simulate a peer's concurrent write landing in the window between
        # this op's read and its write.
        (root / "a.txt").write_bytes(peer_content)
        return result

    monkeypatch.setattr(mod, "_index_blobs", racing_index_blobs)

    result = mod._eol_repair({"target_root": str(d), "mutate": True})

    skipped_by_path = {s["path"]: s for s in result["skipped"]}
    assert skipped_by_path["a.txt"]["reason"] == "changed-under-us"
    assert (d / "a.txt").read_bytes() == peer_content


# ---------------------------------------------------------------------------
# F3 -- subdirectory target_root is refused
# ---------------------------------------------------------------------------


def test_subdirectory_target_root_is_refused(tmp_path):
    d = _new_fixture_repo(tmp_path)
    sub = d / "sub"
    sub.mkdir()
    (sub / "keep.txt").write_bytes(b"hi\n")
    _git(d, "add", "sub/keep.txt")
    _git(d, "commit", "-qm", "sub")

    with pytest.raises(PathEscapeError):
        _eol_repair({"target_root": str(sub)})


# ---------------------------------------------------------------------------
# F9 -- bounded runtime: candidate count capped per invocation
# ---------------------------------------------------------------------------


def test_candidate_count_is_capped_per_invocation(tmp_path, monkeypatch):
    import coordinator_core.ops.eol.repair as mod

    d = _new_fixture_repo(tmp_path)  # a.txt and b.txt are clean candidates
    monkeypatch.setattr(mod, "_MAX_REPAIR_CANDIDATES", 1)

    result = mod._eol_repair({"target_root": str(d), "mutate": True})

    assert result["capped_remainder_count"] == 1
    assert result["repaired_count"] == 1
    assert (
        result["repaired_count"] + result["skipped_count"] + result["capped_remainder_count"]
        == result["violation_count"]
    )


# ---------------------------------------------------------------------------
# F11 -- a path containing a newline is dropped, not batched
# ---------------------------------------------------------------------------


def test_newline_in_path_is_skipped_with_explicit_reason(tmp_path, monkeypatch):
    """`_index_blobs`'s `cat-file --batch` protocol is newline-delimited;
    a tracked path containing `\\n` would desync it. Rather than exercise
    that against a real newline-containing filename (fragile across
    filesystems), stub `_census` to hand `repair()` a violation carrying
    one directly, proving the guard fires before any git spawn or disk
    read is attempted for that path."""
    import coordinator_core.ops.eol.repair as mod

    d = _new_fixture_repo(tmp_path)
    fake_census_result = {
        "violations": [
            {"path": "weird\npath.txt", "declared": "lf", "found": "crlf-present", "dirty": False},
        ],
        "violation_count": 1,
    }
    monkeypatch.setattr(mod, "_census", lambda root: fake_census_result)

    result = mod.repair(d, mutate=True)

    skipped_by_path = {s["path"]: s for s in result["skipped"]}
    assert skipped_by_path["weird\npath.txt"]["reason"] == "newline-in-path"
    assert result["repaired_count"] == 0

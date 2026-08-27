"""coordinator_core.ops.eol.tests.test_census -- tests for `census()`, the
FUNCTION.

`eol.census` the OP ID is a K-062 gravestone (state/kill-ledger.md): the trio
collapsed to the single `eol.repair`, which imports and calls `census()` on
every run. So the mechanism is still on the hot path and still needs these
tests -- what went away is the second op id over it, not the code.

Shape:
  - gravestone defence -- the op id stays out of _REGISTRY and
    OP_CLASSIFICATION. Registration/dispatch/classification tests for the op id
    itself were removed with the id; do not reinstate them.
  - negative paths -- missing target_root -> ValueError; a propagating
    PathEscapeError is not swallowed.
  - end-to-end round trip against a fixture repo carrying one violation of EACH
    direction plus one dirty file that must be reported (flagged dirty, never
    dropped -- census reports dirty violations, repair is what skips them).
  - F1 binary-content guard, both halves.

Spawns real `git` against a fixture repo -- tiered off the per-commit path
per this repo's spawn ratchet (coordinator_core/tests/test_no_new_spawning_tests.py).

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C2, C6
"""

from __future__ import annotations

import subprocess

import pytest

# ---------------------------------------------------------------------------
# Imported deliberately: if someone re-decorates census.py, this import is
# what makes the gravestone defence below see it.
# ---------------------------------------------------------------------------
import coordinator_core.ops.eol.census  # noqa: F401 -- imported so the gravestone defence sees any restored @register_op

from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.eol.census import _eol_census, census
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
# gravestone defence
# ---------------------------------------------------------------------------


def test_op_id_stays_gravestoned():
    """K-062 defence. `eol.census` the OP ID is a gravestone (state/kill-ledger.md);
    `census()` the FUNCTION survives because `eol.repair` imports and calls it.
    Re-decorating this module puts back the second registration the collapse
    removed -- a second op id over one mechanism -- so assert its absence rather
    than leaving the door unlatched."""
    assert _OP_NAME not in _REGISTRY, (
        f"{_OP_NAME!r} is registered again -- it is a K-062 gravestone. If a caller "
        "genuinely needs a census against an arbitrary root, that is a param on "
        "eol.repair, not a restored op id."
    )
    assert _OP_NAME not in OP_CLASSIFICATION, (
        f"{_OP_NAME!r} has an OP_CLASSIFICATION entry again -- see above."
    )


# ---------------------------------------------------------------------------
# negative paths
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
# end-to-end round trip
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
# F1 -- binary-content guard, both halves
# ---------------------------------------------------------------------------


def test_wildcard_eol_crlf_over_binary_content_is_not_a_violation(tmp_path):
    """Regression for the critical finding: a wildcard `* text=auto
    eol=crlf` declaration makes `check-attr eol` answer `crlf` for a binary
    path too, and `text=auto` reports the literal "auto" macro value from
    `check-attr text` rather than a per-file binary verdict -- so the
    `text`-attribute guard alone cannot exclude this path. The NUL-byte
    belt-and-braces guard must."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "core.autocrlf", "false")
    (d / ".gitattributes").write_bytes(b"* text=auto eol=crlf\n")
    _git(d, "add", ".gitattributes")
    _git(d, "commit", "-qm", "attrs")

    # b"\n" present, no b"\r\n" at all, and a NUL byte -- the shape that
    # would otherwise be flagged "crlf declared, lf-only found" (a real
    # eol.repair candidate) if the belt-and-braces guard did not exclude it.
    binary_content = b"\x00\x01\x02fake\nbinary\x00content\n"
    (d / "image.bin").write_bytes(binary_content)
    _git(d, "add", "image.bin")
    _git(d, "commit", "-qm", "binary")

    result = census(d)

    assert "image.bin" not in {v["path"] for v in result["violations"]}


def test_explicit_binary_attribute_is_not_a_violation(tmp_path):
    """The other half of F1: a path with an explicit `binary` macro (which
    implies `-text`) reports `text: unset` from `check-attr`, distinct from
    the `text=auto` case above -- the `text`-attribute guard must exclude
    it directly, without needing the NUL-byte fallback."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "config", "core.autocrlf", "false")
    (d / ".gitattributes").write_bytes(
        b"* text=auto eol=crlf\nblob.bin binary\n"
    )
    _git(d, "add", ".gitattributes")
    _git(d, "commit", "-qm", "attrs")

    # No NUL byte here -- if the text-attribute guard were absent, only the
    # belt-and-braces NUL check would be left, and this content would slip
    # past it, proving the two guards are independently necessary.
    binary_like_content = b"fake\nbinary-ish\ncontent\n"
    (d / "blob.bin").write_bytes(binary_like_content)
    _git(d, "add", "blob.bin")
    _git(d, "commit", "-qm", "binary")

    result = census(d)

    assert "blob.bin" not in {v["path"] for v in result["violations"]}


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
        _eol_census({"target_root": str(sub)})


# ---------------------------------------------------------------------------
# F4 -- census reads tracked files uncached
# ---------------------------------------------------------------------------


def test_census_reads_tracked_files_uncached(tmp_path, monkeypatch):
    d = _new_fixture_repo(tmp_path)
    seen_kwargs = {}
    import coordinator_core.ops.eol.census as mod

    real = mod.tracked_files_bytes

    def spy(root, pathspec=".", use_cache=True):
        seen_kwargs["use_cache"] = use_cache
        return real(root, pathspec, use_cache=use_cache)

    monkeypatch.setattr(mod, "tracked_files_bytes", spy)

    census(d)

    assert seen_kwargs.get("use_cache") is False

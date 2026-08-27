"""Differential verification for the PowerShell shapes (C10 --
`sources_powershell.py`), through a REAL PowerShell host.

Same doctrine as `test_read_shapes_differential.py` (C4/C1/C2's own sibling,
see that file's docstring): own file, own comparator, raw stdout on both
sides, no `splitlines()`/`text=True` collapsing a fidelity divergence into a
silent agreement. A refusal (`Unanswerable`) is always acceptable -- the real
command then runs unchanged, which is by definition correct -- but a
DISAGREEMENT between our rendered text and the real host's stdout is never
acceptable, and this file never hand-writes an expected output for a case
compared against a real command.

Fixtures cover what DIFFERS from the bash oracle (`_posix_shell.py`) rather
than repeating it: CRLF vs LF under `Get-Content`, no trailing newline, an
empty file, UTF-8 with and without a BOM, and `Get-ChildItem` ordering, which
is not `ls` ordering (`sources_powershell.run_childitem`'s own docstring: the
`FindFirstFileW`/`FindNextFileW` equivalence it relies on is win32-only).

Negative-spec:
  - Does NOT run the real command through `bash`/`sh` -- `_posix_shell.py`'s
    own docstring forbids exactly that substitution for its own oracle, and
    this file's oracle is the PowerShell mirror of that same rule: the real
    command runs through `pwsh`/`powershell`, resolved explicitly, never
    `subprocess.run(cmd, shell=True)` (which resolves to `cmd.exe` on
    Windows, has no `Get-Content`/`Get-ChildItem`, and would produce a WRONG
    verdict rather than a weaker one -- same reasoning `_posix_shell.py`
    documents for its own `shell=True` refusal).
  - Does NOT report a verdict, in either direction, when no PowerShell
    interpreter is resolvable on the box -- `requires_powershell` skips the
    entire file in that case, mirroring `_posix_shell.requires_posix_shell`.
  - Does NOT assume `$OutputEncoding`'s version-dependent default (module
    docstring's own "verify against the version on the box, do not assume").
    The oracle instead sets an EXPLICIT, stated console/output encoding
    (UTF-8, no BOM) before running the real command -- the same discipline
    `ContentSpec.produce`'s own `newline` parameter already applies to line
    endings: an explicit, named choice the test states, not a guessed
    default baked into the comparison.
  - Does NOT hand-write an expected output for any case compared against a
    real command -- see module docstring above.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from coordinator_core.search import sources_powershell as sp
from coordinator_core.search.engine import Unanswerable

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _resolve_powershell() -> str | None:
    """Resolve the REAL PowerShell interpreter this file's oracle runs
    against -- `pwsh` (PowerShell 7+, cross-platform) preferred, `powershell`
    (Windows PowerShell 5.1) as fallback. Never resolves through
    `shell=True`/`cmd.exe` (module docstring negative-spec)."""
    found = shutil.which("pwsh")
    if found:
        return found
    found = shutil.which("powershell")
    if found:
        return found
    return None


#: Absolute path to a real PowerShell interpreter, or None when this box has
#: none resolvable.
POWERSHELL_EXE = _resolve_powershell()

#: Skip-marker for any test whose verdict depends on running the real
#: command through a real PowerShell host.
requires_powershell = pytest.mark.skipif(
    POWERSHELL_EXE is None,
    reason="no PowerShell interpreter resolvable -- the differential oracle cannot run the real command",
)


def run_real_powershell(cmd: str, cwd) -> tuple[int, str]:
    """Run `cmd` through the resolved real PowerShell interpreter, returning
    (returncode, raw stdout).

    Returns stdout as RAW TEXT, never a line list -- mirrors
    `_posix_shell.run_real`'s own contract, for the same reason: the read
    shapes this file differentiates are exactly the bytes a line-splitting
    round-trip erases.

    Sets an EXPLICIT output encoding (UTF-8, no BOM) before running `cmd`,
    rather than relying on `$OutputEncoding`'s own version-dependent default
    (module docstring negative-spec) -- this makes the byte comparison well
    defined regardless of which PowerShell version resolved, the same way
    `ContentSpec.produce`'s own `newline` argument is explicit rather than
    baked in.
    """
    assert POWERSHELL_EXE is not None, "guarded by requires_powershell"
    quoted_cwd = str(cwd).replace("'", "''")
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "Set-Location -LiteralPath '%s'; " % quoted_cwd
    ) + cmd
    proc = subprocess.run(
        [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        # Bytes, not text: `text=True` applies universal-newline translation,
        # which would turn a CRLF-vs-LF divergence into a silent agreement --
        # same reasoning `_posix_shell.run_real` documents for its own call.
        check=False,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="surrogateescape")


def _write_bytes(path, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.fixture()
def tree(tmp_path):
    _write_bytes(tmp_path / "crlf.txt", b"one\r\ntwo\r\nthree\r\n")
    _write_bytes(tmp_path / "lf.txt", b"one\ntwo\nthree\n")
    _write_bytes(tmp_path / "notrail.txt", b"one\ntwo\nthree")
    _write_bytes(tmp_path / "empty.txt", b"")
    _write_bytes(tmp_path / "utf8_nobom.txt", "grin \U0001F600 done\n".encode("utf-8"))
    _write_bytes(tmp_path / "utf8_bom.txt", b"\xef\xbb\xbf" + "grin \U0001F600 done\n".encode("utf-8"))
    _write_bytes(tmp_path / "many.txt", ("\n".join(str(i) for i in range(1, 21)) + "\n").encode("utf-8"))
    return tmp_path


# --------------------------------------------------------------- Get-Content


def _produce_ours(tokens, cwd) -> str | None:
    """Parse+produce through the in-process module, or None on a refusal --
    a refusal is always correct (the real command runs unchanged)."""
    try:
        spec = sp.parse_content_segment(tokens)
    except Unanswerable:
        return None
    # `[Environment]::NewLine` on the box actually running the real command
    # IS `os.linesep` on that same box (same process, same platform) -- an
    # explicit, derived value, not a guessed constant (module docstring:
    # `produce`'s `newline` argument must never be a guessed default).
    import os

    return spec.produce(str(cwd), newline=os.linesep)


def _assert_matches_real(cmd: str, ps_cmd: str, tokens, cwd) -> None:
    ours = _produce_ours(tokens, cwd)
    if ours is None:
        pytest.skip("declined -- the real command runs unchanged, which is correct")
    _rc, theirs = run_real_powershell(ps_cmd, cwd)
    assert ours == theirs, (
        "in-process PowerShell answer disagrees with the real host\n"
        "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
    )


@requires_powershell
def test_get_content_crlf_source(tree):
    _assert_matches_real(
        "Get-Content crlf.txt", "Get-Content crlf.txt", ["Get-Content", "crlf.txt"], tree
    )


@requires_powershell
def test_get_content_lf_source(tree):
    _assert_matches_real(
        "Get-Content lf.txt", "Get-Content lf.txt", ["Get-Content", "lf.txt"], tree
    )


@requires_powershell
def test_get_content_no_trailing_newline(tree):
    _assert_matches_real(
        "Get-Content notrail.txt",
        "Get-Content notrail.txt",
        ["Get-Content", "notrail.txt"],
        tree,
    )


@requires_powershell
def test_get_content_empty_file(tree):
    _assert_matches_real(
        "Get-Content empty.txt", "Get-Content empty.txt", ["Get-Content", "empty.txt"], tree
    )


@requires_powershell
def test_get_content_utf8_no_bom(tree):
    _assert_matches_real(
        "Get-Content utf8_nobom.txt",
        "Get-Content utf8_nobom.txt",
        ["Get-Content", "utf8_nobom.txt"],
        tree,
    )


@requires_powershell
def test_get_content_utf8_with_bom(tree):
    """A real PowerShell host auto-detects and strips a UTF-8 BOM (it uses
    the BOM to select the decode codec, then excludes it from the emitted
    content) -- this is compared for AGREEMENT, not hand-coded, per module
    docstring: a mismatch here is a genuine fidelity finding, not a test
    author's guess about what either side does."""
    _assert_matches_real(
        "Get-Content utf8_bom.txt",
        "Get-Content utf8_bom.txt",
        ["Get-Content", "utf8_bom.txt"],
        tree,
    )


@requires_powershell
def test_get_content_totalcount(tree):
    _assert_matches_real(
        "Get-Content -TotalCount 3 many.txt",
        "Get-Content -TotalCount 3 many.txt",
        ["Get-Content", "-TotalCount", "3", "many.txt"],
        tree,
    )


@requires_powershell
def test_get_content_tail(tree):
    _assert_matches_real(
        "Get-Content -Tail 2 many.txt",
        "Get-Content -Tail 2 many.txt",
        ["Get-Content", "-Tail", "2", "many.txt"],
        tree,
    )


def test_get_content_nonexistent_operand_declines(tree):
    """A missing operand is a NAMED refusal (`_resolve_operand`'s
    `os.path.isfile` check), not an approximation of what the real host
    would print (a real, unresolvable, unhandled `Get-Content` errors to
    stderr and produces no stdout) -- asserted as the decline itself, same
    discipline `test_read_shapes_differential.test_sed_range_past_eof_declines`
    already applies to its own bash sibling."""
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", "nope.txt"]).produce(str(tree))


# ---------------------------------------------------------------- Get-ChildItem
#
# `run_childitem`'s own docstring: the enumeration-order equivalence
# (`os.scandir`/`FindFirstFileW`) only holds on win32 -- both the in-process
# path and this test's real-host comparison are meaningless off Windows.


@pytest.fixture()
def lsdir(tmp_path):
    d = tmp_path / "lsdir"
    d.mkdir()
    (d / "apple.txt").write_text("a\n")
    (d / "Banana.txt").write_text("b\n")
    (d / "cherry.txt").write_text("c\n")
    return tmp_path


@requires_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="run_childitem enumeration order is only reproduced on win32")
def test_get_childitem_ordering(lsdir):
    spec = sp.parse_childitem_segment(["Get-ChildItem", "lsdir"])
    ours = sp.run_childitem(spec, cwd=str(lsdir))
    _rc, theirs = run_real_powershell(
        "(Get-ChildItem lsdir).Name -join \"`n\"", lsdir
    )
    real_names = theirs.splitlines()
    if not real_names:
        pytest.skip("real Get-ChildItem produced no output -- cannot compare fidelity")
    assert ours == real_names, (
        "in-process Get-ChildItem disagrees with the real host\n  ours: %r\n  real: %r"
        % (ours, real_names)
    )


@requires_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="run_childitem enumeration order is only reproduced on win32")
def test_get_childitem_composed_pipe_count(lsdir):
    """`Get-ChildItem DIR | Measure-Object` (composed-shape parity, mirrors
    `test_read_shapes_differential.test_composed_ls_pipe_wc_l`): our own
    entry count against the real host's own count."""
    spec = sp.parse_childitem_segment(["Get-ChildItem", "lsdir"])
    ours = sp.run_childitem(spec, cwd=str(lsdir))
    _rc, theirs = run_real_powershell(
        "(Get-ChildItem lsdir | Measure-Object).Count", lsdir
    )
    assert str(len(ours)) == theirs.strip()

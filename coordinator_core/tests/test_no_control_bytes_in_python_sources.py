"""Guard: no tracked `*.py` carries a raw C0 control byte.

WHY THIS GUARD EXISTS (empirical, and the instrument it replaces was dead)
    On 2026-09-01, `close_out_and_stamp.py` was found holding a literal
    0x08 byte inside a regex whose own docstring said it wanted a word
    boundary:

        _AC_UNRESOLVED_STATUS_RE = re.compile(
            r"^(?:$|(?:pending|todo|tbd|open)<0x08>)", re.IGNORECASE)

    Somewhere between an author's keyboard and the file, the two characters
    `\\` and `b` collapsed into the single byte they name. The pattern then
    demanded a backspace after the vocabulary word and matched nothing any
    real plan can contain, so the AC-table desync ADVISORY -- the surface a
    cross-repo memo had just been negotiated over -- never fired for a cell
    reading `pending`, `todo`, `tbd`, or `open`. Four of its own tests were
    red on it and were not being read as a defect.

    Two siblings carried the same corruption, and the second is the one that
    names the real risk:

        coordinator_core/invoke/tests/test_warm_fail_hard.py
          assert not re.search(r"...[a-z]*<0x08>", stderr), stderr

    An `assert not re.search(...)` over a pattern that cannot match is a
    guard that passes unconditionally. A lost backslash does not raise, does
    not fail to compile, and does not read wrong at a glance -- it turns a
    live check into a decoration. That is the failure class this guard
    covers, and it is why the guard proves itself against a planted
    violation below rather than only asserting the repo is clean: a scanner
    that has never been seen to fire is the same dead instrument one layer
    up.

WHY THE CLASS IS NARROW, AND WHY `*.py` ONLY
    This is not a "no unusual characters" lint. The failure is specifically
    an escape sequence that lost its backslash in a file whose PARSER then
    reads the survivor as data -- a regex, a path, a format string. That
    makes the blast radius behavioural, and it makes Python the surface
    worth gating: five tracked `*.md` files carry stray control bytes today
    (archived handoffs, sent memos, an archived spec), where the consumer is
    a human reading prose and the byte is cosmetic. Gating them would train
    readers to append exemptions, which is the habit
    `test_gitattributes_eol_coverage.py`'s own docstring warns against for
    the same reason.

    Tab, LF and CR are excluded because they are ordinary source bytes.
    Everything else below 0x20, plus 0x7F, is in. NUL is in the class and
    `-a` is passed so a NUL-bearing file cannot duck the scan by being
    classified binary.

COST
    One `git grep` spawn, PCRE, pathspec-scoped to `*.py`. Measured
    2026-09-01 on a loaded box: 317ms / 365ms / 406ms / 497ms wall against
    4761 tracked `*.py` (86.3MB), on a box where a bare `git --version`
    measured 149-870ms in the same window -- so the scan's marginal cost
    over process creation is small and the spread is peer load, per
    CLAUDE.md's own note that wall clock measures the neighbours.

    The obvious alternative was measured and rejected: reading every tracked
    `*.py` from Python and scanning the bytes in-process costs 585ms to
    enumerate plus 6959ms to read -- 7.5s against a 500ms bar. `git grep`
    does the same work in C, threaded, and is one spawn rather than 4761
    opens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_BACKSLASH = chr(92)

CONTROL_BYTE_CLASS = (
    "["
    + _BACKSLASH + "x00-" + _BACKSLASH + "x08"
    + _BACKSLASH + "x0b" + _BACKSLASH + "x0c"
    + _BACKSLASH + "x0e-" + _BACKSLASH + "x1f"
    + _BACKSLASH + "x7f"
    + "]"
)
"""The scanned class, built from `chr(92)` rather than written as a literal.

Deliberate, and the reason is this module's own subject matter: a source line
spelling the pattern `"[\\x00-\\x08...]"` is exactly the shape that loses its
backslashes in transit through a shell layer, and it would do so silently --
the class would still compile, still scan, and still find nothing. Assembling
it from an explicit byte value makes that failure impossible to introduce by
copy-paste. `test_scanner_sees_a_planted_control_byte` is the second half of
the same defence."""


def _scan(repo_root: Path) -> list[str]:
    """Return the tracked `*.py` paths under `repo_root` carrying a control
    byte, as `git grep -l` reports them.

    Fails loud rather than empty on anything that is not a clean hit/no-hit:
    `git grep` exits 0 with matches, 1 with none, and >1 on a real error
    (notably a git built without PCRE, where `-P` is refused). Treating a
    refusal as "no matches" would rebuild the dead instrument this guard
    exists to catch."""
    proc = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-a",
            "-P",
            "-e",
            CONTROL_BYTE_CLASS,
            "--",
            "*.py",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if proc.returncode > 1:
        raise AssertionError(
            "git grep could not run the control-byte scan "
            f"(exit {proc.returncode}): {proc.stderr.strip()!r}. "
            "A git built without PCRE cannot honour -P; this guard reports "
            "that rather than passing on an unrun scan."
        )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_no_tracked_python_source_carries_a_control_byte():
    offenders = _scan(REPO_ROOT)
    assert offenders == [], (
        "Tracked *.py carrying a raw C0 control byte (tab/LF/CR excluded): "
        + ", ".join(offenders)
        + ". Almost always an escape that lost its backslash -- `\\b` "
        "becoming 0x08 is the observed case. Read the byte's context "
        "before repairing: in a regex it changes what the pattern matches, "
        "in a path it changes what the path names."
    )


def test_scanner_sees_a_planted_control_byte(tmp_path):
    """The guard proves it can fire -- see the module docstring's WHY block
    for the dead-instrument failure this proves against.

    The fixture COMMITS. `git grep` against a repo with no HEAD reports no
    matches for a worktree file that is merely staged, so an uncommitted
    fixture would make this test pass for the wrong reason and take the
    proof with it."""
    repo = tmp_path / "planted"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )

    git("init", "-q")
    (repo / "clean.py").write_bytes(b"x = 1\n")
    (repo / "backspace.py").write_bytes(b'PATTERN = r"open' + bytes([8]) + b'"\n')
    (repo / "prose.md").write_bytes(b"a comment about " + bytes([8]) + b"\n")
    git("add", "-A")
    git("-c", "user.email=guard@test", "-c", "user.name=guard", "commit", "-qm", "planted")

    offenders = _scan(repo)

    assert offenders == ["backspace.py"], (
        "The scanner must see the planted 0x08 in backspace.py, must leave "
        "clean.py alone, and must not reach prose.md -- the pathspec is "
        f"*.py by design. Got: {offenders!r}"
    )


def test_a_scan_that_could_not_run_raises_rather_than_reading_clean(tmp_path):
    """`_scan`'s `returncode > 1` arm, exercised against the real binary.

    Restored after two reviewers disagreed about it, and the disagreement was
    settled by measurement rather than by preference. The overengineering pass
    argued the arm needed no test of its own because a git that cannot honour
    `-P` would make the other two tests raise anyway. That is true only on a
    machine that has such a git, and this fleet has none -- so on every machine
    that actually runs this suite the arm had no coverage at all. Measured by
    deleting the `raise` outright: both surviving tests still passed. A
    fail-loud branch nothing exercises can be deleted silently, which is the
    same shape as the dead assertion this whole module exists to prevent.

    Exercised through a real `git grep` rather than a faked exit code, which
    answers the reviewer's actual objection: a monkeypatched 128 only feeds the
    branch the constant it tests for. A directory that is not a git repository
    makes the real binary exit 128 on its own, so this pins the behaviour end
    to end -- and a scan that returned `[]` here, rather than raising, would be
    reporting a clean tree it never looked at."""
    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()

    with pytest.raises(AssertionError, match="could not run the control-byte scan"):
        _scan(not_a_repo)


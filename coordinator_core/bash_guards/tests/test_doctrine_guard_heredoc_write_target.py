"""P143-T10 -- the doctrine-surface Bash guard on a Python heredoc write.

`check` missed one case and over-fired on another (`docs/plans/2026-09-22-
inbox-blitz-bundled-xs-s-fixes-2026-09-11.md` P143-T10):

  * MISS: `python3 <<PY ... PY` (no `-` flag) reads its heredoc as the
    program exactly like `python3 - <<PY` does -- POSIX/CPython behaviour,
    not a heuristic -- but `_STDIN_PROGRAM_RE` required the `-` flag, so a
    governed path assembled from a variable BOUND inside such a body
    (`p = '<gov>'` then `open(p, 'w')`) evaded every leg: the assignment
    line carries no write marker on its own, and the write line's own text
    never mentions the governed name (it dereferences `p`). Reproduced
    quoted and unquoted (the delimiter's quoting controls shell expansion
    of the body, never whether `python3` reads it as its program).

  * OVER-FIRE: a heredoc whose body is plain DATA redirected into an
    UNGOVERNED file (`cat > script.py <<'EOF' ... EOF`) denied whenever that
    body merely CONTAINED write-shaped text mentioning a governed path --
    even though nothing in the command executes it. The main deny loop
    classified every heredoc body as live text regardless of whether the
    heredoc's own introducing command ever runs it.

Negative-spec: this file does NOT assert on deny TEXT (the composer's own
concern -- see `test_guard_doctrine_surface_point4_by_sink.py`'s own
negative-spec), does NOT re-cover the quoted-heredoc INDIRECTION leg
(`test_guard_doctrine_surface_point3_quoted_heredoc_indirection.py` owns
that), and does NOT re-cover point 4's bash-level variable indirection
(`test_guard_doctrine_surface_point4_by_sink.py` owns that) -- this file is
scoped to the heredoc-as-program-vs-heredoc-as-data discriminant alone.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

SURFACES = ["coordinator/snippets/em-operating-doctrine.md"]
IDENTIFIERS = guard._governed_identifiers_lower(SURFACES)
GOV = SURFACES[0]
UNGOVERNED = "script.py"


def _heredoc(body: str, *, delimiter: str, intro: str) -> str:
    return f"{intro}<<{delimiter}\n{body}\n{delimiter.strip(chr(39)).strip(chr(34))}"


DENY_CASES = [
    (
        "python heredoc, no `-` flag, quoted delimiter, variable-bound write",
        _heredoc(
            f"p = '{GOV}'\nopen(p, 'w').write('x')",
            delimiter="'PY'",
            intro="python3 ",
        ),
    ),
    (
        "python heredoc, no `-` flag, unquoted delimiter, variable-bound write",
        _heredoc(
            f"p = '{GOV}'\nopen(p, 'w').write('x')",
            delimiter="PY",
            intro="python3 ",
        ),
    ),
    (
        "python heredoc, no `-` flag, direct literal write (regression control)",
        _heredoc(
            f"open('{GOV}', 'w').write('x')",
            delimiter="'PY'",
            intro="python3 ",
        ),
    ),
    (
        "python heredoc, `-` flag, no `-` needed but present (regression control)",
        _heredoc(
            f"open('{GOV}', 'w').write('x')",
            delimiter="'PY'",
            intro="python3 - ",
        ),
    ),
    (
        "data heredoc redirected AT the governed surface itself (regression control)",
        _heredoc("corrupted", delimiter="'EOF'", intro=f"cat > {GOV} "),
    ),
]

ALLOW_CASES = [
    (
        "data heredoc, quoted delimiter, ungoverned target, body looks like a write",
        _heredoc(
            f"open('{GOV}', 'w').write('x')",
            delimiter="'EOF'",
            intro=f"cat > {UNGOVERNED} ",
        ),
    ),
    (
        "data heredoc, unquoted delimiter, ungoverned target, body looks like a write",
        _heredoc(
            f"open('{GOV}', 'w').write('x')",
            delimiter="EOF",
            intro=f"cat > {UNGOVERNED} ",
        ),
    ),
    (
        "data heredoc via tee, ungoverned target, body looks like a write",
        _heredoc(
            f"open('{GOV}', 'w').write('x')",
            delimiter="'EOF'",
            intro=f"tee {UNGOVERNED} ",
        ),
    ),
]


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_python_heredoc_write_denies(label, cmd):
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, label


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_data_heredoc_to_ungoverned_target_allows(label, cmd):
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, label


def test_strip_data_heredoc_bodies_keeps_program_body_live():
    program = "python3 <<'PY'\nopen('x', 'w')\nPY"
    assert guard._strip_data_heredoc_bodies(program) == program

    data = "cat > out.txt <<'EOF'\nopen('x', 'w')\nEOF"
    stripped = guard._strip_data_heredoc_bodies(data)
    assert "open('x', 'w')" not in stripped
    assert stripped.startswith("cat > out.txt <<'EOF'")
    assert stripped.endswith("EOF")

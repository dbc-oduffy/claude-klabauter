"""Differential verification for the in-process search answer path.

The binding correctness question for this package is NOT "does the answer look right"
-- it is "does the answer match what the command the agent actually typed would have
printed". So every test here runs the REAL command through a shell and compares, rather
than asserting against hand-written expected output. A hand-written expectation encodes
the author's belief about grep; a differential comparison encodes grep.

Refusals are always acceptable (`answer()` returning None means the real command runs
unchanged, which is by definition correct). A DISAGREEMENT is never acceptable, and is
what these tests exist to catch.
"""

from __future__ import annotations

import os
import textwrap

import pytest

from coordinator_core.search.answer import answer
from coordinator_core.search.tests._posix_shell import requires_posix_shell, run_real

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FIXTURE_FILES = {
    "alpha.py": textwrap.dedent(
        """\
        import os

        def check_alpha(value):
            return value + 1

        def check_beta(value):
            # TODO: handle None
            return value - 1
        """
    ),
    "beta.py": textwrap.dedent(
        """\
        def check_gamma():
            pass

        CONSTANT = "alpha"
        """
    ),
    "notes.md": textwrap.dedent(
        """\
        # Notes
        alpha appears here
        and beta appears here
        a line with a + plus sign
        a line with a | pipe character
        """
    ),
    "nested/deep.txt": "alpha nested\nbeta nested\n",
    # Review: coordinator:code-reviewer -- F7(d): matches spaced >1 line apart within
    # one file, to exercise -A/-B/-C group-separator ("--") insertion (F4). Deliberately
    # uses a pattern ("zeta") not shared with any other fixture, so these files don't
    # perturb the "alpha"-recursive-walk cases (e.g. `grep -rn alpha . | head -2`,
    # which is order-sensitive on the number and identity of matching files).
    "spaced.txt": "one\nzeta two\nthree\nfour\nfive\nzeta six\nseven\n",
    # F7(d): matches whose -A1 windows are adjacent (window1 end == window2 start - 1)
    # -- must MERGE into one group with no "--" separator, not just avoid a false one.
    "adjacent.txt": "one zeta\ntwo\nthree zeta\nfour\n",
    # F7(d): matches whose -A1/-B1 windows OVERLAP -- the shared line must render once,
    # not twice.
    "overlap.txt": "l1\nl2 zeta\nl3 zeta\nl4\n",
}

#: Commands exercised differentially, tagged with whether their output order is part of
#: the contract. Recursive-walk cases have no cross-implementation directory-order
#: guarantee and compare as multisets; every other case's line order is significant
#: (F6) and compares in-order, so an ordering bug or a missing/duplicated structural
#: line (e.g. a dropped "--" separator, F4) cannot hide behind `sorted()`.
CASES = [
    ("grep -n alpha notes.md", False),
    ("grep -rn alpha .", True),
    ("grep -rln alpha .", True),
    ("grep -c alpha notes.md", False),
    ("grep -in ALPHA notes.md", False),
    ("grep -rn --include=*.py check .", True),
    ("grep -n -A1 alpha notes.md", False),
    ("grep -n -B1 beta notes.md", False),
    ("grep -vn alpha notes.md", False),
    ("grep -rn alpha . | head -2", True),
    ("grep -n alpha notes.md | grep -v nested", False),
    ("grep -rnE 'alpha|beta' .", True),
    ("grep -rn 'a + plus' .", True),
    ("grep -n 'a | pipe' notes.md", False),
    ("grep -rnw alpha .", True),
    ("grep -rn nomatchanywhere .", True),
    # F7(a): -c on a zero-match target must still print "0" (F1), not omit the line.
    ("grep -c nomatchanywhere notes.md", False),
    ("grep -c nomatchanywhere alpha.py beta.py", False),
    # F7(a) sibling: -l on zero matches correctly omits the file (unaffected by F1).
    ("grep -l nomatchanywhere notes.md", False),
    # F7(d): non-adjacent matches within one file under -A/-B/-C get a "--" separator.
    ("grep -n -A1 zeta spaced.txt", False),
    # F7(d): adjacent windows merge -- no separator, no duplicated line.
    ("grep -n -A1 zeta adjacent.txt", False),
    # F7(d): overlapping -A1/-B1 windows merge -- the shared line renders once.
    ("grep -n -A1 -B1 zeta overlap.txt", False),
    # F7(f): multiple explicitly-named files exercises the filename-prefix rule outside
    # the recursive-walk path.
    ("grep -n alpha alpha.py beta.py", False),
    # B1 (2026-08-06 architecture-survey digest #1): a shell glob operand reaches this
    # seam UNEXPANDED, and was previously scanned as a literal filename -- yielding an
    # authoritative "(no matches)" for a search that never ran. Differential by
    # construction: the real command's shell expands the glob, ours must agree.
    ("grep -n alpha *.py", False),
    ("grep -n check *.py", False),
    ("grep -n alpha nested/*.txt", False),
    # F7(g): -m/--max-count.
    ("grep -n -m1 zeta spaced.txt", False),
    ("grep -rn --max-count=1 alpha .", True),
]


@pytest.fixture()
def tree(tmp_path):
    for relative, content in FIXTURE_FILES.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


def _real(cmd: str, cwd) -> tuple[int, list[str]]:
    returncode, stdout = run_real(cmd, cwd)
    return returncode, stdout.splitlines()


def _answered_body(cmd: str, cwd) -> list[str] | None:
    text = answer(cmd, cwd=str(cwd))
    if text is None:
        return None
    body = text.split("\n\n[searched in-process")[0]
    if body == "(no matches)":
        return []
    return body.splitlines()


@pytest.mark.parametrize("cmd,recursive", CASES)
@requires_posix_shell
def test_answer_matches_real_command(cmd, recursive, tree):
    ours = _answered_body(cmd, tree)
    if ours is None:
        pytest.skip("declined -- the real command runs unchanged, which is correct")
    _rc, theirs = _real(cmd, tree)
    if recursive:
        # Recursive walks have no cross-implementation ordering guarantee; compare as
        # multisets so a legitimate ordering difference is not reported as a defect.
        assert sorted(ours) == sorted(theirs), (
            "in-process answer disagrees with real command\n"
            "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
        )
    else:
        # F6: non-recursive/single-target output order IS part of the contract --
        # sorting here would mask an ordering bug or a missing/duplicated structural
        # line (e.g. a dropped "--" group separator) that sorts identically either way.
        assert ours == theirs, (
            "in-process answer disagrees with real command\n"
            "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
        )


@requires_posix_shell
def test_wc_count_agrees_but_padding_deliberately_diverges(tree):
    """`wc -l`: the VALUE must agree; BSD's width-8 padding deliberately does not.

    Reproducing the host's padding would require probing the host's own `wc` -- a
    process spawn, which is the cost this package exists to remove. Asserted as an
    explicit, named divergence so it cannot later be mistaken for a defect or, worse,
    silently "fixed" into a host probe.
    """
    ours = _answered_body("grep -rn alpha . | wc -l", tree)
    assert ours is not None
    _rc, theirs = _real("grep -rn alpha . | wc -l", tree)
    assert [ln.strip() for ln in ours] == [ln.strip() for ln in theirs]
    assert ours == [ours[0].strip()], "our own output must never be padded"


def test_declines_when_grep_is_fed_by_upstream(tree):
    """`<cmd> | grep` cannot be answered -- the input does not exist yet.

    The upstream here must be a command this package does NOT serve. The original
    spelling used `cat notes.md | grep alpha`, which was a faithful decline only for as
    long as `cat` was unservable; once the read shapes are served (C1/C3) that pipeline
    became a legitimate read-source + grep-stage answer, and the assertion started
    encoding the absence of a feature rather than the rule it names. The RULE is what is
    pinned here: an upstream whose output does not exist yet cannot feed an answer. A
    served upstream is verified positively in `test_read_shapes_differential.py`.
    """
    assert answer("curl https://example.com | grep alpha", cwd=str(tree)) is None


def test_declines_on_semicolon_compound(tree):
    """Other real work is sequenced around the grep; answering half would drop it."""
    assert answer("grep -n alpha notes.md ; echo done", cwd=str(tree)) is None


def test_declines_on_unabsorbable_downstream_stage(tree):
    assert answer("grep -rn alpha . | xargs wc -l", cwd=str(tree)) is None


def test_declines_on_pcre(tree):
    assert answer("grep -Pn '\\d+' notes.md", cwd=str(tree)) is None


def test_declines_bare_wc_no_flags(tree):
    """F7(b)/F2: bare `wc` prints three numbers, not a line count -- must decline."""
    assert answer("grep -rn alpha . | wc", cwd=str(tree)) is None


def test_declines_downstream_grep_context_flags(tree):
    """F7(c)/F3: -A/-B/-C on a downstream filter grep is not derivable from already-
    rendered piped lines -- must decline rather than silently drop the flag."""
    assert answer("grep -n alpha notes.md | grep -A1 nested", cwd=str(tree)) is None


def test_declines_include_on_explicitly_named_target(tree):
    """F7(e)/F5: --include/--exclude semantics genuinely diverge between GNU and BSD
    grep for an explicitly-named (non-directory) target -- must decline rather than
    pick a dialect."""
    assert answer("grep --include=*.py -n alpha notes.md", cwd=str(tree)) is None


@requires_posix_shell
def test_multi_path_glob_is_not_a_silent_empty_answer(tree):
    """B1 regression, stated as the failure it closes rather than as a diff.

    The defect was not "a glob answers slightly differently" -- it was that a glob
    matching several files answered `(no matches)` with `[searched in-process:
    1 file(s)]`, which reads as an authoritative negative. A refusal here would be
    acceptable to the module's contract; a confident empty answer never is.
    """
    text = answer("grep -n alpha *.py", cwd=str(tree))
    if text is None:
        pytest.fail("declining is legal in general, but this shape must stay answerable")
    assert "(no matches)" not in text
    assert "1 file(s)" not in text

    _rc, theirs = _real("grep -n alpha *.py", tree)
    assert _answered_body("grep -n alpha *.py", tree) == theirs
    # The filename prefix is a post-expansion property: two operands reach grep, so
    # every line is prefixed even though the command as typed named one.
    assert all(line.startswith(("alpha.py:", "beta.py:")) for line in theirs)


def test_declines_on_glob_matching_nothing(tree):
    """Bash without `nullglob` passes the pattern through and grep exits 2 on stderr --
    a shape this seam cannot reproduce, so it must refuse rather than print nothing."""
    assert answer("grep -n alpha *.rs", cwd=str(tree)) is None


def test_declines_on_nonexistent_target(tree):
    """Same stderr/exit-2 shape as an unmatched glob, reached by a plain operand."""
    assert answer("grep -n alpha does_not_exist.py", cwd=str(tree)) is None


def test_prunes_dot_git_by_default(tree):
    """Pruning is a capability win, and it is deliberately a DIVERGENCE from grep."""
    git_dir = tree / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("alpha in git metadata\n")
    ours = _answered_body("grep -rn alpha .", tree)
    assert ours is not None
    assert not any(".git" in line for line in ours)


def test_truncation_forces_refusal_for_aggregate_stage(tree, monkeypatch):
    """A truncated search feeding `wc -l` would produce a confidently wrong count."""
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_MATCH_LINES", 1)
    assert answer("grep -rn alpha . | wc -l", cwd=str(tree)) is None


def test_truncation_is_tolerated_for_head(tree, monkeypatch):
    """`head` only needs the leading lines, so a capped search still answers it."""
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_MATCH_LINES", 50)
    ours = _answered_body("grep -rn alpha . | head -1", tree)
    assert ours is not None
    assert len(ours) == 1

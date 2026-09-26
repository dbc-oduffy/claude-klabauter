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
    "spaced.txt": "one\nzeta two\nthree\nfour\nfive\nzeta six\nseven\n",
    "adjacent.txt": "one zeta\ntwo\nthree zeta\nfour\n",
    "overlap.txt": "l1\nl2 zeta\nl3 zeta\nl4\n",
}

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
    ("grep -c nomatchanywhere notes.md", False),
    ("grep -c nomatchanywhere alpha.py beta.py", False),
    ("grep -l nomatchanywhere notes.md", False),
    ("grep -n -A1 zeta spaced.txt", False),
    ("grep -n -A1 zeta adjacent.txt", False),
    ("grep -n -A1 -B1 zeta overlap.txt", False),
    ("grep -n alpha alpha.py beta.py", False),
    # seam UNEXPANDED, and was previously scanned as a literal filename -- yielding an
    ("grep -n alpha *.py", False),
    ("grep -n check *.py", False),
    ("grep -n alpha nested/*.txt", False),
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
        assert sorted(ours) == sorted(theirs), (
            "in-process answer disagrees with real command\n"
            "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
        )
    else:
        assert ours == theirs, (
            "in-process answer disagrees with real command\n"
            "  command : %s\n  ours    : %r\n  real    : %r" % (cmd, ours, theirs)
        )


@requires_posix_shell
def test_wc_count_agrees_but_padding_deliberately_diverges(tree):
    ours = _answered_body("grep -rn alpha . | wc -l", tree)
    assert ours is not None
    _rc, theirs = _real("grep -rn alpha . | wc -l", tree)
    assert [ln.strip() for ln in ours] == [ln.strip() for ln in theirs]
    assert ours == [ours[0].strip()], "our own output must never be padded"


def test_declines_when_grep_is_fed_by_upstream(tree):
    assert answer("curl https://example.com | grep alpha", cwd=str(tree)) is None


def test_declines_on_semicolon_compound(tree):
    assert answer("grep -n alpha notes.md ; echo done", cwd=str(tree)) is None


def test_declines_on_unabsorbable_downstream_stage(tree):
    assert answer("grep -rn alpha . | xargs wc -l", cwd=str(tree)) is None


def test_declines_on_pcre(tree):
    assert answer("grep -Pn '\\d+' notes.md", cwd=str(tree)) is None


def test_declines_bare_wc_no_flags(tree):
    assert answer("grep -rn alpha . | wc", cwd=str(tree)) is None


def test_declines_downstream_grep_context_flags(tree):
    assert answer("grep -n alpha notes.md | grep -A1 nested", cwd=str(tree)) is None


def test_declines_include_on_explicitly_named_target(tree):
    assert answer("grep --include=*.py -n alpha notes.md", cwd=str(tree)) is None


@requires_posix_shell
def test_multi_path_glob_is_not_a_silent_empty_answer(tree):
    text = answer("grep -n alpha *.py", cwd=str(tree))
    if text is None:
        pytest.fail("declining is legal in general, but this shape must stay answerable")
    assert "(no matches)" not in text
    assert "1 file(s)" not in text

    _rc, theirs = _real("grep -n alpha *.py", tree)
    assert _answered_body("grep -n alpha *.py", tree) == theirs
    assert all(line.startswith(("alpha.py:", "beta.py:")) for line in theirs)


def test_declines_on_glob_matching_nothing(tree):
    assert answer("grep -n alpha *.rs", cwd=str(tree)) is None


def test_declines_on_nonexistent_target(tree):
    assert answer("grep -n alpha does_not_exist.py", cwd=str(tree)) is None


def test_prunes_dot_git_by_default(tree):
    """Pruning is a capability win, and it is deliberately a DIVERGENCE from grep."""
    git_dir = tree / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("alpha in git metadata\n")
    ours = _answered_body("grep -rn alpha .", tree)
    assert ours is not None
    assert not any(".git" in line for line in ours)


def test_declines_empty_answer_when_default_pruned_dir_could_hold_the_match(tree):
    node_modules = tree / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.js").write_text("needle only lives here\n")
    assert answer("grep -rn needle .", cwd=str(tree)) is None


def test_answers_empty_when_no_default_prune_dir_present(tree):
    text = answer("grep -rn zzzznosuchneedle .", cwd=str(tree))
    assert text is not None
    assert text.startswith("(no matches)")


def test_downstream_grep_filter_declines_on_truncated_upstream(tree, monkeypatch):
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_MATCH_LINES", 1)
    assert answer("grep -rn alpha . | grep check", cwd=str(tree)) is None


def test_truncation_forces_refusal_for_aggregate_stage(tree, monkeypatch):
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_MATCH_LINES", 1)
    assert answer("grep -rn alpha . | wc -l", cwd=str(tree)) is None


def test_truncation_is_tolerated_for_head(tree, monkeypatch):
    from coordinator_core.search import engine

    monkeypatch.setattr(engine, "MAX_MATCH_LINES", 50)
    ours = _answered_body("grep -rn alpha . | head -1", tree)
    assert ours is not None
    assert len(ours) == 1

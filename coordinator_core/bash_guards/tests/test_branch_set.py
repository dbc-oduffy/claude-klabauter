"""Tests for coordinator_core.bash_guards._branch_set -- the branch-set
enumerator with fast-bail ordering (plan
docs/plans/2026-08-01-branch-creation-seam-guards.md chunk C3).

Covers: for-each-ref row parsing into (name, epoch) pairs, current-branch
exclusion, non-canonical-shape exclusion, empty-output handling, the
single-subprocess cost-discipline property (no git call at import time / when
the public functions are never invoked), and `ahead_of_main` spending at most
one `rev-list --count` call for the single named branch.
"""

from __future__ import annotations

import ast
import inspect
import subprocess

from coordinator_core.bash_guards import _branch_set


class _FakeGit:
    """Injectable git-call recorder. Each entry in `responses` is keyed by
    the joined argv (space-separated) and maps to a (returncode, stdout)
    pair; a call whose argv is not a registered key returns (1, "")."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list = []

    def __call__(self, args, timeout=15, cwd=None):
        self.calls.append(list(args))
        key = " ".join(args)
        returncode, stdout = self.responses.get(key, (1, ""))
        return subprocess.CompletedProcess(args=["git", *args], returncode=returncode, stdout=stdout, stderr="")


def _for_each_ref_args(main_ref: str) -> list:
    return [
        "for-each-ref",
        f"--no-merged={main_ref}",
        "--format=%(refname:short) %(committerdate:unix)",
        "refs/heads/",
    ]


def test_parses_name_and_epoch_rows(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-parse --abbrev-ref HEAD": (0, "work/host/2026-07-30\n"),
            " ".join(_for_each_ref_args("origin/main")): (
                0,
                "work/host/2026-07-20 1721500000\nwork/host/2026-07-25 1721900000\n",
            ),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    result = _branch_set.other_canonical_branches()

    assert result == [
        ("work/host/2026-07-20", 1721500000),
        ("work/host/2026-07-25", 1721900000),
    ]


def test_excludes_current_branch(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-parse --abbrev-ref HEAD": (0, "work/host/2026-07-25\n"),
            " ".join(_for_each_ref_args("origin/main")): (
                0,
                "work/host/2026-07-20 1721500000\nwork/host/2026-07-25 1721900000\n",
            ),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    result = _branch_set.other_canonical_branches()

    assert result == [("work/host/2026-07-20", 1721500000)]


def test_excludes_non_canonical_shapes(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-parse --abbrev-ref HEAD": (0, "main\n"),
            " ".join(_for_each_ref_args("origin/main")): (
                0,
                "work/host/2026-07-20 1721500000\n"
                "feature/foo 1721600000\n"
                "hotfix/bar 1721700000\n",
            ),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    result = _branch_set.other_canonical_branches()

    assert result == [("work/host/2026-07-20", 1721500000)]


def test_empty_output_yields_empty_result(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-parse --abbrev-ref HEAD": (0, "main\n"),
            " ".join(_for_each_ref_args("origin/main")): (0, ""),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    assert _branch_set.other_canonical_branches() == []


def test_malformed_rows_are_dropped_while_good_row_survives(monkeypatch):
    """`_parse_for_each_ref_row` returns None on a malformed row (blank
    line, missing epoch, non-integer epoch) rather than raising -- pin the
    None-return contract the function's own docstring advertises.
    # Review: coordinator:code-reviewer -- P2, malformed-row robustness was
    untested (Finding 3)
    """
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-parse --abbrev-ref HEAD": (0, "main\n"),
            " ".join(_for_each_ref_args("origin/main")): (
                0,
                "\n"
                "work/host/2026-07-20 1721500000\n"
                "work/host/missing-epoch\n"
                "work/host/bad-epoch not-an-int\n",
            ),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    result = _branch_set.other_canonical_branches()

    assert result == [("work/host/2026-07-20", 1721500000)]


def test_no_resolvable_main_ref_yields_empty_result_and_no_for_each_ref_call(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (1, ""),
            "rev-parse main": (1, ""),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    assert _branch_set.other_canonical_branches() == []
    for_each_ref_calls = [c for c in fake.calls if c and c[0] == "for-each-ref"]
    assert for_each_ref_calls == []


def test_no_module_level_git_call_in_source(monkeypatch):
    """Cost discipline (plan ruling R3): no `_git(` call appears at module
    level (i.e. outside a function/class body) in `_branch_set.py`'s source
    -- the real structural guarantee that this module spends zero
    subprocesses at import/load time. Checked via AST inspection of the
    module's top-level statements, since a monkeypatched call-log assertion
    made without ever invoking the module cannot distinguish "no import-time
    call" from "no call was made at all."
    # Review: coordinator:code-reviewer -- P2, prior version asserted a fake
    call log was empty without ever invoking the module (Finding 2)
    """
    source = inspect.getsource(_branch_set)
    tree = ast.parse(source)

    def _is_git_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_git"
        )

    for top_level_node in tree.body:
        if isinstance(top_level_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(top_level_node):
            assert not _is_git_call(node), (
                f"module-level `_git(` call found: {ast.dump(node)}"
            )


def test_ahead_of_main_spends_exactly_one_rev_list_call(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (0, "abc123\n"),
            "rev-list --count origin/main..work/host/2026-07-20": (0, "3\n"),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    ahead = _branch_set.ahead_of_main("work/host/2026-07-20")

    assert ahead == 3
    rev_list_calls = [c for c in fake.calls if c and c[0] == "rev-list"]
    assert len(rev_list_calls) == 1


def test_ahead_of_main_no_resolvable_main_ref_returns_zero(monkeypatch):
    fake = _FakeGit(
        {
            "rev-parse origin/main": (1, ""),
            "rev-parse main": (1, ""),
        }
    )
    monkeypatch.setattr(_branch_set, "_git", fake)

    assert _branch_set.ahead_of_main("work/host/2026-07-20") == 0
    rev_list_calls = [c for c in fake.calls if c and c[0] == "rev-list"]
    assert rev_list_calls == []

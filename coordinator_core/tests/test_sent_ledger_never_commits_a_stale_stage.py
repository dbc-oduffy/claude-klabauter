"""
coordinator_core.tests.test_sent_ledger_never_commits_a_stale_stage — the
performing guard for P1 `09cf57f3b909`: no production module may commit
`state/memo-outbox/sent-ledger.jsonl` through a STAGE-WINS route.

WHAT THIS PINS, and why the class matters more than the instance
================================================================
The sent-ledger is fleet-shared and append-only: every sending session on
this box appends its row to the one file, under `locked_rmw`'s cross-process
exclusive lock, so the WORKTREE copy is always the union of every completed
append. An INDEX entry for it is the opposite — it freezes at whatever was
staged and goes stale the instant anybody else appends.

`git_native.commit_scoped` reads a staged blob DIFFERING FROM HEAD as a
deliberate partial stage and preserves it. That is right for a hand-staged
hunk and wrong for this file, and `memo.send` called it until 2026-08-30:
measured on the live tree that day, ten consecutive memo.send commits landed
the byte-identical stale blob b2a5f7d9d (2376 rows) while every one of their
parents held a richer one, so HEAD lost each row appended in between — the
sender's own included, since its append never reached the commit either.

Fixing the one call site closes the instance. This gate closes the CLASS: any
future module that learns about the ledger and reaches for a stage-wins
committer fails here rather than silently regressing HEAD again. The failure
mode it guards is invisible in review — both routes commit, both return a
sha, and only the committed BYTES differ.

THE DISCRIMINATOR IS `index != HEAD`, NOT `index != worktree`
=============================================================
Recorded because the bug row got this wrong, and the error costs a reader a
green test that proves nothing: `commit_scoped` treats a staged blob
differing from HEAD as a deliberate stage. An index differing merely from the
WORKTREE is an ordinary unstaged edit and takes the safe `git add` branch —
so a reproduction built on the "the path is also staged" framing PASSES
against the broken code. The behavioural arm of this defect is pinned by
`ops/fleet/tests/test_memo_send.py ::
test_a_stale_staged_ledger_blob_is_not_committed_over_the_worktree`, which
uses a stale stage and fails pre-fix with HEAD at `['first']`.

Why AST, not grep
=================
A substring grep cannot tell a call from prose naming the same function —
this module's own docstring says `commit_scoped` several times and must not
trip its own gate. AST sees only materialized calls, the same discipline
`test_no_bare_chain_terminal_literal.py` and `test_no_hardcoded_paths.py`
already use.

Negative-spec: this gate does NOT forbid `commit_scoped` anywhere else. It
fires only on a module that ALSO names the sent-ledger, because the hazard is
the pairing, not either half.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Scanned production surfaces. `coordinator/tests/` is out of scope: a test
#: may legitimately drive `commit_scoped` over a ledger fixture to prove the
#: contrast arm, which is how this defect was measured in the first place.
_SCAN_ROOTS = ("coordinator_core", "coordinator/bin")

#: A module "knows about the ledger" if it materializes either spelling of
#: the path, or references one of the canonical names for it.
_LEDGER_LITERALS = frozenset({
    "sent-ledger.jsonl",
    "state/memo-outbox/sent-ledger.jsonl",
})
_LEDGER_NAMES = frozenset({
    "_SENT_LEDGER_RELPATH",
    "_SENT_LEDGER_REL",
    "_SENT_LEDGER_FILENAME",
    "_sent_ledger_path",
})

#: Routes that commit STAGED bytes when the index diverges from HEAD.
#: `explicit_stage` is included because it is the other half of the pipeline
#: pair: it deliberately declines to re-stage a diverged path precisely so a
#: stage-wins committer can preserve it, so its presence beside the ledger is
#: the same hazard arriving one layer up.
_STAGE_WINS_ROUTES = frozenset({
    "commit_scoped",
    "run_commit_pipeline",
    "explicit_stage",
})


def _python_files():
    for root in _SCAN_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if name.endswith(".py"):
                    yield Path(dirpath) / name


def _called_names(tree: ast.AST) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


def _names_the_ledger(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in _LEDGER_LITERALS:
                return True
        elif isinstance(node, ast.Name) and node.id in _LEDGER_NAMES:
            return True
        elif isinstance(node, ast.Attribute) and node.attr in _LEDGER_NAMES:
            return True
    return False


def _stage_wins_violations(files) -> list:
    """(relpath, sorted routes) for every ledger-aware module that calls a
    stage-wins committer."""
    violations = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        if not _names_the_ledger(tree):
            continue
        bad = _called_names(tree) & _STAGE_WINS_ROUTES
        if bad:
            violations.append((path.relative_to(_REPO_ROOT).as_posix(), sorted(bad)))
    return violations


def test_no_ledger_aware_module_commits_through_a_stage_wins_route():
    violations = _stage_wins_violations(_python_files())
    assert violations == [], (
        "these modules name the sent-ledger AND call a stage-wins committer, "
        "which is the arm that dropped rows from HEAD on the live tree "
        "(P1 09cf57f3b909):\n"
        + "\n".join(f"  {rel}: {routes}" for rel, routes in violations)
        + "\n\nThe sent-ledger's worktree copy is the union of every session's "
        "appends; a staged blob for it is stale by construction. Commit it "
        "with `git.commit.commit_paths` (worktree-wins, zero spawns), and "
        "never name it in `prefer_staged`."
    )


def test_the_ledger_is_never_named_in_a_staged_bytes_preference():
    """`prefer_staged` is the declared opt-in that selects STAGED bytes for a
    named path, and `prefer_deliberate_stage` widens the same substitution to
    every diverged path in the call. Either one applied to the ledger picks
    the losing arm deliberately — the same committed bytes `commit_scoped`
    produced, reached through the sanctioned door, so the gate above cannot
    see it."""
    offenders = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        ledger_aware = _names_the_ledger(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "prefer_staged":
                    named = any(
                        (isinstance(sub, ast.Constant)
                         and isinstance(sub.value, str)
                         and sub.value in _LEDGER_LITERALS)
                        or (isinstance(sub, ast.Name) and sub.id in _LEDGER_NAMES)
                        or (isinstance(sub, ast.Attribute)
                            and sub.attr in _LEDGER_NAMES)
                        for sub in ast.walk(kw.value)
                    )
                elif kw.arg == "prefer_deliberate_stage":
                    # A blanket True in a module that commits the ledger
                    # substitutes staged bytes for it without naming it.
                    named = ledger_aware and (
                        isinstance(kw.value, ast.Constant) and kw.value.value is True
                    )
                else:
                    continue
                if named:
                    offenders.append(
                        f"{path.relative_to(_REPO_ROOT).as_posix()}:"
                        f"{node.lineno} ({kw.arg})"
                    )
    assert offenders == [], (
        "the sent-ledger must never be handed to a staged-bytes preference — "
        "its worktree copy is the union, the staged blob is a stale snapshot:"
        "\n  " + "\n  ".join(offenders)
    )


def test_the_gate_fires_on_a_planted_violation(tmp_path):
    """A gate nobody has seen fail is a gate nobody knows works. This plants
    the exact pre-2026-08-30 `memo.send` shape — a module that names the
    ledger and commits it via `commit_scoped` — and drives the SAME oracle
    the live scan uses over it."""
    planted = tmp_path / "planted_sender.py"
    planted.write_text(
        "_SENT_LEDGER_RELPATH = 'state/memo-outbox/sent-ledger.jsonl'\n"
        "def send(worktree, msg_file):\n"
        "    return git_native.commit_scoped(\n"
        "        ['sent/x.md', _SENT_LEDGER_RELPATH], msg_file, worktree,\n"
        "    )\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    assert _names_the_ledger(tree), "the planted module must be ledger-aware"
    assert _called_names(tree) & _STAGE_WINS_ROUTES == {"commit_scoped"}, (
        "the planted module must trip the stage-wins half of the oracle"
    )


def test_the_gate_ignores_a_stage_wins_call_that_is_not_ledger_aware():
    """The pairing is the hazard, not either half. `commit_scoped` remains
    legitimate for a caller preserving a genuine hand-staged hunk, and this
    gate must not be read as deprecating it."""
    planted = ast.parse(
        "def commit(paths, msg, cwd):\n"
        "    return git_native.commit_scoped(paths, msg, cwd)\n"
    )
    assert not _names_the_ledger(planted)
    assert _called_names(planted) & _STAGE_WINS_ROUTES == {"commit_scoped"}

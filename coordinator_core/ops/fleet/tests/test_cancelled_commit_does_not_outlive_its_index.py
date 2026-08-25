"""A private index must never be unlinked while its `git commit` is still running.

WHY THIS EXISTS — this is the ROOT CAUSE behind both empty-tree firings, as
distinct from `test_pathspec_less_commit_seams_are_guarded.py`, which pins the
refusal that those firings prompted:

- 2026-08-10 `0a3462b72` (`session.boot_sweep`) and 2026-08-18 `fbfbd061d`
  (`fleet.archive_actioned_memos`) each committed git's canonical empty tree
  onto a shared, already-pushed branch. The second deleted 26,264 tracked files.
- The mechanism, established 2026-08-19: op dispatch cancels the handler on
  timeout (`ipc.py :: dispatch_message`), asyncio does NOT kill the spawned
  `git commit`, and the handler's `finally:` then unlinks `GIT_INDEX_FILE` out
  from under the still-running child. git resolves that path AFTER the
  pre-commit hook returns, so the unlink lands inside the hook window and
  `git write-tree` yields the empty tree at rc=0.

The fix is an ORDERING one: kill the child, then unlink. This file exists
because ordering is exactly the kind of thing a later refactor reverses without
noticing — moving the unlink up, or dropping the kill while "simplifying" the
cleanup, silently re-arms a bomb that has already gone off twice.

WHY THE GUARD ALONE IS NOT ENOUGH — `_empty_private_index_breach` runs BEFORE
the commit is spawned; the unlink lands AFTER. It is TOCTOU against this race
and passes while the empty tree still commits. Both defences are required and
neither replaces the other; see that function's own TIME-OF-CHECK/TIME-OF-USE
note.

SCOPE — deliberately NON-SPAWNING. The kill path is exercised against stub
processes rather than real children, and the ordering is asserted statically
over the AST. The end-to-end reproduction (slow pre-commit hook, cancelled
await, observed `4b825dc…` at rc=0) was run during the 2026-08-19 post-mortem
and is recorded in `docs/problems/2026-08-18-the-empty-tree-commit-bomb.md`;
re-running it per-commit would spawn real git under the 50–70-session load norm
for no added signal, which is precisely what the spawn ratchet
(`coordinator_core/tests/test_no_new_spawning_tests.py`) exists to prevent.

NEGATIVE-SPEC: do NOT "fix" a failure here by deleting the kill call and
relying on `_empty_private_index_breach`, and do NOT reach for `asyncio.shield`
around the commit — the awaiting coroutine still receives `CancelledError`, so
the `finally:` still runs and still unlinks a live child's index.

Spec backlink: docs/problems/2026-08-18-the-empty-tree-commit-bomb.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import _kill_orphaned_commit

ENGINE_ROOT = Path(__file__).resolve().parents[4]
KILL_NAME = "_kill_orphaned_commit"


class _StubProc:
    """Stands in for `asyncio.subprocess.Process` — records whether kill() ran.

    Duck-typed deliberately: `_kill_orphaned_commit` reads only `returncode`
    and calls `kill()`, so a real child is unnecessary to pin the contract.
    """

    def __init__(self, returncode=None, raises=None):
        self.returncode = returncode
        self._raises = raises
        self.killed = False

    def kill(self):
        self.killed = True
        if self._raises is not None:
            raise self._raises


# ---------------------------------------------------------------------------
# The kill contract
# ---------------------------------------------------------------------------


def test_a_still_running_commit_is_killed():
    proc = _StubProc(returncode=None)
    _kill_orphaned_commit(proc, "archive_and_commit")
    assert proc.killed, (
        "a commit still running (returncode is None) was NOT killed before its "
        "private index would be unlinked — this is the exact window that "
        "deleted 26,264 files on 2026-08-18"
    )


def test_a_finished_commit_is_left_alone():
    proc = _StubProc(returncode=0)
    _kill_orphaned_commit(proc, "archive_and_commit")
    assert not proc.killed, (
        "a commit that already completed must not be killed — its returncode "
        "is set, so there is no live child and nothing to race"
    )


def test_a_failed_but_finished_commit_is_left_alone():
    proc = _StubProc(returncode=1)
    _kill_orphaned_commit(proc, "rm_and_commit")
    assert not proc.killed


def test_no_commit_in_flight_is_safe():
    _kill_orphaned_commit(None, "rm_and_commit")


@pytest.mark.parametrize("exc", [ProcessLookupError(), OSError("gone")])
def test_a_child_that_died_between_check_and_kill_does_not_break_cleanup(exc):
    """The unlink after this call must still happen — cleanup cannot be skipped."""
    proc = _StubProc(returncode=None, raises=exc)
    _kill_orphaned_commit(proc, "archive_and_commit")
    assert proc.killed


# ---------------------------------------------------------------------------
# The ordering, asserted over the engine's own AST
# ---------------------------------------------------------------------------


def _iter_private_index_sources():
    """Engine modules that drive a private index — the failure class."""
    for path in sorted((ENGINE_ROOT / "coordinator_core").rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "GIT_INDEX_FILE" not in source:
            continue
        try:
            yield path, ast.parse(source)
        except SyntaxError:  # pragma: no cover - engine must parse
            continue


#: Substrings that name a private-index path variable. Both spellings are
#: listed deliberately: matching only "idx" would let a future rename of
#: `idx_path` to the arguably more idiomatic `index_path` drop that seam out of
#: this walk's view entirely — producing no offender line and no failure, just
#: silence. Widen this set rather than narrowing it.
_INDEX_VAR_HINTS = ("idx", "index")


def _is_index_unlink(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if getattr(node.func, "attr", "") != "unlink":
        return False
    return any(
        isinstance(a, ast.Name)
        and any(h in a.id.lower() for h in _INDEX_VAR_HINTS)
        for a in node.args
    )


def _is_kill_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        getattr(node.func, "id", "") == KILL_NAME
        or getattr(node.func, "attr", "") == KILL_NAME
    )


def _arg_literals(call: ast.Call) -> list[str]:
    out: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
    return out


def _has_pathspec_less_commit(fn: ast.AST) -> bool:
    """True when this function spawns a `git commit` with NO trailing pathspec.

    That is the catastrophic shape and the reason this file exists. WITH a
    pathspec a lost index commits nothing; WITHOUT one it commits git's empty
    tree, i.e. deletes every tracked file. See `_empty_private_index_breach`.

    `commit-tree` counts as the same shape, unconditionally: the HEAD-race CAS
    ladder (2026-08-23) replaced both fleet seams' `git commit` with
    `write-tree` → `commit-tree` → `update-ref`, and this walk keying only on
    the literal `"commit"` went blind on both of them the moment it landed —
    caught here because both tests below fail loud on an empty walk rather
    than passing vacuously. The hazard did not move with the argv: `write-tree`
    against a lost index still yields git's canonical empty tree, and
    `commit-tree` then lands exactly that. A pathspec cannot rescue it either,
    since `commit-tree` takes none by construction — so there is no
    `"--"` escape hatch to check for.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
        if name not in {
            "create_subprocess_exec", "run", "Popen", "check_call",
            "check_output", "call",
        }:
            continue
        lits = _arg_literals(node)
        if "commit-tree" in lits:
            return True
        if "commit" not in lits:
            continue
        if not any(lit == "--" for lit in lits):
            return True
    return False


def _index_cleanup_finallys():
    """Yield (path, funcname, kill_line, unlink_line) per at-risk index cleanup.

    SCOPE — only functions that ALSO carry a pathspec-less `git commit`, which
    is the class that deletes the repo. A private-index seam whose commit has a
    trailing pathspec is deliberately out of scope: with a pathspec, an index
    lost mid-commit makes git commit NOTHING rather than the empty tree, so the
    cancellation race there is a benign no-op rather than a deletion.

    Known and excluded on exactly that basis:
    `coordinator_core/ops/distill_apply_disposal.py`, whose batch commit passes
    `"--", *commit_paths`. If it ever loses that pathspec it starts failing here
    automatically — which is the intended behaviour, not a bug. This mirrors how
    `test_pathspec_less_commit_seams_are_guarded.py` scopes out the two
    real-index bootstrap seams.

    That exclusion is EVIDENCED, not assumed — a 2026-08-19 review challenged it
    on the theory that a missing index would make git treat every pathspec'd
    path as staged-deleted, thereby deleting tracked parent files rather than
    committing nothing. Probed directly on git 2.55.0.windows.4 against
    throwaway repos, both legs:

    - `git commit -- <paths>` with `GIT_INDEX_FILE` naming an ABSENT file, one
      pathspec'd path edited in the worktree: rc=0, the edit committed
      correctly, no path deleted.
    - The same with a path `git rm`-ed into the private index first (distill's
      actual reaped shape), then the index deleted before the commit: rc=0, the
      reaped path deleted as intended, the edited path updated, and an
      unrelated tracked bystander left untouched.

    The reason is the FORWARD-B property itself: WITH a pathspec git reads the
    WORKTREE for those paths rather than the index, so a lost index is simply
    not consulted. The same property that makes a pathspec unusable on the
    fleet seams is what makes its absence harmless here.
    """
    for path, tree in _iter_private_index_sources():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _has_pathspec_less_commit(fn):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try) or not node.finalbody:
                    continue
                unlinks = [
                    n.lineno
                    for stmt in node.finalbody
                    for n in ast.walk(stmt)
                    if _is_index_unlink(n) and hasattr(n, "lineno")
                ]
                if not unlinks:
                    continue
                # Only a BARE top-level `finally:` statement counts. A kill
                # nested in a branch would still satisfy a line-number
                # comparison while providing no runtime guarantee, because
                # ast.walk finds calls regardless of control flow.
                kills = [
                    stmt.value.lineno
                    for stmt in node.finalbody
                    if isinstance(stmt, ast.Expr) and _is_kill_call(stmt.value)
                ]
                yield (
                    path,
                    fn.name,
                    min(kills) if kills else None,
                    min(unlinks),
                )


def test_every_index_cleanup_kills_the_commit_before_unlinking():
    blocks = list(_index_cleanup_finallys())
    assert blocks, (
        "found no `finally:` that unlinks a private index — the AST walk is "
        "broken, not the engine"
    )

    offenders = []
    for path, fn_name, kill_line, unlink_line in blocks:
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        if kill_line is None:
            offenders.append(
                f"{rel}::{fn_name}: unlinks the private index at line "
                f"{unlink_line} without calling {KILL_NAME}() at all"
            )
        elif kill_line > unlink_line:
            offenders.append(
                f"{rel}::{fn_name}: calls {KILL_NAME}() at line {kill_line}, "
                f"AFTER the unlink at line {unlink_line} — the order is the "
                f"entire fix"
            )

    assert not offenders, (
        "private-index cleanup that can unlink an index a live `git commit` "
        "still holds:\n  "
        + "\n  ".join(offenders)
        + "\n\nOn a dispatch timeout the handler is cancelled mid-commit, "
        "asyncio leaves the child running, and unlinking its GIT_INDEX_FILE "
        "makes the pathspec-less commit resolve to git's empty tree at rc=0 — "
        "deleting every tracked file. Call "
        f"{KILL_NAME}(commit_proc, '<caller>') BEFORE the unlink. Do NOT "
        "instead remove the empty-tree guard, and do NOT reach for "
        "asyncio.shield — see this module's NEGATIVE-SPEC."
    )


def test_both_fleet_seams_are_covered():
    """Pins the two seams that actually fired, so the walk cannot silently narrow."""
    covered = {
        (path.name, fn_name)
        for path, fn_name, kill_line, _unlink in _index_cleanup_finallys()
        if kill_line is not None
    }
    seams = {fn for name, fn in covered if name == "_common.py"}
    assert seams == {"archive_and_commit", "rm_and_commit"}, (
        "the two fleet seams that actually committed the empty tree must both "
        "stay covered; found: " + (", ".join(sorted(seams)) or "none")
    )

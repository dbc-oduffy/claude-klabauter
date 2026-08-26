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
    """Engine modules that drive a private index — the failure class.

    Gates on either the literal `GIT_INDEX_FILE` OR a mention of
    `_make_git_env`, the fleet helper that sets it (`idx_path=` -> private
    index). The literal-only gate missed modules that establish a private
    index INDIRECTLY: `push_suggestion.py` imports and calls `_make_git_env`
    but contains no `GIT_INDEX_FILE` literal of its own, so a future edit
    adding `idx_path=` there would establish a genuine private index while
    staying permanently invisible to this walk. Widening to the helper's own
    name is coarser (it also admits shared-index-only callers like
    `push_suggestion.py` today) but visible-and-noisy beats invisible.
    """
    for path in sorted((ENGINE_ROOT / "coordinator_core").rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "GIT_INDEX_FILE" not in source and "_make_git_env" not in source:
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
    """String literals passed to a spawn call — direct AND list/tuple argv.

    Argv-as-list is this engine's DOMINANT convention, not a corner case:
    `git_native.py`'s own `_git(args, *, cwd, env)` and `boot_backstop.py`'s
    `_git(worktree, args)` both take the whole git subcommand+argv as ONE
    positional `ast.List` argument. A walk that only read direct positional
    `ast.Constant` args (the original shape here) saw zero literals for every
    call written this way and silently skipped it — no offender line, no
    failure, just a blind spot. Exposed by `git_native.py::
    commit_with_message_file_pathspec_scoped`, whose sole `_git([...], ...)`
    call has "commit" sitting inside an `ast.List`, invisible to the old walk.
    """
    out: list[str] = []
    for arg in call.args:
        out.extend(_leaf_literal_texts(arg))
        if isinstance(arg, (ast.List, ast.Tuple)):
            for elt in arg.elts:
                out.extend(_leaf_literal_texts(elt))
    return out


def _leaf_literal_texts(node: ast.AST) -> list[str]:
    """A plain string constant, or an f-string's leading literal segment.

    `--pathspec-from-file=<f>` is built as an f-string
    (`f"--pathspec-from-file={pathspec_file}"`,
    `git_native.py::commit_with_message_file_pathspec_scoped`) — the
    interpolated suffix is unknowable statically, but its literal PREFIX is
    an `ast.Constant` inside the `ast.JoinedStr.values` list and is enough to
    match `_has_pathspec`'s `startswith("--pathspec-from-file=")` check. Not
    reading it would make this exact demonstrated call site read as
    pathspec-LESS the moment `_arg_literals` was widened to see it at all.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return [first.value]
    return []


def _has_pathspec(lits: list[str]) -> bool:
    """True when the collected argv literals carry ANY recognised pathspec form.

    The bare `"--"` sentinel (`git commit -- <paths>`) is not the only one in
    production use here: `--pathspec-from-file=<f>` and `--pathspec-file-nul`
    exist specifically because argv-length limits make a trailing
    `"--", *paths` list unsafe at scale (see `git_native.py::
    commit_with_message_file_pathspec_scoped` and `add_paths_pathspec_file`).
    Recognise both alongside the bare form — narrowing this back to `"--"`
    alone reintroduces a false "pathspec-less" positive on every call site
    written this way.
    """
    for lit in lits:
        if lit == "--":
            return True
        if lit.startswith("--pathspec-from-file=") or lit == "--pathspec-file-nul":
            return True
    return False


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
        if not _has_pathspec(lits):
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


#: A synthetic private-index seam carrying the exact hazard shape this file
#: exists to catch: a pathspec-less `commit-tree` spawn whose `finally:`
#: unlinks a private index. Used ONLY to prove `_index_cleanup_finallys`'s
#: detection logic still fires (RE-SITED, 2026-08-26, C7) — see
#: `test_the_ast_walk_detects_a_synthetic_offender` for why: both fleet
#: seams retired their live `commit-tree`/`update-ref` spawns in favour of
#: `_commit_via_head_spine` (C2 `dccf2fc01`, C3 concurrently), which lands
#: fully in-process with no child to orphan — so the engine can now
#: legitimately have ZERO real instances of this hazard, and "found none"
#: stopped being distinguishable from "the walk is broken" on its own.
_SYNTHETIC_OFFENDER_SOURCE = '''
import asyncio
import os


def _make_git_env(idx_path=None):
    return {}


def _kill_orphaned_commit(proc, caller):
    pass


async def _synthetic_seam(worktree_root, idx_path):
    commit_proc = None
    try:
        commit_proc = await asyncio.create_subprocess_exec(
            "git", "commit-tree", "sha",
        )
    finally:
        _kill_orphaned_commit(commit_proc, "synthetic")
        os.unlink(idx_path)
'''


def test_the_ast_walk_detects_a_synthetic_offender(monkeypatch):
    """Proves the detection logic itself still fires, independent of whether
    the real engine currently contains any live instance of the hazard —
    see the module-level comment on `_SYNTHETIC_OFFENDER_SOURCE`. Without
    this, an empty `_index_cleanup_finallys()` result is ambiguous between
    "the hazard is genuinely retired" and "the walk quietly broke", and the
    two tests below can no longer tell those apart from a bare `assert
    blocks`/`assert seams == {...}` the way they could when real specimens
    existed to anchor them."""
    synthetic_tree = ast.parse(_SYNTHETIC_OFFENDER_SOURCE)
    monkeypatch.setattr(
        "coordinator_core.ops.fleet.tests.test_cancelled_commit_does_not_outlive_its_index"
        "._iter_private_index_sources",
        lambda: iter([(Path("synthetic.py"), synthetic_tree)]),
    )
    blocks = list(_index_cleanup_finallys())
    assert blocks, (
        "the AST walk did not detect a synthetic offender carrying the exact "
        "hazard shape (pathspec-less commit-tree spawn + finally: unlinking "
        "an idx-named path) — the detection logic itself is broken"
    )
    names = {fn_name for _path, fn_name, _kill, _unlink in blocks}
    assert names == {"_synthetic_seam"}
    _path, _fn, kill_line, unlink_line = blocks[0]
    assert kill_line is not None and kill_line < unlink_line, (
        "the synthetic fixture's own kill-before-unlink ordering was not "
        "read correctly — fixture or walk logic mismatch"
    )


def test_every_index_cleanup_kills_the_commit_before_unlinking():
    """`blocks` legitimately empty means the hazard has ZERO live instances
    right now — both fleet seams retired their private-index commit-tree
    spawns onto `_commit_via_head_spine`, which lands in process with no
    child to orphan (see `_SYNTHETIC_OFFENDER_SOURCE`'s comment).
    `test_the_ast_walk_detects_a_synthetic_offender` is what proves this is
    not the walk quietly breaking instead — an empty result here no longer
    fails on its own; a NON-empty result with a bad ordering still must."""
    blocks = list(_index_cleanup_finallys())

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


#: See `test_pathspec_less_commit_seams_are_guarded.py`'s identically-named
#: constant — same helper, same reason: a fleet seam that no longer spawns a
#: pathspec-less `git commit`/`commit-tree` under a private index satisfies
#: this file's own subject vacuously unless the replacement is checked for
#: explicitly.
_COMMIT_LANDING_HELPERS = frozenset({"_commit_via_head_spine"})


def _calls_any(fn: ast.AST, names: frozenset) -> bool:
    return any(
        (getattr(c.func, "id", "") in names or getattr(c.func, "attr", "") in names)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    )


def test_both_fleet_seams_are_covered():
    """Pins the two seams that actually fired, so the walk cannot silently
    narrow — RE-SITED 2026-08-26, C7. Each of `archive_and_commit` and
    `rm_and_commit` satisfies this ONE of two ways, and the two must not be
    conflated: (1) still detected by `_index_cleanup_finallys` (a live
    pathspec-less commit-tree spawn with a kill-before-unlink `finally:`);
    or (2) no such spawn at all, but a call to `_commit_via_head_spine` —
    the in-process, spawn-free landing path both seams now use (C2
    `dccf2fc01`, C3 concurrently), which has no child process to orphan and
    so cannot re-arm this exact race. A seam with NEITHER is a silent loss
    of coverage, not a narrowed-but-still-correct walk.
    """
    fleet = ENGINE_ROOT / "coordinator_core" / "ops" / "fleet" / "_common.py"
    source = fleet.read_text(encoding="utf-8", errors="replace")
    fn_by_name = {
        fn.name: fn
        for fn in ast.walk(ast.parse(source))
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    covered = {
        (path.name, fn_name)
        for path, fn_name, kill_line, _unlink in _index_cleanup_finallys()
        if kill_line is not None
    }
    seams = {fn for name, fn in covered if name == "_common.py"}

    for name in ("archive_and_commit", "rm_and_commit"):
        if name in seams:
            continue
        assert name in fn_by_name, f"{name} no longer exists in {fleet.name}"
        assert _calls_any(fn_by_name[name], _COMMIT_LANDING_HELPERS), (
            f"{name} has neither a kill-before-unlink-guarded private-index "
            f"commit spawn nor a call to {sorted(_COMMIT_LANDING_HELPERS)} — "
            f"its commit landing has silently disappeared, or the orphaned-"
            f"commit-vs-unlink race re-armed without this walk noticing."
        )


# ---------------------------------------------------------------------------
# What actually retired the mechanism, pinned so it cannot come back quietly
# ---------------------------------------------------------------------------

#: Spawn helpers used anywhere in this engine to start a git child.
_SPAWN_NAMES = frozenset({
    "create_subprocess_exec", "run", "Popen", "check_call", "check_output",
    "call", "_git", "_run_git",
})


def _porcelain_commit_seams():
    """Yield (path, lineno, has_pathspec) per porcelain `git commit` under a
    private index.

    Porcelain `git commit` is the ONLY form that runs the pre-commit hook, and
    the hook window is the whole mechanism: git resolves `GIT_INDEX_FILE` AFTER
    the hook returns, so an unlink landing inside it makes `write-tree` yield
    the empty tree at rc=0. `commit-tree` is plumbing — it runs no hook and
    takes an already-computed tree sha as an argument, so an index unlinked
    after that sha exists cannot change what lands.
    """
    for path, tree in _iter_private_index_sources():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
            if name not in _SPAWN_NAMES:
                continue
            lits = _arg_literals(node)
            if "commit" not in lits or "commit-tree" in lits:
                continue
            yield path, node.lineno, _has_pathspec(lits)


def test_no_private_index_seam_runs_a_hook_running_commit_without_a_pathspec():
    """The property that closed the empty-tree class — assert it, don't assume it.

    All three firings ran a porcelain `git commit`. None of those call sites
    survives in that form: the two fleet seams were rewritten to the
    `write-tree` -> `commit-tree` -> `update-ref` CAS ladder (2026-08-23), and
    `session.boot_sweep` was rebuilt onto the SHARED index, which nothing
    unlinks. The one porcelain `git commit` left under a private index is
    `distill_apply_disposal`'s, and it carries a trailing pathspec — the shape
    a 2026-08-19 probe on git 2.55.0.windows.4 measured as harmless, because
    with a pathspec git reads the worktree rather than the index.

    So this is not defence-in-depth over a live hazard; it is the pin under a
    hazard that is currently GONE, and whose absence is easy to undo by one
    person swapping a `commit-tree` ladder back for a simpler `git commit`.
    A new pathspec-less porcelain commit under a private index re-arms a bomb
    that has already gone off three times.
    """
    offenders = [
        f"{path.relative_to(ENGINE_ROOT).as_posix()}:{lineno}"
        for path, lineno, has_pathspec in _porcelain_commit_seams()
        if not has_pathspec
    ]
    assert not offenders, (
        "porcelain `git commit` with NO trailing pathspec, under a private "
        "index:\n  " + "\n  ".join(offenders)
        + "\n\nThis is the exact shape behind 0a3462b72, fbfbd061d and "
        "e3e0b857e. Porcelain `git commit` runs the pre-commit hook, and git "
        "resolves GIT_INDEX_FILE only after the hook returns — so a "
        "cancellation-driven unlink landing in that window makes write-tree "
        "yield the empty tree at rc=0 and the pathspec-less commit deletes "
        "every tracked file. Land via write-tree -> commit-tree -> update-ref "
        "against an explicit, already-validated tree sha instead."
    )


def test_the_distill_seam_is_the_only_porcelain_commit_and_keeps_its_pathspec():
    """Pins the surviving porcelain seams, so the walk cannot narrow to zero
    and pass vacuously — and so any of them losing its pathspec fails loud here.

    Widened 2026-08-25 (review of 501c426e1) from a single-seam pin to three:
    fixing `_arg_literals`'s blindness to argv-as-list and `_has_pathspec`'s
    blindness to `--pathspec-from-file=` (findings 1+2) made two more real
    call sites visible for the first time — `git_native.py::
    commit_with_message_file` (bare `"--"`) and `::
    commit_with_message_file_pathspec_scoped` (`--pathspec-from-file=`, an
    f-string, both dedup'd to one `git_native.py` entry below since the pin
    keys on (module, has_pathspec) not line). Widening `_iter_private_index_
    sources`'s module gate to also admit `_make_git_env` mentions (finding 3)
    made a fourth visible — `push_suggestion.py`'s receiver-repo commit (bare
    `"--"`), which is NOT actually under a private index (calls
    `_make_git_env()` with no `idx_path=`) but is swept in by the coarser
    module-level gate; harmless here because it carries a pathspec either way.
    All three newly-visible seams were checked BEFORE this pin was widened:
    every one carries a pathspec, so none is a live instance of the bug this
    file exists to catch. If a seam was legitimately added, removed, or lost
    its pathspec, update this pin deliberately rather than widening the walk
    above."""
    seams = {
        (path.name, has_pathspec)
        for path, _lineno, has_pathspec in _porcelain_commit_seams()
    }
    assert seams == {
        ("distill_apply_disposal.py", True),
        ("git_native.py", True),
        ("push_suggestion.py", True),
    }, (
        "expected exactly the three known porcelain `git commit` seams "
        "(distill_apply_disposal.py, git_native.py, push_suggestion.py), all "
        f"WITH a pathspec. Found: {sorted(seams)}. "
        "If a seam was legitimately added or removed, update this pin "
        "deliberately rather than widening the walk above."
    )

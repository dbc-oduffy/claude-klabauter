"""coordinator_core.hooks.guard_test_tree_git_fixture_spawn — PreToolUse
(Write|Edit|MultiEdit|NotebookEdit) advisory op: fires when a proposed
test-tree edit shells `git` to construct fixture state.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-test-tree-git-fixture-
spawn.py`. That script ran as an in-process guard body enrolled into a
second, doctrine-plane-resident guard registry fired only via
`preuse-write-dispatch.py`'s own dispatch. None of that applies here: this
lands as its own `hooks.<name>` op per this row's body.

ONE shape change, forced by the port: DoE's source vendored a byte-verbatim
COPY of `coordinator/lib/spawn_detect.py` from this engine repo (its own
docstring: "vendored byte-verbatim from the sibling engine repo; imported
here, never edited"). That copy is no longer needed here — this module
imports the CANONICAL original directly,
`coordinator_core.spawn_policy.detect` (`sites_in_source`,
`is_test_tree_site`), never a DoE-repo path or a second copy.

Original PM ruling (2026-08-07, carried over unchanged): "I never said 'no
git'. I said I wanted to end the spawning and deterioration." — this is an
ADVISORY (`allow_advisory`), never a deny; it never blocks the edit, and it
leads with the alternative (a fake-git fixture module, resolved on disk
per-repo) rather than a prohibition.

WHAT FIRES — fixture-construction git call sites only. A `git` argv0 whose
SUBCOMMAND is one of `_FIXTURE_CONSTRUCTION_SUBCOMMANDS` (state-building:
init, config, add, commit, worktree, clone, checkout, reset, branch, tag,
remote, mv, rm, stash, merge, rebase, cherry-pick, push, pull, apply)
fires. A read-only subcommand, or one this hook cannot statically resolve
(a variable, an f-string, a `**`-unpacked list), never fires — fails
toward NOT advising, consistent with an offer rather than a deny.

SURFACING — only the first fixture-construction hit in a proposed edit is
surfaced per invocation; an edit with several such sites gets one advisory,
not one per site.

SCOPE — `coordinator_core.spawn_policy.detect.is_test_tree_site()` is the
SAME shared, structural test-tree predicate the suite-level spawn census
already uses (a `tests/` directory component anywhere, or a `test_*`
basename) — no hardcoded path literal, correct in this repo and any
consuming repo's own test tree.

BOUNDARY NOTE — scoped to the test tree only. Production code legitimately
shells to git (the cross-repo-memo CLI, `scoped_git_commit`, the SessionEnd
hook) — none of those live under a `tests/` directory component or carry a
`test_*` basename, so `is_test_tree_site()` never matches them.

Fail-open (returns `no_advisory()`), in order: `tool_name` not in the
guarded set; no target path; target not test-tree-shaped; `spawn_policy.
detect` unimportable; `after`-content reconstruction ambiguous;
`sites_in_source` raising `SpawnParseError` (an unparseable proposed edit is
not this hook's business); zero fixture-construction git sites found.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
Tripwire token: TEST-TREE-GIT-FIXTURE-SPAWN.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

#: git subcommands that BUILD fixture/repository state. Deliberately an
#: allow-list, not a deny-list: an unrecognised subcommand does not fire.
_FIXTURE_CONSTRUCTION_SUBCOMMANDS = frozenset(
    {
        "init",
        "config",
        "add",
        "commit",
        "worktree",
        "clone",
        "checkout",
        "reset",
        "branch",
        "tag",
        "remote",
        "mv",
        "rm",
        "stash",
        "merge",
        "rebase",
        "cherry-pick",
        "push",
        "pull",
        "apply",
    }
)

#: Read-only history/inspection subcommands — never fires this hook. Not
#: consulted for the fire decision (the allow-list above already excludes
#: them by omission); named here so a future subcommand added to
#: `_FIXTURE_CONSTRUCTION_SUBCOMMANDS` cannot silently also appear here.
_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "show",
        "log",
        "rev-parse",
        "ls-files",
        "diff",
        "status",
        "cat-file",
        "blame",
        "describe",
        "ls-remote",
        "reflog",
        "shortlog",
        "name-rev",
    }
)

assert not (_FIXTURE_CONSTRUCTION_SUBCOMMANDS & _READ_ONLY_SUBCOMMANDS)

#: Basename of the fake-git fixture module, probed for on disk in the tree
#: being edited — never rendered as a literal unless found.
_ANCHOR_BASENAME = "_fake_git.py"

#: This repo's own location for it, probed (not asserted) as the fallback
#: when the edited file's own test tree carries no sibling copy.
_ANCHOR_CANONICAL_RELPATH = "coordinator_core/tests/_fake_git.py"

_TOKEN = "TEST-TREE-GIT-FIXTURE-SPAWN"

_GIT_ARGV0 = frozenset({"git", "git.exe"})

#: `git` global flags that can precede the subcommand token. Flags in this
#: set that take a separate argument consume the following element too.
_GIT_GLOBAL_FLAGS_WITH_ARG = frozenset({"-C", "--git-dir", "--work-tree", "-c"})


def _index_calls_by_lineno(tree: ast.AST) -> "dict[int, list[ast.Call]]":
    index: "dict[int, list[ast.Call]]" = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            index.setdefault(node.lineno, []).append(node)
    return index


def _const_str(node: "ast.AST | None") -> "Optional[str]":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _git_subcommand_at(
    lineno: int, calls_by_lineno: "dict[int, list[ast.Call]]"
) -> "Optional[str]":
    """The literal SUBCOMMAND element of the argv-bearing list/tuple literal
    of a `subprocess.*`-shaped call at `lineno`, if statically resolvable —
    `None` for a dynamic argv."""
    for call in calls_by_lineno.get(lineno, ()):
        if not call.args:
            continue
        argv_node = call.args[0]
        if not isinstance(argv_node, (ast.List, ast.Tuple)):
            continue
        elts = argv_node.elts
        if len(elts) < 2:
            continue
        i = 1
        subcommand: "Optional[str]" = None
        while i < len(elts):
            token = _const_str(elts[i])
            if token is None:
                break
            if token in _GIT_GLOBAL_FLAGS_WITH_ARG:
                i += 2
                continue
            if token.startswith("-"):
                i += 1
                continue
            subcommand = token
            break
        if subcommand:
            return subcommand
    return None


def _fixture_construction_sites(text: str, relpath: str) -> "list[tuple[str, int]]":
    """`[(subcommand, lineno), ...]` for every fixture-construction git
    call site in `text`. Empty (never raises) on any parse failure."""
    try:
        from coordinator_core.spawn_policy import detect as spawn_detect
    except Exception:
        return []

    try:
        sites = spawn_detect.sites_in_source(text, relpath)
    except Exception:
        return []

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    calls_by_lineno = _index_calls_by_lineno(tree)

    hits: "list[tuple[str, int]]" = []
    for site in sites:
        if site.argv0 not in _GIT_ARGV0:
            continue
        subcommand = _git_subcommand_at(site.lineno, calls_by_lineno)
        if subcommand in _FIXTURE_CONSTRUCTION_SUBCOMMANDS:
            hits.append((subcommand, site.lineno))
    return hits


def _display_path(path: Path, root: "Optional[Path]") -> str:
    if root is not None:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _resolve_anchor(chain: "tuple[Path, ...]", root: "Optional[Path]") -> "Optional[str]":
    try:
        for directory in chain:
            candidate = directory / _ANCHOR_BASENAME
            if candidate.is_file():
                return _display_path(candidate, root)
            if directory == root:
                break

        if root is not None:
            canonical = root / _ANCHOR_CANONICAL_RELPATH
            if canonical.is_file():
                return _display_path(canonical, root)
    except Exception:
        return None

    return None


def _resolve_offer_paths(target_raw: str) -> "tuple[str, Optional[str]]":
    """Both paths the offer renders, resolved against the tree the edited
    file lives in. Never renders a path this guard has not seen on disk."""
    try:
        resolved = Path(target_raw).resolve()
        start = resolved.parent
        chain = (start, *start.parents)
        root = next((d for d in chain if (d / ".git").exists()), None)
    except Exception:
        return target_raw, None

    return _display_path(resolved, root), _resolve_anchor(chain, root)


def _advisory_reason(target: str, subcommand: str, lineno: int, anchor: "Optional[str]") -> str:
    alternative = anchor if anchor else "plain files"
    return f"{_TOKEN}: line {lineno} shells git {subcommand} to build fixture state. Use instead: `{alternative}`"


@register_op("hooks.guard_test_tree_git_fixture_spawn")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit|NotebookEdit) op: advise (never deny)
    when a proposed test-tree edit shells `git` to build fixture state."""
    if params.get("tool_name", "") not in _GUARDED_TOOLS:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return no_advisory()

    try:
        from coordinator_core.spawn_policy import detect as spawn_detect
    except Exception:
        return no_advisory()

    normalized = target_raw.replace(os.sep, "/")
    if not spawn_detect.is_test_tree_site(normalized):
        return no_advisory()

    try:
        target_path = Path(target_raw)
        before = (
            target_path.read_text(encoding="utf-8", errors="replace")
            if target_path.is_file()
            else ""
        )
    except Exception:
        before = ""

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    hits = _fixture_construction_sites(after, normalized)
    if not hits:
        return no_advisory()

    subcommand, lineno = hits[0]
    display_target, anchor = _resolve_offer_paths(target_raw)
    context = render(compose(_advisory_reason(display_target, subcommand, lineno, anchor), anchor=anchor))
    return allow_advisory("PreToolUse", context)

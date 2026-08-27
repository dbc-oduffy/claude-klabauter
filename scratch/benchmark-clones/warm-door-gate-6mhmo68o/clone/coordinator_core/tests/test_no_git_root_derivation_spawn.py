"""Collector (class-shaped) for the `git rev-parse` ROOT-DERIVATION shell-out
class: `--show-toplevel` / `--git-dir` / `--absolute-git-dir` /
`--git-common-dir`.

Spec backlink: `docs/plans/2026-08-16-a-process-per-predicate.md`, chunk C4.
Conversion target: `coordinator_core.git.repo_root` (`show_toplevel`,
`git_dir`, `git_common_dir`) landed by chunk C1/C2, plus
`coordinator_core.git.git_dir` (`resolve_git_dir`,
`resolve_git_common_dir`) landed earlier and `coordinator_core.git.
remote_url` (chunk C3, a DIFFERENT rev-parse-adjacent form -- see "Scope"
below). Structural template: `test_no_machine_local_cli_read_spawn.py`
(same class shape: AST-driven call-site census, frozen burn-down inventory,
proof-of-fire + silent-on-converted self-tests, exhaustiveness pin) --
NOT `test_no_unbatched_per_item_git_spawn.py`; see "Not a generalization
of the amplification collector" below for why that collector was checked
first and found not to apply.

NOT A GENERALIZATION OF THE AMPLIFICATION COLLECTOR. `test_no_unbatched_
per_item_git_spawn.py`'s own docstring (`AmpSite`, six detection routes)
was widened by AC11 to "every spawn verb, not just git", and its opening
paragraph invites exactly the question this chunk starts with: does that
generalization already cover this class? It does not, on inspection:

  - That collector's discriminating axis is LOOP-BATCHABILITY -- one spawn
    per loop item, reachable through a local helper/cross-module import/
    injected runner/generic wrapper/default-bound parameter, that COULD be
    hoisted into one call. Its `AmpSite` carries no notion of WHICH rev-parse
    form a site uses, and its frozen `_KNOWN_SITES` inventory keys on
    `(path, enclosing, callee)`, not on argv shape.
  - This chunk's class is ROOT-IDENTITY REDUNDANCY -- ANY call site, loop or
    not, that re-derives the repo toplevel/gitdir/common-dir by spawning
    `git rev-parse`, when a shared cwd-keyed memo (`coordinator_core.git.
    repo_root`) already resolves the identical value for the same cwd
    within the process, mostly WITHOUT spawning at all (the walk-based
    path). A single, unlooped `git rev-parse --show-toplevel` at module
    scope -- the dominant shape found here, ~100 of 103 sites -- is
    entirely invisible to the amplification collector: there is no loop
    for `_QualifyingLoopVisitor` to mark, so no `AmpSite` is ever produced
    for it, regardless of how many other single-shot sites exist
    repo-wide performing the exact same resolution.

  These are orthogonal axes over disjoint call-site sets (a handful of this
  collector's 103 sites may ALSO sit inside a qualifying loop and so
  independently appear in the other collector's inventory too -- that is
  not a conflict, both frozensets may legitimately contain overlapping
  facts about the same line), and neither collector's frozenset entails or
  subsumes the other's. Extending `find_unbatched_per_item_spawns` to also
  carry an argv-shape/rev-parse-form axis would conflate "this call is
  redundant with an in-process resolver" with "this call is one of many
  batchable per-item spawns" -- two different remedies (call the shared
  seam once; batch N calls into one) under one boolean. A SEVENTH collector
  file is therefore warranted, following the same "one class, one file"
  precedent `test_no_machine_local_cli_read_spawn.py` already set as the
  git-argv-only amplification collector's own sibling (its docstring: "this
  file is its `machine-local` sibling, not a replacement").

WHAT COUNTS AS A HIT: an `ast.Call` to `subprocess.run` / `subprocess.Popen`
/ `subprocess.check_output` / `subprocess.check_call` / `subprocess.
getoutput` whose unparsed source text contains the literal `rev-parse` AND
at least one of the four root-derivation flags (`--show-toplevel`,
`--git-dir`, `--absolute-git-dir`, `--git-common-dir`). Matched on unparsed
call-node source text, same technique and for the same reason as
`test_no_machine_local_cli_read_spawn.py`: the dominant call shape here is
`subprocess.run(["git", "rev-parse", "--show-toplevel"], ...)`, a plain list
literal `spawn_policy.detect._resolve_argv0` already resolves fine, but
several sites build the argv via unpacking or a locally-named list variable
interpolated through an f-string-shaped log line, and unparsed-text matching
sees through all of those uniformly without needing a second code path per
shape.

SCOPE. Root-derivation forms ONLY -- the four flags above. Deliberately
EXCLUDES every other `rev-parse` form found in the same repo-wide grep
sweep (`--show-prefix`, `--is-inside-work-tree`, `--verify`, `--abbrev-ref`,
and the `remote get-url` form C3 already converted): those are either not
yet given a shared conversion target, or (remote URL) already have a
DIFFERENT dedicated seam this chunk does not touch. Restricting the
frozenset below to exactly the four root-derivation flags keeps this
collector's inventory legible against the one target it names, rather than
becoming a second, broader rev-parse audit under a narrower docstring.

MEASURED COUNT DIVERGES FROM THE PLAN'S ~342 ESTIMATE. A repo-wide AST
census (`coordinator_core/` + `coordinator/bin/`, excluding `coordinator_
core/git/` itself and test-tree files) found 103 live call sites across 96
files, not ~342. This is reported as measured, not reconciled toward the
plan's figure, per this chunk's own instructions. The gap is most plausibly
the plan's estimate having counted EVERY `rev-parse` invocation (all forms,
~273 files contain the literal `rev-parse` in this same scope) rather than
the four root-derivation forms alone, plus counting file-level docstring/
comment mentions of the flag strings (many already-converted call sites --
e.g. `archive_stamp.py`'s `_worktree_root`/`_git_common_dir` -- still name
`git rev-parse --show-toplevel` in prose even though the call itself now
goes through `coordinator_core.git.repo_root`). Whatever the estimate's
origin, this collector's frozenset is the ground truth the gate reads, not
the plan's number.

`coordinator_core/engine_root.py` and `coordinator/bin/lib/git_hook_
install.py` -- named by this chunk's brief as PEER-PLAN-HELD and never to
be converted here -- were checked directly and carry ZERO root-derivation
`rev-parse` spawn sites of this collector's shape; both resolve their repo
root through a `repo_identity.resolve_checked_repo_root` seam belonging to
that peer plan, not a literal `git rev-parse --show-toplevel`/`--git-dir`
spawn. Neither therefore has a row in the inventory below -- there is
nothing of this collector's class to mark blocked-on-peer-plan. Recorded
here rather than silently omitted, so a future re-census does not
mistake the absence for an oversight.

NEGATIVE-SPEC:
  - Does NOT match `--show-prefix`, `--is-inside-work-tree`, `--verify`, or
    any other `rev-parse` form -- see "Scope" above.
  - Does NOT match the `git remote get-url` form C3 converted
    (`coordinator_core.git.remote_url`) -- a different git subcommand
    entirely, already served by its own seam.
  - `coordinator_core/git/repo_root.py` and `coordinator_core/git/
    git_dir.py` are excluded from the scan -- they ARE the conversion
    target's spawn fallback (see `repo_root.py`'s own docstring: `show_
    toplevel()`/`absolute_git_dir()`/`show_prefix()`/`is_inside_work_tree()`
    legitimately spawn `git rev-parse` themselves as the seam's own
    ground-truth path), not a call site this collector protects sites
    from bypassing.
  - `engine_root.py` and `git_hook_install.py` are NOT converted here even
    though named by the dispatch brief -- see the paragraph above; they
    are held by a peer plan and, independently, carry no hits of this
    collector's shape to convert.
  - Does not attempt to resolve WHICH cwd a matched call resolves against,
    or whether a walk-based short-circuit would apply at that cwd --
    purely a static call-site census, matching every sibling collector's
    stated "reports call-sites only" convention.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from coordinator_core.spawn_policy import is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

pytestmark = [pytest.mark.cadence]

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_SPAWN_FUNCS = {"run", "Popen", "check_output", "check_call", "getoutput"}

_ROOT_DERIVATION_FLAGS = (
    "--show-toplevel",
    "--git-dir",
    "--absolute-git-dir",
    "--git-common-dir",
)

# The conversion target itself -- excluded so the collector protects call
# sites without alarming on the seam's own ground-truth spawn fallback.
# See module docstring's negative-spec.
_SURFACE_MODULES = frozenset(
    {
        "coordinator_core/git/repo_root.py",
        "coordinator_core/git/git_dir.py",
    }
)


def _spawn_func_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_FUNCS:
        return func.attr
    if isinstance(func, ast.Name) and func.id in _SPAWN_FUNCS:
        return func.id
    return None


def _root_derivation_flag(text: str) -> str | None:
    if "rev-parse" not in text:
        return None
    for flag in _ROOT_DERIVATION_FLAGS:
        if flag in text:
            return flag
    return None


class _ScopeCallVisitor(ast.NodeVisitor):
    """Walks with a def/class-name stack so each matched call can be tagged
    with its ENCLOSING SCOPE (`"<module>"`, `"func"`, or `"Class.method"`)
    rather than only its raw line number.

    Review: code-reviewer (P2) -- `KNOWN_UNCONVERTED_SITES` previously keyed
    on exact `path:lineno`, so any unrelated edit that shifted line numbers
    ABOVE a listed site (a docstring tweak, an added import) produced a
    spurious "stale: X, new: Y" failure for the same logical site, not a
    real regression. Every measured site here is exactly one root-derivation
    call per enclosing function (see the fixture-derived qualnames next to
    `KNOWN_UNCONVERTED_SITES` below), so `path:qualname` is unique per site
    today and stays stable across line-shifting edits that don't touch the
    call's own function -- while still catching a genuinely NEW site (which
    lands in a qualname not already in the frozenset) and still catching a
    site that MOVES to a different function (same reason: new qualname).
    Line number is still carried alongside for human-readable reporting, just
    no longer part of the identity key.
    """

    def __init__(self) -> None:
        self._stack: list[str] = []
        self.hits: list[tuple[str, int]] = []  # (qualname, lineno)

    def _qualname(self) -> str:
        return ".".join(self._stack) if self._stack else "<module>"

    def _enter_scope(self, node: ast.AST, name: str) -> None:
        self._stack.append(name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._enter_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._enter_scope(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._enter_scope(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _spawn_func_name(node) is not None:
            try:
                unparsed = ast.unparse(node)
            except Exception:
                unparsed = ""
            if _root_derivation_flag(unparsed) is not None:
                self.hits.append((self._qualname(), node.lineno))
        self.generic_visit(node)


def _scan_file(rel_posix: str, text: str) -> list[tuple[str, str, int]]:
    """Returns `(rel_posix, qualname, lineno)` per hit -- see
    `_ScopeCallVisitor` for why identity keys on qualname, not lineno."""
    if rel_posix in _SURFACE_MODULES:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    visitor = _ScopeCallVisitor()
    visitor.visit(tree)
    return [(rel_posix, qualname, lineno) for qualname, lineno in visitor.hits]


def _collect_all_hits() -> list[tuple[str, str, int]]:
    hits: list[tuple[str, str, int]] = []
    for root in (_REPO_ROOT / "coordinator_core", _REPO_ROOT / "coordinator" / "bin"):
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel_posix, file_path in discovered:
            repo_rel = (root / rel_posix).relative_to(_REPO_ROOT).as_posix()
            if is_test_tree_site(repo_rel):
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except OSError:
                continue
            hits.extend(_scan_file(repo_rel, text))
    return hits


# NEGATIVE-SPEC -- the surviving root-derivation spawn sites, each with the
# reason it cannot convert. Every row below is an EXCEPTION, not a backlog
# item: this list is not expected to reach zero, and a row leaves only when
# its stated reason stops being true.
#
# Census 2026-08-16 (chunks C5/C6, docs/plans/2026-08-16-a-process-per-
# predicate.md): 103 sites over `coordinator_core/` + `coordinator/bin/`,
# excluding `coordinator_core/git/` (the conversion target) and test trees.
# 95 converted, 8 remain.
#
#   assert-cwd.py -- portability, not oversight. Its own docstring pins "no
#     coordinator_core / claude-klabauter import ... resolvable from any git worktree
#     without a claude-klabauter checkout on PATH", and DoE's new-project ceremony
#     invokes it from OTHER repos. Importing the seam breaks the contract
#     the file exists to hold.
#
#   check-bin-sh-polyglot.py -- FALLBACK ONLY. The primary path is the seam
#     (`_show_toplevel`); this spawn is reached solely when the engine is
#     not importable. It runs on the commit path, where a guard that raises
#     is worse than a guard that pays 13ms, so the fallback stays. The
#     sibling `--show-prefix` call is untouched by design: the seam's own
#     `show_prefix()` spawns too, so routing it through trades a spawn for a
#     spawn plus an indirection.
#
#   coordinator-prepare-commit-msg{,.py} -- MEASURED, not assumed. The
#     file's own docstring claims it "cannot cheaply import
#     coordinator_core"; that reason is false (the seam imports in ~3ms
#     marginal, and the hook already pays for `subprocess`). The true reason
#     is the trade: end-to-end the hook measures 52.0ms today against 48.3ms
#     converted (median; p90 identical) -- ~4ms per commit, inside the 46%
#     A/A noise floor `benchmarks/shim_decision_rule.py` measured on this
#     box -- bought by adding an import-time failure mode to every commit on
#     the machine. Bad trade, so it stays.
#
#   cross-repo-memo.py, git_scope.py -- NOT EXPRESSIBLE through the seam.
#     Both run `git -C <a DIFFERENT repo> rev-parse --absolute-git-dir`
#     under a scrubbed env, precisely to detect a poisoned GIT_DIR, a `.git`
#     file pointing elsewhere, or a GIT_CEILING_DIRECTORIES interaction. The
#     seam is keyed to the CURRENT process's cwd and accepts neither a
#     target repo nor a custom env; converting deletes the confinement check
#     that is the entire point of the call.
#
#   frontmatter/schema_validate.py -- hard prohibition (repo CLAUDE.md and
#     this plan's own anti-scope). DoE imports this module BY FILE PATH and
#     its leniency is contract; ~1350 records depend on it.
#
#   subagent_sandbox/engine.py -- attempted and reverted; see that
#     function's own docstring. Three pinning tests count actual `git`
#     spawns and require ALWAYS spawning, so the seam's walk makes the
#     counts stop matching. Flagged for its own pass rather than forced: the
#     barrier here is a test asserting that a spawn happens, which is the
#     cost this campaign exists to remove.
#
# New entries are REFUSED (`test_inventory_is_exhaustive_and_matches_known_
# sites` below): the fix for a new site is to convert it, never to add a row
# here.
#
# Review: code-reviewer (P2) -- keyed on `path:qualname` (the site's
# enclosing function/class.method, `<module>` if module-level), NOT
# `path:lineno` -- see `_ScopeCallVisitor`'s docstring for why. Each entry
# below is exactly one root-derivation call per named function today.
KNOWN_UNCONVERTED_SITES: frozenset[str] = frozenset(
    {
        "coordinator/bin/assert-cwd.py:main",
        "coordinator/bin/check-bin-sh-polyglot.py:_show_toplevel",
        "coordinator/bin/coordinator-prepare-commit-msg.py:_resolve_git_dir",
        "coordinator/bin/coordinator-prepare-commit-msg:_resolve_git_dir",
        "coordinator/bin/cross-repo-memo.py:_receiver_repo_unusable_reason",
        "coordinator_core/frontmatter/schema_validate.py:_lint_find_repo_root",
        "coordinator_core/git_scope.py:foreign_repo_unusable_reason",
        "coordinator_core/subagent_sandbox/engine.py:_resolve_git_root_uncached",
    }
)


def test_collector_fires_on_the_show_toplevel_shape() -> None:
    """Proof-of-fire: the dominant pre-conversion shape (`subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], ...)`) as a source fixture --
    "a gate that cannot see the defect is not covering it"."""
    fixture = '''
import subprocess

def _worktree_root(start):
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()
'''
    hits = _scan_file("coordinator_core/ops/_fixture_unconverted.py", fixture)
    assert hits, "collector failed to flag the known pre-conversion --show-toplevel shape"
    assert hits[0][1] == "_worktree_root"  # qualname is the enclosing function


def test_collector_fires_on_git_common_dir_shape() -> None:
    """Proof-of-fire for the `--git-common-dir` form specifically -- distinct
    flag from `--show-toplevel`, must independently be matched."""
    fixture = '''
import subprocess

def _common_dir(root):
    return subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], cwd=root, text=True
    ).strip()
'''
    hits = _scan_file("coordinator_core/ops/_fixture_unconverted.py", fixture)
    assert hits, "collector failed to flag the known pre-conversion --git-common-dir shape"
    assert hits[0][1] == "_common_dir"  # qualname is the enclosing function


def test_collector_silent_on_converted_archive_stamp() -> None:
    """`archive_stamp.py`'s `_worktree_root`/`_git_common_dir` already call
    `coordinator_core.git.repo_root`'s seam, not a literal `git rev-parse`
    spawn -- the collector must not flag it even though its docstrings still
    name `git rev-parse --show-toplevel`/`--git-common-dir` in prose."""
    path = _REPO_ROOT / "coordinator_core" / "archive_stamp.py"
    hits = _scan_file("coordinator_core/archive_stamp.py", path.read_text(encoding="utf-8"))
    assert hits == []


def test_collector_silent_on_own_conversion_target() -> None:
    """`coordinator_core/git/repo_root.py` is the seam this collector
    protects call sites from bypassing, not a call site itself -- excluded
    via `_SURFACE_MODULES`, matching `test_no_machine_local_cli_read_spawn.
    py`'s identical treatment of `machine_resolver.py`."""
    path = _REPO_ROOT / "coordinator_core" / "git" / "repo_root.py"
    hits = _scan_file("coordinator_core/git/repo_root.py", path.read_text(encoding="utf-8"))
    assert hits == []


def test_engine_root_and_git_hook_install_carry_no_hits() -> None:
    """`engine_root.py` and `git_hook_install.py` are named by this
    collector's dispatch brief as peer-plan-held and never to be converted
    here. Pins that they genuinely have nothing of this collector's shape
    to convert (see module docstring) -- if a future edit introduces a
    literal root-derivation `rev-parse` spawn into either file, this test
    fails, signalling that the "nothing to mark blocked" premise no longer
    holds and the peer-plan boundary needs re-examination, not a silent
    frozenset addition."""
    for repo_rel in (
        "coordinator_core/engine_root.py",
        "coordinator/bin/lib/git_hook_install.py",
    ):
        path = _REPO_ROOT / repo_rel
        if not path.exists():
            continue
        hits = _scan_file(repo_rel, path.read_text(encoding="utf-8"))
        assert hits == [], f"{repo_rel} now has root-derivation spawn site(s): {hits}"


def test_inventory_is_exhaustive_and_matches_known_sites() -> None:
    """The live census must equal `KNOWN_UNCONVERTED_SITES` exactly -- a
    new, unlisted hit fails closed (regrowth caught), and a listed site
    that no longer appears must be removed (burn-down is visible, not
    silently stale).

    Keyed on `path:qualname`, not `path:lineno` -- see `_ScopeCallVisitor`'s
    docstring (code-reviewer P2): an unrelated line-shifting edit inside the
    same enclosing function must NOT fail this test, while a call that is
    genuinely new (or that moves to a different function) still must."""
    all_hits = _collect_all_hits()
    hits = {f"{path}:{qualname}" for path, qualname, _lineno in all_hits}
    stale = KNOWN_UNCONVERTED_SITES - hits
    new = hits - KNOWN_UNCONVERTED_SITES
    assert not stale, f"sites converted but still listed -- remove from KNOWN_UNCONVERTED_SITES: {sorted(stale)}"
    if new:
        # Render with line numbers for the human fixing this, even though
        # line number is not part of the identity key.
        by_key = {f"{path}:{qualname}": lineno for path, qualname, lineno in all_hits}
        detail = [f"{key} (line {by_key[key]})" for key in sorted(new)]
        raise AssertionError(
            "new git-root-derivation rev-parse shell-out(s) -- convert to "
            f"coordinator_core.git.repo_root, do not add here: {detail}"
        )

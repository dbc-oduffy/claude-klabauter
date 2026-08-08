"""coordinator_core.write_guards.nudge_private_git_fact_resolver — advisory guard.

Write-time ergonomic surface for the twelfth-private-resolver problem
(docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md, chunk
D3). The plan's census found ELEVEN write_guards each hand-rolled a private
`git rev-parse` resolver while a shared, non-spawning-in-the-ordinary-case
seam sat unadopted one package over — that has now been fixed (peer commit
`8fef08d6d` for the repo-root leg; chunk D4 for the git-dir leg,
`coordinator_core.git.repo_root`). This guard is the only artifact that
stops the TWELFTH private resolver being written: nothing else in the repo
notices a new hand-rolled `git rev-parse` call in a hot-path module.

MODELED ON `nudge_shell_shaped_spawn.py` (the "reuse a shared enumeration,
assert a different property" precedent) for its overall shape — whole body
in one try/except-Exception-return-None, gate on tool_name/extension/size,
one advisory envelope per response — but this guard does NOT reuse
`spawn_policy.sites_in_source` for its OWN classification: that API reports
call-site SHAPE (argv0, kind) only, with no visibility into WHICH `git
rev-parse` flag a call passes, and the fire-set/silent-set split below is
defined entirely by that flag. This guard runs its own narrow AST scan for
the argv-flag membership test instead — modeled on
`coordinator_core/tests/test_no_cwd_unguarded_coordinator_core_subprocess_
spawn.py`'s own precedent for "collect literal argv strings from a
subprocess call, test set membership on them", not on `sites_in_source`.

WHOLE-FILE RECONSTRUCTION is reused, not re-derived, from the live sibling
`nudge_windows_subprocess_popup` (DR-077 part 1): `_read_file_safely`,
`_extract_content` (which drives `_apply_one_edit` internally) are imported
directly — an argv-flag membership test needs the same "did the edit
really produce this text" whole-file fidelity DR-077 established, and
importing keeps the two guards' reconstruction plumbing in lockstep by
construction.

FIRE-SET — exactly the three WALK-BACKED forms
(`coordinator_core.git.repo_root`'s own docstring, "Non-spawning
parent-walk vs spawn, honestly per form"):
  - `--show-toplevel`  -> offer `coordinator_core.git.repo_root.show_toplevel`
  - `--git-dir`        -> offer `coordinator_core.git.repo_root.git_dir`
  - `--git-common-dir` -> offer `coordinator_core.git.repo_root.git_common_dir`
...and ONLY when the proposed file lives in a hot-path module (`write_guards/`,
`bash_guards/`, `hooks/`, `ops/session/`) — a test-tree site never runs in a
live session (`spawn_policy.is_test_tree_site`, chunk D2, `c88496d6b`) and is
never a culprit, and a non-hot-path module is out of this detector's stated
remit.

MATCH BY ARGV MEMBERSHIP, NEVER POSITION OR ADJACENCY. The plan's baseline
audit (`state/audits/2026-08-07-spawn-storm-culprit-taxonomy.md`,
scenario-iv enumeration) records the live-measured invocation as
`git rev-parse --path-format=absolute --git-common-dir` — the discriminating
flag (`--git-common-dir`) is neither the second argv element nor adjacent to
`rev-parse`. `_collect_git_rev_parse_flags` below scans the FULL argv-string
list for `git`/`rev-parse` presence and returns every OTHER literal string
in the same call — a positional assumption would silently miss this exact
form, which is why the discriminating flag is tested via `in`, never via
index.

SILENT SET — the three forms `coordinator_core.git.repo_root`'s own
docstring states ALWAYS spawn, deliberately:
  - `--show-prefix` / `--absolute-git-dir` — the offered seam is
    fork-EQUAL on first call, not cheaper (repo_root.py's per-form
    docstring). A "fork-equal on first call, zero thereafter" memoization
    claim is a DIFFERENT property than "cheaper in the ordinary case" — see
    `_alternative_liveness`'s 2026-07-29 false-capability catch, which is
    exactly the mixing this guard must not repeat.
  - `--is-inside-work-tree` — deriving it from walk state is a documented
    semantic trap (`repo_root.is_inside_work_tree`'s own docstring: a bare
    repo has no working tree and no toplevel, but for DIFFERENT reasons a
    truthiness check on `show_toplevel()` would conflate), not a refactor
    opportunity.

AC5 HONESTY: the offer text says "cheaper in the ordinary case, never more
expensive" — NEVER "eliminates the spawn". `show_toplevel()` still falls
back to a spawn for the no-`.git`-found case; the walk DOES recognise a
`.git` FILE as well as a directory (so `--separate-git-dir` clones and
submodules are handled by the walk, not a walk gap) — see
`coordinator_core/git/repo_root.py`'s own "Non-spawning parent-walk vs
spawn, honestly per form" section, which this guard's offer text mirrors
rather than oversells.

`--git-common-dir` absolute-path confirmation (per this chunk's brief):
`coordinator_core.git.repo_root.git_common_dir` is the offered symbol, NOT
`coordinator_core.git.git_dir.resolve_git_common_dir` directly —
`repo_root.git_common_dir()` normalizes the spawn-fallback path against the
resolved cwd before returning (see its own docstring's "Asymmetry" section),
so it is absolute in every path this guard's offer would replace, unlike the
lower-level `resolve_git_common_dir`, which builds a Path via join/normpath
with no `.resolve()` and is only absolute if ITS OWN caller's `repo_root`
already is. Offering the seam-level function, not the primitive, is what
keeps this guard's "cheaper, never more expensive" claim true regardless of
what a hypothetical private resolver's own `repo_root` value would have been.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only `additionalContext`, never `permissionDecision`.
  - Does NOT fire on `--show-prefix`, `--absolute-git-dir`, or
    `--is-inside-work-tree` — see SILENT SET above. A future author widening
    the fire-set to include one of these must first establish (and state,
    not assume) that the offered seam is at least as cheap for that form.
  - Does NOT fire outside a hot-path module (`write_guards/`, `bash_guards/`,
    `hooks/`, `ops/session/`) or inside a test-tree site
    (`spawn_policy.is_test_tree_site`) — neither runs in a live session, so
    neither is a culprit this detector's remit covers.
  - Does NOT consult `spawn_policy.allowlist` — a shell-out carve-out
    register is orthogonal to "does a cheaper non-spawning seam exist for
    this specific git rev-parse form"; re-deriving that here would duplicate
    a different workstream's enforcement in an advisory surface that must
    stay cheap and simple.
  - Never raises: any unexpected input shape, oversized file, or parse
    failure returns None (ALLOW/no-op).

Spec backlink: docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md § D3
Grep anchors: SPAWN-STORM-CULPRIT-TAXONOMY D3
"""

from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional

from coordinator_core.spawn_policy import is_test_tree_site
from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_content,
    _extract_file_path,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 200  # advisory/deny-offer band; next slot after nudge_shell_shaped_spawn (190)

#: Hot-path module markers — a hand-rolled resolver outside these never runs
#: in a live session, so is never a culprit this detector's remit covers.
_HOT_PATH_MARKERS = ("write_guards/", "bash_guards/", "hooks/", "ops/session/")

#: FIRE-SET: the three walk-backed forms, each mapped to its offered
#: importable symbol. See module docstring "FIRE-SET".
_FIRE_FLAG_TO_OFFER: Dict[str, str] = {
    "--show-toplevel": "coordinator_core.git.repo_root.show_toplevel",
    "--git-dir": "coordinator_core.git.repo_root.git_dir",
    "--git-common-dir": "coordinator_core.git.repo_root.git_common_dir",
}

#: SILENT SET: the three always-spawning forms. Named here (not merely
#: absent from `_FIRE_FLAG_TO_OFFER`) so a reader — and the guard's own
#: self-tests — can see this is a deliberate exclusion, not an oversight.
_SILENT_FLAGS = frozenset(
    {"--show-prefix", "--absolute-git-dir", "--is-inside-work-tree"}
)

_OFFER_TEMPLATE = (
    "OFFER: {symbol} walks the parent directories for `.git` (dir or file) "
    "instead of spawning `git rev-parse {flag}` — cheaper in the ordinary "
    "case, never more expensive (it still falls back to a spawn when no "
    "`.git` entry is found on the walk). See coordinator_core/git/repo_root.py "
    "\"Non-spawning parent-walk vs spawn, honestly per form\".\n"
    "Found a private `git rev-parse {flag}` call in {file_path}."
)


def _is_hot_path(file_path: str) -> bool:
    return any(marker in file_path for marker in _HOT_PATH_MARKERS)


def _spawn_call_name(func: ast.expr) -> Optional[str]:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _string_constants(node: ast.expr) -> List[str]:
    strings: List[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                strings.append(elt.value)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    return strings


_SPAWN_CALL_NAMES = {"run", "Popen", "check_output", "call", "check_call"}


class _GitRevParseVisitor(ast.NodeVisitor):
    """Collects every FIRE-SET flag found (by MEMBERSHIP, never position)
    in a `git rev-parse ...`-shaped subprocess call's argv.

    Mirrors `test_no_cwd_unguarded_coordinator_core_subprocess_spawn.py`'s
    own "collect literal argv strings, test set membership" precedent — see
    module docstring's "MATCH BY ARGV MEMBERSHIP" section for why this is
    NOT delegated to `spawn_policy.sites_in_source`.
    """

    def __init__(self) -> None:
        self.fire_flags: List[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _spawn_call_name(node.func)
        if name in _SPAWN_CALL_NAMES:
            strings: List[str] = []
            for arg in node.args:
                strings.extend(_string_constants(arg))
            for kw in node.keywords:
                if kw.arg == "args" and kw.value is not None:
                    strings.extend(_string_constants(kw.value))
            if "git" in strings and "rev-parse" in strings:
                for flag, symbol in _FIRE_FLAG_TO_OFFER.items():
                    if flag in strings:
                        self.fire_flags.append(flag)
        self.generic_visit(node)


def _collect_fire_flags(content: str) -> List[str]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    visitor = _GitRevParseVisitor()
    visitor.visit(tree)
    return visitor.fire_flags


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        raw_file_path = _extract_file_path(payload)
        if not raw_file_path:
            return None

        file_path = raw_file_path.replace("\\", "/")
        if not file_path.endswith(".py"):
            return None

        if is_test_tree_site(file_path):
            return None

        if not _is_hot_path(file_path):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        content = _extract_content(tool_name, tool_input, file_path)
        if not content:
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

        fire_flags = _collect_fire_flags(content)
        if not fire_flags:
            return None

        # One advisory envelope per response (INTERFACE.md) — lead with the
        # first (lowest-ordinal-in-source-order) fire flag found.
        flag = fire_flags[0]
        symbol = _FIRE_FLAG_TO_OFFER[flag]
        reason = _OFFER_TEMPLATE.format(symbol=symbol, flag=flag, file_path=file_path)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error — this guard offers only on a
        # positive fire-set match, never on an error.
        return None

"""coordinator_core.write_guards.nudge_unmarked_spawning_test — advisory guard.

Write-time ergonomic surface for the spawn ratchet
(`coordinator_core/tests/test_no_new_spawning_tests.py`, Rule 2/Rule 4):
today an author only learns a new test function spawns a real process
without declaring it by running the 23-minute fast tier and reading a
ratchet failure. This guard makes the correct answer ("mark it, or stub
the spawn") visible at the moment the file is written, before that
round-trip — modelled directly on the shipped sibling
`nudge_shell_shaped_spawn.py` (same MATCHERS shape, same nudge-not-block
posture, same reuse of a pinned text-in/sites-out API rather than
importing test-collection internals as a production dependency).

RE-SITED FROM A PRE-COMMIT GATE (staff-eng F4/F5, DR-223 point 2): an AST
scan of staged test files for pytest markers is a pure function of
committed tree state — it does not need to run before the commit object
exists, and it is not un-CI-able, so DR-223 sends it to CI, not a local
hook. Standing up CI for this repo is a direction-class call outside an
EM's discharge authority, and this chunk sidesteps that conflict rather
than resolving it by re-siting to the write-guard seam instead. Commit-
time teeth remain available to the PM separately, unaffected by this
guard's existence.

CLASS is "advisory" for the identical reason `nudge_shell_shaped_spawn`
is: the enforcement teeth are `test_no_new_spawning_tests.py` itself
(Rule 2/Rule 4), this module only makes the correct path cheaper than an
unmarked spawn BEFORE the write lands.

DETECTION reuses `coordinator_core.spawn_policy.sites_in_source` (the
same pinned text-in/sites-out API `nudge_shell_shaped_spawn` uses) for
"does this file's post-edit content contain a spawn site", and the
LIFTED marker-check module `coordinator_core.spawn_policy.marker_check`
(staff-eng F7 — hoisted out of the ratchet test itself, not re-derived
here and not imported from the pytest module, which would make a module
with `import pytest` and `_WRAPPER_RESOLVER` construction a production
dependency of every matching write on this box) for "is that spawn
covered by a marker."

WHOLE-FILE RECONSTRUCTION is REUSED, not re-derived, from
`nudge_windows_subprocess_popup` — identical fidelity requirement to
`nudge_shell_shaped_spawn`'s own reuse of the same helpers.

SCOPE is the TEST TREE ONLY (`test_*.py` / `conftest.py`), matching the
ratchet's own `_iter_test_files` target set — a non-test `.py` write
never fires, since the ratchet itself never inspects one.

NEGATIVE-SPEC precision note (deliberately looser than the ratchet, in
the SAFE direction): this guard's spawn-site detection is
`spawn_policy.sites_in_source`'s SHELL_BINARY/SHELL_TRUE/PLAIN_SPAWN
site inventory, not the ratchet's own narrower `_REAL_BINARIES`-gated
`_classify_spawn`. A site this guard flags that the ratchet would not
(different classification set) still nudges an author toward a TRUE
statement — "this call looks like a spawn" — never a false one; the
ratchet's test gate remains the sole authority on whether a file
actually fails Rule 2/Rule 4. This is an advisory, not a duplicate
enforcement of the ratchet's own rule.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope
    carries only `additionalContext`, never `permissionDecision`.
  - Does NOT fire on a module-level pytestmark-covered file, or a file
    with no spawn sites at all.
  - Does NOT fire on a non-test-tree `.py` file (see SCOPE above) or a
    non-`.py` file.
  - Does NOT name the override key inline — this guard is advisory and
    has no unlock path of its own; nothing here invents one.
  - Never raises: any unexpected input shape, oversized file, or parse
    failure returns None (ALLOW/no-op), mirroring `nudge_shell_shaped_spawn`.

Spec backlink: docs/plans/2026-08-20-the-spawn-ratchet-stops-accumulating-arrears.md § C4
"""

from __future__ import annotations

import ast
import os
from typing import Any, Dict, Optional

from coordinator_core.spawn_policy import SpawnParseError, sites_in_source
from coordinator_core.spawn_policy.marker_check import (
    SPAWNS_PROCESS_MARKER,
    has_marker_decorator,
    has_module_level_pytestmark,
)
from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_content,
    _extract_file_path,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 191  # advisory/deny-offer band; next slot after nudge_shell_shaped_spawn.py (190)


def _is_test_tree_path(file_path: str) -> bool:
    """Mirrors `test_no_new_spawning_tests.py::_iter_test_files`'s own
    per-candidate filename test (`conftest.py` or `test_*.py`) — this
    guard's target set is the same population the ratchet itself scans,
    not a re-derivation of `_read_testpaths()`'s directory roots (a
    write-time guard sees one path at a time, and the ratchet's own
    filename check is what actually decides membership per-file)."""
    name = os.path.basename(file_path)
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def _decorators_by_enclosing(tree: ast.Module) -> Dict[str, list[ast.expr]]:
    """Dotted-scope-path -> decorator list, built with the identical
    scope-stack join `spawn_policy.detect._SiteCollector` uses for
    `SpawnSite.enclosing` (`"Class.method"`, `"func"`), so a lookup by
    `site.enclosing` lands on the right decorator list."""
    out: Dict[str, list[ast.expr]] = {}
    stack: list[str] = []

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                stack.append(child.name)
                _visit(child)
                stack.pop()
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stack.append(child.name)
                out[".".join(stack)] = child.decorator_list
                _visit(child)
                stack.pop()
            else:
                _visit(child)

    _visit(tree)
    return out


def _reason_for(file_path: str, unmarked_enclosings: list[str]) -> str:
    plural = "function" if len(unmarked_enclosings) == 1 else "functions"
    names = ", ".join(sorted(unmarked_enclosings))
    return (
        f"OFFER: mark it (@{SPAWNS_PROCESS_MARKER}, plus @pytest.mark.cadence) "
        "or stub the spawn instead of calling a real process.\n"
        f"Unmarked spawning {plural} in {file_path}: {names}."
    )


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
        if not _is_test_tree_path(file_path):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        content = _extract_content(tool_name, tool_input, file_path)
        if not content:
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

        try:
            sites = sites_in_source(content, file_path)
        except SpawnParseError:
            return None
        if not sites:
            return None

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None

        if has_module_level_pytestmark(tree):
            return None

        decorators_by_enclosing = _decorators_by_enclosing(tree)

        unmarked: set[str] = set()
        for site in sites:
            if site.enclosing == "<module>":
                # Rule 1 territory (import-time spawn) -- no marker can
                # excuse it; not this advisory's shape to fix, the ratchet
                # itself is unconditional here.
                continue
            decorators = decorators_by_enclosing.get(site.enclosing, [])
            if has_marker_decorator(decorators):
                continue
            unmarked.add(site.enclosing)

        if not unmarked:
            return None

        reason = _reason_for(file_path, sorted(unmarked))

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard offers only on a
        # positive unmarked-spawn match, never on an error.
        return None

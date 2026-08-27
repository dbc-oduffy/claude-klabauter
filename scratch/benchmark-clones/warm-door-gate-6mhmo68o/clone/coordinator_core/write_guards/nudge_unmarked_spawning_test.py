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

RE-SITED FROM A PRE-COMMIT GATE (DR-223 point 2): an AST
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
(hoisted out of the ratchet test itself, not re-derived
here and not imported from the pytest module, which would make a module
with `import pytest` and `_WRAPPER_RESOLVER` construction a production
dependency of every matching write on this box) for "is that spawn
covered by a marker."

WHOLE-FILE RECONSTRUCTION is REUSED, not re-derived, from
`nudge_windows_subprocess_popup` — identical fidelity requirement to
`nudge_shell_shaped_spawn`'s own reuse of the same helpers. `_extract_content_ex`
is itself fail-open to an EDIT FRAGMENT (unreadable/oversized file, or an
unresolvable `old_string`) and reports that via its own `used_fallback`
bool — `has_module_level_pytestmark` is a whole-file property that always
reads False on a fragment, so this guard's `check` reads that flag directly
(never re-derives it by comparing content against the fragment, which is
not a sound proxy: a whole-file `Edit` rewrite where `old_string` is the
entire prior file makes the reconstructed whole file byte-identical to the
bare fragment even though reconstruction fully succeeded) and stays silent
rather than risk a false-positive nudge on a file the fragment cannot prove
is unmarked.

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
    _extract_content_ex,
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


#: Local mirror of `coordinator_core.bash_guards._message_size.
#: MESSAGE_PROSE_CAP_BYTES` -- deliberately NOT imported: that module pulls
#: in `dispatch.py`'s full guard-registration chain, a cost this write-time
#: guard cannot afford on every Write/Edit/MultiEdit (see this guard's own
#: F5 hot-path finding). A drift between this mirror and the SSOT constant
#: is caught by `guard_message_corpus.py`'s own render of this guard's
#: real fire row against the SSOT, not by this local copy agreeing with
#: itself.
_MESSAGE_PROSE_CAP_BYTES = 220

#: Hard ceiling on the rendered basename, bytes -- so a pathological
#: filename cannot itself eat the whole cap and leave `_fit_unmarked_names`
#: no budget at all (measured: a bare "no names" render already costs 159
#: bytes of fixed prose/template, leaving 61 for basename+names; this
#: caps the basename's share, always leaving room for at least a "+N more"
#: fallback -- see `_reason_for`).
_BASENAME_MAX_BYTES = 40


def _cap_basename(basename: str, max_bytes: int) -> str:
    """Truncate `basename` to at most `max_bytes` UTF-8 bytes (ellipsis
    included), dropping one character at a time -- basenames are short, so
    this loop is cheap; never raises on an empty/tiny `max_bytes`."""
    if len(basename.encode("utf-8")) <= max_bytes:
        return basename
    truncated = basename
    while truncated and len((truncated + "…").encode("utf-8")) > max_bytes:
        truncated = truncated[:-1]
    return truncated + "…" if truncated else ""


def _fit_unmarked_names(names: list[str], budget_bytes: int) -> str:
    """Greedily render as many of `names` (already sorted) as fit, UTF-8
    measured, within `budget_bytes` -- the prior
    `", ".join(sorted(unmarked_enclosings))` had no cap at all, and an
    absolute `file_path` plus two-or-more real function names routinely
    clears `MESSAGE_PROSE_CAP_BYTES`. Budget-based rather than a fixed
    name-count cap, because `budget_bytes` itself shrinks with a long
    basename -- a fixed "first two" cap can still overflow on a long
    basename plus two long names. Degrades to a bare "+N more" (itself
    budget-checked) if not even one name fits, and to "" if that too does
    not fit -- never renders past `budget_bytes`."""
    if not names or budget_bytes <= 0:
        return ""
    shown: list[str] = []
    for name in names:
        remaining = len(names) - len(shown) - 1
        tail = f", +{remaining} more" if remaining > 0 else ""
        rendered = ", ".join(shown + [name]) + tail
        if len(rendered.encode("utf-8")) > budget_bytes:
            break
        shown.append(name)
    if shown:
        remaining = len(names) - len(shown)
        tail = f", +{remaining} more" if remaining > 0 else ""
        return ", ".join(shown) + tail
    fallback = f"+{len(names)} more"
    return fallback if len(fallback.encode("utf-8")) <= budget_bytes else ""


def _reason_for(file_path: str, unmarked_enclosings: list[str]) -> str:
    ordered = sorted(unmarked_enclosings)
    plural = "function" if len(ordered) == 1 else "functions"
    basename = _cap_basename(os.path.basename(file_path), _BASENAME_MAX_BYTES)
    prefix = (
        f"OFFER: mark it (@{SPAWNS_PROCESS_MARKER}, plus @pytest.mark.cadence) "
        "or stub the spawn instead of calling a real process.\n"
        f"Unmarked spawning {plural} in {basename}: "
    )
    budget = _MESSAGE_PROSE_CAP_BYTES - len(prefix.encode("utf-8")) - 1  # trailing "."
    names = _fit_unmarked_names(ordered, budget)
    tail = f"{names}." if names else "(see file)."
    return prefix + tail


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

        content, used_fallback = _extract_content_ex(tool_name, tool_input, file_path)
        if not content:
            return None

        if used_fallback:
            # Whole-file reconstruction fell back to the edit fragment --
            # has_module_level_pytestmark is a whole-file property and
            # always reads False on a fragment, which would false-positive-
            # nudge a file that already carries a covering pytestmark
            # elsewhere. Stay silent rather than risk a wrong nudge; `Write`
            # always supplies the true whole file and never takes this
            # branch.
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

        # Cheapest, whole-file veto first: a single
        # `ast.parse` plus a module-level-statements-only walk, before the
        # more expensive `sites_in_source` site collection -- a correctly
        # `pytestmark`-covered file returns here after one parse instead of
        # paying for site collection it will discard anyway.
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return None

        if has_module_level_pytestmark(tree):
            return None

        try:
            sites = sites_in_source(content, file_path)
        except SpawnParseError:
            return None
        if not sites:
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

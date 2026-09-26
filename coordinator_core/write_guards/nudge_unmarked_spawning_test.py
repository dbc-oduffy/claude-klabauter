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

REGISTERED-MARKER GATE (Item 27,
docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md § R27): the
offer names `@pytest.mark.spawns_process` unconditionally, which is
wrong for a consumer repo that never registered that marker -- pytest
runs an unregistered custom marker under `--strict-markers` as a hard
collection error, so the nudge would be steering the author to add a
line that breaks their own test run. This guard resolves the target
repo root the same way its write-guard siblings do
(`write_guards._repo_root.resolve_repo_root`, no new subprocess), reads
ONLY `pyproject.toml`'s `[tool.pytest.ini_options] markers` list (via
stdlib `tomllib`, no spawn) or, failing that, an ini-shaped
`pytest.ini`/`setup.cfg`'s `[pytest] markers` key (via stdlib
`configparser`) -- never spawns pytest to ask it. If neither file
registers a `markers` list at all, the guard stays silent: it cannot
tell whether the marker exists, and a wrong assertion in either
direction is worse than no offer. If a markers list IS found but does
not contain the bare `spawns_process` name, the guard stays silent for
the same reason -- offering a marker the target repo has not opted into
would recommend an addition that itself fails `--strict-markers`. Only
when `spawns_process` is confirmed registered does the existing
detection-and-render pipeline below run.

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
  - Does NOT fire when the target repo has no `markers` list in either
    `pyproject.toml`'s `[tool.pytest.ini_options]` or an ini-shaped
    `pytest.ini`/`setup.cfg`'s `[pytest]` section -- absence is silence,
    never an assumed registration.
  - Does NOT fire when a `markers` list IS found but does not register
    the bare `spawns_process` name.
  - Does NOT spawn pytest, or any process, to answer either question
    above -- both files are parsed directly off disk.
  - Does NOT name the override key inline — this guard is advisory and
    has no unlock path of its own; nothing here invents one.
  - Never raises: any unexpected input shape, oversized file, or parse
    failure returns None (ALLOW/no-op), mirroring `nudge_shell_shaped_spawn`.

Spec backlink: docs/plans/2026-08-20-the-spawn-ratchet-stops-accumulating-arrears.md § C4
"""

from __future__ import annotations

import ast
import configparser
import os
import tomllib
from typing import Any, Dict, Optional

from coordinator_core.spawn_policy import SpawnParseError, sites_in_source
from coordinator_core.spawn_policy.marker_check import (
    SPAWNS_PROCESS_MARKER,
    has_marker_decorator,
    has_module_level_pytestmark,
)
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_content_ex,
    _extract_file_path,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 191

#: bare marker name registered via `@pytest.mark.spawns_process` --
#: `SPAWNS_PROCESS_MARKER` is the dotted decorator form ("pytest.mark.…"),
#: registration lists carry only the trailing segment.
_SPAWNS_PROCESS_MARKER_NAME = SPAWNS_PROCESS_MARKER.rsplit(".", 1)[-1]


def _marker_names_from_list(raw_markers: Any) -> Optional[set[str]]:
    if not isinstance(raw_markers, (list, tuple)):
        return None
    names: set[str] = set()
    for entry in raw_markers:
        if not isinstance(entry, str):
            continue
        name = entry.split(":", 1)[0].strip()
        if name:
            names.add(name)
    return names


def _registered_markers_from_pyproject(repo_root: str) -> Optional[set[str]]:
    path = os.path.join(repo_root, "pyproject.toml")
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    ini_options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    if not isinstance(ini_options, dict):
        return None
    return _marker_names_from_list(ini_options.get("markers"))


def _registered_markers_from_ini(repo_root: str) -> Optional[set[str]]:
    for basename in ("pytest.ini", "setup.cfg"):
        path = os.path.join(repo_root, basename)
        parser = configparser.ConfigParser()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                parser.read_file(fh)
        except (OSError, configparser.Error, UnicodeDecodeError):
            continue
        if not parser.has_section("pytest"):
            continue
        raw = parser.get("pytest", "markers", fallback=None)
        if raw is None:
            continue
        lines = [line for line in raw.splitlines() if line.strip()]
        names = _marker_names_from_list(lines)
        if names is not None:
            return names
    return None


def _spawns_process_marker_registered(repo_root: Optional[str]) -> bool:
    """True only if `repo_root` explicitly registers the bare
    `spawns_process` marker name in `pyproject.toml`'s
    `[tool.pytest.ini_options] markers` or an ini-shaped
    `pytest.ini`/`setup.cfg`'s `[pytest] markers`. Never spawns pytest;
    parses both candidate files directly off disk. Absent root, absent
    file, or a file present without a `markers` list at all all return
    False -- the caller reads False as "stay silent," not as "confirmed
    unregistered."""
    if not repo_root:
        return False
    registered = _registered_markers_from_pyproject(repo_root)
    if registered is None:
        registered = _registered_markers_from_ini(repo_root)
    if registered is None:
        return False
    return _SPAWNS_PROCESS_MARKER_NAME in registered


def _is_test_tree_path(file_path: str) -> bool:
    name = os.path.basename(file_path)
    return name == "conftest.py" or (name.startswith("test_") and name.endswith(".py"))


def _decorators_by_enclosing(tree: ast.Module) -> Dict[str, list[ast.expr]]:
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


#: MESSAGE_PROSE_CAP_BYTES` -- deliberately NOT imported: that module pulls
_MESSAGE_PROSE_CAP_BYTES = 220

_BASENAME_MAX_BYTES = 40


def _cap_basename(basename: str, max_bytes: int) -> str:
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
    budget = _MESSAGE_PROSE_CAP_BYTES - len(prefix.encode("utf-8")) - 1
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
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

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
                continue
            decorators = decorators_by_enclosing.get(site.enclosing, [])
            if has_marker_decorator(decorators):
                continue
            unmarked.add(site.enclosing)

        if not unmarked:
            return None

        repo_root = resolve_repo_root(payload.get("cwd"))
        if not _spawns_process_marker_registered(repo_root):
            return None

        reason = _reason_for(file_path, sorted(unmarked))

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        return None

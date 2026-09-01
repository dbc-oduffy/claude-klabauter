"""Standing gate: no test file flips the process-global `os.name`.

Purpose: `monkeypatch.setattr(os, "name", "nt")` (or the `unittest.mock`
equivalents, or a bare `os.name = ...` assignment) makes CPython's
`pathlib.Path(...)` construct `WindowsPath` for the REST OF THE PROCESS,
not just inside the patched function — every `str()` of a path built
anywhere afterwards (including inside unrelated library code) silently
turns "/" into "\\". That corrupted a lock-file path built from
`str(Path(...))` in `fleet_env.py` and planted a stray backslash-named file
at the repo root (the xdist-parallel `popen-gw2` sighting this gate closes;
see `coordinator_core/install/conftest.py`'s runtime litter guard for the
same defect's symptom-side backstop). A named per-module platform seam
(`junction._host_is_nt`, `raw_cmdline_recovery._host_is_nt`) exercises the
same Windows-only branch without touching the process-global.

AST-based, not regex, per the in-repo precedent this module is modelled on
(`coordinator/lib/home_resolution_lint.py`) — a regex over source text
cannot distinguish `os.name` from an unrelated `foo.name` attribute access
or a string that merely contains the substring.

Spec backlink: earlier dispatch on the `os.name`/pathlib litter defect
(`coordinator_core/install/junction.py::_host_is_nt`,
`coordinator_core/install/conftest.py`).

Negative-spec:
    - No allowlist / exemption mechanism. A site that genuinely cannot be
      converted to a named seam is a case to raise with the author of this
      gate, not a suppression to add here.
    - Does NOT scan non-test files — a production module reading `os.name`
      directly (e.g. inside a `_host_is_nt`-shaped seam's own body) is
      exactly correct and out of this gate's scope.

RATCHET, NOT ALLOWLIST — `state/tests/os-name-monkeypatch-baseline.json`
carries a per-file CEILING count for the 8 pre-existing offenders (31
sites total), verified latent-not-active on the day this ratchet was
built (see that file's `_debt_status`). An allowlist grants a site
permanent exemption; this baseline instead pins today's count as a
ceiling that may only ever go DOWN, following the shape of
`test_known_red_ratchet.py`'s registry-vs-observed-set split:

    - `test_no_new_os_name_offender_beyond_the_baseline` — the ratchet
      itself. Any site in a file NOT in the baseline, or beyond a listed
      file's ceiling, fails and names the file/line and the seam
      remediation `_REMEDIATION` already gives.
    - `test_baseline_ceiling_matches_observed_count_or_lower` — forces
      the baseline DOWN as work lands. A file whose observed count drops
      below its recorded ceiling fails until the baseline JSON is edited
      to match, so debt cannot quietly stall at "good enough for now".

Both gates read `_find_violations` — the AST matcher above — as their
sole source of truth; neither re-derives or weakens it.
"""

from __future__ import annotations

import ast
import functools
import json
import os
from pathlib import Path
from typing import Iterable

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_EXCLUDED_PARTS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "build",
        "node_modules",
        "archive",
        "state",
        "tasks",
        "scratch",
        "scratchpad",
        "cross-repo",
        "pip",
    }
)

_REMEDIATION = (
    "monkeypatch.setattr(os, \"name\", ...) / mock.patch(\"os.name\", ...) / "
    "os.name = ... corrupts every pathlib.Path built for the rest of the "
    "process. Give the code under test a named platform seam "
    "(`def _host_is_nt() -> bool: return os.name == \"nt\"`) and patch that "
    "function instead — see junction.py or raw_cmdline_recovery.py."
)


def _is_test_file(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _iter_test_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_PARTS]
        for filename in filenames:
            if filename.endswith(".py") and _is_test_file(Path(filename)):
                yield Path(dirpath) / filename


def _is_os_name_attr(node: ast.AST) -> bool:
    """True for the AST shape `os.name` (an `Attribute` node `attr="name"`
    on a bare `Name` node `id="os"`) — never a same-named attribute on any
    other object."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "name"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_name_string_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "os.name"


def _find_violations(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _is_os_name_attr(target):
                    lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            # monkeypatch.setattr(os, "name", ...)
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "os"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "name"
            ):
                lines.append(node.lineno)
            # mock.patch.object(os, "name", ...)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "object"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "patch"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "os"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "name"
            ):
                lines.append(node.lineno)
            # mock.patch("os.name", ...) / patch("os.name", ...)
            elif (
                (
                    (isinstance(func, ast.Attribute) and func.attr == "patch")
                    or (isinstance(func, ast.Name) and func.id == "patch")
                )
                and node.args
                and _is_os_name_string_literal(node.args[0])
            ):
                lines.append(node.lineno)
    return lines


_BASELINE_PATH = _REPO_ROOT / "state" / "tests" / "os-name-monkeypatch-baseline.json"


def _cannot_contain_os_name_site(data: bytes) -> bool:
    """Cheap-reject test: True only when `data` cannot possibly contain any
    shape `_find_violations` matches, so callers may skip `ast.parse`
    entirely.

    Branch-by-branch mapping from `_find_violations` to the substring each
    branch's source text is guaranteed to contain (anchored on the QUOTED
    string-literal spelling of "name", not a bare `name` occurrence -- a
    variable, kwarg, or docstring merely containing the word `name` does
    NOT satisfy any of these):

        os.name = ...                        -> contains b"os.name"
        setattr(os, "name", ...)             -> contains b'"name"'
        monkeypatch.setattr(os, 'name', ...) -> contains b"'name'"
        patch.object(os, "name", ...)        -> contains b'"name"'
        mock.patch("os.name", ...)           -> contains b'"os.name"',
                                                 a superset of b"os.name"

    A file containing none of `os.name`, `"name"`, or `'name'` cannot
    satisfy `_is_os_name_attr`, `_is_os_name_string_literal`, or the
    inlined `args[1].value == "name"` checks, regardless of formatting or
    whitespace around the `os` token -- this is a strict superset of the
    matcher's trigger set, never a same-shape re-implementation of it.

    KNOWN RESIDUAL HAZARD, closed structurally rather than argued away:
    a source form where the matcher fires but neither literal appears
    contiguously in the file bytes -- e.g. implicit string concatenation
    (`setattr(os, "na" "me", x)`) or line-continuation inside an attribute
    access (`(os\n.name)`). `test_prefilter_matches_unfiltered_scan` runs
    `_find_violations` over the WHOLE tree with this prefilter disabled and
    asserts the offender map is identical to the prefiltered path, so any
    such gap fails that check rather than silently under-detecting here.
    Do not "optimize" one of these two tests without re-running the other.
    """
    return (
        b"os.name" not in data
        and b'"name"' not in data
        and b"'name'" not in data
    )


def _scan_offenders(*, use_prefilter: bool) -> dict[str, list[int]]:
    """Shared scan body. `use_prefilter=False` is the ground truth the
    cadence cross-check (`test_prefilter_matches_unfiltered_scan`) compares
    the fast, prefiltered path against -- see `_cannot_contain_os_name_site`'s
    docstring for why that comparison, not a docstring argument, is what
    backs the prefilter's soundness."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(_iter_test_files(_REPO_ROOT)):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if use_prefilter and _cannot_contain_os_name_site(data):
            continue
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        lines = _find_violations(tree)
        if lines:
            offenders[path.relative_to(_REPO_ROOT).as_posix()] = lines
    return offenders


@functools.lru_cache(maxsize=None)
def _observed_offenders() -> dict[str, list[int]]:
    return _scan_offenders(use_prefilter=True)


def _load_baseline() -> dict[str, int]:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))["baseline"]


def test_no_new_os_name_offender_beyond_the_baseline():
    """The ratchet: a site in an unlisted file, or beyond a listed file's
    recorded ceiling, is a NEW offender -- regrowth, not debt already on
    the books. Not an allowlist: a listed file still fails the moment its
    count exceeds its ceiling."""
    observed = _observed_offenders()
    baseline = _load_baseline()
    new_offenders: dict[str, list[int]] = {}
    for path, lines in observed.items():
        ceiling = baseline.get(path, 0)
        if len(lines) > ceiling:
            new_offenders[path] = lines[ceiling:]

    assert not new_offenders, (
        "Test file(s) flip the process-global os.name beyond the recorded "
        "state/tests/os-name-monkeypatch-baseline.json ceiling: "
        + "; ".join(f"{p}:{ls}" for p, ls in sorted(new_offenders.items()))
        + f" -- {_REMEDIATION}"
    )


def test_baseline_ceiling_matches_observed_count_or_lower():
    """Forces the ratchet DOWN: a baseline entry whose recorded ceiling is
    ABOVE the current observed count is stale and must be edited to match
    as work lands -- a ratchet that silently tolerates improvement is not
    a ratchet."""
    observed = _observed_offenders()
    baseline = _load_baseline()
    stale: dict[str, tuple[int, int]] = {}
    for path, ceiling in baseline.items():
        actual = len(observed.get(path, []))
        if actual < ceiling:
            stale[path] = (ceiling, actual)

    assert not stale, (
        "state/tests/os-name-monkeypatch-baseline.json ceiling is stale -- "
        "lower it to match the improved count (path: (ceiling, actual)): "
        + "; ".join(f"{p}: {vals}" for p, vals in sorted(stale.items()))
    )


@pytest.mark.cadence
def test_prefilter_matches_unfiltered_scan():
    """Cadence-tier structural soundness check for
    `_cannot_contain_os_name_site`: re-runs the SAME `_find_violations`
    matcher over the whole tree with the prefilter disabled, and asserts
    the offender map is byte-identical to the prefiltered, cached path
    both `test_no_new_os_name_offender_beyond_the_baseline` and
    `test_baseline_ceiling_matches_observed_count_or_lower` rely on.

    This is the check that makes the prefilter's soundness a property of
    the real tree rather than a docstring argument: a source form the
    prefilter's substring reasoning does not anticipate (implicit string
    concatenation, an attribute access split across a line continuation)
    fails HERE, on the tree as it actually is, rather than silently
    under-detecting in the fast path forever. Do not "optimize" the fast
    tests in this module without re-running this one -- they are the same
    property observed two ways, not two independent checks.
    """
    prefiltered = _observed_offenders()
    unfiltered = _scan_offenders(use_prefilter=False)
    assert prefiltered == unfiltered, (
        "_cannot_contain_os_name_site rejected a file _find_violations "
        "actually flags -- the prefilter is unsound. Prefiltered-only keys: "
        f"{sorted(set(prefiltered) - set(unfiltered))}; "
        f"unfiltered-only keys: {sorted(set(unfiltered) - set(prefiltered))}"
    )

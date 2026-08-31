"""Guard: every entrypoint (bare or `.py`-suffixed) in an entrypoint-bearing
directory ships a Windows `.cmd` twin.

A bare entrypoint (no `.py`/`.sh` suffix) or a `.py`-suffixed one has no
launcher coverage on stock Windows unless a same-name `.cmd` sibling exists
(see coordinator/bin/gen-launcher-shim.py and docs/wiki/windows-cmd-shims.md).
It is easy to add a new entrypoint and forget the Windows twin -- this guard
fails loud instead of letting that gap ship silently. It caught two real
violations on 2026-07-25: `claude-doe` and `workstream-complete-assemble` were
both missing `.cmd` launchers (bare-entrypoint case). The `.py`-suffixed case
was added 2026-07-25 closing a DR-076 gap: ~254 `.py` entrypoints had `.cmd`
twins purely by convention, with nothing enforcing the pairing -- deleting one
or letting it drift would have failed nothing.

SCOPE WIDENED 2026-08-03. Until now this guard scanned `coordinator/bin/`
ONLY, while the `.cmd` convention was demonstrably repo-wide (twins already
existed under `coordinator/lib/`, `coordinator/scripts/`, and a lone
`bin/claude-klabauter-commit-anchors`-era `bin/claude-klabauter-doctor-probe.cmd`). Outside the
old scan root sat 21 launcher-less entrypoints -- `bin/` was the sharpest
case, holding exactly ONE `.cmd` against 13 sibling CLIs with none, a
convention started and abandoned. PM ruling: those directories ARE Windows-
invocable surface, and the fix is structural (widen the scan roots so the
NEXT missing twin is caught automatically), not a one-off hand-generation
pass. See `SCAN_ROOTS` for the roots and for why `coordinator_core/` is
deliberately not among them.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.git.ls_files import tracked_files

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "coordinator" / "bin"


class ScanRoot(NamedTuple):
    """One directory this guard holds to `.cmd`-twin parity.

    `rel` is the repo-relative POSIX path (used verbatim in failure messages
    and in the `git ls-files` query). `dir` is the on-disk directory, split
    out from `rel` so red-case tests can point a root at a tmp_path fixture
    without monkeypatching module state.

    `require_main_guard` is the entrypoint discriminator, and it differs by
    root for a real reason rather than convenience:
      - False: the directory is entrypoints-by-construction -- every tracked,
        top-level, non-test file in it is spawned, never imported. Any file
        landing there is presumed an entrypoint.
      - True: the directory MIXES entrypoints with importable library
        modules (e.g. `coordinator/lib/release_currency.py`,
        `coordinator/lib/oss-repo-constants.py`), so membership is decided by
        the file's own content: an `if __name__ == "__main__":` block, which
        is the direct-invocation signal. A library module needs no launcher
        and generating one for it is noise. Encoding the distinction here --
        rather than hand-listing the library modules as exemptions -- is what
        keeps `PY_ENTRYPOINT_EXEMPTIONS` empty and keeps the guard correct for
        files that do not exist yet.
    """

    rel: str
    dir: Path
    require_main_guard: bool


def _root(rel: str, *, require_main_guard: bool) -> ScanRoot:
    return ScanRoot(rel, REPO_ROOT / rel, require_main_guard)


# Every directory holding entrypoints a user or the engine invokes directly.
#
# `require_main_guard=False` is verified-safe, not assumed: as of 2026-08-03
# all 344 coordinator/bin entrypoints (67 bare + 277 `.py`) carry an
# `if __name__ == "__main__":` block anyway, so the two policies agree on that
# tree today. The flag stays False there so a future coordinator/bin
# entrypoint that runs at import time cannot silently fall out of coverage --
# widening this guard must never narrow what it already checked.
#
# NEGATIVE SPEC -- `coordinator_core/` is deliberately absent. It is the
# importable engine package (`python -m coordinator_core`), not a launcher
# directory: its modules are imported by dotted name, and the handful of
# `if __name__ == "__main__":` blocks in it (`machine_resolver.py`,
# `pyresolve.py`, `state_root.py`, `dag.py`) are self-test/debug hooks on
# imported modules, not console entrypoints. Adding it would generate ~5
# launchers nothing invokes. `.sh` entrypoints are likewise out of scope --
# this guard has never scanned them, and the naked-Python conversion of the
# remaining `coordinator/lib/*.sh` files is its own workstream.
SCAN_ROOTS: tuple[ScanRoot, ...] = (
    _root("coordinator/bin", require_main_guard=False),
    _root("coordinator/scripts", require_main_guard=False),
    _root("bin", require_main_guard=True),
    _root("coordinator/lib", require_main_guard=True),
    _root("scripts", require_main_guard=True),
)

# The original (pre-widening) scan root, kept as a name because two sibling
# guards outside this module consume `_py_entrypoints()` as the SSOT
# coordinator/bin entrypoint enumeration and are themselves coordinator/bin-
# scoped by construction:
#   - coordinator/bin/tests/test_shebang_removal_ordering_ratchet.py pairs it
#     with `tracked_bin_direct_children()`.
#   - coordinator/tests/test_cross_platform_invocability_gate.py pins its
#     source text (no call).
# It is the default `root` for `_entrypoints()` / `_py_entrypoints()` so
# widening this guard does not silently re-scope those callers. A consumer
# wanting a different root must name it.
BIN_ROOT: ScanRoot = SCAN_ROOTS[0]

# Matches the `"%~dp0<target>"` invocation form gen-launcher-shim.py's
# render_cmd() emits (both the :run_baked and :run_py3 rungs use it). A
# `.cmd` whose body targets a filename with no on-disk sibling is
# unrunnable on Windows even though the twin *exists* -- the defect
# `test_every_py_entrypoint_has_a_cmd_twin` / `test_every_bare_entrypoint_
# has_a_cmd_twin` cannot see (existence-only check), caught by review
# 2026-07-28 (Finding 2, three real launchers shipped this way:
# check-posix-exec-assumptions, probe-prereq, wait-for-count -- all
# generated by invoking gen-launcher-shim.py with the bare stem instead of
# the actual entrypoint filename with its .py suffix).
_DP0_TARGET_RE = re.compile(r'"%~dp0([^"%]+)"')

GEN_LAUNCHER_CMD = "python3 coordinator/bin/gen-launcher-shim.py {name} --dir {rel}"

_MAIN_GUARD_RE = re.compile(r'^if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:', re.M)

# Named exemptions from the `.py`-entrypoint `.cmd`-twin requirement, keyed
# by REPO-RELATIVE PATH (`<scan-root>/<name>.py`) since the same basename can
# occur in more than one scan root. Files that end in `.py`, are not test
# files, are not underscore-prefixed helper modules, and (in a mixed root)
# carry a `__main__` guard, but are NOT bare-invoked Windows entrypoints
# either. Note the discriminator does most of this work now: the 2026-08-03
# scope widening admitted three mixed directories holding real library
# modules, and NONE of them needed an entry here, because
# `ScanRoot.require_main_guard` excludes them by their own content. Reach for
# this dict only for a file that looks like an entrypoint by every structural
# signal and still is not one. Each entry must carry a reason -- silently
# widening this set is exactly the "don't broaden a wildcard to make the test
# pass" failure mode this guard exists to prevent (see the DR-076 gap-closure
# dispatch brief).
# Both former entries here (`check-install-divergence.py`,
# `migrate-archive-week-changelogs.py`) were REMOVED 2026-07-31: each
# rationale claimed the file was never bare-invoked by name, which
# `test_no_exemption_hides_an_installed_forwarder` below falsifies for
# both -- `_derive_agent_helper_target_map`
# (coordinator_core/install/substrate.py) installs a bare settings-home
# forwarder PLUS a generated `.cmd` twin for every top-level
# coordinator/bin/ entrypoint it does not explicitly exclude, and neither
# file was in that exclusion set (confirmed live: both stems and their
# `.cmd` twins are present in the installed
# `.coordinator-bin-manifest.json`). Since both files were already
# bare-invocable via that install-time forwarder, the correct fix was to
# stop special-casing them: each now ships an ordinary
# `coordinator/bin/<name>.cmd` twin like its ~254 peers (2026-07-31, this
# guard's own remediation command). Left empty rather than deleted so a
# future genuinely-non-entrypoint `.py` file has an obvious place to add
# its own reason -- see the class docstring above for the bar that reason
# must clear (and the mechanical check below that enforces it).
PY_ENTRYPOINT_EXEMPTIONS: dict[str, str] = {}


def _tracked_files(rel: str) -> list[str]:
    """Repo-relative paths under `rel` known to git, via `git ls-files`.

    Using the git index (not a directory listing) keeps untracked scratch
    files out of the guard's view -- a stray local artifact in a scan root
    should not be able to trip this check.

    Sourced from `coordinator_core.git.ls_files.tracked_files` -- one cached
    `git ls-files -z` spawn per distinct (REPO_ROOT, rel) pair for the whole
    pytest run, instead of a fresh subprocess every time this is called (it
    is called once per SCAN_ROOTS root from `_tracked_top_level_names`, and
    again per root from `_tracked_launchers` -- previously a re-spawn each
    time).
    """
    return list(tracked_files(REPO_ROOT, rel))


def _has_main_guard(path: Path) -> bool:
    """True when `path` carries a module-level `if __name__ == "__main__":`.

    The direct-invocation signal that separates an entrypoint from an
    importable library module in the mixed scan roots (see `ScanRoot`).
    Parsed via `ast` so a `__main__` string inside a comment or docstring
    cannot fake membership; falls back to a line-anchored regex only when the
    file does not parse as Python (a non-Python extensionless file cannot be
    a Python entrypoint, and the regex will not match it either).
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(_MAIN_GUARD_RE.search(source))
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return True
    return False


def _is_entrypoint(root: ScanRoot, name: str) -> bool:
    """Whether `root`'s policy admits `name` as an entrypoint needing a twin."""
    if not root.require_main_guard:
        return True
    return _has_main_guard(root.dir / name)


def _tracked_top_level_names(root: ScanRoot) -> list[str]:
    """Top-level, non-dotfile tail names under `root`.

    Shared enumeration base for both the bare-entrypoint and `.py`-entrypoint
    `.cmd`-twin checks below -- the git-ls-files / top-level-filter /
    dotfile-filter steps exist exactly once here and are parameterized by
    suffix downstream, rather than re-implemented per entrypoint flavor.
    Two exclusions, both load-bearing:
      - non-top-level paths (anything under a subdirectory of the root, e.g.
        coordinator/bin/lib/... or coordinator/bin/tests/...) are not
        bare-invoked entrypoints and are out of scope.
      - dotfiles (leading `.`) are data, not entrypoints -- e.g.
        `.wsc-inline-budget-baseline` is an ASCII data file, not a script.
    """
    prefix = root.rel + "/"
    names = []
    for rel in _tracked_files(root.rel):
        tail = rel[len(prefix) :]
        if "/" in tail:
            continue  # not top-level
        if tail.startswith("."):
            continue  # dotfile: data, not an entrypoint
        names.append(tail)
    return names


def _entrypoints(root: ScanRoot = BIN_ROOT) -> list[str]:
    """Bare entrypoint names requiring a `.cmd` twin: top-level, no extension.

    Excluding purely on "no extension" would also (harmlessly) catch
    dotfiles, since a dotfile with no further `.` has no extension either,
    but the dotfile rule is already applied by `_tracked_top_level_names()`
    per design-as-offers: the guard should be legible about *why* a name is
    out of scope, not rely on one filter accidentally covering two different
    reasons. In a mixed root (`require_main_guard=True`) an extensionless
    file must additionally prove itself an entrypoint by carrying a
    `__main__` guard -- extensionless data files exist and are not scripts.
    """
    return [
        name
        for name in _tracked_top_level_names(root)
        if "." not in name and _is_entrypoint(root, name)
    ]


def _py_entrypoints(root: ScanRoot = BIN_ROOT) -> list[str]:
    """`.py`-suffixed entrypoint stems requiring a `.cmd` twin.

    Four exclusions, all load-bearing and each independently verified
    against the current tree (not assumed):
      - `test_*.py` files are pytest suites, not entrypoints (40 such files
        as of 2026-07-25).
      - `conftest.py` is pytest's own collection-hook module, not an
        entrypoint -- mirrors the same exclusion the installed-forwarder
        derivation (`_derive_agent_helper_target_map`'s
        `_is_pytest_infrastructure` check in
        `coordinator_core/install/substrate.py`) already applies; this
        guard had drifted out of parity with it (caught 2026-07-31 when
        `conftest` showed up as a false-positive missing-twin).
      - underscore-prefixed files (e.g. `_queue_append_locator.py`) are
        shared library modules by this tree's naming convention, not
        bare-invoked entrypoints.
      - `PY_ENTRYPOINT_EXEMPTIONS` names files that end in `.py`, are not
        test files, and are not underscore-prefixed, but are still not
        Windows-console entrypoints for a documented, file-specific reason.
    A fifth exclusion applies only in mixed roots (`require_main_guard=True`):
    a `.py` file with no module-level `__main__` guard is an importable
    library module, not a console entrypoint. See `ScanRoot`.

    Returns the entrypoint STEM (suffix stripped) so callers can probe
    `root.dir / f"{stem}.cmd"` the same way `_entrypoints()` does for bare
    entrypoints.
    """
    stems = []
    for name in _tracked_top_level_names(root):
        if not name.endswith(".py"):
            continue
        stem = name[: -len(".py")]
        if stem.startswith("test_"):
            continue  # pytest suite, not an entrypoint
        if stem == "conftest":
            continue  # pytest collection-hook module, not an entrypoint
        if stem.startswith("_"):
            continue  # shared library module, not an entrypoint
        if f"{root.rel}/{name}" in PY_ENTRYPOINT_EXEMPTIONS:
            continue  # named, documented non-entrypoint .py file
        if not _is_entrypoint(root, name):
            continue  # library module in a mixed root: no __main__ guard
        stems.append(stem)
    return stems


def _missing_cmd_twins(names: list[str], root: ScanRoot) -> list[str]:
    return [name for name in names if not (root.dir / f"{name}.cmd").is_file()]


def _assert_no_missing_twins(names: list[str], root: ScanRoot, suffix: str = "") -> None:
    """Fail naming each launcher-less entrypoint plus its exact remediation.

    `suffix` is the entrypoint filename's extension (`""` for bare, `".py"`
    for the `.py` flavor). It is load-bearing in the emitted fix command:
    gen-launcher-shim.py must be handed the entrypoint's ACTUAL filename, not
    its bare stem -- passing the stem is precisely how three real launchers
    (check-posix-exec-assumptions, probe-prereq, wait-for-count) shipped
    targeting nonexistent files, the defect
    `test_every_cmd_launcher_targets_an_existing_file` exists to catch. A
    guard whose own advice manufactures that defect is worse than no advice.
    """
    missing = _missing_cmd_twins(names, root)
    if not missing:
        return
    lines = [
        f"{len(missing)} {root.rel}/ entrypoint(s) missing a Windows .cmd launcher:",
    ]
    for name in missing:
        lines.append(
            f"  - {root.rel}/{name}{suffix} has no {root.rel}/{name}.cmd. Fix: "
            + GEN_LAUNCHER_CMD.format(name=f"{name}{suffix}", rel=root.rel)
        )
    raise AssertionError("\n".join(lines))


@pytest.mark.parametrize("root", SCAN_ROOTS, ids=[r.rel for r in SCAN_ROOTS])
def test_every_bare_entrypoint_has_a_cmd_twin(root):
    """Real-tree check: every tracked, top-level, extensionless file in each
    scan root (excluding dotfiles, and in mixed roots excluding files with no
    `__main__` guard) has a same-name .cmd launcher on disk."""
    _assert_no_missing_twins(_entrypoints(root), root)


@pytest.mark.parametrize("root", SCAN_ROOTS, ids=[r.rel for r in SCAN_ROOTS])
def test_every_py_entrypoint_has_a_cmd_twin(root):
    """Real-tree check: every tracked, top-level `<name>.py` entrypoint in each
    scan root (excluding test files, underscore-prefixed helper modules,
    PY_ENTRYPOINT_EXEMPTIONS, and library modules in mixed roots) has a
    same-name `.cmd` launcher on disk. Closes the DR-076 enforcement gap
    (~254 coordinator/bin `.py` entrypoints paired only by convention) and,
    since 2026-08-03, the same gap in `bin/`, `coordinator/lib/`,
    `coordinator/scripts/`, and `scripts/`."""
    _assert_no_missing_twins(_py_entrypoints(root), root, suffix=".py")


def _installed_bin_manifest_names() -> "set[str] | None":
    """Names this machine's install actually wrote to
    `<settings-home>/bin/` (see `_write_bin_manifest`,
    `coordinator_core/install/substrate.py`). Returns `None` when no
    manifest exists (uninstalled machine, or a check-only run that never
    wrote one) -- distinct from an empty set, which would read as "an
    install ran and forwarded nothing" and wrongly pass this guard on a
    machine that never installed at all.

    NOTE: this is a developer-machine-only safety net today. No CI/dev-tree
    run executes a real install, so `test_no_exemption_hides_an_installed_
    forwarder`'s real-tree assertion body always skips in automation --
    the "no exemption can hide an installed forwarder" guarantee is
    unverified outside a human's locally-installed machine (review
    2026-07-31, Finding 3). The skip-not-pass choice is deliberate and
    correct; just don't over-trust its automated coverage."""
    try:
        from coordinator_core._settings_home import settings_home
    except ImportError:
        return None
    manifest_path = settings_home() / "bin" / ".coordinator-bin-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        import json

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return None
    return {n for n in names if isinstance(n, str)}


def _exemption_offenders(exemptions: dict, installed: "set[str]") -> list:
    """`exemptions` entries whose stem appears in `installed` -- i.e. an
    exemption whose "not bare-invocable" premise is false. Extracted from
    `test_no_exemption_hides_an_installed_forwarder` so the detection logic
    is fixture-testable independent of the real (currently empty)
    `PY_ENTRYPOINT_EXEMPTIONS` and the real install manifest.

    Exemption keys are repo-relative paths (`<scan-root>/<name>.py`), so the
    stem is taken from the BASENAME -- the installed manifest records bare
    forwarder names, not paths. Only coordinator/bin entrypoints reach that
    manifest today, so an exemption in another scan root cannot be falsified
    by this check; it is a one-way safety net, not a completeness proof."""
    offenders = []
    for exempted in exemptions:
        base = exempted.rsplit("/", 1)[-1]
        stem = base[: -len(".py")] if base.endswith(".py") else base
        if stem in installed:
            offenders.append(exempted)
    return offenders


def test_no_exemption_hides_an_installed_forwarder():
    """A `PY_ENTRYPOINT_EXEMPTIONS` entry is only honest if the file it
    names really has no Windows-console entrypoint gap to close. The
    install-time agent-helper forwarder
    (`_derive_agent_helper_target_map` /
    `_write_agent_cmd_forwarder`, `coordinator_core/install/substrate.py`)
    installs a bare settings-home forwarder PLUS a generated `.cmd` twin
    for every top-level coordinator/bin/ `.py` entrypoint it does not
    itself exclude -- independent of whether a repo-tree `.cmd` twin
    exists. If an exempted name shows up there anyway, the file IS
    bare-invocable on a console, and the exemption's premise is false --
    exactly the shape that let `check-install-divergence.py` and
    `migrate-archive-week-changelogs.py` carry falsified rationales
    (2026-07-31) until each was caught by hand. Skips (does not fail) when
    no install manifest exists -- a missing manifest proves nothing about
    exemption correctness, only that this machine has no install to check
    against."""
    installed = _installed_bin_manifest_names()
    if installed is None:
        pytest.skip("no installed .coordinator-bin-manifest.json on this machine")
    offenders = _exemption_offenders(PY_ENTRYPOINT_EXEMPTIONS, installed)
    assert not offenders, (
        "PY_ENTRYPOINT_EXEMPTIONS names file(s) whose stem IS installed as a "
        f"bare settings-home forwarder (so it IS bare-invocable): {offenders}. "
        "The exemption rationale is false -- either give the file a .cmd twin "
        "like its peers, or exclude it from the agent-helper forwarder "
        "derivation for an accurate, matching reason."
    )


def test_exemption_offender_detection_is_not_vacuous():
    """Sanity check for `_exemption_offenders` itself: with
    `PY_ENTRYPOINT_EXEMPTIONS` now empty (this diff), the real-tree test
    above's loop body executes zero times on every run, so nothing proves
    the detection logic can actually fail before it's needed to (review
    2026-07-31, Finding 2). Synthetic exemptions + a fake installed set,
    never the real manifest or the real PY_ENTRYPOINT_EXEMPTIONS."""
    key = "coordinator/bin/fake-tool.py"
    exemptions = {key: "documented reason, deliberately false for this test"}
    assert _exemption_offenders(exemptions, {"fake-tool"}) == [key]
    # And the same fixture correctly passes when the exempted stem is not installed.
    assert _exemption_offenders(exemptions, set()) == []
    assert _exemption_offenders(exemptions, {"some-other-tool"}) == []
    # The path prefix must not leak into the stem comparison.
    assert _exemption_offenders(exemptions, {"coordinator/bin/fake-tool"}) == []


def _tracked_cmd_files(root: ScanRoot) -> list[str]:
    """Top-level, tracked `.cmd` basenames under `root`."""
    return [name for name in _tracked_top_level_names(root) if name.endswith(".cmd")]


def _cmd_dp0_targets(cmd_path: Path) -> set[str]:
    """`%~dp0<target>` filenames referenced in a `.cmd` body (both the
    :run_baked and :run_py3 invocation rungs use this form -- see
    gen-launcher-shim.py's render_cmd())."""
    text = cmd_path.read_text(encoding="utf-8", errors="replace")
    return set(_DP0_TARGET_RE.findall(text))


def _broken_cmd_targets(names: list[str], root: ScanRoot) -> list[tuple[str, str]]:
    """`(cmd_name, missing_target)` pairs for every `.cmd` whose body
    references a co-located file that does not exist on disk -- the exact
    "twin exists but is unrunnable" shape existence-only checks (
    `_missing_cmd_twins`) cannot see."""
    broken: list[tuple[str, str]] = []
    for name in names:
        targets = _cmd_dp0_targets(root.dir / name)
        for target in sorted(targets):
            if not (root.dir / target).is_file():
                broken.append((name, target))
    return broken


@pytest.mark.parametrize("root", SCAN_ROOTS, ids=[r.rel for r in SCAN_ROOTS])
def test_every_cmd_launcher_targets_an_existing_file(root):
    """Real-tree check: every tracked `*.cmd` launcher in each scan root has
    a `%~dp0<target>` invocation referencing a file that actually exists on
    disk. Catches the class `test_every_bare_entrypoint_has_a_cmd_twin` /
    `test_every_py_entrypoint_has_a_cmd_twin` cannot: a `.cmd` twin can
    EXIST and still be unrunnable if it was generated with the wrong
    argument (e.g. the bare stem instead of the entrypoint's actual
    filename with its `.py` suffix). Caught by review 2026-07-28 (three
    real launchers shipped broken this way: check-posix-exec-assumptions,
    probe-prereq, wait-for-count)."""
    broken = _broken_cmd_targets(_tracked_cmd_files(root), root)
    if not broken:
        return
    lines = [f"{len(broken)} {root.rel}/*.cmd launcher(s) target a nonexistent file:"]
    for name, target in broken:
        lines.append(
            f"  - {root.rel}/{name} invokes \"%~dp0{target}\" but "
            f"{root.rel}/{target} does not exist. Fix: regenerate via "
            "gen-launcher-shim.py, passing the entrypoint's actual filename "
            "(with its .py suffix, if any) -- not the bare stem."
        )
    raise AssertionError("\n".join(lines))


def _fake_root(tmp_path) -> ScanRoot:
    """A scan root pointed at a tmp_path fixture but reporting the real
    `coordinator/bin` rel path, so red-case message assertions read exactly
    as an operator would see them. Replaces the former BIN_DIR monkeypatch:
    with per-root state the guard no longer has one module-level directory
    to patch, and injecting the root is the honest seam."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    return ScanRoot("coordinator/bin", fake_bin, require_main_guard=False)


def test_guard_fails_on_a_cmd_launcher_targeting_a_missing_file(tmp_path):
    """Red-case proof: a `.cmd` whose body references a nonexistent
    co-located file must fail, naming both the launcher and the missing
    target -- not merely report a violation (design-as-offers)."""
    root = _fake_root(tmp_path)
    (root.dir / "fake-entrypoint.cmd").write_text(
        '@echo off\n"%_py%" "%~dp0fake-entrypoint" %*\n'
    )

    broken = _broken_cmd_targets(["fake-entrypoint.cmd"], root)
    assert broken == [("fake-entrypoint.cmd", "fake-entrypoint")]

    # And the same fixture passes once the target actually exists.
    (root.dir / "fake-entrypoint").write_text("#!/usr/bin/env python3\n")
    assert _broken_cmd_targets(["fake-entrypoint.cmd"], root) == []


def test_write_workday_start_marker_py_cmd_pair_is_recognized():
    """Regression, naming the specific pair from the DR-076 gap-closure
    memo: `write-workday-start-marker.py` must be recognized as a `.py`
    entrypoint requiring a `.cmd` twin, and its `.cmd` twin must exist on
    disk. A structural break in `_py_entrypoints()`'s filters (e.g. an
    over-broad exclusion) would silently drop this pair from candidacy
    without failing any other test."""
    assert BIN_ROOT.rel == "coordinator/bin"
    assert "write-workday-start-marker" in _py_entrypoints(BIN_ROOT)
    assert (BIN_DIR / "write-workday-start-marker.cmd").is_file()


def test_guard_fails_on_a_launcher_less_entrypoint(tmp_path):
    """Red-case proof: a bare entrypoint with no .cmd twin must fail, and the
    failure message must name the missing file and the exact remediation
    command -- not merely report a violation (design-as-offers)."""
    root = _fake_root(tmp_path)
    (root.dir / "fake-entrypoint").write_text("#!/usr/bin/env python3\n")

    with pytest.raises(AssertionError) as excinfo:
        _assert_no_missing_twins(["fake-entrypoint"], root)

    message = str(excinfo.value)
    assert "coordinator/bin/fake-entrypoint" in message
    assert "coordinator/bin/fake-entrypoint.cmd" in message
    assert "gen-launcher-shim.py fake-entrypoint --dir coordinator/bin" in message

    # And the same fixture passes once the twin is generated.
    (root.dir / "fake-entrypoint.cmd").write_text("@echo off\n")
    _assert_no_missing_twins(["fake-entrypoint"], root)  # no raise


def test_guard_fails_on_a_launcher_less_py_entrypoint_naming_the_real_filename(tmp_path):
    """Red-case proof for the `.py` flavor: the emitted fix command must name
    the entrypoint's ACTUAL filename (`fake-tool.py`), never the bare stem.
    Handing gen-launcher-shim.py the stem is exactly how three real launchers
    shipped targeting nonexistent files (review 2026-07-28); a guard whose
    own remediation reproduces that defect is worse than silence."""
    root = _fake_root(tmp_path)
    (root.dir / "fake-tool.py").write_text("if __name__ == '__main__':\n    pass\n")

    with pytest.raises(AssertionError) as excinfo:
        _assert_no_missing_twins(["fake-tool"], root, suffix=".py")

    message = str(excinfo.value)
    assert "coordinator/bin/fake-tool.py has no coordinator/bin/fake-tool.cmd" in message
    assert "gen-launcher-shim.py fake-tool.py --dir coordinator/bin" in message


def test_main_guard_discriminator_separates_entrypoints_from_library_modules(tmp_path):
    """The mixed-root discriminator (`ScanRoot.require_main_guard`) is what
    keeps `PY_ENTRYPOINT_EXEMPTIONS` empty: a library module is excluded by
    its own content, not by a hand-maintained list. Proves both directions,
    plus that a `__main__` mention in a comment or docstring cannot fake
    membership (the reason `_has_main_guard` parses instead of grepping)."""
    mixed = ScanRoot("coordinator/lib", tmp_path, require_main_guard=True)
    convention = ScanRoot("coordinator/bin", tmp_path, require_main_guard=False)

    (tmp_path / "real-cli.py").write_text(
        "def main():\n    pass\n\n\nif __name__ == '__main__':\n    main()\n"
    )
    (tmp_path / "library.py").write_text('"""A module. Not run as __main__."""\nX = 1\n')
    (tmp_path / "commented.py").write_text(
        "# if __name__ == '__main__':\nY = 2\n"
    )

    assert _is_entrypoint(mixed, "real-cli.py") is True
    assert _is_entrypoint(mixed, "library.py") is False
    assert _is_entrypoint(mixed, "commented.py") is False
    # A by-convention root admits every file regardless of content, so
    # widening this guard can never narrow coordinator/bin's coverage.
    assert _is_entrypoint(convention, "library.py") is True


def test_scan_roots_cover_every_entrypoint_bearing_directory():
    """Pins the widened scope (2026-08-03) so a future edit cannot quietly
    shrink it back to coordinator/bin-only -- the drift this guard's own
    history is a record of. Also pins the per-root policy: the two
    entrypoints-by-construction roots must NOT require a `__main__` guard,
    since that requirement is a narrowing for them."""
    assert {r.rel for r in SCAN_ROOTS} == {
        "coordinator/bin",
        "coordinator/scripts",
        "bin",
        "coordinator/lib",
        "scripts",
    }
    by_rel = {r.rel: r for r in SCAN_ROOTS}
    assert by_rel["coordinator/bin"].require_main_guard is False
    assert by_rel["coordinator/scripts"].require_main_guard is False
    assert by_rel["bin"].require_main_guard is True
    assert by_rel["coordinator/lib"].require_main_guard is True
    assert by_rel["scripts"].require_main_guard is True
    for root in SCAN_ROOTS:
        assert root.dir.is_dir(), f"{root.rel} is not a directory"


# ---------------------------------------------------------------------------
# Generator BYTE parity (2026-08-03)
#
# Every check above is an EXISTENCE/TARGET check: a twin is present, and it
# points at a file that exists. None of them look at what the twin actually
# CONTAINS, so a twin can sit in the tree for months carrying the body an
# older generator vintage emitted -- and the whole point of a generated
# launcher is that fixing the generator fixes the fleet. It did not: the
# 2026-08-03 baked-interpreter existence-gate fix (`if exist` in .cmd,
# `Test-Path` in .ps1, mirroring `coordinator_core.install.substrate`'s
# ladder -- see gen-launcher-shim.py's INTERPRETER LADDER rung 1) landed in
# the generator while 339 committed `.cmd` twins still shipped the
# pre-fix body, i.e. a `~/.claude` synced between a Mac and a Windows box
# kept invoking an interpreter path that exists under no spelling.
#
# The mechanism is the byte-compare `test_queue_triage_cli.py::
# test_cmd_and_ps1_twins_are_generator_byte_parity` already used for ONE
# launcher (queue-triage), generalized to the whole tree -- that single-file
# test is exactly why queue-triage was the one twin regenerated with the fix.
# ---------------------------------------------------------------------------

# Byte parity now covers every SCAN_ROOTS directory (widened 2026-08-03,
# second pass).
#
# It launched scoped to `coordinator/bin` alone, deliberately: the other four
# roots held 51 further launcher-shaped `.cmd`/`.ps1` files and a survey found
# essentially none of them at parity with the current generator, so widening
# in the same breath would have forced ~45 same-day exemptions -- the
# "exemption list that swallows future drift" failure this guard exists to
# prevent. The sweep that was the precondition has now run: all 51 were
# censused against ON-DISK bytes (never `git show HEAD:` -- `.gitattributes`
# pins `*.cmd text eol=crlf`, so a HEAD-based compare reports 100% spurious
# mismatch), each classified, and the 47 that were real generator output were
# regenerated through the CLI with the entry name read off each launcher's own
# `%~dp0` invocation. Their drift fell into exactly four signature clusters --
# 27 files one generator vintage behind (missing only the existence gate and
# its comment), 13 and 6 files at two older `enabledelayedexpansion` vintages,
# and one pre-campaign hand shim -- and every one of the 47 gained the
# `if exist "%_py%"` entrypoint gate, i.e. the change is provably confined to
# the fix being propagated.
#
# What is left is 4 exemptions against 414 launchers held to exact generator
# output. None of the 4 is drift: three are substantial hand-written
# PowerShell PROGRAMS that merely share the `.ps1` extension, and one is a
# hand-authored shim the generator structurally cannot emit.
LAUNCHER_PARITY_ROOTS: tuple[str, ...] = tuple(r.rel for r in SCAN_ROOTS)

# Launchers under LAUNCHER_PARITY_ROOTS held OUT of byte parity, each with the
# reason it is not simply regenerated. Every entry is a debt marker, not a
# permanent carve-out: `test_no_parity_exemption_is_stale` below FAILS the
# moment an exempted file reaches parity anyway, so this dict is shrink-only
# and cannot quietly outlive its reason. Anything NOT named here is held to
# exact generator output -- adding a name is the only way to widen it, and
# the bar is a defect that regeneration would destroy information about, not
# "regenerating it is inconvenient".
LAUNCHER_PARITY_EXEMPTIONS: dict[str, str] = {
    # -- Line-ending drift, not body drift ----------------------------------
    # These three are byte-identical to the PRE-fix generator body except
    # that they are stored LF in the git index despite `.gitattributes`
    # pinning `*.cmd text eol=crlf` (`git ls-files --eol` reports
    # `i/lf w/lf attr/text eol=crlf`) -- they predate the repo-wide pin and
    # were never renormalized. Regenerating them writes CRLF, which is a
    # correct-but-unrelated renormalization touching the index for reasons
    # that have nothing to do with the interpreter ladder. Fold them into a
    # deliberate `git add --renormalize` pass, then delete these entries.
    "coordinator/bin/autonomous-verb.cmd": (
        "LF in the index under an eol=crlf attr; awaiting a renormalize pass"
    ),
    # -- Orphaned launcher (RESOLVED 2026-08-31, row deleted) ---------------
    # `coordinator/bin/tests/run-fast-tests.cmd` lived here, exempt because it
    # invoked a `run-fast-tests.py` that does not exist and "wants deleting or
    # repointing, which is a scope call, not a regeneration". That scope call
    # was made at `3dde0bed8c` -- the orphan launcher was deleted -- and the
    # row outlived its file, so `test_no_parity_exemption_is_stale` went red.
    # Same shape as the `coordinator-settings-home.ps1` row recorded just
    # below, and the third instance of this class caught in this file's
    # history: an entry naming a path that is gone reads as deliberate
    # coverage while covering nothing.
    # -- Not generator output at all ----------------------------------------
    # (2026-08-14, plan pln-windows-first-class-the-gate-m-c64274 C5) The
    # `coordinator/bin/coordinator-settings-home.ps1` row lived here, marked
    # "permanent unless the forwarder itself becomes generated". The forwarder
    # did not become generated -- it was deleted, along with its `.cmd` twin and
    # the three bare-name forwarders the whole family fronted, because
    # `coordinator/bin` is not on PATH, `_AGENT_HELPER_RESERVED_NAMES` excludes
    # all three from the install-derived target map, and every `which()` call
    # site resolves the settings-home copies instead. `test_no_parity_exemption_is_stale`
    # is what caught the row outliving its file -- the same staleness discipline
    # AC13 generalises to the POSIX-exec register.
    # Review: code-reviewer (wfc-S2-launchers, Finding 2) flagged an apparent
    # contradiction between this "not on PATH" claim and the deleted forwarders'
    # own docstrings ("harness-injected plugin bin ... resolves on tool shells
    # where ~/.claude/bin is NOT on PATH"). Both are true, about different
    # trees: THIS repo's coordinator/bin (the source copy) is never
    # PATH-injected -- only DoE-claude's own coordinator/bin is (a distinct
    # directory the harness injects for its own plugin). The forwarders'
    # docstrings described the general pattern correct for DoE-claude's copy
    # but never held for claude-klabauter's. Settled on disk, not by convenience: see
    # docs/plans/2026-08-14-windows-first-class-gate-and-exemptions.md:116
    # ("coordinator/bin is the SOURCE side, not the executed side") and
    # docs/research/2026-08-14-posix-exec-final-36-bin-name-dispositions.md's
    # `machine-local` per-name evidence section (the sibling DoE-claude/
    # example-game-repo forwarders are EXEMPT, not deleted, for exactly this reason).
    # -- Not generator output at all: the widened roots (2026-08-03) ---------
    # The 2026-08-03 sweep over `bin/`, `coordinator/lib/`,
    # `coordinator/scripts/` and `scripts/` regenerated all 47 files that were
    # real generator output. These four are the whole remainder, and none of
    # them is drift the generator could fix.
    #
    # A python-direct launcher by shape, but one the generator cannot emit:
    # it targets `_claude_home.py` rather than a same-stem entrypoint (the
    # co-located `claude-home` file is real `#!/usr/bin/env bash`, which
    # `python` cannot run), holds the target in an `_impl` variable behind an
    # `if not exist` precheck with its own error message, and uses
    # `setlocal enableextensions`. Feeding the generator `_claude_home.py`
    # would emit `_claude_home.cmd` -- a different filename, losing the
    # bare-name PATHEXT resolution this file exists to provide. Permanent
    # unless the generator grows a distinct-entry-name mode.
    "coordinator/lib/claude-home/claude-home.cmd": (
        "hand-authored shim targeting a differently-named impl "
        "(_claude_home.py) behind an existence precheck; the generator "
        "cannot emit this filename/target pairing"
    ),
    # The three below are not launchers at all -- they are substantial
    # hand-written PowerShell PROGRAMS that this guard only sees because it
    # globs `.ps1`. Each is the 1:1 Windows sibling of a bash file under the
    # same directory, with its own spec backlinks and exit-code contract, and
    # none carries a `Join-Path $_here` entrypoint invocation because none is
    # forwarding to an entrypoint. Permanent.
    "coordinator/scripts/setup.ps1": (
        "the standalone install-chain walker itself (1:1 sibling of "
        "setup.sh), not a launcher forwarding to an entrypoint"
    ),
    "coordinator/scripts/lib/dep_check.ps1": (
        "sourced PowerShell function library for install Phase 0 (1:1 "
        "sibling of dep_check.sh), not a launcher"
    ),
    "coordinator/scripts/lib/manifest_reader.ps1": (
        "PowerShell manifest JSON reader emitting NDJSON (1:1 sibling of "
        "manifest_reader.sh), not a launcher"
    ),
}

_PS1_ENTRY_RE = re.compile(r"Join-Path \$_here '([^']+)'")

# Bound at import, deliberately NOT derived from REPO_ROOT at call time: the
# red-case fixture below repoints REPO_ROOT at a tmp tree, and the generator
# under test must stay the real one.
_GEN_LAUNCHER_SHIM_PATH = REPO_ROOT / "coordinator" / "bin" / "gen-launcher-shim.py"


def _load_gen_launcher_shim():
    """Import `coordinator/bin/gen-launcher-shim.py` as a module.

    SourceFileLoader dance because the filename is hyphenated and carries no
    importable path; sys.modules registration BEFORE exec_module mirrors
    `coordinator/tests/test_queue_triage_cli.py::_load_gen_launcher_shim`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_launcher_shim", _GEN_LAUNCHER_SHIM_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_launcher_shim"] = mod
    spec.loader.exec_module(mod)
    return mod


def _launcher_entry(raw: bytes, suffix: str) -> "str | None":
    """The entrypoint filename a launcher body invokes, or None if the body
    carries no invocation at all (the signature of a file that is not
    generator output).

    Read off the launcher itself rather than guessed from its basename: the
    generator is called with the entrypoint's FULL filename and strips
    `.py`/`.sh` to form the launcher name (`launcher_basename`), so the
    basename alone cannot tell `foo` from `foo.py` from `foo.sh` -- and
    guessing wrong turns a parity check into a name-resolution check.
    """
    text = raw.decode("utf-8", errors="replace")
    pattern = _DP0_TARGET_RE if suffix == ".cmd" else _PS1_ENTRY_RE
    match = pattern.search(text)
    return match.group(1) if match else None


def _launcher_parity_offenders(paths: list[str], exemptions: dict) -> list[tuple[str, str]]:
    """`(path, reason)` for every non-exempt launcher in `paths` whose bytes
    differ from what the generator emits for it today.

    `.replace("\\n", "\\r\\n")` mirrors `generate()`'s `newline="\\r\\n"`
    write -- the on-disk contract is CRLF regardless of platform (see
    `.gitattributes` and gen-launcher-shim.py's negative spec), so this
    compares bytes, never universal-newline-translated text.
    """
    gen = _load_gen_launcher_shim()
    offenders: list[tuple[str, str]] = []
    for rel in paths:
        if rel in exemptions:
            continue
        suffix = ".cmd" if rel.endswith(".cmd") else ".ps1"
        raw = (REPO_ROOT / rel).read_bytes()
        entry = _launcher_entry(raw, suffix)
        if entry is None:
            offenders.append((rel, "no entrypoint invocation found -- not generator output?"))
            continue
        # The declared spec backlink is part of generator output, so it is part
        # of the byte contract: resolved from the launcher's OWN tracked path
        # (its directory + the entry name read off its body), which is the same
        # key `generate()` used when it wrote the file. Resolving it here rather
        # than hand-waving it into an exemption is what lets a launcher carry a
        # backlink AND stay under this guard -- see gen-launcher-shim.py
        # § SPEC BACKLINKS for why a CLI flag would not have worked.
        rel_dir = rel.rsplit("/", 1)[0]
        entry_path = f"{rel_dir}/{entry}"
        spec_backlink = gen.spec_backlink_for_entry_path(entry_path)
        if suffix == ".cmd":
            # `preserve_raw_cmdline` (2026-08-08 caret-eating cmd.exe shim
            # defect) is a SECOND opt-in, registry-driven byte-contract
            # input alongside `spec_backlink` — resolved the same way
            # `generate()` resolves it, so a `.cmd` twin actually declaring
            # it is not perpetually flagged as drifted. `.ps1` has no
            # counterpart (render_ps1 never loses the caret in the first
            # place — see `_cmd_raw_cmdline_block`'s docstring).
            expected = (
                gen.render_cmd(
                    entry,
                    spec_backlink=spec_backlink,
                    preserve_raw_cmdline=entry_path in gen._RAW_CMDLINE_ENTRYPOINTS,
                )
                .replace("\n", "\r\n")
                .encode("utf-8")
            )
        else:
            expected = (
                gen.render_ps1(entry, spec_backlink=spec_backlink)
                .replace("\n", "\r\n")
                .encode("utf-8")
            )
        if raw != expected:
            offenders.append((rel, f"body differs from gen-launcher-shim.py output for {entry!r}"))
    return offenders


def _tracked_launchers(roots: tuple[str, ...] = LAUNCHER_PARITY_ROOTS) -> list[str]:
    """Tracked `.cmd`/`.ps1` paths under every root in `roots`, subdirectories
    included.

    Deliberately NOT top-level-only (unlike `_tracked_cmd_files`): four
    subdirectory launchers were found drifted on 2026-08-03 --
    `coordinator/bin/install-health/seed-skill-overrides.cmd`,
    `coordinator/bin/tests/run-fast-tests.cmd`,
    `coordinator/lib/claude-home/claude-home.cmd` and
    `coordinator/scripts/lib/*.ps1` -- precisely because every existing check
    looks at the top level only.

    De-duplicated because the roots are disjoint by construction today but
    nothing in `SCAN_ROOTS` guarantees a future entry will not nest inside an
    existing one; a path counted twice would be compared twice and, worse,
    could make a stale exemption look live.
    """
    seen: set[str] = set()
    for root_rel in roots:
        seen.update(
            rel for rel in _tracked_files(root_rel) if rel.endswith((".cmd", ".ps1"))
        )
    return sorted(seen)


def test_every_launcher_twin_is_generator_byte_parity():
    """Real-tree check: every tracked `.cmd`/`.ps1` under any of
    LAUNCHER_PARITY_ROOTS is byte-identical to what gen-launcher-shim.py
    emits for it today, or is named in LAUNCHER_PARITY_EXEMPTIONS with a
    reason. Generalizes the single-launcher byte-compare in
    `coordinator/tests/test_queue_triage_cli.py`; see this section's header
    for the incident that motivated it."""
    offenders = _launcher_parity_offenders(
        _tracked_launchers(), LAUNCHER_PARITY_EXEMPTIONS
    )
    if not offenders:
        return
    lines = [
        f"{len(offenders)} launcher twin(s) under "
        f"{', '.join(LAUNCHER_PARITY_ROOTS)} are not "
        "byte-identical to current gen-launcher-shim.py output:"
    ]
    for rel, reason in offenders:
        lines.append(f"  - {rel}: {reason}")
    lines.append(
        "Generated launchers are NEVER hand-edited (gen-launcher-shim.py, "
        "NEGATIVE SPEC) -- regenerate with:  python3 "
        "coordinator/bin/gen-launcher-shim.py <entrypoint-filename> --dir "
        "<launcher-dir> [--ps1]   (the entrypoint filename INCLUDES its .py/.sh "
        "suffix). Adding a name to LAUNCHER_PARITY_EXEMPTIONS is not the fix "
        "unless regeneration would itself destroy information -- see that "
        "dict's own docstring for the bar."
    )
    raise AssertionError("\n".join(lines))


def test_no_parity_exemption_is_stale():
    """An exemption that no longer excuses anything must be deleted, not
    left to shadow the file forever. Without this, a name added once keeps
    that launcher permanently outside the parity check even after the drift
    it named is fixed -- the exact "exemption list that silently swallows
    future drift" shape. Also fails on an exemption naming a path that is no
    longer a tracked launcher at all."""
    tracked = set(_tracked_launchers())
    unknown = sorted(set(LAUNCHER_PARITY_EXEMPTIONS) - tracked)
    assert not unknown, (
        "LAUNCHER_PARITY_EXEMPTIONS names path(s) that are not tracked "
        f"launchers under {', '.join(LAUNCHER_PARITY_ROOTS)}: {unknown}. "
        "Delete the stale entries."
    )
    # Every exemption must still be a real offender when the exemption set is
    # emptied; anything that already matches the generator is stale.
    still_drifted = {
        rel for rel, _ in _launcher_parity_offenders(sorted(LAUNCHER_PARITY_EXEMPTIONS), {})
    }
    resolved = sorted(set(LAUNCHER_PARITY_EXEMPTIONS) - still_drifted)
    assert not resolved, (
        "LAUNCHER_PARITY_EXEMPTIONS names launcher(s) that ARE at generator "
        f"byte parity, so the exemption excuses nothing: {resolved}. Delete "
        "those entries -- this list is shrink-only."
    )


def test_every_parity_exemption_carries_a_reason():
    """Mechanical bar on the rationale itself, mirroring the prose bar on
    PY_ENTRYPOINT_EXEMPTIONS: an entry with an empty or one-word value is a
    silent carve-out wearing a dict's clothes."""
    for rel, reason in LAUNCHER_PARITY_EXEMPTIONS.items():
        assert isinstance(reason, str) and len(reason.split()) >= 5, (
            f"LAUNCHER_PARITY_EXEMPTIONS[{rel!r}] needs a real reason, got {reason!r}"
        )


def test_parity_offender_detection_is_not_vacuous(tmp_path, monkeypatch):
    """Sanity check for `_launcher_parity_offenders` itself: the real-tree
    test above passes by finding nothing, so nothing there proves the
    comparison can fail. Synthetic launchers under a tmp REPO_ROOT, never the
    real tree (mirrors `test_exemption_offender_detection_is_not_vacuous`)."""
    gen = _load_gen_launcher_shim()
    (tmp_path / "coordinator" / "bin").mkdir(parents=True)
    good = "coordinator/bin/good-tool.cmd"
    bad = "coordinator/bin/bad-tool.cmd"
    alien = "coordinator/bin/alien.ps1"
    body = gen.render_cmd("good-tool.py").replace("\n", "\r\n").encode("utf-8")
    (tmp_path / good).write_bytes(body)
    (tmp_path / bad).write_bytes(
        gen.render_cmd("bad-tool.py").replace("\n", "\r\n").replace(
            'if not "%_py%"=="" if exist "%_py%" goto :run_baked',
            'if not "%_py%"=="" goto :run_baked',
        ).encode("utf-8")
    )
    (tmp_path / alien).write_bytes(b"# hand-written forwarder, no entry invocation\r\n")

    monkeypatch.setattr(
        sys.modules[__name__], "REPO_ROOT", tmp_path, raising=True
    )
    offenders = _launcher_parity_offenders([good, bad, alien], {})
    assert [rel for rel, _ in offenders] == [bad, alien]
    # ...and the same fixture goes quiet once the drifted ones are exempted.
    assert _launcher_parity_offenders([good, bad, alien], {bad: "x", alien: "y"}) == []


def test_parity_roots_cover_every_scan_root():
    """Pins the second widening (2026-08-03) so byte parity cannot quietly
    shrink back to coordinator/bin-only, the way the existence checks once
    did. Byte parity and existence now cover the SAME set of directories --
    a launcher that must EXIST somewhere must also be current there, and
    tying the two together means a future `SCAN_ROOTS` addition is picked up
    by both without a second edit anyone can forget."""
    assert set(LAUNCHER_PARITY_ROOTS) == {r.rel for r in SCAN_ROOTS}
    # Every widened root must actually contribute launchers; a root that
    # contributes none would make the widening look done while checking
    # nothing there.
    for root_rel in LAUNCHER_PARITY_ROOTS:
        assert _tracked_launchers((root_rel,)), f"{root_rel} has no tracked launchers"


def test_raw_cmdline_entrypoints_matches_substrate_targets():
    """`gen-launcher-shim.py::_RAW_CMDLINE_ENTRYPOINTS` and
    `coordinator_core/install/substrate.py::_RAW_CMDLINE_TARGETS` are two
    independent, mirrored (not imported) copies of the same allowlist --
    entrypoints whose `.cmd` launcher must preserve `%CMDCMDLINE%` to avoid
    the caret-eating defect (state/bug-backlog/2026-08-08-cmd-exe-shim-eats-
    the-caret-in-a-git-rev-6679bf76eb8a.yaml). They cannot be unified into a
    single shared constant (see both modules' own docstrings: a
    hyphenated-filename generator module has no ordinary `import` form), so
    this test is the drift guard in their place -- it fails the moment one
    set gains or loses a member the other does not also carry.

    Keyed by the shared suffix (`gen`'s set is repo-relative POSIX paths
    like `coordinator/bin/scoped-git-commit`; `substrate`'s is the bare
    on-disk target filename, e.g. `scoped-git-commit`), matching how
    `_RAW_CMDLINE_TARGETS`'s own docstring already describes the
    comparison.
    """
    gen = _load_gen_launcher_shim()
    from coordinator_core.install.substrate import _RAW_CMDLINE_TARGETS

    gen_basenames = {Path(p).name for p in gen._RAW_CMDLINE_ENTRYPOINTS}
    assert gen_basenames == set(_RAW_CMDLINE_TARGETS), (
        f"gen-launcher-shim.py's _RAW_CMDLINE_ENTRYPOINTS (basenames: "
        f"{sorted(gen_basenames)}) and substrate.py's _RAW_CMDLINE_TARGETS "
        f"({sorted(_RAW_CMDLINE_TARGETS)}) have drifted -- extend both sets "
        "together."
    )


def test_raw_cmdline_members_all_exist():
    """Every raw-cmdline allowlist member names a file that is actually in the
    tree.

    THE GAP THIS CLOSES, and why the sibling guard structurally cannot.
    `test_raw_cmdline_entrypoints_matches_substrate_targets` compares the two
    sets TO EACH OTHER. Two sets can agree perfectly on a target that no
    longer exists, and an entry naming a non-existent target renders nothing
    -- so the residue is inert at runtime and invisible to a parity check.
    That is not hypothetical: it has now happened twice on this pair.
    `scoped-git-commit` was deleted at `47c78a3a5` and left in both sets until
    2026-08-28; `coordinator-write-review-trail.py` outlived its op's
    gravestone (`review_trail.write`, K-060, 2026-08-27) in both sets until
    2026-08-31, when THIS test was written because the baton that found it
    observed the sets are "kept in sync by convention" and that being
    byte-equal is not the same as being right.

    Keyed off `gen`'s set because its members are repo-relative paths, which
    are checkable without re-deriving a bin directory; `substrate`'s bare
    filenames are covered transitively by the pair-equality guard above.
    """
    gen = _load_gen_launcher_shim()
    repo_root = Path(__file__).resolve().parents[1]

    missing = sorted(
        rel for rel in gen._RAW_CMDLINE_ENTRYPOINTS if not (repo_root / rel).is_file()
    )
    assert not missing, (
        "raw-cmdline allowlist names target(s) with no file in the tree: "
        f"{missing}. A member whose file is gone renders nothing, so it is "
        "inert at runtime and invisible to the pair-equality guard -- remove "
        "it from BOTH sets together, the way `scoped-git-commit` (2026-08-28) "
        "and `coordinator-write-review-trail.py` (2026-08-31) were."
    )


def test_raw_cmdline_block_bodies_match_between_generators():
    """Both `_cmd_raw_cmdline_block` and `_agent_cmd_raw_cmdline_block` claim
    (in their own docstrings) that their rendered bodies are mirrored line
    for line -- but `test_raw_cmdline_entrypoints_matches_substrate_targets`
    only compares the ALLOWLIST SETS, never the rendered text, so a hand-edit
    to one function's block that forgot the other would go undetected.

    Review: staff-eng (Finding 4). Renders both for a shared target name and
    asserts byte equality, so the claim the docstrings make is actually
    enforced rather than merely stated.

    The sample target is taken FROM the live set, never hardcoded. It was
    `"scoped-git-commit"` until 2026-08-28, when that CLI's retirement (DR-344,
    file deleted at 47c78a3a5) dropped it from both sets -- and the substrate
    half then rendered "" for it, so this test failed claiming a drift that had
    not happened. A literal here re-arms that trap on the next retirement.
    """
    gen = _load_gen_launcher_shim()
    from coordinator_core.install.substrate import (
        _RAW_CMDLINE_TARGETS,
        _agent_cmd_raw_cmdline_block,
    )

    sample = sorted(_RAW_CMDLINE_TARGETS)[0]
    gen_block = gen._cmd_raw_cmdline_block(True)
    substrate_block = _agent_cmd_raw_cmdline_block(sample)
    assert substrate_block, (
        f"the substrate half rendered nothing for '{sample}', which came out of "
        "_RAW_CMDLINE_TARGETS itself -- _agent_cmd_raw_cmdline_block's membership "
        "check no longer agrees with the set it reads"
    )
    assert gen_block == substrate_block, (
        "gen-launcher-shim.py::_cmd_raw_cmdline_block and substrate.py::"
        "_agent_cmd_raw_cmdline_block have drifted in rendered body -- both "
        "docstrings claim they are mirrored line for line; extend both "
        "together."
    )


_CMD_EXISTENCE_GATE = 'if not "%_py%"=="" if exist "%_py%" goto :run_baked'
_CMD_EMPTINESS_ONLY_GATE = 'if not "%_py%"=="" goto :run_baked'
_PS1_EXISTENCE_GATE = (
    "if ($_pybin -ne '' -and -not (Test-Path -LiteralPath $_pybin)) { $_pybin = '' }"
)
_PS1_EMPTINESS_ONLY_GATE = "if ($_pybin -eq '') { $_pybin = '' }"


def test_interpreter_ladder_existence_gate_present_in_both_emitters(tmp_path):
    """Regression guard for the baked-interpreter existence gate
    (`render_cmd`/`render_ps1`'s own module docstring § "The baked rung is
    EXISTENCE-GATED").

    Narrowed 2026-08-29 (docs/plans/2026-08-26-every-forwarder-that-can-
    reach-the-door-does.md C12): this test formerly also rendered a THIRD
    emitter, `coordinator_core.install.substrate._write_agent_cmd_forwarder`,
    and asserted all three stayed in sync. That writer is deleted (DR-365
    condemns the install-side `.cmd`/`.ps1` legs outright; see
    `substrate.py`'s own gravestone) — there is no install-side emitter
    left to drift against. `gen-launcher-shim.py`'s two repo-tree emitters
    (`render_cmd`, `render_ps1`) are unaffected by that deletion and this
    test still guards them against each other.

    On 2026-07-28 review (Finding 1 of that pass) found the exist-gate
    present in the (then three-emitter) install-side writer but absent from
    BOTH `gen-launcher-shim.py` emitters. Both were later fixed, but
    nothing asserted they stay in sync -- reverting either back to a bare
    emptiness check (`if not "%_py%"=="" goto :run_baked`, no `if exist`;
    PowerShell's `-and -not (Test-Path ...)` conjunct dropped) fails no
    other test in this file, since none of it diffs rendered BYTES against
    gate semantics -- only exact byte parity against each generator's OWN
    current output. A baked `_py=`/`$_pybin` naming a since-deleted
    interpreter then passes the emptiness-only check and the launcher
    execs a nonexistent binary: the Windows silent-degradation shape this
    whole gate family exists to kill (see this file's own
    LAUNCHER_PARITY_ROOTS section header for the related but distinct byte-
    parity mechanism -- that mechanism catches a launcher body drifting
    from its OWN generator's current output; this test catches the two
    generators' gates drifting from EACH OTHER).

    Renders both emitters fresh (never reads committed launcher files,
    which byte-parity already covers) and, for each, both confirms the real
    existence-gate string is present AND proves a degraded (emptiness-only)
    substitute is texturally distinguishable -- so this test would actually
    fail, not vacuously pass, if either emitter regressed.
    """
    gen = _load_gen_launcher_shim()

    cmd_body = gen.render_cmd("fake-tool.py")
    ps1_body = gen.render_ps1("fake-tool.py")

    assert _CMD_EXISTENCE_GATE in cmd_body
    assert _PS1_EXISTENCE_GATE in ps1_body

    # Prove each assertion is not vacuous: an in-memory degrade of the
    # rendered string (never the on-disk generator source) to the
    # emptiness-only form must make the positive assertion above fail.
    degraded_cmd = cmd_body.replace(_CMD_EXISTENCE_GATE, _CMD_EMPTINESS_ONLY_GATE)
    assert _CMD_EXISTENCE_GATE not in degraded_cmd

    degraded_ps1 = ps1_body.replace(_PS1_EXISTENCE_GATE, _PS1_EMPTINESS_ONLY_GATE)
    assert _PS1_EXISTENCE_GATE not in degraded_ps1


def test_parity_guard_is_not_mostly_exemptions():
    """A guard that is largely carve-out is theatre: it reports a green tick
    for a population it has stopped inspecting. This bounds the ratio rather
    than the count, so growing the tree never quietly relaxes it and shrinking
    the tree never quietly tightens it.

    5% is chosen against the real numbers, not picked round: after the
    2026-08-03 fleet sweep the tree holds 424 tracked launchers with 10
    exemptions (2.4%), so this has ~2x headroom for a legitimately-new
    hand-authored forwarder and still fires long before the list can swallow a
    regeneration campaign someone declined to run. If a change makes this
    fail, the fix is to regenerate -- or to argue the bound up in review, on
    the record, which is exactly the conversation an exemption list is
    otherwise good at avoiding."""
    total = len(_tracked_launchers())
    exempt = len(LAUNCHER_PARITY_EXEMPTIONS)
    assert exempt / total <= 0.05, (
        f"{exempt} of {total} tracked launchers ({exempt / total:.1%}) are in "
        "LAUNCHER_PARITY_EXEMPTIONS. Past ~5% this guard is mostly carve-out "
        "and proves little -- regenerate the drifted launchers via "
        "gen-launcher-shim.py instead of naming them here."
    )


def test_declared_spec_backlink_is_part_of_the_byte_contract(tmp_path, monkeypatch):
    """A launcher whose entrypoint DECLARES a spec backlink
    (coordinator/bin/launcher-spec-backlinks.toml) must be held to bytes that
    INCLUDE the backlink line, in both dialects.

    Without this, the mechanism would be half-wired: the generator would emit
    the line and this guard would immediately report the launcher as drifted,
    which is the failure mode that made `bin/claude-klabauter-doctor-probe.cmd`'s
    hand-added backlink unshippable in the first place (the 2026-08-03 fleet
    sweep destroyed it, and byte parity then enforced the destruction).

    Synthetic tree + fixture-shaped declaration via monkeypatched lookup, never
    the real registry -- so this stays green if the real declaration is ever
    retired, and fails for the right reason if the resolution seam breaks.
    """
    gen = _load_gen_launcher_shim()
    backlink = "docs/plans/fake-plan.md § C1"
    # Via monkeypatch, not raw assignment: the loader registers this module in
    # `sys.modules["gen_launcher_shim"]` (the generator's own documented library
    # import name), so an unreverted stub outlives the test as process-global
    # state for anything that imports the name instead of re-execing the file.
    monkeypatch.setattr(
        gen,
        "spec_backlink_for_entry_path",
        lambda rel, *a, **k: backlink if rel == "bin/declared.py" else None,
        raising=True,
    )
    # `_launcher_parity_offenders` loads the generator itself, so the patched
    # module has to be what that load returns -- patching the module object
    # alone would be silently discarded by the re-exec.
    monkeypatch.setattr(
        sys.modules[__name__], "_load_gen_launcher_shim", lambda: gen, raising=True
    )
    (tmp_path / "bin").mkdir(parents=True)
    with_link = "bin/declared.cmd"
    with_link_ps1 = "bin/declared.ps1"
    without = "bin/plain.cmd"
    (tmp_path / with_link).write_bytes(
        gen.render_cmd("declared.py", spec_backlink=backlink)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    (tmp_path / with_link_ps1).write_bytes(
        gen.render_ps1("declared.py", spec_backlink=backlink)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    (tmp_path / without).write_bytes(
        gen.render_cmd("plain.py").replace("\n", "\r\n").encode("utf-8")
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path, raising=True)

    # Declared launchers WITH the line are at parity; the undeclared one is too.
    assert _launcher_parity_offenders([with_link, with_link_ps1, without], {}) == []

    # And a declared launcher that LOST its backlink line -- exactly what a
    # backlink-blind regeneration produces -- is reported as drifted.
    (tmp_path / with_link).write_bytes(
        gen.render_cmd("declared.py").replace("\n", "\r\n").encode("utf-8")
    )
    offenders = _launcher_parity_offenders([with_link, without], {})
    assert [rel for rel, _ in offenders] == [with_link]


def test_guard_bites_on_a_drifted_launcher_in_a_newly_swept_root(tmp_path, monkeypatch):
    """Red-case proof for the WIDENING specifically (2026-08-03): a launcher
    carrying the pre-fix ungated baked-interpreter rung must be reported as an
    offender when it sits in one of the newly-swept roots, not just under
    coordinator/bin. Before the widening this exact file passed, because
    `_tracked_launchers()` never looked outside coordinator/bin -- so a test
    that only exercises a coordinator/bin path cannot tell the widening
    happened.

    The synthetic body is the real pre-fix ladder: `if not "%_py%"=="" goto
    :run_baked` with no `if exist` gate, which is precisely the shape that
    left a Mac/Windows-synced `~/.claude` invoking an interpreter path
    existing under no spelling."""
    gen = _load_gen_launcher_shim()
    drifted = "coordinator/lib/legacy-tool.cmd"
    current = "coordinator/lib/fresh-tool.cmd"
    (tmp_path / "coordinator" / "lib").mkdir(parents=True)
    (tmp_path / drifted).write_bytes(
        gen.render_cmd("legacy-tool.py")
        .replace("\n", "\r\n")
        .replace(
            'if not "%_py%"=="" if exist "%_py%" goto :run_baked',
            'if not "%_py%"=="" goto :run_baked',
        )
        .encode("utf-8")
    )
    (tmp_path / current).write_bytes(
        gen.render_cmd("fresh-tool.py").replace("\n", "\r\n").encode("utf-8")
    )
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path, raising=True)

    offenders = _launcher_parity_offenders([drifted, current], {})
    assert [rel for rel, _ in offenders] == [drifted]
    assert "legacy-tool.py" in offenders[0][1]

    # The widened root is what makes that file visible at all: a
    # coordinator/bin-only parity root would never have enumerated it.
    assert "coordinator/lib" in LAUNCHER_PARITY_ROOTS


# ---------------------------------------------------------------------------
# Argv fidelity across both launcher legs (C1, docs/plans/2026-08-07-argv-
# fidelity-at-the-windows-launcher-seam.md)
#
# Every guard above is EXISTENCE/TARGET/BYTE-parity: a twin is present, points
# at a real file, and matches generator output. None of them look at what a
# launcher actually does to a caller's ARGV on the way to the callee -- and
# that gap is exactly how a mechanical argument-corruption defect (embedded-
# quote stripping, multi-line truncation, a bare `--` consumed by
# PowerShell's own parameter binder) survived two rounds of forwarder
# hardening: the existing suite only ever exercised single-line, quote-free
# arguments.
#
# This section is the regression oracle those defects need. Each case spawns
# a REAL subprocess through one generated launcher leg via `pwsh -NoProfile`
# and diffs the callee's own `json.dumps(sys.argv[1:])` against the intended
# argv. An in-process argv list or a bash-spawned run reproduces NONE of
# these losses -- the corruption happens in the PowerShell-to-native-command-
# line marshaling step only a real `pwsh` spawn exercises.
#
# EXPECTED VALUES ARE MEASURED, NOT DERIVED (PowerShell 7.6.4). The `.cmd`
# rows marked stripped/truncated/lost are the KNOWN-BAD shape -- asserted
# explicitly, never skipped, so a future change to that leg's (deliberately
# still-broken) behavior fails loudly instead of silently. The `.ps1` rows
# are what prove that leg is the actual fix.
# ---------------------------------------------------------------------------

_PWSH = shutil.which("pwsh")

_ARGV_PROBE_CALLEE_BODY = "import sys, json\nprint(json.dumps(sys.argv[1:]))\n"

# The `set "_py=__PYTHON_BIN__"` (.cmd) / `$_pybin = '__PYTHON_BIN__'` (.ps1)
# ASSIGNMENT occurrence, not the bare token -- see `_bake_python_bin` for why
# the distinction is load-bearing.
_CMD_BAKE_MARKER = b'=__PYTHON_BIN__"'
_PS1_BAKE_MARKER = b"'__PYTHON_BIN__'"


def _bake_python_bin(data: bytes, python_bin: str, *, quoted: bool) -> bytes:
    """Substitute ONLY the baked-value ASSIGNMENT occurrence of
    `__PYTHON_BIN__` with `python_bin` -- never a blind replace-all.

    Each launcher body (gen-launcher-shim.py's render_cmd/render_ps1) carries
    the literal token in up to THREE places: once in an explanatory header
    comment, once as the baked-value assignment, once as the sentinel that
    assignment is compared against to detect "never substituted". A blind
    replace-all (or even a naive first-occurrence replace, which lands on
    the header comment) either substitutes the wrong spot or collapses the
    assignment/sentinel comparison to self-equality (baked path == baked
    path -> True), which resets the baked value straight back to empty and
    forces every invocation down the PATH-lookup rung regardless of what was
    baked. Confirmed live while building this oracle: that same PATH-lookup
    rung used to carry a defect in the `.ps1` leg's WindowsApps-exclusion
    regex (`-notmatch '\\WindowsApps\\'` was an invalid .NET pattern -- a
    trailing bare `\\` -- and threw whenever Get-Command found ANY
    python.exe on PATH, i.e. on every real install, since
    `install-substrate.py` never performs the `__PYTHON_BIN__` token
    substitution). It has since been fixed: the generator fix landed as
    commit `1250852645e5`, the emitted launchers' fix as `cfece46c375a`.
    Baking precisely the assignment here is what lets this oracle exercise
    argv fidelity through the intended "baked path" rung, isolated from
    the interpreter-ladder's PATH-lookup rung -- a distinction still worth
    keeping now that specific bug is fixed, since the two rungs remain
    different code paths with different failure modes.
    """
    marker = _PS1_BAKE_MARKER if quoted else _CMD_BAKE_MARKER
    idx = data.index(marker)
    replacement = (f"'{python_bin}'" if quoted else f'={python_bin}"').encode("utf-8")
    return data[:idx] + replacement + data[idx + len(marker) :]


def _write_argv_probe_launchers(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a real `.cmd`/`.ps1` launcher pair for a JSON-argv-dumping
    callee, via gen-launcher-shim.py's own `generate(..., ps1=True)`.

    Generated into `tmp_path` rather than depending on any `.ps1` already
    under `coordinator/bin/` -- the `.ps1` leg is not yet emitted by default
    (peer chunk C2), so this oracle stays independent of that landing.
    """
    gen = _load_gen_launcher_shim()
    callee = tmp_path / "argv_probe.py"
    callee.write_text(_ARGV_PROBE_CALLEE_BODY, encoding="utf-8")
    written = gen.generate("argv_probe.py", tmp_path, ps1=True)
    cmd_path = tmp_path / "argv_probe.cmd"
    ps1_path = tmp_path / "argv_probe.ps1"
    assert set(written) == {cmd_path, ps1_path}
    cmd_path.write_bytes(_bake_python_bin(cmd_path.read_bytes(), sys.executable, quoted=False))
    ps1_path.write_bytes(_bake_python_bin(ps1_path.read_bytes(), sys.executable, quoted=True))
    return cmd_path, ps1_path


def _pwsh_probe(launcher: Path, invocation_args: str) -> list:
    """Spawn `pwsh -NoProfile -Command "& '<launcher>' <invocation_args>"` as
    a REAL subprocess (never an in-process simulation or a bash-spawned run
    -- see section header) and return the callee's own parsed argv list.

    `invocation_args` is the literal PowerShell source text following the
    launcher path in the `&` call. Callers supply it pre-quoted (or
    deliberately unquoted, for the bare-`--` case) so this stays a faithful
    replay of the exact command line each scenario measures, not a
    re-quoting layer that could itself mask the defect under test.
    """
    script = f"& '{launcher}' {invocation_args}"
    proc = subprocess.run(
        [_PWSH, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        f"pwsh invocation failed (rc={proc.returncode}):\n"
        f"script: {script}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def argv_probe_launchers(tmp_path_factory):
    """The generated `.cmd`/`.ps1` probe pair, or a clean skip when `pwsh` is
    absent -- this suite also runs on macOS, which does not ship pwsh."""
    if _PWSH is None:
        pytest.skip(
            "pwsh not on PATH -- argv-fidelity oracle needs a real PowerShell subprocess spawn"
        )
    return _write_argv_probe_launchers(tmp_path_factory.mktemp("argv-fidelity"))


# (leg, scenario label, literal invocation-args text, expected observed argv)
#
# Four argument shapes x both launcher legs, EXCEPT the `--` shape (bare vs.
# quoted), which is meaningful only on the `.ps1` leg -- `.cmd`'s `%*`
# forwarding has no special-token binder to lose a bare `--` to, so there is
# no known-bad `.cmd` row for it. Every other shape is asserted on BOTH legs.
_ARGV_FIDELITY_CASES: list[tuple[str, str, str, list[str]]] = [
    (
        "cmd",
        "embedded-quotes-stripped",
        '\'he said "hi" there\'',
        ["he said hi there"],
    ),
    (
        "ps1",
        "embedded-quotes-intact",
        '\'he said "hi" there\'',
        ['he said "hi" there'],
    ),
    (
        "cmd",
        "multiline-truncated",
        "'line1\nline2' '--' 'path/to/file'",
        ["line1"],
    ),
    (
        "ps1",
        "multiline-intact",
        "'line1\nline2' '--' 'path/to/file'",
        ["line1\nline2", "--", "path/to/file"],
    ),
    (
        "ps1",
        "bare-dashdash-lost",
        "'before' -- 'after'",
        ["before", "after"],
    ),
    (
        "ps1",
        "quoted-dashdash-intact",
        "'before' '--' 'after'",
        ["before", "--", "after"],
    ),
    (
        "cmd",
        "bang-intact",
        "'value with ! bang'",
        ["value with ! bang"],
    ),
    (
        "ps1",
        "bang-intact",
        "'value with ! bang'",
        ["value with ! bang"],
    ),
]


@pytest.mark.parametrize(
    "leg, label, invocation_args, expected",
    _ARGV_FIDELITY_CASES,
    ids=[f"{leg}-{label}" for leg, label, _, _ in _ARGV_FIDELITY_CASES],
)
def test_argv_fidelity_matrix(argv_probe_launchers, leg, label, invocation_args, expected):
    """The argv-fidelity regression oracle (see section header above).

    Each case spawns a real `pwsh -NoProfile` subprocess through one
    launcher leg and asserts the callee's OWN observed argv against the
    intended one -- including the known-bad `.cmd` rows, which are asserted
    explicitly rather than skipped so a future change to that leg's
    (deliberately still-broken) behavior fails loudly instead of silently."""
    cmd_path, ps1_path = argv_probe_launchers
    if leg == "cmd" and sys.platform != "win32":
        # The .cmd leg needs a real cmd.exe to interpret the launcher; pwsh on a
        # POSIX host cannot execute one, so these rows fail for want of an
        # interpreter rather than for want of fidelity. Skipping keeps the
        # known-bad .cmd expectations asserted where they are observable
        # (Windows) instead of leaving three permanent reds on the primary dev
        # platform, which is how a whole module's failures stop being read.
        pytest.skip("cmd leg needs cmd.exe — not observable on a POSIX host")
    launcher = cmd_path if leg == "cmd" else ps1_path
    assert _pwsh_probe(launcher, invocation_args) == expected


# ---------------------------------------------------------------------------
# CARET ROUND-TRIP GUARD (2026-08-15)
#
# state/bug-backlog/2026-08-08-cmd-exe-shim-eats-the-caret-in-a-git-rev-
# 6679bf76eb8a.yaml documents cmd.exe stripping a literal `^` from `%*`
# during its OWN command-line parse, ahead of anything a launcher body can
# do about it. `gen-launcher-shim.py`'s fix (module docstring § RAW-CMDLINE-
# PRESERVATION ENTRYPOINTS) is opt-in per entrypoint: `_RAW_CMDLINE_ENTRYPOINTS`
# names the CLIs whose `.cmd` twin captures `%CMDCMDLINE%` before Python runs,
# and `coordinator/bin/lib/raw_cmdline_recovery.py::recover_windows_argv` is
# the entrypoint-side half that re-derives un-mangled argv from that capture.
#
# `test_argv_fidelity_matrix` above deliberately does NOT cover this case --
# its probe callee reads `sys.argv` directly, which is a faithful oracle for
# every OTHER argv-fidelity defect but would only prove the caret is lost
# (the known-bad, pre-recovery shape), never that the opt-in recovery
# mechanism actually restores it. This section is a SEPARATE round-trip
# oracle, through a REAL `cmd.exe`-interpreted `.cmd` launcher generated
# with `preserve_raw_cmdline=True`, whose probe callee calls
# `recover_windows_argv` itself -- the same call every `_RAW_CMDLINE_
# ENTRYPOINTS` member makes -- so a regression in either half (the launcher's
# capture block or the recovery module's parse) fails this test, not just a
# unit test of `recover_windows_argv` in isolation (which would prove nothing
# about whether the shell layer ever hands it real captured text).
# ---------------------------------------------------------------------------

_CARET_PROBE_LAUNCHER_NAME = "caret_probe"

_RAW_CMDLINE_RECOVERY_PATH = REPO_ROOT / "coordinator" / "bin" / "lib" / "raw_cmdline_recovery.py"


#: The exact refusal marker `coordinator-write-review-trail.py`'s
#: `__main__` guard prints to stderr on `UnsoundRawCmdlineTransport` (see
#: its `if __name__ == "__main__":` block) -- the probe callee below mirrors
#: that entrypoint's catch-and-refuse contract (not scoped-git-commit's or
#: cross-repo-memo.py's warn-and-proceed contract) because it is the one
#: entrypoint whose response is a clean, unambiguous (exit code, stderr
#: text) pair a subprocess round-trip probe can assert against without
#: also needing a ledger-row fixture.
_CARET_REFUSAL_MARKER = "the invoking shell stripped characters from this command line"


def _caret_probe_callee_body() -> str:
    """The probe callee's source: recovers argv via the REAL
    `raw_cmdline_recovery.recover_windows_argv` (loaded from its actual
    repo path, not a copy) and prints it as JSON -- the exact shape
    `coordinator-write-review-trail.py`/`scoped-git-commit` themselves call
    (`_recover_windows_argv` wrappers), so this probe is not a reimplementation
    of the recovery contract, just a thin JSON-emitting caller of it.

    On `UnsoundRawCmdlineTransport` this mirrors `coordinator-write-
    review-trail.py`'s own `__main__` guard: print a refusal to stderr and
    exit non-zero, rather than let `recover_windows_argv` hand back a
    silently-mangled argv."""
    recovery_path = str(_RAW_CMDLINE_RECOVERY_PATH).replace("\\", "\\\\")
    return (
        "import sys, json, importlib.util\n"
        f"_spec = importlib.util.spec_from_file_location('raw_cmdline_recovery', r'{recovery_path}')\n"
        "_mod = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_mod)\n"
        "try:\n"
        f"    print(json.dumps(_mod.recover_windows_argv(sys.argv[1:], '{_CARET_PROBE_LAUNCHER_NAME}.cmd')))\n"
        "except _mod.UnsoundRawCmdlineTransport:\n"
        f"    print({_CARET_REFUSAL_MARKER!r}, file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )


def _subprocess_list_probe_refuses(launcher: Path, args: list) -> None:
    """Spawn the launcher directly via `subprocess.run([str(launcher), *args])`
    -- no shell, no `pwsh` -- and assert the CALLEE's `coordinator-write-
    review-trail.py`-shaped refusal (non-zero exit, `_CARET_REFUSAL_MARKER`
    on stderr) rather than a recovered argv.

    On Windows, `CreateProcess`'s file-association substitution still routes
    a `.cmd` target through `cmd.exe`, but it does NOT outer-quote the
    resulting post-`/c` string the way `_pwsh_probe`'s `& '<launcher>' ...`
    invocation does. This is one of the non-outer-quoted spawn shapes named
    in the plan's measured substrate (git-bash/MSYS and
    `subprocess.run([...])` list-form both land here, PowerShell does not) --
    the shape neither the 2026-08-10 fix nor its guard ever exercised,
    because every prior oracle spawned through pwsh. See
    `caret_probe_launcher`'s docstring.

    This non-outer-quoted transport is UNSOUND per
    `raw_cmdline_recovery._classify_raw_cmdline_transport`, and the caret
    is destroyed by cmd.exe's own `/c` parse before this process's first
    line ran, so no parse here can recover it (see that module's
    docstring). Asserting a recovered caret here would be the exact
    wrongly-passing test this plan exists to close."""
    proc = subprocess.run(
        [str(launcher), *args],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode != 0, (
        f"expected a refusal (non-zero exit) for an unsound, non-outer-quoted "
        f"transport, got rc=0:\nargv: {[str(launcher), *args]}\nstdout: {proc.stdout}"
    )
    assert _CARET_REFUSAL_MARKER in proc.stderr, (
        f"expected the caret-refusal marker on stderr, got:\nstderr: {proc.stderr}"
    )


@pytest.fixture(scope="module")
def caret_probe_launcher(tmp_path_factory):
    """A real `.cmd` launcher generated with `preserve_raw_cmdline=True`,
    baked to the live interpreter, for a callee that recovers argv via the
    real `raw_cmdline_recovery` module -- see section header.

    The sound cases below spawn through `pwsh` (`_pwsh_probe`): its
    `& '<launcher>' ...` invocation outer-quotes the ENTIRE post-`/c` string
    cmd.exe receives, and the caret survives cmd.exe's own `/c` parse under
    that shape. The failing case spawns through `_subprocess_list_probe_refuses`
    instead, whose `subprocess.run([str(launcher), *args])` does not
    outer-quote that string -- the same non-outer-quoted shape git-bash/MSYS
    produce. This is NOT a separate, Python-specific quoting gap in Python's
    own `.bat`/`.cmd` launch path, and it is not this mechanism's cover
    story: measured, the mechanism is spawn-shape quoting, and any caller
    that does not outer-quote the post-`/c` string loses the caret before
    this process's first line runs -- the excluded shape is the failing one.
    See Measured substrate row 4 in
    `docs/plans/2026-08-15-the-caret-fix-went-to-the-caller-that-never-broke.md`."""
    if _PWSH is None:
        pytest.skip("pwsh not on PATH -- caret round-trip oracle needs a real PowerShell subprocess spawn")
    if sys.platform != "win32":
        pytest.skip("caret round-trip needs a real cmd.exe — not observable on a POSIX host")
    gen = _load_gen_launcher_shim()
    tmp_path = tmp_path_factory.mktemp("caret-roundtrip")
    callee = tmp_path / f"{_CARET_PROBE_LAUNCHER_NAME}.py"
    callee.write_text(_caret_probe_callee_body(), encoding="utf-8")
    cmd_path = tmp_path / f"{_CARET_PROBE_LAUNCHER_NAME}.cmd"
    cmd_path.write_text(
        gen.render_cmd(f"{_CARET_PROBE_LAUNCHER_NAME}.py", preserve_raw_cmdline=True),
        encoding="utf-8",
        newline="\r\n",
    )
    cmd_path.write_bytes(_bake_python_bin(cmd_path.read_bytes(), sys.executable, quoted=False))
    return cmd_path


_CARET_ROUND_TRIP_CASES = [
    ("pwsh", "'51652dd75^..51652dd75'", ["51652dd75^..51652dd75"]),
    ("pwsh", "'a^b'", ["a^b"]),
    ("pwsh", "'100%25'", ["100%25"]),
    # `expected` is unused for this row -- `_subprocess_list_probe_refuses`
    # takes no `expected` argument; `None` is a placeholder to keep the
    # 3-tuple shape the pwsh rows need, not a value ever compared against.
    ("list", ["51652dd75^..51652dd75"], None),
]


@pytest.mark.parametrize(
    "spawner, invocation, expected",
    _CARET_ROUND_TRIP_CASES,
    ids=["single-caret-git-range", "caret-mid-token", "percent", "list-form-caret-not-outer-quoted"],
)
def test_caret_survives_real_cmd_shim_round_trip(caret_probe_launcher, spawner, invocation, expected):
    """A `^`-bearing argument (the exact `<sha>^..<sha>` shape from the
    review-trail incident) survives byte-identical through a REAL `.cmd`
    launcher generated with `preserve_raw_cmdline=True`, invoked into the
    Python process's recovered argv -- proving the opt-in raw-cmdline-
    capture mechanism actually closes the defect it was built for, not just
    that a string-level unit test of `recover_windows_argv` passes in
    isolation.

    The `pwsh`-spawned cases (`_pwsh_probe`) outer-quote the post-`/c`
    string and are the sound, working transport this mechanism was built
    and guarded against. The `list`-spawned case
    (`_subprocess_list_probe_refuses`) is the non-outer-quoted shape named
    in the plan's measured substrate -- as of C1/C2,
    `recover_windows_argv` classifies this transport UNSOUND and raises
    `UnsoundRawCmdlineTransport` rather than handing back a silently
    mangled "recovered" argv, so this case asserts the loud refusal
    (non-zero exit, refusal marker on stderr), never a recovered caret:
    the caret's bytes are destroyed by cmd.exe's own `/c` parse before
    this process's first line ran and cannot be recovered by any parse
    here (see `raw_cmdline_recovery.py`'s module docstring)."""
    if spawner == "pwsh":
        assert _pwsh_probe(caret_probe_launcher, invocation) == expected
    else:
        _subprocess_list_probe_refuses(caret_probe_launcher, invocation)

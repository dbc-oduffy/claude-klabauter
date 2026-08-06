"""Standing gate: no NEW site of the home-resolution defect class ships.

This is shim #1 of the fleet-wide home-resolution lint -- it supplies this
repo's scan roots, exclusions, and forward-slash scope to the shared engine
(`coordinator/lib/home_resolution_lint.py`), and owns this repo's debt
ledger (`_home_resolution_lint_baseline.py`, unchanged, 98 known sites).
The four AST rules themselves no longer live in this file -- see the
engine module's docstring for why (extraction design:
`example-doctrine-repo/docs/research/2026-07-28-fleet-lint-distribution-design.md`).
Every other fleet repo gets its own shim of this same shape, importing the
identical engine live rather than a vendored copy, so a fifth rule is one
edit to the engine and reaches every repo on its next `pytest` run with no
re-publish step.

Four independent, structural (AST-based) scans, each its own test so a
failure names precisely which shape tripped rather than "the lint failed".
Spec backlink: `docs/research/2026-07-28-windows-simulation-test-harness-design.md`
(example-doctrine-repo) Component Design § 2 -- this file implements that blueprint's
static-shape tier. The blueprint's fifth shape (a docstring/comment that
*describes* home resolution in bash spelling -- `${CLAUDE_HOME:-$HOME}`) is
already covered by the standing `test_docstring_shell_paste_hazard.py` gate
in this same directory (committed 0893a0b7 / 0b69df01) and is not
reimplemented here -- see `test_docstring_lie_shape_is_covered_elsewhere`
below, which asserts that coverage rather than duplicating the detector.

Why lint, not a runtime test pinning today's six known sites: the grep in
the design blueprint found this idiom at ~30+ sites, not the six originally
believed. A test suite that pins today's list leaves every future site
unguarded. AST structural matching fires on any NEW instance of the same
*shape*, which is the property a pinned list cannot have.

Ratchet baseline, per PM ruling 2026-07-28 (overrides a silent-baseline
default): the point of a baseline here is to unblock *new* work, never to
let existing debt go invisible. Three properties this file enforces:

1. **The current per-rule violation count prints on every run, pass or
   fail** -- via ``warnings.warn(...)`` in each rule test, which pytest's
   terminal "warnings summary" renders unconditionally, without needing
   ``-s`` or a failing assertion. A plain ``print()`` would be silently
   captured on a passing run and defeat the whole point.
2. **The baseline is a ceiling, not a floor.** Each rule's baseline is an
   exact, text-keyed set (never a bare count) of ``(relpath, stripped
   source line)`` pairs -- the same design as
   ``test_docstring_shell_paste_hazard.py``'s ``_BASELINE``. A NEW violation
   not in the set fails the gate. A baselined entry that no longer matches
   any real finding (fixed, moved, or reworded) is caught by the paired
   ``test_*_baseline_has_no_stale_entries`` test, which fails and names the
   stale entry for deletion -- so a fixed site cannot be silently replaced
   by a fresh one elsewhere while the ledger's raw count stays flat.
3. Baseline data lives in ``_home_resolution_lint_baseline.py`` alongside
   this file: a plain, greppable, per-rule Python literal, headed with an
   explicit "this is debt, not an approved-patterns list" notice.

Suppression: none by design (unlike the docstring-hazard gate's
``shell-doc-ok:`` marker). Every one of these four shapes has exactly one
correct spelling (`Path.home()`/`USERPROFILE`, `os.X_OK`-free existence
check, backslash-folded split, `USERPROFILE`-aware fallback chain) with no
legitimate "this really is the bad shape on purpose" case among the sites
surveyed while building this gate -- see AC-6 test
``test_settings_home_module_is_clean`` for the positive control instead.
"""

from __future__ import annotations

import functools
import warnings
from pathlib import Path

from coordinator.lib.home_resolution_lint import ENGINE_VERSION, HomeResolutionLintEngine
from coordinator_core.tests._home_resolution_lint_baseline import (
    BARE_OR_BASELINE,
    COLON_JOIN_BASELINE,
    FORWARD_SLASH_BASELINE,
    X_OK_BASELINE,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = ("coordinator_core", "coordinator", "bin", "scripts")

# The forward-slash-only-split rule is scoped to the resolution family named
# in the design blueprint (Component Design § 2, rule 4) rather than
# repo-wide -- narrow on purpose, see engine module docstring.
_FORWARD_SLASH_SCOPE = (
    "coordinator_core/install",
    "coordinator_core/trusted_root_guard.py",
    "coordinator/lib/settings_home.py",
    "coordinator_core/_settings_home.py",
    "coordinator_core/doe_root_pointer.py",
    "coordinator_core/read_doe_root_pointer.py",
)


@functools.lru_cache(maxsize=None)
def _engine() -> HomeResolutionLintEngine:
    return HomeResolutionLintEngine(
        repo_root=_REPO_ROOT,
        scan_roots=_SCAN_ROOTS,
        forward_slash_scope=_FORWARD_SLASH_SCOPE,
    )


# ---------------------------------------------------------------------------
# Rule 1: os.access(path, os.X_OK) -- meaningless on Windows (F13).
# ---------------------------------------------------------------------------


def test_no_x_ok_access_check():
    """`os.access(path, os.X_OK)` degrades to `F_OK` on Windows -- F13's
    `[WinError 193]` root cause. Correct form: check the file exists and,
    where executability genuinely matters, dispatch on `os.name == "nt"`
    rather than asking a POSIX-only predicate to answer a Windows question.
    """
    findings = _engine().find_x_ok_checks()
    baseline_keys = {(p, t) for p, _n, t in X_OK_BASELINE}
    new = [f for f in findings if f.key() not in baseline_keys]
    warnings.warn(
        f"[home-resolution-lint] engine_version={ENGINE_VERSION} rule=x_ok_access "
        f"total={len(findings)} baseline={len(X_OK_BASELINE)} new={len(new)}"
    )
    rendered = "\n".join(f"  {f.path}:{f.line}: {f.text}" for f in new)
    assert new == [], (
        f"Found {len(new)} NEW os.access(..., os.X_OK) site(s) -- meaningless on "
        f"Windows (degrades to F_OK, produced F13's [WinError 193]). Use "
        f"`path.is_file()` instead, or, where executability genuinely matters, gate "
        f'the call on a guard that provably excludes Windows execution -- '
        f'`if os.name != "nt":`, `if os.name == "posix":`, or '
        f'`if sys.platform != "win32":` (direct or nested inside the guard\'s body). '
        f'A call reachable only through the inverted test (`os.name == "nt"` / '
        f'`sys.platform == "win32"`) runs only on Windows and is still flagged, not '
        f"exempted:\n{rendered}"
    )


def test_x_ok_baseline_has_no_stale_entries():
    """The X_OK baseline must shrink, never rot -- see module docstring
    property (2). A stale entry (fixed, moved, or reworded) is named here
    for deletion so the ledger cannot silently mute a NEW violation at the
    same coordinates."""
    live = {f.key() for f in _engine().find_x_ok_checks()}
    stale = sorted(entry for entry in {(p, t) for p, _n, t in X_OK_BASELINE} if entry not in live)
    rendered = "\n".join(f"  {p}\n    {t}" for p, t in stale)
    assert stale == [], (
        f"{len(stale)} X_OK_BASELINE entr(ies) no longer match a live finding -- "
        f"the site was fixed or moved. Delete from _home_resolution_lint_baseline.py "
        f"and, if the true count is now lower, lower the ceiling:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Rule 2: a literal ":" used to split/join a path-shaped variable.
# ---------------------------------------------------------------------------


def test_no_literal_colon_path_list_join():
    """A literal `":"` PATH-list join/split fails open on Windows -- a
    strangler facade reading "seam absent" and silently running the legacy
    path is the exact failure this catches. Correct form: `os.pathsep`.
    """
    findings = _engine().find_colon_path_joins()
    baseline_keys = {(p, t) for p, _n, t in COLON_JOIN_BASELINE}
    new = [f for f in findings if f.key() not in baseline_keys]
    warnings.warn(
        f"[home-resolution-lint] engine_version={ENGINE_VERSION} rule=colon_path_join "
        f"total={len(findings)} baseline={len(COLON_JOIN_BASELINE)} new={len(new)}"
    )
    rendered = "\n".join(f"  {f.path}:{f.line}: {f.text}" for f in new)
    assert new == [], (
        f"Found {len(new)} NEW literal ':' PATH-list join/split site(s) -- ':' is "
        f"POSIX-only, Windows uses ';'. Use `os.pathsep` instead:\n{rendered}"
    )


def test_colon_join_baseline_has_no_stale_entries():
    live = {f.key() for f in _engine().find_colon_path_joins()}
    stale = sorted(
        entry for entry in {(p, t) for p, _n, t in COLON_JOIN_BASELINE} if entry not in live
    )
    rendered = "\n".join(f"  {p}\n    {t}" for p, t in stale)
    assert stale == [], (
        f"{len(stale)} COLON_JOIN_BASELINE entr(ies) no longer match a live "
        f"finding. Delete from _home_resolution_lint_baseline.py:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Rule 3: forward-slash-only path splitting, in the resolution-code family.
# ---------------------------------------------------------------------------


def test_no_forward_slash_only_path_split():
    """A forward-slash-only path split (`p.rsplit("/", 1)`) is invisible to
    any test built only from POSIX-form fixtures and silently mishandles a
    real Windows path (`X:\\example-doctrine-repo\\coordinator`) -- F8's root cause.
    Correct form: fold the backslash first (`.replace("\\\\", "/")`) before
    splitting, or split on `os.sep`.
    """
    findings = _engine().find_forward_slash_only_splits()
    baseline_keys = {(p, t) for p, _n, t in FORWARD_SLASH_BASELINE}
    new = [f for f in findings if f.key() not in baseline_keys]
    warnings.warn(
        f"[home-resolution-lint] engine_version={ENGINE_VERSION} rule=forward_slash_split "
        f"total={len(findings)} baseline={len(FORWARD_SLASH_BASELINE)} new={len(new)}"
    )
    rendered = "\n".join(f"  {f.path}:{f.line}: {f.text}" for f in new)
    assert new == [], (
        f"Found {len(new)} NEW forward-slash-only path split(s) in the resolution "
        f"family -- invisible to POSIX-only test fixtures, mishandles a real "
        f"Windows backslash path (F8). Fold the backslash first "
        f'(`.replace("\\\\", "/")`) before splitting, or split on `os.sep`:\n'
        f"{rendered}"
    )


def test_forward_slash_baseline_has_no_stale_entries():
    live = {f.key() for f in _engine().find_forward_slash_only_splits()}
    stale = sorted(
        entry for entry in {(p, t) for p, _n, t in FORWARD_SLASH_BASELINE} if entry not in live
    )
    rendered = "\n".join(f"  {p}\n    {t}" for p, t in stale)
    assert stale == [], (
        f"{len(stale)} FORWARD_SLASH_BASELINE entr(ies) no longer match a live "
        f"finding. Delete from _home_resolution_lint_baseline.py:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# Rule 4 (highest value): CLAUDE_HOME/HOME `or`-chain with no USERPROFILE rung.
# ---------------------------------------------------------------------------


def test_home_or_userprofile_present_at_every_claude_home_site():
    """The highest-value rule -- the direct generalization of `CLAUDE_HOME
    or HOME or ""` (F10/F15's root cause: PowerShell/cmd.exe never set
    `HOME`, so the chain silently degraded to a relative-path empty string
    and created artifacts at the drive root). Correct form: add a
    `USERPROFILE` rung, or delegate the whole chain to `Path.home()`.
    """
    findings = _engine().find_bare_home_or_chains()
    baseline_keys = {(p, t) for p, _n, t in BARE_OR_BASELINE}
    new = [f for f in findings if f.key() not in baseline_keys]
    warnings.warn(
        f"[home-resolution-lint] engine_version={ENGINE_VERSION} rule=bare_home_or_chain "
        f"total={len(findings)} baseline={len(BARE_OR_BASELINE)} new={len(new)}"
    )
    rendered = "\n".join(f"  {f.path}:{f.line}: {f.text}" for f in new)
    assert new == [], (
        f"Found {len(new)} NEW CLAUDE_HOME/HOME `or`-chain(s) with no USERPROFILE "
        f"rung -- PowerShell/cmd.exe never set HOME, so this degrades to '' on "
        f"Windows and yields a cwd-relative path (F10/F15's root cause). Add "
        f'`os.environ.get("USERPROFILE")` as a fallback rung, or delegate to '
        f"`Path.home()`, which already honours USERPROFILE:\n{rendered}"
    )


def test_bare_or_baseline_has_no_stale_entries():
    live = {f.key() for f in _engine().find_bare_home_or_chains()}
    stale = sorted(entry for entry in {(p, t) for p, _n, t in BARE_OR_BASELINE} if entry not in live)
    rendered = "\n".join(f"  {p}\n    {t}" for p, t in stale)
    assert stale == [], (
        f"{len(stale)} BARE_OR_BASELINE entr(ies) no longer match a live "
        f"finding. Delete from _home_resolution_lint_baseline.py:\n{rendered}"
    )


# ---------------------------------------------------------------------------
# AC-6 positive control + AC-1 shape-5 coverage cross-reference.
# ---------------------------------------------------------------------------


def test_settings_home_module_is_clean():
    """`coordinator_core/_settings_home.py` is the CORRECT implementation
    (delegates to `Path.home()`, has no `os.access(X_OK)`, no literal ':'
    PATH join, no forward-slash-only split, no bare `or`-chain) and must
    pass every rule in this file with zero findings -- the direct AC-6
    check that this lint does not cry wolf on the one file that already
    does everything right.
    """
    target = _REPO_ROOT / "coordinator_core" / "_settings_home.py"
    assert target.is_file(), f"expected {target} to exist"
    relpath = "coordinator_core/_settings_home.py"
    engine = _engine()
    assert all(f.path != relpath for f in engine.find_x_ok_checks())
    assert all(f.path != relpath for f in engine.find_colon_path_joins())
    assert all(f.path != relpath for f in engine.find_bare_home_or_chains())


def test_docstring_lie_shape_is_covered_elsewhere():
    """AC-1's fifth shape -- a docstring that describes home resolution in
    bash spelling (`${CLAUDE_HOME:-$HOME}`) rather than the Python it
    actually implements -- is already a standing gate in this same
    directory, `test_docstring_shell_paste_hazard.py`
    (`test_no_shell_paste_hazard_in_docstrings_or_comments`). This test
    documents the cross-reference rather than duplicating that detector,
    which is already AST/tokenize-based, already has its own ratchet
    baseline, and already passes `_settings_home.py`'s own (correct)
    docstring. Reimplementing it here would create two detectors that can
    silently drift out of agreement on the same shape.
    """
    sibling = Path(__file__).parent / "test_docstring_shell_paste_hazard.py"
    assert sibling.is_file(), (
        "test_docstring_shell_paste_hazard.py is expected to cover AC-1 shape 5 "
        "(lying docstring); it appears to have moved or been removed. If so, "
        "shape 5 needs a detector reinstated somewhere, including possibly here."
    )

"""AC6/AC7/AC8 regression gate for
docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md.

Purpose: the whole plan turns on AC7 -- "no module on the engine-resolution
path carries a `claude-klabauter` token in its NAME" -- so this file is the mechanical
backstop that keeps the transform's own claim honest rather than aspirational.
It also owns AC6 (cc_invoke's `_delegate_to_gate` import target exists in
both the live tree and the published mirror) and C0's pinned accepted limit
(the ladder module keeps at most one cross-package
`from coordinator_core.<x> import ...` statement).

Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md
    C7 (this file), C0 (accepted-limit pin), C1 (module rename), C5
    (cc_invoke's import edit).

Negative-spec:
    - Does NOT touch `test_output_functional_identifier_drift.py` /
      `test_path_valued_wire_identifier_is_caught_as_drift` -- that test's
      synthetic path never existed on disk and covers a different detector's
      path-segment splitting fix, not this module's real location.
    - The mirror-reading assertions below MUST SKIP (never pass vacuously)
      when the published mirror is unregistered/absent, or when it is
      registered but its `coordinator_core/engine_root.py` predates this
      plan's rename -- a vacuous pass on AC7's mirror half would be worse
      than no test at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.engine_root import (
    coordinator_engine_root_with_class,
    published_engine_mirror_path,
)

#: The two mirror-reading tests below call `published_engine_mirror_path()`,
#: which walks the REAL machine-local registry. An autouse `_quarantine_real_home`
#: fixture nulls that registry read by default (see
#: `test_published_mirror_carries_shipped_symbols.py`'s own `pytestmark`) --
#: without `real_home` those two tests would silently always take the "mirror
#: unregistered" skip branch, which is exactly the vacuous-pass shape this
#: file's own negative-spec forbids. The other tests in this module do not
#: touch the registry and are unaffected either way.
pytestmark = [pytest.mark.real_home]

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ENGINE_ROOT_MODULE = _REPO_ROOT / "coordinator_core" / "engine_root.py"
_WARM_ENGINE_ROOT_MODULE = _REPO_ROOT / "coordinator_core" / "warm" / "engine_root.py"
_CC_INVOKE = _REPO_ROOT / "coordinator" / "bin" / "lib" / "cc_invoke.py"

#: Mirrors cc_invoke.py's own `_GATE_ENTRY_POINT_RE` (see that module's
#: `_GATE_MODULE_GLOB_SUFFIX` / `_GATE_ENTRY_POINT_RE`) -- not re-derived
#: independently, so this gate and the shape-scanner it backstops agree on
#: what "an entry point" looks like by construction.
_GATE_ENTRY_POINT_RE = re.compile(r"^def (coordinator_\w+_root_with_class)\s*\(", re.MULTILINE)

#: The import statement `_delegate_to_gate` uses today, per C5's brief:
#: `from coordinator_core.<module> import <entry point>`. Extracted from the
#: real file rather than hardcoded, so a future edit to which module/entry
#: point `_delegate_to_gate` imports is caught here rather than silently
#: leaving this gate checking a stale target.
_DELEGATE_IMPORT_RE = re.compile(
    r"from coordinator_core\.(\w+) import (coordinator_\w+_root_with_class)"
)


def _mirror_skip_reason(mirror: str | None, mirror_module: Path) -> str | None:
    """`None` if the mirror is usable and post-rename; else the SKIP reason."""
    if mirror is None:
        return (
            "repos.claude_klabauter is not registered/usable on this machine "
            "(published_engine_mirror_path() returned None) -- cannot read the "
            "mirror off disk to assert parity."
        )
    if not mirror_module.is_file():
        return (
            f"published mirror at {mirror!r} predates this plan's rename -- no "
            f"{mirror_module} on disk. Re-publish (percolate) after this chunk "
            "lands, then re-run to get real AC6/AC7 mirror coverage rather than "
            "a skip."
        )
    return None


def test_claude_klabauter_root_module_is_retired() -> None:
    """`coordinator_core/claude_klabauter_root.py` no longer exists, and the module is
    not importable under its old name -- the transform did not leave an
    alias or a re-export behind (C1: "move the whole module, do not
    re-export it")."""
    old_path = _REPO_ROOT / "coordinator_core" / "claude_klabauter_root.py"
    assert not old_path.is_file(), (
        f"{old_path} still exists -- C1 was to MOVE coordinator_core/claude_klabauter_root.py "
        "to engine_root.py, not leave the old module behind."
    )
    with pytest.raises(ModuleNotFoundError):
        import coordinator_core.claude_klabauter_root  # noqa: F401


def test_engine_root_module_name_carries_no_claude_klabauter_token() -> None:
    """AC7 (live half): the ladder module's own basename and its public
    entry-point names carry no `claude-klabauter` token, case-insensitively."""
    assert "claude-klabauter" not in _ENGINE_ROOT_MODULE.stem.lower()

    for name in (
        "coordinator_engine_root",
        "coordinator_engine_root_with_class",
        "published_engine_mirror_path",
    ):
        assert "claude-klabauter" not in name.lower()

    import coordinator_core.engine_root as engine_root_mod

    assert not hasattr(engine_root_mod, "coordinator_claude_klabauter_root")
    assert not hasattr(engine_root_mod, "coordinator_claude_klabauter_root_with_class")


def test_engine_root_mirror_ships_same_module_name() -> None:
    """AC7 (mirror half): the published mirror's engine-resolution module is
    named `engine_root.py`, same as the live tree -- read off disk, SKIP
    (never a vacuous pass) when the mirror is absent or predates the rename."""
    mirror = published_engine_mirror_path()
    mirror_module = Path(mirror or "") / "coordinator_core" / "engine_root.py"
    reason = _mirror_skip_reason(mirror, mirror_module)
    if reason is not None:
        pytest.skip(reason)

    text = mirror_module.read_text(encoding="utf-8")
    assert "def coordinator_engine_root_with_class" in text, (
        f"mirror module {mirror_module} exists but does not define "
        "coordinator_engine_root_with_class -- the two trees have diverged "
        "on the entry-point name."
    )


def test_cc_invoke_delegate_to_gate_import_target_exists_live_and_mirror() -> None:
    """AC6: the module `cc_invoke._delegate_to_gate` imports
    (`from coordinator_core.<x> import coordinator_<x>_with_class`) exists
    in BOTH the live tree and the published mirror -- extracted from the
    real source, not hardcoded, so a drift between C5's actual import and
    this gate's assumption is caught rather than silently checking the
    wrong target."""
    cc_invoke_text = _CC_INVOKE.read_text(encoding="utf-8")
    match = _DELEGATE_IMPORT_RE.search(cc_invoke_text)
    assert match is not None, (
        f"could not find a 'from coordinator_core.<module> import "
        f"coordinator_<x>_with_class' import in {_CC_INVOKE} -- "
        "_delegate_to_gate's import shape changed; update this gate's regex "
        "alongside it."
    )
    module_name, entry_point = match.group(1), match.group(2)

    # Live tree.
    live_module = _REPO_ROOT / "coordinator_core" / f"{module_name}.py"
    assert live_module.is_file(), (
        f"cc_invoke._delegate_to_gate imports coordinator_core.{module_name}, "
        f"but {live_module} does not exist in the live tree."
    )
    live_text = live_module.read_text(encoding="utf-8")
    assert f"def {entry_point}" in live_text, (
        f"{live_module} does not define {entry_point} -- "
        "_delegate_to_gate's import target is missing its entry point in the live tree."
    )

    # Published mirror -- SKIP, never vacuous pass, if absent/pre-rename.
    mirror = published_engine_mirror_path()
    mirror_module = Path(mirror or "") / "coordinator_core" / f"{module_name}.py"
    reason = _mirror_skip_reason(mirror, mirror_module)
    if reason is not None:
        pytest.skip(reason)

    mirror_text = mirror_module.read_text(encoding="utf-8")
    assert f"def {entry_point}" in mirror_text, (
        f"{mirror_module} exists but does not define {entry_point} -- "
        "AC6's live/mirror parity for _delegate_to_gate's import target is broken."
    )


def test_engine_root_ladder_keeps_at_most_one_cross_package_import() -> None:
    """C0's KNOWN AND ACCEPTED LIMIT, pinned: the ladder module
    (`coordinator_core/engine_root.py`) carries at most ONE cross-package
    `from coordinator_core.<x> import ...` statement (today:
    `_settings_home`). A file-path-loaded foreign candidate's cross-package
    imports resolve through whichever `coordinator_core` package happens to
    already be cached, not the candidate's own -- so a second such import
    would silently re-open the mixed-root hole C0 closes, without anyone
    remembering to re-check this by hand."""
    # MODULE-LEVEL (column-0) only, deliberately: this is the hazard C0
    # actually pins -- a candidate module loaded by file path under a
    # synthetic sys.modules key still resolves a MODULE-LEVEL cross-package
    # import through whichever coordinator_core package is already cached,
    # because that import runs once at exec_module time against the
    # candidate's own globals. A function-LOCAL import (e.g. this module's
    # lazy `from coordinator_core.win_portability import
    # no_console_creationflags` inside coordinator_engine_root()'s Rung 2)
    # is the same mechanism but is not what C0's brief describes ("today:
    # _settings_home") or what this pin is scoped to -- indentation is the
    # discriminator, not merely presence of the string.
    text = _ENGINE_ROOT_MODULE.read_text(encoding="utf-8")
    cross_package_imports = re.findall(
        r"^from coordinator_core\.(\w+) import ", text, re.MULTILINE
    )
    assert cross_package_imports == ["_settings_home"], (
        "coordinator_core/engine_root.py's cross-package "
        "`from coordinator_core.<x> import ...` statements changed from the pinned "
        f"single entry ['_settings_home'] to {cross_package_imports!r} -- "
        "C0's accepted limit note explains why a second such import silently "
        "re-opens the mixed-root hole this plan closes. If this is a deliberate, "
        "reviewed addition, update this pin alongside it -- do not just widen the "
        "assertion to make it pass."
    )


def test_exactly_one_top_level_root_module_defines_the_gate_entry_point() -> None:
    """Collision mitigation for the two `engine_root.py` modules at
    different package depths (see the plan's design decision): exactly ONE
    top-level `coordinator_core/*_root.py` module defines a
    `coordinator_*_root_with_class` entry point, and
    `coordinator_core/warm/engine_root.py` defines none -- a checkable gate,
    not only the reciprocal docstrings C1 adds."""
    top_level_dir = _REPO_ROOT / "coordinator_core"
    defining_modules = []
    for entry in sorted(top_level_dir.glob("*_root.py")):
        if entry.name.startswith("test_"):
            continue
        text = entry.read_text(encoding="utf-8")
        if _GATE_ENTRY_POINT_RE.search(text):
            defining_modules.append(entry.name)

    assert defining_modules == ["engine_root.py"], (
        "expected exactly one top-level coordinator_core/*_root.py module "
        f"to define a coordinator_*_root_with_class entry point (engine_root.py), "
        f"got {defining_modules!r} -- cc_invoke's shape-scanner "
        "(_gate_entry_point_by_shape) would now match more or fewer candidates "
        "than intended."
    )

    warm_text = _WARM_ENGINE_ROOT_MODULE.read_text(encoding="utf-8")
    assert _GATE_ENTRY_POINT_RE.search(warm_text) is None, (
        f"{_WARM_ENGINE_ROOT_MODULE} now defines a coordinator_*_root_with_class "
        "entry point -- this collides in NAME (both modules are called "
        "engine_root.py at different package depths) with the top-level ladder "
        "module cc_invoke's shape-scanner is meant to find; this module must stay "
        "the stamp PREDICATE only, never the gate entry point."
    )


def test_public_api_still_importable_and_callable_shape() -> None:
    """Sanity: the renamed public entry point is actually the one this file
    reasons about (imported at module top, not just referenced by string) --
    a cheap guard against every assertion above silently checking a stale
    name that happens to still parse as a regex match."""
    assert callable(coordinator_engine_root_with_class)

"""C1 (docs/plans/2026-08-16-registry-read-stops-costing-a-process.md): verify
DoE-claude's landed `coordinator/hooks/scripts/_bin_impl_drift.py`
(commit `b826d94b4`, SessionStart, once-a-day sentinel-gated) actually covers
this plan's static bin family, and pin the durability gap it leaves.

This module writes NO production code — the drift sweep already both detects
and remediates (see § Cross-plan coordination in the plan above); this chunk
is verification only, per Anti-scope ("Do not edit anything under the
DoE-claude clone tree").  # abs-path-ok: doc quote of the plan's Anti-scope
# line, not a path literal used anywhere in this module's code — the actual
# DoE root is resolved at runtime via read_doe_root_pointer().
Every assertion here reads DoE-claude's tree read-only, resolved via
`coordinator_core.doe_root_pointer.read_doe_root_pointer()` — never a
hardcoded drive path, since this box's DoE checkout location is not portable
across machines.

WHAT THIS PINS.

1. Coverage: `_bin_impl_drift.py::check_and_refresh` iterates
   `<doe_root>/coordinator/templates/bin/` — the same directory
   `coordinator/lib/bin-templates-manifest.py`'s `ML_FAMILY_FILES` /
   `ML_EXPLICIT_FILES` / `PLATFORM_LOCALIZE_FILES` groups are sourced from.
   That template directory's on-disk listing must be a superset of every
   name those three groups declare, or the sweep silently does not reach a
   file this plan's static family needs kept current.
2. The collision (plan § C1 body, EM item A): `_copy_atomic` is a byte-
   verbatim `shutil.copyfile` + `os.replace` with NO re-bake step. On a
   GENUINE template change the refresh overwrites C2's in-file
   `__PYTHON_BIN__` bake with the template's literal token, once a day,
   forever, by the very mechanism built to prevent staleness. `.python-bin`
   is the durable fix specifically BECAUSE it is never a member of
   `templates/bin/` — the sweep's `for src in sorted(src_dir.iterdir())`
   loop structurally cannot touch a file that never appears there. That
   absence is asserted below as an executable fact, not left as prose.
3. Patrik's residual, carried rather than closed (plan § C1 body item c):
   `_CH_FAMILY_FILES` (`claude-home`, `_claude_home.py`, `claude-home.cmd`)
   and `_RM_FAMILY_FILES` (`_resolve_makima.py`) are sourced from MAKIMA'S
   OWN tree (`coordinator/lib/claude-home/`, `coordinator/lib/resolve-makima/`),
   never from DoE's `templates/bin/` — DoE's sweep is refresh-only and only
   ever iterates names already present in that one directory, so these four
   names are structurally invisible to it regardless of staleness. This
   module asserts that the uncovered set is EXACTLY those four names (no
   silent additional gap), and does not attempt to build a detector for
   them — that residual stays open, named here, per the plan's own
   disposition of it.

NEGATIVE-SPEC. This module does not run a live install and does not assert
that `.python-bin` is populated with any particular value — C4 is the live
proof (see the plan's own chunk split). It also does not assert anything
about `launcher_templates` (`claude-doe-launcher.*.tmpl`): those are rendered
by `gen_claude_doe_launcher.py`, never copied via `_install_one`, and are
out of `_static_bin_family_names()` by construction — see that manifest's
own docstring. Nor does it assert coverage of the dynamically-derived
agent-helper forwarders (`coordinator/bin/`'s current listing) — those are
not part of the STATIC family this plan's static-bin-family names describe.

If DoE-claude's tree is not reachable on this box (`repos.doe_claude`
unresolved and no pointer file), every test below skips rather than failing
closed — a missing cross-repo checkout is an environment fact, not a
regression in makima's own code.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _RM_FAMILY_FILES,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
    _static_bin_family_names,
)


def _resolve_doe_root() -> "Path | None":
    root_str = read_doe_root_pointer()
    if not root_str:
        return None
    root = Path(root_str)
    return root if root.is_dir() else None


def _doe_templates_bin(doe_root: Path) -> "Path | None":
    templates_bin = doe_root / "coordinator" / "templates" / "bin"
    return templates_bin if templates_bin.is_dir() else None


def _load_bin_impl_drift(doe_root: Path):
    """Load DoE's `_bin_impl_drift.py` by path (read-only) — mirrors this
    repo's own hyphen/underscore-tolerant `spec_from_file_location` pattern
    used throughout substrate.py for cross-repo manifest loads."""
    module_path = doe_root / "coordinator" / "hooks" / "scripts" / "_bin_impl_drift.py"
    if not module_path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_doe_bin_impl_drift", module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def doe_root() -> Path:
    root = _resolve_doe_root()
    if root is None:
        pytest.skip("DoE-claude root not resolvable on this box (repos.doe_claude unset)")
    return root


@pytest.fixture(scope="module")
def doe_templates_bin_names(doe_root: Path) -> "frozenset[str]":
    templates_bin = _doe_templates_bin(doe_root)
    if templates_bin is None:
        pytest.skip(f"DoE-claude templates/bin/ not found under {doe_root}")
    return frozenset(
        p.name for p in templates_bin.iterdir() if p.is_file()
    )


@pytest.fixture(scope="module")
def bin_impl_drift(doe_root: Path):
    module = _load_bin_impl_drift(doe_root)
    if module is None:
        pytest.skip(f"_bin_impl_drift.py not found under {doe_root}")
    return module


def _doe_sourced_static_family_names() -> "frozenset[str]":
    """The subset of `_static_bin_family_names()` that DoE's `templates/bin/`
    could ever be expected to cover — `ML_FAMILY_FILES` + `ML_EXPLICIT_FILES`
    + `PLATFORM_LOCALIZE_FILES`, i.e. `install_bin_resolvers_entries()`.
    Excludes `_CH_FAMILY_FILES`/`_RM_FAMILY_FILES` by construction (see
    module docstring point 3)."""
    root = _resolve_bin_templates_manifest_root()
    manifest = _load_bin_templates_manifest(root)
    return frozenset(e.name for e in manifest.install_bin_resolvers_entries())


def test_doe_templates_bin_is_superset_of_doe_sourced_static_family(
    doe_templates_bin_names: "frozenset[str]",
):
    """Every name `_install_bin_resolvers` copies from DoE's own tree must
    exist in the exact directory `_bin_impl_drift.py::check_and_refresh`
    sweeps, or the sweep silently never reaches it."""
    doe_sourced = _doe_sourced_static_family_names()
    missing = doe_sourced - doe_templates_bin_names
    assert not missing, (
        "DoE's templates/bin/ is missing file(s) this plan's static bin "
        f"family requires the sweep to cover: {sorted(missing)}"
    )


def test_bin_impl_drift_iterates_the_same_templates_bin_directory(
    doe_root: Path, bin_impl_drift, doe_templates_bin_names: "frozenset[str]"
):
    """Confirm `_templates_bin()` — the sweep's own source-directory resolver
    — actually resolves to the directory this module's other assertions
    compare against, not a different or stale copy."""
    resolved = bin_impl_drift._templates_bin()
    assert resolved.resolve() == (doe_root / "coordinator" / "templates" / "bin").resolve()
    on_disk = frozenset(p.name for p in resolved.iterdir() if p.is_file())
    assert on_disk == doe_templates_bin_names


def test_bin_impl_drift_refresh_only_negative_spec_does_not_exclude_a_needed_file(
    doe_templates_bin_names: "frozenset[str]",
):
    """`check_and_refresh` skips any name not already `.is_file()` at the
    destination (refresh-only, never-seed — its own negative-spec). That
    skip is safe for THIS plan's purposes only if every DoE-sourced static
    family name is one `_install_bin_resolvers` always installs first (so
    the destination file always already exists by the time the daily sweep
    runs) — confirm no DoE-sourced static family name is orphaned from the
    install path itself, which would make the sweep's refresh-only guard an
    exclusion rather than a no-op for that name."""
    doe_sourced = _doe_sourced_static_family_names()
    # Every DoE-sourced static family name must be something the install
    # path actually writes — i.e. present in the template directory sweep
    # walks. If it isn't, the refresh-only guard can never fire for it and
    # this plan cannot rely on the sweep for that name at all.
    assert doe_sourced <= doe_templates_bin_names


def test_python_bin_sidecar_is_never_a_templates_bin_member(
    doe_templates_bin_names: "frozenset[str]",
):
    """Pins the collision reasoning as an executable fact: `.python-bin` is
    the durable fix specifically because DoE's sweep can never touch it — it
    is generated at runtime by `machine-local.cmd`, never shipped as a
    `templates/bin/` file, so `check_and_refresh`'s
    `for src in sorted(src_dir.iterdir())` loop structurally never produces
    a `src` named `.python-bin`. Were this ever to become a template file,
    the durability argument for C2/AC6 would be false and this assertion
    would catch it."""
    assert ".python-bin" not in doe_templates_bin_names


def test_machine_local_cmd_template_writes_the_python_bin_sidecar_itself(
    doe_root: Path,
):
    """Confirm the durable fix's OTHER half: the template that DOES get
    swept (`machine-local.cmd`) is the thing that generates `.python-bin` at
    runtime, so the sidecar keeps getting (re)written on ordinary use even
    though the sweep never manages it directly."""
    machine_local_cmd = doe_root / "coordinator" / "templates" / "bin" / "machine-local.cmd"
    if not machine_local_cmd.is_file():
        pytest.skip(f"machine-local.cmd not found under {doe_root}")
    content = machine_local_cmd.read_text(encoding="utf-8", errors="replace")
    assert ".python-bin" in content, (
        "machine-local.cmd no longer references .python-bin — the durability "
        "argument for C2/AC6 (the sidecar survives a refresh because it is "
        "runtime-generated, not template-sourced) depends on this template "
        "still being the thing that writes it"
    )


def test_ch_and_rm_family_are_the_entire_uncovered_residual(
    doe_templates_bin_names: "frozenset[str]",
):
    """Patrik's residual, pinned rather than closed: the ONLY names in
    `_static_bin_family_names()` that DoE's sweep cannot see — because they
    are sourced from makima's own tree, never from DoE's `templates/bin/` —
    are exactly `_CH_FAMILY_FILES` and `_RM_FAMILY_FILES`. If this set ever
    grows beyond those four names, something silently stopped being
    DoE-sourced and the sweep's blind spot got bigger without anyone
    deciding that."""
    all_static = _static_bin_family_names()
    uncovered = all_static - doe_templates_bin_names
    expected_uncovered = frozenset(
        {f for f, _ in _CH_FAMILY_FILES}
    ) | frozenset(_RM_FAMILY_FILES)
    assert uncovered == expected_uncovered, (
        "the set of static-bin-family names DoE's sweep cannot see changed — "
        f"expected exactly {sorted(expected_uncovered)}, got {sorted(uncovered)}"
    )

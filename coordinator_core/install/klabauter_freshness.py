"""coordinator_core.install.klabauter_freshness — pure freshness interface.

No I/O: the decide table, the changed-path -> installer map, the stale-copy
comparison and the surfaced-key record shape. `klabauter_freshness_apply`
(probe/fetch/ff/install) and the roster reader build on these names.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, NamedTuple, Optional

ACTION_CURRENT = "current"
ACTION_FAST_FORWARD = "fast_forward"
ACTION_DEFERRED_LIVE_RUN = "deferred_live_run"
ACTION_SURFACE_DIRTY = "surface_dirty"
ACTION_SURFACE_DIVERGED = "surface_diverged"

ACTIONS = (
    ACTION_CURRENT,
    ACTION_FAST_FORWARD,
    ACTION_DEFERRED_LIVE_RUN,
    ACTION_SURFACE_DIRTY,
    ACTION_SURFACE_DIVERGED,
)

#: Surfaced states: the verdicts that name a tree the engine will not touch.
STATE_DIRTY = "dirty"
STATE_DIVERGED = "diverged"

INSTALLER_UNIMATRIX = "unimatrix_install_scripts"
INSTALLER_BIN_SHIMS = "bin_impl_drift_check_and_refresh"
INSTALLER_CURRENCY_CACHE = "skew_write_currency_cache"

#: Mirrors `coordinator_core.warm.skew._ENGINE_TOUCHING_PATHS`
#: (pinned equal by the test; skew is not imported to keep this module cheap).
ENGINE_TOUCHING_PATHS = ("coordinator_core/", "coordinator/")

#: Doctrine-plane bin template sources, as `bin_impl_drift._templates_bin` roots them.
BIN_TEMPLATE_PREFIX = "templates/bin/"

_UNIMATRIX_DIR = "coordinator/bin"
_UNIMATRIX_PREFIX = "unimatrix-"

FRESHNESS_RECORD_RELPATH = ("coordinator", "klabauter-freshness.json")


class Verdict(NamedTuple):
    """`action` is one of `ACTIONS`; `reason` is a short human-readable line."""

    action: str
    reason: str


def decide(
    local_sha: str,
    origin_sha: str,
    ahead: int,
    behind: int,
    dirty: bool,
    live_runs: int,
) -> Verdict:
    """Ref verdict for a checkout against origin. Pure.

    A tree not behind origin is `current` even when dirty or ahead (nothing to
    update, nothing to surface). Behind-or-diverged trees: dirty surfaces
    first, then diverged, then a live engine run defers, else fast-forward.
    `live_runs` > 0 means defer; callers pass a positive count for an
    unreadable run registry.
    """
    if local_sha == origin_sha:
        return Verdict(ACTION_CURRENT, "checkout at origin tip")
    if behind <= 0:
        return Verdict(ACTION_CURRENT, "checkout not behind origin")
    if dirty:
        return Verdict(ACTION_SURFACE_DIRTY, f"behind origin by {behind} with uncommitted edits")
    if ahead > 0:
        return Verdict(ACTION_SURFACE_DIVERGED, f"diverged: ahead {ahead}, behind {behind}")
    if live_runs > 0:
        return Verdict(ACTION_DEFERRED_LIVE_RUN, f"{live_runs} live engine run(s)")
    return Verdict(ACTION_FAST_FORWARD, f"behind origin by {behind}, clean")


def stale_installs(
    source_digests: Mapping[str, str],
    installed_digests: Mapping[str, Optional[str]],
) -> tuple[str, ...]:
    """Sorted names whose installed digest is absent or differs from the source.

    Independent of `decide`: a current checkout can still carry a stale copy.
    Names only in `installed_digests` are not judged (no source to compare).
    """
    return tuple(
        sorted(
            name
            for name, digest in source_digests.items()
            if installed_digests.get(name) != digest
        )
    )


def installers_for(changed_paths: Iterable[str]) -> tuple[str, ...]:
    """Installer names to re-run for repo-relative (posix) changed paths, stable order."""
    unimatrix = bin_shims = currency = False
    for raw in changed_paths:
        path = str(raw).replace("\\", "/")
        pp = PurePosixPath(path)
        if (
            str(pp.parent) == _UNIMATRIX_DIR
            and pp.name.startswith(_UNIMATRIX_PREFIX)
            and pp.suffix == ".py"
        ):
            unimatrix = True
        if path.startswith(BIN_TEMPLATE_PREFIX):
            bin_shims = True
        if path.startswith(ENGINE_TOUCHING_PATHS):
            currency = True
    out = []
    if unimatrix:
        out.append(INSTALLER_UNIMATRIX)
    if bin_shims:
        out.append(INSTALLER_BIN_SHIMS)
    if currency:
        out.append(INSTALLER_CURRENCY_CACHE)
    return tuple(out)


class SurfacedKey(NamedTuple):
    """Identity of one surfaced condition; an equal key is never surfaced twice."""

    hostname: str
    local_sha: str
    origin_sha: str
    state: str


def freshness_record_path(settings_home: Path) -> Path:
    """`<settings home>/coordinator/klabauter-freshness.json`."""
    return Path(settings_home).joinpath(*FRESHNESS_RECORD_RELPATH)


def key_to_record(key: SurfacedKey) -> dict:
    """The JSON object persisted at `freshness_record_path`."""
    return dict(key._asdict())


def key_from_record(record: object) -> Optional[SurfacedKey]:
    """Parse a persisted record; `None` for anything that is not a full string-keyed record."""
    if not isinstance(record, Mapping):
        return None
    try:
        values = [record[f] for f in SurfacedKey._fields]
    except KeyError:
        return None
    if not all(isinstance(v, str) for v in values):
        return None
    return SurfacedKey(*values)


def is_new_surface(recorded: object, key: SurfacedKey) -> bool:
    """True when `key` differs from the persisted record (surface it once)."""
    return key_from_record(recorded) != key


def surfaced_state_for(action: str) -> Optional[str]:
    """`STATE_*` for a surface verdict action, else `None`."""
    return {
        ACTION_SURFACE_DIRTY: STATE_DIRTY,
        ACTION_SURFACE_DIVERGED: STATE_DIVERGED,
    }.get(action)

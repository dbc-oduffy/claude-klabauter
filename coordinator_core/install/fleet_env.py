"""
coordinator_core.install.fleet_env — idempotent, health-probed provisioner
for the fleet shared Python environment (C4).

Purpose: creates/refreshes the environment at the root C1's registry key
(`fleet_env.root`) resolves, falling back through C5's ladder
(`fleet_env_resolve.resolve_fleet_env_fallback_root`) when the key is absent
or unusable, and installs EXACTLY C3's committed lock
(`docs/install/fleet-env.lock`) — never re-resolves it. A second run against
a healthy environment is a no-op (AC4); an environment missing any
contractually-guaranteed import (`_FLEET_ENV_IMPORT_PROBES` below) is
rebuilt, never silently accepted.

Mirrors `coordinator_core.install.ensure_venv`'s proven shape rather than
inventing a second one — that module is READ-ONLY reference here (out of
this chunk's write scope), so the patterns below are re-derived in this
module's own functions, not imported:

  - Idempotent fast path: a health probe under the environment's own
    interpreter (never in-process — a different interpreter's site-packages
    is not importable from this process) gates every mutation.
  - Build-lock: `coordinator_core.locked_write`'s existing dual-backend
    advisory-lock primitive (`_plat_try_lock`/`_plat_unlock`) on a `.lock`
    sidecar file next to the environment root — the same shared primitive
    `ensure_venv` uses, not a second lock mechanism. Fail-loud on contention
    (`FleetEnvContention`), no polling. The publish step (below) runs while
    this lock is held, so two rebuilders can never interleave a publish.
  - Junction-publish rebuild (C2/C3): `env_root` is a JUNCTION (nt) /
    directory symlink (posix) — see `coordinator_core.install.junction` —
    never itself a real directory. The environments it points at are sibling
    GENERATION directories, `<env_root name>.gen-<pid>-<hex>`, that are
    NEVER renamed while published. A rebuild populates a fresh generation
    sibling, health-probes it, then publishes by retargeting the junction:
    `junction.remove_junction(env_root)` followed by
    `junction.create_junction(env_root, new_generation)`. The vacated old
    generation is then reclaimed with a plain `shutil.rmtree` (immediately on
    POSIX; best-effort with deferred reclaim via `_sweep_orphaned_swap_dirs`
    on Windows, where a reader's still-open handle can make the delete fail —
    that guard was always correct, since it never touched the published
    name). This matters MORE here than for the settings-home coordinator
    venv: `ensure_venv`'s own module docstring records the equivalent
    rename-swap pattern replacing an in-place `rmtree` (DoE `4591a557`)
    because readers hold no lock, and this environment has the WHOLE FLEET
    as such readers on a 50-70-session box.

    NEGATIVE SPEC — history, so the fix is not rediscovered as a live
    defect. Measured on a real Windows host 2026-08-15 (see
    `state/bug-backlog/2026-08-15-windows-venv-swap-fails-winerror-5-when-a3c85da8f0bf.yaml`)
    and again 2026-08-20 against this module directly: `os.rename` of a
    directory raises WinError 5 when ANY plain-open file handle exists
    anywhere inside it, even several path segments down — so a rename-swap
    publish (this module's ORIGINAL shape, before `099e51046224`/this
    junction rewrite) failed outright while a fleet reader was mid-import.
    Bounded retry could not have closed it: the handle is held for the whole
    call, so nothing about it was transient. `os.replace` retargeting a live
    junction fails the SAME WinError 5 (measured 2026-08-20 — see
    `docs/plans/2026-08-20-the-fleet-env-publishes-through-a-juncti.md`
    "What was measured"), so atomic retarget is not on the table either; the
    only primitive that works is `remove_junction` + `create_junction`,
    which is what `_swap_in_new_env` below does. That makes publication
    NON-ATOMIC: a measured ~1.2ms (median of 20 samples, range 0.75–1.44ms,
    this host, 2026-08-20) window in which `env_root` names nothing at all.
    A reader that resolves `env_root` inside that window gets
    `FileNotFoundError`, not a coherent old-or-new read. This is an accepted,
    documented trade (see the plan's "the cost, stated plainly"): today's
    alternative is a rebuild that fails ~100% of the time under fleet load
    and never lands at all. `_fleet_env_healthy` / `ensure_fleet_env` treat
    "env_root absent but a healthy generation sibling exists" as a TORN
    PUBLISH (a crash inside that window, or the restore-on-failure guard
    itself losing its race) and repair it with one `create_junction` call
    rather than a full rebuild — try-acquiring the same build lock
    (non-blocking) first, so it never fights a concurrent legitimate
    publisher for the name (slice-A review finding 1; see
    `_swap_in_new_env`'s docstring for the corruption sequence a lock-free
    repair used to allow).

Installs from the committed lock via `uv sync --frozen --no-install-project`
against a reconstructed copy of C3's synthetic project
(`fleet_env_lock.render_lock_pyproject` over the same requirements-input and
overrides-file `fleet_env_lock` already exposes public loaders for) paired
with the checked-in `docs/install/fleet-env.lock` renamed to `uv.lock` in an
ephemeral project directory — `--frozen` makes uv install the lock verbatim
and refuse to re-resolve it, which is what "installing exactly C3's lock"
requires. `UV_PROJECT_ENVIRONMENT` points uv's sync target at the build
sibling directly; this module never re-implements dependency resolution
(`fleet_env_lock` already owns that, and only at lock-generation time, not
here).

Health contract / probe set: `_FLEET_ENV_IMPORT_PROBES` below is a
deliberately small, representative subset of the fleet union's DIRECT
(first-class) requests — not its full transitive closure, and not every
package in the lock — chosen to cover the union's genuinely distinct
consumption shapes (lightweight cross-repo utility, GPU-heavy ML stack, and
the PM-ruled `huggingface_hub` floor) without making every provisioning run
pay for probing all ~250 packages. This is also documented in
`docs/reference/fleet-shared-environment-contract.md` § Provisioning the
environment (C4) — that is the promise this constant discharges; the two
must not drift apart independently. `_fleet_env_healthy` also gates on the
target interpreter's own `sys.version_info` matching `LOCK_PYTHON_MINOR` —
added as a follow-up to C6 (which flipped the minor 3.12 -> 3.14 and
regenerated the lock without any propagation path to an already-provisioned
box): import success alone cannot detect a stale minor, since an old
environment imports its own contracted modules just fine.

Binding registry (C6): a sibling repo binds through exactly one call,
`register_sibling_binding(repo, sibling, sibling_root)`, which (1) persists a
`{repo, sibling, path}` entry into a versioned JSON registry
(`_binding_registry_path()`, under this machine's settings-home, machine-
local like `registry.local.toml` — the paths it names are absolute and
machine-specific, never portable across machines) and (2) immediately writes
the corresponding namespaced `.pth` file
(`<repo>_<sibling>_sibling.pth`, convention adopted from
`example-market-data-repo/scripts/setup.py::path_wire_example_retrieval_repo` — see
`docs/reference/fleet-shared-environment-contract.md`) into the environment's
site-packages if the environment already exists. `deregister_sibling_binding`
is the inverse: removes the registry entry and deletes the `.pth` file
immediately if present (AC6b).

Rebuild durability: `_replay_sibling_bindings` is called UNCONDITIONALLY
into the build tree BEFORE `_swap_in_new_env` publishes it (never on the
healthy fast path, where nothing was destroyed), so the swap's rename stays
the single atomic publish point and a reader spawning right after the swap
never sees a bindings-less window. It always consults the registry itself
(`_replay_registered_bindings`) — replay does not depend on any other module
having imported this one first or having set a global. `BINDING_REPLAY_HOOK`
remains as a module-level `Optional[Callable[[Path], None]]`, `None` by
default, purely as an ADDITIONAL extension point called after the registry
replay; it is never the only path by which registered bindings get replayed.
A dangling registration (an absolute path that no longer exists — a moved or
deleted sibling) is detected during replay and reported to stderr, never left
silently broken (AC6b); `check_sibling_bindings` exposes the same detection
for an external doctor/probe pass to call directly.

Concurrency: the registry file and the `.pth` writes it drives are protected
by `coordinator_core.locked_write.held_lock` — the same locking primitive
`ensure_venv`/this module's own build-lock reuse, not a second file-locking
mechanism. The registry lock and the site-packages lock are two distinct,
never-nested lock targets (registry mutation completes and releases before
any site-packages write begins) to respect `held_lock`'s non-reentrancy
contract. `_replay_registered_bindings` reads the registry via
`_load_binding_registry` WITHOUT holding the registry lock — this is safe,
not an omission: `_atomic_write_registry_unlocked`'s mkstemp+`os.replace`
makes every write atomic, so a concurrent `register_sibling_binding`/
`deregister_sibling_binding` on another process can only make this read see
the pre- or post-mutation registry cleanly, never a torn one. Review:
coordinatorcode-reviewer-97d5c433 finding 8.

Negative-spec:
  - Does NOT read the `fleet_env.root` registry key itself — delegates to
    `coordinator/bin/fleet-env.py::resolve_fleet_env_root` (C1), loaded via
    `importlib.util.spec_from_file_location` (the sanctioned pattern for
    loading a `coordinator/bin` script as a module in-process — see
    `coordinator_core/install/substrate.py`'s own use of the same primitive)
    since that script is not part of this package's import tree.
  - Does NOT implement the absent/unwritable-key fallback ladder itself —
    delegates to `coordinator_core.install.fleet_env_resolve` (C5).
  - Does NOT generate or re-resolve the lock — `uv sync --frozen` against
    the checked-in `docs/install/fleet-env.lock` only; a resolution mismatch
    is a `FleetEnvError`, not silently patched here (that is C3's surface).
  - Does NOT implement sibling `.pth` binding via a hook a caller must
    remember to set — the registry and the `.pth`-writer live in this module
    and are always consulted by `_replay_sibling_bindings`; `BINDING_REPLAY_HOOK`
    is an additional extension point only, never the sole replay path.
  - Does NOT create the environment as a side effect of import — every
    disk-touching function requires an explicit call with an explicit root
    (or the injectable `settings_home_factory`/`env_root` override); nothing
    here runs at module import time.

One-time cutover (C4 of the junction-publication plan): `_swap_in_new_env`
above REFUSES when `env_root` is a real pre-junction directory (see its
`elif env_root.exists()` branch) — it will not silently rename a real
directory out from under a fleet reader. `_cutover_to_junction_layout`
performs that one-time move, exactly once per environment root, under the
same build lock `ensure_fleet_env` uses so the two can never interleave.
`coordinator/bin/fleet-env-cutover.py` is the operator-facing script that
calls it against the resolved live root; see that script and
`_cutover_to_junction_layout`'s own docstring for the bootstrapping problem
and the measured retry-budget rationale.

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md § C4
Spec backlink: docs/reference/fleet-shared-environment-contract.md
    § Provisioning the environment (C4)
Spec backlink: docs/plans/2026-08-20-the-fleet-env-publishes-through-a-juncti.md § C4
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from coordinator_core._settings_home import settings_home as _default_settings_home
from coordinator_core.install import junction
from coordinator_core.locked_write import _plat_try_lock, _plat_unlock, held_lock
from coordinator_core.win_portability import is_executable, no_console_creationflags
from coordinator_core.install.fleet_env_lock import (
    LOCK_PYTHON_MINOR,
    load_override_dependency_specs,
    load_requirements_in_specs,
    render_lock_pyproject,
)
from coordinator_core.install.fleet_env_resolve import (
    FleetEnvResolutionError,
    resolve_fleet_env_fallback_root,
)
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCK_PATH = _REPO_ROOT / "docs" / "install" / "fleet-env.lock"
_C1_RESOLVER_PATH = _REPO_ROOT / "coordinator" / "bin" / "fleet-env.py"

#: See module docstring "Health contract / probe set" — kept in sync with
#: the same-named section of the contract doc by hand (both name this
#: constant's value, not merely its existence).
_FLEET_ENV_IMPORT_PROBES = (
    "yaml",
    "pydantic",
    "psutil",
    "numpy",
    "torch",
    "transformers",
    "chromadb",
    "huggingface_hub",
)

#: Generous timeout: `uv sync --frozen` installing ~250 packages incl. a
#: multi-GB cu130 torch build, with a possibly-cold uv cache, on a
#: 50-70-session machine is a slow op, not a hung one (CLAUDE.md § Load
#: norm) — this is a cold-path provisioning call, never a request-path one.
_UV_SYNC_TIMEOUT_SECS = 3600

#: Bounded: a health probe imports at most `_FLEET_ENV_IMPORT_PROBES`, whose
#: heaviest member (`torch`) can take several seconds cold but never
#: approaches this ceiling under normal disk/CPU conditions.
_HEALTH_PROBE_TIMEOUT_SECS = 120

#: Additional extension point ONLY — see module docstring "Binding registry
#: (C6)". `_replay_sibling_bindings` always consults the registry itself
#: first (`_replay_registered_bindings`), unconditionally and regardless of
#: import order; this hook, if set, runs after that as a second, optional
#: replay path. `None` means "no extra hook registered" — it does NOT mean
#: "no bindings registered": bindings live in the registry file, not here.
BINDING_REPLAY_HOOK: "Optional[Callable[[Path], None]]" = None

#: Binding registry — see module docstring "Binding registry (C6)". Machine-
#: local like `registry.local.toml` (a sibling repo's absolute path is
#: specific to THIS machine, never portable), so it lives under settings-
#: home, not committed inside this repo.
_BINDING_REGISTRY_FILENAME = "fleet-env-bindings.json"
_BINDING_REGISTRY_SCHEMA_VERSION = 1

#: `<repo>_<sibling>_sibling.pth` — namespacing convention adopted verbatim
#: from `example-market-data-repo/scripts/setup.py::path_wire_example_retrieval_repo`
#: (writes `example_market_data_repo_example_retrieval_repo_sibling.pth`), settled prior art
#: cited in `docs/reference/fleet-shared-environment-contract.md`. Under
#: this convention N repos writing into one shared site-packages tree
#: cannot collide on filename.
def _pth_basename(repo: str, sibling: str) -> str:
    return f"{repo}_{sibling}_sibling.pth"


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="fleet-env",
    source_module="coordinator_core.install.fleet_env",
    clauses=(
        # clauses[0] -- the environment tree itself. `env_root` is a
        # JUNCTION (nt) / directory symlink (posix); `ensure_fleet_env`
        # populates a fresh `.gen-<pid>-<hex>` sibling generation via
        # `_provision_uv_environment` (`uv sync --frozen` under
        # `UV_PROJECT_ENVIRONMENT=build_dir`), then `_swap_in_new_env`
        # retargets the junction at it via `coordinator_core.install.junction`
        # (never an in-place mutation of the live tree, and never a rename of
        # a real directory that a fleet reader may hold open). SHAPED:
        # `env_root` is resolved at runtime via C1+C5
        # (`resolve_environment_root`), never a literal constant here.
        ShapedClause(
            discovered_by="ensure_fleet_env (env_root via resolve_environment_root)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<fleet-env-root>/",
                reason=(
                    "generation sibling populated by `uv sync --frozen` then "
                    "published by retargeting the env_root junction "
                    "(_swap_in_new_env); the vacated old generation is "
                    "reclaimed by shutil.rmtree (immediately on POSIX, "
                    "deferred via _sweep_orphaned_swap_dirs on Windows)"
                ),
            ),
        ),
        # clauses[1] -- `.gen-*` orphaned generation siblings of env_root
        # (built-but-never-published, or vacated-but-not-yet-reclaimed),
        # `shutil.rmtree`'d by `_sweep_orphaned_swap_dirs` before every
        # rebuild attempt -- EXCEPT the generation the junction currently
        # points at, which is excluded by construction. SHAPED for the same
        # reason as clauses[0]: rooted under the runtime-resolved env_root's
        # own parent.
        ShapedClause(
            discovered_by="_sweep_orphaned_swap_dirs (env_root.parent)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<fleet-env-root>.gen-*/",
                effect="delete",
                reason=(
                    "best-effort reclaim of a prior process's abandoned or "
                    "vacated generation sibling directories, run under the "
                    "build lock before starting a fresh rebuild; the "
                    "currently-published generation (junction.junction_target"
                    "(env_root)) is always excluded"
                ),
            ),
        ),
        # clauses[2] -- the build-lock sidecar, `<env_root>.lock`, O_CREAT'd
        # by `ensure_fleet_env` and never unlinked (the advisory flock
        # releases on close, but the file persists) -- same durable-sidecar
        # shape as `ensure_venv.WRITE_SURFACE`'s clauses[5].
        ShapedClause(
            discovered_by="ensure_fleet_env (build lock sidecar)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<fleet-env-root>.lock",
                reason=(
                    "build-lock sidecar, O_CREAT'd by ensure_fleet_env and "
                    "never unlinked; the flock releases on close but the "
                    "file remains"
                ),
            ),
        ),
        # clauses[3] -- sibling `.pth` files written into the resolved
        # environment's site-packages by `_write_pth_unlocked`, called from
        # `register_sibling_binding` (immediate bind) and
        # `_replay_registered_bindings` (unconditional post-rebuild replay).
        # SHAPED: both the env root and the `<repo>_<sibling>_sibling.pth`
        # basename are runtime-derived from caller/registry inputs.
        ShapedClause(
            discovered_by="_write_pth_unlocked (register_sibling_binding / _replay_registered_bindings)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<fleet-env-root>/site-packages/<repo>_<sibling>_sibling.pth",
                reason=(
                    "one .pth file per registered sibling binding, pointing "
                    "at the caller-supplied absolute sibling_root"
                ),
            ),
        ),
        # clauses[4] -- the inverse of clauses[3]: `_delete_pth_unlocked`,
        # called from `deregister_sibling_binding`, immediately deletes the
        # corresponding `.pth` file if the environment already exists.
        ShapedClause(
            discovered_by="_delete_pth_unlocked (deregister_sibling_binding)",
            entry_template=WriteSurfaceEntry(
                kind="file-path",
                path="<fleet-env-root>/site-packages/<repo>_<sibling>_sibling.pth",
                effect="delete",
                reason="deregister_sibling_binding: removes a sibling's .pth file immediately, not deferred to the next rebuild",
            ),
        ),
        # clauses[5] -- the binding registry itself,
        # `<settings-home>/machine-local/fleet-env-bindings.json`, written by
        # `_atomic_write_registry_unlocked` (mkstemp+os.replace) from
        # `register_sibling_binding`/`deregister_sibling_binding`. STATIC:
        # unlike clauses[3]/[4], the registry's own path is a fixed basename
        # under settings-home -- only the settings-home root varies by
        # machine, the same "<settings-home>/..." template shape as
        # `ensure_venv.WRITE_SURFACE`'s clauses[0]/[5].
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<settings-home>/machine-local/fleet-env-bindings.json",
                    reason=(
                        "binding registry: a versioned JSON list of "
                        "{repo, sibling, path} entries, atomically "
                        "mkstemp+os.replace-written by "
                        "_atomic_write_registry_unlocked; entries are added "
                        "by register_sibling_binding and removed by "
                        "deregister_sibling_binding"
                    ),
                ),
            ),
        ),
    ),
)


class FleetEnvError(RuntimeError):
    """Any condition that must stop provisioning rather than silently
    accept an unhealthy or partially-built environment."""


class FleetEnvContention(FleetEnvError):
    """Raised when the build lock is already held by another process —
    fail-loud, immediate, no polling (same contention contract as
    `ensure_venv.EnsureVenvContention`)."""


def _load_c1_resolver() -> "Callable[[], Optional[str]]":
    """Load `coordinator/bin/fleet-env.py::resolve_fleet_env_root` via
    `importlib.util.spec_from_file_location` — the sanctioned in-process
    load pattern for a `coordinator/bin` script that is not part of this
    package's import tree (precedent: `coordinator_core/install/substrate.py`'s
    own manifest-module loads)."""
    if not _C1_RESOLVER_PATH.is_file():
        raise FleetEnvError(
            f"fleet_env: C1 resolver not found on disk: {_C1_RESOLVER_PATH}"
        )
    spec = importlib.util.spec_from_file_location(
        "_fleet_env_c1_resolver", _C1_RESOLVER_PATH
    )
    if spec is None or spec.loader is None:
        raise FleetEnvError(
            f"fleet_env: could not load C1 resolver module spec from {_C1_RESOLVER_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve_fleet_env_root


def resolve_environment_root(
    *, settings_home_factory: "Callable[[], Path]" = _default_settings_home
) -> Path:
    """C1 (registry read) + C5 (fallback ladder), composed the way every
    consumer of the fleet environment's location must: read the key, hand
    its value (or `None`) to the ladder, and let the ladder's own
    `FleetEnvResolutionError` surface as this module's `FleetEnvError` with
    the ladder's actionable remediation text preserved verbatim."""
    resolve_fleet_env_root = _load_c1_resolver()
    try:
        primary_candidate = resolve_fleet_env_root()
    except Exception as exc:
        # C1's resolver is out of this module's write scope, so an
        # unexpected failure there (e.g. an OS-level error reading the
        # registry key) must still surface as FleetEnvError, never a raw
        # exception — callers (including scripts/setup.py) catch only
        # FleetEnvError per the install-never-fails-outside-it contract.
        # Review: coordinatorcode-reviewer-97d5c433 finding 4.
        raise FleetEnvError(
            f"fleet_env: C1 resolver ({_C1_RESOLVER_PATH}) raised "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        return resolve_fleet_env_fallback_root(
            primary_candidate, settings_home_factory=settings_home_factory
        )
    except FleetEnvResolutionError as exc:
        raise FleetEnvError(str(exc)) from exc


def _is_windows_shell() -> bool:
    return (
        os.environ.get("OSTYPE") in ("msys", "cygwin")
        or os.environ.get("OS") == "Windows_NT"
    )


def _env_python_path(env_dir: Path) -> Path:
    """Cross-platform interpreter path under a `uv`-managed environment:
    `Scripts/python.exe` on Windows, `bin/python` on POSIX — the same
    layout `python -m venv` and `uv venv`/`uv sync` both use."""
    if _is_windows_shell():
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _fleet_env_healthy(python_bin: Path) -> bool:
    """Healthy iff `python_bin` is executable AND its interpreter's own
    `sys.version_info` minor matches `LOCK_PYTHON_MINOR` AND every module
    named in `_FLEET_ENV_IMPORT_PROBES` imports successfully under it.

    The minor check is executed under the TARGET interpreter (`sys.version_info`
    inside the same subprocess as the import probe below, not a second spawn)
    rather than inferred from the environment's `lib/python3.X/` directory
    name: a directory-name read is itself an unverified declaration — exactly
    the "it runs" oracle this check exists to replace — while asking the
    interpreter what it actually is cannot drift from the truth. A prior
    version of this environment stayed reported "healthy" after C6 flipped
    `LOCK_PYTHON_MINOR` from 3.12 to 3.14 and regenerated the lock, because
    only executability and imports were checked and a 3.12 environment
    imports its own contracted modules just fine — the flip never propagated
    to any existing box. Everything below still executes under the TARGET
    interpreter, never in-process — a different interpreter's site-packages
    is not importable from this one (same isolation-boundary rationale
    `ensure_venv._venv_healthy` documents and cites)."""
    if not is_executable(python_bin):
        return False
    minor_check = (
        "import sys; "
        f"_want = {LOCK_PYTHON_MINOR!r}; "
        "_got = '%d.%d' % (sys.version_info.major, sys.version_info.minor); "
        "assert _got == _want, "
        "'python minor mismatch: found ' + _got + ', contract requires ' + _want"
    )
    probe = minor_check + "; " + "; ".join(f"import {mod}" for mod in _FLEET_ENV_IMPORT_PROBES)
    try:
        proc = subprocess.run(
            [str(python_bin), "-c", probe],
            capture_output=True,
            timeout=_HEALTH_PROBE_TIMEOUT_SECS,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        # Routine on a fresh/rebuilding environment (exec missing yet, or a
        # cold-cache probe genuinely taking longer than the ceiling) — False
        # here just means "not healthy yet", which the caller rebuilds.
        return False
    return proc.returncode == 0


def _build_dir_for(env_root: Path) -> Path:
    """A fresh GENERATION sibling a rebuild populates BEFORE ever touching
    the published name — under the junction-publish layout (C2) this
    directory is not a transient build scratch space: once it passes the
    health probe it becomes (and stays, until superseded) the tree
    `env_root` points at, so it is named `.gen-<pid>-<hex>` from the start
    rather than renamed at publish time. `_sweep_orphaned_swap_dirs` matches
    this same prefix to reclaim any generation a crashed process never
    published."""
    return env_root.parent / f"{env_root.name}.gen-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _sweep_orphaned_swap_dirs(env_root: Path, *, assume_build_lock_held: bool) -> None:
    """Best-effort reclaim of `.gen-*` generation siblings abandoned by a
    prior process that crashed mid-rebuild or mid-publish — EXCEPT the
    generation `env_root` currently points at, resolved via
    `junction.junction_target`, which must never be swept out from under a
    live publish. Safe unconditionally here: the caller holds the build lock
    before calling this, so nothing else is concurrently populating its own
    `.gen-*` sibling of THIS environment right now. Never raises — a sweep
    failure must not block the rebuild it is merely tidying up after. Same
    reasoning as `uninstall_legs._sweep_orphaned_swap_dirs` (relocated there
    from `ensure_venv` — docs/plans/2026-08-18-retire-coordinator-venv.md
    chunk C3): every OTHER `.gen-*` match already either failed the health
    probe that triggered its own rebuild, or was already vacated by a prior
    publish, so there is no "still-good" tree here to lose.

    Slice-A review finding 3 (tradeoff, pinned rather than restructured):
    if `env_root` is ABSENT when this runs, `junction.junction_target`
    resolves `None` and every `.gen-*` sibling is swept, healthy or not —
    correct only because the sole existing call site always holds the build
    lock first, which rules out `env_root` being mid-repair by anything
    else. `assume_build_lock_held` is a REQUIRED keyword, not a default, so
    that invariant is stated at every call site rather than left implicit —
    a future caller that reaches this function without actually holding the
    lock must fail loudly here, not silently sweep a healthy generation out
    from under a concurrent repair or rebuild."""
    if not assume_build_lock_held:
        raise AssertionError(
            "fleet_env: _sweep_orphaned_swap_dirs must only be called while "
            "the build lock is held — pass assume_build_lock_held=True from "
            "a call site that actually holds it"
        )
    parent = env_root.parent
    gen_prefix = f"{env_root.name}.gen-"
    current_target = junction.junction_target(env_root)
    current_resolved = current_target.resolve() if current_target is not None else None
    try:
        children = list(parent.iterdir())
    except OSError:
        return
    for child in children:
        if not child.name.startswith(gen_prefix):
            continue
        if current_resolved is not None and child.resolve() == current_resolved:
            continue
        shutil.rmtree(child, ignore_errors=True)


def _swap_in_new_env(env_root: Path, build_dir: Path) -> None:
    """Publish `build_dir` as `env_root` by retargeting the `env_root`
    junction (nt) / symlink (posix) at it — never a rename or in-place
    mutation of any real directory. This is the mechanism that actually
    delivers the guarantee the old rename-swap only claimed to on Windows
    (see module docstring § NEGATIVE SPEC for the measured `os.rename`/
    `os.replace` failures this replaces): a reader that already resolved
    `env_root` before this call keeps reading the OLD generation through its
    already-open handles (unaffected by the link retarget), and a reader
    that resolves `env_root` after this call gets the NEW generation. Only
    the two-syscall window strictly between `remove_junction` and
    `create_junction` is unreadable at all (`FileNotFoundError`) — see the
    NEGATIVE SPEC for why that residual cannot be closed and how C3 makes it
    self-repairing.

    AC5 — the restore guard. If `create_junction` raises AFTER
    `remove_junction` already succeeded, `env_root` would be left absent and
    the whole fleet hard-broken until the next rebuild notices. This
    re-points `env_root` at the PREVIOUS generation and re-raises — it never
    swallows the failure. First-ever publish (no prior junction, no prior
    generation to fall back to) is the one case with nothing to restore.

    Concurrent-publisher race (slice-A review finding 1): the module
    docstring's C3 self-repair runs lock-free, before this function's caller
    ever takes the build lock. A second process can observe the `remove_junction`
    window here, find a healthy `.gen-*` sibling, and plant its own junction
    at `env_root` before this function's `create_junction` call runs — so
    "already exists" here does not always mean "restore is needed": if the
    name now already points at OUR `build_dir`, the repair (or a second
    legitimate publisher racing us) already finished the job we were doing,
    and there is nothing to restore or fail. Anything else genuinely needs
    the restore attempt. And if the restore attempt ITSELF fails, the two
    causes are combined into one `FleetEnvError` rather than letting the
    second exception silently supersede the first (slice-A review finding
    1's WARN) — `ensure_fleet_env`'s wrapper only stringifies the exception
    it catches, so an implicitly-chained `__context__` would otherwise be
    invisible to an operator reading the failure.

    The vacated old generation is then reclaimed with a plain
    `shutil.rmtree` (never `rmtree(env_root)` — the link itself is retargeted
    in place, not removed and recreated as a directory). Reclaim failure is
    guarded exactly as it always was: a reader's still-open handle can make
    the delete raise a sharing violation on Windows, so failure there is
    best-effort and left for `_sweep_orphaned_swap_dirs` on the next
    rebuild."""
    had_prior_publish = junction.is_junction(env_root)
    if had_prior_publish:
        previous_target = junction.junction_target(env_root)
        junction.remove_junction(env_root)
    elif env_root.exists():
        raise FleetEnvError(
            f"fleet_env: {env_root} exists but is neither absent nor a "
            "junction — it looks like the pre-junction real-directory "
            "layout, which must go through the C4 cutover step before "
            "_swap_in_new_env can publish to it"
        )
    else:
        previous_target = None

    try:
        junction.create_junction(env_root, build_dir)
    except Exception as create_exc:
        current_target = junction.junction_target(env_root)
        if current_target is not None and current_target.resolve() == build_dir.resolve():
            # Benign race, not a failure: something else (C3's lock-free
            # self-repair, or a second legitimate publisher) already
            # retargeted env_root at OUR build_dir between remove_junction
            # and this create_junction call. env_root is already correctly
            # published — restoring would undo a publish that already
            # succeeded.
            return
        if had_prior_publish and previous_target is not None:
            try:
                junction.create_junction(env_root, previous_target)
            except Exception as restore_exc:
                raise FleetEnvError(
                    f"fleet_env: could not publish {build_dir} to {env_root} "
                    f"({type(create_exc).__name__}: {create_exc}), AND the "
                    f"restore attempt back to the previous generation "
                    f"({previous_target}) also failed "
                    f"({type(restore_exc).__name__}: {restore_exc}) — env_root "
                    f"may be left absent or in an inconsistent state; check "
                    f"{env_root} manually before the next rebuild"
                ) from restore_exc
        raise

    if had_prior_publish and previous_target is not None and previous_target.exists():
        try:
            shutil.rmtree(previous_target)
        except OSError as exc:
            print(
                f"[fleet-env] WARNING: could not immediately reclaim the prior "
                f"environment generation ({type(exc).__name__}: {exc}) — a reader "
                f"likely still has it open (expected on Windows). Left at "
                f"{previous_target} for reclamation on the next rebuild.",
                file=sys.stderr,
            )


def _provision_uv_environment(build_dir: Path, *, uv_executable: str = "uv") -> None:
    """Populate `build_dir` with a `uv`-managed environment installing
    EXACTLY `docs/install/fleet-env.lock` — never re-resolved.

    Reconstructs C3's synthetic project (`fleet_env_lock.render_lock_pyproject`
    over the checked-in requirements-input and overrides file — the same
    public loaders C3's own `generate_lock` uses, so this can never drift
    from what the committed lock was generated against) in an EPHEMERAL
    temp project directory, pairs it with a copy of the checked-in lock
    (renamed `uv.lock`, uv's own required basename), and runs
    `uv sync --frozen --no-install-project` with `UV_PROJECT_ENVIRONMENT`
    pointed at `build_dir` — `--frozen` makes uv install the lock verbatim
    and fail rather than silently re-resolving it if the reconstructed
    project drifted from the lock; `--no-install-project` skips trying to
    install the synthetic `fleet-env` package itself (it has no source,
    C3's project exists only to give `uv lock`/`uv sync` a project
    interface to resolve/install against).

    Spawns `uv` directly via an argv list (`shell=False`,
    `no_console_creationflags()` on Windows) — the same ordinary-CLI-tool
    class as `git`/other `uv` spawns already in this repo (e.g.
    `fleet_env_lock.generate_lock`, `prereq_probe.py::probe_uv`); no
    shell-out-carve-outs entry required (that doc governs shell-INTERPRETER
    spawns only).
    """
    if not _LOCK_PATH.is_file():
        raise FleetEnvError(
            f"fleet_env: committed lock not found: {_LOCK_PATH} — run "
            "`python3 -m coordinator_core.install.fleet_env_lock --emit-lock` "
            "first (C3)."
        )
    specs = load_requirements_in_specs()
    override_specs = load_override_dependency_specs()
    pyproject_text = render_lock_pyproject(specs, override_specs)
    lock_text = _LOCK_PATH.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="fleet-env-sync-") as tmp_dir:
        project_dir = Path(tmp_dir)
        (project_dir / "pyproject.toml").write_text(pyproject_text, encoding="utf-8", newline="\n")
        (project_dir / "uv.lock").write_text(lock_text, encoding="utf-8", newline="\n")

        argv = [
            uv_executable,
            "sync",
            "--frozen",
            "--no-install-project",
            "--project",
            str(project_dir),
            "--python",
            LOCK_PYTHON_MINOR,
        ]
        env = dict(os.environ)
        env["UV_PROJECT_ENVIRONMENT"] = str(build_dir)
        try:
            result = subprocess.run(
                argv,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=_UV_SYNC_TIMEOUT_SECS,
                env=env,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FleetEnvError(
                f"fleet_env: could not run `{uv_executable} sync`: {exc}"
            ) from exc

        if result.returncode != 0:
            raise FleetEnvError(
                "fleet_env: `uv sync --frozen` failed to install "
                f"{_LOCK_PATH} verbatim (exit {result.returncode}). This means "
                "either the environment cannot resolve the committed lock on "
                "this machine, or the reconstructed project no longer matches "
                "the lock — regenerating the lock (C3) is not this module's "
                "call to make. stderr:\n" + (result.stderr or "").strip()
            )


def _binding_registry_path(
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
) -> Path:
    """Machine-local registry file location — see module docstring "Binding
    registry (C6)"."""
    return settings_home_factory() / "machine-local" / _BINDING_REGISTRY_FILENAME


def _site_packages_dir(env_root: Path) -> Path:
    """Conventional `site-packages` layout for a `uv`-managed venv at
    `LOCK_PYTHON_MINOR`: `Lib/site-packages` on Windows,
    `lib/python<minor>/site-packages` on POSIX — the same layout
    `uv venv`/`uv sync` produce, so no interpreter spawn is needed just to
    locate it (a different interpreter's `sysconfig` is not importable from
    this process anyway — same isolation-boundary rationale as
    `_fleet_env_healthy`)."""
    if _is_windows_shell():
        return env_root / "Lib" / "site-packages"
    return env_root / "lib" / f"python{LOCK_PYTHON_MINOR}" / "site-packages"


def _parse_binding_registry(text: str) -> "list[dict]":
    """Parse the registry JSON text into its `bindings` list. Empty/missing
    text is a fresh, empty registry — never an error (mirrors
    `locked_rmw(..., missing_ok=True)`'s "absent means empty" contract)."""
    if not text.strip():
        return []
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("bindings"), list):
        raise FleetEnvError(
            f"fleet_env: binding registry is malformed (expected "
            f"{{'schema_version': int, 'bindings': [...]}}) — got {text[:200]!r}"
        )
    return list(data["bindings"])


def _render_binding_registry(bindings: "list[dict]") -> str:
    ordered = sorted(bindings, key=lambda b: (b["repo"], b["sibling"]))
    return json.dumps(
        {"schema_version": _BINDING_REGISTRY_SCHEMA_VERSION, "bindings": ordered},
        indent=2,
    ) + "\n"


def _load_binding_registry(registry_path: Path) -> "list[dict]":
    if not registry_path.is_file():
        return []
    return _parse_binding_registry(registry_path.read_text(encoding="utf-8"))


def _atomic_write_registry_unlocked(registry_path: Path, bindings: "list[dict]") -> None:
    """Atomic mkstemp+replace write of the registry. Caller MUST already
    hold `held_lock(registry_path)` — no locking of its own, same
    no-own-locking contract as `_write_pth_unlocked`."""
    new_text = _render_binding_registry(bindings)
    old_text = registry_path.read_text(encoding="utf-8") if registry_path.is_file() else ""
    if new_text == old_text:
        return
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(registry_path.parent))
    try:
        os.write(tmp_fd, new_text.encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_path, str(registry_path))
        tmp_path = None
    finally:
        if tmp_fd != -1:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _write_pth_unlocked(site_packages: Path, repo: str, sibling: str, path_str: str) -> None:
    """Write one `.pth` file atomically (mkstemp+`os.replace`, mirroring
    `_atomic_write_registry_unlocked` above). Caller MUST already hold the
    `held_lock` on `site_packages` — this has no locking of its own (see
    `register_sibling_binding`/`_replay_registered_bindings`, the only two
    callers, each of which acquires that lock exactly once around
    potentially many of these calls).

    Plain `Path.write_text` is not safe here even under `held_lock`: CPython's
    `site` module reads `.pth` files at interpreter startup and does not
    consult this module's advisory lock, so a reader interpreter spawning
    during a truncate-then-write window could observe a partial/empty file.
    `os.replace` is a single directory-entry swap on both POSIX and Windows,
    so a concurrent reader always sees either the old or the new complete
    content, never a torn write. Review: coordinatorcode-reviewer-97d5c433 finding 2."""
    site_packages.mkdir(parents=True, exist_ok=True)
    dest = site_packages / _pth_basename(repo, sibling)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(site_packages))
    try:
        os.write(tmp_fd, (path_str + "\n").encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = -1
        os.replace(tmp_path, str(dest))
        tmp_path = None
    finally:
        if tmp_fd != -1:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _delete_pth_unlocked(site_packages: Path, repo: str, sibling: str) -> None:
    """Inverse of `_write_pth_unlocked` — same no-own-locking contract."""
    try:
        (site_packages / _pth_basename(repo, sibling)).unlink()
    except FileNotFoundError:
        pass


def register_sibling_binding(
    repo: str,
    sibling: str,
    sibling_root: "str | Path",
    *,
    registry_path: "Optional[Path]" = None,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
    env_root_factory: "Optional[Callable[[], Path]]" = None,
) -> None:
    """THE one documented call a sibling repo makes to bind into the fleet
    environment (AC6). Persists a `{repo, sibling, path}` entry into the
    binding registry (survives a rebuild — see module docstring "Binding
    registry (C6)") and, if the environment already exists, immediately
    writes the corresponding `.pth` file too, so a caller does not have to
    wait for the next rebuild to take effect.

    `sibling_root` MUST be an absolute path — a relative literal resolves
    against site-packages, not the caller's repo root, and silently fails to
    import (example-market-data-repo's own documented failure mode; see contract
    doc). Raises `FleetEnvError` on a relative path rather than writing a
    binding that would silently misbehave.
    """
    sibling_root_str = str(sibling_root)
    if not os.path.isabs(sibling_root_str):
        raise FleetEnvError(
            "fleet_env: register_sibling_binding requires an absolute path "
            "(a relative literal resolves against site-packages, not the "
            f"caller's repo root, and silently fails to import) — got {sibling_root_str!r}"
        )
    if registry_path is None:
        registry_path = _binding_registry_path(settings_home_factory)

    with held_lock(registry_path):
        bindings = _load_binding_registry(registry_path)
        bindings = [b for b in bindings if not (b["repo"] == repo and b["sibling"] == sibling)]
        bindings.append({"repo": repo, "sibling": sibling, "path": sibling_root_str})
        _atomic_write_registry_unlocked(registry_path, bindings)

    if env_root_factory is None:
        env_root_factory = lambda: resolve_environment_root(
            settings_home_factory=settings_home_factory
        )
    try:
        env_root = env_root_factory()
    except FleetEnvError:
        return  # not yet resolvable; the registry entry survives for the next provisioning pass
    site_packages = _site_packages_dir(env_root)
    if site_packages.is_dir():
        with held_lock(site_packages):
            _write_pth_unlocked(site_packages, repo, sibling, sibling_root_str)


def deregister_sibling_binding(
    repo: str,
    sibling: str,
    *,
    registry_path: "Optional[Path]" = None,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
    env_root_factory: "Optional[Callable[[], Path]]" = None,
) -> None:
    """Inverse of `register_sibling_binding` (AC6b): removes the registry
    entry and deletes the corresponding `.pth` file immediately if the
    environment exists (never left for "the next provisioning pass" to
    catch when it can be done now)."""
    if registry_path is None:
        registry_path = _binding_registry_path(settings_home_factory)

    with held_lock(registry_path):
        bindings = _load_binding_registry(registry_path)
        bindings = [b for b in bindings if not (b["repo"] == repo and b["sibling"] == sibling)]
        _atomic_write_registry_unlocked(registry_path, bindings)

    if env_root_factory is None:
        env_root_factory = lambda: resolve_environment_root(
            settings_home_factory=settings_home_factory
        )
    try:
        env_root = env_root_factory()
    except FleetEnvError:
        return
    site_packages = _site_packages_dir(env_root)
    if site_packages.is_dir():
        with held_lock(site_packages):
            _delete_pth_unlocked(site_packages, repo, sibling)


def check_sibling_bindings(
    env_root: Path,
    *,
    registry_path: "Optional[Path]" = None,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
) -> "list[dict]":
    """Every registered binding with something wrong, reported rather than
    left silently broken (AC6b): a `reason` of `"stale_path"` (the
    registered absolute path no longer exists — a moved or deleted sibling)
    or `"missing_pth"` (registered, path still exists, but `env_root`'s
    site-packages carries no `.pth` for it — e.g. registered while the
    environment was absent and never since replayed). Callable directly by
    an external doctor/probe pass; `_replay_registered_bindings` uses the
    same stale-path detection internally during rebuild replay. Read-only —
    never mutates the registry or any `.pth` file."""
    if registry_path is None:
        registry_path = _binding_registry_path(settings_home_factory)
    bindings = _load_binding_registry(registry_path)
    site_packages = _site_packages_dir(env_root)
    flagged = []
    for binding in bindings:
        if not Path(binding["path"]).exists():
            flagged.append({**binding, "reason": "stale_path"})
        elif not (site_packages / _pth_basename(binding["repo"], binding["sibling"])).is_file():
            flagged.append({**binding, "reason": "missing_pth"})
    return flagged


def _replay_registered_bindings(env_root: Path, *, registry_path: Path) -> "list[dict]":
    """Rewrite every registered binding's `.pth` file into `env_root`'s
    site-packages — the declarative-registry side of AC4's "a rebuild
    reproduces every registered binding" clause. Returns the dangling
    (stale-path) subset for the caller to report. A no-op write pass when
    the registry is empty (no `site_packages.mkdir` side effect in that
    case, so a fleet environment with zero registered siblings never grows
    an unexplained directory)."""
    bindings = _load_binding_registry(registry_path)
    if not bindings:
        return []
    site_packages = _site_packages_dir(env_root)
    with held_lock(site_packages):
        for binding in bindings:
            _write_pth_unlocked(
                site_packages, binding["repo"], binding["sibling"], binding["path"]
            )
    return [b for b in bindings if not Path(b["path"]).exists()]


def _replay_sibling_bindings(
    env_root: Path,
    *,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
) -> None:
    """Always consults the binding registry directly — see module docstring
    "Binding registry (C6)". This does NOT depend on any other module
    having been imported first or having set a global: the registry lives
    in this module, and this function is called unconditionally during
    every rebuild (never on the healthy fast path, where the tree was never
    destroyed and any prior bindings are still present). `BINDING_REPLAY_HOOK`,
    if set, runs afterward as an additional extension point only.

    `settings_home_factory` MUST be threaded from the caller (`ensure_fleet_env`)
    rather than defaulted here — every sibling function in this module
    (`resolve_environment_root`, `register_sibling_binding`,
    `deregister_sibling_binding`) forwards an injected factory so a caller
    that isolated itself via a `tmp_path`-backed factory stays isolated on
    this path too; a bare `_binding_registry_path()` call would silently
    fall through to the real machine settings-home. Review: coordinatorcode-reviewer-97d5c433 finding 1."""
    registry_path = _binding_registry_path(settings_home_factory)
    dangling = _replay_registered_bindings(env_root, registry_path=registry_path)
    for binding in dangling:
        print(
            "[fleet-env] WARNING: sibling binding "
            f"{binding['repo']} -> {binding['sibling']} points at a path that no "
            f"longer exists ({binding['path']}) — deregister it or fix the "
            "registered path.",
            file=sys.stderr,
        )
    if BINDING_REPLAY_HOOK is not None:
        BINDING_REPLAY_HOOK(env_root)


def _env_root_absent(env_root: Path) -> bool:
    """True iff the `env_root` NAME itself carries no directory entry at
    all — not a junction, not a real directory, nothing. This is the exact
    condition `_swap_in_new_env`'s two-syscall window (and, transiently, a
    process that crashed inside it) produces; a junction whose TARGET has
    since gone missing is a different, unrelated problem and must not be
    confused with this one, so this checks presence of the entry itself
    (`is_junction` or `is_dir`), never `Path.exists()` (which follows the
    reparse point and would read False for a dangling-target junction too)."""
    return not junction.is_junction(env_root) and not env_root.is_dir()


def _find_torn_publish_generation(env_root: Path) -> "Optional[Path]":
    """C3's torn-publish detector: scan `env_root`'s siblings for a
    `.gen-<pid>-<hex>` directory that is itself healthy, for
    `ensure_fleet_env` to repair `env_root` onto with one `create_junction`
    call rather than a full rebuild. Only called when `_env_root_absent`
    is already true, so there is no currently-published generation to
    accidentally prefer over — every candidate here is equally "the last
    one built". Returns the first healthy candidate found, or `None` if
    none is (the caller then falls through to a normal rebuild, which is
    always correct — repair is an optimization, never a requirement for
    correctness)."""
    parent = env_root.parent
    gen_prefix = f"{env_root.name}.gen-"
    try:
        children = list(parent.iterdir())
    except OSError:
        return None
    for child in children:
        if not (child.name.startswith(gen_prefix) and child.is_dir()):
            continue
        if _fleet_env_healthy(_env_python_path(child)):
            return child
    return None


def ensure_fleet_env(
    *,
    check_only: bool = False,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
    uv_executable: str = "uv",
) -> str:
    """Idempotently ensure the fleet shared Python environment exists and
    is healthy at the resolved root (C1 + C5), installing exactly C3's
    committed lock.

    Returns one of `"ready"` (already healthy, or a torn publish was
    repaired onto an already-healthy generation sibling — no `uv sync`
    either way), `"rebuilt"` (was absent or unhealthy with no repairable
    generation, freshly built and published), or `"would-rebuild"`
    (`check_only=True` and the environment is currently absent or unhealthy
    — dry-run, no mutation, including no torn-publish repair: repair is
    itself a disk mutation and `check_only`'s contract is none at all).

    Raises `FleetEnvError` (or its `FleetEnvContention` subclass on lock
    contention) on any failure — this function never exits the process
    itself; callers decide their own disposition, same discipline as
    `ensure_venv.ensure_coordinator_venv`.
    """
    env_root = resolve_environment_root(settings_home_factory=settings_home_factory)
    python_bin = _env_python_path(env_root)

    if check_only:
        return "ready" if _fleet_env_healthy(python_bin) else "would-rebuild"

    # Fast path: already healthy — no mutation, no lock taken (AC4).
    if _fleet_env_healthy(python_bin):
        return "ready"

    lock_path = Path(str(env_root) + ".lock")

    # C3 torn-publish self-repair: env_root absent (see _env_root_absent)
    # but a healthy generation sibling still sits on disk — one
    # create_junction call fixes it, never a uv sync. Slice-A review
    # finding 1: this used to run fully lock-free, which let it race a
    # different process's in-flight `_swap_in_new_env` (see that function's
    # docstring for the corruption sequence this closes). It now
    # TRY-ACQUIRES the SAME build-lock sidecar the rebuild path below uses
    # (non-blocking, `_plat_try_lock` — not a second lock mechanism): on
    # contention a legitimate publisher already owns the name, so there is
    # nothing here to repair, and this falls through to the normal health
    # re-check / lock-acquired rebuild path unconditionally. This keeps the
    # repair at syscall scale — never a rebuild — while closing the race
    # against a concurrent publisher.
    if _env_root_absent(env_root):
        candidate = _find_torn_publish_generation(env_root)
        if candidate is not None:
            try:
                repair_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            except OSError:
                repair_fd = None
            if repair_fd is not None:
                try:
                    try:
                        repair_acquired = _plat_try_lock(repair_fd)
                    except OSError:
                        repair_acquired = False
                    if repair_acquired:
                        try:
                            try:
                                junction.create_junction(env_root, candidate)
                            except OSError:
                                pass
                            else:
                                if _fleet_env_healthy(python_bin):
                                    return "ready"
                        finally:
                            try:
                                _plat_unlock(repair_fd)
                            except OSError:
                                pass
                finally:
                    os.close(repair_fd)

    # Both the parent mkdir and the lock-sidecar open are plain filesystem
    # calls made before any guarded region — on a read-only install
    # location, a full disk, or a permission-denied path they raise a bare
    # OSError, which scripts/setup.py's `except FleetEnvError` (the
    # advisory-not-fatal contract for this step) does not catch. Wrap both
    # so this function honours its own docstring: "Raises FleetEnvError...
    # on any failure". Review: coordinatorE-reviewer finding 4 (fix-now,
    # relayed to review-integrator mid-pass).
    try:
        env_root.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise FleetEnvError(
            f"fleet_env: could not prepare the environment root or its lock "
            f"sidecar ({lock_path}): {type(exc).__name__}: {exc}. Check disk "
            "space and write permission on that location, or repoint it: "
            "machine-local set fleet_env.root <writable-path>."
        ) from exc
    acquired = False
    try:
        try:
            acquired = _plat_try_lock(fd)
        except OSError as exc:
            raise FleetEnvError(
                f"fleet_env: could not acquire the build lock at {lock_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not acquired:
            raise FleetEnvContention(
                "[fleet-env] another session is rebuilding the fleet environment; "
                "retry in a moment"
            )

        # Re-check health after acquiring the lock — another session may
        # have finished building while we waited for it.
        if _fleet_env_healthy(python_bin):
            return "ready"

        _sweep_orphaned_swap_dirs(env_root, assume_build_lock_held=True)
        build_dir = _build_dir_for(env_root)
        build_python_bin = _env_python_path(build_dir)

        try:
            _provision_uv_environment(build_dir, uv_executable=uv_executable)
            if not _fleet_env_healthy(build_python_bin):
                raise FleetEnvError(
                    "[fleet-env] ERROR: freshly-built environment failed the "
                    "health probe (import check) before swap-in; discarding it."
                )
        except FleetEnvError:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise

        # Replay registered bindings into the BUILD tree before the swap
        # (never after), so `_swap_in_new_env`'s junction retarget stays the
        # single publish point: a reader spawning right after the retarget
        # sees an environment that already carries every registered binding,
        # rather than a window where the swap has landed but bindings have
        # not replayed yet. Review: coordinatorcode-reviewer-97d5c433 finding 3.
        try:
            _replay_sibling_bindings(build_dir, settings_home_factory=settings_home_factory)
            _swap_in_new_env(env_root, build_dir)
        except OSError as exc:
            # Same leak class as the pre-lock mkdir/open above:
            # `junction.remove_junction`/`create_junction` (the publish) and
            # the `.pth` mkstemp/replace (the replay) are plain filesystem
            # calls that can raise OSError on a read-only or
            # permission-denied target — must not escape as a raw
            # exception past this function's own FleetEnvError contract.
            raise FleetEnvError(
                f"fleet_env: could not publish the rebuilt environment at "
                f"{env_root}: {type(exc).__name__}: {exc}"
            ) from exc
        return "rebuilt"
    finally:
        if acquired:
            try:
                _plat_unlock(fd)
            except OSError:
                pass  # best-effort unlock; os.close(fd) below and process exit release it regardless
        os.close(fd)


#: C4 — the one-time cutover from today's real `env_root` directory to the
#: junction layout. MEASURED, not assumed (scratch fixture mimicking the env
#: shape, N background reader processes doing short open/read/close cycles,
#: bounded retry on `os.rename` of the tree root, 2s budget, 20 trials per N,
#: this host, 2026-08-20):
#:
#:   N=1    success 100%   ~1.05 attempts   ~0.8ms
#:   N=10   success 100%   ~1.35 attempts   ~1.8ms
#:   N=30   success 100%   ~2.25 attempts   ~3.2ms
#:   N=70   success  40%   ~415  attempts   ~0.79s on success; failures hit the 2s cap
#:
#: This box's stated norm is 50-70 concurrent sessions (CLAUDE.md § Load
#: norm), so a 2s budget wins easily at typical load and is marginal at
#: peak. This is a ONE-TIME bootstrap, not a hot-path call and not something
#: that runs again once the fleet is on the junction layout (AC7's idempotent
#: re-run takes the already-junction no-op branch instead) — so it can afford
#: to spend more wall-clock than the measurement above budgeted, in exchange
#: for a materially better shot at the N=70 tail. 10s (5x the measured
#: budget) is chosen on that basis, not from a re-run of the N=70 probe: a
#: 70-process load probe is itself real load on a 50-70-session machine and
#: must not be re-run casually (see the measurement's own note). If 10s is
#: still not enough, `_cutover_to_junction_layout` refuses loudly rather than
#: looping unboundedly — `coordinator/bin/fleet-env-cutover.py` is the named
#: retry-later fallback.
_CUTOVER_RETRY_BUDGET_SECS = 10.0

#: Retry backoff shape for the rename loop — same pattern as
#: `locked_write._acquire_flock`'s poll-with-backoff (not reused directly:
#: that function locks an fd, this retries a rename), so the two don't drift
#: into different retry idioms for what is conceptually the same "wait for a
#: transient holder to let go" operation.
_CUTOVER_RETRY_INITIAL_INTERVAL_SECS = 0.005
_CUTOVER_RETRY_MAX_INTERVAL_SECS = 0.1


class FleetEnvCutoverBlocked(FleetEnvError):
    """Raised when `_cutover_to_junction_layout`'s bounded retry is exhausted.

    Names the condition (a fleet session is importing, holding a handle
    inside `env_root`) and the runnable fallback
    (`coordinator/bin/fleet-env-cutover.py`) rather than looping unboundedly
    or forcing the rename. Mutates nothing when raised — `env_root` is left
    exactly as found (a real directory)."""


@dataclass(frozen=True)
class CutoverOutcome:
    """Result of `_cutover_to_junction_layout` — what the CLI script prints."""

    status: str  # "already-junction" | "cutover"
    env_root: Path
    generation: "Optional[Path]"


def _is_transient_rename_failure(exc: OSError) -> bool:
    """True iff `exc` is the exact WinError 5 shape this cutover retries —
    a `PermissionError` raised because a plain-open reader handle exists
    somewhere inside the tree being renamed (measured, see module docstring
    NEGATIVE SPEC). Any other `OSError` (real permission denial, a bad path,
    a different Windows error code) is NOT transient and must propagate
    immediately rather than being silently retried for up to
    `_CUTOVER_RETRY_BUDGET_SECS`."""
    if not isinstance(exc, PermissionError):
        return False
    if os.name != "nt":
        # POSIX PermissionError is a real permission problem, never the
        # transient reader-handle shape this retries.
        return False
    return getattr(exc, "winerror", None) == 5


def _rename_with_retry(src: Path, dst: Path, *, budget_secs: float) -> None:
    """`os.rename(src, dst)`, bounded-retrying only the transient WinError 5
    shape (see `_is_transient_rename_failure`) for up to `budget_secs` of
    wall clock. Raises the last `PermissionError` seen once the budget is
    exhausted — the caller (`_cutover_to_junction_layout`) turns that into
    the named `FleetEnvCutoverBlocked` remediation. Any non-transient
    `OSError` propagates on first occurrence, unretried."""
    deadline = time.monotonic() + budget_secs
    interval = _CUTOVER_RETRY_INITIAL_INTERVAL_SECS
    while True:
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            if not _is_transient_rename_failure(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(interval, remaining))
            interval = min(interval * 1.5, _CUTOVER_RETRY_MAX_INTERVAL_SECS)


def _verify_read_through(env_root: Path) -> None:
    """AC7's "verify a real read through env_root before returning" —
    iterating the directory forces the OS to resolve the junction, not just
    check that a reparse point exists at the name. Raises `FleetEnvError`
    (never lets a raw `OSError` escape) on failure so a caller catching this
    module's contract sees it."""
    try:
        list(env_root.iterdir())
    except OSError as exc:
        raise FleetEnvError(
            f"fleet_env: cutover created the junction at {env_root} but a "
            f"read through it failed ({type(exc).__name__}: {exc}) — the "
            "environment is not readable; investigate before retrying."
        ) from exc


def _cutover_to_junction_layout(
    env_root: Path, *, retry_budget_secs: float = _CUTOVER_RETRY_BUDGET_SECS
) -> CutoverOutcome:
    """C4 — the one-time cutover of `env_root` from today's real-directory
    layout to the junction layout `_swap_in_new_env` (C2/C3) requires.

    The bootstrapping problem this exists to solve: to put a junction AT
    `env_root` you must first vacate that name, and vacating a real
    directory means renaming it — the exact operation that raises WinError 5
    under an open reader handle (see module docstring NEGATIVE SPEC). So
    this CANNOT use `_swap_in_new_env`'s fast path to install itself; it is
    the one place in this module that still renames a real directory, and it
    does so exactly once, ever, per environment root.

    Idempotent (AC7 requires re-running it safely):
      - `env_root` already a junction -> no-op, returns "already-junction".
      - `env_root` a real directory -> bounded-retry rename under the build
        lock (the SAME lock `ensure_fleet_env` uses, so a cutover can never
        interleave with a concurrent rebuild), then `create_junction`
        pointing at the moved generation, then `_verify_read_through`.
      - retry exhausted -> raises `FleetEnvCutoverBlocked` naming the
        condition and the runnable fallback script. Nothing is mutated: the
        rename never succeeded, so `env_root` is exactly as found.
      - rename succeeded but `create_junction` failed -> renames the
        generation BACK to `env_root` through the SAME bounded retry the
        vacate rename used (the tree can still carry an open reader handle
        post-rename, so the restore is exposed to the identical transient
        WinError-5 shape) and re-raises the original `create_junction`
        failure. Leaving the fleet with no `env_root` at all is the one
        unacceptable outcome here; if the restore's own retry is ALSO
        exhausted, that terminal state is raised as a `FleetEnvError` naming
        `env_root` as absent and the generation directory the environment
        still lives at, rather than a bare traceback.
      - `env_root` absent entirely (never provisioned) -> raises
        `FleetEnvError`; there is nothing to cut over, and silently treating
        absence as success would hide a real problem from the caller.
    """
    if junction.is_junction(env_root):
        return CutoverOutcome(
            status="already-junction",
            env_root=env_root,
            generation=junction.junction_target(env_root),
        )

    if not env_root.is_dir():
        raise FleetEnvError(
            f"fleet_env: {env_root} does not exist — nothing to cut over. "
            "Run ensure_fleet_env() first to provision the environment."
        )

    lock_path = Path(str(env_root) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            acquired = _plat_try_lock(fd)
        except OSError as exc:
            raise FleetEnvError(
                f"fleet_env: could not acquire the build lock at {lock_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not acquired:
            raise FleetEnvContention(
                "[fleet-env] another session is rebuilding or cutting over "
                "the fleet environment; retry in a moment"
            )

        # Re-check after acquiring the lock — another process may have
        # already performed the cutover while this one waited.
        if junction.is_junction(env_root):
            return CutoverOutcome(
                status="already-junction",
                env_root=env_root,
                generation=junction.junction_target(env_root),
            )

        generation = _build_dir_for(env_root)
        try:
            _rename_with_retry(env_root, generation, budget_secs=retry_budget_secs)
        except OSError as exc:
            if not _is_transient_rename_failure(exc):
                # A real permission/path problem, not the reader-handle
                # shape this retries — propagate as-is rather than
                # mislabelling it as a retry-budget exhaustion.
                raise
            raise FleetEnvCutoverBlocked(
                f"fleet_env: could not cut {env_root} over to the junction "
                f"layout — a fleet session is importing (holding a handle "
                f"inside the tree) and the {retry_budget_secs:.0f}s retry "
                f"budget was exhausted ({type(exc).__name__}: {exc}). "
                "Nothing was mutated. Retry at a quieter moment with: "
                f"python3 {_C1_RESOLVER_PATH.parent / 'fleet-env-cutover.py'}"
            ) from exc

        try:
            junction.create_junction(env_root, generation)
        except Exception as create_exc:
            # Restore: undo the rename so env_root is a real directory
            # again, exactly as found. Leaving env_root absent is the one
            # unacceptable outcome (see docstring). This undoes the SAME
            # vacate-rename `_rename_with_retry` above just retried under
            # load — the tree can still carry an open reader handle that
            # survives a rename — so the restore is exposed to the
            # identical transient WinError-5 shape and must go through the
            # same bounded retry, never a bare os.rename (slice-B review
            # HIGH finding).
            try:
                _rename_with_retry(generation, env_root, budget_secs=retry_budget_secs)
            except OSError as restore_exc:
                # Genuinely unrecoverable: the restore's own retry budget
                # is exhausted too. env_root is left ABSENT — name that
                # state and where the environment actually lives, plus the
                # runnable fallback, rather than letting create_exc's
                # traceback (now stale) reach the operator alone.
                raise FleetEnvError(
                    f"fleet_env: cutover of {env_root} failed to create the "
                    f"junction ({type(create_exc).__name__}: {create_exc}), and "
                    f"restoring the vacated directory back to {env_root} also "
                    f"failed after the retry budget was exhausted "
                    f"({type(restore_exc).__name__}: {restore_exc}) — env_root "
                    f"is currently ABSENT; the environment itself is intact at "
                    f"{generation}. Rename {generation} back to {env_root} "
                    "manually once no session holds it open, then retry: "
                    f"python3 {_C1_RESOLVER_PATH.parent / 'fleet-env-cutover.py'}"
                ) from restore_exc
            raise

        _verify_read_through(env_root)
        return CutoverOutcome(status="cutover", env_root=env_root, generation=generation)
    finally:
        if acquired:
            try:
                _plat_unlock(fd)
            except OSError:
                pass
        os.close(fd)


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="fleet_env.py",
        description="Idempotently ensure the fleet shared Python environment.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: report status without mutating disk.",
    )
    args = parser.parse_args(argv)
    try:
        status = ensure_fleet_env(check_only=args.check)
    except FleetEnvError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"fleet_env: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

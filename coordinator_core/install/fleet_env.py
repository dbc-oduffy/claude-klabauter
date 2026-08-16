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
    (`FleetEnvContention`), no polling.
  - Rename-swap rebuild: a rebuild NEVER mutates the live environment tree
    in place. The replacement is built at a fresh `.build-<pid>-<hex>`
    sibling and only `os.rename`-swapped into `env_root` once it has passed
    the health probe; the vacated old tree is moved to a `.stale-<pid>-<hex>`
    sibling first, then reclaimed (immediately on POSIX, best-effort with
    deferred reclaim via `_sweep_orphaned_swap_dirs` on Windows, where a
    reader's still-open handle can make immediate deletion fail). This
    matters MORE here than for the settings-home coordinator venv:
    `ensure_venv`'s own module docstring records this pattern replacing an
    in-place `rmtree` (DoE `4591a557`) because readers hold no lock, and this
    environment has the WHOLE FLEET as such readers on a 50-70-session box.

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
must not drift apart independently.

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
after every rebuild (never on the healthy fast path, where nothing was
destroyed) and always consults the registry itself
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
contract.

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

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md § C4
Spec backlink: docs/reference/fleet-shared-environment-contract.md
    § Provisioning the environment (C4)
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional

from coordinator_core._settings_home import settings_home as _default_settings_home
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
    primary_candidate = resolve_fleet_env_root()
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
    """Healthy iff `python_bin` is executable AND every module named in
    `_FLEET_ENV_IMPORT_PROBES` imports successfully under it. Executes under
    the TARGET interpreter, never in-process — a different interpreter's
    site-packages is not importable from this one (same isolation-boundary
    rationale `ensure_venv._venv_healthy` documents and cites)."""
    if not is_executable(python_bin):
        return False
    probe = "; ".join(f"import {mod}" for mod in _FLEET_ENV_IMPORT_PROBES)
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
    """A fresh sibling path a rebuild populates BEFORE ever touching the
    live tree — mirrors `ensure_venv._build_dir_for`'s naming convention so
    `_sweep_orphaned_swap_dirs` here matches the same `.build-<pid>-<hex>`
    shape an operator or diagnostic script already knows to look for."""
    return env_root.parent / f"{env_root.name}.build-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _sweep_orphaned_swap_dirs(env_root: Path) -> None:
    """Best-effort reclaim of `.build-*`/`.stale-*` siblings abandoned by a
    prior process that crashed mid-rebuild or mid-swap. Safe unconditionally
    here: the caller holds the build lock before calling this, so nothing
    else is concurrently populating its own `.build-*` sibling of THIS
    environment right now. Never raises — a sweep failure must not block the
    rebuild it is merely tidying up after. Same reasoning as
    `ensure_venv._sweep_orphaned_swap_dirs`: every `.stale-*` match already
    failed the health probe that triggered its own rebuild, so there is no
    "still-good" tree here to lose."""
    parent = env_root.parent
    build_prefix = f"{env_root.name}.build-"
    stale_prefix = f"{env_root.name}.stale-"
    try:
        children = list(parent.iterdir())
    except OSError:
        return
    for child in children:
        if child.name.startswith(build_prefix) or child.name.startswith(stale_prefix):
            shutil.rmtree(child, ignore_errors=True)


def _swap_in_new_env(env_root: Path, build_dir: Path) -> None:
    """Publish `build_dir` as `env_root` via a rename-swap — never an
    in-place `rmtree` of the live tree. Both `os.rename` calls are
    metadata-only directory-entry updates on POSIX AND Windows, so a
    concurrent reader executing out of the OLD tree right now keeps a
    coherent view instead of having its interpreter gutted out from under
    it — the exact property that matters more here than anywhere else in
    this repo, since the fleet is this environment's reader set on a
    50-70-session box. Reclaiming the vacated old tree differs by platform
    only in WHEN: POSIX can `rmtree` immediately (a reader's already-open
    file descriptor survives unlinking); Windows can raise a sharing
    violation on that same delete while a reader's handle is open, so that
    failure is caught and the sibling is left for
    `_sweep_orphaned_swap_dirs` to reclaim on this environment's NEXT
    rebuild (deferred, not leaked)."""
    stale_dir: Optional[Path] = None
    if env_root.is_dir():
        stale_dir = env_root.parent / f"{env_root.name}.stale-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        os.rename(env_root, stale_dir)
    os.rename(build_dir, env_root)
    if stale_dir is not None:
        try:
            shutil.rmtree(stale_dir)
        except OSError as exc:
            print(
                f"[fleet-env] WARNING: could not immediately reclaim the prior "
                f"environment tree ({type(exc).__name__}: {exc}) — a reader likely "
                f"still has it open (expected on Windows). Left at {stale_dir} for "
                "reclamation on the next rebuild.",
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
        (project_dir / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
        (project_dir / "uv.lock").write_text(lock_text, encoding="utf-8")

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
    """Write one `.pth` file. Caller MUST already hold the `held_lock` on
    `site_packages` — this has no locking of its own (see
    `register_sibling_binding`/`_replay_registered_bindings`, the only two
    callers, each of which acquires that lock exactly once around
    potentially many of these calls)."""
    site_packages.mkdir(parents=True, exist_ok=True)
    (site_packages / _pth_basename(repo, sibling)).write_text(path_str + "\n", encoding="utf-8")


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


def _replay_sibling_bindings(env_root: Path) -> None:
    """Always consults the binding registry directly — see module docstring
    "Binding registry (C6)". This does NOT depend on any other module
    having been imported first or having set a global: the registry lives
    in this module, and this function is called unconditionally after every
    rebuild (never on the healthy fast path, where the tree was never
    destroyed and any prior bindings are still present). `BINDING_REPLAY_HOOK`,
    if set, runs afterward as an additional extension point only."""
    registry_path = _binding_registry_path()
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


def ensure_fleet_env(
    *,
    check_only: bool = False,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
    uv_executable: str = "uv",
) -> str:
    """Idempotently ensure the fleet shared Python environment exists and
    is healthy at the resolved root (C1 + C5), installing exactly C3's
    committed lock.

    Returns one of `"ready"` (already healthy, no mutation), `"rebuilt"`
    (was absent or unhealthy, freshly built and swapped in), or
    `"would-rebuild"` (`check_only=True` and the environment is currently
    absent or unhealthy — dry-run, no mutation).

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

    env_root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(env_root) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        acquired = _plat_try_lock(fd)
        if not acquired:
            raise FleetEnvContention(
                "[fleet-env] another session is rebuilding the fleet environment; "
                "retry in a moment"
            )

        # Re-check health after acquiring the lock — another session may
        # have finished building while we waited for it.
        if _fleet_env_healthy(python_bin):
            return "ready"

        _sweep_orphaned_swap_dirs(env_root)
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

        _swap_in_new_env(env_root, build_dir)
        _replay_sibling_bindings(env_root)
        return "rebuilt"
    finally:
        if acquired:
            try:
                _plat_unlock(fd)
            except OSError:
                pass  # best-effort unlock; os.close(fd) below and process exit release it regardless
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

"""
machine_resolver.py — Python port of the machine/contributor resolver core
(cs_compute_machine, cs_compute_machine_live, cs_compute_contributor,
cs_compute_contributor_live).

Port of: coordinator-daily-branch.sh (example-doctrine-repo 2fbe0e77, 2026-07-19).
De-bash campaign: kill bash on the Windows critical path — this is the Zone-A
"machine_resolver" native op (import-only module, sibling shape to daily_branch.py).

Purpose: the external-primitive resolvers daily_branch.py's own docstring
explicitly carves OUT of that module's scope (registry / hostname / git-config
shell-outs, as opposed to pure branch-shape parsing). This module is that
carved-out half:
    cs_compute_machine        -> compute_machine
    cs_compute_machine_live   -> compute_machine_live
    cs_compute_contributor    -> compute_contributor
    cs_compute_contributor_live -> compute_contributor_live

Reuses existing claude-klabauter bindings rather than re-deriving them:
    coordinator_core._settings_home.machine_local_dir() — registry directory root
    socket.gethostname()                                — hostname primitive
    subprocess ['git', 'config', 'user.email']           — contributor-slug seed
    coordinator_core.daily_branch.sanitize_slug          — cs_sanitize_slug port
                                                             (raw-email -> slug)
    coordinator_core.ops.emit._slug.machine_slug         — final tail-sanitization
                                                             (lowercase + collapse
                                                             non-[a-z0-9] runs),
                                                             mirroring the bash
                                                             originals' trailing
                                                             `tr '[:upper:]' '[:lower:]'`
                                                             pipeline on every
                                                             resolved value.

Registry read is a DIRECT TOML read (no `machine-local` CLI subprocess) — this
port sits alongside coordinator_core.ops.check_machine_local_regeneratability,
which established the same in-process tomllib pattern for the identical
registry directory. `coordinator.machine_slug` / `coordinator.contributor_slug`
are flat quoted-dotted-key entries at the root of registry.local.toml /
registry.toml (not a promoted concern file) — confirmed against the tracked
templates/machine-local/registry.toml.example and a live registry.local.toml
instance. A `MACHINE_LOCAL_<KEY>` env escape hatch mirrors the real CLI's
env-var override rung (`_machine_local.py::_env_key`) so callers/tests can pin
a value without touching the on-disk registry.

`registry_get` (public since DR-071, 2026-07-22) is this same direct-tomllib
reader, promoted to a public name so the example-doctrine-repo-root anchor consumers listed on
its docstring can bind to `repos.example_doctrine_repo` reset-safely without duplicating
a second TOML parser. `_registry_get` is kept as an alias for this module's
own pre-existing internal callers.

Negative-spec (do NOT "fix" while porting):
  - Do NOT shell out to a `machine-local` CLI/binary or `_machine_local.py` —
    the whole point of this module is to be Windows-invocable without a shell
    trampoline; a direct tomllib read is the load-bearing choice.
  - Registry precedence here is registry.local.toml > registry.toml only (no
    concern-file layer) — `coordinator.*` keys are not a promoted concern
    namespace on any observed registry instance. If that ever changes, this
    module's `registry_get` needs its layer list extended, not silently
    reworked into the full `_machine_local.py::_build_resolution_layers` stack.
  - An empty-string registry value is treated as NOT FOUND (falls through to
    the next resolution rung) — mirrors the bash `[[ -n "$_slug" ]]` gate on
    the CLI's `get --default ""` result.

De-bash spawn-amplification hardening (2026-08-05, PM directive): the
`git config user.email` spawn on ``compute_contributor()``'s fallback rung
(registry key absent) is now process-lifetime-cached via
``_git_user_email_cached``/``reset_git_user_email_cache`` — same shape as
``subagent_sandbox.engine.resolve_git_root``'s cache (cache successes only,
never memoize a failure into a success, explicit reset seam). Do NOT let this
cache leak into ``compute_contributor_live()`` — that function calls
``_git_user_email_uncached`` directly and must keep spawning fresh every
call; see each function's own docstring.

Circular-import note (2026-07-22): ``coordinator_core.ops.emit._slug`` (the
tail-sanitizer, see ``_tail_slug`` below) is imported lazily, function-local,
rather than at module level. Importing ANY name under ``coordinator_core.ops``
forces Python to first fully execute ``coordinator_core/ops/__init__.py``,
which (default/eager mode) walks its full op-module list — including
``doe_root_pointer``'s transitive chain back to THIS module's ``registry_get``.
A module-level import here raced that cascade: whichever of
{``machine_resolver``, ``coordinator_core.ops``} was imported first left the
other partially initialized, so the loser's needed name (``registry_get``,
not yet defined at that point in this file) failed to resolve. Deferring the
``ops.emit._slug`` import to call time — after this module has finished
executing top to bottom — sidesteps the race entirely; it doesn't matter
which side the eager-import cascade reaches first, because by the time
``_tail_slug`` is actually called, this module is always fully initialized.
"""

from __future__ import annotations

import functools
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core import _settings_home
from coordinator_core.daily_branch import sanitize_slug

_GIT_TIMEOUT = 10


def _tail_slug(value: str) -> str:
    """Final tail-sanitization rung (lowercase + collapse non-``[a-z0-9]``
    runs) — thin call-time-deferred wrapper around
    ``coordinator_core.ops.emit._slug.machine_slug``. See the module
    docstring's "Circular-import note" for why this import is function-local
    rather than hoisted to module level."""
    from coordinator_core.ops.emit._slug import machine_slug

    return machine_slug(value)


# ---------------------------------------------------------------------------
# Registry read (direct TOML, no CLI subprocess)
# ---------------------------------------------------------------------------


def registry_dir() -> Path:
    """Resolve the machine-local registry directory.

    ``MACHINE_LOCAL_REGISTRY_DIR`` env override (test isolation) takes
    precedence over the settings-home-derived default — mirrors the same
    override rung used by ``check_machine_local_regeneratability._resolve_registry_dir``.

    Public since the ``refresh-plugin-live-install.py`` split-brain fix
    (2026-07-28): this is now the SOLE registry-directory ladder — every
    caller that needs "where is the machine-local registry" (gate checks,
    field-value reads via ``registry_get`` below) MUST resolve through this
    one function, never re-derive `os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    or <settings-home fallback>` inline. A second hand-written copy of this
    ladder is exactly the split-brain this fix closed — see that CLI's
    ``main()`` and ``_read_registry`` docstring for the incident.
    """
    override = os.environ.get("MACHINE_LOCAL_REGISTRY_DIR")
    if override:
        return Path(override)
    return _settings_home.machine_local_dir()


# Legacy alias — retained for any pre-existing internal callers that predate
# the public promotion above. New callers should use ``registry_dir`` directly.
_registry_dir = registry_dir


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        import tomllib as _tomllib  # type: ignore[import-not-found]
    except ImportError:
        # Pre-3.11 interpreter without the tomllib stdlib module and without the
        # tomli backport installed: degrade to "registry not found" rather than
        # raising — same graceful-degradation contract as the malformed-file
        # case below. Not expected to fire under this repo's Python 3.11+ floor.
        try:
            import tomli as _tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        with open(path, "rb") as f:
            return _tomllib.load(f)
    except Exception:  # noqa: BLE001 — faithful catch-all; a malformed registry degrades to "not found"
        return {}


def _flatten(data: dict, prefix: str = "") -> dict:
    """Flatten nested TOML tables into dotted keys.

    Mirrors ``_machine_local.py::_flatten_nested`` so both natural table
    syntax (``[coordinator]\\nmachine_slug = "..."``) and the flat
    quoted-dotted-key form (``"coordinator.machine_slug" = "..."``) resolve
    to the same canonical key.
    """
    out: dict = {}
    for k, v in data.items():
        if k in ("schema", "concerns"):
            continue
        full = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{full}."))
        else:
            out[full] = v
    return out


def load_flat_registry_file(path: Path) -> dict:
    """Public wrapper: load and flatten ONE registry TOML file — no
    multi-file resolution/precedence, that's ``registry_get``'s job. Exists
    for callers that need a specific file's raw declared keys (e.g. "is this
    key EXPLICITLY declared empty in the tracked ``registry.toml``, as
    opposed to absent" — a distinction ``registry_get``'s merged
    not-found-collapses-to-None contract can't express) without reaching
    into the ``_load_toml``/``_flatten`` privates directly."""
    return _flatten(_load_toml(path))


def _env_override_key(key: str) -> str:
    """Convert a dotted registry key to its env-var override name (mirrors ``_machine_local.py::_env_key``)."""
    return "MACHINE_LOCAL_" + key.upper().replace(".", "_")


def registry_get(key: str) -> Optional[str]:
    """Resolve a dotted registry key, or None if unresolved.

    Resolution order: ``MACHINE_LOCAL_<KEY>`` env override -> registry.local.toml
    -> registry.toml. Empty-string values are treated as not-found (see module
    docstring negative-spec).

    Public promotion (DR-071, 2026-07-22): this is the direct-tomllib registry
    reader every example-doctrine-repo-root anchor consumer (``coordinator_core.doe_root_pointer``,
    ``coordinator_core.trusted_root_guard``, ``coordinator_core.
    resolve_coordinator_clone``, ``coordinator_core.install._shared``) now binds
    ``repos.example_doctrine_repo`` reads to, in preference to the ``machine-local`` CLI —
    the CLI's reader/exec bits live under the resettable ``~/.claude/bin/``, so
    a Claude Code reset that wipes ``~/.claude`` breaks the CLI even though the
    registry TOML under settings-home survives untouched. "``machine-local get``
    works" is therefore not proof of reset-survival; a direct read of this
    function is.
    """
    env_override = os.environ.get(_env_override_key(key))
    if env_override:
        return env_override

    reg_dir = registry_dir()
    for fname in ("registry.local.toml", "registry.toml"):
        flat = _flatten(_load_toml(reg_dir / fname))
        if key in flat:
            val = flat[key]
            if isinstance(val, list):
                val = "\n".join(str(i) for i in val)
            s = str(val)
            if s:
                return s
    return None


# Legacy alias — retained for the pre-existing internal callers in this module
# (``compute_machine``/``compute_contributor``) that predate the DR-071 public
# promotion. New external callers should import ``registry_get`` directly.
_registry_get = registry_get


# ---------------------------------------------------------------------------
# Machine resolution
# ---------------------------------------------------------------------------


def _hostname_short() -> Optional[str]:
    """Return the short (domain-stripped) local hostname, or None on failure."""
    try:
        h = socket.gethostname()
    except OSError:
        # One rung of the documented multi-tier fallback chain (see
        # compute_machine/compute_machine_live docstrings) — falls through to
        # $HOSTNAME or "unknown"; not logged since this runs on every op
        # invocation and a healthy environment never hits it.
        return None
    if not h:
        return None
    return h.split(".", 1)[0]


def compute_machine() -> str:
    """Port of cs_compute_machine — the coordinator machine name, always lowercase.

    Resolution order, first hit wins: the COORDINATOR_MACHINE env var, then the
    machine-local registry key coordinator.machine_slug, then the COMPUTERNAME
    env var, then the short hostname, then the HOSTNAME env var, then the
    literal "unknown". Pure-read: never writes to the registry.
    """
    m: Optional[str]
    override = os.environ.get("COORDINATOR_MACHINE", "")
    if override:
        m = override
    else:
        m = _registry_get("coordinator.machine_slug")
        if not m:
            computername = os.environ.get("COMPUTERNAME", "")
            if computername:
                m = computername
            else:
                m = _hostname_short()
                if not m:
                    m = os.environ.get("HOSTNAME", "") or None

    return _tail_slug(m or "unknown")


def compute_machine_live() -> str:
    """Port of cs_compute_machine_live — machine name without a registry read.

    Resolution order, first hit wins: the COORDINATOR_MACHINE env var, then the
    COMPUTERNAME env var, then the short hostname, then the HOSTNAME env var,
    then the literal "unknown". Seed source for the registry key and drift-detection
    comparator; avoids circularity with compute_machine's registry-preferring
    resolution.
    """
    m: Optional[str]
    override = os.environ.get("COORDINATOR_MACHINE", "")
    if override:
        m = override
    else:
        computername = os.environ.get("COMPUTERNAME", "")
        if computername:
            m = computername
        else:
            m = _hostname_short()
            if not m:
                m = os.environ.get("HOSTNAME", "") or None

    return _tail_slug(m or "unknown")


# ---------------------------------------------------------------------------
# Contributor resolution
# ---------------------------------------------------------------------------


def _git_user_email_uncached() -> str:
    """Return `git config user.email`, or "" on any failure (missing git, no config, timeout).

    Uncached — spawns `git config` every call. This is the sole spawn site;
    ``compute_contributor_live()`` calls this directly (never the cached
    wrapper below) so the `_live` contract — "bypass cached/registry state,
    observe current reality" — holds even after the caching added below for
    ``compute_contributor()``'s own fallback rung.

    Review: code-reviewer (F4, nit) — by design, this collapses several
    operationally-distinct failure modes ("git not installed" vs. "git
    present but repo/config corrupt") to the same "" signal; not a bug.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


class _GitUserEmailResolutionFailed(Exception):
    """Internal-only signal so ``functools.lru_cache`` does NOT memoize a
    failed (empty) `git config user.email` resolution — mirrors
    ``subagent_sandbox.engine._GitRootResolutionFailed``: a transient
    failure (no git, no config, timeout) must not poison the cache for the
    rest of the process the way a successful resolution legitimately can."""


@functools.lru_cache(maxsize=1)
def _git_user_email_cached() -> str:
    """Process-lifetime, cwd-INsensitive cache of a resolved `git config
    user.email` (failure is not memoized — see ``_GitUserEmailResolutionFailed``).
    ``maxsize=1`` and no cwd key: the first successful resolution in a
    process is served to every later call regardless of ``os.chdir()`` in
    between. Safe today because claude-klabauter is spawn-per-call with no resident
    daemon — nothing in this codebase resolves, chdirs into a different
    repo with a different local `user.email`, and resolves again in the
    same process. A future long-lived or cwd-changing caller would need to
    key this cache on ``os.getcwd()`` (or an explicit repo-root argument)
    to stay correct; that widening is out of scope here since no live
    caller needs it.

    Review: code-reviewer (F2, P2) — named per the reviewer's recommendation
    rather than keying the cache, since no current caller changes cwd
    mid-process.
    """
    result = _git_user_email_uncached()
    if not result:
        raise _GitUserEmailResolutionFailed()
    return result


def reset_git_user_email_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``_git_user_email_cached()`` cache. Call this in test teardown/setup for
    any test that stubs/varies the `git config user.email` spawn, since the
    cache is process-global and otherwise leaks a prior test's resolved
    value into a later one."""
    _git_user_email_cached.cache_clear()


def _git_user_email_for_non_live() -> str:
    """Cached read used ONLY by ``compute_contributor()``'s own fallback rung
    (registry absent) — never by ``compute_contributor_live()``, which must
    always observe current reality. See ``reset_git_user_email_cache()``."""
    try:
        return _git_user_email_cached()
    except _GitUserEmailResolutionFailed:
        return ""


def _resolve_contributor_from_email(get_raw_email) -> str:
    """Shared sanitization pipeline for both the live and cached raw-email
    sources: env override -> sanitized local-part of the raw email -> the
    literal "unknown". The @domain portion of user.email is dropped before
    sanitizing (PII-minimize) — only the local-part seeds the slug. A
    malformed value with no `@` passes through unchanged and is sanitized
    as-is."""
    override = os.environ.get("COORDINATOR_CONTRIBUTOR", "")
    if override:
        c = override
    else:
        raw = get_raw_email()
        local_part = raw.split("@", 1)[0]
        c = sanitize_slug(local_part)

    if not c:
        c = "unknown"
    return _tail_slug(c)


def compute_contributor_live() -> str:
    """Port of cs_compute_contributor_live — contributor slug without a registry
    read AND without the process-lifetime cache added for
    ``compute_contributor()``'s fallback rung: always spawns
    `git config user.email` fresh. Resolution order, first hit wins: the
    COORDINATOR_CONTRIBUTOR env var, then the sanitized local-part of git
    user.email, then the literal "unknown".
    """
    return _resolve_contributor_from_email(_git_user_email_uncached)


def compute_contributor() -> str:
    """Port of cs_compute_contributor — the coordinator contributor slug, always lowercase.

    Resolution order, first hit wins: the COORDINATOR_CONTRIBUTOR env var, then
    the machine-local registry key coordinator.contributor_slug, then a
    process-lifetime-cached git-user-email resolution (see
    ``_git_user_email_for_non_live``/``reset_git_user_email_cache``), then the
    literal "unknown". Registry-preferring canonical resolver — mirrors
    compute_machine's shape exactly. The fallback rung intentionally does NOT
    call ``compute_contributor_live()`` — that function must stay spawn-fresh
    every call (the `_live` contract); this rung instead shares
    ``_resolve_contributor_from_email``'s sanitization pipeline against the
    cached raw-email source.
    """
    override = os.environ.get("COORDINATOR_CONTRIBUTOR", "")
    if override:
        c = override
    else:
        c = _registry_get("coordinator.contributor_slug")
        if not c:
            c = _resolve_contributor_from_email(_git_user_email_for_non_live)

    if not c:
        c = "unknown"
    return _tail_slug(c)


def main(argv: Optional[list] = None) -> int:  # pragma: no cover — manual smoke aid
    """Manual smoke entry point: print all four resolved values."""
    print(f"machine: {compute_machine()}")
    print(f"machine_live: {compute_machine_live()}")
    print(f"contributor: {compute_contributor()}")
    print(f"contributor_live: {compute_contributor_live()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))

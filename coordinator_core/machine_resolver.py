"""
machine_resolver.py — Python port of the machine/contributor resolver core
(cs_compute_machine, cs_compute_machine_live, cs_compute_contributor,
cs_compute_contributor_live).

Port of: coordinator-daily-branch.sh (DoE 2fbe0e77, 2026-07-19).
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
reader, promoted to a public name so the DoE-root anchor consumers listed on
its docstring can bind to `repos.doe_claude` reset-safely without duplicating
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

Repo-root cache-key fix (2026-08-15, C7): ``_git_user_email_cached`` was
``lru_cache(maxsize=1)`` and ZERO-ARG, and ``_git_user_email_uncached`` ran
`git config --get user.email` with no ``cwd=``. Under a spawn-per-call
process this was inert (one process, one cwd), but under the warm resident
engine (DR-315) a single process resolves contributors for clients in
DIFFERENT repos — a missing-key COLLISION, not staleness: the first repo's
resolved email is silently served to every other repo's resolution for the
rest of the process. Fixed by keying the cache on the resolved repo root
(``_resolve_repo_root``) AND passing that same root as ``cwd=`` to the
underlying `git config` spawn, so the value cached under a given key is
actually resolved against that key's directory. ``_resolve_repo_root``
(``coordinator_core._repo_root_probe.resolve_repo_root``, shared with
``person_resolver``) is memoized per AMBIENT cwd (2026-08-16, review-
integrator P2 fix) rather than per call — see that module's own docstring
for why a spawn on every cache-hit-computing call was itself a regression
this fix closes, and why the memo is keyed on cwd (not a single slot) to
avoid recreating the exact missing-key collision this note describes.

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

import datetime
import functools
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core import _settings_home
from coordinator_core._repo_root_probe import (
    reset_repo_root_memo as _reset_repo_root_memo,
    resolve_repo_root as _resolve_repo_root,
)
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
    reader every DoE-root anchor consumer (``coordinator_core.doe_root_pointer``,
    ``coordinator_core.trusted_root_guard``, ``coordinator_core.
    resolve_coordinator_clone``, ``coordinator_core.install._shared``) now binds
    ``repos.doe_claude`` reads to, in preference to the ``machine-local`` CLI —
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


_REGISTRY_TARGET_FILE = "registry.local.toml"

_REGISTRY_NEW_FILE_HEADER = (
    "# registry.local.toml  (created by `machine-local set`)\n"
    "#\n"
    "# WARNING: Use `machine-local set <key> <value>` to add or change values.\n"
    "# Direct hand-edits are fragile: they do not reproduce on reinstall and\n"
    "# will not transfer automatically to a new machine.\n"
    "schema = 1\n"
)


def _parse_toml_text(text: str) -> dict:
    """Best-effort ``tomllib.loads`` over in-memory text — malformed content
    degrades to ``{}`` (mirrors ``_load_toml``'s own graceful-degradation
    contract; this call site only ever consults the result for an
    idempotency comparison, never as the sole source of truth)."""
    try:
        import tomllib as _tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as _tomllib  # type: ignore[no-redef]
        except ImportError:
            return {}
    try:
        return _tomllib.loads(text)
    except Exception:  # noqa: BLE001 — faithful catch-all, see docstring
        return {}


def registry_set(key: str, value: str) -> None:
    """Write one flat, root-namespace registry key into
    ``registry.local.toml`` — in-process, no ``machine-local`` CLI
    subprocess.

    Restoration note (2026-08-19): ``coordinator_core.install.first_run.
    _seed_machine_local_registry`` used to shell out to the ``machine-local``
    CLI binary (``coordinator/bin/machine-local`` and its ``.cmd``/``.ps1``
    Windows twins) to perform this write. That binary was deleted in
    ``3bd2738f4`` (2026-08-14, "C5: delete the three dead bareword
    forwarders and their Windows twins") as unreachable dead code — correct
    on its own terms (nothing on PATH ever resolved those files), but it
    silently broke Step 3's registry seed, which guarded on the binary's
    existence and had nothing left to spawn from that date forward. This
    function restores the capability as a genuine in-process write rather
    than resurrecting a forwarder with nothing left to forward to — do NOT
    "fix" this by re-adding a `_run([machine_local_bin, "set", ...])` spawn.
    The real, full-featured ``machine-local`` CLI still lives at
    ``<settings-home>/bin/machine-local`` for interactive/operator use; this
    function is a narrower, purpose-built writer for this module's own
    single-writer ``repos.*`` namespace, not a reimplementation of it.

    Scope: ONLY the flat, root-namespace ``"<key>" = '<value>'`` shape — the
    same shape ``repos.*`` and ``publish.mirrors.*.path`` keys always use
    (never promoted to a concern-file namespace — see
    ``merged_flat_registry``'s docstring). Does not handle table-form
    definitions, concern-namespaced keys, or array values; a caller needing
    those must go through the real ``machine-local`` CLI.

    Shape sanctioned for exactly this single-writer-namespaced-table case by
    DoE-claude's ``docs/wiki/machine-local-registry.md``: "Append-only
    writers that structurally preserve sibling tables (read ->
    tomllib-parse-absent-check -> append -> atomic os.replace) satisfy the
    preserve-unrelated-tables property by construction and need no
    provenance."

    Idempotent: a key already present with the same value is a no-op (no
    file write at all — the journal contract this feeds needs to tell a
    genuinely-performed write from a no-op). A key present with a DIFFERENT
    value has its single line replaced in place — never a blind append,
    since TOML forbids redefining a top-level key and a naive append would
    corrupt the file on next read.

    Raises ``ValueError`` if ``value`` cannot be represented as a TOML
    literal string (contains ``'`` or a newline) — TOML literal strings have
    no escape mechanism, matching the real CLI's own refusal policy. Raises
    ``OSError`` on a genuine filesystem failure. Callers seeding multiple
    keys should catch both and warn-and-continue per key.
    """
    if "'" in value or "\n" in value:
        raise ValueError(
            f"registry_set({key!r}, ...): value cannot be written as a TOML "
            "literal string (contains a single quote or a newline)"
        )

    reg_dir = registry_dir()
    reg_dir.mkdir(parents=True, exist_ok=True)
    target_path = reg_dir / _REGISTRY_TARGET_FILE

    if target_path.is_file():
        content = target_path.read_text(encoding="utf-8")
    else:
        content = _REGISTRY_NEW_FILE_HEADER

    date_tag = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_line = f"\"{key}\" = '{value}'  # set {date_tag}"

    pattern = re.compile(r'^"' + re.escape(key) + r'"\s*=.*$', re.MULTILINE)
    match = pattern.search(content)
    if match:
        existing_value = _flatten(_parse_toml_text(content)).get(key)
        if existing_value == value:
            return  # already correct -- no-op, no write, no journal-worthy mutation
        new_content = content[: match.start()] + new_line + content[match.end() :]
    else:
        if not content.endswith("\n"):
            content += "\n"
        new_content = content + new_line + "\n"

    tmp_path = target_path.with_name(target_path.name + f".tmp{os.getpid()}")
    tmp_path.write_text(new_content, encoding="utf-8", newline="\n")
    os.replace(tmp_path, target_path)


def merged_flat_registry() -> dict:
    """Merge `registry.local.toml` over `registry.toml` (local wins), flattened
    to dotted keys, via a direct `tomllib` read — no `machine-local` CLI
    subprocess.

    Promoted from `coordinator_core.ops.discover_working_repos._merged_flat_registry`
    (2026-08-16) — the same helper, given a shared home so a MULTI-KEY caller
    (one that needs to enumerate a whole `repos.*`/`prefix.*` namespace rather
    than resolve a single dotted key, for which `registry_get` above is
    already the whole answer) does not reinvent this merge per call site.
    `repos.*` and `publish.mirrors.*.path` are confirmed root-namespace-only
    keys (never a promoted concern-file namespace — see
    `_machine_local.py::_flatten_nested`'s docstring and its
    concern-namespace-exclusivity check), so the same two-file precedence
    chain `registry_get` uses is sufficient here; no concern-file layer is
    consulted.

    Best-effort, matching every caller's never-block contract: a missing or
    unreadable registry file degrades to `{}` for that file (see
    `load_flat_registry_file`/`_load_toml`), never raises.
    """
    reg_dir = registry_dir()
    merged: dict = {}
    merged.update(load_flat_registry_file(reg_dir / "registry.toml"))
    merged.update(load_flat_registry_file(reg_dir / "registry.local.toml"))
    return merged


def registry_value(key: str, flat: dict) -> Optional[str]:
    """Resolve one dotted key against `flat` (a `merged_flat_registry()`
    result), honoring the per-key `MACHINE_LOCAL_<KEY>` env override rung
    first — mirrors `registry_get`'s precedence (env override ->
    registry.local.toml -> registry.toml) exactly, since `merged_flat_registry`
    itself only merges the two TOML files and never consults the environment.

    Promoted alongside `merged_flat_registry` (2026-08-16) — see that
    function's docstring. A multi-key caller iterates every matched key, so
    the override is applied per-key here rather than inside
    `merged_flat_registry`, which has no per-key concept.
    """
    env_override = os.environ.get(_env_override_key(key))
    if env_override:
        return env_override
    val = flat.get(key)
    return None if val is None else str(val)


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


# `_resolve_repo_root` is `coordinator_core._repo_root_probe.resolve_repo_root`
# (imported above) — a per-cwd-memoized shared probe. Review:
# review-integrator (P2, 2026-08-16) — this used to be a module-local,
# deliberately UNcached `git rev-parse --show-toplevel` spawn: correct in
# intent (the cwd is what varies call-to-call under the warm engine) but it
# meant a cache HIT on `_git_user_email_cached` still paid a fresh subprocess
# spawn just to compute the key, inverting this module's own
# spawn-elimination thesis. The shared probe also de-duplicates the
# near-identical copy that used to live in `person_resolver.py` (P3 nit).


def _git_user_email_uncached(cwd: Optional[str] = None) -> str:
    """Return `git config user.email`, or "" on any failure (missing git, no config, timeout).

    Uncached — spawns `git config` every call. This is the sole spawn site;
    ``compute_contributor_live()`` calls this directly with ``cwd=None``
    (never the cached wrapper below) so the `_live` contract — "bypass
    cached/registry state, observe current reality" — holds even after the
    caching added below for ``compute_contributor()``'s own fallback rung.
    ``cwd`` lets ``_git_user_email_cached`` pin the spawn to the same
    resolved repo root its cache is keyed on (see module docstring's
    "Repo-root cache-key fix" note) — ``cwd=None`` inherits the process's
    ambient directory, the original (defective) behaviour. Review:
    review-integrator (P3 nit, 2026-08-16) — this docstring previously named
    ``_git_user_email_for_non_live`` as the pinning call site; that function
    never calls this one directly, it goes through ``_git_user_email_cached``
    (which is what actually calls this with ``cwd=repo_root``).

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
            cwd=cwd,
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


@functools.lru_cache(maxsize=None)
def _git_user_email_cached(repo_root: Optional[str]) -> str:
    """Process-lifetime cache of a resolved `git config user.email`, keyed on
    ``repo_root`` (failure is not memoized — see
    ``_GitUserEmailResolutionFailed``).

    Keyed on the resolved repo root (2026-08-15, C7) rather than
    ``maxsize=1``/zero-arg: under the warm resident engine (DR-315) one
    process resolves contributors for clients in different repos, so a
    single unkeyed slot is a missing-key COLLISION — the first repo's
    resolved email would otherwise be served to every other repo's
    resolution for the rest of the process. ``repo_root`` is also passed
    through as ``cwd=`` to the underlying spawn, so the cached value is
    actually resolved against the directory it is keyed on.

    Review: code-reviewer (F2, P2) — original ``maxsize=1`` was named per
    the reviewer's recommendation on the (since superseded) assumption that
    no caller changes cwd mid-process; the warm engine removed that
    assumption.
    """
    result = _git_user_email_uncached(cwd=repo_root)
    if not result:
        raise _GitUserEmailResolutionFailed()
    return result


def reset_git_user_email_cache() -> None:
    """Test/diagnostic escape hatch — clears the process-local
    ``_git_user_email_cached()`` cache, and the shared per-cwd repo-root memo
    (``coordinator_core._repo_root_probe``) alongside it. Call this in test
    teardown/setup for any test that stubs/varies the `git config user.email`
    spawn, since the cache is process-global and otherwise leaks a prior
    test's resolved value into a later one. The repo-root memo is included
    so a test that stubs `subprocess.run` (and therefore also answers the
    `git rev-parse` repo-root probe) does not leak a prior test's fake
    "resolved root" into a later one either."""
    _git_user_email_cached.cache_clear()
    _reset_repo_root_memo()


def _git_user_email_for_non_live() -> str:
    """Cached read used ONLY by ``compute_contributor()``'s own fallback rung
    (registry absent) — never by ``compute_contributor_live()``, which must
    always observe current reality. Resolves the current repo root (see
    ``_resolve_repo_root``, per-cwd-memoized) and uses it as the cache key, so
    a warm process serving different repos never cross-serves one repo's
    cached email to another. See ``reset_git_user_email_cache()``."""
    repo_root = _resolve_repo_root()
    try:
        return _git_user_email_cached(repo_root)
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

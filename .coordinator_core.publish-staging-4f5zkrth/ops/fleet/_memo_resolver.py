"""
coordinator_core.ops.fleet._memo_resolver — shared receiver-registry resolver.

Purpose: ONE canonical machine-local-registry → receiver-inbox resolution surface,
factored out of `memo_send.py` (DoE-ratified 2026-07-05 Q1 resolver, already-shipped)
so every memo verb — `memo.send` (memo_send.py), `memo.list` (C2), `memo.draft`/
`memo.compose` (C5) — consumes this ONE implementation rather than forking a second
registry-resolution layer. Landed by C3 of
docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md (the full-ownership move
ratified by the DR-210 Amendment 2026-07-21, Option A).

Spec backlink:
    docs/decisions/DR-210-makima-native-tooling-ownership-strangler.md § Amendment
    2026-07-21 ("the shared registry-resolution mechanics ... factored out of
    memo_send.py and hardened to fail-loud-only").
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C3, AC3.
    Parity source (resolution rules only, not the fallback this hardens away):
    DoE coordinator/bin/cross-repo-memo.py receiver-resolution section.

Negative-spec:
  - Does NOT fall back to a sender-parent-folder scan (or ANY directory scan) on
    registry-read failure or on receiver-not-found, in any form — no opt-in flag,
    no environment-variable gate, no default-on-first-run convenience path. This is
    precisely the uncommanded-write safety edge (footgun #3, project-rag-em hit it
    live 2026-07-17) this module exists to remove. A future change proposing ANY
    scan-based fallback here is reintroducing that footgun, not fixing a gap.
  - `read_registry_repos()` distinguishes "registry not configured" (no
    registry.toml/registry.local.toml present at all — legitimate, returns `{}`)
    from "registry configured but unreadable" (a present file fails to parse, or
    tomllib is unavailable — a genuine environment/data defect) by RAISING
    `RegistryReadError` in the latter case. The pre-C3 memo_send.py implementation
    silently degraded both cases to `{}`, which could make a corrupt registry file
    look identical to "receiver not registered" to a caller — this is the fail-loud
    hardening AC3 asks for.
  - `resolve_receiver_inbox()` raises `AmbiguousReceiverError` when a central
    receiver id (identity.centralReceiverIds) fans in to MORE THAN ONE distinct
    registered repos.* key across the manifest's central-id set. The pre-C3
    implementation picked the first match found while iterating an unordered
    `set`, which was non-deterministic under genuine multi-registration
    misconfiguration; this replaces silent-arbitrary-pick with fail-loud.
  - Does NOT import DoE's `coordinator_registry` Python loader (DR-210 §1
    negative-spec, reaffirmed) — direct `registry.toml`/`registry.local.toml`
    read via stdlib `tomllib` remains the sanctioned surface.
  - Does NOT hardcode any alias, central-receiver id, or registry key literal —
    all come from the DoE-ratified manifest (`coordinator-registry.manifest.json`)
    or from the pure convention-mapping rule.

Public API
----------
Exceptions (all importable from this module):
  - `RegistryReadError(Exception)` — raised by `read_registry_repos()` when a
    present registry file cannot be parsed, or when `tomllib` is unavailable.
    NEVER raised for "no registry file present" (that is `{}`, not an error).
    `.reason: str` holds the human-readable cause.
  - `AmbiguousReceiverError(Exception)` — RAISED by `resolve_receiver_inbox()` when
    a central receiver id resolves to more than one distinct registered repos.*
    key. `.receiver_em_id: str`, `.candidate_keys: tuple[str, ...]` (sorted).

Functions:
  - `registry_home() -> Path`
        The machine-local registry directory (`<settings-home>/machine-local`).
        Never raises.
  - `read_doe_identity() -> dict`
        The DoE registry manifest's `identity` object — THE one manifest read
        in this module, resolving the DoE root through the canonical DR-071
        ladder (`coordinator_core.doe_root_pointer.read_doe_root_pointer()`:
        registry `repos.doe_claude`, then `<settings-home>/machine-local/
        .doe-root`, then the legacy `${CLAUDE_HOME:-$HOME}/.claude/.doe-root`).
        Graceful degradation: `{}` on any resolution/read/parse failure.
        Never raises.
  - `read_receiver_aliases() -> dict[str, str]`
        `{shortname: registryKey}` from the DoE manifest's `identity.repoAliases`.
        Graceful degradation: returns `{}` if the manifest does not resolve.
        Never raises.
  - `read_central_receiver_ids() -> set[str]`
        Lowercased `identity.centralReceiverIds` from the DoE manifest (via
        `read_doe_identity()`). Graceful degradation: returns `set()` on any
        read/parse failure. Never raises.
  - `read_redirect_aliases() -> set[str]`
        Lowercased, stripped `identity.redirectAliases` from the DoE manifest —
        the set of receiver ids that redirect to *self* (e.g. `.claude-em`,
        `coordinator-claude`, all redirecting to `claude-central-em`). Graceful
        degradation: returns `set()` if the manifest does not resolve, is
        unparseable, or the field is absent from a given manifest.
        Never raises. Consumed by `memo.check_addressee`'s redirect-MATCH path
        (defect-1); this module never hardcodes the alias literals themselves —
        DoE promoted `identity.redirectAliases` into the manifest 2026-07-21.
  - `read_registry_repos() -> dict[str, str]`
        `{repos.<key>: <abs-path-string>}` merged from `registry.toml` (baseline)
        + `registry.local.toml` (local overrides win). Returns `{}` when NEITHER
        file is present (legitimate "nothing configured" state — not an error).
        Raises `RegistryReadError` when a present file exists but fails to parse,
        or when `tomllib` is unavailable (Python <3.11). NO folder-scan fallback
        in either branch.
  - `convention_repo_key(receiver_em_id: str) -> str`
        Pure convention mapping: strip trailing `-em`, dashes→underscores, prefix
        `repos.`. Example: `'project-rag-em' -> 'repos.project_rag'`. Never raises.
  - `receiver_em_to_repo_key(receiver_em_id: str) -> str`
        Manifest-alias lookup, else `convention_repo_key()` fallback. NON-central
        resolution path only (see `resolve_receiver_inbox` for central fan-in).
        Never raises (aliases/manifest reads degrade gracefully per above).
  - `same_repo_path(a: Path, b: Path) -> bool`
        Cross-platform path-equality check (samefile, normcase+realpath fallback).
        THE ONE path-equality helper in this module — never raises.
  - `resolve_self_em_id(self_root: Path) -> str`
        Path-based self-identity resolution (the makima-side `em_id_for_root`
        port) — registered-repo match, else the unregistered-repo `basename +
        '-em'` convention. Best-effort/display-only; never raises.
  - `read_publish_mirror_owners() -> dict[str, str]`
        `{alias: owner_em_id}` merged from `registry.toml` (baseline) + `registry.local.toml`
        (local overrides win) `[publish.mirrors.<key>]` nested tables. Alias set per mirror
        key: the mechanically-derived pair (`<hyphenated-key>`, `<hyphenated-key>-em`) plus
        any explicit `<key>.aliases` list entries — mirrors the DoE cross-repo-memo CLI's
        `_derive_mirror_alias_set` (bin/cross-repo-memo). A mirror key with no `.owner`
        contributes no aliases (an incomplete/malformed mirror table is silently excluded,
        not fail-loud — see negative-spec). Returns `{}` when neither registry file is
        present, or on genuine parse failure of a present file (graceful degradation —
        publish-target detection is an advisory safety layer over `resolve_receiver_inbox`'s
        authoritative resolution, not itself the fail-loud registry surface). Never raises.
  - `read_publish_mirrors() -> dict[str, dict]`
        `{mirror_key: {"owner", "path", "aliases"}}` — sibling of
        `read_publish_mirror_owners()` returning the FULL per-mirror record (not just the
        alias->owner flattening) for callers needing display fields beyond ownership
        routing. Same merge (`registry.toml` + `registry.local.toml`,
        `[publish.mirrors.<key>]` nested tables) as `read_publish_mirror_owners()` — single
        TOML-merge authority, two projections. Never raises.
  - `canonical_receiver_id(receiver_em_id: str) -> str`
        Canonicalize a receiver identity to the ONE central id that actually
        has a registered `repos.*` entry on this machine, when `receiver_em_id`
        is a central id (`identity.centralReceiverIds`) or a redirect alias
        (`identity.redirectAliases`) — both fan in to the same registered
        central repo, so both canonicalize to the SAME id (today, that's
        `doe-claude-em` -> `repos.doe_claude`, never hardcoded — derived by
        the same fan-in scan `resolve_receiver_inbox` uses). For any other
        receiver id, returns the input stripped/lowercased, unchanged
        otherwise — this is the addressee-gate normalization: a memo's
        stamped `to:` should be verifiable-by-inspection as ONE name per
        seat, not whichever of several aliases the sender happened to type.
        Graceful degradation: when the manifest/sentinel is absent (both
        `read_central_receiver_ids()` and `read_redirect_aliases()` degrade
        to `set()`), `receiver_em_id` matches neither set and this is a
        pure passthrough — never a crash, never a wrong rewrite.
        Raises `RegistryReadError`/`AmbiguousReceiverError` under the exact
        same conditions `resolve_receiver_inbox` does (shared fan-in logic).
  - `resolve_receiver_inbox(receiver_em_id: str) -> tuple[Optional[Path], Optional[Path], dict[str, str]]`
        Resolve a receiver EM identity to `(inbox_dir, receiver_repo_path,
        all_repos)`:
          - `inbox_dir`: `<receiver_repo_path>/cross-repo/inbox` when resolved,
            else `None`.
          - `receiver_repo_path`: the registered repo root `Path` when resolved,
            else `None`. (Callers should use this directly rather than deriving
            it via `inbox_dir.parent.parent`.)
          - `all_repos`: the full `repos.*` dict — always populated (even on a
            zero-match `to`) for containment-check allowed-set construction.
        Resolution order: exact-normalized-name match only (case-insensitive
        strip+lower on central ids; verbatim shortname/convention mapping on
        non-central ids) — no fuzzy/near match here (that is C4's separate
        "did you mean?" layer, built ON TOP of a `None` result from this
        function, not inside it).
        Zero-match (receiver identity does not resolve to any registered
        `repos.*` key) returns `(None, None, all_repos)` — NOT an exception —
        preserving the existing call-site contract in `memo_send.py` (which
        turns the `None` into its own structured `build_setup_error_result`
        envelope; still fail-loud end-to-end, just via a return-value check
        rather than a raised exception at this layer).
        Raises `RegistryReadError` (propagated from `read_registry_repos()`) on
        genuine registry-read failure — callers MUST catch this and turn it into
        a fail-loud error envelope; they MUST NOT catch-and-fall-back to a scan.
        Raises `AmbiguousReceiverError` when a central receiver id fans in to more
        than one distinct registered repos.* key (see negative-spec above).
"""

from __future__ import annotations
import sys

import difflib
import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from coordinator_core._settings_home import machine_local_dir, normalize_native_path
from coordinator_core.doe_root_pointer import read_doe_root_pointer

_LOG = logging.getLogger(__name__)


class RegistryReadError(Exception):
    """Raised when the machine-local registry cannot be read/parsed.

    Distinct from "no registry configured" (which is `{}`, not an error) — this
    is raised only for a PRESENT registry file that fails to parse, or when
    `tomllib` itself is unavailable. Callers MUST fail loud on this (never
    fall back to a folder scan or any other implicit resolution).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AmbiguousReceiverError(Exception):
    """Raised when a central receiver id fans in to more than one registered key.

    identity.centralReceiverIds names several ids (e.g. 'central-em',
    'doe-claude-em') that are all supposed to fan in to ONE authoritative
    registry key. If more than one distinct repos.* key is registered across
    that id set, the manifest and the machine-local registry disagree about
    which repo is "central" — fail loud rather than arbitrarily pick one.
    """

    def __init__(self, receiver_em_id: str, candidate_keys):
        self.receiver_em_id = receiver_em_id
        self.candidate_keys = tuple(sorted(candidate_keys))
        super().__init__(
            f"receiver {receiver_em_id!r} is ambiguous: central-id fan-in "
            f"matched {len(self.candidate_keys)} distinct registered repos.* "
            f"keys {self.candidate_keys} — register only one, or fix the "
            f"machine-local registry/manifest disagreement."
        )


# ---------------------------------------------------------------------------
# Registry reader
# ---------------------------------------------------------------------------

_MACHINE_LOCAL_IMPL_ENV = "MACHINE_LOCAL_IMPL"


def registry_home() -> Path:
    """Return the machine-local registry directory.

    Resolves `<settings-home>/machine-local` via the C1 bootstrap-safe primitive
    (site 9, Zolí eng-director review finding #1 audit-gap) — NOT the doomed
    `~/.claude/machine-local` symlink read. `machine_local_dir()` honours
    CLAUDE_HOME/COORDINATOR_SETTINGS_HOME internally for test isolation.

    Also honours `MACHINE_LOCAL_IMPL` (the same test-isolation override ~8
    sibling `coordinator_core/ops/*.py` modules already honour via their own
    `_machine_local_impl()` helper — e.g. `queue_append.py`,
    `deliverable_rollup.py`) so a test fixture that stands up a synthetic
    settings-home for those ops also redirects this module's DIRECT tomllib
    reads to the same synthetic tree, with the ONE documented override. This
    module never shells out to `_machine_local.py` (DR-210 negative-spec:
    direct `registry.toml`/`registry.local.toml` reads via stdlib `tomllib`
    remain the sanctioned surface here) — so `MACHINE_LOCAL_IMPL` cannot be
    honoured by spawning the overridden script the way the subprocess-based
    sibling ops do. Instead it is honoured by mirroring the REAL install's
    directory convention: `<settings-home>/bin/_machine_local.py` sits
    alongside `<settings-home>/machine-local/` (the same settings-home root
    `_machine_local_impl()`'s own default and `machine_local_dir()`'s own
    default both resolve against) — so when the override points at
    `<X>/bin/_machine_local.py`, the registry directory is derived as
    `<X>/machine-local`, the sibling the real layout would put there. Prior to
    this fix, `_enumerate_publish_mirrors()`/`_enumerate_candidates()` (both
    routed through this function) never saw a test's `MACHINE_LOCAL_IMPL`
    fixture at all, so mirror/receiver rows never rendered under a mocked
    machine-local (cross-repo/inbox/2026-07-21-claude-central-em-correction-
    no-live-detector-for-double-list-plus-machine-local-impl-gap.md).

    The override is honoured ONLY when it conforms to that convention — i.e. the
    impl script actually sits in a `bin/` directory, so `<X>/bin/_machine_local.py`
    yields the sibling `<X>/machine-local`. An override pointing anywhere else
    (a bare `<tmpdir>/_mock_machine_local.py`, as several sibling fixtures use to
    redirect the SPAWN target only) carries no settings-home information: deriving
    `parent.parent` from it climbs one level too high and silently resolves the
    registry to an unrelated directory, which reads as "receiver not registered".
    In that case the settings-home resolution (`machine_local_dir()`, honouring
    CLAUDE_HOME/COORDINATOR_SETTINGS_HOME) is authoritative.

    Spec backlink: pln-repoint-coordinator-core-claud-56d805 § C2 (site 9)
    """
    override = os.environ.get(_MACHINE_LOCAL_IMPL_ENV)
    if override:
        impl_path = Path(override).resolve()
        if impl_path.parent.name == "bin":
            return impl_path.parent.parent / "machine-local"
    return machine_local_dir()


def read_doe_identity() -> dict:
    """Return the DoE registry manifest's `identity` object, or `{}`.

    THE ONE DoE-manifest read in this module — `read_receiver_aliases()`,
    `read_central_receiver_ids()` and `read_redirect_aliases()` are three
    projections over it, not three independent sentinel readers.

    DoE-root resolution delegates to `coordinator_core.doe_root_pointer.
    read_doe_root_pointer()`, the repo's canonical DR-071 ladder:
        1. registry `repos.doe_claude`                (canonical anchor)
        2. <settings-home>/machine-local/.doe-root    (durable file mirror)
        3. ${CLAUDE_HOME:-$HOME}/.claude/.doe-root    (legacy fallback)
    Each reader here previously implemented rung 3 ALONE, and implemented it
    against `<CLAUDE_HOME>/.doe-root` — a location no writer has written since
    `ops.gen_doe_root_pointer` moved the pointer to rung 2 (the tracked
    `~/.claude` meta-repo syncs between machines, so a per-machine clone path
    could not live there). The result was a reader/writer split that reported
    "repos.doe_claude not registered on this machine" from `--list-receivers`
    on a machine where delivery to that receiver worked: `is_central` was
    False for every candidate because the central-id set was empty.

    Graceful degradation: returns {} if no DoE root resolves, or the manifest
    is absent/unreadable/unparseable. Intentionally NOT fail-loud (unlike
    read_registry_repos) — the manifest is an ergonomic convenience layer, not
    the load-bearing repos.* registry itself. Never raises.
    """
    try:
        # The full DR-071 ladder, per this function's own docstring: registry
        # `repos.doe_claude`, then <settings-home>/machine-local/.doe-root,
        # then the legacy path. This read USED to be a bare `<CLAUDE_HOME>/
        # .claude/.doe-root` file read — rung 3 alone, the one rung the
        # docstring above explicitly identifies as "a location no writer has
        # written since ops.gen_doe_root_pointer moved the pointer to rung 2".
        # So it reproduced, verbatim, the reader/writer split the docstring
        # narrates as already fixed: every receiver's `is_central` came back
        # False and manifest-backed resolution silently disabled itself on a
        # machine where `repos.doe_claude` was registered and delivery worked.
        # `read_doe_root_pointer` was already imported at the top of this
        # module and simply never called.
        raw_root = read_doe_root_pointer()
        if not raw_root.strip():
            _LOG.warning(
                "_memo_resolver: no DoE root resolved (registry repos.doe_claude, "
                "<settings-home>/machine-local/.doe-root, and the legacy "
                "${CLAUDE_HOME:-$HOME}/.claude/.doe-root all came back empty) — "
                "manifest-backed receiver identity resolution disabled",
            )
            return {}
        doe_root = normalize_native_path(raw_root.strip())
        manifest_path = (
            doe_root / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
        )
        if not manifest_path.exists():
            _LOG.warning(
                "_memo_resolver: coordinator-registry.manifest.json absent at %s "
                "— manifest-backed receiver identity resolution disabled",
                manifest_path,
            )
            return {}
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        identity = manifest.get("identity", {})
        return identity if isinstance(identity, dict) else {}
    except Exception as exc:
        _LOG.warning(
            "_memo_resolver: failed to read the DoE registry manifest: %s", exc
        )
        return {}


def read_receiver_aliases() -> dict[str, str]:
    """Return {shortname: registryKey} from the DoE manifest's identity.repoAliases.

    DoE-ratified alias surface: DoE consult 2026-07-05 strang-03 follow-up, Q1.
    Manifest + doe-root resolution: `read_doe_identity()` (DR-071 ladder).

    Graceful degradation: returns {} if the manifest does not resolve — the
    convention fallback in receiver_em_to_repo_key handles non-aliased
    receivers; the alias set is small and stable.
    """
    try:
        aliases: dict[str, str] = {}
        for entry in read_doe_identity().get("repoAliases", []):
            shortname = entry.get("shortname")
            registry_key = entry.get("registryKey")
            if shortname and registry_key:
                aliases[shortname] = registry_key
        return aliases
    except Exception as exc:
        _LOG.warning(
            "_memo_resolver: failed to read receiver aliases from DoE manifest: %s", exc
        )
        return {}


def read_central_receiver_ids() -> set[str]:
    """Return the lowercased set of central receiver EM ids from the DoE manifest.

    DoE-ratified central-receiver surface: identity.centralReceiverIds in
    <doe-root>/coordinator/schemas/coordinator-registry.manifest.json (same
    manifest, same DR-071 doe-root ladder as read_receiver_aliases(), via
    read_doe_identity()).

    Graceful degradation: returns set() if the manifest does not resolve —
    mirrors read_receiver_aliases()'s degradation contract exactly (ergonomic
    convenience layer, not fail-loud).
    """
    try:
        central_ids: set[str] = set()
        for entry in read_doe_identity().get("centralReceiverIds", []):
            if isinstance(entry, str) and entry.strip():
                central_ids.add(entry.strip().lower())
        return central_ids
    except Exception as exc:
        _LOG.warning(
            "_memo_resolver: failed to read central receiver ids from DoE manifest: %s", exc
        )
        return set()


def read_redirect_aliases() -> set[str]:
    """Return the lowercased, stripped set of redirect-alias ids from the DoE manifest.

    Sibling reader of read_central_receiver_ids() — same read_doe_identity()
    ladder → graceful-degradation-to-empty structure. Reads
    identity.get("redirectAliases", []): receiver ids that
    redirect to *self* (e.g. DoE's `.claude-em` / `claude-home` /
    `coordinator-claude` / `coordinator-claude-em`, all redirecting to
    `claude-central-em`). DoE promoted `identity.redirectAliases` into the
    manifest 2026-07-21; a manifest that lacks the field (or is absent/
    unreadable) still degrades to `set()` per the graceful-degradation
    contract below.

    Graceful degradation: returns set() if the manifest does not resolve —
    mirrors read_central_receiver_ids()'s degradation contract exactly
    (ergonomic convenience layer, not fail-loud).

    Negative-spec: does NOT hardcode any alias literal — the redirect set is
    read declaratively from the manifest only, same discipline as every other
    reader in this module.
    """
    try:
        redirect_aliases: set[str] = set()
        for entry in read_doe_identity().get("redirectAliases", []):
            if isinstance(entry, str) and entry.strip():
                redirect_aliases.add(entry.strip().lower())
        return redirect_aliases
    except Exception as exc:
        _LOG.warning(
            "_memo_resolver: failed to read redirect aliases from DoE manifest: %s", exc
        )
        return set()


def read_registry_repos() -> dict[str, str]:
    """Read repos.* keys from the machine-local registry (baseline + local layer).

    DoE-ratified resolver surface (DoE consult 2026-07-05 strang-03 follow-up, Q1):
    direct registry.toml + registry.local.toml read via stdlib tomllib is the
    sanctioned surface for the engine. The coordinator_registry Python loader
    [DoE-side, DR-210:58] is explicitly NOT used here.

    Layer order: registry.toml (tracked baseline) merged with registry.local.toml
    (per-machine local overrides). Only non-empty string values are included —
    an empty string means "declared but unset on this machine" (not a hit).

    Returns {} when NEITHER registry.toml NOR registry.local.toml is present —
    a legitimate "nothing configured on this machine yet" state, not a failure.

    Raises:
        RegistryReadError: a registry file IS present but fails to parse, or
            tomllib itself is unavailable (Python <3.11). NO folder-scan
            fallback is attempted in either case — this is the fail-loud
            hardening C3/AC3 requires; the pre-C3 memo_send.py implementation
            silently swallowed both into {}, indistinguishable from "not
            configured".
    """
    try:
        import tomllib  # stdlib Python 3.11+
    except ImportError as exc:
        raise RegistryReadError(
            f"tomllib unavailable — requires Python 3.11+: {exc}"
        ) from exc

    reg_dir = registry_home()
    merged: dict[str, str] = {}
    for fname in ("registry.toml", "registry.local.toml"):
        path = reg_dir / fname
        if not path.exists():
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            raise RegistryReadError(
                f"could not parse registry file {path}: {exc}"
            ) from exc
        for key, val in data.items():
            # repos.* keys with non-empty string values are registered receivers.
            if key.startswith("repos.") and isinstance(val, str) and val:
                merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Publish-target mirror ownership — shared classification seam (C5 addition)
#
# Publish-target mirrors (OSS distribution destinations, e.g. `coordinator-claude`,
# `deep-research-claude`) are NOT EM working trees — they are outward `publish.sh`
# destinations. A memo addressed `to` a mirror is invisible to any EM and gets
# clobbered on the next publish run (mirrors DoE cross-repo-memo's
# `_is_publish_target_em` / `_get_publish_target_owners` guard, C4 2026-06-30).
# Mirrors live in the SAME `registry.toml`/`registry.local.toml` files
# `read_registry_repos()` reads, under a DISTINCT `[publish.mirrors.<key>]`
# namespace (never `repos.*` — mirrors were removed from `repos.*` by the
# 2026-06-30 registry-publish-vs-working-targets migration).
#
# Spec backlink: pln-memo-tool-rebuild-makima-owns--bd5745 § C5 (AC5)
#                 Parity source: DoE coordinator/bin/cross-repo-memo.py
#                 `_get_publish_target_owners` / `_derive_mirror_alias_set`.
# ---------------------------------------------------------------------------

def _read_merged_publish_mirrors() -> dict[str, dict]:
    """Shared TOML-merge internals for `read_publish_mirror_owners()`/`read_publish_mirrors()`.

    Reads `registry.toml` (baseline) then `registry.local.toml` (local overrides
    win), extracting the `publish.mirrors.<key>.*` namespace and merging
    per-mirror sub-dicts (not whole-table-overwriting) across the two files —
    a mirror's `owner` typically lives in the tracked baseline and its
    per-machine `path` in the local override, both contributing to the SAME
    mirror table. Returns `{mirror_key: {raw TOML sub-dict}}`, `{}` on any
    read/parse failure or when `tomllib` is unavailable (graceful degradation,
    never raises — see the two public readers' docstrings for why this layer
    is advisory rather than fail-loud). Internal — public callers use
    `read_publish_mirror_owners()` or `read_publish_mirrors()`.

    2026-08-07 incident fix: this previously read ONLY the genuine nested-TOML
    shape (`[publish.mirrors.<key>]` header, `tomllib` yielding
    `data["publish"]["mirrors"][<key>]`). But the sanctioned per-machine
    writer, `machine-local set <key> <value>` (`_machine_local.py:cmd_set` —
    the SAME tool `repos.*` entries are written with, and the tool this
    repo's own `registry.local.toml` header tells operators to use instead of
    hand-editing), writes a FLAT quoted-dotted key —
    `"publish.mirrors.<key>.<field>" = "<value>"` — never a nested table.
    `tomllib` parses that as a top-level string key, not nested structure, so
    `data.get("publish", {})` found nothing for every mirror entry an
    operator actually added via `machine-local set` (verified: this
    machine's real `registry.local.toml` has `publish.mirrors.coordinator_
    claude.path` and `publish.mirrors.claude_klabauter.{path,owner}` in
    exactly this flat form) — `read_publish_mirrors()`/
    `read_publish_mirror_owners()` silently returned `{}` for every
    machine-local mirror declaration ever made the sanctioned way, which is
    why `block_oss_mirror_memo_delivery`'s `_guarded_roots()` (built on
    `read_publish_mirrors()`) never had a root to guard, independent of the
    owner-vs-path gap fixed alongside this. Both shapes are now merged: the
    nested-table walk stays (covers direct hand-edits and every existing
    test fixture), plus a flat-key scan splitting on `publish.mirrors.` and
    the trailing `.<field>` segment.

    Intra-file precedence: within a SINGLE file, if the same field for the
    same mirror key is declared via BOTH shapes, the flat-key scan runs
    AFTER the nested-table walk (see code order below) and its `.update()`/
    assignment silently wins — the flat quoted-dotted value overrides the
    nested-table value for that field. (Cross-file precedence is separate
    and documented on the two public readers: `registry.local.toml` wins
    over `registry.toml`.)
    """
    try:
        import tomllib  # stdlib Python 3.11+
    except ImportError:
        print(f"skip: _read_merged_publish_mirrors: import tomllib  # stdlib Python 3.11+ failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}

    _FLAT_FIELDS = ("owner", "path", "aliases")
    _FLAT_PREFIX = "publish.mirrors."

    reg_dir = registry_home()
    merged_mirrors: dict[str, dict] = {}
    for fname in ("registry.toml", "registry.local.toml"):
        path = reg_dir / fname
        if not path.exists():
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            _LOG.warning(
                "_memo_resolver: failed to parse %s while reading publish mirrors: %s",
                path, exc,
            )
            continue

        # Shape 1: genuine nested TOML table (`[publish.mirrors.<key>]`).
        mirrors = data.get("publish", {})
        mirrors = mirrors.get("mirrors", {}) if isinstance(mirrors, dict) else {}
        if isinstance(mirrors, dict):
            for mirror_key, entry in mirrors.items():
                if not isinstance(entry, dict):
                    continue
                merged_mirrors.setdefault(mirror_key, {}).update(entry)

        # Shape 2: flat quoted-dotted key, the `machine-local set` output
        # format — `"publish.mirrors.<key>.<field>" = "<value>"`. Only the
        # three known per-mirror fields are recognised (matches the DoE
        # CLI's own `.owner`/`.path`/`.aliases` sentinel set); `aliases` is
        # newline-joined text here (mirrors `_machine_local_get(...)
        # .splitlines()` on the DoE CLI side), split into a list to match
        # the nested-table shape's list-of-strings contract.
        for raw_key, val in data.items():
            if not isinstance(raw_key, str) or not raw_key.startswith(_FLAT_PREFIX):
                continue
            remainder = raw_key[len(_FLAT_PREFIX):]
            for field in _FLAT_FIELDS:
                suffix = f".{field}"
                if remainder.endswith(suffix):
                    mirror_key = remainder[: -len(suffix)]
                    if not mirror_key or "." in mirror_key:
                        continue  # not a one-segment mirror key — skip
                    if field == "aliases" and isinstance(val, str):
                        val = [a.strip() for a in val.splitlines() if a.strip()]
                    merged_mirrors.setdefault(mirror_key, {})[field] = val
                    break
    return merged_mirrors


def read_publish_mirror_owners() -> dict[str, str]:
    """Return {alias: owner_em_id} for every publish-target mirror in publish.mirrors.*.

    See module docstring's Public API entry for the full contract. A mirror key
    with no `.owner` contributes no aliases (an incomplete/malformed mirror
    table is silently excluded, not fail-loud — see negative-spec).

    Alias derivation per mirror key (mirrors DoE `_derive_mirror_alias_set`):
      - Mechanically-derived pair: `<hyphenated-key>` and `<hyphenated-key>-em`
        (e.g. `coordinator_claude` -> `coordinator-claude`, `coordinator-claude-em`).
      - Plus any explicit `<key>.aliases` list entries (legacy short-forms not
        derivable from the key name, e.g. `deep-research`, `deep-research-em`
        for `deep_research_claude`).
    All aliases are lowercased/stripped before becoming dict keys.

    Graceful degradation, mirrors `read_receiver_aliases()`/`read_central_receiver_ids()`:
    returns `{}` on any read/parse failure of EITHER registry file, or when
    neither is present. This is deliberately NOT fail-loud (unlike
    `read_registry_repos()`) — publish-target detection is an advisory
    safety layer over `resolve_receiver_inbox()`'s authoritative resolution;
    a corrupt registry file already surfaces fail-loud via
    `read_registry_repos()`/`resolve_receiver_inbox()`, so this reader degrading
    silently does not mask a genuine registry defect. Never raises.

    Negative-spec: does NOT hardcode any mirror key or owner literal — both are
    read declaratively from the registry files only, same discipline as every
    other reader in this module.
    """
    merged_mirrors = _read_merged_publish_mirrors()
    owners: dict[str, str] = {}
    for mirror_key, entry in merged_mirrors.items():
        owner = entry.get("owner")
        path = entry.get("path")
        if (not owner or not isinstance(owner, str)) and not (
            path and isinstance(path, str)
        ):
            continue
        if not owner or not isinstance(owner, str):
            # 2026-08-07 incident fix: a mirror declared via `.path` alone
            # (no `.owner` set — the exact claude_klabauter registration
            # gap) must still classify as a publish target, mirroring the
            # DoE CLI's `_get_publish_target_owners` placeholder-owner fix.
            owner = (
                f"<owner unset — run: machine-local set "
                f"publish.mirrors.{mirror_key}.owner <em-id>>"
            )
        hyphenated = mirror_key.replace("_", "-").lower()
        aliases = {hyphenated, f"{hyphenated}-em"}
        extra_aliases = entry.get("aliases")
        if isinstance(extra_aliases, list):
            for alias in extra_aliases:
                if isinstance(alias, str) and alias.strip():
                    aliases.add(alias.strip().lower())
        for alias in aliases:
            owners[alias] = owner
    return owners


def _path_is_within(candidate: Path, root: Path) -> bool:
    """True if ``candidate`` resolves to ``root`` itself or a path NESTED inside it.

    2026-08-07 nested-subdirectory hardening: `publish_mirror_path_match()`
    previously used `same_repo_path()` alone, which is exact-path-equality
    only — a `repos.<key>` receiver registered at a path INSIDE a declared
    mirror root (rather than equal to it) compared unequal to the mirror
    root and the cross-check silently passed, the original incident shape
    one path segment removed. This adds containment as a second layer.

    Compares RESOLVED path parts (`Path.resolve().parts`), never raw
    strings — a bare `str.startswith`/`in` check would wrongly match a
    sibling directory whose name merely shares a prefix (e.g.
    `/dev/claude-klabauter-notes` vs `/dev/claude-klabauter`, where the
    former is NOT inside the latter). Comparing tuples of path parts avoids
    that false positive because `("dev", "claude-klabauter-notes")` is not
    a superset-prefix of `("dev", "claude-klabauter")`. `.resolve()` is
    used unconditionally (not `same_repo_path`'s samefile-first strategy)
    because containment is a structural relationship between two paths,
    not an existence-sensitive identity check — both paths may be
    not-yet-cloned registry entries, and resolution still normalizes
    separators/case/`..`/symlinks consistently for a pure parts comparison.
    Never raises — any resolution failure returns False (fail-safe: no
    false containment claim on a malformed path).
    """
    try:
        candidate_parts = os.path.normcase(str(candidate.resolve())).split(os.sep)
        root_parts = os.path.normcase(str(root.resolve())).split(os.sep)
    except Exception:
        return False
    # Strip trailing empty segments from a trailing separator, if any.
    candidate_parts = [p for p in candidate_parts if p != ""] or candidate_parts
    root_parts = [p for p in root_parts if p != ""] or root_parts
    if len(root_parts) > len(candidate_parts):
        return False
    return candidate_parts[: len(root_parts)] == root_parts


def publish_mirror_path_match(candidate: Path) -> Optional[str]:
    """Return the mirror key whose declared ``.path`` matches ``candidate``, or None.

    2026-08-07 incident fix: a repo can be double-registered — as an ordinary
    ``repos.<key>`` receiver AND (separately, sometimes incompletely) as a
    ``publish.mirrors.<key>`` entry. `read_publish_mirror_owners()` only
    recognises a mirror once its `.owner` sentinel is set, so a mirror
    declared with `.path` alone (or a `repos.*` entry that happens to point
    at a path some `publish.mirrors.*` table also names) was invisible to
    every owner-gated publish-target check — `resolve_receiver_inbox()`
    happily resolved the receiver via `repos.*` and a memo landed in a
    published OSS mirror's `cross-repo/inbox/` uncaught
    (`claude-klabauter`, 2026-08-07: `repos.claude_klabauter` was registered
    as an ordinary receiver while `publish.mirrors.claude_klabauter.owner`
    was never set — only `.path` was).

    This check is deliberately PATH-based and OWNER-INDEPENDENT — it fires
    on `.path` alone, the one field a mirror declaration cannot omit and
    still be useful (an ownerless mirror is still a publish destination; a
    pathless one guards nothing). Callers needing the owner-attribution
    for a rejection message should follow up with
    `read_publish_mirrors()[key]["owner"]` (may be `None`).

    Uses `same_repo_path()` (samefile, else normcase+realpath fallback) —
    the one path-equality helper this module already standardises on, so a
    ``repos.*`` value and a ``publish.mirrors.*.path`` value written with
    different casing/separators/trailing-slash still compare equal.

    Second layer (2026-08-07 nested-subdirectory hardening): when a
    mirror's declared `.path` does not exact-match `candidate`, also checks
    `_path_is_within(candidate, mirror_root)` — a receiver registered at a
    path NESTED inside a declared mirror clone (not the mirror root itself)
    still resolves to this mirror key. See `_path_is_within()` for why this
    is a parts-comparison, not a string-prefix check (avoids the
    `claude-klabauter` vs `claude-klabauter-notes` sibling-prefix false
    positive).

    Returns the FIRST matching mirror key (deterministic: `dict` insertion
    order from `_read_merged_publish_mirrors()`, itself deterministic per
    TOML-file read order), or `None` if no mirror declares this path, or on
    any read/parse failure (graceful degradation — never raises).
    """
    try:
        mirrors = read_publish_mirrors()
    except Exception:
        return None
    for mirror_key, entry in mirrors.items():
        path = entry.get("path") if isinstance(entry, dict) else None
        if not path or not isinstance(path, str):
            continue
        try:
            mirror_root = Path(path)
            if same_repo_path(candidate, mirror_root) or _path_is_within(
                candidate, mirror_root
            ):
                return mirror_key
        except Exception:
            continue
    return None


def read_publish_mirrors() -> dict[str, dict]:
    """Return {mirror_key: {"owner", "path", "aliases"}} for every declared mirror.

    Sibling of `read_publish_mirror_owners()` returning the FULL per-mirror
    record (not just the alias->owner flattening) for callers that need display
    fields — path, explicit aliases — beyond ownership routing. Consumer:
    `memo.list`'s enumeration-mode candidate listing
    (`_enumerate_publish_mirrors` in `coordinator_core/ops/fleet/memo_list.py`,
    Finding 2 of the 2026-07-21 memo-tool-rebuild review — `memo.list` must not
    re-implement its own TOML merge; this is the single authority both readers
    share). Shares the SAME merge (`_read_merged_publish_mirrors`) as
    `read_publish_mirror_owners()` — single TOML-merge authority, two
    projections.

    Unlike `read_publish_mirror_owners()`, a mirror key with no `.owner` IS
    still included here (`owner: None`) — a display/enumeration listing wants
    to surface an incomplete/malformed mirror table to a human, whereas the
    routing reader correctly excludes ownerless entries (nothing to route to).

    `aliases` here is only the EXPLICIT `<key>.aliases` TOML list (lowercased/
    stripped, sorted) — the mechanically-derived `<hyphenated-key>`/
    `<hyphenated-key>-em` pair is NOT included (that derivation is the
    caller's job, same division of labor `read_publish_mirror_owners()` uses
    internally, just not pre-merged into the return value here since a
    display caller may want to distinguish "explicit" from "derived" aliases).

    Graceful degradation: returns `{}` on any read/parse failure of either
    registry file, or when neither is present. Never raises.
    """
    merged_mirrors = _read_merged_publish_mirrors()
    result: dict[str, dict] = {}
    for mirror_key, entry in merged_mirrors.items():
        owner = entry.get("owner")
        owner = owner if isinstance(owner, str) and owner else None
        path = entry.get("path")
        path = path if isinstance(path, str) and path else None
        aliases_raw = entry.get("aliases")
        aliases = sorted({
            a.strip().lower() for a in aliases_raw if isinstance(a, str) and a.strip()
        }) if isinstance(aliases_raw, list) else []
        result[mirror_key] = {"owner": owner, "path": path, "aliases": aliases}
    return result


# ---------------------------------------------------------------------------
# Receiver identity → inbox resolution seam
# ---------------------------------------------------------------------------

def convention_repo_key(receiver_em_id: str) -> str:
    """Pure convention mapping: strip trailing '-em', dashes→underscores, prefix 'repos.'.

    Example: 'project-rag-em' → 'repos.project_rag'.

    Shared by receiver_em_to_repo_key's convention-fallback branch and the
    central-receiver resolution path in resolve_receiver_inbox — factored out
    so both call sites use the identical mapping rule (no divergent literal).
    """
    shortname = receiver_em_id[:-3] if receiver_em_id.endswith("-em") else receiver_em_id
    return "repos." + shortname.replace("-", "_")


def receiver_em_to_repo_key(receiver_em_id: str) -> str:
    """Map a receiver EM identity to its machine-local repos.* registry key.

    Resolution order:
    1. Manifest alias lookup: identity.repoAliases in coordinator-registry.manifest.json
       maps shortname → registryKey (e.g. holodeck → claude_unreal_holodeck).
    2. Convention fallback: strip trailing '-em', dashes→underscores, prefix 'repos.'.
       Example: 'project-rag-em' → 'repos.project_rag'.

    Central receiver IDs (identity.centralReceiverIds in the manifest, e.g. 'central-em',
    'doe-claude-em') are NOT resolved through this function's convention path — they
    fan-in to a single authoritative registry key via read_central_receiver_ids() +
    the central-resolution branch in resolve_receiver_inbox (multiple aliases, one
    registered repo). This function remains the non-central resolution path.

    DoE-ratified alias surface: DoE consult 2026-07-05 strang-03 follow-up, Q1.
    """
    shortname = receiver_em_id[:-3] if receiver_em_id.endswith("-em") else receiver_em_id
    # Review: code-reviewer — removed unreachable `if not shortname` guard; _validate_send_params
    # rejects empty `to` before this point, so the "-em"-alone → empty-shortname path is
    # unreachable from the handler. Caller gets repos. (missing suffix), correctly fails lookup.

    # Manifest alias lookup (DoE-ratified; alias set is small and stable).
    aliases = read_receiver_aliases()
    if shortname in aliases:
        return "repos." + aliases[shortname]

    # Convention fallback: dashes → underscores.
    return convention_repo_key(receiver_em_id)


def _central_fan_in_matches(
    central_ids: set[str], all_repos: dict[str, str]
) -> dict[str, str]:
    """Shared central-id fan-in scan: which registered repos.* key(s) does the
    manifest's centralReceiverIds set resolve to?

    Factored out of `resolve_receiver_inbox` so `canonical_receiver_id` reuses
    the IDENTICAL derivation rather than a second copy that could silently
    diverge (both must agree on which id is "the" canonical central receiver).

    Returns `{candidate_key: the central id that produced it}`, iterating
    `central_ids` in sorted order for determinism. More than one distinct
    `candidate_key` means the manifest and the machine-local registry
    disagree about which repo is central — callers raise
    `AmbiguousReceiverError` on `len(...) > 1`, they do not resolve this here.
    """
    aliases = read_receiver_aliases()
    matched_keys: dict[str, str] = {}
    for cid in sorted(central_ids):
        shortname = cid[:-3] if cid.endswith("-em") else cid
        candidate_key = (
            "repos." + aliases[shortname] if shortname in aliases
            else convention_repo_key(cid)
        )
        if candidate_key in all_repos:
            matched_keys.setdefault(candidate_key, cid)
    return matched_keys


def canonical_receiver_id(receiver_em_id: str) -> str:
    """Canonicalize a receiver identity to the repo-matching central id, or pass through.

    See module docstring's Public API entry for the full contract. Existence of
    this function answers the addressee-gate verifiability problem: a receiver
    seat is addressable via several aliases (`claude-central-em`, `central-em`,
    `central`, `doe-claude-em`, plus redirect aliases like `coordinator-claude`)
    that all fan in to ONE registered repo — without canonicalization, a memo's
    stamped `to:` echoes whichever alias the sender typed, and a reader cannot
    verify by inspection that two differently-addressed memos went to the same
    seat.

    Negative-spec: does NOT hardcode any alias/central-id literal — the
    central-id and redirect-alias sets are read declaratively from the DoE
    manifest (`read_central_receiver_ids()`, `read_redirect_aliases()`), same
    discipline as every other reader in this module.
    """
    normalized = receiver_em_id.strip().lower()
    central_ids = read_central_receiver_ids()
    redirect_aliases = read_redirect_aliases()
    if normalized not in central_ids and normalized not in redirect_aliases:
        return normalized
    all_repos = read_registry_repos()  # RegistryReadError propagates, fail-loud
    matched_keys = _central_fan_in_matches(central_ids, all_repos)
    if len(matched_keys) > 1:
        raise AmbiguousReceiverError(receiver_em_id, matched_keys.keys())
    repo_key = next(iter(matched_keys), None)
    if repo_key is None:
        # Central/redirect alias, but no central repo registered anywhere yet —
        # nothing to canonicalize TO. Passthrough; resolve_receiver_inbox is
        # the authority that turns this into a fail-loud setup error at the
        # point a send/list actually needs the registered inbox.
        return normalized
    return _repo_key_to_receiver_em_id(repo_key)


def resolve_receiver_inbox(
    receiver_em_id: str,
) -> Tuple[Optional[Path], Optional[Path], dict[str, str]]:
    """Resolve receiver EM identity → (inbox_dir, receiver_repo_path, all_repos_registry).

    Returns:
        (inbox_dir, receiver_repo_path, all_repos) where inbox_dir is the
        cross-repo/inbox/ Path for the receiver, receiver_repo_path is the
        registered repo root Path (avoids implicit .parent.parent navigation
        at the call site), and all_repos is the full repos.* dict for the
        containment allowed-set. inbox_dir and receiver_repo_path are None
        when the receiver is not registered (zero-match; NOT an exception —
        see module docstring).

    Central receivers (receiver_em_id in identity.centralReceiverIds, e.g.
    'claude-central-em', 'central-em', 'central', 'doe-claude-em') fan in to a
    single authoritative registry key: every central id maps through the same
    convention/alias rules any other receiver would use, but only ONE of those
    ids is expected to have a registered repos.* entry on a given machine
    (today, that's 'doe-claude-em' → repos.doe_claude — never hardcoded here,
    always derived by scanning the manifest's central-id set, in sorted order,
    against the registered repos). This mirrors the DoE cross-repo-memo CLI's
    "central is not a repos.* key — anchored on repos.doe_claude" special-case
    without importing DoE's coordinator_registry or hardcoding the literal key.

    Raises:
        RegistryReadError: propagated from read_registry_repos() on genuine
            registry-read failure. Callers MUST fail loud on this — never
            fall back to a folder scan or any other implicit resolution.
        AmbiguousReceiverError: a central receiver id fans in to more than one
            DISTINCT registered repos.* key (manifest/registry disagreement).
    """
    all_repos = read_registry_repos()
    central_ids = read_central_receiver_ids()
    normalized_id = receiver_em_id.strip().lower()
    if normalized_id in central_ids:
        matched_keys = _central_fan_in_matches(central_ids, all_repos)
        if len(matched_keys) > 1:
            raise AmbiguousReceiverError(receiver_em_id, matched_keys.keys())
        repo_key = next(iter(matched_keys), None)
    else:
        repo_key = receiver_em_to_repo_key(receiver_em_id)
    repo_path_str = all_repos.get(repo_key) if repo_key else None
    if not repo_path_str:
        return None, None, all_repos
    receiver_repo_path = Path(repo_path_str)
    inbox_dir = receiver_repo_path / "cross-repo" / "inbox"
    return inbox_dir, receiver_repo_path, all_repos


# ---------------------------------------------------------------------------
# Nearest-match "did you mean?" suggestion (C4, footgun #2)
# ---------------------------------------------------------------------------

def same_repo_path(a: Path, b: Path) -> bool:
    """True if two paths resolve to the same directory (cross-platform).

    `samefile` when both exist; `normcase`+`realpath` fallback so an absent
    repo (a registry entry pointing at a not-yet-cloned sibling) never
    raises. Mirrors DoE CLI's `_same_path` helper (`bin/cross-repo-memo`).

    THE ONE path-equality helper for receiver/self resolution in
    `coordinator_core` — `memo_check_addressee.py` imports this rather than
    carrying its own copy (2026-07-26 subprocess-elision spinoff).

    DRIFT SEAM — a third copy exists that CANNOT import this one:
    `_same_repo_path` in DoE-claude's `coordinator/hooks/scripts/_engine_root.py`.
    That module bootstraps engine resolution, so importing `coordinator_core`
    would close a cycle; it reimplements these exact semantics by necessity.
    Changing the semantics here (not the implementation — the ANSWER for some
    pair of paths) silently desynchronizes the engine-working-repo gate that
    reads it, and no test on either side catches the divergence. Makima owns
    this note; a semantics change here ships with a memo to `doe-claude-em`.
    """
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(
            os.path.realpath(str(b))
        )


def resolve_self_em_id(self_root: Path) -> str:
    """Resolve a repo root path to its OWN EM identity string — the makima-
    side port of the DoE CLI's `_sender_em_id()` / `em_id_for_root()`
    (`bin/cross-repo-memo:1204-1220`, `bin/lib/coordinator_registry.py:
    263-284`).

    Path-matches `self_root` against every registered `repos.*` entry
    (central included — a repo registered under `repos.doe_claude` resolves
    to `_repo_key_to_self_em_id('repos.doe_claude')`, i.e.
    `'doe-claude-em'` today, the SAME id `em_id_for_root`'s dedicated
    central-canonical branch produces, without a second special case here).
    Falls back to the unregistered-repo convention
    (`basename(self_root) + '-em'`) when no registered path matches, or when
    the registry cannot be read at all — self-identity derivation is
    best-effort/display-only (mirrors the DoE CLI's own "self_em is
    best-effort/display-only" comment at the `--check-addressee` call site)
    and must never raise.

    THE ONE self-identity resolver in `coordinator_core` — every in-process
    caller that needs "what is this repo's own EM id" (the addressee gate's
    `self:` display line, the reply-closure sender-id derivation) calls this
    rather than each hand-rolling the `basename + '-em'` convention;
    do not paste a second copy.

    Uses `_repo_key_to_self_em_id` (alias-aware), NOT the lossy
    `_repo_key_to_receiver_em_id` suggestion helper below — a matched repo
    key that requires `REPO_ALIASES` translation (e.g.
    `claude_unreal_holodeck` -> `holodeck` -> `holodeck-em`) must resolve to
    the SAME id the DoE CLI's `repo_key_to_em_id` produces for the same key,
    or `compute_reply_closure`'s sender-id match silently breaks for every
    aliased repo (2026-07-26 review finding 1).
    """
    basename_fallback = os.path.basename(str(self_root).rstrip("/\\")) + "-em"
    try:
        all_repos = read_registry_repos()
    except RegistryReadError:
        return basename_fallback
    for repo_key, path_str in all_repos.items():
        if same_repo_path(self_root, Path(path_str)):
            return _repo_key_to_self_em_id(repo_key)
    return basename_fallback


def _repo_key_to_self_em_id(repo_key: str) -> str:
    """Alias-aware inverse of the registry key -> EM id mapping, for
    `resolve_self_em_id` ONLY — the load-bearing self-identity form.

    Mirrors the DoE CLI's `repo_key_to_em_id`
    (`bin/lib/coordinator_registry.py:235-260`): checks
    `read_receiver_aliases()`'s reverse mapping (registry-key suffix ->
    alias shortname) before falling back to the naive underscore->dash
    convention, so an aliased repo (e.g. `claude_unreal_holodeck` ->
    `holodeck` -> `holodeck-em`) resolves to the SAME id the CLI produces,
    not just the un-aliased convention form.

    Distinct on purpose from `_repo_key_to_receiver_em_id` below, which is
    deliberately lossy and reserved for "did you mean?" suggestion text —
    do not reuse that helper here, and do not reuse this one there (it does
    an extra alias-manifest read this fast, spam-prone suggestion path
    doesn't need).
    """
    suffix = repo_key[len("repos."):] if repo_key.startswith("repos.") else repo_key
    aliases = read_receiver_aliases()  # {shortname: registryKey-suffix}
    reverse_aliases = {registry_key: shortname for shortname, registry_key in aliases.items()}
    canonical = reverse_aliases.get(suffix)
    if canonical is not None:
        return canonical + "-em"
    return suffix.replace("_", "-") + "-em"


def _repo_key_to_receiver_em_id(repo_key: str) -> str:
    """Inverse of `convention_repo_key`: 'repos.project_makima' -> 'project-makima-em'.

    Best-effort/lossy — the forward mapping (alias lookup, then convention) is
    not perfectly invertible when an alias was used, so this only reconstructs
    the CONVENTION form. That is sufficient for a "did you mean?" suggestion
    (which never resolves, only prints a candidate string) even when the
    receiver was originally registered via an alias.

    Not alias-aware on purpose — do NOT use this for `resolve_self_em_id`'s
    load-bearing self-identity derivation; that is `_repo_key_to_self_em_id`
    above (2026-07-26 review finding 1: a load-bearing caller was reusing
    this lossy helper and silently diverging from the CLI for aliased repos).
    """
    suffix = repo_key[len("repos."):] if repo_key.startswith("repos.") else repo_key
    return suffix.replace("_", "-") + "-em"


def _nearest_receiver_matches(
    receiver_em_id: str, all_repos: dict[str, str], n: int
) -> list[str]:
    """Shared candidate-pool + fuzzy-match internals for the did-you-mean surfaces below.

    Candidate pool: every currently-registered `repos.*` key (converted back to
    its conventional receiver-em-id form), plus every DoE-manifest alias
    shortname whose aliased registry key is itself currently registered. Only
    receivers actually present in `all_repos` are ever candidates — this never
    suggests an id that would ALSO fail to resolve.

    Returns up to `n` closest candidates (case-preserved, best-first), or `[]`
    if nothing is within a reasonable edit-distance similarity threshold
    (cutoff 0.5) or the registry has no candidates at all.

    Review: code-reviewer — callers detecting uniqueness (`unique_nearest_receiver`)
    rely on calling this with `n=2` and treating a returned length of exactly 2 as
    "ambiguous" (2+ candidates cleared the cutoff) vs exactly 1 as "unique" — i.e.
    on `difflib.get_close_matches(..., n=2, ...)` returning exactly 2 entries iff
    2 or more candidates clear the cutoff. Do not change this truncation behavior
    (e.g. a "soft cap" that can return fewer entries even when more exist, or a
    different tie-break) without updating that caller's ambiguity gate.
    """
    candidates: set[str] = set()
    for repo_key in all_repos:
        candidates.add(_repo_key_to_receiver_em_id(repo_key))

    aliases = read_receiver_aliases()
    for shortname, registry_key in aliases.items():
        if ("repos." + registry_key) in all_repos:
            candidates.add(shortname if shortname.endswith("-em") else shortname + "-em")

    if not candidates:
        return []

    normalized = receiver_em_id.strip().lower()
    lowered_to_original = {c.lower(): c for c in candidates}
    matches = difflib.get_close_matches(
        normalized, list(lowered_to_original.keys()), n=n, cutoff=0.5
    )
    return [lowered_to_original[m] for m in matches]


def suggest_nearest_receiver(
    receiver_em_id: str, all_repos: dict[str, str]
) -> Optional[str]:
    """Suggest the closest REGISTERED receiver id to an unresolved `receiver_em_id`.

    Design-as-offers (footgun #2): when `resolve_receiver_inbox()` returns a
    zero-match, this gives the caller a candidate to print alongside the
    fail-loud error — e.g. 'makima-em' -> suggests 'project-makima-em'.

    This is a SUGGESTION SURFACE ONLY. It must never be used to auto-select a
    receiver or to resolve/deliver a memo — it is purely advisory text for a
    human to read and re-issue the command with the corrected `--to`. Callers
    MUST continue to treat the original resolution as failed regardless of
    whether a suggestion is found. (Callers that want to auto-accept an
    UNAMBIGUOUS suggestion use `unique_nearest_receiver()` instead — see its
    docstring for why that is a distinct, narrower guarantee than this
    function provides.)

    Returns:
        The single closest candidate (case-preserved), or `None` if nothing is
        within a reasonable edit-distance similarity threshold (cutoff 0.5) or
        the registry has no candidates at all.
    """
    matches = _nearest_receiver_matches(receiver_em_id, all_repos, n=1)
    return matches[0] if matches else None


def unique_nearest_receiver(
    receiver_em_id: str, all_repos: dict[str, str]
) -> Optional[str]:
    """Return the sole did-you-mean candidate, but ONLY when it is unambiguous.

    2026-07-24 papercut fix (sibling-EM report): `cross-repo-memo draft --to
    makima-em` hard-failed with UNKNOWN RECEIVER and printed "Did you mean
    'project-makima-em'?" — forcing a manual retype of a suggestion the CLI
    was already confident enough to compute. Unlike `suggest_nearest_receiver`
    (advisory-only, never auto-selected — see its docstring), this function is
    the auto-accept surface: callers MAY treat its return value as the
    resolved receiver id, but ONLY when it returns non-None, which happens
    ONLY when EXACTLY ONE registered candidate clears the similarity cutoff.
    Two or more candidates within cutoff (a genuine ambiguity — e.g. a typo
    equidistant between two registered repos) returns `None` here even though
    `suggest_nearest_receiver` would still offer its top-ranked pick; auto-
    accept requires uniqueness, not just a best-of ranking.

    Returns:
        The single unambiguous candidate (case-preserved), or `None` when zero
        or multiple candidates clear the cutoff.
    """
    matches = _nearest_receiver_matches(receiver_em_id, all_repos, n=2)
    return matches[0] if len(matches) == 1 else None

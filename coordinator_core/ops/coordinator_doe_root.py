"""
coordinator_core.ops.coordinator_doe_root — Port of:
coordinator-doe-root.sh (DoE 09e5e5f9, 2026-07-19, sourced-lib shape, DOE-PORT R2-R6 wave).

Purpose: resolves the DoE-claude sibling-repo root, analogous to how CLAUDE_KLABAUTER_ROOT
works for the claude-klabauter plane. The bash oracle exposes a single public shell function,
`coordinator_doe_root`, meant to be `source`d by other bash scripts. This module
provides the same resolution chain as a plain Python function so any coordinator_core
caller can import it directly without shelling back out to bash.

NOTE — the DoE-side `.sh` is a SOURCED LIB, not an executable: nothing can `source` a
`.py` file, so `coordinator-doe-root.sh` is left UNCHANGED by this port (its bash
callers keep sourcing it as-is). This module is authored so that a *future* Python
caller (or a bash→Python cutover of one of its callers, e.g.
`coordinator-state-root.sh`) has a drop-in equivalent to import instead of source.

Resolution chain (mirrors the bash oracle's header comment verbatim, rung-for-rung):
  1. REPO_DOE_CLAUDE env var — if already set (non-empty), return it unchanged.
  2. `machine-local get repos.doe_claude` (CANONICAL, DR-071) — the machine-local
     registry's own four-rung discovery ladder runs inside that CLI; not
     reimplemented here.
  2.5. Fallback: `machine-local get plugin.mirrors.coordinator-claude.live_path`
     — only fires when rung 2 (repos.doe_claude) returned nothing. Covers a machine
     that registered the coordinator-specific mirror key but never set the general
     repos.doe_claude key.
  2.75. Codename-free rung ladder (added 2026-08-07, C1B; reordered below rungs
     2/2.5 by the B2 review fix, 2026-08-08 — DR-071 ratifies the registry
     `repos.doe_claude` key as the canonical, authoritative anchor and explicitly
     demotes `.doe-root`; this ladder must not let a stale marketplace install or
     pointer file outrank that registry entry on a private dev box). Only fires
     when rungs 2 and 2.5 both returned nothing. On the published mirror, the
     registry never carries `repos.doe_claude` nor its scrubbed successor
     `repos.example_doctrine_repo`, so on a genuine OSS box rungs 2/2.5 are always
     dead and this ladder remains fully load-bearing at its new position (executed
     proof: `coordinator_doe_root()` returns `None` from the mirror with registry
     reachability removed — see
     `tasks/2026-08-07-published-engine-codename/briefs/C1B.md`). None of these
     candidates carry a private codename, so the depersonalize scrub cannot touch
     them:
       a. `<settings-home>/machine-local/.doe-root`, then
          `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` — via
          `coordinator_core.doe_root_pointer.read_doe_root_pointer_file()` (the
          file-rungs-only sibling of `read_doe_root_pointer()`, which additionally
          has a registry-first rung this ladder does not want spliced in ahead of
          its own registry rung below).
       a.5. Claude Code's REAL marketplace-install location (F1 fix,
          2026-08-08),
          `<claude_home>/plugins/cache/coordinator-claude/coordinator/<version>/`,
          newest version wins — via `_cf_marketplace_cache_rung()`. Ported
          from the two bin/-side twins
          (`coordinator_registry.py::_mp_marketplace_cache_rung`,
          `coordinator/bin/lib/coordinator_data_root.py::_cdr_marketplace_cache_rung`),
          which had it while this engine-plane copy did not.
       b. the flat `~/.claude/plugins/coordinator-claude` marketplace-clone layout,
          gated on the `.claude-plugin/plugin.json` marker.
       c. `CLAUDE_PLUGIN_ROOT` env var, normalized via
          `_cf_repo_root_from_plugin_root_candidate()` (C1E fix) — the raw
          env value is a *content* root, one level below the repo root the
          private/dev layout expects; see that helper's docstring.
       d. `plugin.mirrors.coordinator-claude.live_path` (registry) — same call as
          rung 2.5 above, just tried again here as this sub-ladder's last resort.
     Each candidate is accepted only if it is a directory AND contains one of the
     two published manifest layouts (OSS flat `schemas/...`, private
     `coordinator/schemas/...`) — see `_cf_manifest_present`.
  3. Rung 3 fallback: calls the native `coordinator_core.resolve_coordinator_clone`
     port (`resolve_clone_root()`) — the shared unified resolver lib, ported native
     in a prior C11-core wave. Covers the `.doe-root` pointer-file rung and the
     flat-layout rung. Only fires when rungs 2, 2.5, and 2.75 all returned nothing.
     PORT NOTE (2026-07-21, C11): this rung previously shelled out to
     `bash coordinator/lib/resolve-coordinator-clone.sh --clone-root`; that bash
     bridge is now retired in favor of the in-process native call — same
     best-effort/fold-to-None-on-failure contract, no subprocess spawn.
  4. Hard error: returns None, remediation message written to stderr by the caller
     (see `main()` below for the CLI-shaped stderr contract, faithfully reproduced).

Pure resolver — does NOT mutate `os.environ` (REVERSED 2026-07-21; see below).
Rung 1 still READS `REPO_DOE_CLAUDE` as an operator override; nothing here writes it.

DECISION REVERSAL (2026-07-21) — this module previously exported
`os.environ["REPO_DOE_CLAUDE"]` on rungs 2/2.5/3, mirroring the bash oracle's
`export`, and its docstring defended that as deliberate bash parity against
`coordinator_core.claude_klabauter_root`'s opposite choice. That parity argument was wrong,
and the asymmetry is now retired: an `export` in a spawn-per-call shell script dies
with the process, whereas the same write from an IMPORTED Python module persists for
the life of the interpreter. Under pytest the first test to resolve pinned the value
for the whole session (the rung-1 guard at the top of `coordinator_doe_root()` made
every later resolution a no-op), and the write leaked into the `os.environ.copy()`
handed to every `subprocess.run` child. Same defect class as the
`COORDINATOR_SETTINGS_HOME` leak fixed in `cab185fa`
(`probe_cwd_example_retrieval_repo_relevance`); this module was the largest-blast-radius
instance (9 importers).

No caller depended on the export: every importer (`state_root`,
`bash_guards.commit_tripwires`, `ops.coordinator_complete_entry`,
`ops.verify_parallel_review_lens_orthogonality`) consumes the RETURN VALUE. The
`export`'s only load-bearing effect was same-process re-resolution avoidance, which
is now carried explicitly by a module-scope memo plus a `_reset_doe_root_cache()`
test seam (mirroring `coordinator_core.liveness._reset_live_ids_cache`) — the
idempotency without the interpreter-global side effect. Callers that genuinely need
`REPO_DOE_CLAUDE` in a CHILD process's environment pass it explicitly via `env=`
(as `install.maximalist` and `install.sandbox_check` already do).

Review: code-reviewer — `coordinator_core.claude_klabauter_root`'s negative-spec cites this
module as the deliberately-asymmetric counter-example. That cross-reference is now
STALE: both resolvers are pure. The two modules agree; claude_klabauter_root's note should be
updated when that file is next touched.

Spec backlink: docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.1 [DEAD-CITATION: plan file never committed to this repo]
             + docs/plans/2026-07-09-resolver-unification-v3split-01.md § C3
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Negative-spec (faithfully reproduced from the bash oracle -- do NOT "fix" mid-port):
    - Rung 2 folds every registry-read failure (key absent, unreadable/missing
      registry file) into "rung 2 failed, fall through" -- no distinct
      "operational failure" vs "missing key" branch survives the port (the bash
      oracle's own header comment claims that distinction exists via rc=1 vs rc=2,
      but the implementation never actually branched on that beyond presence, so
      there is nothing differentiated to preserve).
    - Rung 2.5 and rung 3 are UNCONDITIONAL best-effort: any resolution failure
      degrades to empty string and falls through to the next rung, matching the
      oracle's `|| _resolved_fallback=""` / `|| true` shape.
    - Rungs 2 and 2.5 read the machine-local registry directly, in-process, via
      `machine_resolver.registry_get` (converted 2026-08-16, C7b) -- no
      `machine-local` CLI subprocess or PATH lookup; see `_machine_local_get`'s
      own docstring.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from coordinator_core import machine_resolver as _machine_resolver
from coordinator_core import resolve_coordinator_clone as _resolve_coordinator_clone
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file as _cf_read_doe_root_pointer_file

# Published-manifest relpath (OSS flat layout). The private DoE-repo layout
# nests the same relpath under `coordinator/`. Shared by the codename-free
# rung ladder's acceptance gate below.
_CF_MANIFEST_RELPATH = os.path.join("schemas", "coordinator-registry.manifest.json")


def _machine_local_get(key: str) -> Optional[str]:
    """Resolve `key` via the direct-registry reader
    (`machine_resolver.registry_get`) -- no `machine-local` CLI subprocess.

    Converted 2026-08-16 (C7b): the module's own test suite
    (`coordinator_core/ops/test_coordinator_doe_root.py`) now seeds the
    machine-local registry FILE (`MACHINE_LOCAL_REGISTRY_DIR` + a scratch
    `registry.toml`) instead of faking the CLI as a real subprocess-invoked
    stub, so this in-process conversion no longer silently stops exercising
    those fakes. Registry-not-found (missing key, unreadable/missing file)
    degrades to None -- same "no signal" contract the subprocess shape had
    for a missing binary, non-zero exit, or empty stdout."""
    return _machine_resolver.registry_get(key)


def _cf_manifest_present(root: str) -> bool:
    """True if `root` contains either published manifest layout: OSS flat
    (`<root>/schemas/coordinator-registry.manifest.json`) or the private
    DoE-repo shape (`<root>/coordinator/schemas/coordinator-registry.manifest.json`).
    Gates each codename-free rung so a resolved-but-empty/unrelated directory
    is not accepted over a later, correct rung.

    LIMITATION (C1E): probing both layouts here is deliberately
    level-ambiguous by itself -- a CONTENT root (`<repo_root>/coordinator`)
    satisfies the OSS-flat relpath (`<content_root>/schemas/...`) exactly as
    readily as a genuine repo root satisfies the private relpath. This gate
    cannot and does not disambiguate levels; it only confirms "some published
    manifest lives under this directory." Every candidate reaching this gate
    MUST already be repo-root-shaped BEFORE it gets here -- the pointer-file,
    flat-layout, and live_path rungs already are (see module docstring rung
    1.5 a/b/d); `CLAUDE_PLUGIN_ROOT` (rung c) is normalized to repo-root shape
    by `_cf_repo_root_from_plugin_root_candidate()` immediately before being
    passed in here, precisely so this gate never has to arbitrate the level
    question it structurally cannot answer. Do not add a new rung 1.5
    candidate without doing the same normalization first.
    """
    for relpath in (_CF_MANIFEST_RELPATH, os.path.join("coordinator", _CF_MANIFEST_RELPATH)):
        if os.path.exists(os.path.join(root, relpath)):
            return True
    return False


def repo_root_from_plugin_root_candidate(
    candidate: str,
    *,
    drive_root_guard: str = "preserve",
    basename_compare: str = "normcase",
    manifest_relpath_fallback: bool = True,
    allow_unchanged_fallback: bool = True,
) -> str:
    """Normalize a CLAUDE_PLUGIN_ROOT-shaped value to the coordinator REPO
    root a caller expects.

    Single-sourced (state/debt-backlog/2026-08-08-three-divergent-copies-of-
    the-plugin-roo-8d584d3b90d3.yaml) from what were three drifting copies:
    this module's own `_cf_repo_root_from_plugin_root_candidate` (now a thin
    wrapper below), `coordinator_registry.py::_mp_repo_root_from_plugin_root_
    candidate`, and a since-merged inline copy inside that same module's
    `doe_root()`. Each copy had picked up a different review fix; none is
    "more correct" than the others in isolation, so every divergence is
    preserved as an explicit parameter rather than picked-and-discarded:

      drive_root_guard: "preserve" (this module's B7 fix — restore the raw
        candidate when rstrip("/\\") would truncate a bare Windows drive
        root syntax example, not a hardcoded host path)  # abs-path-ok
        vs "normpath" (coordinator_registry.py's shape — routes through
        os.path.normpath() first; on a bare drive root this does NOT
        protect against the same truncation, see the module docstring's
        "Known cross-copy divergence" note).
      basename_compare: "normcase" (this module — case-insensitive only on
        Windows, case-sensitive on POSIX) vs "casefold" (coordinator_
        registry.py — case-insensitive on every platform). See the same
        "Known cross-copy divergence" note.
      manifest_relpath_fallback: this module's B5 fix (a private DoE repo
        root without the marketplace marker still normalizes via the
        manifest-relpath shape). coordinator_registry.py's copy does not
        carry this fallback — pass False to reproduce its behaviour.
      allow_unchanged_fallback: coordinator_registry.py's manifest-bootstrap
        call site (True, the default here) vs its own doe_root() rungs
        4-5, which pass False — an unrecognizable candidate must not
        silently win over an operator's explicit override just because
        os.path.isdir() happens to be true. This module's own call sites
        never vary this (always effectively True).

    CLAUDE_PLUGIN_ROOT is a *content* root: in the private/dev DoE layout
    this is `<repo_root>/coordinator`, one level below the repo root this
    module's docstring contracts to return. In the OSS flat layout the
    content root and the repo root coincide. Disambiguate the same way:
    gate on the `.claude-plugin/plugin.json` marketplace marker. If it sits
    directly under `candidate`, candidate already IS the repo root (OSS flat
    case). If it sits one level up (candidate's basename is "coordinator"
    and the marker is beside it), the repo root is the parent (private/dev
    layout). Otherwise return candidate unchanged (or "", per
    allow_unchanged_fallback) as a best-effort fallback — callers still gate
    on os.path.isdir() / manifest-presence before trusting the result.

    KNOWN CROSS-COPY DIVERGENCES (not fixed here; reported, per the
    single-sourcing task, rather than silently resolved):

    (1) LATENT — on a bare Windows drive-root-syntax candidate (a
    two-character path ending in a colon plus separator — syntax example,
    not a hardcoded host path)  # abs-path-ok  drive_root_guard="normpath"
    (coordinator_registry.py's shape) truncates its internal working value
    the same way drive_root_guard="preserve"'s B7 fix exists to prevent —
    but the truncation is normally MASKED: the unmatched-fallback branch
    below returns the original `candidate`, not either guard's internal
    value, so for the common case (no marketplace marker at the drive
    root) both guards return the same thing. The divergence would only
    become externally visible if the truncated working value itself
    matched something the untruncated one would not have (an edge case not
    exercised by either copy's current call sites, and not practically
    unit-testable without a real marker at a drive root).

    (2) LIVE — basename_compare="casefold" (coordinator_registry.py) is
    case-insensitive on every platform, where basename_compare="normcase"
    (this module's own default) is case-insensitive on Windows only — B7's
    docstring frames that fix as Windows-specific, so coordinator_
    registry.py's broader casefold compare is very likely unintentional
    scope drift, not a deliberate wider fix. Flagged for the EM/PM; left
    unchanged pending a decision.
    """
    if drive_root_guard == "preserve":
        raw = candidate
        stripped = raw.rstrip("/\\")
        if len(stripped) == 2 and stripped[1] == ":" and raw != stripped:
            # Bare drive root (e.g. "C:\\") -- rstrip would turn it into "C:", a
            # drive-relative path on Windows, not the drive root. Leave the
            # original value alone; the isdir()/manifest gates downstream reject
            # it either way (a drive root never satisfies the manifest gate).
            stripped = raw
    elif drive_root_guard == "normpath":
        stripped = os.path.normpath(candidate).rstrip("/\\")
    else:
        raise ValueError(f"unknown drive_root_guard: {drive_root_guard!r}")

    if os.path.isfile(os.path.join(stripped, ".claude-plugin", "plugin.json")):
        return stripped
    parent = os.path.dirname(stripped)
    _basename = os.path.basename(stripped)
    if basename_compare == "normcase":
        basename_matches_coordinator = os.path.normcase(_basename) == os.path.normcase("coordinator")
    elif basename_compare == "casefold":
        basename_matches_coordinator = _basename.casefold() == "coordinator"
    else:
        raise ValueError(f"unknown basename_compare: {basename_compare!r}")
    if basename_matches_coordinator and os.path.isfile(
        os.path.join(parent, ".claude-plugin", "plugin.json")
    ):
        return parent
    if manifest_relpath_fallback and basename_matches_coordinator and os.path.isfile(
        os.path.join(parent, "coordinator", "schemas", "coordinator-registry.manifest.json")
    ):
        return parent
    return candidate if allow_unchanged_fallback else ""


def _cf_repo_root_from_plugin_root_candidate(candidate: str) -> str:
    """This module's own call shape onto `repo_root_from_plugin_root_candidate()`
    — drive_root_guard="preserve", basename_compare="normcase",
    manifest_relpath_fallback=True, allow_unchanged_fallback=True (all
    defaults). Kept as a private alias so this module's own call sites and
    test module need no rename."""
    return repo_root_from_plugin_root_candidate(candidate)


def _cf_flat_layout_probe() -> Optional[str]:
    """Codename-free rung (b): the flat `~/.claude/plugins/coordinator-claude`
    marketplace-clone layout, gated on the `.claude-plugin/plugin.json`
    marker — same marker `resolve_coordinator_clone._resolve_source_mode`
    gates its OSS-install check on.

    Duplicated inline (matching C1's shape, `git show 067da377c1b0`) rather
    than delegating into `resolve_coordinator_clone` for this specific probe:
    that module exposes only the full `resolve_clone_root()` /
    `resolve_content_root()` verbs (raise-on-failure, `.git`-gated /
    plugin-manifest-marker-gated for their own different purposes), not a
    standalone flat-marker check to import and reuse here.
    """
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home:
        return None
    candidate = os.path.join(home, ".claude", "plugins", "coordinator-claude")
    marker = os.path.join(candidate, ".claude-plugin", "plugin.json")
    return candidate if os.path.isfile(marker) else None


def _cf_marketplace_cache_rung() -> Optional[str]:
    """Codename-free rung: Claude Code's REAL marketplace-install location —
    `<claude_home>/plugins/cache/coordinator-claude/coordinator/<version>/`,
    newest version wins (numeric compare, DR-148-safe).

    F1 fix (2026-08-08, hermetic-ac-reverify) -- this rung existed in the two
    bin/-side twins (`coordinator_registry.py::_mp_marketplace_cache_rung`,
    `coordinator/bin/lib/coordinator_data_root.py::_cdr_marketplace_cache_rung`)
    but was never ported to this third, engine-plane copy, so
    `coordinator_doe_root()` (and every coordinator_core caller that chains
    through it, including `coordinator_core.data_root`) returned None on a
    real marketplace-cache install while the bin/-side resolvers both
    succeeded -- see F1 in
    state/review-findings/2026-08-08-successor-partitioned/hermetic-ac-reverify.md.

    Duplicated inline rather than imported from either bin/-side twin
    (matching this module's own `_cf_flat_layout_probe()` precedent above --
    `coordinator/bin/lib/` is not on coordinator_core's import path, and
    reaching sideways into a sibling tree's bin/lib/ is exactly the coupling
    DR-047 exists to avoid; see `coordinator_core/data_root.py`'s module
    docstring for the same reasoning applied one layer up).

    Home resolution uses the plain `CLAUDE_HOME`-or-`HOME`-or-`USERPROFILE`
    ladder this module's own `_cf_flat_layout_probe()` already uses (not
    `machine_local_impl_resolve.claude_home()` -- that helper lives in
    `coordinator/bin/lib/`, the same cross-tree import this module's
    docstring already declines for `_cf_flat_layout_probe()`).

    Resolves to the repo root directly (OSS-flat shaped: schemas/ sits
    directly under the version dir) -- gated by `_cf_manifest_present` like
    every other candidate in this ladder, so no normalization is needed
    before that gate.
    """
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if not home:
        return None
    cache_parent = os.path.join(home, ".claude", "plugins", "cache", "coordinator-claude", "coordinator")
    if not os.path.isdir(cache_parent):
        return None
    best: Optional[str] = None
    best_key = (-1, -1, -1)
    try:
        entries = os.listdir(cache_parent)
    except OSError:
        return None
    for name in entries:
        child = os.path.join(cache_parent, name)
        if not os.path.isdir(child):
            continue
        parts = (name.split(".") + ["0", "0", "0"])[:3]
        nums: List[int] = []
        for part in parts:
            digits = ""
            for ch in part:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            nums.append(int(digits) if digits else 0)
        key = (nums[0], nums[1], nums[2])
        if key > best_key:
            best_key = key
            best = child
    return best


def _cf_codename_free_root() -> Optional[str]:
    """Codename-free rung ladder (rung 2.75, C1B; reordered below rungs 2/2.5
    per B2 review fix, 2026-08-08 -- see `coordinator_doe_root()`).

    Tries, in order: the `.doe-root` pointer file (durable, then legacy),
    the real marketplace-cache install location (F1 fix, 2026-08-08 -- see
    `_cf_marketplace_cache_rung()`), the flat marketplace-clone layout,
    `CLAUDE_PLUGIN_ROOT`, then the
    `plugin.mirrors.coordinator-claude.live_path` registry key. Each
    candidate is accepted only once it is a directory AND
    `_cf_manifest_present` confirms one of the two published manifest
    layouts lives under it. See module docstring § Resolution chain, rung
    2.75, for why this ladder exists and why none of its rungs can be reached
    by the OSS depersonalize scrub.

    Review: B1 (BLOCKER, 2026-08-08) -- the candidate sequence was previously
    a tuple LITERAL, so every rung (including the `machine-local` subprocess
    spawn on rung (d)) evaluated eagerly on every call, even when an earlier
    rung had already resolved. Candidates are now zero-arg callables probed
    lazily, so a rung after the first hit is never reached.
    """
    _plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    probes = (
        _cf_read_doe_root_pointer_file,
        _cf_marketplace_cache_rung,
        _cf_flat_layout_probe,
        (lambda: _cf_repo_root_from_plugin_root_candidate(_plugin_root_env)) if _plugin_root_env else (lambda: None),
        lambda: _machine_local_get("plugin.mirrors.coordinator-claude.live_path"),
    )
    for probe in probes:
        candidate = probe() or None
        if not candidate or not os.path.isdir(candidate):
            continue
        if _cf_manifest_present(candidate):
            return candidate
    return None


def _resolve_via_clone_root_script() -> Optional[str]:
    """Rung 3: native `resolve_coordinator_clone.resolve_clone_root()`, best-effort.

    Retired bash bridge (C11, 2026-07-21): previously located
    `coordinator/lib/resolve-coordinator-clone.sh` and shelled out to
    `bash <script> --clone-root`. That module is now a native Python port
    (`coordinator_core.resolve_coordinator_clone`) -- call it in-process and fold
    any resolution failure to None, preserving the same best-effort/fall-through
    contract the bash rung had.
    """
    try:
        return _resolve_coordinator_clone.resolve_clone_root()
    except _resolve_coordinator_clone.ResolveCoordinatorCloneError:
        print(f"skip: _resolve_via_clone_root_script: return _resolve_coordinator_clone.resolve_clone_root() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


# Module-scope memo replacing the retired `os.environ["REPO_DOE_CLAUDE"]` export as
# the same-process re-resolution guard (see module docstring § DECISION REVERSAL).
# `_DOE_ROOT_RESOLVED` distinguishes "not yet attempted" from "attempted, resolved to
# None" so a hard failure is not re-shelled once per call either.
_RESOLVED_DOE_ROOT: Optional[str] = None
_DOE_ROOT_RESOLVED: bool = False


def _reset_doe_root_cache() -> None:
    """Test-only helper: clear the coordinator_doe_root() process-scope memo.

    Exists because the memo is interpreter-lifetime state: under pytest the first
    test to resolve would otherwise pin the value for every later test. Mirrors
    ``coordinator_core.liveness._reset_live_ids_cache``. Wired into the suite-root
    autouse reset in ``coordinator_core/conftest.py``.
    """
    global _RESOLVED_DOE_ROOT, _DOE_ROOT_RESOLVED
    _RESOLVED_DOE_ROOT = None
    _DOE_ROOT_RESOLVED = False


def coordinator_doe_root() -> Optional[str]:
    """Resolve the DoE-claude sibling-repo root via the documented rung chain.

    Returns the resolved absolute path, or None on hard failure (rung 4) -- the
    caller is responsible for printing the remediation message (see `main()`),
    matching the bash oracle's separation of "resolve" (function return) from
    "report" (stderr side-effect the caller sees only via the function's own
    output, in this Python port split out for testability).

    PURE with respect to ``os.environ`` -- rung 1 reads ``REPO_DOE_CLAUDE`` as an
    operator override, but no rung writes it (see module docstring § DECISION
    REVERSAL). Rung 1 is evaluated BEFORE the memo so an override set after a
    prior resolution still wins, which the old export-based guard could not do.
    """
    global _RESOLVED_DOE_ROOT, _DOE_ROOT_RESOLVED

    # Rung 1: REPO_DOE_CLAUDE already set in environment (operator override).
    # Checked ahead of the memo: the env var is the authoritative override, and a
    # cached value must never shadow it.
    existing = os.environ.get("REPO_DOE_CLAUDE", "")
    if existing:
        return existing

    if _DOE_ROOT_RESOLVED:
        return _RESOLVED_DOE_ROOT

    resolved_root: Optional[str] = None

    # Rung 2: machine-local registry (canonical, DR-071).
    resolved = _machine_local_get("repos.doe_claude")
    if resolved:
        resolved_root = resolved
    else:
        # Rung 2.5: fallback to plugin.mirrors.coordinator-claude.live_path.
        resolved_fallback = _machine_local_get("plugin.mirrors.coordinator-claude.live_path")
        if resolved_fallback:
            resolved_root = resolved_fallback
        else:
            # Rung 2.75: codename-free ladder (C1B) -- see module docstring.
            # Review: B2 (MAJOR, 2026-08-08) -- this ladder was previously
            # placed AHEAD of rungs 2/2.5, so a stale marketplace install or
            # pointer file could outrank DR-071's canonical registry anchor
            # on a private dev box. DR-071 ratifies repos.doe_claude as the
            # authoritative anchor and explicitly demotes .doe-root beneath
            # it; nothing in DR-071 blesses placing this ladder ahead of the
            # registry rungs. Moved below 2/2.5 -- still fully load-bearing
            # on a genuine OSS box, where rungs 2/2.5 always return None.
            resolved_root = _cf_codename_free_root()
            if resolved_root is None:
                # Rung 3: native resolve_coordinator_clone port.
                # Rung 4 (hard failure) is `resolved_root` staying None here.
                resolved_root = _resolve_via_clone_root_script()

    _RESOLVED_DOE_ROOT = resolved_root
    _DOE_ROOT_RESOLVED = True
    return resolved_root


_REMEDIATION = (
    "coordinator_doe_root: cannot resolve REPO_DOE_CLAUDE — repos.doe_claude is not set.\n"
    "  The machine-local registry has no 'repos.doe_claude' entry (canonical) or\n"
    "  'plugin.mirrors.coordinator-claude.live_path' entry (fallback) on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.doe_claude /path/to/DoE-claude\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c\n"
)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI-shaped wrapper for parity testing against the bash oracle: prints the
    resolved path to stdout (no trailing newline, matching the oracle's
    `printf '%s'`) and returns 0, or writes the remediation block to stderr and
    returns 1. Not the primary call shape (Python callers should import
    `coordinator_doe_root()` directly) -- provided so this module is independently
    exercisable/parity-testable the same way the bash oracle's function is."""
    root = coordinator_doe_root()
    if root is None:
        sys.stderr.write(_REMEDIATION)
        return 1
    sys.stdout.write(root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

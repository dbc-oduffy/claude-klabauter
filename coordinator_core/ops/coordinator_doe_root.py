"""
coordinator_core.ops.coordinator_doe_root — Port of:
coordinator-doe-root.sh (example-doctrine-repo 09e5e5f9, 2026-07-19, sourced-lib shape, DOE-PORT R2-R6 wave).

Purpose: resolves the example-doctrine-repo sibling-repo root, analogous to how CLAUDE_KLABAUTER_ROOT
works for the claude-klabauter plane. The bash oracle exposes a single public shell function,
`coordinator_doe_root`, meant to be `source`d by other bash scripts. This module
provides the same resolution chain as a plain Python function so any coordinator_core
caller can import it directly without shelling back out to bash.

NOTE — the example-doctrine-repo-side `.sh` is a SOURCED LIB, not an executable: nothing can `source` a
`.py` file, so `coordinator-doe-root.sh` is left UNCHANGED by this port (its bash
callers keep sourcing it as-is). This module is authored so that a *future* Python
caller (or a bash→Python cutover of one of its callers, e.g.
`coordinator-state-root.sh`) has a drop-in equivalent to import instead of source.

Resolution chain (mirrors the bash oracle's header comment verbatim, rung-for-rung):
  1. REPO_EXAMPLE_DOCTRINE_REPO env var — if already set (non-empty), return it unchanged.
  1.5. Codename-free rung ladder (added 2026-08-07, C1B) — runs ahead of rung 2.
     On the published mirror, the registry never carries `repos.example_doctrine_repo` nor its
     scrubbed successor `repos.example_doctrine_repo`, so on a genuine OSS box every
     rung below this one is dead (executed proof: `coordinator_doe_root()` returns
     `None` from the mirror with registry reachability removed — see
     `tasks/2026-08-07-published-engine-codename/briefs/C1B.md`). None of these
     candidates carry a private codename, so the depersonalize scrub cannot touch
     them:
       a. `<settings-home>/machine-local/.doe-root`, then
          `${CLAUDE_HOME:-$HOME}/.claude/.doe-root` — via
          `coordinator_core.doe_root_pointer.read_doe_root_pointer_file()` (the
          file-rungs-only sibling of `read_doe_root_pointer()`, which additionally
          has a registry-first rung this ladder does not want spliced in ahead of
          its own registry rung below).
       b. the flat `~/.claude/plugins/coordinator-claude` marketplace-clone layout,
          gated on the `.claude-plugin/plugin.json` marker.
       c. `CLAUDE_PLUGIN_ROOT` env var, normalized via
          `_cf_repo_root_from_plugin_root_candidate()` (C1E fix) — the raw
          env value is a *content* root, one level below the repo root the
          private/dev layout expects; see that helper's docstring.
       d. `plugin.mirrors.coordinator-claude.live_path` (registry) — same call as
          rung 2.5 below, just tried earlier in this sub-ladder.
     Each candidate is accepted only if it is a directory AND contains one of the
     two published manifest layouts (OSS flat `schemas/...`, private
     `coordinator/schemas/...`) — see `_cf_manifest_present`.
  2. `machine-local get repos.example_doctrine_repo` (CANONICAL) — the machine-local registry's
     own four-rung discovery ladder runs inside that CLI; not reimplemented here.
  3. Rung 2.5 fallback: `machine-local get plugin.mirrors.coordinator-claude.live_path`
     — only fires when rung 2 (repos.example_doctrine_repo) returned nothing. Covers a machine
     that registered the coordinator-specific mirror key but never set the general
     repos.example_doctrine_repo key.
  4. Rung 3 fallback: calls the native `coordinator_core.resolve_coordinator_clone`
     port (`resolve_clone_root()`) — the shared unified resolver lib, ported native
     in a prior C11-core wave. Covers the `.doe-root` pointer-file rung and the
     flat-layout rung. Only fires when rungs 2 and 2.5 both returned nothing.
     PORT NOTE (2026-07-21, C11): this rung previously shelled out to
     `bash coordinator/lib/resolve-coordinator-clone.sh --clone-root`; that bash
     bridge is now retired in favor of the in-process native call — same
     best-effort/fold-to-None-on-failure contract, no subprocess spawn.
  5. Hard error: returns None, remediation message written to stderr by the caller
     (see `main()` below for the CLI-shaped stderr contract, faithfully reproduced).

Pure resolver — does NOT mutate `os.environ` (REVERSED 2026-07-21; see below).
Rung 1 still READS `REPO_EXAMPLE_DOCTRINE_REPO` as an operator override; nothing here writes it.

DECISION REVERSAL (2026-07-21) — this module previously exported
`os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` on rungs 2/2.5/3, mirroring the bash oracle's
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
`REPO_EXAMPLE_DOCTRINE_REPO` in a CHILD process's environment pass it explicitly via `env=`
(as `install.maximalist` and `install.sandbox_check` already do).

Review: code-reviewer — `coordinator_core.claude_klabauter_root`'s negative-spec cites this
module as the deliberately-asymmetric counter-example. That cross-reference is now
STALE: both resolvers are pure. The two modules agree; claude_klabauter_root's note should be
updated when that file is next touched.

Spec backlink: docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.1
             + docs/plans/2026-07-09-resolver-unification-v3split-01.md § C3
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Negative-spec (faithfully reproduced from the bash oracle -- do NOT "fix" mid-port):
    - Rung 2's machine-local invocation captures stdout+returncode only; any
      exception locating/running the `machine-local` binary (not on PATH, OSError,
      timeout) is folded into "rung 2 failed, fall through" exactly like the bash
      oracle's `2>/dev/null || _ml_rc=$?` discard-and-continue shape -- no distinct
      "operational failure" vs "missing key" branch survives the port (the bash
      oracle's own header comment claims that distinction exists via rc=1 vs rc=2,
      but the implementation never actually branches on the rc value beyond
      `-eq 0`, so there is nothing differentiated to preserve).
    - Rung 2.5 and rung 3 are UNCONDITIONAL best-effort: any subprocess failure
      (non-zero exit, missing binary, timeout) degrades to empty string and falls
      through to the next rung, matching the oracle's `|| _resolved_fallback=""` /
      `|| true` shape.
    - `machine-local` is resolved via bare PATH lookup (`shutil.which`), matching
      the bash oracle's bare `machine-local get ...` invocation (no co-located
      sibling-binary fallback attempted, unlike gen_doe_root_pointer.py's Tier 2 --
      the bash oracle here never had that optimization to begin with).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List, Optional

from coordinator_core import resolve_coordinator_clone as _resolve_coordinator_clone
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file as _cf_read_doe_root_pointer_file

_SUBPROCESS_TIMEOUT_SECS = 15

# Published-manifest relpath (OSS flat layout). The private example-doctrine-repo-repo layout
# nests the same relpath under `coordinator/`. Shared by the codename-free
# rung ladder's acceptance gate below.
_CF_MANIFEST_RELPATH = os.path.join("schemas", "coordinator-registry.manifest.json")


def _machine_local_get(key: str) -> Optional[str]:
    """Run `machine-local get <key>`, returning stripped stdout on success (rc==0,
    non-empty) or None on any failure (missing binary, non-zero exit, timeout,
    empty output). Mirrors the bash oracle's discard-and-continue shape."""
    ml_bin = shutil.which("machine-local")
    if ml_bin is None:
        return None
    try:
        result = subprocess.run(
            [ml_bin, "get", key],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    resolved = (result.stdout or "").strip()
    return resolved or None


def _cf_manifest_present(root: str) -> bool:
    """True if `root` contains either published manifest layout: OSS flat
    (`<root>/schemas/coordinator-registry.manifest.json`) or the private
    example-doctrine-repo-repo shape (`<root>/coordinator/schemas/coordinator-registry.manifest.json`).
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


def _cf_repo_root_from_plugin_root_candidate(candidate: str) -> str:
    """Normalize a CLAUDE_PLUGIN_ROOT-shaped value to the coordinator REPO
    root this ladder must return.

    CLAUDE_PLUGIN_ROOT is a *content* root (see
    coordinator_registry.py::_mp_repo_root_from_plugin_root_candidate(),
    which this function ports rung-for-rung): in the private/dev example-doctrine-repo layout
    this is `<repo_root>/coordinator`, one level below the repo root this
    module's docstring contracts to return. In the OSS flat layout the
    content root and the repo root coincide. Disambiguate the same way:
    gate on the `.claude-plugin/plugin.json` marketplace marker. If it sits
    directly under `candidate`, candidate already IS the repo root (OSS flat
    case). If it sits one level up (candidate's basename is "coordinator"
    and the marker is beside it), the repo root is the parent (private/dev
    layout). Otherwise return candidate unchanged as a best-effort fallback
    — callers still gate on os.path.isdir() / manifest-presence before
    trusting the result.

    Do NOT edit coordinator_registry.py's own copy of this helper — this is
    a deliberate duplicate (see chunk C1E task notes): that module cannot be
    imported from here at this call site without violating this module's own
    layering, so the same technique is ported rather than shared.
    """
    stripped = candidate.rstrip("/\\")
    if os.path.isfile(os.path.join(stripped, ".claude-plugin", "plugin.json")):
        return stripped
    parent = os.path.dirname(stripped)
    if os.path.basename(stripped) == "coordinator" and os.path.isfile(
        os.path.join(parent, ".claude-plugin", "plugin.json")
    ):
        return parent
    return candidate


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


def _cf_codename_free_root() -> Optional[str]:
    """Codename-free rung ladder (rung 1.5, C1B) — runs ahead of rung 2.

    Tries, in order: the `.doe-root` pointer file (durable, then legacy),
    the flat marketplace-clone layout, `CLAUDE_PLUGIN_ROOT`, then the
    `plugin.mirrors.coordinator-claude.live_path` registry key. Each
    candidate is accepted only once it is a directory AND
    `_cf_manifest_present` confirms one of the two published manifest
    layouts lives under it. See module docstring § Resolution chain, rung
    1.5, for why this ladder exists and why none of its rungs can be reached
    by the OSS depersonalize scrub.
    """
    _plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    for candidate in (
        _cf_read_doe_root_pointer_file() or None,
        _cf_flat_layout_probe(),
        _cf_repo_root_from_plugin_root_candidate(_plugin_root_env) if _plugin_root_env else None,
        _machine_local_get("plugin.mirrors.coordinator-claude.live_path"),
    ):
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


# Module-scope memo replacing the retired `os.environ["REPO_EXAMPLE_DOCTRINE_REPO"]` export as
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
    """Resolve the example-doctrine-repo sibling-repo root via the documented rung chain.

    Returns the resolved absolute path, or None on hard failure (rung 4) -- the
    caller is responsible for printing the remediation message (see `main()`),
    matching the bash oracle's separation of "resolve" (function return) from
    "report" (stderr side-effect the caller sees only via the function's own
    output, in this Python port split out for testability).

    PURE with respect to ``os.environ`` -- rung 1 reads ``REPO_EXAMPLE_DOCTRINE_REPO`` as an
    operator override, but no rung writes it (see module docstring § DECISION
    REVERSAL). Rung 1 is evaluated BEFORE the memo so an override set after a
    prior resolution still wins, which the old export-based guard could not do.
    """
    global _RESOLVED_DOE_ROOT, _DOE_ROOT_RESOLVED

    # Rung 1: REPO_EXAMPLE_DOCTRINE_REPO already set in environment (operator override).
    # Checked ahead of the memo: the env var is the authoritative override, and a
    # cached value must never shadow it.
    existing = os.environ.get("REPO_EXAMPLE_DOCTRINE_REPO", "")
    if existing:
        return existing

    if _DOE_ROOT_RESOLVED:
        return _RESOLVED_DOE_ROOT

    resolved_root: Optional[str] = None

    # Rung 1.5: codename-free ladder (C1B) -- see module docstring. Ahead of
    # rung 2 because the published mirror's registry never carries
    # repos.example_doctrine_repo nor its scrubbed successor, so every rung below is
    # dead on a genuine OSS box.
    resolved_root = _cf_codename_free_root()

    if resolved_root is None:
        # Rung 2: machine-local registry (canonical).
        resolved = _machine_local_get("repos.example_doctrine_repo")
        if resolved:
            resolved_root = resolved
        else:
            # Rung 2.5: fallback to plugin.mirrors.coordinator-claude.live_path.
            resolved_fallback = _machine_local_get("plugin.mirrors.coordinator-claude.live_path")
            if resolved_fallback:
                resolved_root = resolved_fallback
            else:
                # Rung 3: native resolve_coordinator_clone port.
                # Rung 4 (hard failure) is `resolved_root` staying None here.
                resolved_root = _resolve_via_clone_root_script()

    _RESOLVED_DOE_ROOT = resolved_root
    _DOE_ROOT_RESOLVED = True
    return resolved_root


_REMEDIATION = (
    "coordinator_doe_root: cannot resolve REPO_EXAMPLE_DOCTRINE_REPO — repos.example_doctrine_repo is not set.\n"
    "  The machine-local registry has no 'repos.example_doctrine_repo' entry (canonical) or\n"
    "  'plugin.mirrors.coordinator-claude.live_path' entry (fallback) on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.example_doctrine_repo /path/to/example-doctrine-repo\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c\n"
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

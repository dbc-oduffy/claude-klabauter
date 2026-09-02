"""
bin/claude-klabauter-doctor-probe.py — Tier-1 static diagnostic probe for the claude-klabauter install chain.

Purpose: Exercises the claude-klabauter bootstrap chain without depending on a live coordinator_core
process or any resident process.  Invoked directly as `python bin/claude-klabauter-doctor-probe.py`;
run by scripts/setup.py (guarantee-3 post-install verification) and by
the test suite.

DR-215: coordinator_core is a command-type, spawn-per-call engine — no resident process,
no UDS server, no MCP shim liveness to check.  All probes in this file are static checks.

Checks — one ProbeResult per registered probe, run in dependency order (which is
why `run_probes` arranges sys.path and resolves COORDINATOR_ENGINE_ROOT as unconditional
prerequisites before any selection is applied — see `_ensure_core_importable`):

  claude-klabauter.root.resolve    REQUIRED — COORDINATOR_ENGINE_ROOT resolves via env/machine-local/git-root.
  claude-klabauter.registry.key    REQUIRED (OPTIONAL when machine-local absent) — repos.claude_klabauter
                         registered.  Skipped as advisory when coordinator-claude is not
                         installed (machine-local absent → key not needed).
  claude-klabauter.core.import     REQUIRED — import coordinator_core succeeds from COORDINATOR_ENGINE_ROOT.
  claude-klabauter.resident.debris REQUIRED — detects stale paths from the retired daemon (INFO
                         when found; PASS when absent).
  claude-klabauter.version.sanity  REQUIRED — coordinator_core version helper resolves; retired
                         submodule coordinator_core.client correctly absent.
  claude-klabauter.invoke.smoke    OPTIONAL — spawn-per-call dispatch smoke via coordinator_core.invoke
                         ping; proves entrypoint can dispatch (not session liveness).
                         SKIP on an unstamped engine root — DR-331 rules such a tree
                         ineligible to dispatch, so there is nothing to grade.
  claude-klabauter.strategic.draft_staleness  OPTIONAL — nudges when state/strategic/self-description
                         .draft.yaml is missing (SKIP, not a fault) or older than the newest
                         state/week-changelog/*.md entry (INFO nudge).
  claude-klabauter.schema.vendor_drift  OPTIONAL — every schema vendored under coordinator_core/
                         frontmatter/schemas/ still matches DoE HEAD (DEGRADED on drift,
                         DEGRADED-as-INDETERMINATE when the DoE clone is unreadable,
                         SKIP when no DoE clone exists on this machine).
  claude-klabauter.root.pointer    OPTIONAL — claude-klabauter-live-root pointer file present at
                         <settings-home>/machine-local/.claude-klabauter-live-root and matches the resolved
                         COORDINATOR_ENGINE_ROOT (DEGRADED, not hard FAIL, on absence/mismatch — without it,
                         per-invoke resolution falls back to a bash subprocess with a 5 s
                         timeout on Windows).
  claude-klabauter.invoke.latency  OPTIONAL — measures a single coordinator_core.invoke round-trip
                         as PROCESS TIME (never wall clock) against a 500 ms brightline
                         budget (hooks share a ~3-5 s total budget across multiple
                         invokes); DEGRADED (not BROKEN) over budget or on timeout;
                         SKIP on an unstamped engine root (DR-331).
  claude-klabauter.commitments.recheck  OPTIONAL — re-resolves state/cross-repo-commitments/ evidence:
                         strings live against each record's committing sibling (DEGRADED when
                         any record's evidence resolves truthy while status: is still open;
                         never auto-flips status:).
  claude-klabauter.execnet.orphaned_gateways  OPTIONAL — flags execnet gateway (pytest -n worker)
                         processes whose controller has died (DEGRADED on any found with
                         no live controller; PASS when none found or all found are under
                         a live controller — a healthy in-flight run, never flagged).

Run modes:

  (default)    Emit full JSON verdict envelope to stdout; exit 0 always.
  --step-zero  Emit five-key NDJSON per step-zero-emitter-contract.md; exit 1 if any
               REQUIRED probe is BROKEN or DEGRADED, else exit 0.

Imports coordinator_core.doctor_envelope (C0) for envelope emission when coordinator_core
is importable; falls back to an equivalent local implementation otherwise so the probe
still reports useful results on a broken tree.

Negative-spec:
  - stdlib-only at module scope: no third-party import at module top-level. ONE
    probe-local exception is documented in place — a lazy, guarded `import psutil`
    inside `_run_probe_orphaned_execnet_gateways` only, mirroring
    `coordinator_core.diagnostics.contained_run`'s own guarded-import convention
    (psutil is a declared, required coordinator_core dependency, not an
    arbitrary third party); ImportError there degrades to SKIP and never breaks
    module import or any other probe.
  - Does NOT depend on a live coordinator_core process — designed to run when it is dead.
  - Does NOT hardcode the claude-klabauter root path — resolves via COORDINATOR_ENGINE_ROOT ladder.
  - Does NOT probe a resident UDS service — retired under DR-215 (command-type engine).
  - Does NOT handshake a coordinator-core shim — shim probes retired under DR-215.

Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C1a
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
try:
    import tomllib
    _TOMLLIB_AVAILABLE = True
except ImportError:
    tomllib = None  # type: ignore[assignment]
    _TOMLLIB_AVAILABLE = False
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GENERATES = []  # writes only state/doctor-last-run.json, which is gitignored (.gitignore: "Per-machine cadence/health sentinels") — never a tracked artifact

# ---------------------------------------------------------------------------
# Step-zero NDJSON vocabulary (step-zero-emitter-contract.md §The Five Keys)
# ---------------------------------------------------------------------------
_SZ_PASS         = "pass"
_SZ_FAIL         = "fail"
_SZ_WARN         = "warn"
_SZ_INCONCLUSIVE = "inconclusive"

_SZ_HARD     = "hard"
_SZ_ADVISORY = "advisory"

# ---------------------------------------------------------------------------
# Advisory-vs-hard human rendering
#
# The step-zero vocabulary has always carried the distinction in `severity`
# (hard | advisory), but every renderer downstream leads with `status`, and an
# advisory `fail` is visually identical to a required one. On 2026-07-28 a fresh
# installer read `claude-klabauter.schema.vendor_drift`'s advisory `fail` as actionable and
# spent an entire detour re-vendoring a schema that was never blocking the
# install (state/audits/2026-07-28-windows-install-dogfood-friction.md § F3).
#
# The fix is rendering-only, and deliberately so: `status`, `severity`, the
# exit-code contract, and every state/doctor-last-run.json key shape are
# UNCHANGED — they have documented external consumers (see _write_doctor_sentinel's
# ADDITIVE-KEY POLICY). What changes is that the free-text `detail` an
# advisory-severity failure carries now SAYS it is advisory, so a renderer that
# prints status + detail (scripts/setup.py's human branch does exactly that)
# cannot show the failure without also showing that it is non-gating.
#
# Negative-spec:
#   - Does NOT touch `status` / `severity` / exit codes / sentinel key shapes.
#   - Applies ONLY to non-required probes whose status is BROKEN/DEGRADED. A
#     required failure is left alone: nothing may soften a hard failure's text.
#   - Idempotent — a detail already carrying the marker is not re-prefixed.
# ---------------------------------------------------------------------------
_ADVISORY_DETAIL_MARKER = "ADVISORY (non-gating — the install is not broken): "


def _mark_advisory_detail(detail: str, required: bool, status: str) -> str:
    """Prefix an advisory-severity failure's detail so it cannot render as a hard one.

    Rendering-only; see the block comment above for the full contract. Returns
    `detail` unchanged for required probes, for non-failure statuses, and for a
    detail that already carries the marker.
    """
    if required or status not in (_BROKEN, _DEGRADED):
        return detail
    if detail.startswith(_ADVISORY_DETAIL_MARKER):
        return detail
    return _ADVISORY_DETAIL_MARKER + detail


# ---------------------------------------------------------------------------
# Enumeration cap — a detail names examples, never a whole population.
# ---------------------------------------------------------------------------
_ENUMERATION_CAP = 5


def _capped_join(items: Any, cap: int = _ENUMERATION_CAP, sep: str = ", ") -> str:
    """Join `items` naming at most `cap` of them, followed by "(+N more)".

    A probe detail is read by an operator scanning install output on one line:
    the count and the named hazard carry the decision, the tail of identifiers
    does not. An uncapped join over a corpus-sized population (223 session ids,
    ~15KB, one line) buries every probe result printed after it.

    Negative-spec: does NOT truncate an individual item, and does NOT touch a
    _ProbeResult's `data` — the full population stays machine-readable there.
    """
    materialized = [str(i) for i in items]
    named = sep.join(materialized[:cap])
    if len(materialized) > cap:
        named += f" (+{len(materialized) - cap} more)"
    return named

# ---------------------------------------------------------------------------
# Verdict constants — mirror coordinator_core.doctor_envelope for local fallback.
# The authoritative definitions live in coordinator_core.doctor_envelope (C0).
# ---------------------------------------------------------------------------
_BROKEN   = "BROKEN"
_DEGRADED = "DEGRADED"
_INFO     = "INFO"
_PASS     = "PASS"

_RANK: dict[str, int] = {_BROKEN: 3, _DEGRADED: 2, _PASS: 1}


# Python-version gate constants — shared between _python_version_broken_envelope() and
# the step-zero NDJSON path in main(). Extracted to module-level to eliminate the DRY
# violation between two sites that previously held identical inline copies.
_PYVER_DETAIL = (
    f"Python 3.11+ required to run the claude-klabauter doctor probe "
    f"(running {sys.version.split()[0]}); tomllib unavailable."
)
_PYVER_REMEDIATION = (
    "Install or select Python 3.11+ (python3 scripts/setup.py provisions a supported "
    "interpreter), then re-run the probe."
)


# ---------------------------------------------------------------------------
# Local _ProbeResult — mirrors coordinator_core.doctor_envelope.ProbeResult.
#
# Used throughout probe collection so the script works even before coordinator_core
# is importable.  Converted to the real ProbeResult inside _build_envelope_via_module()
# when coordinator_core.doctor_envelope is available.
#
# Negative-spec: this dataclass duplicates the coordinator_core.doctor_envelope
# interface by design — the probe must remain functional on a broken tree where
# coordinator_core is not importable.  Do NOT import this from coordinator_core.
# ---------------------------------------------------------------------------


@dataclass
class _ProbeResult:
    """Local probe-result carrier; mirrors coordinator_core.doctor_envelope.ProbeResult."""

    probe: str
    status: str
    detail: str
    remediation: str
    data: dict[str, Any] | None = None
    required: bool = True
    skipped: bool = False
    # Wall time this probe's own function spent, stamped by run_probes' `_timed`
    # wrapper. None when the result was synthesised rather than run (selector INFO
    # stubs, the dependency-skipped claude-klabauter.core.import placeholder, the broken-tree
    # envelopes). Local-only: `_build_envelope_via_module` constructs
    # coordinator_core.doctor_envelope.ProbeResult field-by-field, so this never
    # reaches the shared cross-repo dataclass.
    duration_ms: float | None = None


# ---------------------------------------------------------------------------
# Local envelope builder — fallback when coordinator_core is not importable.
# Implements the same reduction logic as coordinator_core.doctor_envelope.
# ---------------------------------------------------------------------------


def _local_reduce_overall(results: list[_ProbeResult]) -> str:
    """Worst-of reduction identical to coordinator_core.doctor_envelope.reduce_overall."""
    best: str | None = None
    for r in results:
        if r.skipped:
            s: str | None = _DEGRADED if r.required else None
        else:
            s = r.status if r.status != _INFO else None
        if s is None:
            continue
        rank = _RANK.get(s, 0)
        if best is None or rank > _RANK.get(best, 0):
            best = s
    return best if best is not None else _INFO


def _local_build_envelope(results: list[_ProbeResult]) -> dict[str, Any]:
    """Build envelope dict mirroring coordinator_core.doctor_envelope.build_envelope."""
    overall = _local_reduce_overall(results)
    probe_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    missing_optional: list[str] = []

    for r in results:
        if r.skipped:
            if r.required:
                probe_rows.append({
                    "probe": r.probe, "status": _DEGRADED,
                    "detail": r.detail, "remediation": r.remediation, "data": r.data,
                })
            else:
                missing_optional.append(r.probe)
                warnings.append(
                    f"optional probe skipped (coordinator-dependent or prerequisite absent):"
                    f" {r.probe} — {r.detail}"
                )
                probe_rows.append({
                    "probe": r.probe, "status": "SKIP",
                    "detail": r.detail, "remediation": r.remediation, "data": r.data,
                })
        else:
            probe_rows.append({
                "probe": r.probe, "status": r.status,
                "detail": r.detail, "remediation": r.remediation, "data": r.data,
            })

    return {
        "schema_version": 1,
        "status_vocab": [_BROKEN, _DEGRADED, _INFO, _PASS],
        "overall": overall,
        "probes": probe_rows,
        "warnings": warnings,
        "missing_optional": missing_optional,
    }


def _build_envelope_via_module(
    results: list[_ProbeResult],
    claude_klabauter_root: Path | None,
) -> dict[str, Any]:
    """Use coordinator_core.doctor_envelope.build_envelope if available; else fall back.

    Purpose: honours the C1 spec requirement "Imports coordinator_core.doctor_envelope
    (C0) for envelope emit" when coordinator_core is importable, while remaining
    functional on a broken tree where coordinator_core is not importable.
    """
    if claude_klabauter_root is not None:
        try:
            from coordinator_core.doctor_envelope import (  # type: ignore[import]
                ProbeResult as RealProbeResult,
                build_envelope,
            )
            real_results = [
                RealProbeResult(
                    probe=r.probe,
                    status=r.status,
                    detail=r.detail,
                    remediation=r.remediation,
                    data=r.data,
                    required=r.required,
                    skipped=r.skipped,
                )
                for r in results
            ]
            return build_envelope(real_results)
        except (ImportError, Exception):
            # coordinator_core unimportable, or its envelope shape diverged --
            # fall through to the self-contained local envelope builder below
            # so this probe stays functional even on a broken tree (see
            # docstring: the whole point of this function).
            pass
    return _local_build_envelope(results)


# ---------------------------------------------------------------------------
# COORDINATOR_ENGINE_ROOT resolution ladder
#
# Mirrors coordinator-claude-klabauter-root.sh and coordinator-core-shim._resolve_claude_klabauter_root():
#   Rung 1 — COORDINATOR_ENGINE_ROOT env var (falls back to the retired
#             CLAUDE_KLABAUTER_ROOT name if unset; caller already exported it)
#   Rung 2 — machine-local get repos.claude_klabauter (authoritative coordinator-side key)
#   Rung 3 — git-root auto-discovery from this script's bin/ location
#
# EXCLUDED BY NAME from the C3 collapse (plan
# 2026-08-19-an-engine-root-is-a-stamped-build § C3, § "The fourth site"):
# this ladder stays its own local implementation and is NOT routed through
# `coordinator_core.warm.engine_root`. The doctor must stay functional on a
# tree where `coordinator_core` is not importable -- that is the broken
# state operators actually run a doctor in -- and it resolves the live
# CLAUDE-KLABAUTER source tree (a locator question), never a stamped engine (a
# dispatch question), so collapsing it onto the engine-root predicate would
# both destroy the case the doctor exists for and conflate the two axes.
# `coordinator/bin/tests/test_doctor_probe_ladder_parity.py` pins that this
# ladder and the shared resolver agree on rung semantics without merging
# their implementations.
# ---------------------------------------------------------------------------


def _resolve_claude_klabauter_root() -> tuple[Path | None, str]:
    """Return (resolved_path, source_description) or (None, error_message).

    Tries three rungs in order; the first hit wins.
    """
    # Rung 1 — env var (idempotency gate: already exported by a parent shell).
    #
    # C23: this rung read the RETIRED name and nothing else, so it went dark
    # the moment C14 stopped anything exporting it — an operator with only
    # COORDINATOR_ENGINE_ROOT set got no rung-1 hit at all and fell silently
    # through to the registry ladder. In a DOCTOR PROBE that is worse than
    # ordinary: the tool whose job is diagnosing a misresolved root was itself
    # misresolving it, and reporting rung 2's answer as though rung 1 had
    # nothing to say.
    #
    # The retired name is kept as an explicit second rung rather than dropped:
    # this is a diagnostic run against boxes in unknown states, including ones
    # mid-migration, and a probe that cannot see a stale pin cannot report it.
    # It is reported under its own source string so the operator sees WHICH
    # name answered.
    val = os.environ.get("COORDINATOR_ENGINE_ROOT", "").strip()
    if val:
        return Path(val), "env COORDINATOR_ENGINE_ROOT"
    val = os.environ.get("CLAUDE_KLABAUTER_ROOT", "").strip()
    if val:
        return Path(val), "env CLAUDE_KLABAUTER_ROOT (RETIRED — set COORDINATOR_ENGINE_ROOT)"

    # Rung 2 — machine-local registry (authoritative coordinator-side path).
    # Read in-process via coordinator_core.machine_resolver.registry_get (a
    # direct registry.local.toml/registry.toml tomllib read) rather than
    # shelling out to `machine-local get` -- this script lives inside the
    # very repo it is trying to locate (<claude-klabauter-live-root>/bin/
    # claude-klabauter-doctor-probe.py), so the same git-root candidate rung 3 derives
    # below is tried first as a sys.path anchor for the import. Lazy,
    # probe-local import per this module's own negative-spec (see module
    # docstring).
    try:
        candidate = Path(__file__).resolve().parent.parent
        candidate_str = str(candidate)
        added = candidate_str not in sys.path
        if added:
            sys.path.insert(0, candidate_str)
        try:
            from coordinator_core.machine_resolver import registry_get

            val = registry_get("repos.claude_klabauter")
        finally:
            if added:
                sys.path.remove(candidate_str)
        if val:
            return Path(val), "machine-local repos.claude_klabauter"
    except Exception:
        # coordinator_core not importable from the candidate root, or the
        # key is unresolved -- not fatal, just fall through to the next
        # rung in the resolution ladder.
        pass

    # Rung 3 — git-root auto-discovery from this script's location.
    # This script lives at <claude-klabauter-live-root>/bin/claude-klabauter-doctor-probe.py, so its
    # parent's parent is the repo root.
    try:
        candidate = Path(__file__).resolve().parent.parent
        if (candidate / ".git").is_dir():
            return candidate, "git-root auto-discovery (bin/ parent)"
    except Exception:
        # __file__ resolution or the .git probe failed (unusual filesystem,
        # symlink oddities) -- this is the last rung, so fall through to
        # the "could not resolve" error message below.
        pass

    return None, (
        "Cannot resolve the engine root via (1) COORDINATOR_ENGINE_ROOT env var "
        "(or the retired CLAUDE_KLABAUTER_ROOT, reported separately if set), "
        "(2) machine-local get repos.claude_klabauter, "
        "(3) git-root auto-discovery from bin/ parent. "
        "Remediation: run scripts/setup.py to register repos.claude_klabauter, "
        "or: export COORDINATOR_ENGINE_ROOT=/path/to/claude-klabauter"
    )


# ---------------------------------------------------------------------------
# Probe 1: COORDINATOR_ENGINE_ROOT resolves
# ---------------------------------------------------------------------------


_CLAUDE_KLABAUTER_ROOT_PROBE = "claude-klabauter.root.resolve"


def _run_probe_claude_klabauter_root() -> tuple[_ProbeResult, Path | None]:
    """Probe claude-klabauter.root.resolve — REQUIRED.

    Returns (probe_result, resolved_path_or_None).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.
    """
    try:
        root, source = _resolve_claude_klabauter_root()

        if root is None:
            return _ProbeResult(
                probe=_CLAUDE_KLABAUTER_ROOT_PROBE,
                status=_BROKEN,
                detail=source,
                remediation=(
                    "Run scripts/setup.py to register repos.claude_klabauter, "
                    "or set COORDINATOR_ENGINE_ROOT=/path/to/claude-klabauter in the environment."
                ),
            ), None

        if not root.exists():
            return _ProbeResult(
                probe=_CLAUDE_KLABAUTER_ROOT_PROBE,
                status=_BROKEN,
                detail=(
                    f"COORDINATOR_ENGINE_ROOT resolved to {str(root)!r} (via {source}) "
                    "but path does not exist on disk."
                ),
                remediation=(
                    "Ensure claude-klabauter is checked out at the resolved path. "
                    "Update the registry: machine-local set repos.claude_klabauter <correct-path>"
                ),
            ), None

        core_dir = root / "coordinator_core"
        if not core_dir.is_dir():
            return _ProbeResult(
                probe=_CLAUDE_KLABAUTER_ROOT_PROBE,
                status=_BROKEN,
                detail=(
                    f"COORDINATOR_ENGINE_ROOT={str(root)!r} (via {source}) but coordinator_core/ "
                    "directory absent. Is this the correct claude-klabauter repo root?"
                ),
                remediation=(
                    "Verify the path is the claude-klabauter repo root (must contain coordinator_core/). "
                    "Update: machine-local set repos.claude_klabauter /correct/path"
                ),
            ), None

        return _ProbeResult(
            probe=_CLAUDE_KLABAUTER_ROOT_PROBE,
            status=_PASS,
            detail=(
                f"COORDINATOR_ENGINE_ROOT={str(root)!r} (resolved via {source}); "
                "coordinator_core/ directory present."
            ),
            remediation="—",
            data={"claude_klabauter_root": str(root), "source": source},
        ), root
    except Exception as exc:
        return _ProbeResult(
            probe=_CLAUDE_KLABAUTER_ROOT_PROBE,
            status=_BROKEN,
            detail=f"Unexpected error in root resolve probe: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
        ), None


# ---------------------------------------------------------------------------
# Probe 2: repos.claude_klabauter registered
# ---------------------------------------------------------------------------


def _resolve_machine_local() -> str | None:
    """Resolve the machine-local shim to an invocable command, or None if absent.

    Root cause (F10): a bare ``subprocess.run(["machine-local", ...])`` fails with
    FileNotFoundError on Windows even when the shim IS installed and on PATH — Win32
    CreateProcess (which subprocess.run uses directly, no shell) does not apply PATHEXT
    resolution to extension-less command names the way cmd.exe/PowerShell do, so it
    never finds ``machine-local.cmd``. This mirrors resolving `machine-local` by hand
    only via a shell, never via a bare CreateProcess call.

    Resolution order mirrors the `machine-local` forwarder itself
    (canonical home <settings-home>/bin/machine-local; see its own
    "LOCATION-INDEPENDENT FORWARDER" comment):
      1. shutil.which("machine-local") — respects PATHEXT (finds machine-local.cmd on
         Windows) and the current process's actual PATH; the correct general-purpose
         resolver, and sufficient whenever PATH is intact.
      2. ${COORDINATOR_SETTINGS_HOME}/bin/machine-local[.cmd] — primary post-migration
         settings-home location, checked explicitly in case PATH was not inherited by
         this subprocess's environment (e.g. mid-orchestrator, see F2).
      3. ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings/bin/machine-local[.cmd] —
         same settings-home seam, CLAUDE_HOME/HOME-derived default.
      4. ${CLAUDE_HOME:-$HOME}/.claude/bin/machine-local[.cmd] — transitional compat
         forwarder location predating the settings-home migration.
    """
    found = shutil.which("machine-local")
    if found:
        return found

    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.path.expanduser("~")
    candidate_dirs = [
        os.environ.get("COORDINATOR_SETTINGS_HOME", ""),
        os.path.join(home, ".coordinator-claude-settings"),
        os.path.join(home, ".claude"),
    ]
    names = ["machine-local.cmd", "machine-local.exe", "machine-local"] if os.name == "nt" else ["machine-local"]
    for d in candidate_dirs:
        if not d:
            continue
        bin_dir = os.path.join(d, "bin")
        for name in names:
            candidate = os.path.join(bin_dir, name)
            if os.path.isfile(candidate):
                return candidate

    return None


def _is_machine_local_available() -> bool:
    """Return True iff machine-local is resolvable (coordinator-claude installed)."""
    cmd = _resolve_machine_local()
    if cmd is None:
        return False
    try:
        r = subprocess.run(
            [cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


_REGISTRY_KEY_PROBE = "claude-klabauter.registry.key"


def _run_probe_registry_key() -> _ProbeResult:
    """Probe claude-klabauter.registry.key — REQUIRED when machine-local present; OPTIONAL otherwise.

    Checks that `machine-local get repos.claude_klabauter` returns a valid path.
    When machine-local is absent (coordinator-claude not installed), the probe is
    skipped as advisory: the shim is also absent, so the key is not needed.

    F10 fix: resolve the machine-local shim via _resolve_machine_local() rather than
    invoking the bare "machine-local" name. A bare-name subprocess.run() call fails
    with FileNotFoundError on Windows even when the shim IS installed and registered
    (Win32 CreateProcess does not apply PATHEXT to extension-less names, so
    machine-local.cmd at the settings-home/claude-home bin dirs is never found) — this
    previously made the probe report "machine-local not found" right after setup.py's
    own registration step had just PASSed.
    """
    machine_local_cmd = _resolve_machine_local()
    if machine_local_cmd is None:
        # machine-local absent → coordinator-claude not installed → key not needed.
        return _ProbeResult(
            probe=_REGISTRY_KEY_PROBE,
            status=_PASS,
            detail=(
                "machine-local not found — coordinator-claude absent; "
                "repos.claude_klabauter registration not required (shim not installed)."
            ),
            remediation="—",
            required=False,
            skipped=True,
        )

    # Read the registered value in-process via
    # coordinator_core.machine_resolver.registry_get (direct
    # registry.local.toml/registry.toml tomllib read) rather than shelling
    # out to `machine-local get` -- machine_local_cmd's resolvability (just
    # confirmed above) is the coordinator soft-dep gate; the value itself
    # is a registry read, not a shim-liveness check. Lazy, probe-local
    # import per this module's own negative-spec (see module docstring).
    try:
        candidate = Path(__file__).resolve().parent.parent
        candidate_str = str(candidate)
        added = candidate_str not in sys.path
        if added:
            sys.path.insert(0, candidate_str)
        try:
            from coordinator_core.machine_resolver import registry_get

            val = registry_get("repos.claude_klabauter")
        finally:
            if added:
                sys.path.remove(candidate_str)
    except Exception as exc:
        return _ProbeResult(
            probe=_REGISTRY_KEY_PROBE,
            status=_BROKEN,
            detail=f"Failed to read repos.claude_klabauter from the registry: {exc}",
            remediation="Verify machine-local is installed and the registry is readable.",
        )

    # machine-local is present — evaluate the registered value.
    if val:
        return _ProbeResult(
            probe=_REGISTRY_KEY_PROBE,
            status=_PASS,
            detail=f"repos.claude_klabauter → {val!r}",
            remediation="—",
            data={"registered_path": val},
        )

    return _ProbeResult(
        probe=_REGISTRY_KEY_PROBE,
        status=_BROKEN,
        detail="repos.claude_klabauter not registered in the machine-local registry.",
        remediation=(
            "Register the key by running scripts/setup.py, or manually: "
            "machine-local set repos.claude_klabauter /path/to/claude-klabauter"
        ),
    )


# ---------------------------------------------------------------------------
# Probe 3: import coordinator_core
# ---------------------------------------------------------------------------


_CORE_IMPORT_PROBE = "claude-klabauter.core.import"


def _ensure_core_importable(claude_klabauter_root: Path | None) -> None:
    """Put *claude_klabauter_root* on sys.path so any probe can import coordinator_core.

    Purpose: this is a PREREQUISITE, not a probe. The script runs as
    `python bin/claude-klabauter-doctor-probe.py`, so `sys.path[0]` is `<root>/bin`, never
    the repo root, and five probe functions import coordinator_core without
    inserting anything themselves (`_run_probe_version_sanity`,
    `_run_probe_launch_chain`, and the three warm probes that are not
    `roundtrip`). `_run_probe_invoke_smoke` shells out to a subprocess that
    resolves its own `sys.path[0]` from its own `cwd`, so it never depended on
    this insert. They used to free-ride on the insert
    `_run_probe_core_import` left behind, which was invisible and correct only
    because every probe always ran. Under a pre-run selector it stops being
    true: `--probe claude-klabauter.warm.generation` would report DEGRADED with no defect
    behind it. `run_probes` therefore calls this unconditionally, whatever the
    selection.

    Negative-spec:
      - Does NOT import anything — `claude-klabauter.core.import` is still the probe that
        reports whether the import works, and still the only thing that grades it.
      - Does NOT remove the entry afterwards: the whole point is that it outlives
        the call, for every later probe in the same process.

    Spec backlink: docs/plans/2026-08-19-the-selector-gates-before-the-run.md § C1.
    """
    if claude_klabauter_root is None:
        return
    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def _run_probe_core_import(claude_klabauter_root: Path) -> _ProbeResult:
    """Probe claude-klabauter.core.import — REQUIRED.

    Attempts to import coordinator_core + coordinator_core.lifecycle +
    coordinator_core.doctor_envelope. The sys.path setup this needs is
    `_ensure_core_importable`'s job, run as a prerequisite by `run_probes`
    before any probe — this function grades the import, it does not arrange it.
    """
    root_str = str(claude_klabauter_root)

    try:
        import coordinator_core          # noqa: F401
        import coordinator_core.lifecycle  # noqa: F401
        import coordinator_core.doctor_envelope  # noqa: F401
        return _ProbeResult(
            probe=_CORE_IMPORT_PROBE,
            status=_PASS,
            detail=(
                f"import coordinator_core, coordinator_core.lifecycle, "
                f"coordinator_core.doctor_envelope all succeeded from {root_str!r}."
            ),
            remediation="—",
            data={"import_root": root_str},
        )
    except ImportError as exc:
        return _ProbeResult(
            probe=_CORE_IMPORT_PROBE,
            status=_BROKEN,
            detail=f"import coordinator_core failed: {exc}",
            remediation=(
                f"Verify {root_str!r} contains a valid coordinator_core/ package "
                f"(coordinator_core/__init__.py must exist). "
                f"Diagnose: python3 -c \"import sys; sys.path.insert(0,'{root_str}'); "
                f"import coordinator_core\""
            ),
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_CORE_IMPORT_PROBE,
            status=_BROKEN,
            detail=(
                f"import coordinator_core raised {type(exc).__name__}: {exc}"
            ),
            remediation=(
                "Inspect coordinator_core for syntax errors or missing stdlib modules. "
                f"Run: python3 -c \"import sys; sys.path.insert(0,'{root_str}'); "
                "import coordinator_core\" for the full traceback."
            ),
        )


_DIALECT_GUARD_ARMED_PROBE = "claude-klabauter.dialect_guard.armed"


def _run_probe_dialect_guard_armed(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.dialect_guard.armed — OPTIONAL (required=False); never gating.

    Reports whether `coordinator_core/bash_guards/_dialect.py`'s PowerShell
    dialect guard is ARMED (`tree_sitter`/`tree_sitter_pwsh` importable and
    parsing) under the interpreter(s) that actually run bash guard hooks.
    That module's ImportError -> SILENT routing is legal at runtime by
    design — a guard may decline to rule on PowerShell rather than crash —
    but a DISARMED state going undetected forever is the defect this probe
    exists to end (docs/plans/2026-08-17-machine-first-install-surface.md
    § C1). This probe does NOT arm the guard — that's a later chunk's job
    (§ C2) — it only reports, loudly, what state the guard is in.

    Reuses `coordinator_core.bash_guards._dialect.probe_armed`, which
    exercises the guard's OWN code path (`_powershell_tokens`) rather than
    a second, parallel check — a disarmed result durably logs through the
    existing `_log_dialect_parser_unavailable` observability record, which
    (DR-402, C12) now also appends a `KIND_COLD_FAILED` row to
    `warm/telemetry.py`'s `degrade.jsonl` — the durable, attributable
    record DR-402 requires for a guard that proceeds rather than a
    settings-home log alone. This probe reads that same record's path
    (`dialect_parser_unavailable_log_path()`) for its remediation, rather
    than inventing a second one, and surfaces the degrade record's path
    alongside it in `data["durable_degrade_record"]`.

    Checks bare `python3` on PATH — what `hooks.json` runs every bash guard
    hook under — plus `sys.executable` (the interpreter running THIS
    probe), since a healthy result under the probe's own interpreter alone
    would miss the actual disarmed case on a box where that interpreter is
    a fallback venv, not what hooks actually run under.

    `required=False` throughout: a disarmed guard is the CURRENT, still-
    legal runtime state (until the install-chain arms it), so this must
    never punish an otherwise-healthy install to DEGRADED-required. status
    is PASS when every checked interpreter is ARMED, DEGRADED otherwise.
    """
    if claude_klabauter_root is None:
        return _ProbeResult(
            probe=_DIALECT_GUARD_ARMED_PROBE,
            status=_INFO,
            detail="Probe skipped — COORDINATOR_ENGINE_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve COORDINATOR_ENGINE_ROOT first (probe 1 remediation).",
            required=False,
            skipped=True,
        )

    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        from coordinator_core.bash_guards._dialect import (
            dialect_parser_unavailable_log_path,
            probe_armed,
        )
    except ImportError as exc:
        return _ProbeResult(
            probe=_DIALECT_GUARD_ARMED_PROBE,
            status=_INFO,
            detail=f"Probe skipped — coordinator_core.bash_guards._dialect not importable: {exc}",
            remediation="Resolve coordinator_core importability first (see claude-klabauter.core.import).",
            required=False,
            skipped=True,
        )

    interpreters: dict[str, str] = {"probe interpreter (sys.executable)": sys.executable}
    bare_python3 = shutil.which("python3")
    if bare_python3 and bare_python3 != sys.executable:
        interpreters["bare python3 on PATH (what hooks.json runs guard hooks under)"] = bare_python3

    checked: dict[str, Any] = {}
    disarmed: list[str] = []
    for label, interpreter in interpreters.items():
        armed, detail = probe_armed(interpreter, claude_klabauter_root)
        checked[label] = {"interpreter": interpreter, "armed": armed, "detail": detail}
        if not armed:
            disarmed.append(f"{label} ({interpreter}): {detail}")

    try:
        from coordinator_core.warm.telemetry import degrade_path

        durable_degrade_record = str(degrade_path(claude_klabauter_root))
    except Exception:  # noqa: BLE001 — advisory field, never fails the probe
        durable_degrade_record = None

    data = {
        "checked": checked,
        "durable_record": str(dialect_parser_unavailable_log_path()),
        "durable_degrade_record": durable_degrade_record,
    }

    if not disarmed:
        return _ProbeResult(
            probe=_DIALECT_GUARD_ARMED_PROBE,
            status=_PASS,
            detail=f"PowerShell dialect guard ARMED under all {len(interpreters)} checked interpreter(s).",
            remediation="—",
            required=False,
            data=data,
        )

    # `probe_armed`'s detail carries a `cause: missing-package` /
    # `cause: not-a-missing-package` tag (reviewer finding: a remediation
    # that always names the missing-grammar-package fix, regardless of
    # which of `_powershell_tokens`'s three SILENT cases fired, sends an
    # operator to fix the wrong thing once a parser regression -- not a
    # missing package -- is what's live).
    any_missing_package = any(
        "cause: missing-package" in entry["detail"]
        for entry in checked.values()
        if not entry["armed"]
    )
    remediation = (
        "Install tree_sitter and tree_sitter_pwsh under the named interpreter(s), "
        "e.g.: <interpreter> -m pip install tree_sitter tree_sitter_pwsh"
        if any_missing_package
        else "Cause is not a missing package (see the cause tag in each detail above) — "
        "inspect the reported cause before assuming a package install fixes this."
    )

    return _ProbeResult(
        probe=_DIALECT_GUARD_ARMED_PROBE,
        status=_DEGRADED,
        detail=(
            "PowerShell dialect guard DISARMED under: " + "; ".join(disarmed)
            + f". Durable record: {data['durable_record']}"
            + (
                f". Durable degrade record (DR-402): {data['durable_degrade_record']}"
                if data["durable_degrade_record"]
                else ""
            )
        ),
        remediation=remediation,
        required=False,
        data=data,
    )


_SETTINGS_HOME_COMPLETE_PROBE = "claude-klabauter.settings_home.complete"


def _run_probe_settings_home_complete(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.settings_home.complete — OPTIONAL (required=False); never gating.

    Reports whether `<settings-home>` (`~/.coordinator-claude-settings`) is
    actually populated, per `coordinator_core.install.settings_home_report`
    — the same enumeration+check `scripts/setup.py :: install_verify_settings_home`
    uses, so a cold re-check here and the install-time report line share one
    oracle rather than two that can drift apart (docs/plans/
    2026-08-17-machine-first-install-surface.md § C5).

    Every sub-check is a live `Path.exists()`/dir-listing read against the
    resolved settings-home path — never a read of a self-reported manifest
    the installer itself wrote (see that module's docstring for why). A
    disarmed/incomplete settings home is a real, currently-reachable state
    (a partial install, an interrupted `scripts/setup.py` run, a manually
    pruned `bin/`) rather than something this probe can fix — it only
    reports, loudly, what is actually on disk. required=False: an
    incomplete settings home does not represent a broken claude-klabauter *engine*,
    so this must never punish an otherwise-healthy install to a required
    DEGRADED.
    """
    if claude_klabauter_root is None:
        return _ProbeResult(
            probe=_SETTINGS_HOME_COMPLETE_PROBE,
            status=_INFO,
            detail="Probe skipped — COORDINATOR_ENGINE_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve COORDINATOR_ENGINE_ROOT first (probe 1 remediation).",
            required=False,
            skipped=True,
        )

    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        from coordinator_core._settings_home import settings_home
        from coordinator_core.install.settings_home_report import check_settings_home
    except ImportError as exc:
        return _ProbeResult(
            probe=_SETTINGS_HOME_COMPLETE_PROBE,
            status=_INFO,
            detail=(
                "Probe skipped — coordinator_core._settings_home / "
                f"coordinator_core.install.settings_home_report not importable: {exc}"
            ),
            remediation="Resolve coordinator_core importability first (see claude-klabauter.core.import).",
            required=False,
            skipped=True,
        )

    try:
        settings_home_path = settings_home()
    except RuntimeError as exc:
        return _ProbeResult(
            probe=_SETTINGS_HOME_COMPLETE_PROBE,
            status=_INFO,
            detail=f"Probe skipped — settings-home unresolvable: {exc}",
            remediation="Resolve COORDINATOR_SETTINGS_HOME/CLAUDE_HOME/HOME first.",
            required=False,
            skipped=True,
        )

    report = check_settings_home(settings_home_path, claude_klabauter_root)
    data = {
        "settings_home": str(settings_home_path),
        "fixed_missing": [m.label for m in report.fixed_missing],
        "forwarder_expected": report.forwarder_expected,
        "forwarder_present": report.forwarder_present,
        "forwarder_missing_count": len(report.forwarder_missing),
        "forwarder_unverified_count": len(report.forwarder_unverified),
        "forwarder_derivation_error": report.forwarder_derivation_error,
    }

    if report.complete:
        return _ProbeResult(
            probe=_SETTINGS_HOME_COMPLETE_PROBE,
            status=_PASS,
            detail=f"Settings home complete at {settings_home_path} "
                   f"({report.forwarder_present}/{report.forwarder_expected} bin/ forwarders verified).",
            remediation="—",
            required=False,
            data=data,
        )

    problems: list[str] = [m.label for m in report.fixed_missing]
    if report.forwarder_derivation_error is not None:
        problems.append(f"bin/ forwarder set undeterminable: {report.forwarder_derivation_error}")
    else:
        if report.forwarder_missing:
            problems.append(
                f"{len(report.forwarder_missing)}/{report.forwarder_expected} bin/ forwarders missing "
                f"(e.g. {', '.join(report.forwarder_missing[:5])})"
            )
        if report.forwarder_unverified:
            problems.append(
                f"{len(report.forwarder_unverified)}/{report.forwarder_expected} bin/ forwarders carry "
                f"another engine root's body (e.g. {', '.join(report.forwarder_unverified[:5])})"
            )

    return _ProbeResult(
        probe=_SETTINGS_HOME_COMPLETE_PROBE,
        status=_DEGRADED,
        detail=f"Settings home at {settings_home_path} is incomplete: " + "; ".join(problems),
        remediation="Re-run scripts/setup.py from claude-klabauter — its "
                     "'Install: settings-home completeness' step re-lands the missing members.",
        required=False,
        data=data,
    )


_ENTRYPOINTS_PATH_RESOLVED_PROBE = "claude-klabauter.entrypoints.path_resolved"


def _run_probe_entrypoints_path_resolved(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.entrypoints.path_resolved — OPTIONAL (required=False); never gating.

    Reports whether `coordinator-invoke` and `coordinator-cockpit-emit-schema`
    actually RESOLVE and EXECUTE on PATH after install
    (docs/plans/2026-08-17-machine-first-install-surface.md § C3). This is
    the "chain-walk" leg of that chunk's PATH-resolution probe — the
    "standalone script" leg is
    `coordinator_core.install.path_resolution_report`'s own CLI. Both call
    the same `check_entrypoint_path_resolution` oracle so the two never
    drift apart, matching this file's existing dialect-guard/settings-home
    probes' one-oracle-two-consumers shape.

    required=False: a resolution failure here means the settings-home `bin/`
    forwarders (a later install step than COORDINATOR_ENGINE_ROOT resolution) have not
    landed yet or PATH has not been refreshed in this shell — a real,
    currently-reachable partial-install state, not a broken engine.

    On Windows this probe's result reflects THIS process's inherited
    environment, not a live re-read of `HKCU\\Environment` — see
    `path_resolution_report._windows_caveat`'s docstring. A FAIL here on
    Windows inside a long-lived session is not conclusive; the caveat is
    surfaced in `data.platform_caveat` rather than silently omitted.
    """
    if claude_klabauter_root is None:
        return _ProbeResult(
            probe=_ENTRYPOINTS_PATH_RESOLVED_PROBE,
            status=_INFO,
            detail="Probe skipped — COORDINATOR_ENGINE_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve COORDINATOR_ENGINE_ROOT first (probe 1 remediation).",
            required=False,
            skipped=True,
        )

    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        from coordinator_core.install.path_resolution_report import check_entrypoint_path_resolution
    except ImportError as exc:
        return _ProbeResult(
            probe=_ENTRYPOINTS_PATH_RESOLVED_PROBE,
            status=_INFO,
            detail=f"Probe skipped — coordinator_core.install.path_resolution_report not importable: {exc}",
            remediation="Resolve coordinator_core importability first (see claude-klabauter.core.import).",
            required=False,
            skipped=True,
        )

    report = check_entrypoint_path_resolution()
    data: dict[str, Any] = {
        "platform": report.platform,
        "method": report.method,
        "checks": {
            c.name: {"resolved_path": c.resolved_path, "executed_ok": c.executed_ok, "detail": c.detail}
            for c in report.checks
        },
        "platform_caveat": report.platform_caveat,
        "transport_error": report.transport_error,
    }

    if report.transport_error is not None:
        return _ProbeResult(
            probe=_ENTRYPOINTS_PATH_RESOLVED_PROBE,
            status=_INFO,
            detail=f"Probe could not run: {report.transport_error}",
            remediation="Re-run manually: python3 -m coordinator_core.install.path_resolution_report",
            required=False,
            skipped=True,
            data=data,
        )

    if report.all_ok:
        return _ProbeResult(
            probe=_ENTRYPOINTS_PATH_RESOLVED_PROBE,
            status=_PASS,
            detail=f"Both entrypoints resolve and execute on PATH ({report.method}).",
            remediation="—",
            required=False,
            data=data,
        )

    failing = [c.name for c in report.checks if not (c.resolved_path and c.executed_ok)]
    detail = f"Entrypoint(s) not resolving/executing on PATH: {', '.join(failing)} ({report.method})."
    if report.platform_caveat:
        detail += f" CAVEAT: {report.platform_caveat}"
    return _ProbeResult(
        probe=_ENTRYPOINTS_PATH_RESOLVED_PROBE,
        status=_DEGRADED,
        detail=detail,
        remediation="Re-run scripts/setup.py from claude-klabauter, then open a NEW shell/session "
                     "before re-checking — a PATH write only takes effect in a shell started after it.",
        required=False,
        data=data,
    )


# ---------------------------------------------------------------------------
# Probe 5: resident debris detection
# ---------------------------------------------------------------------------


_RESIDENT_DEBRIS_PROBE = "claude-klabauter.resident.debris"


def _run_probe_resident_debris(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.resident.debris — REQUIRED; emits INFO on debris-found, never BROKEN/DEGRADED.

    Detects stale paths from the retired coordinator_core daemon:
      - <claude_klabauter_root>/.git/coordinator-service/  (endpoint sentinels)
      - /tmp/coordinator-svc-<uid>/              (socket directory)

    Debris is harmless post-DR-215 (no running process attaches to it).
    INFO class matches the analogous version-drift advisory — surfaces in
    data.warnings, does NOT drag the overall verdict below PASS.

    Negative-spec:
      - uid derived DYNAMICALLY via os.getuid(); never hardcoded.
      - Does NOT emit BROKEN or DEGRADED on normal detection paths — debris found →
        INFO, debris absent → PASS. Own-failure exception → BROKEN per probe-authoring
        invariant.
      - Remediation text for the git-sentinel path (under .git/) does NOT recommend an
        API chosen because this repo's own rm guard (check_destructive_rm) cannot see
        it. The guard's .git/ blanket deny is correct and deliberate; the remediation
        names that plainly, states the debris is safe to leave in place, and offers
        only routes an operator can actually execute (their own shell outside the
        agent's guarded Bash tool, or the guard's own documented override).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C1b
    """
    try:
        debris_paths: list[str] = []
        git_guarded_paths: list[str] = []

        # Check <claude_klabauter_root>/.git/coordinator-service/ sentinels.
        if claude_klabauter_root is not None:
            git_sentinel = claude_klabauter_root / ".git" / "coordinator-service"
            if git_sentinel.exists():
                debris_paths.append(str(git_sentinel))
                git_guarded_paths.append(str(git_sentinel))

        # Check /tmp/coordinator-svc-<uid>/ socket directory.
        # os.getuid() is POSIX-only; on Windows the path does not exist anyway.
        try:
            uid = os.getuid()
            tmp_socket_dir = Path(f"/tmp/coordinator-svc-{uid}")
            if tmp_socket_dir.exists():
                debris_paths.append(str(tmp_socket_dir))
        except AttributeError:
            # os.getuid absent on Windows — socket path convention differs; skip.
            pass

        if debris_paths:
            remediation_parts: list[str] = []

            unguarded_paths = [p for p in debris_paths if p not in git_guarded_paths]
            if unguarded_paths:
                cleanup = "; ".join(f"rm -rf {p!r}" for p in unguarded_paths)
                remediation_parts.append(
                    f"Optional cleanup for {', '.join(unguarded_paths)}: {cleanup}"
                )

            if git_guarded_paths:
                # Deliberately does NOT recommend shutil.rmtree or any other
                # API chosen because it is invisible to check_destructive_rm.
                # The .git/ blanket deny in that guard is correct and stays
                # exactly as written; this text says so plainly instead of
                # routing around it.
                sample_path = git_guarded_paths[0]
                remediation_parts.append(
                    f"Optional cleanup for {', '.join(git_guarded_paths)}: these paths sit "
                    "under .git/, where this repo's own guard "
                    "(coordinator_core/bash_guards/dispatch_checks.py check_destructive_rm) "
                    "blanket-denies agent-issued `rm` by design, not malfunction — the debris "
                    "is inert (no process attaches to it post-DR-215) and non-recurring, so "
                    "leaving it in place is a fully acceptable outcome. To remove it anyway: "
                    f"run `rm -rf {sample_path!r}` in an operator's own shell outside the "
                    "agent's guarded Bash tool, or set COORDINATOR_ALLOW_RM=1 in the "
                    "environment of the process that evaluates the guard before the guarded "
                    "command runs (the override is read from that process's own os.environ at "
                    "check time — an inline `COORDINATOR_ALLOW_RM=1 rm ...` prefix on the same "
                    "command line does not reach it)."
                )

            return _ProbeResult(
                probe=_RESIDENT_DEBRIS_PROBE,
                status=_INFO,
                detail=(
                    f"Stale resident debris from the retired coordinator_core daemon "
                    f"detected at {len(debris_paths)} path(s): "
                    f"{', '.join(debris_paths)}. "
                    "Debris is harmless (no running process attaches to it post-DR-215) "
                    "but can be cleaned up."
                ),
                remediation=" ".join(remediation_parts),
                data={"debris_paths": debris_paths},
            )

        return _ProbeResult(
            probe=_RESIDENT_DEBRIS_PROBE,
            status=_PASS,
            detail=(
                "No stale resident debris from the retired coordinator_core daemon found."
            ),
            remediation="—",
            data={"debris_paths": []},
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_RESIDENT_DEBRIS_PROBE,
            status=_BROKEN,
            detail=(
                f"Unexpected error in resident debris probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
        )


# ---------------------------------------------------------------------------
# Probe 5b: worktree bloat detection
# ---------------------------------------------------------------------------


_WORKTREE_BLOAT_DEFAULT_THRESHOLD_BYTES = 1073741824  # 1 GiB


def _format_bytes_human(size_bytes: int) -> str:
    """Format a byte count as a human-readable GiB/MiB string (e.g. '2.4 GiB')."""
    gib = size_bytes / (1024 ** 3)
    if gib >= 1:
        return f"{gib:.1f} GiB"
    mib = size_bytes / (1024 ** 2)
    return f"{mib:.1f} MiB"


_WORKTREE_BLOAT_PROBE = "claude-klabauter.worktree.bloat"


def _run_probe_worktree_bloat(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.worktree.bloat — REQUIRED; emits INFO on large-file-found, never BROKEN/DEGRADED.

    Defense-in-depth tripwire against runaway/junk files sitting untracked in the
    worktree. Walks the filesystem (not `git ls-files`) so untracked junk is caught —
    a git-tracked-only scan would miss exactly the motivating incident.

    Motivation: a 365 GB untracked junk file named `correct?*` (full of repeated `→`
    characters from a mis-quoted shell redirect) sat in the repo root undetected for
    days until found manually.

    Negative-spec:
      - Scans the FILESYSTEM worktree, not `git ls-files` — the motivating file was
        untracked; a git-tracked-only scan would have missed it entirely.
      - Does NOT emit BROKEN or DEGRADED on normal detection paths — large file(s)
        found → INFO, none found → PASS. Own-failure exception → BROKEN per
        probe-authoring invariant.
      - Prunes `.git/` from the walk — git internals are not worktree bloat and the
        object count would otherwise dominate the scan.
      - Does NOT follow symlinks — a symlink to a large file elsewhere must not be
        counted as worktree bloat.
      - Threshold is env-overridable via CLAUDE_KLABAUTER_DOCTOR_LARGE_FILE_BYTES; an unset or
        invalid value falls back to the 1 GiB default rather than crashing.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.
    """
    try:
        threshold_bytes = _WORKTREE_BLOAT_DEFAULT_THRESHOLD_BYTES
        env_override = os.environ.get("CLAUDE_KLABAUTER_DOCTOR_LARGE_FILE_BYTES")
        if env_override:
            try:
                threshold_bytes = int(env_override)
            except (TypeError, ValueError):
                threshold_bytes = _WORKTREE_BLOAT_DEFAULT_THRESHOLD_BYTES

        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_WORKTREE_BLOAT_PROBE,
                status=_PASS,
                detail=(
                    "Worktree bloat scan skipped — COORDINATOR_ENGINE_ROOT unresolved "
                    "(see claude-klabauter.root.resolve)."
                ),
                remediation="—",
                data={"large_files": [], "threshold_bytes": threshold_bytes},
            )

        large_files: list[dict[str, Any]] = []

        for dirpath, dirnames, filenames in os.walk(claude_klabauter_root, followlinks=False):
            # Prune .git/ — do not descend into it.
            dirnames[:] = [d for d in dirnames if d != ".git"]

            for filename in filenames:
                file_path = Path(dirpath) / filename
                try:
                    if file_path.is_symlink():
                        continue
                    st = os.lstat(file_path)
                    if st.st_size >= threshold_bytes:
                        try:
                            rel_path = str(file_path.relative_to(claude_klabauter_root))
                        except ValueError:
                            rel_path = str(file_path)
                        large_files.append(
                            {"path": rel_path, "size_bytes": st.st_size}
                        )
                except OSError:
                    # Permission denied or file vanished mid-walk — skip, don't abort.
                    continue

        threshold_human = _format_bytes_human(threshold_bytes)

        if large_files:
            listing = _capped_join(
                (
                    f"{f['path']} ({_format_bytes_human(f['size_bytes'])})"
                    for f in large_files
                ),
                sep="; ",
            )
            return _ProbeResult(
                probe=_WORKTREE_BLOAT_PROBE,
                status=_INFO,
                detail=(
                    f"{len(large_files)} file(s) exceed the {threshold_human} worktree "
                    f"bloat threshold: {listing}. These are likely runaway/junk files "
                    "(e.g. from a mis-quoted shell redirect) worth inspecting."
                ),
                remediation=(
                    "Inspect each flagged file and `rm` it if confirmed junk: "
                    + _capped_join(
                        (f"inspect {f['path']!r}" for f in large_files), sep="; "
                    )
                ),
                data={"large_files": large_files, "threshold_bytes": threshold_bytes},
            )

        return _ProbeResult(
            probe=_WORKTREE_BLOAT_PROBE,
            status=_PASS,
            detail=f"No worktree files exceed the {threshold_human} bloat threshold.",
            remediation="—",
            data={"large_files": [], "threshold_bytes": threshold_bytes},
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_WORKTREE_BLOAT_PROBE,
            status=_BROKEN,
            detail=(
                f"Unexpected error in worktree bloat probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
        )


# ---------------------------------------------------------------------------
# Probe 6: version sanity
# ---------------------------------------------------------------------------


_VERSION_SANITY_PROBE = "claude-klabauter.version.sanity"


def _run_probe_version_sanity(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.version.sanity — REQUIRED.

    Verifies three things:
      1. import coordinator_core succeeds.
      2. coordinator_core.lifecycle._compute_core_version() resolves (returns
         a hex SHA-256 string).
      3. Retired submodules are ABSENT — import coordinator_core.client should
         raise ImportError (ImportError = healthy; successful import = BROKEN).

    Intentional tradeoff — registered-vs-resolved root divergence:
    health._probe_import_graph formerly subprocess-imported coordinator_core from
    the machine-local-registered root, catching registered/resolved root divergence.
    That check is intentionally NOT reproduced here (decided tradeoff: marginal
    signal; the other health probes were genuine dead resident/UDS state making
    the whole op retire correct). This is a decided tradeoff, not an oversight.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C1b
    """
    try:
        # sys.path already set by `_ensure_core_importable`, run_probes' unconditional
        # prerequisite — NOT by an earlier probe (see that helper's docstring).

        # Check 1: import coordinator_core.
        try:
            import coordinator_core  # noqa: F401
        except ImportError as exc:
            return _ProbeResult(
                probe=_VERSION_SANITY_PROBE,
                status=_BROKEN,
                detail=f"import coordinator_core failed: {exc}",
                remediation=(
                    "Verify coordinator_core/ is present in COORDINATOR_ENGINE_ROOT and contains "
                    "__init__.py. See also claude-klabauter.core.import probe."
                ),
            )

        # Check 2: _compute_core_version() resolves.
        try:
            from coordinator_core.lifecycle import _compute_core_version  # Private: intentional — spec §C1b requires this specific version helper; update alongside lifecycle.py if it is renamed.
            version_hash = _compute_core_version()
        except (ImportError, Exception) as exc:
            return _ProbeResult(
                probe=_VERSION_SANITY_PROBE,
                status=_BROKEN,
                detail=(
                    f"coordinator_core.lifecycle._compute_core_version() failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                remediation=(
                    "Verify coordinator_core/lifecycle.py is present and intact. "
                    "Run: python3 -c \"from coordinator_core.lifecycle import "
                    "_compute_core_version; print(_compute_core_version())\""
                ),
            )

        # Check 3: retired submodule coordinator_core.client is ABSENT.
        # ImportError on import is HEALTHY; successful import is BROKEN.
        retired_dangling: list[str] = []
        try:
            import coordinator_core.client  # noqa: F401
            # Reaching here means the retired module is still importable — BROKEN.
            retired_dangling.append("coordinator_core.client")
        except ImportError:
            pass  # Expected — retired module correctly absent.
        except Exception:
            pass  # Other error importing client — treat as absent (import failed).

        if retired_dangling:
            return _ProbeResult(
                probe=_VERSION_SANITY_PROBE,
                status=_BROKEN,
                detail=(
                    f"Retired submodule(s) still importable (should raise ImportError): "
                    f"{', '.join(retired_dangling)}. "
                    "Stale .pyc or __init__ artifacts from the retired path are likely."
                ),
                remediation=(
                    "Remove stale artifacts from the retired submodule path: "
                    "git clean -fdx coordinator_core/client/ (or re-clone). "
                    "Verify: python3 -c 'import coordinator_core.client' should raise ImportError."
                ),
                data={"retired_dangling": retired_dangling},
            )

        return _ProbeResult(
            probe=_VERSION_SANITY_PROBE,
            status=_PASS,
            detail=(
                f"coordinator_core imports cleanly; _compute_core_version() resolved "
                f"(hash prefix: {version_hash[:12]!r}); retired submodule "
                f"coordinator_core.client correctly absent (ImportError). "
                "Note: registered-vs-resolved root divergence check intentionally not "
                "reproduced (decided tradeoff — see plan § C1b)."
            ),
            remediation="—",
            data={"version_hash_prefix": version_hash[:12]},
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_VERSION_SANITY_PROBE,
            status=_BROKEN,
            detail=(
                f"Unexpected error in version sanity probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
        )


# ---------------------------------------------------------------------------
# Probe 7: invoke dispatch smoke (OPTIONAL)
# ---------------------------------------------------------------------------


_INVOKE_SMOKE_PROBE = "claude-klabauter.invoke.smoke"

#: Repo-relative parts to an engine build stamp. One of SIX independent
#: copies of `coordinator_core.warm.skew`'s constant on this box (see
#: `coordinator_core/tests/test_engine_stamp_predicate_pin.py`'s module
#: docstring for the full list): this probe is stdlib-only at module scope
#: and must grade a tree it may not be able to import. Keep in sync by hand
#: if the stamp filename or location changes; that test is the drift guard.
_ENGINE_STAMP_RELATIVE_PARTS = ("coordinator_core", "_engine_stamp")

#: The one detail sentence both invoke probes emit when neither this tree nor
#: a published mirror can serve the dispatch.
_NO_DISPATCH_ROOT_DETAIL = (
    "no stamped engine root to dispatch from: {root} carries no build stamp "
    "(DR-331 § Decision; DR-315 § 2, the ruled state of a source clone), and no "
    "published mirror is registered and usable on this machine. Absence of a "
    "mirror clone is not evidence the engine is broken — cannot tell."
)

#: Remediation for the same case. Names a runnable registration, never a slash
#: command: this fires from a bare CLI before any session exists.
_NO_DISPATCH_ROOT_REMEDIATION = (
    "Register the published mirror: machine-local set repos.claude_klabauter "
    "<path-to-clone>, then re-run this probe."
)


def _engine_root_is_stamped(engine_root: Path) -> bool:
    """Is `engine_root` a published engine build (DR-331 — an engine root is a
    stamped build)?

    Pure local read, never raises. The two invoke probes call this as a
    pre-flight: an unstamped clone refuses every live dispatch at
    `ipc.py`'s stamp gate and at `invoke.__main__`'s no-cold-fallback arm,
    so measuring dispatch from one grades a ruled-unavailable path.

    Negative-spec:
      - Does NOT import `coordinator_core.warm.skew` — this file is
        stdlib-only at module scope and runs against trees whose import may
        itself be the thing under test (`claude-klabauter.core.import`).
      - Does NOT opt out of the stamp gate. `--allow-unstamped-dispatch` has
        two PM-sanctioned callers (`ipc.py :: allow_unstamped_dispatch`);
        an automated probe passing it would make an explicitly
        per-invocation carve-out ambient.
    """
    try:
        return engine_root.joinpath(*_ENGINE_STAMP_RELATIVE_PARTS).stat().st_size > 0
    except OSError:
        return False


def _resolve_dispatch_root(claude_klabauter_root: Path) -> Path | None:
    """The tree the two invoke probes should actually dispatch from, or None
    when no stamped root is reachable.

    `claude_klabauter_root` (COORDINATOR_ENGINE_ROOT) wins when it is itself a published
    build -- that is the klabauter case, where the installer's own root IS the
    engine. From a source clone it never can be (DR-331), and dispatching from
    it grades a refusal rather than the engine: so the probes fall through to
    the registered published mirror, which is the tree every operator's
    dispatch actually resolves to (DR-326 -- the engine does not resolve via
    claude-klabauter). Measured on machine-b 2026-08-22: `ping` from the mirror returns
    ok=true in 65ms end-to-end, from this clone it returns the
    no-cold-fallback refusal.

    `published_engine_mirror_path` is the SINGLE registered-and-usable
    resolver (`coordinator_core.engine_root`) -- imported lazily, never
    re-derived here, and never raising (its own fail-open contract). Its
    verdict is still stamp-checked: a registered path that is not a published
    build is not a dispatch root either.

    Negative-spec:
      - Does NOT fall back to `claude_klabauter_root` when the mirror is absent. A
        refusal measured from an unstamped clone is what made these probes
        permanently red; returning None so the caller reports inconclusive is
        the point.
      - Does NOT spawn anything to resolve (1.7ms, registry read only).
    """
    if _engine_root_is_stamped(claude_klabauter_root):
        return claude_klabauter_root

    try:
        from coordinator_core.engine_root import published_engine_mirror_path
        mirror = published_engine_mirror_path()
    except Exception:
        return None

    if not mirror:
        return None
    mirror_path = Path(mirror)
    return mirror_path if _engine_root_is_stamped(mirror_path) else None


def _run_probe_invoke_smoke(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.invoke.smoke — OPTIONAL (required=False).

    Runs the cheapest registered COMPUTE_ONLY op via the spawn-per-call
    command-type entrypoint:
        python3 -m coordinator_core.invoke ping '{}'

    PASS when the subprocess returns a well-formed result envelope
    (JSON dict with ok=True).  On spawn failure (interpreter/module absent),
    emits a SKIP result — never a bare crash.

    Pre-flight, `_engine_root_is_stamped`: an UNSTAMPED root skips rather
    than grades. DR-331 rules such a tree ineligible to dispatch, so the
    refusal envelope it returns is the ruling landing, not a broken
    entrypoint — grading it BROKEN made this probe permanently red in every
    source clone.

    Every failure arm's remediation names `dispatch_root`, never a bare
    command line. The same `python3 -m coordinator_core.invoke ping '{}'`
    pasted from a source clone returns that DR-331 refusal, so a rootless
    remediation hands the operator a reproduction of the ruling and points
    them at `ops/ping.py`, which is never the cause.

    Probe detail explicitly states: GREEN proves the entrypoint CAN dispatch,
    NOT that a live session IS connected.  Post-DR-215 there is no resident
    process; session-binding is not a concept here.

    Negative-spec:
      - Do NOT reframe this probe as a liveness ping or UDS round-trip check.
      - This is an invoke-dispatch smoke (spawn-per-call path), not UDS liveness.
      - Scope discipline: state/lessons/2026-07-04-out-of-band-doctor-green-live-session-mc.yaml

    Exception handling — two distinct failure modes:
      - FileNotFoundError (interpreter absent): SKIP + skipped=True. Probe never spawned;
        treated as gracefully absent, not a fault.
      - TimeoutExpired (spawn started but 30 s elapsed): BROKEN + skipped=False.
        Intentionally a harder signal — a spawn-per-call op that hangs for 30 s on an
        optional probe indicates a real execution problem distinct from "tool not installed."
        This elevates `overall` in the envelope, which is deliberate for this failure mode.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a SKIP verdict (not a crash), per the always-emit-parseable-verdict contract
    for optional probes.

    Question the sink cannot answer:
      Does the cold spawn-per-call dispatch path work RIGHT NOW, on THIS box?
      A recent `complete` row in the op census answers "did dispatch ever work",
      not "does it work at this instant" — a sink that has gone quiet (no
      recent traffic, e.g. between sessions or on a fresh clone) is
      indistinguishable in the census from a dispatch path that has broken,
      because passive telemetry has no signal for its own silence. This probe
      supplies that signal by actually spawning the path and reading its
      result, rather than reading a historical record of some other spawn.
      RETAINED (disposition: pln-2026-08-27-the-undeclared-harness-and-the-
      redundant-probes § C4) — this REVERSES the origin baton's expectation
      ("likely none") on evidence it did not have: the baton did not draw the
      distinction between "did it ever work" and "does it work now."

    Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C1b
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_INFO,
                detail="Cannot run invoke smoke — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        dispatch_root = _resolve_dispatch_root(claude_klabauter_root)
        if dispatch_root is None:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_INFO,
                detail=_NO_DISPATCH_ROOT_DETAIL.format(root=claude_klabauter_root),
                remediation=_NO_DISPATCH_ROOT_REMEDIATION,
                required=False,
                skipped=True,
                data={"engine_root": str(claude_klabauter_root), "dispatch_root": None},
            )

        try:
            result = subprocess.run(
                [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(dispatch_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_INFO,
                detail="Python interpreter not found on PATH; invoke smoke skipped.",
                remediation="Ensure python3 is on PATH.",
                required=False,
                skipped=True,
            )
        except subprocess.TimeoutExpired:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_BROKEN,
                detail=(
                    "python3 -m coordinator_core.invoke ping '{}' timed out after 30 s. "
                    "Spawn-per-call dispatch should complete in well under 1 s."
                ),
                remediation=(
                    f"Run manually from {dispatch_root}: "
                    "python3 -m coordinator_core.invoke ping '{}'. "
                    "Investigate hangs or missing dependencies."
                ),
                required=False,
            )

        if result.returncode != 0:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_BROKEN,
                detail=(
                    f"coordinator_core.invoke ping '{{}}' exited {result.returncode}. "
                    f"stderr: {result.stderr.strip()!r}"
                ),
                remediation=(
                    f"Run manually from {dispatch_root}: "
                    "python3 -m coordinator_core.invoke ping '{}'. "
                    "Verify claude-klabauter.core.import and claude-klabauter.version.sanity probes pass first."
                ),
                required=False,
            )

        stdout = result.stdout.strip()
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_BROKEN,
                detail=(
                    f"coordinator_core.invoke ping returned non-JSON output: {stdout!r}"
                ),
                remediation=(
                    f"Run manually from {dispatch_root}: "
                    "python3 -m coordinator_core.invoke ping '{}'. "
                    "Verify the invoke entrypoint emits a well-formed JSON result envelope."
                ),
                required=False,
            )

        # The invoke entrypoint emits a JSON-RPC envelope; the ping payload
        # ({"ok": true, "ts": ...}) is nested under "result". Accept a flat
        # payload too, for forward-compatibility.
        payload = envelope.get("result", envelope) if isinstance(envelope, dict) else None
        if not isinstance(payload, dict) or not payload.get("ok"):
            return _ProbeResult(
                probe=_INVOKE_SMOKE_PROBE,
                status=_BROKEN,
                detail=(
                    "invoke ping returned a malformed result envelope "
                    f"(expected result.ok=true): {str(envelope)[:200]!r}"
                ),
                remediation=(
                    f"Run manually from {dispatch_root}: "
                    "python3 -m coordinator_core.invoke ping '{}' and verify it emits a "
                    "JSON-RPC envelope with result.ok=true. "
                    "Check coordinator_core/ops/ping.py is intact."
                ),
                required=False,
            )

        return _ProbeResult(
            probe=_INVOKE_SMOKE_PROBE,
            status=_PASS,
            detail=(
                "dispatch smoke PASS: the coordinator_core.invoke ping op "
                f"returned a well-formed result envelope (ok=true, ts={payload.get('ts')!r}) "
                f"from {dispatch_root}. "
                "GREEN proves the entrypoint CAN dispatch (cheapest registered "
                "COMPUTE_ONLY op: ping) — NOT that a live session IS connected. "
                "Post-DR-215: no resident process; session-binding is not a concept here."
            ),
            remediation="—",
            required=False,
            data={
                "ok": payload.get("ok"),
                "ts": payload.get("ts"),
                "dispatch_root": str(dispatch_root),
            },
        )
    except Exception as exc:
        # Probe-authoring invariant: optional probe unexpected failure → SKIP envelope.
        return _ProbeResult(
            probe=_INVOKE_SMOKE_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in invoke smoke probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


# ---------------------------------------------------------------------------
# Probe 8: strategic self-description draft staleness (OPTIONAL)
# ---------------------------------------------------------------------------


_STRATEGIC_DRAFT_STALENESS_PROBE = "claude-klabauter.strategic.draft_staleness"


def _run_probe_strategic_draft_staleness(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.strategic.draft_staleness — OPTIONAL (required=False); never fatal.

    Nudge surface for the strategic.generate draft-consumption seam (DEC-4/DEC-5):
    checks whether <claude_klabauter_root>/state/strategic/self-description.draft.yaml exists
    and, if it does, whether its mtime is OLDER than the newest
    state/week-changelog/*.md entry's mtime (stale).

    A MISSING draft is NOT a fault — strategic.generate is invoked on demand, not
    scheduled; a fresh install or a repo that has never run generation has no draft
    and that is a healthy, unremarkable state. This probe emits SKIP (never
    DEGRADED/BROKEN) when the draft is absent.

    A STALE draft (older than the newest week-changelog entry) emits an INFO nudge —
    surfaces the one-line marker but never drags the overall verdict below PASS,
    matching the resident-debris / worktree-bloat advisory pattern.

    Negative-spec:
      - Does NOT treat a missing draft as DEGRADED or BROKEN — draft absence is the
        common, healthy case (DEC-5: nudge, not scheduler; no cron requires a draft
        to exist).
      - Does NOT emit BROKEN/DEGRADED on staleness — staleness is an INFO advisory,
        surfaced for the operator to act on, not a hard failure.
      - Does NOT write to state/strategic/ — read-only probe.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a SKIP verdict (not a crash), matching the optional-probe contract.

    Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C5(b)
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
                status=_INFO,
                detail="Cannot check draft staleness — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        draft_path = claude_klabauter_root / "state" / "strategic" / "self-description.draft.yaml"

        if not draft_path.exists():
            return _ProbeResult(
                probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
                status=_INFO,
                detail=(
                    "No strategic self-description draft present at "
                    f"{str(draft_path)!r} — not a fault; strategic.generate is invoked "
                    "on demand, not scheduled (DEC-5)."
                ),
                remediation=(
                    "Optional: run strategic.generate to produce a draft, then the DoE "
                    "refresh ceremony to reconcile it against the canonical "
                    "self-description.yaml."
                ),
                required=False,
                skipped=True,
                data={"draft_present": False},
            )

        try:
            draft_mtime = draft_path.stat().st_mtime
        except OSError as exc:
            return _ProbeResult(
                probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
                status=_INFO,
                detail=f"Draft present but stat() failed: {exc}",
                remediation="Investigate file permissions on state/strategic/.",
                required=False,
                skipped=True,
                data={"draft_present": True},
            )

        changelog_dir = claude_klabauter_root / "state" / "week-changelog"
        newest_changelog_mtime: float | None = None
        newest_changelog_name: str | None = None
        if changelog_dir.is_dir():
            for entry in changelog_dir.glob("*.md"):
                # HEADER.md is not a dated changelog entry — exclude it from the
                # "newest entry" comparison so an unrelated header edit does not
                # falsely flag the draft as stale.
                if entry.name == "HEADER.md":
                    continue
                try:
                    m = entry.stat().st_mtime
                except OSError:
                    # Permission denied or file vanished mid-scan — skip this entry;
                    # an OPTIONAL/INFO-only nudge, not worth aborting the probe over.
                    continue
                if newest_changelog_mtime is None or m > newest_changelog_mtime:
                    newest_changelog_mtime = m
                    newest_changelog_name = entry.name

        if newest_changelog_mtime is None:
            return _ProbeResult(
                probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
                status=_PASS,
                detail=(
                    f"Draft present at {str(draft_path)!r}; no dated "
                    "state/week-changelog/*.md entries found to compare against — "
                    "staleness check skipped, draft presence itself is healthy."
                ),
                remediation="—",
                required=False,
                data={"draft_present": True, "changelog_entries_found": False},
            )

        if draft_mtime < newest_changelog_mtime:
            return _ProbeResult(
                probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
                status=_INFO,
                detail=(
                    f"Strategic self-description draft ({str(draft_path)!r}) is OLDER "
                    f"than the newest state/week-changelog entry ({newest_changelog_name!r}) "
                    "— the draft may be stale."
                ),
                remediation=(
                    "Run strategic.generate to refresh the draft, then the DoE refresh "
                    "ceremony to reconcile it against the canonical self-description.yaml."
                ),
                required=False,
                data={
                    "draft_present": True,
                    "stale": True,
                    "newest_changelog_entry": newest_changelog_name,
                },
            )

        return _ProbeResult(
            probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
            status=_PASS,
            detail=(
                f"Strategic self-description draft ({str(draft_path)!r}) is at least as "
                f"recent as the newest state/week-changelog entry ({newest_changelog_name!r})."
            ),
            remediation="—",
            required=False,
            data={
                "draft_present": True,
                "stale": False,
                "newest_changelog_entry": newest_changelog_name,
            },
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_STRATEGIC_DRAFT_STALENESS_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in draft staleness probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_LAUNCH_CHAIN_PROBE = "claude-klabauter.launch.shim_chain"


def _launch_chain_claude_home() -> Path:
    return Path(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or str(Path.home())
    )


def _run_probe_launch_chain() -> _ProbeResult:
    """Probe claude-klabauter.launch.shim_chain — OPTIONAL (required=False); never gating.

    Answers "will bare `claude` load the coordinator plugin in a NEW shell?".

    Exists because that question had no automated answer, and its failure mode is
    silent by construction. On 2026-08-14 this box ran every session without the
    coordinator plugin for an extended period: the console-corruption workaround was
    to comment the shim block out of the operator's PowerShell profile, after which
    `claude` runs but resolves no plugin. Coordinator's own SessionStart hooks cannot
    catch that — they do not run when coordinator fails to load — so the check has to
    live out here, in a surface that runs without a session.

    Two distinct silent shapes are checked, both observed live:

    1. DISABLED profile block — the wired source line present but commented out. The
       shim is never dot-sourced, so `claude()` is never defined and bare `claude`
       is plain claude.exe.
    2. WRONG-DIALECT shim — a `.ps1` shim holding bash bytes. `maximalist` hardcoded
       the `.sh` template while the destination filename follows the shell family, so
       a native-Windows install wrote bash into `claude-doe-shim.ps1`. The profile
       block is present and correct in this shape, which is what makes it nastier
       than (1): every other check reports a healthy install.

    Shape-conditioned, per this file's own precedent (`_run_probe_vendored_schema_drift`):
    SKIPs when no DoE clone resolves, i.e. the marketplace population that never has
    this chain.

    `required` is set PER RESULT, not per probe, which is what lets this gate without
    punishing anyone else. The SKIP path returns `required=False` — a skipped REQUIRED
    probe reduces to DEGRADED (`_local_reduce_overall`), so a blanket `required=True`
    would degrade every marketplace install for lacking a chain it is not supposed to
    have. The real-failure paths return `required=True` and therefore exit 1 from
    `--step-zero`: on a box that HAS this chain, a shim that cannot define `claude()`
    means every session runs without coordinator, and an install that ends by calling
    itself healthy is the exact failure this probe exists to end.

    Spec backlink: docs/reference/interactive-launch-chain.md.
    """
    shell_dir = _launch_chain_claude_home() / ".claude" / "shell"
    is_windows = os.name == "nt"
    shim = shell_dir / ("claude-doe-shim.ps1" if is_windows else "claude-doe-shim.sh")
    data: dict[str, Any] = {"shim_path": str(shim), "platform": os.name}

    try:
        from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

        doe_root = coordinator_doe_root()
    except Exception:
        doe_root = None
    if not doe_root:
        return _ProbeResult(
            probe=_LAUNCH_CHAIN_PROBE,
            status=_INFO,
            detail="no DoE clone resolves — not the source-clone launch shape",
            remediation=(
                "Optional: check out the DoE-claude sibling repo (or set REPO_DOE_CLAUDE) "
                "to enable the launch-chain watch on this machine."
            ),
            required=False,
            skipped=True,
            data=data,
        )
    data["doe_root"] = str(doe_root)

    fix = (
        f"python3 <engine-clone>/coordinator/bin/gen-claude-doe-shim.py --shell "
        f"{'powershell' if is_windows else 'bash'} --template "
        f"{doe_root}/coordinator/templates/shell/"
        f"claude-doe-shim.{'ps1' if is_windows else 'sh'}.tmpl"
    )

    if not shim.is_file():
        return _ProbeResult(
            probe=_LAUNCH_CHAIN_PROBE,
            status=_DEGRADED,
            detail=f"shim absent: {shim} — bare `claude` loads no coordinator plugin",
            remediation=fix,
            required=True,
            data=data,
        )

    body = shim.read_text(encoding="utf-8", errors="replace")

    # Dialect: a PowerShell shim defines `function claude`; a bash shim defines
    # `claude()`. Checking for the DEFINITION (not a shebang) is what catches
    # verbatim-copied bytes of the other family, which carry no shebang mismatch.
    if is_windows:
        dialect_ok = "function claude" in body
        wrong = "claude()" in body and not dialect_ok
    else:
        dialect_ok = "claude()" in body
        wrong = "function claude" in body and not dialect_ok
    data["dialect_ok"] = dialect_ok
    if not dialect_ok:
        detail = f"{shim.name} defines no claude() — "
        detail += "it holds the OTHER shell family's bytes" if wrong else "unrecognised content"
        return _ProbeResult(
            probe=_LAUNCH_CHAIN_PROBE,
            status=_BROKEN,
            detail=detail + "; every session launches without coordinator",
            remediation=fix,
            required=True,
            data=data,
        )

    return _ProbeResult(
        probe=_LAUNCH_CHAIN_PROBE,
        status=_PASS,
        detail=f"{shim.name} present and defines claude()",
        remediation="",
        required=False,
        data=data,
    )


_VENDOR_DRIFT_PROBE = "claude-klabauter.schema.vendor_drift"


def _run_probe_vendored_schema_drift(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.schema.vendor_drift — OPTIONAL (required=False); never gating.

    Cadence surface for "has DoE moved since our vendored-schema pin?". Delegates the
    whole comparison to coordinator_core.frontmatter.schema_drift_watch, which globs
    every schema under coordinator_core/frontmatter/schemas/ and runs the non-gating
    check_schema_drift_advisory over each against DoE HEAD.

    Exists because that advisory had ZERO callers: claude-klabauter's vendored
    improvement-queue.schema.json drifted ~12h behind DoE on 2026-07-22 and the gap was
    only found when a sibling repo's CLI rejected a value valid on their surface. This
    probe is what makes the next one self-surface — its verdict reaches
    state/doctor-last-run.json, which DoE's /workday-start already reads.

    Two-oracle warning (the remediation below exists for this reason): THIS probe
    compares against DoE **HEAD**, while the GATING tamper-check
    (`check_schema_drift(..., ref=...)` in
    `coordinator_core/frontmatter/tests/test_schema_validate.py`) compares against a
    per-schema pinned SHA in `_QUEUE_SCHEMA_PINS`. Copying DoE's file in by hand
    satisfies this probe and breaks that gate — an installing agent did exactly that
    on 2026-07-28 (state/audits/2026-07-28-windows-install-dogfood-friction.md § F3).
    The remediation therefore names `bin/claude-klabauter-revendor-schema.py`, which moves the
    bytes and the pin together, and must never be reduced back to a `cp` instruction.

    Verdict mapping:
      DRIFT          -> DEGRADED (sentinel AMBER + hint) — re-vendor.
      INDETERMINATE  -> DEGRADED, worded as INDETERMINATE — the check could not run;
                        neither a drift claim nor a clean bill of health.
      UNRESOLVED     -> SKIP (required=False) — no DoE clone on this machine at all
                        (fresh install / CI without the sibling); not applicable, not
                        a fault. Surfaces in envelope.warnings / missing_optional.
      MATCH          -> PASS.

    Negative-spec:
      - NEVER BROKEN and never required=True — vendored-schema drift is an advisory
        re-vendor nudge, not a broken install, and must never fail --step-zero (whose
        exit code keys off REQUIRED probes only).
      - Does NOT report a clean PASS for a comparison it could not perform; an
        unreadable DoE clone is DEGRADED-as-indeterminate, never silent green, and
        never a false drift alarm.
      - Does NOT re-vendor anything — read-only.
      - Does NOT hard-depend on coordinator_core being importable: an ImportError
        degrades to SKIP (the probe must stay parseable on a broken tree).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become a SKIP
    verdict (not a crash), matching the optional-probe contract.

    Each `data["drifted"]` entry additionally carries `local_version`/`doe_version`
    (str | None, the two sides' top-level `x-schema-version` values, passed through
    from scan_vendored_schema_drift verbatim — never re-parsed here). Additive keys
    (2026-07-26, cross-repo schema-version surfacing); consumed by
    `_write_doctor_sentinel`'s `vendor_drift` sentinel key.

    Spec backlink: coordinator_core/frontmatter/schema_drift_watch.py module docstring;
    cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_VENDOR_DRIFT_PROBE,
                status=_INFO,
                detail="Cannot check vendored-schema drift — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        root_str = str(claude_klabauter_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        try:
            from coordinator_core.frontmatter.schema_drift_watch import (  # type: ignore[import]
                scan_vendored_schema_drift,
            )
        except Exception as exc:
            return _ProbeResult(
                probe=_VENDOR_DRIFT_PROBE,
                status=_INFO,
                detail=(
                    "Cannot import coordinator_core.frontmatter.schema_drift_watch "
                    f"from {root_str!r}: {type(exc).__name__}: {exc}"
                ),
                remediation="See claude-klabauter.core.import probe — the engine tree is not importable.",
                required=False,
                skipped=True,
            )

        report = scan_vendored_schema_drift()
        status_str = str(report.get("status") or "")
        summary = str(report.get("summary") or "")
        data = {
            "status": status_str,
            "doe_repo_path": report.get("doe_repo_path"),
            "checked": report.get("checked"),
            "drifted": [
                {
                    "schema": d.get("schema"),
                    "direction": d.get("direction"),
                    "local_version": d.get("local_version"),
                    "doe_version": d.get("doe_version"),
                }
                for d in report.get("drifted") or []
            ],
            "indeterminate": [d.get("schema") for d in report.get("indeterminate") or []],
        }

        if status_str == "UNRESOLVED":
            return _ProbeResult(
                probe=_VENDOR_DRIFT_PROBE,
                status=_INFO,
                detail=summary,
                remediation=(
                    "Optional: check out the DoE-claude sibling repo (or set REPO_DOE_CLAUDE) "
                    "to enable the vendored-schema drift watch on this machine."
                ),
                required=False,
                skipped=True,
                data=data,
            )

        if status_str == "DRIFT":
            return _ProbeResult(
                probe=_VENDOR_DRIFT_PROBE,
                status=_DEGRADED,
                detail=summary,
                remediation=(
                    "Re-vendor via the entrypoint, not by hand: "
                    "python3 bin/claude-klabauter-revendor-schema.py <name> --dry-run, then re-run "
                    "without --dry-run (add --reason '<why>' when it reports a pin move). "
                    "It writes the bytes AND updates the gating pin in "
                    "coordinator_core/frontmatter/tests/test_schema_validate.py"
                    "::_QUEUE_SCHEMA_PINS in one verified operation. Do NOT cp the file in "
                    "by hand: THIS probe compares against DoE HEAD, but the gating "
                    "tamper-check compares against that pinned SHA, so a hand copy turns "
                    "this probe green while breaking check_schema_drift. "
                    "A shape recorded as declined is refused: the entrypoint reports the "
                    "decline's reason and the commit that backed it out."
                ),
                required=False,
                data=data,
            )

        if status_str == "INDETERMINATE":
            return _ProbeResult(
                probe=_VENDOR_DRIFT_PROBE,
                status=_DEGRADED,
                detail=summary,
                remediation=(
                    "Vendored-schema drift is UNKNOWN, not clean. Verify the DoE-claude "
                    "clone is a readable git repo whose HEAD carries coordinator/schemas/, "
                    "then re-run the drift probe."
                ),
                required=False,
                data=data,
            )

        return _ProbeResult(
            probe=_VENDOR_DRIFT_PROBE,
            status=_PASS,
            detail=summary,
            remediation="—",
            required=False,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_VENDOR_DRIFT_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in vendored-schema drift probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_GENERATOR_STALENESS_PROBE = "claude-klabauter.generator.output_staleness"

#: A since-range comparison actually ran and produced a verdict.
_GENERATOR_STALENESS_COMPARED = ("STALE", "FRESH")

#: A DECLARED pair that owes a comparison and did not get one. Joins the
#: denominator: the pair exists, the contract exists, the comparison failed —
#: a real, closeable gap (give the artifact a readable stamp under the declared
#: `stamp_key` and it becomes comparable).
_GENERATOR_STALENESS_UNCOMPARABLE = (("UNSTAMPED", "unstamped"),)

#: A module carrying a resolved tracked write and no usable declaration. Not a
#: pair, so not in the denominator — what it owes is a declaration, not a
#: comparison, and folding it into a pair ratio misnames the remedy.
_GENERATOR_STALENESS_OWES_DECLARATION = ("UNDECLARED",)

#: Swept, but structurally outside the staleness contract — no fix to any of
#: these entries could ever raise the compared ratio, so counting them against
#: it reports a coverage gap that does not exist. Census of the live
#: population, docs/problems/2026-08-26-what-a-reinstall-on-the-mac-actually-hits.md
#: § 6: MUTATES_DECLARED is a declared corpus mutator whose output set is
#: data-dependent by design, so "stale relative to what?" has no answer;
#: WRITE_TARGET_UNRESOLVED is dominated by writers whose destination is a
#: caller-supplied parameter (`path`, `log_path`, `record_path`) that does not
#: exist at parse time; INDETERMINATE is an environmental failure (dirty peer
#: tree, unresolvable clone), not a missing contract.
_GENERATOR_STALENESS_EXEMPT_LABELS = (
    ("MUTATES_DECLARED", "declared corpus mutator"),
    ("WRITE_TARGET_UNRESOLVED", "write target not statically resolvable"),
    ("INDETERMINATE", "indeterminate"),
)


def _generator_staleness_coverage(data: dict) -> str:
    """Render an actionable coverage split for the staleness probe's `detail`.

    The denominator is DECLARED PAIRS, never the swept population. A ratio over
    the sweep counts every corpus mutator and every runtime-destination writer
    as uncovered, which reads as 96 missing contracts when the census says 89 of
    them are outside the contract by construction and nothing could ever move
    them into it. `coverage 4/100` that is really "4 of 8 pairs compared, 89
    exempt" names no action a reader can take.

    Four disjoint groups, each carrying its own remedy:
      - compared (STALE/FRESH) plus declared-but-uncomparable (UNSTAMPED) form
        the ratio — both are pairs, and an UNSTAMPED one is closeable.
      - UNDECLARED is reported as modules owing a declaration, beside the ratio
        rather than inside it: the remedy is a `GENERATES`/`MUTATES` block, not
        a stamp.
      - the exempt group is reported with its size and reasons, so a reader sees
        what was excluded and why rather than it being silently dropped.
    """
    counts: dict[str, int] = {}
    for entry in data.values():
        verdict = entry.get("verdict")
        counts[verdict] = counts.get(verdict, 0) + 1

    compared = sum(counts.get(name, 0) for name in _GENERATOR_STALENESS_COMPARED)
    uncomparable = sum(counts.get(name, 0) for name, _label in _GENERATOR_STALENESS_UNCOMPARABLE)
    declared_pairs = compared + uncomparable

    if declared_pairs:
        clauses = [f"coverage {compared}/{declared_pairs} declared pairs compared"]
    else:
        clauses = ["coverage: no declared pair carried a comparison"]

    pair_gaps = [
        f"{counts[name]} {label}"
        for name, label in _GENERATOR_STALENESS_UNCOMPARABLE
        if counts.get(name)
    ]
    owed = sum(counts.get(name, 0) for name in _GENERATOR_STALENESS_OWES_DECLARATION)
    if owed:
        pair_gaps.append(f"{owed} module(s) owe a declaration")
    if pair_gaps:
        clauses.append(", ".join(pair_gaps))

    exempt = sum(counts.get(name, 0) for name, _label in _GENERATOR_STALENESS_EXEMPT_LABELS)
    if exempt:
        reasons = ", ".join(
            f"{counts[name]} {label}"
            for name, label in _GENERATOR_STALENESS_EXEMPT_LABELS
            if counts.get(name)
        )
        clauses.append(f"{exempt} outside the staleness contract ({reasons})")

    # An unrecognised verdict belongs to no group above, so it can neither join
    # the ratio nor be called exempt -- surface it by name and count rather than
    # let it vanish from the breakdown silently.
    known = (
        set(_GENERATOR_STALENESS_COMPARED)
        | {name for name, _label in _GENERATOR_STALENESS_UNCOMPARABLE}
        | set(_GENERATOR_STALENESS_OWES_DECLARATION)
        | {name for name, _label in _GENERATOR_STALENESS_EXEMPT_LABELS}
    )
    unrecognised = sorted(
        (verdict, count) for verdict, count in counts.items() if verdict not in known
    )
    if unrecognised:
        clauses.append(
            ", ".join(f"{count} unrecognised ({verdict!r})" for verdict, count in unrecognised)
        )

    return "; ".join(clauses)


def _run_probe_generator_output_staleness(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.generator.output_staleness — OPTIONAL (required=False); never gating.

    Cadence surface for "did a generator move after the artifact it emits was last
    stamped?" (AC7). Delegates the whole comparison to
    coordinator_core.ops.check_generator_output_staleness.compute_all_staleness, which
    merges the local leg (C3, generator_provenance-declared pairs) and the vendored leg
    (C6, DoE's emission-stamped artifacts) into one verdict dict keyed by artifact path,
    each entry carrying `{"artifact", "verdict", "detail"}`. This probe carries that dict
    into `data` verbatim, per the shipped `_run_probe_vendored_schema_drift` pattern
    (same seam, same lazy-import-and-fold-to-SKIP discipline) — it never re-derives a
    verdict, only classifies the worst one present.

    THIS PROBE IS THE CADENCE: the `--triage` envelope already writes
    state/doctor-last-run.json, and DoE's /workday-start already reads it via
    coordinator_core.ops.check_claude_klabauter_doctor_sentinel — no second cadence hook, no new
    sentinel, no git hook. Staleness surfaces daily with NO change to any DoE-owned
    surface.

    Verdict mapping (worst-of across the returned dict):
      any STALE          -> DEGRADED (AMBER) — a declared pair has drifted.
      no STALE, any
        UNDECLARED/
        WRITE_TARGET_UNRESOLVED/
        INDETERMINATE    -> INFO/skipped=True — coverage gap or the check could not
                            resolve a verdict; excluded from `_local_reduce_overall`
                            so it cannot capture the daily sentinel's verdict/hint the
                            way a real STALE finding must (mirrors
                            `_run_probe_vendored_schema_drift`'s UNRESOLVED case).
                            WRITE_TARGET_UNRESOLVED is a standing ~230-record population
                            — it is folded into the same INFO/skipped path as UNDECLARED
                            precisely so it cannot pin the sentinel to AMBER or consume
                            a hint slot.
      no STALE/UNDECLARED/WRITE_TARGET_UNRESOLVED/INDETERMINATE, all
        FRESH/UNSTAMPED/
        MUTATES_DECLARED -> PASS. UNSTAMPED pairs are named in `detail` but do not
                            degrade the probe on their own — C2 is the chunk that closes
                            UNSTAMPED coverage, not this cadence wiring. MUTATES_DECLARED
                            is a HEALTHY declared state (a corpus mutator with no fixed
                            artifact, so no staleness contract), NOT a coverage gap — it
                            deliberately does not join `gap` and folds into this same PASS
                            path, closer to FRESH than to UNDECLARED for reporting purposes.
      empty dict (no
        declared pairs)  -> PASS — a repo with nothing declared has nothing stale.

    Every non-empty `detail` carries `_generator_staleness_coverage`'s split.
    Its denominator is DECLARED PAIRS, not the swept population: the sweep is
    dominated by declared corpus mutators and by writers whose destination is a
    caller-supplied parameter, neither of which any fix could move into the
    contract, so a ratio over the sweep reports a coverage gap that does not
    exist. Exempt entries are still counted and reasoned in the same line —
    excluded from the ratio, never dropped from the report.

    Negative-spec (AC7):
      - NEVER a non-zero process exit, a commit hook, or a gate — this probe REPORTS,
        it does not GATE. It fails no suite and blocks no ceremony.
      - Does NOT re-derive a verdict from raw git state; the checker's own dict is
        carried through unchanged.
      - Does NOT raise past this probe: an ImportError (checker not importable) or any
        unexpected exception folds into a SKIP/INFO result, matching the optional-probe
        contract shared with `_run_probe_vendored_schema_drift`.

    Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § Tasks
    id C5, § Acceptance Criteria AC7.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_INFO,
                detail="Cannot check generator/output staleness — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        root_str = str(claude_klabauter_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        try:
            from coordinator_core.ops.check_generator_output_staleness import (  # type: ignore[import]
                compute_all_staleness,
            )
        except Exception as exc:
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_INFO,
                detail=(
                    "Cannot import coordinator_core.ops.check_generator_output_staleness "
                    f"from {root_str!r}: {type(exc).__name__}: {exc}"
                ),
                remediation="See claude-klabauter.core.import probe — the engine tree is not importable.",
                required=False,
                skipped=True,
            )

        # Review: coordinator:code-reviewer — thread claude_klabauter_root through so this
        # resolves against the claude-klabauter repo, not compute_all_staleness's cwd-derived
        # git_root() fallback.
        report = compute_all_staleness(repo_root=claude_klabauter_root)

        def _verdict_str(entry: dict) -> str:
            verdict = entry.get("verdict")
            value = getattr(verdict, "value", None)
            return value if value is not None else str(verdict)

        data = {
            artifact: {
                "artifact": entry.get("artifact"),
                "verdict": _verdict_str(entry),
                "detail": entry.get("detail"),
            }
            for artifact, entry in report.items()
        }

        stale = {k: v for k, v in data.items() if v["verdict"] == "STALE"}
        gap = {
            k: v
            for k, v in data.items()
            if v["verdict"] in ("UNDECLARED", "WRITE_TARGET_UNRESOLVED", "INDETERMINATE")
        }

        if stale:
            names = _capped_join(sorted(str(k) for k in stale))
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_DEGRADED,
                detail=(
                    f"Generator/output pair(s) STALE: {names} "
                    f"({_generator_staleness_coverage(data)})"
                ),
                remediation=(
                    "A generator moved after the artifact it emits was last stamped. "
                    "Re-run the generator named in the pair's `detail` field to "
                    "refresh the artifact, then re-run this probe."
                ),
                required=False,
                data=data,
            )

        if gap:
            # Review: coordinator:code-reviewer — a coverage/resolution gap (no STALE
            # present) must not capture the daily sentinel's verdict/hint the way a
            # real STALE finding does; mirrors _run_probe_vendored_schema_drift's
            # UNRESOLVED -> INFO/skipped=True precedent, which _local_reduce_overall
            # excludes from the reduction.
            names = _capped_join(sorted(str(k) for k in gap))
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_INFO,
                detail=(
                    f"Generator/output staleness verdict incomplete "
                    f"({_generator_staleness_coverage(data)}) for: {names}"
                ),
                remediation=(
                    "UNDECLARED means a discovered generator writes a resolved tracked "
                    "path with no GENERATES declaration; WRITE_TARGET_UNRESOLVED means a "
                    "discovered generator writes through a path expression the sweep "
                    "could not resolve, so whether it emits a repo artifact is unknown; "
                    "INDETERMINATE means the check could not resolve a verdict (git "
                    "failure, unreadable peer clone, or a malformed sources pathspec). "
                    "See each entry's `detail` for the specific cause."
                ),
                required=False,
                skipped=True,
                data=data,
            )

        return _ProbeResult(
            probe=_GENERATOR_STALENESS_PROBE,
            status=_PASS,
            detail=(
                f"No STALE pair among {len(data)} swept generator(s) "
                f"({_generator_staleness_coverage(data)})."
                if data
                else "No declared generator/output pairs found."
            ),
            remediation="—",
            required=False,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_GENERATOR_STALENESS_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in generator/output staleness probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_COMMITMENTS_RECHECK_PROBE = "claude-klabauter.commitments.recheck"


def _run_probe_commitments_recheck(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.commitments.recheck — OPTIONAL (required=False); never gating.

    Cadence surface for "has any state/cross-repo-commitments/ record's evidence:
    resolved while status: is still open?". Delegates the whole re-resolution to
    coordinator_core.reconcile.commitments_recheck.recheck_commitments, which parses
    each record's C12a evidence: convention and resolves it live via sibling_fact.

    Exists because the ledger reproduced the exact staleness unstructured prose
    reproduces on its own — one record's own title read "(now satisfied)" beside
    status: open, thirteen days on, with nothing to ever re-check it. This probe is
    what makes the corpus self-surface — its verdict reaches state/doctor-last-run.json,
    which DoE's /workday-start already reads.

    Verdict mapping:
      any record actionable (evidence resolved truthy, status still "open")
                     -> DEGRADED (AMBER + the named record(s), a human applies the flip).
      none actionable (incl. "no evidence yet", "sibling unreadable", "already
      fulfilled")    -> PASS.

    Negative-spec:
      - NEVER BROKEN and never required=True — a stale commitment is a nudge, not a
        broken install, and must never fail --step-zero.
      - Does NOT flip, write, or append to any ledger record — read-only probe (D5;
        mirrors commitments_recheck.recheck_commitments's own NEVER AUTO-CLEARS
        contract).
      - Does NOT hard-depend on coordinator_core being importable: an ImportError
        degrades to SKIP.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become a SKIP
    verdict (not a crash), matching the optional-probe contract.

    Spec backlink: coordinator_core/reconcile/commitments_recheck.py module docstring;
    docs/plans/2026-07-26-structured-sibling-evidence-gates.md § C12b.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_COMMITMENTS_RECHECK_PROBE,
                status=_INFO,
                detail="Cannot recheck cross-repo commitments — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        root_str = str(claude_klabauter_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        try:
            from coordinator_core.reconcile.commitments_recheck import (  # type: ignore[import]
                recheck_commitments,
            )
        except Exception as exc:
            return _ProbeResult(
                probe=_COMMITMENTS_RECHECK_PROBE,
                status=_INFO,
                detail=(
                    "Cannot import coordinator_core.reconcile.commitments_recheck "
                    f"from {root_str!r}: {type(exc).__name__}: {exc}"
                ),
                remediation="See claude-klabauter.core.import probe — the engine tree is not importable.",
                required=False,
                skipped=True,
            )

        report = recheck_commitments(commitments_dir=claude_klabauter_root / "state" / "cross-repo-commitments")
        actionable = report.get("actionable") or []
        data = {
            "checked": report.get("checked"),
            "actionable": [
                {"entry": a.get("entry"), "title": a.get("title")} for a in actionable
            ],
        }

        if actionable:
            named = _capped_join(str(a.get("entry")) for a in actionable)
            return _ProbeResult(
                probe=_COMMITMENTS_RECHECK_PROBE,
                status=_DEGRADED,
                detail=(
                    f"{len(actionable)} cross-repo-commitments record(s) have evidence "
                    f"resolving truthy while status: is still open: {named}."
                ),
                remediation=(
                    "Review the named record(s) — if genuinely satisfied, flip status: "
                    "by hand (this probe never auto-mutates it)."
                ),
                required=False,
                data=data,
            )

        return _ProbeResult(
            probe=_COMMITMENTS_RECHECK_PROBE,
            status=_PASS,
            detail=(
                f"Checked {report.get('checked')} cross-repo-commitments record(s); "
                "none have resolved evidence outstanding against an open status."
            ),
            remediation="—",
            required=False,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_COMMITMENTS_RECHECK_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in cross-repo-commitments recheck probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_STABLE_PID_MISS_PROBE = "claude-klabauter.session.stable_pid_miss"


def _run_probe_stable_pid_miss(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.session.stable_pid_miss — OPTIONAL (required=False); never gating.

    Cadence surface for the K-006 F0 hazard (state/kill-ledger.md): a session whose
    ``stable_pid`` capture misses AND which runs only Bash/PowerShell for 30 minutes
    reads DEAD on the Layer-2 recency path in
    ``coordinator_core/session/liveness.py::session_live``, making it takeover- and
    reap-eligible while still alive. ``hooks.session_heartbeat`` — the sole discharge
    of that hazard — was deregistered 2026-08-16 (both DoE-side hook registrations).
    Deregistration was ruled safe ONLY because the miss rate measured 0% (10/10
    live + 354/354 archived sessions since 2026-08-10). That 0% did NOT hold:
    re-measured 2026-08-26 over the ledger's own window it was 65.3% (147/225),
    one cause (``posix-parent-miss:name-mismatch``), closed 2026-08-22 by
    ``session/core.py::init``'s POSIX leg (b). See ``state/kill-ledger.md``
    § K-006's CORRECTION paragraph — the cut stands, but its warrant moved from
    a number to two artifacts, and this probe is one of them.

    Delegates the whole scan to coordinator_core.session.stable_pid_watch
    .scan_stable_pid_misses, which mirrors session_live's own Layer-1 fall-through
    logic exactly (see that module's docstring) rather than re-deriving it — a
    ``stable_pid`` that is empty, OR present with BOTH ``stable_pid_lstart`` and
    ``stable_pid_start_epoch`` absent, is a miss.

    Threshold: ANY single miss alerts — not a rate, deliberately (see
    stable_pid_watch's module docstring for why a percentage threshold has no
    justifiable denominator here).

    Verdict mapping:
      MISS  -> DEGRADED (sentinel AMBER + the named session(s)/reason(s)) — F0 is
               live again; someone must investigate why capture regressed.
      CLEAN -> PASS.
      EMPTY -> SKIP (required=False) — no coordinator-sessions dir, or no session
               records in it (fresh install / CI); not applicable, not a fault.

    Negative-spec:
      - NEVER BROKEN and never required=True — this is a regression-net nudge, not a
        broken install, and must never fail --step-zero.
      - Does NOT restore the heartbeat, write last_activity, or mutate any session
        state — read-only, matching stable_pid_watch's own contract.
      - Does NOT hard-depend on coordinator_core being importable: an ImportError
        degrades to SKIP (the probe must stay parseable on a broken tree).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become a SKIP
    verdict (not a crash), matching the optional-probe contract.

    Spec backlink: state/kill-ledger.md § K-006;
    coordinator_core/session/stable_pid_watch.py module docstring.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_STABLE_PID_MISS_PROBE,
                status=_INFO,
                detail="Cannot check stable_pid capture — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        root_str = str(claude_klabauter_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

        try:
            from coordinator_core.session.stable_pid_watch import (  # type: ignore[import]
                STATUS_CLEAN,
                STATUS_EMPTY,
                STATUS_MISS,
                scan_stable_pid_misses,
            )
        except Exception as exc:
            return _ProbeResult(
                probe=_STABLE_PID_MISS_PROBE,
                status=_INFO,
                detail=(
                    "Cannot import coordinator_core.session.stable_pid_watch "
                    f"from {root_str!r}: {type(exc).__name__}: {exc}"
                ),
                remediation="See claude-klabauter.core.import probe — the engine tree is not importable.",
                required=False,
                skipped=True,
            )

        report = scan_stable_pid_misses()
        status_str = str(report.get("status") or "")
        summary = str(report.get("summary") or "")
        data = {
            "status": status_str,
            "checked": report.get("checked"),
            "misses": report.get("misses") or [],
        }

        if status_str == STATUS_EMPTY:
            return _ProbeResult(
                probe=_STABLE_PID_MISS_PROBE,
                status=_INFO,
                detail=summary,
                remediation="—",
                required=False,
                skipped=True,
                data=data,
            )

        if status_str == STATUS_MISS:
            # scan_stable_pid_misses' own `summary` names every miss — 223 session
            # ids on one line on this box. The count and the hazard name carry the
            # decision; the tail does not (full list stays in `data`).
            misses = list(report.get("misses") or [])
            named = _capped_join(
                f"{m.get('session')} ({m.get('reason')})" for m in misses
            )
            return _ProbeResult(
                probe=_STABLE_PID_MISS_PROBE,
                status=_DEGRADED,
                detail=(
                    f"{len(misses)} of {report.get('checked')} session(s) missing "
                    f"stable_pid capture — F0 hazard (K-006) is live again: {named}."
                ),
                remediation=(
                    "One or more current sessions are missing stable_pid capture — the "
                    "F0 hazard K-006 left undischarged is reachable again. Read "
                    "stable_pid_capture in the named session(s)' meta.json, then the leg "
                    "it names in coordinator_core/session/core.py::init."
                ),
                required=False,
                data=data,
            )

        return _ProbeResult(
            probe=_STABLE_PID_MISS_PROBE,
            status=_PASS,
            detail=summary,
            remediation="—",
            required=False,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_STABLE_PID_MISS_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in stable_pid miss probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


# ---------------------------------------------------------------------------
# Probe 9: claude-klabauter-live-root pointer presence (Windows-portability, DEC-2)
# ---------------------------------------------------------------------------


def _resolve_settings_home() -> Path:
    """Resolve the coordinator settings-home root via env/home precedence.

    Mirrors the ladder used throughout the coordinator tri-plane
    (coordinator_core._settings_home.settings_home() and _resolve_machine_local()
    above): COORDINATOR_SETTINGS_HOME, falling back to CLAUDE_HOME, falling back
    to HOME, falling back to the platform home directory (expanduser —
    USERPROFILE on Windows, the passwd entry on POSIX), with
    `.coordinator-claude-settings` appended to the fall-back rungs. Reimplemented locally (rather than importing
    coordinator_core) so this probe works even when COORDINATOR_ENGINE_ROOT/coordinator_core is unresolved —
    the pointer-presence check must be runnable independent of probe 1's outcome.
    """
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".coordinator-claude-settings"


_ENGINE_TARGET_ROLLOUT_PROBE = "claude-klabauter.registry.engine_target_rollout"


def _run_probe_engine_target_rollout() -> _ProbeResult:
    """Probe claude-klabauter.registry.engine_target_rollout — REQUIRED=False, INFO-only.

    C8 (docs/plans/2026-08-16-one-engine-for-the-whole-box.md), AC20: an
    unreadable/absent `engine.target` never diverts a session's resolved
    engine (that read-site default lives in
    `coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::resolve_engine_target`,
    untouched here) — but AC20 also disposes of the "silent rollout no-op"
    day-one consequence by making the absent state MEANINGFUL: since C8's
    installer (`scripts/setup.py::register_claude_klabauter_root`) now writes
    `engine.target` on the SAME pass that registers
    `repos.claude_klabauter`, a machine holding the mirror key with no
    `engine.target` can only be one thing — not yet rolled onto this
    chunk's installer, never a deliberate opt-out (absence is never an
    opt-out, since opting out is itself a WRITTEN value). This probe
    enumerates that set.

    Verdict shape — reports, never blocks (C8's body, verbatim):
      - `repos.claude_klabauter` unregistered -> INFO, nothing to enumerate
        (a klabauter mirror is the precondition; a plain claude-klabauter-only box
        with no mirror is out of scope for this probe).
      - `repos.claude_klabauter` registered AND `engine.target` present ->
        INFO, rolled out.
      - `repos.claude_klabauter` registered AND `engine.target` absent ->
        INFO (never DEGRADED/BROKEN) naming the not-yet-rolled-out box —
        `_INFO` is excluded from the worst-of rank reduction
        (`_local_reduce_overall`), so this can never move the fleet verdict
        off PASS by itself, matching the doctor-probe design's status
        vocabulary contract.

    Negative-spec: does not write `engine.target` itself (read-only
    diagnostic, matching every other probe in this module) and does not
    interpret `engine.target`'s VALUE — presence is the only signal AC20
    depends on.

    Probe-authoring invariant: wraps all logic so unexpected exceptions
    become an INFO/skipped envelope, never an unhandled crash.
    """
    probe_id = _ENGINE_TARGET_ROLLOUT_PROBE
    try:
        candidate = Path(__file__).resolve().parent.parent
        candidate_str = str(candidate)
        added = candidate_str not in sys.path
        if added:
            sys.path.insert(0, candidate_str)
        try:
            from coordinator_core.machine_resolver import registry_get

            klabauter_mirror = registry_get("repos.claude_klabauter")
            engine_target = registry_get("engine.target")
        finally:
            if added:
                sys.path.remove(candidate_str)
    except Exception as exc:
        return _ProbeResult(
            probe=probe_id,
            status=_INFO,
            detail=f"Unexpected error reading the registry: {type(exc).__name__}: {exc}",
            remediation="—",
            required=False,
            skipped=True,
        )

    if not klabauter_mirror:
        return _ProbeResult(
            probe=probe_id,
            status=_INFO,
            detail="repos.claude_klabauter not registered — no mirror to check engine.target rollout against.",
            remediation="—",
            required=False,
            data={"repos.claude_klabauter": None, "engine.target": engine_target or None},
        )

    if engine_target:
        return _ProbeResult(
            probe=probe_id,
            status=_INFO,
            detail=f"engine.target = {engine_target!r} — install-class default rolled out.",
            remediation="—",
            required=False,
            data={"repos.claude_klabauter": klabauter_mirror, "engine.target": engine_target},
        )

    return _ProbeResult(
        probe=probe_id,
        status=_INFO,
        detail=(
            "repos.claude_klabauter is registered with no engine.target — "
            "not yet rolled out."
        ),
        remediation=(
            "Run python3 scripts/setup.py to write the install-class engine.target "
            "default (candidate for a claude-klabauter developer box, main for a "
            "claude-klabauter install)."
        ),
        required=False,
        data={"repos.claude_klabauter": klabauter_mirror, "engine.target": None},
    )


_ROOT_CHANNELS_PROBE = "claude-klabauter.root.channels_reconciled"


def _run_probe_root_channels_reconciled(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.root.channels_reconciled — do the channels that each
    answer "where is root X?" agree, and does each name a path that is here?

    Consumes the shared oracle `coordinator_core.root_channel_reconcile`; the
    other consumer is that module's own CLI, which is the runnable cold-path
    remediation `warm.client` names. This probe is the chain-walk half.

    Distinct from `claude-klabauter.root.pointer`, which compares ONE pointer file
    against the ladder answer this probe script itself resolved. This one
    compares the channels against EACH OTHER, across every root the box
    writes down more than once — the split that made a correct
    `repos.claude_klabauter` and a foreign-platform
    `.claude-klabauter-root` coexist silently until every live op failed.

    Verdict shape:
      - Every channel agrees, or only one channel spoke -> PASS.
      - Two channels name different paths, or a channel names a path that is
        not a directory here -> DEGRADED. Not BROKEN: a resolver ladder still
        answers, and which of the two paths is the real one is an operator
        decision this probe must not guess at.

    Negative-spec:
      - Does NOT repair or normalise any channel. One of them lives in an
        operator's git-synced meta-repo, shared across machines.
      - Does NOT cite a decision record. Channels disagreeing is a data
        condition; a ruling reference would send the reader to a decision
        record instead of to the file named in the detail.
    """
    try:
        from coordinator_core.root_channel_reconcile import disagreement_message, reconcile_all

        reports = reconcile_all()
        message = disagreement_message(reports)
        data = {
            report.root: {
                channel.origin: {"value": channel.value, "exists": channel.exists}
                for channel in report.channels
            }
            for report in reports
        }

        if message is None:
            return _ProbeResult(
                probe=_ROOT_CHANNELS_PROBE,
                status=_PASS,
                detail="every root-resolution channel agrees and names a path that exists.",
                remediation="—",
                required=False,
                data=data,
            )

        return _ProbeResult(
            probe=_ROOT_CHANNELS_PROBE,
            status=_DEGRADED,
            detail=message,
            remediation=(
                "Correct the channel named above, then re-check with "
                "python3 -m coordinator_core.root_channel_reconcile."
            ),
            required=False,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_ROOT_CHANNELS_PROBE,
            status=_INFO,
            detail=f"Unexpected error in root channel reconciliation probe: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_ROOT_POINTER_PROBE = "claude-klabauter.root.pointer"


def _run_probe_root_pointer(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.root.pointer — REQUIRED=False (WARN, not hard FAIL) on absence.

    Checks that the claude-klabauter-live-root pointer file exists at
    <settings-home>/machine-local/.claude-klabauter-live-root and that its content matches the
    resolved COORDINATOR_ENGINE_ROOT.

    Rationale (DEC-2, F17): without the pointer, COORDINATOR_ENGINE_ROOT resolution falls back to a
    bash subprocess (coordinator-claude-klabauter-root.sh) with a 5 s timeout — on Windows this
    bash-fallback subprocess spawn is the dominant per-invoke latency cost, hanging ~5 s
    on every hook/invoke round-trip when the pointer is absent.

    Verdict shape:
      - Pointer present AND content matches resolved root -> PASS.
      - Pointer present but content diverges from resolved root -> DEGRADED (actionable,
        not hard FAIL) — stale pointer, same remediation as absent.
      - Pointer absent -> DEGRADED (actionable, not hard FAIL) — remediation points at
        the install-time writer (gen-claude-klabauter-root-pointer.py).
      - claude_klabauter_root is None (probe 1 unresolved) -> pointer existence is still checked;
        content-match is skipped (nothing to compare against) but presence alone is
        reported PASS/DEGRADED.

    Negative-spec:
      - Does NOT write the pointer file — read-only diagnostic; the writer is a
        separate install-time step (gen-claude-klabauter-root-pointer.py, DoE-claude C1b).
      - Does NOT emit BROKEN/hard-fail on absence — a missing pointer degrades
        per-invoke latency, it does not break correctness (the ladder fallback still
        resolves COORDINATOR_ENGINE_ROOT, just slowly).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14
    """
    try:
        settings_home = _resolve_settings_home()
        pointer_path = settings_home / "machine-local" / ".claude-klabauter-live-root"

        if not pointer_path.exists():
            return _ProbeResult(
                probe=_ROOT_POINTER_PROBE,
                status=_DEGRADED,
                detail=(
                    f"claude-klabauter-live-root pointer absent at {str(pointer_path)!r}. Without it, "
                    "per-invoke COORDINATOR_ENGINE_ROOT resolution falls back to a bash subprocess "
                    "with a 5 s timeout — this is the dominant latency cost on Windows "
                    "per-invoke/hook round-trips."
                ),
                remediation=(
                    "Run the install-time pointer writer (gen-claude-klabauter-root-pointer.py) to "
                    f"populate {str(pointer_path)!r} with the resolved COORDINATOR_ENGINE_ROOT path."
                ),
                required=False,
                data={"pointer_path": str(pointer_path), "present": False},
            )

        try:
            pointer_content = pointer_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            return _ProbeResult(
                probe=_ROOT_POINTER_PROBE,
                status=_DEGRADED,
                detail=f"claude-klabauter-live-root pointer present but unreadable: {exc}",
                remediation=(
                    "Check permissions on the pointer file, or re-run the install-time "
                    f"pointer writer (gen-claude-klabauter-root-pointer.py) to regenerate {str(pointer_path)!r}."
                ),
                required=False,
                data={"pointer_path": str(pointer_path), "present": True},
            )

        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_ROOT_POINTER_PROBE,
                status=_PASS,
                detail=(
                    f"claude-klabauter-live-root pointer present at {str(pointer_path)!r} "
                    f"(content: {pointer_content!r}); content-match skipped — "
                    "COORDINATOR_ENGINE_ROOT unresolved (see claude-klabauter.root.resolve)."
                ),
                remediation="—",
                required=False,
                data={"pointer_path": str(pointer_path), "present": True, "content": pointer_content},
            )

        # Normalize both sides (str, no trailing separators) before comparing —
        # avoid false-positive mismatches from trailing slashes / path separator style.
        resolved_str = str(claude_klabauter_root).rstrip("/\\")
        pointer_str = pointer_content.rstrip("/\\")

        if pointer_str != resolved_str:
            try:
                pointer_resolved = Path(pointer_content).resolve()
                same_target = pointer_resolved == claude_klabauter_root.resolve()
            except Exception:
                same_target = False
            if not same_target:
                return _ProbeResult(
                    probe=_ROOT_POINTER_PROBE,
                    status=_DEGRADED,
                    detail=(
                        f"claude-klabauter-live-root pointer content {pointer_content!r} does not match "
                        f"resolved COORDINATOR_ENGINE_ROOT {resolved_str!r} — stale pointer."
                    ),
                    remediation=(
                        "Re-run the install-time pointer writer (gen-claude-klabauter-root-pointer.py) "
                        f"to refresh {str(pointer_path)!r} with the current COORDINATOR_ENGINE_ROOT."
                    ),
                    required=False,
                    data={
                        "pointer_path": str(pointer_path),
                        "present": True,
                        "content": pointer_content,
                        "resolved_root": resolved_str,
                    },
                )

        return _ProbeResult(
            probe=_ROOT_POINTER_PROBE,
            status=_PASS,
            detail=(
                f"claude-klabauter-live-root pointer present at {str(pointer_path)!r} and matches "
                f"resolved COORDINATOR_ENGINE_ROOT {resolved_str!r}."
            ),
            remediation="—",
            required=False,
            data={
                "pointer_path": str(pointer_path),
                "present": True,
                "content": pointer_content,
                "resolved_root": resolved_str,
            },
        )
    except Exception as exc:
        # Probe-authoring invariant: optional probe unexpected failure -> SKIP envelope.
        return _ProbeResult(
            probe=_ROOT_POINTER_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in root pointer probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


# ---------------------------------------------------------------------------
# publish provenance staleness — docs/plans/2026-08-19-the-published-engine-
# says-what-it-was-published-from.md (C2). Reads the machine-local record
# publish.py's `write_publish_provenance_record` writes at round end (C1)
# and reports how far claude-klabauter's own HEAD has moved past the sha that record
# says was actually published — the read half of the DR-326 stale-dispatch
# fix (a warm-client fix landed, dispatch flipped to the published build an
# hour later, and the only signal available — `git log` — could not
# distinguish "running the fix" from "running an hour-old build").
# ---------------------------------------------------------------------------
_PUBLISH_PROVENANCE_PROBE = "claude-klabauter.publish.provenance"


def _run_probe_publish_provenance(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.publish.provenance — REQUIRED=False (WARN, not hard FAIL).

    Reads the C1 record (`<settings-home>/machine-local/publish-provenance.json`)
    for a row whose recorded toplevel matches this claude-klabauter checkout, and
    compares its pinned sha against claude-klabauter's own current HEAD via a single
    `git rev-list --count <sha>..HEAD` (Anti-scope 5 — one git call for the
    whole answer, never per row; the amplification gate at
    coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py governs).

    Verdict shape:
      - Record absent, or present but naming no row for this claude-klabauter toplevel
        -> INFO (step-zero `warn`), "not recorded". A box that has never
        published is the normal state of a fresh install, and publishing is
        a workstream act with its own gates that the installer printing this
        line cannot perform (guard-messaging.md § Key Patterns: only offer
        remediation the current reader can run). AC5 still holds — a
        never-published box is never reported as current, it is reported as
        not recorded.
      - Record present but unreadable -> DEGRADED, "unknown". A write landed
        and then broke; that is a fault, not a fresh box.
      - Recorded sha not resolvable in claude-klabauter's own history (e.g. a stale
        record from before a history rewrite) -> DEGRADED, "unknown".
      - Recorded sha IS claude-klabauter's current HEAD -> PASS.
      - Recorded sha resolves but is behind HEAD -> DEGRADED naming the
        commit distance.

    Negative-spec: does not write the record — read-only diagnostic, mirrors
    every other probe in this module. Does not resolve or interpret any
    OTHER toplevel in the record (e.g. a sibling repo's own publish row);
    this probe answers for claude-klabauter's own checkout only.

    Probe-authoring invariant: wraps all logic so unexpected exceptions
    become a DEGRADED-but-never-crashing envelope.
    """
    probe_id = _PUBLISH_PROVENANCE_PROBE
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=probe_id,
                status=_INFO,
                detail="Cannot check publish provenance — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        settings_home = _resolve_settings_home()
        record_path = settings_home / "machine-local" / "publish-provenance.json"

        if not record_path.exists():
            return _ProbeResult(
                probe=probe_id,
                status=_INFO,
                detail=(
                    f"publish provenance not recorded — no record at {str(record_path)!r}; "
                    "this box has not completed a percolate round, which is the normal "
                    "state until it publishes."
                ),
                remediation=(
                    "None at install time. A percolate round writes this record at round "
                    "end; publish first, then this probe reads it."
                ),
                required=False,
                data={"record_path": str(record_path), "present": False},
            )

        try:
            record_raw = record_path.read_text(encoding="utf-8")
            record = json.loads(record_raw)
            rows = record.get("rows", {}) if isinstance(record, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _ProbeResult(
                probe=probe_id,
                status=_DEGRADED,
                detail=f"publish provenance unknown — record at {str(record_path)!r} unreadable: {exc}",
                remediation="Re-run a percolate round to regenerate the record.",
                required=False,
                data={"record_path": str(record_path), "present": True, "readable": False},
            )

        toplevel_result = subprocess.run(
            ["git", "-C", str(claude_klabauter_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if toplevel_result.returncode != 0:
            return _ProbeResult(
                probe=probe_id,
                status=_DEGRADED,
                detail=(
                    f"publish provenance unknown — could not resolve claude-klabauter's own git "
                    f"toplevel: {toplevel_result.stderr.strip()}"
                ),
                remediation="Verify COORDINATOR_ENGINE_ROOT points at a git work tree.",
                required=False,
                data={"record_path": str(record_path), "present": True},
            )
        claude_klabauter_toplevel = str(Path(toplevel_result.stdout.strip()).resolve())

        published_sha: str | None = None
        published_row: str | None = None
        for row_name, row_data in rows.items():
            if not isinstance(row_data, dict) or not row_data.get("published"):
                continue
            toplevels = row_data.get("toplevels", {})
            if not isinstance(toplevels, dict):
                continue
            for toplevel_key, sha in toplevels.items():
                try:
                    normalized = str(Path(toplevel_key).resolve())
                except OSError:
                    normalized = toplevel_key
                if normalized == claude_klabauter_toplevel and isinstance(sha, str) and sha:
                    published_sha = sha
                    published_row = row_name
                    break
            if published_sha is not None:
                break

        if published_sha is None:
            return _ProbeResult(
                probe=probe_id,
                status=_INFO,
                detail=(
                    f"publish provenance not recorded — no published row in "
                    f"{str(record_path)!r} names this claude-klabauter checkout "
                    f"({claude_klabauter_toplevel!r}); this checkout has not published yet, which "
                    "is the normal state until it does."
                ),
                remediation=(
                    "None at install time. A percolate round publishing from this "
                    "checkout records the provenance this probe reads."
                ),
                required=False,
                data={"record_path": str(record_path), "present": True, "claude_klabauter_toplevel": claude_klabauter_toplevel},
            )

        distance_result = subprocess.run(
            ["git", "-C", str(claude_klabauter_root), "rev-list", "--count", f"{published_sha}..HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if distance_result.returncode != 0:
            return _ProbeResult(
                probe=probe_id,
                status=_DEGRADED,
                detail=(
                    f"publish provenance unknown — published sha {published_sha!r} (row "
                    f"{published_row!r}) is not resolvable in claude-klabauter's own history: "
                    f"{distance_result.stderr.strip()}"
                ),
                remediation="Re-run a percolate round to refresh the record.",
                required=False,
                data={
                    "record_path": str(record_path),
                    "published_sha": published_sha,
                    "published_row": published_row,
                },
            )

        distance_str = distance_result.stdout.strip()
        distance = int(distance_str) if distance_str.isdigit() else None
        if distance is None:
            return _ProbeResult(
                probe=probe_id,
                status=_DEGRADED,
                detail=(
                    f"publish provenance unknown — could not parse commit distance from "
                    f"{distance_str!r}."
                ),
                remediation="Re-run a percolate round to refresh the record.",
                required=False,
                data={"record_path": str(record_path), "published_sha": published_sha},
            )

        if distance == 0:
            return _ProbeResult(
                probe=probe_id,
                status=_PASS,
                detail=(
                    f"published engine is current — row {published_row!r} published claude-klabauter "
                    f"@ {published_sha}, which is claude-klabauter's HEAD."
                ),
                remediation="—",
                required=False,
                data={
                    "record_path": str(record_path),
                    "published_sha": published_sha,
                    "published_row": published_row,
                    "commits_behind": 0,
                },
            )

        return _ProbeResult(
            probe=probe_id,
            status=_DEGRADED,
            detail=(
                f"published engine is {distance} commit(s) behind claude-klabauter's HEAD — row "
                f"{published_row!r} published @ {published_sha}."
            ),
            remediation=(
                "An engine edit in claude-klabauter takes effect only when a publish round lands it — "
                "editing coordinator_core/ does not change what runs. Publish with: "
                "python coordinator/bin/coordinator-publish.py"
            ),
            required=False,
            data={
                "record_path": str(record_path),
                "published_sha": published_sha,
                "published_row": published_row,
                "commits_behind": distance,
            },
        )
    except Exception as exc:
        return _ProbeResult(
            probe=probe_id,
            status=_DEGRADED,
            detail=f"publish provenance unknown — unexpected error: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
            required=False,
            data={"unexpected_error": True},
        )


# ---------------------------------------------------------------------------
# Probe 10: per-invoke resolution/dispatch latency budget (Windows-portability)
# ---------------------------------------------------------------------------


# CLAUDE.md's brightline ("Process time and spawn count, never wall clock", "One
# bar: 500ms") is the gate this probe grades against — process time (user+kernel CPU
# across the spawned tree, via coordinator_core.benchmarks.process_time), never wall
# clock, and the same 500ms bar as every other process-time gate in this repo, not a
# bespoke hooks-budget number. Prior to the 2026-08-27 fix this gated wall-clock
# elapsed time against 2000ms — a unit and a bar CLAUDE.md both name as wrong, and a
# gate that measured peer-load noise (wall clock) against a bar loose enough
# (2000ms, four times the brightline) that it could not fail when it should.
_INVOKE_LATENCY_BUDGET_MS = 500


_INVOKE_LATENCY_PROBE = "claude-klabauter.invoke.latency"

# Bounded measurement window: `single_invocation_tree_process_time` has no timeout
# knob of its own (module docstring), so this probe brackets it in a daemon thread
# with `Thread.join(timeout=...)` — the same hand-rolled pattern
# `_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS` uses in this file for the identical
# problem (a primitive with no cancellation surface). A timeout IS the failure being
# detected; the thread is left running (it cannot be forcibly killed from here) but
# the probe itself always returns within this window.
_INVOKE_LATENCY_TIMEOUT_SECONDS = 5.0


def _run_probe_invoke_latency(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.invoke.latency — OPTIONAL (required=False); WARN over budget.

    Measures a single cold `python -m coordinator_core.invoke ping '{}'` round-trip
    as PROCESS TIME (user+kernel CPU across the spawned tree, via
    `coordinator_core.benchmarks.process_time.single_invocation_tree_process_time`)
    and compares it against _INVOKE_LATENCY_BUDGET_MS (500 ms) — CLAUDE.md's own
    brightline bar, never wall clock (CLAUDE.md § "The brightline": "Process time
    and spawn count, never wall clock").

    Question the sink cannot answer:
      What does a cold spawn-to-exit invoke round-trip cost RIGHT NOW, including
      interpreter start and import? The op census's seam-recorded process_time
      starts its clock inside an already-booted interpreter — it excludes the
      ~99ms of interpreter start and import this probe's own cold measurement
      includes (measured 2026-08-27: 149.6ms wall / 98.96ms process on a healthy
      box). No measurement_scope in the sink contains that cost, so there is no
      row to read it back from; this probe has to spawn the cold path itself.
      RETAINED (disposition: pln-2026-08-27-the-undeclared-harness-and-the-
      redundant-probes § C4) on this measurement gap, not on liveness — see
      claude-klabauter.invoke.smoke for the liveness question.

    Rationale (F17): hooks have a ~3-5 s total budget shared across multiple invokes
    on a single hook firing; a single invoke costing meaningfully more than the
    500ms brightline risks blowing that shared budget on its own, before accounting
    for fan-out. This is the same failure mode as the per-invoke bash-fallback hang
    (claude-klabauter.root.pointer) surfaced as a latency measurement rather than a static
    presence check.

    Pre-flight, `_engine_root_is_stamped`: an UNSTAMPED root skips rather than
    measures — a dispatch refused at the stamp gate times the refusal, not the
    round-trip this budget is about (DR-331).

    Bounded-measurement invariant: the measurement runs in a daemon thread bounded
    by `_INVOKE_LATENCY_TIMEOUT_SECONDS` via `Thread.join(timeout=...)`, so this
    probe itself can never hang the doctor — a timeout IS the failure being detected
    and is reported as the over-budget (DEGRADED) case, not re-raised.

    Negative-spec:
      - Does NOT retry or average multiple invocations — a single measurement, kept
        cheap and bounded per the spec (a repeatable-proxy batched measurement is
        `batched_process_time_ms`'s job, not this one-shot cold probe's).
      - Does NOT emit BROKEN on over-budget or timeout — this is a WARN-class latency
        advisory (required=False, DEGRADED), not a correctness failure; the invoke
        still dispatched (or the timeout itself proves the latency problem).
      - Does NOT gate on wall clock — wall clock on this box measures peer load
        (50-70 concurrent sessions is the design condition), not cost.
      - SKIP (not BROKEN, not DEGRADED) when process-time measurement itself is
        unavailable on this platform (`NotImplementedError` off Windows/Darwin) —
        an unmeasurable platform is not the same fact as a slow round-trip.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14;
    pln-2026-08-27-the-undeclared-harness-and-the-redundant-probes § C4 (unit/bar fix).
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail="Cannot measure invoke latency — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        dispatch_root = _resolve_dispatch_root(claude_klabauter_root)
        if dispatch_root is None:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail=_NO_DISPATCH_ROOT_DETAIL.format(root=claude_klabauter_root),
                remediation=_NO_DISPATCH_ROOT_REMEDIATION,
                required=False,
                skipped=True,
                data={"engine_root": str(claude_klabauter_root), "dispatch_root": None},
            )

        try:
            from coordinator_core.benchmarks.process_time import (
                single_invocation_tree_process_time,
            )
        except ImportError as exc:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail=(
                    f"coordinator_core.benchmarks.process_time not importable: {exc}; "
                    "invoke latency probe skipped."
                ),
                remediation="Verify coordinator_core/benchmarks/process_time.py is present.",
                required=False,
                skipped=True,
            )

        result_box: dict[str, Any] = {}

        def _measure() -> None:
            try:
                result_box["measurement"] = single_invocation_tree_process_time(
                    [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
                    cwd=str(dispatch_root),
                    stdout_path=os.devnull,
                    stderr_path=os.devnull,
                )
            except FileNotFoundError:
                result_box["file_not_found"] = True
            except NotImplementedError as exc:
                result_box["not_implemented"] = str(exc)
            except Exception as exc:  # belt-and-braces; surfaced as a SKIP below.
                result_box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_measure, daemon=True)
        thread.start()
        thread.join(_INVOKE_LATENCY_TIMEOUT_SECONDS)

        if thread.is_alive():
            timeout_ms = _INVOKE_LATENCY_TIMEOUT_SECONDS * 1000
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip did not complete within {timeout_ms:.0f} ms — "
                    f"exceeds the {_INVOKE_LATENCY_BUDGET_MS} ms process-time budget. A "
                    "timeout on this bounded measurement IS the failure being detected, "
                    "but this probe cannot tell from here whether the hang is in the "
                    "measured invoke round-trip itself (e.g. a bash-fallback subprocess "
                    "with its own 5 s timeout) or in the measurement primitive before it "
                    "ever spawned a process (e.g. blocked acquiring the Windows job-object "
                    "handle) — result_box carries no partial state to distinguish the two, "
                    "so both are reported identically as DEGRADED."
                ),
                remediation=(
                    "Ensure the claude-klabauter-live-root pointer is present (see claude-klabauter.root.pointer "
                    "probe) so per-invoke resolution avoids the bash-fallback subprocess. "
                    "See the Windows-portability workstream: "
                    "docs/plans/2026-07-14-claude-klabauter-windows-portability.md"
                ),
                required=False,
                data={"budget_ms": _INVOKE_LATENCY_BUDGET_MS, "timed_out": True},
            )

        if result_box.get("file_not_found"):
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail="Python interpreter not found on PATH; invoke latency probe skipped.",
                remediation="Ensure python3 is on PATH.",
                required=False,
                skipped=True,
            )

        if "not_implemented" in result_box:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail=(
                    "process-time measurement unavailable on this platform: "
                    f"{result_box['not_implemented']}"
                ),
                remediation=(
                    "Process-time measurement is Windows/Darwin-only "
                    "(coordinator_core.benchmarks.process_time); no primitive on "
                    "this platform yet — see that module's docstring."
                ),
                required=False,
                skipped=True,
            )

        if "error" in result_box:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_INFO,
                detail=f"Unexpected error in invoke latency probe: {result_box['error']}",
                remediation="Re-run the probe after investigating the error.",
                required=False,
                skipped=True,
            )

        measurement = result_box["measurement"]
        process_time_ms = measurement["process_time_ms"]
        rc = measurement["rc"]

        if rc != 0:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip cost {process_time_ms:.1f} ms process time but "
                    f"exited {rc}; latency measured but dispatch itself failed — see "
                    "claude-klabauter.invoke.smoke."
                ),
                remediation=(
                    f"Run manually from {dispatch_root}: "
                    "python3 -m coordinator_core.invoke ping '{}'. "
                    "Verify claude-klabauter.invoke.smoke passes first."
                ),
                required=False,
                data={
                    "process_time_ms": process_time_ms,
                    "budget_ms": _INVOKE_LATENCY_BUDGET_MS,
                    "timed_out": False,
                    "dispatch_root": str(dispatch_root),
                },
            )

        if process_time_ms > _INVOKE_LATENCY_BUDGET_MS:
            return _ProbeResult(
                probe=_INVOKE_LATENCY_PROBE,
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip cost {process_time_ms:.1f} ms process time — "
                    f"exceeds the {_INVOKE_LATENCY_BUDGET_MS} ms brightline (hooks share a "
                    "~3-5 s total budget across multiple invokes; a single invoke this "
                    "costly risks blowing the shared budget)."
                ),
                remediation=(
                    "Ensure the claude-klabauter-live-root pointer is present (see claude-klabauter.root.pointer "
                    "probe) so per-invoke resolution avoids the bash-fallback subprocess. "
                    "See the Windows-portability workstream: "
                    "docs/plans/2026-07-14-claude-klabauter-windows-portability.md"
                ),
                required=False,
                data={
                    "process_time_ms": process_time_ms,
                    "budget_ms": _INVOKE_LATENCY_BUDGET_MS,
                    "timed_out": False,
                    "dispatch_root": str(dispatch_root),
                },
            )

        return _ProbeResult(
            probe=_INVOKE_LATENCY_PROBE,
            status=_PASS,
            detail=(
                f"invoke round-trip cost {process_time_ms:.1f} ms process time — within "
                f"the {_INVOKE_LATENCY_BUDGET_MS} ms brightline."
            ),
            remediation="—",
            required=False,
            data={
                "process_time_ms": process_time_ms,
                "budget_ms": _INVOKE_LATENCY_BUDGET_MS,
                "timed_out": False,
                "dispatch_root": str(dispatch_root),
            },
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_INVOKE_LATENCY_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in invoke latency probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


# ---------------------------------------------------------------------------
# Probe 11: orphaned execnet gateways (OPTIONAL)
# ---------------------------------------------------------------------------


_EXECNET_GATEWAY_PROBE = "claude-klabauter.execnet.orphaned_gateways"

# The execnet worker bootstrap line every gateway process carries on its command
# line — nothing else on this box carries it (spike-verified,
# docs/research/spike-verdicts/2026-08-13-execnet-gateway-reap-on-abort.md).
_EXECNET_GATEWAY_SIGNATURE = "exec(eval(sys.stdin.readline()))"


def _run_probe_orphaned_execnet_gateways() -> _ProbeResult:
    """Probe claude-klabauter.execnet.orphaned_gateways — OPTIONAL (required=False).

    Detects execnet gateway worker processes (spawned by `pytest -n` / xdist)
    whose controller has died without reaping them — the residual leak an
    uncatchable `SIGKILL` of the runner leaves behind even after C1's
    process-group teardown closes every catchable abort path (see
    docs/plans/2026-08-13-reap-orphaned-execnet-gateways.md § Problem: "the
    two deliverables below are one problem, not two").

    Detection: enumerate every process whose command line contains
    _EXECNET_GATEWAY_SIGNATURE, then test whether its parent (the pytest
    controller / execnet bootstrap channel) is still alive.  A parent that no
    longer exists, or that has been reparented to init (ppid 0 or 1), means
    the controller is gone — orphaned.  A parent that IS alive means this is
    a healthy in-flight test run, not a leak.

    AC4 — the single most likely way to get this wrong (proven live during
    the spike, not hypothetical): four gateway processes matching the
    signature were observed under a LIVE controller — a peer's healthy
    in-flight `pytest -n` run on this shared, 50-70-concurrent-session box.
    A raw command-line count (the shape the originating memo proposed) would
    have flagged those four as a leak. This probe reports ONLY gateways with
    NO live controller; a gateway under a live controller is PASS, never
    reported as orphaned.

    Negative-spec:
      - Does NOT spawn anything — psutil.process_iter/pid_exists are
        process-table reads, no subprocess.
      - Does NOT kill anything — read-only diagnostic; remediation names the
        pid(s) for the operator to act on, this probe never terminates them.
      - Does NOT match on the execnet signature alone to call something
        orphaned — a signature match with a live parent is explicitly PASS
        (AC4). Signature-plus-dead-controller is the whole predicate.
      - Does NOT shell out to `ps`/`tasklist`/`wmic` — psutil.process_iter and
        psutil.pid_exists are the SAME primitives coordinator_core's own
        liveness seam (coordinator_core.session.core) and containment
        self-test (coordinator_core.diagnostics.contained_run) already use,
        cross-platform (POSIX + Windows) with no platform branch needed here.
      - An indeterminate parent-liveness read (psutil raises reading ppid)
        fails CLOSED toward "assume alive, not orphaned" — an ambiguous read
        is not a confirmed orphan, mirroring the fail-closed-to-keep posture
        coordinator_core.ops.session.reap uses for claim-dir liveness.

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a SKIP verdict (not a crash), matching the optional-probe contract.

    Spec backlink: pln-reap-orphaned-execnet-gateways-398c2c § C2, AC4-AC5;
    docs/research/spike-verdicts/2026-08-13-execnet-gateway-reap-on-abort.md.
    """
    try:
        try:
            import psutil  # Probe-local guarded third-party import — see module docstring Negative-spec.
        except ImportError:
            return _ProbeResult(
                probe=_EXECNET_GATEWAY_PROBE,
                status=_INFO,
                detail=(
                    "psutil not importable; orphaned-execnet-gateway detection skipped. "
                    "psutil is a declared, required coordinator_core dependency — its "
                    "absence indicates an incomplete engine install, not a normal state."
                ),
                remediation=(
                    "Install coordinator_core's declared dependencies "
                    "(pip install -e . from COORDINATOR_ENGINE_ROOT), then re-run the doctor probe."
                ),
                required=False,
                skipped=True,
            )

        orphaned_pids: list[int] = []
        live_controlled_count = 0

        # `psutil.process_iter` can itself raise NoSuchProcess/ZombieProcess
        # from its own internal snapshot step -- not only from `proc.info`
        # access below -- when a process vanishes mid-enumeration, which is
        # the normal case under this box's process churn. Advancing the
        # iterator by hand lets a per-process failure skip that process and
        # continue enumerating, instead of aborting the whole probe.
        proc_iter = psutil.process_iter(["pid", "ppid", "cmdline"])
        while True:
            try:
                proc = next(proc_iter)
            except StopIteration:
                break
            except Exception:
                continue

            try:
                cmdline = proc.info.get("cmdline") or []
            except Exception:
                continue
            if not any(_EXECNET_GATEWAY_SIGNATURE in part for part in cmdline):
                continue

            pid = proc.info.get("pid")
            ppid = proc.info.get("ppid")

            if ppid in (0, 1):
                # Reparented to init/kernel — controller definitively gone.
                orphaned_pids.append(pid)
                continue

            try:
                controller_alive = bool(ppid) and psutil.pid_exists(ppid)
            except Exception:
                # Indeterminate parent-liveness read — fail closed toward
                # "assume alive" (AC4: an ambiguous read is not a confirmed
                # orphan; only a confirmed-dead controller is reported).
                controller_alive = True

            if controller_alive:
                live_controlled_count += 1
                continue

            orphaned_pids.append(pid)

        if orphaned_pids:
            pids_str = _capped_join(orphaned_pids)
            return _ProbeResult(
                probe=_EXECNET_GATEWAY_PROBE,
                status=_DEGRADED,
                detail=(
                    f"{len(orphaned_pids)} execnet gateway process(es) with no live "
                    f"controller detected: pid(s) {pids_str}. "
                    f"{live_controlled_count} other gateway process(es) on this box have "
                    "a live controller and are a healthy in-flight test run — not counted "
                    "as orphaned (AC4)."
                ),
                remediation=(
                    f"These pid(s) ({pids_str}) are leaked pytest -n worker processes from "
                    "an aborted run whose controller died without reaping them. Confirm "
                    "none is your own in-flight run, then terminate the named pid(s) "
                    "directly — this probe does not kill anything itself."
                ),
                required=False,
                data={
                    "orphaned_pids": orphaned_pids,
                    "live_controlled_count": live_controlled_count,
                },
            )

        return _ProbeResult(
            probe=_EXECNET_GATEWAY_PROBE,
            status=_PASS,
            detail=(
                "No orphaned execnet gateway processes found"
                + (
                    f" ({live_controlled_count} gateway process(es) under a live "
                    "controller — healthy in-flight run)."
                    if live_controlled_count
                    else "."
                )
            ),
            remediation="—",
            required=False,
            data={
                "orphaned_pids": [],
                "live_controlled_count": live_controlled_count,
            },
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_EXECNET_GATEWAY_PROBE,
            status=_INFO,
            detail=(
                f"Unexpected error in orphaned execnet gateway probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
        )


_WARM_RESIDENCY_PROBE = "claude-klabauter.warm.residency"
_WARM_SERVER_CMDLINE_SIGNATURE = "coordinator_core/warm/server.py"


def _warm_check_pipe_reachable(pipe_name: str) -> tuple[bool | None, bool]:
    """Connect-and-close reachability primitive for a warm server's named pipe.

    Returns (reachable, primitive_skipped). Hand-rolled because there is no
    connect-only helper to reuse: `coordinator_core.warm.client._open_pipe`
    is a plain `open(pipe, "r+b")`, and `try_warm_dispatch`'s liveness path
    is a full dispatch send, not a bare probe. Mirrors client.py's own
    anti-storm exception table (module docstring):
      - FileNotFoundError -> no server -> unreachable.
      - OSError winerror == 231 (ERROR_PIPE_BUSY) -> server up, busy -> REACHABLE.
        Treating 231 as failure would report every healthy busy server as
        unreachable.
      - PermissionError -> someone else's pipe -> unreachable (not addressable
        by this caller).
      - any other OSError -> unreachable.

    Windows-only: `OSError.winerror` does not exist on POSIX, so this branches
    explicitly on `sys.platform` rather than relying on the accident that an
    unguarded `AttributeError` there is caught by a broader except and
    degrades to skipped — on POSIX this primitive cannot establish
    reachability at all, and callers must report that honestly (skipped=True)
    rather than letting an unrelated exception type imply an answer.

    Connect-and-close is a live interaction with a named-pipe instance on a
    server 50-70 sessions are contending for, not a pure read — mutates no
    persistent state, but is not side-effect-free the way a file read is.
    """
    if sys.platform != "win32":
        return None, True
    try:
        fh = open(pipe_name, "r+b")
    except FileNotFoundError:
        return False, False
    except PermissionError:
        return False, False
    except OSError as exc:
        if getattr(exc, "winerror", None) == 231:
            return True, False
        return False, False
    else:
        try:
            fh.close()
        except Exception:
            pass
        return True, False


_REACH_REACHABLE = "reachable"
_REACH_ORPHAN = "orphan"
_REACH_ORPHAN_NO_ENDPOINT = "orphan_no_endpoint"
_REACH_CANNOT_TELL = "cannot_tell"

# Review: coordinator:code-reviewer P3 — unrelated to _ENUMERATION_CAP above
# (display truncation via _capped_join); this bounds real-connect cost, one
# socket connect per resident, not how many identifiers get printed.
_WARM_REACHABILITY_PROBE_CAP = 16


class _ReachabilityCapped(Exception):
    """Raised when the per-run reachability-probe budget is exhausted.

    Each reachability check is a real connect against a resident server, so
    the cost of this probe is linear in resident count. The cap bounds it;
    residents past it report cannot_tell rather than going unprobed and
    silently reading as reachable. A box with more than
    `_WARM_REACHABILITY_PROBE_CAP` resident warm servers has a bigger problem
    than this probe's precision.
    """


def _warm_check_socket_reachable(socket_path: Any, election_module: Any) -> str:
    """Connect-and-close reachability primitive for a warm server's AF_UNIX socket.

    The POSIX twin of `_warm_check_pipe_reachable`. Delegates to
    `coordinator_core.warm.election.probe_endpoint` rather than opening its
    own socket: that primitive already encodes the two rules a hand-rolled
    one gets wrong — `ECONNREFUSED` is the ONLY proof of staleness POSIX
    offers, and a connect that TIMES OUT reads LIVE because a listening
    socket with a full backlog is exactly the busy-server case (the same
    subtlety the Windows leg's `ERROR_PIPE_BUSY == 231` rule encodes).
    Duplicating it here would be the defect this leg exists to close.

    Maps three states onto four classifications, because two would lose a
    distinction the probe must not silently drop:
      - PROBE_LIVE   -> reachable.
      - PROBE_STALE  -> orphan (socket file present, nobody listening).
      - PROBE_ABSENT -> orphan_no_endpoint. Unreachable like the Windows
        `FileNotFoundError` leg, but NOT the same evidence: no endpoint was
        ever found, versus one found and refused. Callers name it separately.
      - anything else propagates, so the caller's guard reports cannot_tell
        rather than a guess.

    Connect-and-close is a live interaction with a server 50-70 sessions are
    contending for — one no-op round through its accept/queue/worker path per
    probe, bounded by `_WARM_REACHABILITY_PROBE_CAP` at the call site.
    """
    verdict = election_module.probe_endpoint(socket_path)
    if verdict == election_module.PROBE_LIVE:
        return _REACH_REACHABLE
    if verdict == election_module.PROBE_STALE:
        return _REACH_ORPHAN
    if verdict == election_module.PROBE_ABSENT:
        return _REACH_ORPHAN_NO_ENDPOINT
    return _REACH_CANNOT_TELL


def _enumerate_resident_warm_servers(psutil_module: Any) -> list[dict[str, Any]]:
    """Shared psutil.process_iter walk matching resident warm server processes
    against `_WARM_SERVER_CMDLINE_SIGNATURE` (`coordinator_core/warm/server.py`).

    SINGLE enumeration site: `_run_probe_warm_residency` and
    `_run_probe_warm_generation` both call this rather than each carrying
    its own copy of the psutil walk — a resident server's engine root is
    encoded in its matched cmdline argument regardless of which probe is
    asking, so there is exactly one correct way to derive it.

    Returns one dict per matched process: `{"pid": int | None,
    "create_time": float | None, "engine_root": Path}`. Pure enumeration
    only — no breadcrumb read, no pipe reachability, no classification;
    callers layer their own per-probe semantics on top.
    """
    servers: list[dict[str, Any]] = []

    proc_iter = psutil_module.process_iter(["pid", "create_time", "cmdline"])
    while True:
        try:
            proc = next(proc_iter)
        except StopIteration:
            break
        except Exception:
            continue

        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue

        script_arg = None
        for part in cmdline:
            if part.replace("\\", "/").endswith(_WARM_SERVER_CMDLINE_SIGNATURE):
                script_arg = part
                break
        if script_arg is None:
            continue

        pid = proc.info.get("pid")
        try:
            create_time = proc.info.get("create_time")
        except Exception:
            create_time = None

        engine_root = Path(script_arg).resolve().parent.parent.parent
        servers.append({"pid": pid, "create_time": create_time, "engine_root": engine_root})

    return servers


def _run_probe_warm_residency(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.warm.residency — REQUIRED (process scan; `warm` cluster).

    Enumerates resident warm server processes via `psutil.process_iter`,
    matching cmdline against `coordinator_core/warm/server.py`
    (`_WARM_SERVER_CMDLINE_SIGNATURE`) — the exact script
    `coordinator_core.warm.client.SERVER_ENTRY_SCRIPT` names as its spawn
    target, respawned by resolved absolute path (never `-m`), so the matched
    cmdline argument itself encodes which engine clone spawned it: the
    argument is `<engine_root>/coordinator_core/warm/server.py`, three path
    components below the clone root.

    Classifies each resident server as REACHABLE or ORPHAN using the
    connect-and-close reachability primitive for the platform's transport:
    `_warm_check_pipe_reachable` over `election.pipe_name` on Windows,
    `_warm_check_socket_reachable` over `election.socket_path` on POSIX.
    Both are composed with `skew.compute_client_token(engine_root)` and
    `engine_clone=` passed EXPLICITLY — both name functions default to THIS
    repo's own endpoint, which is wrong for a server that may run from a
    different engine clone (the entire point of this classification).

    The POSIX leg is exercised on macOS. It is written POSIX-general and is
    UNEXERCISED on Linux — no Linux box is reachable from this fleet
    (PM, 2026-08-26) — so Linux is not claimed as covered merely because the
    code has no macOS-specific branch.

    Cost is linear in resident count: one connect per resident, bounded by
    `_WARM_REACHABILITY_PROBE_CAP`. Residents past the cap report
    cannot_tell rather than going unprobed and reading as reachable.

    Per-server `breadcrumb_state` is enrichment only, never population
    enumeration — the breadcrumb is one file per clone, clobbered per
    generation, so it cannot answer "what servers exist". It IS read (via
    `breadcrumb.read_breadcrumb`, a pure read returning None on
    absent-or-corrupt, never raising) to say whether THIS resident server is
    the one the breadcrumb currently names ("current"), a different pid
    ("superseded"), or unknowable ("cannot_tell" — an absent/corrupt
    breadcrumb is a no-op, not evidence of "no server running"; AC9).
    `stable_pid_alive` (not raw pid_exists) confirms it is the SAME process,
    not a recycled pid.

    Orphan remediation NAMES NO ACTION (AC10): the only stop mechanism
    targets the currently-elected (breadcrumb-named) server, not a specific
    orphaned pid — pointing an operator at it would kill the live server
    50-70 sessions are using and leave the orphan running. A doctor that
    names an unsafe action is worse than one that names none.

    `_ProbeResult.required` is stated explicitly at every return, per
    finding F6: the TOML manifest's `required = false` footer governs
    synthesised INFO stubs for an UN-implemented probe only (inert here,
    since this probe IS implemented) and is not the same field as this
    runtime `required`, which the envelope's worst-of reduction and
    advisory-rendering both read directly. A "cannot tell" (skipped) result
    here is `required=True` so it participates in worst-of ranking, per
    AC9/AC10's intent.

    Probe-authoring invariant: wraps all logic so unexpected exceptions
    become an INFO+skipped verdict, never an unhandled crash.

    Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C5a, AC7, AC9, AC10.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_INFO,
                detail="Cannot check warm residency — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=True,
                skipped=True,
            )

        try:
            import psutil  # Probe-local guarded third-party import — see module docstring Negative-spec (mirrors _run_probe_orphaned_execnet_gateways).
        except ImportError:
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_INFO,
                detail=(
                    "psutil not importable; warm residency detection skipped. "
                    "psutil is a declared, required coordinator_core dependency — its "
                    "absence indicates an incomplete engine install, not a normal state."
                ),
                remediation=(
                    "Install coordinator_core's declared dependencies "
                    "(pip install -e . from COORDINATOR_ENGINE_ROOT), then re-run the doctor probe."
                ),
                required=True,
                skipped=True,
            )

        # sys.path already set by `_ensure_core_importable`, run_probes' unconditional
        # prerequisite — NOT by an earlier probe (see that helper's docstring).
        try:
            from coordinator_core.warm import breadcrumb, election, skew
            from coordinator_core.session.core import stable_pid_alive
        except ImportError as exc:
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_INFO,
                detail=(
                    "coordinator_core.warm / coordinator_core.session.core not "
                    f"importable: {exc}"
                ),
                remediation="Verify coordinator_core/ is present in COORDINATOR_ENGINE_ROOT (see claude-klabauter.core.import probe).",
                required=True,
                skipped=True,
            )

        servers: list[dict[str, Any]] = []

        for probed, resident in enumerate(_enumerate_resident_warm_servers(psutil)):
            pid = resident["pid"]
            create_time = resident["create_time"]
            age_secs = (time.time() - create_time) if create_time else None
            engine_root = resident["engine_root"]

            classification = _REACH_CANNOT_TELL
            cannot_tell_reason = None
            try:
                if probed >= _WARM_REACHABILITY_PROBE_CAP:
                    raise _ReachabilityCapped(
                        f"reachability probing capped at {_WARM_REACHABILITY_PROBE_CAP} residents"
                    )
                token = skew.compute_client_token(engine_root)
                if sys.platform == "win32":
                    pipe = election.pipe_name(token, engine_clone=engine_root)
                    reachable, reach_skipped = _warm_check_pipe_reachable(pipe)
                    if reach_skipped:
                        classification = _REACH_CANNOT_TELL
                    elif reachable:
                        classification = _REACH_REACHABLE
                    else:
                        classification = _REACH_ORPHAN
                else:
                    sock = election.socket_path(token, engine_clone=engine_root)
                    classification = _warm_check_socket_reachable(sock, election)
            except _ReachabilityCapped:
                # Review: coordinator:code-reviewer P2 — cap-exhaustion is
                # cannot_tell for classification purposes (unchanged), but gets
                # its own per-resident diagnostic marker so an operator reading
                # data["servers"] can tell "skipped, cap hit" from "probed, errored"
                # without cross-referencing count against _WARM_REACHABILITY_PROBE_CAP.
                classification = _REACH_CANNOT_TELL
                cannot_tell_reason = "probe_cap_reached"
            except Exception:
                classification = _REACH_CANNOT_TELL
                cannot_tell_reason = "probe_error"

            bc = None
            try:
                bc = breadcrumb.read_breadcrumb(engine_root)
            except Exception:
                bc = None
            if bc is None:
                breadcrumb_state = "cannot_tell"
            else:
                try:
                    matches = bc.get("pid") == pid and stable_pid_alive(
                        pid, stored_start_epoch=bc.get("stable_pid_start_epoch", 0)
                    )
                except Exception:
                    matches = False
                breadcrumb_state = "current" if matches else "superseded"

            server_entry = {
                "pid": pid,
                "age_secs": age_secs,
                "engine_root": str(engine_root),
                "classification": classification,
                "breadcrumb_state": breadcrumb_state,
            }
            if cannot_tell_reason is not None:
                server_entry["cannot_tell_reason"] = cannot_tell_reason
            servers.append(server_entry)

        refused = [s for s in servers if s["classification"] == _REACH_ORPHAN]
        no_endpoint = [s for s in servers if s["classification"] == _REACH_ORPHAN_NO_ENDPOINT]
        orphans = refused + no_endpoint
        cannot_tell = [s for s in servers if s["classification"] == _REACH_CANNOT_TELL]

        if not servers:
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_PASS,
                detail="No resident warm server processes found (matched by cmdline against coordinator_core/warm/server.py).",
                remediation="—",
                required=True,
                data={"servers": []},
            )

        if orphans:
            # Review: coordinator:code-reviewer P3 — the aggregate pid(s) list
            # was previously rendered a third time on top of the refused/
            # no_endpoint breakdowns below (up to 15 identifiers in one detail
            # string for >5 orphans of each kind). Dropped here; the
            # refused-vs-no_endpoint distinction — the ABSENT-vs-refused
            # evidence this probe must not flatten — still carries its own
            # capped pid(s) list in `evidence` below.
            evidence = []
            if refused:
                evidence.append(
                    f"{len(refused)} with an endpoint present but refusing connections "
                    f"(pid(s) {_capped_join(s['pid'] for s in refused)})"
                )
            if no_endpoint:
                evidence.append(
                    f"{len(no_endpoint)} with NO endpoint found at all — a distinct signal from "
                    "a refused connection, not a stale endpoint "
                    f"(pid(s) {_capped_join(s['pid'] for s in no_endpoint)})"
                )
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_DEGRADED,
                detail=(
                    f"{len(orphans)} resident warm server process(es) unreachable (orphaned), "
                    f"of {len(servers)} resident server(s) total. "
                    + "; ".join(evidence)
                    + "."
                ),
                remediation=(
                    "No automated remediation is named — this probe does not point at the "
                    "stop mechanism. That mechanism targets the current, breadcrumb-elected "
                    "server, not a specific orphaned pid; pointing it here would risk killing "
                    "the live server 50-70 sessions are using while leaving the named orphan(s) "
                    "running. Investigate the named pid(s) manually before acting."
                ),
                required=True,
                data={"servers": servers, "orphan_pids": [s["pid"] for s in orphans]},
            )

        if cannot_tell:
            return _ProbeResult(
                probe=_WARM_RESIDENCY_PROBE,
                status=_INFO,
                detail=(
                    f"Found {len(servers)} resident warm server process(es); reachability "
                    "cannot be established for at least one of them "
                    + "(endpoint-name computation or connect attempt failed unexpectedly, or "
                      "the per-run reachability-probe cap was reached)"
                    + ". Absence of evidence here is not evidence of absence — cannot tell, "
                    "not confirmed orphaned and not confirmed reachable."
                ),
                remediation="—",
                required=True,
                skipped=True,
                data={"servers": servers},
            )

        return _ProbeResult(
            probe=_WARM_RESIDENCY_PROBE,
            status=_PASS,
            detail=f"{len(servers)} resident warm server process(es), all reachable.",
            remediation="—",
            required=True,
            data={"servers": servers},
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_WARM_RESIDENCY_PROBE,
            status=_INFO,
            detail=f"Unexpected error in warm residency probe: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
            required=True,
            skipped=True,
        )


_WARM_GENERATION_PROBE = "claude-klabauter.warm.generation"


def _warm_generation_current_token_stale(engine_root: Path, breadcrumb_pipe_token: str) -> bool | None:
    """SINGLE named predicate: is `breadcrumb_pipe_token` (the pipe-name token
    segment the resident server's breadcrumb recorded at boot) stale relative
    to a freshly recomputed `skew.compute_client_token(engine_root)`?

    Returns True (stale) / False (current) / None ("cannot tell" — token
    computation itself failed).

    FORWARD NOTE (state/handoffs/2026-08-19-one-engine-never-the-live-tree.md):
    that plan intends to delete `skew.compute_client_token`'s no-stamp
    fallback and raise a named error in its place. This predicate is the
    ONE site that calls `compute_client_token` for this probe, and its
    `except Exception` is where that future error gets caught (broad today
    because no such error exists yet) — the swap to "is this root stamped?"
    becomes a one-line edit here, not a probe-wide rewrite.
    """
    from coordinator_core.warm import skew

    try:
        current_token = skew.compute_client_token(engine_root)
    except Exception:
        return None
    return current_token != breadcrumb_pipe_token


def _run_probe_warm_generation(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.warm.generation — REQUIRED (pure local read; `warm` cluster).

    Compares the pipe-name TOKEN SEGMENT each RESIDENT SERVER's OWN
    breadcrumb recorded at boot (the last dot-separated component of
    `election.pipe_name(...)`'s output, i.e. the `engine_token` argument
    verbatim) against a freshly recomputed
    `skew.compute_client_token(engine_root)` computed against that SAME
    server's own engine root — never `claude_klabauter_root` (this repo's own root).
    A mismatch means that resident server's generation token predates the
    current tree/mirror it was spawned from — a stale-generation signal,
    not a liveness check.

    Resident servers, and each one's own engine root, are discovered the
    SAME way `_run_probe_warm_residency` discovers them: via
    `_enumerate_resident_warm_servers` (the shared psutil.process_iter walk
    — do not copy-paste a second enumeration site). A warm server on this
    box commonly runs out of a published engine mirror, not this repo's own
    clone (`claude_klabauter_root`); the breadcrumb it wrote lives under ITS clone,
    not this one, so resolving `claude_klabauter_root`'s own breadcrumb here would
    silently miss it (the bug this probe existed to fix). `claude_klabauter_root`
    itself is retained only as the pre-flight "is COORDINATOR_ENGINE_ROOT resolved at
    all" gate — it plays no further role once enumeration starts.

    PURE LOCAL READ, by design: this probe must not connect to any server
    and must not elect.
      - `_enumerate_resident_warm_servers` only reads process cmdlines via
        psutil — no connect, no elect.
      - `breadcrumb.read_breadcrumb` is a pure read, never raises, returns
        None on absent/corrupt (module contract).
      - `skew.compute_client_token` is a stat-only fingerprint (no
        subprocess, no connect).
      - `election.pipe_name` is NOT called here at all — each breadcrumb
        already recorded its own actual bound pipe string; this probe only
        needs to isolate its trailing token segment and diff it against a
        fresh token, not reconstruct the whole pipe name.
      - `election.elect()` is NEVER called — it mutates (contests the
        first named-pipe instance, calls `CreateNamedPipe`, raises
        `ElectionLost`) and has no place in a read-only probe.
      - `ServerVersionState.is_skewed` is NEVER called either — it
        triggers a throttled (2s) source-hash refresh, so it is not the
        zero-cost read it appears to be.
      - `_warm_check_pipe_reachable` (C5a's connect-and-close primitive)
        is NEVER called here — this probe never opens a pipe.

    Multiple resident servers is a real case on this box: EVERY matched
    server's own breadcrumb is read and diffed against its own engine
    root. If ANY resident server's token is stale, the overall verdict is
    the self-resolving INFO arm, naming every stale server's pid. Otherwise, if ANY server's
    generation could not be determined (no breadcrumb, malformed pipe
    field, or token computation failure), the verdict is the "cannot tell"
    skip (naming the affected pids) rather than a false PASS. Only when
    every resident server's token is confirmed current is the verdict
    PASS. No resident server at all is itself a legitimate "cannot tell".

    Attribution note (baton AC6, "resident engine predates last mirror
    publish"): this probe's stale-token reading covers the
    resident-vs-publish angle via `skew.compute_client_token`'s stamp-file
    fingerprint. `claude-klabauter.publish.provenance` (pre-existing probe) answers
    the published-vs-source half; the two are complementary, not
    duplicative.

    `_ProbeResult.required` is stated explicitly at every return, per
    finding F6 (same reasoning as C5a's docstring): the TOML manifest's
    `required = false` footer governs synthesised INFO stubs for an
    UN-implemented probe only, and is not the same field as this runtime
    `required`, which the envelope's worst-of reduction and
    advisory-rendering both read directly. A "cannot tell" (skipped)
    result here is `required=True`.

    The ONE arm that is `required=False` is the stale-generation arm, and
    its own remediation is the reason: a stale generation drains via
    warm.idle's superseded-generation arm and names no action.
    `required` is what `_sz_severity` maps to step-zero `hard`, and
    `scripts/setup.py` exits 94 on any hard probe that is not `pass` — so
    leaving that arm required made every install on a box with an in-flight
    warm server exit non-zero for a condition nobody can act on. That arm's
    status is `_INFO` (step-zero `warn`) for the same reason the severity
    dropped: step-zero `fail` means "the checked condition is NOT satisfied"
    and expects a remediation the reader can run, while this state resolves
    itself with no action taken. The reading is still reported in full; it
    is reported as a self-resolving observation, not a fault. Every other
    arm, PASS and cannot-tell alike, remains `required=True`.

    Probe-authoring invariant: wraps all logic so unexpected exceptions
    become an INFO+skipped verdict, never an unhandled crash.

    Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C5b.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail="Cannot check warm generation skew — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=True,
                skipped=True,
            )

        try:
            import psutil  # Probe-local guarded third-party import — see module docstring Negative-spec (mirrors _run_probe_warm_residency).
        except ImportError:
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail=(
                    "psutil not importable; warm generation detection skipped. "
                    "psutil is a declared, required coordinator_core dependency — its "
                    "absence indicates an incomplete engine install, not a normal state."
                ),
                remediation=(
                    "Install coordinator_core's declared dependencies "
                    "(pip install -e . from COORDINATOR_ENGINE_ROOT), then re-run the doctor probe."
                ),
                required=True,
                skipped=True,
            )

        try:
            from coordinator_core.warm import breadcrumb
        except ImportError as exc:
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail=f"coordinator_core.warm not importable: {exc}",
                remediation="Verify coordinator_core/ is present in COORDINATOR_ENGINE_ROOT (see claude-klabauter.core.import probe).",
                required=True,
                skipped=True,
            )

        resident = _enumerate_resident_warm_servers(psutil)

        if not resident:
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail=(
                    "No resident warm server process found — cannot tell whether any "
                    "server's generation token is stale. Absence of a resident process "
                    "is not evidence of 'stale' or 'current'; it is simply nothing to check."
                ),
                remediation="—",
                required=True,
                skipped=True,
                data={"servers": []},
            )

        stale_pids: list[Any] = []
        cannot_tell_pids: list[Any] = []
        servers_data: list[dict[str, Any]] = []

        for server in resident:
            pid = server["pid"]
            engine_root = server["engine_root"]

            try:
                bc = breadcrumb.read_breadcrumb(engine_root)
            except Exception:
                bc = None

            if bc is None:
                cannot_tell_pids.append(pid)
                servers_data.append({
                    "pid": pid,
                    "engine_root": str(engine_root),
                    "generation_state": "cannot_tell",
                    "reason": "no breadcrumb on disk for this server's engine root",
                })
                continue

            pipe = bc.get("pipe")
            if not isinstance(pipe, str) or "." not in pipe:
                cannot_tell_pids.append(pid)
                servers_data.append({
                    "pid": pid,
                    "engine_root": str(engine_root),
                    "generation_state": "cannot_tell",
                    "reason": "breadcrumb 'pipe' field missing or malformed",
                })
                continue

            breadcrumb_pipe_token = pipe.rsplit(".", 1)[-1]
            stale = _warm_generation_current_token_stale(engine_root, breadcrumb_pipe_token)

            if stale is None:
                cannot_tell_pids.append(pid)
                servers_data.append({
                    "pid": pid,
                    "engine_root": str(engine_root),
                    "generation_state": "cannot_tell",
                    "reason": "skew.compute_client_token(engine_root) could not be computed",
                    "breadcrumb_pipe_token": breadcrumb_pipe_token,
                })
                continue

            if stale:
                stale_pids.append(pid)
                servers_data.append({
                    "pid": pid,
                    "engine_root": str(engine_root),
                    "generation_state": "stale",
                    "breadcrumb_pipe_token": breadcrumb_pipe_token,
                })
            else:
                servers_data.append({
                    "pid": pid,
                    "engine_root": str(engine_root),
                    "generation_state": "current",
                    "breadcrumb_pipe_token": breadcrumb_pipe_token,
                })

        if stale_pids:
            pids_str = _capped_join(stale_pids)
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail=(
                    f"{len(stale_pids)} resident warm server process(es) have a stale "
                    f"generation token (pid(s) {pids_str}): the breadcrumb pipe-name "
                    "token differs from a freshly computed "
                    "skew.compute_client_token(engine_root) for that server's own "
                    "engine root — that resident generation predates its current "
                    "tree/mirror."
                ),
                remediation=(
                    "A stale generation drains on its own via warm.idle's superseded-"
                    "generation arm once a fresh server binds; no direct action is named here."
                ),
                required=False,
                data={"servers": servers_data, "stale_pids": stale_pids},
            )

        if cannot_tell_pids:
            pids_str = _capped_join(cannot_tell_pids)
            return _ProbeResult(
                probe=_WARM_GENERATION_PROBE,
                status=_INFO,
                detail=(
                    f"Found {len(resident)} resident warm server process(es); generation "
                    f"currency cannot be established for at least one of them (pid(s) "
                    f"{pids_str}). Absence of evidence here is not evidence of a stale "
                    "or current generation — cannot tell."
                ),
                remediation="—",
                required=True,
                skipped=True,
                data={"servers": servers_data},
            )

        return _ProbeResult(
            probe=_WARM_GENERATION_PROBE,
            status=_PASS,
            detail=(
                f"{len(resident)} resident warm server process(es); every one's "
                "breadcrumb pipe-name token matches its own engine root's current "
                "generation token."
            ),
            remediation="—",
            required=True,
            data={"servers": servers_data},
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_WARM_GENERATION_PROBE,
            status=_INFO,
            detail=f"Unexpected error in warm generation probe: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
            required=True,
            skipped=True,
        )


_WARM_ROUTE_SHARE_PROBE = "claude-klabauter.warm.route_share"

#: Expanding coverage window (seconds): 1h -> 6h -> 24h, widening only while
#: the window holds fewer than `_ROUTE_MIN_COMPLETE_ROWS` complete rows
#: (plan D1, docs/plans/2026-08-19-a-windowed-coverage-refusal.md). Both
#: endpoints are precedented, not chosen fresh:
#:   - the LOW end is NOT the ~15min warm-server idle-recycle deadline
#:     (`docs/wiki/machine-load-norm.md`'s warm-server generation cadence) —
#:     15min is too tight to *start* from: it held only 453 rows on a *busy*
#:     evening measurement and would routinely trip the row-count minimum on
#:     a quiet box, conflating "the box is quiet" with "the instrument
#:     cannot tell". 1h is the starting horizon instead: it spans roughly
#:     four warm-server recycle generations, held 1,744 rows and 100%
#:     coverage at measurement time, and describes "now" while carrying
#:     enough rows to mean something at this machine's load norm.
#:   - the HIGH end reuses `cost_census.LOOKBACK_SECS_DEFAULT`
#:     (`coordinator_core/telemetry/cost_census.py:140`, 24h) as the CAP
#:     rather than rejecting it as too loose — if 1h and 6h both hold too
#:     few rows, the probe widens to 24h and refuses a verdict only if that
#:     still comes up short. 24h stops being "too loose for a verdict" and
#:     becomes "the point past which we stop trying".
#: Widening is free at runtime: the sink is read exactly once (below) and
#: every horizon here is a re-filter of that one in-memory list, never a
#: second read.
_ROUTE_COVERAGE_WINDOWS_SECS = (3600, 21600, 86400)

#: Absolute row-count minimum a window must hold before its ratio is judged
#: at all (plan D3) — a one-row window reads 100% coverage and a ratio
#: cannot distinguish "1/1" from "1,744/1,744", so this catches a distinct
#: false-PASS the coverage floor structurally cannot see. 50 sits well
#: under the 1,744 rows a live hour produced and well over the handful that
#: would make a ratio noise.
_ROUTE_MIN_COMPLETE_ROWS = 50


def _entry_in_route_window(entry: dict, since_ts: float) -> bool:
    """True if `entry` belongs in a window starting at `since_ts`.

    Mirrors `iter_sink_entries`'s own `since` rule: a row with a numeric
    `t_start` is kept only if it falls inside the window; a row lacking a
    numeric `t_start` is kept unconditionally — a reader cannot safely call
    an untimestamped row "too old", so it survives every window forever
    (this is deliberate, not a leak: such a row also cannot carry `route`
    without `t_start` being written by the same op_latency call, so it
    cannot sink a windowed verdict either — see the paired fixture row in
    the unit tests).
    """
    t_start = entry.get("t_start")
    if isinstance(t_start, (int, float)):
        return t_start >= since_ts
    return True


def _run_probe_warm_route_share(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.warm.route_share — OPTIONAL (route-share reader; `warm` cluster).

    Calls `coordinator_core.telemetry.engine_report.route_distribution` over
    an EXPANDING WINDOW (`_ROUTE_COVERAGE_WINDOWS_SECS`) of
    `engine_report.iter_sink_entries(repo_root=claude_klabauter_root)`, read ONCE, and
    TRANSLATES the windowed verdict into the closed probe-status enum. This
    probe does not re-parse the op-latency sink itself — two parsers drift,
    which is the whole point of building the reader (`iter_sink_entries` /
    `route_distribution`) first.

    Translation (do not extend without also updating this comment and the
    reader's own docstring):
      reader verdict "ok"       -> `_PASS`
      reader verdict "degraded" -> `_DEGRADED` (reserved by the reader for a
                                    future share-threshold check; not emitted
                                    today)
      reader verdict "unknown"  -> `skipped=True`, `required=True` — never
                                    PASS, never FAIL. Fired only when even
                                    the widest (24h) horizon still holds
                                    fewer than `_ROUTE_MIN_COMPLETE_ROWS`
                                    complete rows, or when the best-available
                                    horizon's coverage is below the reader's
                                    own floor — the honest reading in either
                                    case is "cannot tell", not a verdict; a
                                    probe that renders FAIL off it is as
                                    wrong as one that renders PASS.

    NEGATIVE SPEC: the reader's own verdict string ("ok"/"degraded"/"unknown")
    is a richer DATA-PAYLOAD vocabulary than the probe status enum and is
    reported verbatim in `data["reader_verdict"]`, never assigned to
    `_ProbeResult.status` — `status` is the closed, versioned enum in
    `coordinator_core.doctor_envelope.STATUS_VOCAB`, whose own comment says
    do not extend without bumping `ENVELOPE_SCHEMA_VERSION`.

    NEGATIVE SPEC (plan D2, docs/plans/2026-08-19-a-windowed-coverage-refusal.md):
    `data["all_time"]` (the unwindowed `route_distribution(entries)` over the
    same single read) is CONTEXT ONLY — it shows the publish-lag trend across
    the whole corpus. Nothing in this function branches on it; the verdict is
    always computed from the windowed figure alone.

    `_ProbeResult.required` is stated explicitly at every return (F6, same
    convention as C5a/C5b): the TOML manifest's `required = false` footer
    governs only the synthesised-stub path for an unimplemented probe id, not
    this runtime field. The skipped/unknown path here sets required=True so
    it reduces to DEGRADED, never drops out silently.

    Never uses `_INFO`: an INFO verdict is dropped entirely by the envelope's
    worst-of reduction (`_local_reduce_overall` / `build_envelope` both treat
    INFO as "no opinion"), which would let an otherwise-all-PASS probe set
    render overall PASS despite this probe having nothing to say at today's
    coverage — exactly the false-PASS this probe exists to prevent. `_DEGRADED`
    (with `skipped=True` where applicable) is used instead on every path,
    including the claude_klabauter_root-unresolved and unexpected-exception paths.

    Cross-reference (reviewer nit, coordinator:code-reviewer): this probe's
    `_DEGRADED`+`skipped=True`+`required=True` "cannot tell" convention and
    `_run_probe_warm_residency`/`_run_probe_warm_generation`'s `_INFO`+
    `skipped=True`+`required=True` convention are DIFFERENT stored `status`
    values for the same semantic case, but they reduce IDENTICALLY through
    `_local_reduce_overall`/`build_envelope`: a `skipped=True` row's rendered
    verdict is derived from `required` alone, never from `status`. Both are
    correct; this is one convention documented three times across sibling
    probes, not three independent decisions.

    Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C6;
    windowing added by docs/plans/2026-08-19-a-windowed-coverage-refusal.md § C2.
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=_WARM_ROUTE_SHARE_PROBE,
                status=_DEGRADED,
                detail="Cannot check warm route share — COORDINATOR_ENGINE_ROOT unresolved; skipping.",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
                required=True,
                skipped=True,
            )

        try:
            from coordinator_core.telemetry import engine_report
        except ImportError as exc:
            return _ProbeResult(
                probe=_WARM_ROUTE_SHARE_PROBE,
                status=_DEGRADED,
                detail=f"coordinator_core.telemetry.engine_report not importable: {exc}",
                remediation="Verify coordinator_core/ is present in COORDINATOR_ENGINE_ROOT (see claude-klabauter.core.import probe).",
                required=True,
                skipped=True,
            )

        # Read the sink ONCE, unwindowed — every horizon below is a re-filter
        # of this one in-memory list, never a second read (plan D1).
        entries = list(engine_report.iter_sink_entries(repo_root=claude_klabauter_root))
        all_time_result = engine_report.route_distribution(entries)

        now = time.time()
        reader_result: dict = all_time_result
        effective_window_secs = _ROUTE_COVERAGE_WINDOWS_SECS[-1]
        for horizon in _ROUTE_COVERAGE_WINDOWS_SECS:
            since_ts = now - horizon
            windowed_entries = [
                entry for entry in entries if _entry_in_route_window(entry, since_ts)
            ]
            reader_result = engine_report.route_distribution(
                windowed_entries, min_complete_rows=_ROUTE_MIN_COMPLETE_ROWS
            )
            effective_window_secs = horizon
            if reader_result["complete"] >= _ROUTE_MIN_COMPLETE_ROWS:
                break

        verdict = reader_result.get("verdict")
        verdict_reason = reader_result.get("verdict_reason")
        window_label = f"{effective_window_secs // 3600}h"
        data = {
            "reader_verdict": verdict,
            "route_distribution": reader_result,
            "effective_window_secs": effective_window_secs,
            "all_time": all_time_result,
        }

        if verdict == "ok":
            return _ProbeResult(
                probe=_WARM_ROUTE_SHARE_PROBE,
                status=_PASS,
                detail=(
                    f"Route-share coverage sufficient over the last {window_label} "
                    f"({reader_result['complete']} rows): {verdict_reason}"
                ),
                remediation="—",
                required=True,
                data=data,
            )

        if verdict == "degraded":
            return _ProbeResult(
                probe=_WARM_ROUTE_SHARE_PROBE,
                status=_DEGRADED,
                detail=(
                    f"Route-share reader reports degraded over the last {window_label} "
                    f"({reader_result['complete']} rows): {verdict_reason}"
                ),
                remediation="Investigate the warm-route share regression named in the reader's verdict_reason.",
                required=True,
                data=data,
            )

        # "unknown" (or any future unrecognised value) — honest "cannot tell",
        # never PASS and never FAIL.
        return _ProbeResult(
            probe=_WARM_ROUTE_SHARE_PROBE,
            status=_DEGRADED,
            detail=(
                f"Route-share coverage cannot support a verdict even at the "
                f"widest ({window_label}) window: {verdict_reason}"
            ),
            remediation="—",
            required=True,
            skipped=True,
            data=data,
        )
    except Exception as exc:
        return _ProbeResult(
            probe=_WARM_ROUTE_SHARE_PROBE,
            status=_DEGRADED,
            detail=f"Unexpected error in warm route-share probe: {type(exc).__name__}: {exc}",
            remediation="Re-run the probe after investigating the error.",
            required=True,
            skipped=True,
        )


_WARM_ROUNDTRIP_PROBE = "claude-klabauter.warm.roundtrip"

# Connect-deadline for this probe's live warm round-trip attempt (F10). There is no
# existing connect-deadline helper to reuse: coordinator_core.warm.client's only
# timeout surface is a per-op BUDGET resolver (`_mutation_deadline_for` ->
# `coordinator_core.ipc._timeout_for(method)`) that bounds an OP's execution once
# dispatched, not a raw connect attempt — pointing this probe at it would be wrong
# per the chunk brief. Hand-rolled instead via a bounded background thread (below),
# with this short, explicitly-named deadline: long enough to distinguish "reachable"
# from "hung", short enough not to stall a --cluster warm --include-live-roundtrip run.
_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS = 3.0


def _run_probe_warm_roundtrip(
    claude_klabauter_root: Path | None,
    include_live_roundtrip: bool,
) -> _ProbeResult:
    """Probe claude-klabauter.warm.roundtrip — OPTIONAL, weight=heavy, gated behind an explicit opt-in.

    SPLIT OUT OF C6 (staff-eng review, F2): a manifest `weight = "heavy"` /
    `triage = false` row governs OUTPUT MEMBERSHIP, not execution. The selector is a
    pre-run gate now, so it does keep this probe from running on a selection that
    excludes it — but a DEFAULT run selects nothing and runs everything, and that is
    the case this self-gate exists for. The `include_live_roundtrip` parameter,
    threaded from `main()`'s `--include-live-roundtrip` flag, is therefore still the
    only thing standing between a bare invocation and a live warm-server round-trip
    on every default run, every `--triage` run, and every CLI shell in the test suite.

    Default OFF: when `include_live_roundtrip` is False, this probe does NOT attempt
    a connection at all — it returns `skipped=True` (never an INFO stub) so it still
    appears as a row in `--cluster warm`'s membership rather than silently vanishing.
    A human running `--cluster warm --include-live-roundtrip` is the only path that
    exercises it for real.

    A live hit/miss round-trip: attempts one `try_warm_dispatch` ping in a
    daemon thread, bounded by `_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS` via
    `Thread.join(timeout=...)` (see the constant's own comment for why this
    is hand-rolled rather than reused from `ipc._timeout_for`). `try_warm_dispatch`
    itself never raises (its own Backstop 2) and returns `None` uniformly for
    every miss shape (warmth disabled, no pipe, busy, someone else's pipe, a
    broken mid-request pipe, read-deadline expiry) — this probe reports that
    as an informational MISS (`_INFO`, non-gating: cold dispatch is a normal,
    supported outcome), a HIT as `_PASS`, and a join-timeout (the thread still
    alive after the deadline — a hang, not a clean miss) as `_DEGRADED`.

    `required=False` throughout: a live warm-server reachability check is
    never allowed to gate an otherwise-healthy install.

    Question the sink cannot answer:
      Does the resident WARM path serve a round-trip right now, as opposed to
      whether dispatch works at all (that is claude-klabauter.invoke.smoke's question,
      over the spawn-per-call path — a different mechanism entirely)? The op
      census records what an op cost once it dispatched; it has no row for
      "was the warm server reachable and did it answer" because a cold
      dispatch that never touches warmth leaves no warm-specific trace to
      read back. RETAINED (disposition: pln-2026-08-27-the-undeclared-
      harness-and-the-redundant-probes § C4) — cheapest of the three retained
      probes to justify: default-OFF behind --include-live-roundtrip, so it
      costs nothing on a default run.

    Spec backlink: docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md § C9.
    """
    if not include_live_roundtrip:
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_INFO,
            detail=(
                "Live warm round-trip probe skipped — opt-in only. Pass "
                "--include-live-roundtrip to exercise it for real."
            ),
            remediation=(
                "Run: python3 bin/claude-klabauter-doctor-probe.py --cluster warm "
                "--include-live-roundtrip"
            ),
            required=False,
            skipped=True,
        )

    if claude_klabauter_root is None:
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_DEGRADED,
            detail="Cannot attempt a warm round-trip — COORDINATOR_ENGINE_ROOT unresolved.",
            remediation="Resolve COORDINATOR_ENGINE_ROOT first (see claude-klabauter.root.resolve probe).",
            required=False,
            skipped=True,
        )

    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        from coordinator_core.warm import client as warm_client
    except ImportError as exc:
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_DEGRADED,
            detail=f"coordinator_core.warm.client not importable: {exc}",
            remediation="Verify coordinator_core/ is present in COORDINATOR_ENGINE_ROOT (see claude-klabauter.core.import probe).",
            required=False,
            skipped=True,
        )

    msg = {"jsonrpc": "2.0", "id": "doctor-probe-warm-roundtrip", "method": "ping", "params": {}}
    result_box: dict[str, Any] = {}

    def _attempt() -> None:
        start = time.monotonic()
        try:
            response = warm_client.try_warm_dispatch(msg)
        except Exception as exc:  # try_warm_dispatch itself never raises; belt-and-braces only.
            result_box["error"] = f"{type(exc).__name__}: {exc}"
            result_box["latency_ms"] = (time.monotonic() - start) * 1000.0
            return
        result_box["response"] = response
        result_box["latency_ms"] = (time.monotonic() - start) * 1000.0

    thread = threading.Thread(target=_attempt, daemon=True)
    thread.start()
    thread.join(_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS)

    if thread.is_alive():
        # Review: coordinator:code-reviewer — required=False, skipped=True must be
        # paired here: _local_reduce_overall only consults `required` when `skipped`
        # is True, so an un-skipped DEGRADED would gate `overall` regardless of
        # required=False, contradicting this probe's never-gates docstring above.
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_DEGRADED,
            detail=(
                f"Warm round-trip attempt did not return within "
                f"{_WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS}s — treated as a hang, "
                "not a clean miss (the thread is left running as a daemon; it "
                "cannot be forcibly killed from here)."
            ),
            remediation=(
                "Investigate the resident warm server (see claude-klabauter.warm.residency) — "
                "a hung round-trip is not a normal miss."
            ),
            required=False,
            skipped=True,
            data={"timeout_seconds": _WARM_ROUNDTRIP_CONNECT_TIMEOUT_SECONDS},
        )

    if "error" in result_box:
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_DEGRADED,
            detail=f"Warm round-trip attempt raised unexpectedly: {result_box['error']}",
            remediation="Re-run the probe after investigating the error.",
            required=False,
            skipped=True,
            data={"latency_ms": result_box.get("latency_ms")},
        )

    response = result_box.get("response")
    latency_ms = result_box.get("latency_ms")

    if response is not None:
        return _ProbeResult(
            probe=_WARM_ROUNDTRIP_PROBE,
            status=_PASS,
            detail=f"Warm round-trip HIT — server answered in {latency_ms:.1f}ms.",
            remediation="—",
            required=False,
            data={"hit": True, "latency_ms": latency_ms},
        )

    return _ProbeResult(
        probe=_WARM_ROUNDTRIP_PROBE,
        status=_INFO,
        detail=f"Warm round-trip MISS — no warm server answered ({latency_ms:.1f}ms to conclude).",
        remediation="Cold dispatch is a normal, supported outcome — warmth is opportunistic, not required.",
        required=False,
        data={"hit": False, "latency_ms": latency_ms},
    )


# ---------------------------------------------------------------------------
# Probe manifest — doctor-probes.toml loader
# ---------------------------------------------------------------------------


def _load_probe_manifest(claude_klabauter_root: Path | None, step_zero: bool = False) -> dict[str, Any]:
    """Load bin/doctor-probes.toml and return {probe_id: metadata} map.

    Purpose: Provides the SSOT for per-probe metadata (triage flag, cluster, required).
    Used for selector validation and post-run result filtering.

    Resolves the toml path as <claude_klabauter_root>/bin/doctor-probes.toml.
    Falls back to a script-relative sibling path when claude_klabauter_root is None.
    Exits 2 with a loud remediation message if the toml is missing or unparseable —
    the manifest is load-bearing for selector behavior.

    Negative-spec: Does NOT silently fall back when the manifest is missing.
    """
    # Guard against tomllib=None so direct callers (unit tests, future scripts on
    # Python < 3.11) get a clear RuntimeError rather than AttributeError on 'None'.
    # Should be unreachable from main() because main() exits early on !_TOMLLIB_AVAILABLE.
    if not _TOMLLIB_AVAILABLE:
        raise RuntimeError("tomllib unavailable (Python < 3.11); cannot load probe manifest")
    if claude_klabauter_root is not None:
        toml_path = claude_klabauter_root / "bin" / "doctor-probes.toml"
    else:
        # Script and TOML are direct siblings under bin/.
        toml_path = Path(__file__).resolve().parent / "doctor-probes.toml"

    if not toml_path.exists():
        _detail = (
            f"probe manifest not found at {str(toml_path)!r}; "
            "verify doctor-probes.toml exists at bin/doctor-probes.toml in COORDINATOR_ENGINE_ROOT."
        )
        _remediation = (
            "Re-run python3 scripts/setup.py or restore bin/doctor-probes.toml "
            "from version control."
        )
        print(f"Error: {_detail}", file=sys.stderr)
        # Emit a parseable stdout envelope before exit so the probe's
        # always-emit-parseable-verdict contract is honoured for callers
        # (e.g. setup.py --step-zero) that rely on parseable stdout at all exit codes.
        if step_zero:
            sys.stdout.write(
                json.dumps(
                    {
                        "name": "claude-klabauter.manifest.load",
                        "status": _SZ_FAIL,
                        "severity": _SZ_HARD,
                        "detail": _detail,
                        "remediation": _remediation,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                json.dumps(
                    _manifest_broken_envelope(_detail, _remediation), indent=2, default=str
                )
                + "\n"
            )
        sys.stdout.flush()
        sys.exit(2)

    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        _detail = (
            f"could not parse probe manifest {str(toml_path)!r}: {exc}; "
            "verify doctor-probes.toml is valid TOML."
        )
        _remediation = (
            "Check the TOML syntax in bin/doctor-probes.toml. "
            "Validate with: python3 -c \"import tomllib; "
            "tomllib.load(open('bin/doctor-probes.toml','rb'))\""
        )
        print(f"Error: {_detail}", file=sys.stderr)
        # Same always-emit contract as the missing-manifest path above;
        # callers must never see exit 2 with no parseable stdout.
        if step_zero:
            sys.stdout.write(
                json.dumps(
                    {
                        "name": "claude-klabauter.manifest.load",
                        "status": _SZ_FAIL,
                        "severity": _SZ_HARD,
                        "detail": _detail,
                        "remediation": _remediation,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                json.dumps(
                    _manifest_broken_envelope(_detail, _remediation), indent=2, default=str
                )
                + "\n"
            )
        sys.stdout.flush()
        sys.exit(2)

    result: dict[str, Any] = {}
    for entry in data.get("probe", []):
        pid = entry.get("id")
        if pid:
            result[pid] = {
                "triage": bool(entry.get("triage", False)),
                "cluster": entry.get("cluster"),
                "required": bool(entry.get("required", True)),
            }
    return result


# ---------------------------------------------------------------------------
# Post-run selector — filter results per --triage / --cluster / --probe
# ---------------------------------------------------------------------------


_INFO_STUB_DETAIL = "probe declared in manifest but not yet implemented (forward-looking)"


def _apply_selector(
    results: list[_ProbeResult],
    manifest: dict[str, Any],
    args: "argparse.Namespace",
    known_ids: set[str] | None = None,
) -> list[_ProbeResult]:
    """Shape the emitted probe set per the selector flags in args.

    Purpose: the selection is applied BEFORE the run now (`run_probes(selected=...)`),
    so this is no longer where the cost is decided — it synthesises INFO stubs for
    manifest ids nothing implements, and guarantees a non-empty result.

    `known_ids` is what `run_probes` reports its call sites NAME. It exists to keep
    a mis-registration loud: without it, "absent from results" is ambiguous between
    *declared but never implemented* (an INFO stub, correct) and *implemented,
    selected, and did not run* — a wrong id constant or a dropped call site, which
    would otherwise be reported as a benign forward-looking stub at exit 0. Passing
    None keeps the pre-gate behaviour for callers that ran the full suite.

    Invariant: a valid manifest selector NEVER crashes on a legitimate id and NEVER
    returns an empty list.
    """
    implemented_ids = {r.probe for r in results}

    def _stub_or_raise(pid: str) -> _ProbeResult:
        """INFO stub for an unimplemented id; a hard error for one that should have run."""
        if known_ids is not None and pid in known_ids:
            raise RuntimeError(
                f"probe {pid!r} is registered in run_probes but produced no result for "
                "this selection — a mis-wired call site, not a forward-looking stub. "
                "This is never reported as INFO: see _apply_selector's docstring."
            )
        meta = manifest.get(pid, {})
        return _ProbeResult(
            probe=pid,
            status=_INFO,
            detail=_INFO_STUB_DETAIL,
            remediation="—",
            required=meta.get("required", True),
            skipped=False,
        )

    if args.triage:
        # Probes whose id appears in results but is absent from the manifest get
        # manifest.get(r.probe, {}) → {}, so .get("triage", False) → False, and they
        # are silently excluded. This is intentional: probe-id drift surfaces via
        # audit rather than triage output.
        filtered = [r for r in results if manifest.get(r.probe, {}).get("triage", False)]
        # Synthesise INFO stubs for triage=true probes declared in the manifest but not
        # yet implemented, matching the --cluster branch behaviour and honouring
        # the "NEVER returns an empty list" invariant.
        for pid, meta in manifest.items():
            if meta.get("triage", False) and pid not in implemented_ids:
                filtered.append(_stub_or_raise(pid))
        return filtered

    if args.cluster:
        cluster_name = args.cluster
        filtered = [r for r in results if manifest.get(r.probe, {}).get("cluster") == cluster_name]
        # Synthesise INFO stubs for unimplemented probes declared in this cluster.
        for pid, meta in manifest.items():
            if meta.get("cluster") == cluster_name and pid not in implemented_ids:
                filtered.append(_stub_or_raise(pid))
        return filtered

    if args.probe:
        probe_id = args.probe
        if probe_id in implemented_ids:
            return [r for r in results if r.probe == probe_id]
        # Unimplemented but valid manifest id (validated in main()) — or a
        # registered id that failed to run, which raises rather than stubbing.
        return [_stub_or_raise(probe_id)]

    # No selector — return all results unchanged.
    return results


# ---------------------------------------------------------------------------
# Probe orchestration
# ---------------------------------------------------------------------------


def _timed(fn: "Any", *args: "Any") -> _ProbeResult:
    """Run one probe function and stamp its wall time onto the result.

    Purpose: per-probe cost is the only thing that makes a doctor regression
    visible without re-running a hand-written profiler. The whole probe suite
    costs ~41s in-process and two probes carry ~72% of it
    (docs/research/2026-08-19-doctor-per-probe-cost-profile.md); before this
    wrapper the envelope carried no timing at all, so that split had to be
    re-derived by hand every time somebody asked where the time went.

    Negative-spec:
      - Does NOT swallow, retry, or bound a probe. A probe that raises still
        raises; timing is observation, never control flow.
      - Does NOT stamp synthesised results (selector INFO stubs, the
        dependency-skipped core.import placeholder) — those carry duration_ms
        None, which reads as "not run", not "free".
    """
    t0 = time.perf_counter()
    result = fn(*args)
    result.duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return result


def _run_if(
    selected: "set[str] | None",
    probe_id: str,
    fn: "Any",
    *args: "Any",
) -> _ProbeResult | None:
    """Run one probe only when the selection asks for it; time it when it runs.

    Purpose: the selector used to be a POST-run filter, so `--probe X` paid for
    every probe in the suite to emit one — 47.3s of work to report a 43.5ms
    answer (docs/research/2026-08-19-doctor-per-probe-cost-profile.md). Gating
    here, before the call, is what makes a scalpel run cost what a scalpel
    costs. `selected is None` means "no selector" and runs everything, which is
    what every default run, `--step-zero`, and this module's own full-suite
    tests take.

    Returns None for a probe the selection excludes; `run_probes` drops those
    rather than appending a placeholder, so a skipped probe is ABSENT from the
    result set — never a row that could read as PASS.

    Negative-spec:
      - Does NOT decide the selection: `main()` derives it from the manifest.
      - Does NOT swallow a probe's exception, and does NOT bound its runtime.

    Spec backlink: docs/plans/2026-08-19-the-selector-gates-before-the-run.md § C1.
    """
    if selected is not None and probe_id not in selected:
        return None
    return _timed(fn, *args)


def run_probes(
    *,
    include_live_roundtrip: bool = False,
    selected: "set[str] | None" = None,
) -> tuple[list[_ProbeResult], Path | None, set[str]]:
    """Run the static probe suite, or the *selected* subset of it.

    Returns (results, claude_klabauter_root_or_None, known_ids).
    The returned claude_klabauter_root is the resolved path when probe 1 succeeds; None otherwise.
    `known_ids` is every probe id this function's call sites NAME, whether or not
    the selection ran them — so `known_ids == set(manifest)` is a sub-second,
    in-process registration check that catches a probe declared in
    doctor-probes.toml with no call site here, without paying a full suite run.

    `selected` (default None) gates each probe BEFORE it runs: None runs
    everything in dependency order, exactly as a default run always has; a set
    runs only its members. Two things are prerequisites and run whatever the
    selection says — `_run_probe_claude_klabauter_root` (every other probe takes
    claude_klabauter_root) and `_ensure_core_importable` (five probes import
    coordinator_core without arranging sys.path themselves). The root probe's
    RESULT is still emitted only when selected.

    `include_live_roundtrip` (default False) gates ONLY `claude-klabauter.warm.roundtrip`
    (§ C9), independently of `selected`: opting out keeps the probe from
    attempting a live connection even when the selection names it. False is the correct default for every caller that does not
    explicitly opt in — the bare CLI invocation, `--triage`, `--cluster warm`
    without the flag, and this module's own test suite all must skip the live
    round-trip by default (see `_run_probe_warm_roundtrip`'s own docstring for
    why this is a run-time gate, not a manifest-only one).

    DR-215: all live-probe machinery (UDS ping, shim handshake, shim harness-env) was
    retired — coordinator_core is a command-type engine with no resident process to probe.
    Retired under docs/plans/2026-07-06-claude-klabauter-doctor-prose-based-command-type.md § C1a.
    """
    results: list[_ProbeResult] = []
    known_ids: set[str] = set()

    def _add(probe_id: str, fn: "Any", *args: "Any") -> None:
        """Register *probe_id* as known, then run it if the selection includes it."""
        known_ids.add(probe_id)
        result = _run_if(selected, probe_id, fn, *args)
        if result is not None:
            results.append(result)

    # Prerequisite 1: COORDINATOR_ENGINE_ROOT (REQUIRED as a probe, unconditional as a dependency).
    # Every other probe takes claude_klabauter_root, so this runs whatever the selection says;
    # only its RESULT is gated.
    known_ids.add(_CLAUDE_KLABAUTER_ROOT_PROBE)
    _t0 = time.perf_counter()
    probe1, claude_klabauter_root = _run_probe_claude_klabauter_root()
    probe1.duration_ms = round((time.perf_counter() - _t0) * 1000.0, 1)
    if selected is None or _CLAUDE_KLABAUTER_ROOT_PROBE in selected:
        results.append(probe1)

    # Prerequisite 2: sys.path. NOT a probe — six probes import coordinator_core
    # without arranging it themselves, and used to free-ride on the insert
    # claude-klabauter.core.import left behind. See _ensure_core_importable.
    _ensure_core_importable(claude_klabauter_root)

    # Probe 2: registry key (REQUIRED when machine-local present; OPTIONAL otherwise)
    # Runs independently of probe 1 — it checks the machine-local registration path
    # directly, which may differ from the resolution path used in probe 1.
    _add(_REGISTRY_KEY_PROBE, _run_probe_registry_key)

    # Probe 3: import coordinator_core (REQUIRED; depends on probe 1)
    known_ids.add(_CORE_IMPORT_PROBE)
    if selected is None or _CORE_IMPORT_PROBE in selected:
        if claude_klabauter_root is not None:
            results.append(_timed(_run_probe_core_import, claude_klabauter_root))
        else:
            results.append(_ProbeResult(
                probe=_CORE_IMPORT_PROBE,
                # status is ignored when skipped=True (overridden to DEGRADED by the envelope
                # builder); _INFO is the least-misleading placeholder.
                status=_INFO,
                detail="Probe skipped — COORDINATOR_ENGINE_ROOT unresolved (see claude-klabauter.root.resolve).",
                remediation="Resolve COORDINATOR_ENGINE_ROOT first (probe 1 remediation).",
                skipped=True,
            ))

    # Command-type static checks (DR-215 rebuild § C1b). Each accepts claude_klabauter_root
    # (Path | None) and self-handles the unresolved case. Manifest triage flags govern
    # which ids reach `selected`; the id named here is the same symbol the probe's own
    # _ProbeResult carries, so the two cannot drift.
    _add(_DIALECT_GUARD_ARMED_PROBE, _run_probe_dialect_guard_armed, claude_klabauter_root)
    _add(_SETTINGS_HOME_COMPLETE_PROBE, _run_probe_settings_home_complete, claude_klabauter_root)
    _add(_ENTRYPOINTS_PATH_RESOLVED_PROBE, _run_probe_entrypoints_path_resolved, claude_klabauter_root)
    _add(_RESIDENT_DEBRIS_PROBE, _run_probe_resident_debris, claude_klabauter_root)
    _add(_WORKTREE_BLOAT_PROBE, _run_probe_worktree_bloat, claude_klabauter_root)
    _add(_VERSION_SANITY_PROBE, _run_probe_version_sanity, claude_klabauter_root)
    _add(_INVOKE_SMOKE_PROBE, _run_probe_invoke_smoke, claude_klabauter_root)
    _add(_STRATEGIC_DRAFT_STALENESS_PROBE, _run_probe_strategic_draft_staleness, claude_klabauter_root)
    _add(_VENDOR_DRIFT_PROBE, _run_probe_vendored_schema_drift, claude_klabauter_root)
    _add(_GENERATOR_STALENESS_PROBE, _run_probe_generator_output_staleness, claude_klabauter_root)
    _add(_COMMITMENTS_RECHECK_PROBE, _run_probe_commitments_recheck, claude_klabauter_root)
    _add(_STABLE_PID_MISS_PROBE, _run_probe_stable_pid_miss, claude_klabauter_root)
    _add(_ROOT_POINTER_PROBE, _run_probe_root_pointer, claude_klabauter_root)
    _add(_ROOT_CHANNELS_PROBE, _run_probe_root_channels_reconciled, claude_klabauter_root)
    _add(_PUBLISH_PROVENANCE_PROBE, _run_probe_publish_provenance, claude_klabauter_root)
    _add(_ENGINE_TARGET_ROLLOUT_PROBE, _run_probe_engine_target_rollout)
    _add(_INVOKE_LATENCY_PROBE, _run_probe_invoke_latency, claude_klabauter_root)
    _add(_EXECNET_GATEWAY_PROBE, _run_probe_orphaned_execnet_gateways)
    _add(_LAUNCH_CHAIN_PROBE, _run_probe_launch_chain)
    _add(_WARM_RESIDENCY_PROBE, _run_probe_warm_residency, claude_klabauter_root)
    _add(_WARM_GENERATION_PROBE, _run_probe_warm_generation, claude_klabauter_root)
    _add(_WARM_ROUTE_SHARE_PROBE, _run_probe_warm_route_share, claude_klabauter_root)
    _add(_WARM_ROUNDTRIP_PROBE, _run_probe_warm_roundtrip, claude_klabauter_root, include_live_roundtrip)

    return results, claude_klabauter_root, known_ids


# ---------------------------------------------------------------------------
# Step-zero NDJSON emission (step-zero-emitter-contract.md)
# ---------------------------------------------------------------------------


def _sz_status(r: _ProbeResult) -> str:
    """Map _ProbeResult to step-zero status vocabulary (lowercase)."""
    if r.skipped:
        return _SZ_INCONCLUSIVE
    mapping = {
        _PASS: _SZ_PASS,
        _BROKEN: _SZ_FAIL,
        _DEGRADED: _SZ_FAIL,
        _INFO: _SZ_WARN,
    }
    return mapping.get(r.status, _SZ_INCONCLUSIVE)


def _sz_severity(r: _ProbeResult) -> str:
    """Map _ProbeResult.required to step-zero severity vocabulary."""
    return _SZ_HARD if r.required else _SZ_ADVISORY


def emit_step_zero(results: list[_ProbeResult]) -> int:
    """Emit one NDJSON line per probe per step-zero-emitter-contract.md.

    Returns exit code: 1 if any REQUIRED probe has status BROKEN/DEGRADED or
    is skipped (inconclusive), else 0.

    Uses Python's json.dumps for string escaping, which implements the five-escape
    contract (backslash, double-quote, CR, LF, TAB) in the required order.
    """
    any_required_fail = False
    for r in results:
        sz_st = _sz_status(r)
        sz_sv = _sz_severity(r)
        line = json.dumps(
            {
                "name": r.probe,
                "status": sz_st,
                "severity": sz_sv,
                # Advisory failures say so in their own detail text — the five keys,
                # their names, the status/severity vocabulary and the exit rule below
                # are all unchanged. See _mark_advisory_detail.
                "detail": _mark_advisory_detail(r.detail, r.required, r.status),
                "remediation": r.remediation,
            },
            separators=(",", ":"),
        )
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if r.required and sz_st in (_SZ_FAIL, _SZ_INCONCLUSIVE):
            any_required_fail = True
    return 1 if any_required_fail else 0


# ---------------------------------------------------------------------------
# Aggregate envelope emission (default mode)
# ---------------------------------------------------------------------------


def _build_enriched_envelope(
    results: list[_ProbeResult],
    claude_klabauter_root: Path | None,
    manifest: dict[str, Any] | None = None,
    suite_total_ms: float | None = None,
) -> dict[str, Any]:
    """Build the full JSON verdict envelope (does not print).

    Uses coordinator_core.doctor_envelope.build_envelope when coordinator_core is
    importable (honouring the C1 spec requirement); falls back to the local
    implementation on a broken tree.

    Enriches each probe row with ``required``, ``skipped``, and ``cluster`` fields
    (populated from the original _ProbeResult and the doctor-probes.toml manifest).
    These fields are omitted by the shared build_envelope() since they are
    probe-script-level metadata; this probe is their authoritative source.
    """
    envelope = _build_envelope_via_module(results, claude_klabauter_root)

    # Enrich probe rows with required / skipped (from _ProbeResult) and
    # cluster (from manifest).  Post-processing applies to both the module path
    # and the local fallback path so the shape is consistent regardless of whether
    # coordinator_core is importable.
    results_by_id: dict[str, _ProbeResult] = {r.probe: r for r in results}
    manifest_safe: dict[str, Any] = manifest or {}
    for row in envelope.get("probes", []):
        pid = row.get("probe")
        if not pid:
            continue
        pr = results_by_id.get(pid)
        if pr is not None:
            row["required"] = pr.required
            row["skipped"] = pr.skipped
            # Same advisory marking as the step-zero path, so the envelope an agent
            # reads and the NDJSON an installer reads carry the identical signal.
            # Keys/statuses untouched — only the free-text detail is prefixed.
            row["detail"] = _mark_advisory_detail(
                str(row.get("detail") or ""), pr.required, str(row.get("status") or "")
            )
            # Additive per-row key (see _write_doctor_sentinel's ADDITIVE-KEY
            # POLICY): a consumer that does not know about it is unaffected, and
            # None means "not run", never "free".
            row["duration_ms"] = pr.duration_ms
        row["cluster"] = manifest_safe.get(pid, {}).get("cluster")

    # Additive top-level keys. `probe_total_ms` is the cost of what was EMITTED;
    # `probe_suite_total_ms` is the cost of what was RUN. Post-gate, on every
    # selector path, the two are equal by construction: `_apply_selector` only
    # ever narrows `results` to members `run_probes` already gated, so the set
    # of timed rows is identical going in. Neither field counts the
    # unconditional prerequisites (`_run_probe_claude_klabauter_root`'s own resolution,
    # `_ensure_core_importable`) — measured at ~8ms against a 41-53s suite,
    # small enough that folding it in isn't worth a second signature change to
    # `run_probes`. The pair still earns its keep because `suite_total_ms` is
    # summed from the PRE-filter `results` in `main()`: if a future change ever
    # runs more than it emits again, the two fields will diverge and the
    # regression becomes visible in the envelope, not only in wall-clock.
    measured = [r.duration_ms for r in results if r.duration_ms is not None]
    if measured:
        envelope["probe_total_ms"] = round(sum(measured), 1)
    if suite_total_ms is not None:
        envelope["probe_suite_total_ms"] = round(suite_total_ms, 1)

    return envelope


def emit_envelope(
    results: list[_ProbeResult],
    claude_klabauter_root: Path | None,
    manifest: dict[str, Any] | None = None,
    suite_total_ms: float | None = None,
) -> int:
    """Emit the full JSON verdict envelope; always exits 0."""
    envelope = _build_enriched_envelope(results, claude_klabauter_root, manifest, suite_total_ms)
    sys.stdout.write(json.dumps(envelope, indent=2, default=str) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Python-version / tomllib-absent broken envelope
# ---------------------------------------------------------------------------


def _python_version_broken_envelope() -> dict[str, Any]:
    """Return a schema_version 1 BROKEN envelope for the Python version gate.

    Purpose: Provides a parseable verdict when the probe is invoked under
    Python < 3.11, where tomllib is absent.  Mirrors the exact top-level shape
    produced by _local_build_envelope() + the emit_envelope() enrichment step
    (required / skipped / cluster fields on each probe row).

    Negative-spec: Does NOT depend on tomllib — safe to call when
    _TOMLLIB_AVAILABLE is False.

    Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C1
    """
    # Use module-level constants to eliminate the DRY violation between this
    # function and the step-zero path in main().
    detail = _PYVER_DETAIL
    remediation = _PYVER_REMEDIATION
    return {
        "schema_version": 1,
        "status_vocab": [_BROKEN, _DEGRADED, _INFO, _PASS],
        "overall": _BROKEN,
        "probes": [
            {
                "probe": "claude-klabauter.python.version",
                "status": _BROKEN,
                "detail": detail,
                "remediation": remediation,
                "data": None,
                "required": True,
                "skipped": False,
                "cluster": "install",
            }
        ],
        "warnings": [],
        "missing_optional": [],
    }


def _manifest_broken_envelope(detail: str, remediation: str) -> dict[str, Any]:
    """Return a schema_version 1 BROKEN envelope for manifest-load failures.

    Purpose: Provides a parseable verdict when the probe manifest is missing or
    unparseable, so callers that read stdout always receive a schema_version-1 envelope
    regardless of exit code.  Mirrors the shape produced by _python_version_broken_envelope().

    Negative-spec: Does NOT depend on tomllib — safe to call when the manifest cannot
    be loaded.

    Honors the always-emit-parseable-verdict contract on both manifest-missing and
    manifest-unparseable paths in _load_probe_manifest().

    Spec backlink: pln-claude-klabauter-install-doctor-system-f-537d61 § C1
    """
    return {
        "schema_version": 1,
        "status_vocab": [_BROKEN, _DEGRADED, _INFO, _PASS],
        "overall": _BROKEN,
        "probes": [
            {
                "probe": "claude-klabauter.manifest.load",
                "status": _BROKEN,
                "detail": detail,
                "remediation": remediation,
                "data": None,
                "required": True,
                "skipped": False,
                "cluster": "install",
            }
        ],
        "warnings": [],
        "missing_optional": [],
    }


# ---------------------------------------------------------------------------
# Health sentinel — state/doctor-last-run.json
#
# Relocated from skills/doctor/SKILL.md Step 1.5 (that SKILL.md step is being
# retired) so DoE-claude's /workday-start consumer (coordinator_core.ops.
# check_claude_klabauter_doctor_sentinel) keeps seeing a fresh sentinel.
#
# Two decisions already made (do not re-decide):
#   (i)  ONLY --triage and full-run (default, no selector) write the sentinel.
#        --step-zero, --probe, and --cluster do NOT — --step-zero runs the full
#        probe set (a different, larger population than the triage subset the
#        sentinel's verdict is calibrated against), and --probe/--cluster are
#        scalpel runs that must not clobber the fleet-facing sentinel.
#   (ii) state/doctor-last-run.json is in-repo but GITIGNORED, not tracked
#        (.gitignore, "Per-machine cadence/health sentinels"). This reverses the
#        original decision, which read CLAUDE.md's Durable-data plane § as
#        ratifying a tracked convention; that paragraph was establishing claude-klabauter
#        holds no data OUTSIDE the repo tree, and said "tracked" only in passing.
#        Tracked was an active cross-machine hazard: the sentinel records what
#        happened ON THIS BOX, so a synced copy makes check_claude_klabauter_doctor_sentinel's
#        own absent-means-"doctor never run on this machine" branch unreachable —
#        a fresh clone boots holding a peer's GREEN/AMBER and its red_probes.
#        In-repo (so the writer below needs no path change) and untracked.
# ---------------------------------------------------------------------------

_SENTINEL_VERDICT_MAP: dict[str, str] = {_BROKEN: "RED", _DEGRADED: "AMBER", _PASS: "GREEN"}


def _sentinel_verdict(envelope: dict[str, Any]) -> str:
    """Map envelope.overall -> sentinel verdict (BROKEN->RED, DEGRADED->AMBER, PASS->GREEN).

    INFO never reaches envelope.overall per the reduction rule (see
    _local_reduce_overall / coordinator_core.doctor_envelope.reduce_overall), so no
    INFO mapping is needed. Falls back to "AMBER" for any unrecognised overall value
    as a safe middle ground (never silently claim GREEN on an unmapped verdict).
    """
    return _SENTINEL_VERDICT_MAP.get(str(envelope.get("overall")), "AMBER")


def _sentinel_red_probes(envelope: dict[str, Any]) -> list[str]:
    """Array of probe ids whose row status == BROKEN — filtered strictly by status,
    never by envelope.overall (a DEGRADED/AMBER overall must not contribute ids here).
    """
    return [
        row.get("probe")
        for row in envelope.get("probes", [])
        if row.get("status") == _BROKEN and row.get("probe")
    ]


def _sentinel_hint(envelope: dict[str, Any]) -> str:
    """One-line operator-actionable hint: first BROKEN probe, else first DEGRADED, else ''."""
    for target_status in (_BROKEN, _DEGRADED):
        for row in envelope.get("probes", []):
            if row.get("status") == target_status:
                return f"{row.get('probe')} — {row.get('remediation')}"
    return ""


def _sentinel_vendor_drift(envelope: dict[str, Any]) -> dict[str, Any]:
    """Reduce the claude-klabauter.schema.vendor_drift probe row to the sentinel's `vendor_drift` key.

    PUBLIC key — see `_write_doctor_sentinel`'s docstring. Sourced from that probe's
    own `data` dict (built by `_run_probe_vendored_schema_drift`), not re-derived.

    Returns {"status", "checked", "drifted": [...], "indeterminate": [...]} always —
    even when the probe row is ABSENT from this run's envelope (e.g. a `--probe`
    scalpel run for a different probe, or a manifest edit that drops it from the
    triage set). In that absent case: status="UNKNOWN", checked=None, drifted=[],
    indeterminate=[]. This is deliberate: an absent probe row and "we checked and it
    was clean" must never collapse to the same on-disk shape, and a consumer must
    never be able to read a missing/empty-looking key as "no drift" — see this
    module's Negative-spec and schema_drift_watch.py's Public-seam paragraph on why
    UNRESOLVED/INDETERMINATE/absent are kept distinct from MATCH throughout this
    whole chain.

    Never raises — mirrors _write_doctor_sentinel's own contract; a malformed row
    (unexpected shape) degrades to the same UNKNOWN default rather than raising.
    """
    try:
        for row in envelope.get("probes", []):
            if row.get("probe") != _VENDOR_DRIFT_PROBE:
                continue
            data = row.get("data") or {}
            return {
                "status": str(data.get("status") or row.get("status") or "UNKNOWN"),
                "checked": data.get("checked"),
                "drifted": [
                    {
                        "schema": d.get("schema"),
                        "direction": d.get("direction"),
                        "local_version": d.get("local_version"),
                        "doe_version": d.get("doe_version"),
                    }
                    for d in data.get("drifted") or []
                ],
                "indeterminate": list(data.get("indeterminate") or []),
            }
    except Exception:
        pass
    return {"status": "UNKNOWN", "checked": None, "drifted": [], "indeterminate": []}


def _sentinel_advisory_only(envelope: dict[str, Any]) -> bool:
    """True iff the envelope's DEGRADED/BROKEN overall is driven entirely by
    non-required probe rows — i.e. no required-probe row is BROKEN or DEGRADED.

    Root cause this key exists to fix: `_local_reduce_overall` (and its shared
    twin `coordinator_core.doctor_envelope.reduce_overall`) take a RAN probe's
    `status` into the worst-of reduction regardless of `required` — a
    required=False DEGRADED probe (e.g. Claude-klabauter.schema.vendor_drift) drags
    `envelope.overall` to DEGRADED exactly like a real prerequisite failure
    would. `_sentinel_verdict` then maps that to AMBER (or BROKEN to RED) with
    no way for a reader to tell the two cases apart. This function reconstructs
    the distinction from the already-enriched `required` field the envelope
    rows carry (`_build_enriched_envelope`), so `check_claude_klabauter_doctor_sentinel`
    can render ADVISORY instead of a bare AMBER/RED when nothing required
    actually failed.

    Returns False for a GREEN/INFO envelope (the distinction is moot), False
    whenever any required probe row is itself BROKEN or DEGRADED (real
    failure — render as before), and False on any malformed envelope shape —
    the safe default is "render as a real failure", never a silent ADVISORY
    downgrade of something this function failed to classify.

    Never raises — mirrors `_sentinel_vendor_drift`'s own contract.
    """
    try:
        overall = str(envelope.get("overall") or "")
        if overall not in (_BROKEN, _DEGRADED):
            return False
        for row in envelope.get("probes", []):
            if row.get("status") in (_BROKEN, _DEGRADED) and row.get("required") is True:
                return False
        return True
    except Exception:
        return False


def _write_doctor_sentinel(envelope: dict[str, Any], claude_klabauter_root: Path) -> None:
    """Write state/doctor-last-run.json from this run's envelope.

    ADDITIVE-KEY POLICY: the original 7 keys (ran_at, ts, verdict, red_probes, hint,
    schema_version, plugin) keep their exact current shape and semantics forever —
    do not add/remove/rename any of those seven without updating
    coordinator_core.ops.check_claude_klabauter_doctor_sentinel in lockstep. `schema_version`
    stays at 1 for a backward-compatible additive change; it is bumped only for a
    breaking change to an EXISTING key. New top-level keys MAY be added beside them
    without a version bump, provided they are additive-only (never present) and
    every existing consumer (check_claude_klabauter_doctor_sentinel) tolerates their absence
    on an older sentinel already on disk.

    `vendor_drift` is exactly such an additive key (2026-07-26, cross-repo
    ratification — see cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md).
    It is a DOCUMENTED PUBLIC key — external consumers (DoE-claude) MAY gate a
    commit-time check on it directly, unlike the rest of this sentinel's contents
    which are claude-klabauter-internal cadence output. See _sentinel_vendor_drift's docstring
    for its exact shape and the absent-probe-row UNKNOWN default, and
    schema_drift_watch.py's "Public seam" docstring paragraph for why a commit-time
    gate should prefer calling scan_vendored_schema_drift() live over reading this
    (necessarily daily-stale) sentinel value.

    `advisory_only` is another such additive key (2026-08-17): True iff the
    verdict/red_probes/hint above reflect a non-required-only degradation (see
    `_sentinel_advisory_only`). A reader tolerating its absence (older sentinel,
    pre-key) must fall back to today's un-distinguished AMBER/RED rendering.

    Sentinel-write cadence is UNCHANGED by this key: still only `--triage` and full
    runs write this file (see the call site below) — `--step-zero`, `--probe`, and
    `--cluster` scalpel runs still do not, deliberately (see the block comment above
    `_SENTINEL_VERDICT_MAP`).

    If the envelope is not a parseable dict (should be unreachable — this function
    is only ever called with an envelope this script itself just built), do NOT
    write a sentinel: a corrupt sentinel is worse than a stale one.

    Never raises — a sentinel-write failure must not crash the probe run itself.
    """
    if not isinstance(envelope, dict) or "overall" not in envelope or "probes" not in envelope:
        return
    try:
        now = time.time()
        sentinel = {
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "ts": int(now),
            "verdict": _sentinel_verdict(envelope),
            "red_probes": _sentinel_red_probes(envelope),
            "hint": _sentinel_hint(envelope),
            "schema_version": 1,
            "plugin": "claude-klabauter",
            "vendor_drift": _sentinel_vendor_drift(envelope),
            "advisory_only": _sentinel_advisory_only(envelope),
        }
        sentinel_path = claude_klabauter_root / "state" / "doctor-last-run.json"
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        sentinel_path.write_text(json.dumps(sentinel, indent=2) + "\n", encoding="utf-8", newline="\n")
    except Exception as exc:
        # Sentinel-writing is best-effort ancillary work; never let it crash the probe.
        # stderr only — stdout is reserved for the JSON envelope / NDJSON contract.
        print(
            f"WARNING: failed to write state/doctor-last-run.json sentinel: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Advisory-vs-hard severity legend (stderr)
# ---------------------------------------------------------------------------


def _emit_severity_legend(results: list[_ProbeResult]) -> None:
    """Write a one-block advisory-vs-hard summary of this run's failures to STDERR.

    Companion to `_mark_advisory_detail`: the marker tells a reader that ONE row is
    advisory; this tells them how the run's failures split overall, which is the
    question a fresh installer actually has ("is anything here blocking me?").

    STDERR only — stdout is reserved for the JSON envelope / NDJSON contract, and
    `scripts/setup.py` concatenates our stderr onto our stdout before rendering, so
    a stdout write here would corrupt a machine consumer's parse. Deliberately NOT
    called from the `--step-zero` path for the same reason: that mode's consumer
    reads strict NDJSON, and interleaving prose into it is exactly the corruption
    this note exists to avoid.

    Negative-spec: emits nothing at all when there are no failures — a clean run
    must not grow a new always-on banner. Never raises; never affects the exit code.
    """
    try:
        hard = [r for r in results if r.required and not r.skipped
                and r.status in (_BROKEN, _DEGRADED)]
        advisory = [r for r in results if not r.required and not r.skipped
                    and r.status in (_BROKEN, _DEGRADED)]
        if not hard and not advisory:
            return
        print(file=sys.stderr)
        if hard:
            print(
                f"BLOCKING ({len(hard)}): these are REQUIRED probes — the install is not "
                "healthy until each is resolved.",
                file=sys.stderr,
            )
            for r in hard:
                print(f"  - {r.probe}", file=sys.stderr)
        if advisory:
            print(
                f"ADVISORY ({len(advisory)}): non-gating. These probes are declared "
                "required=false; they never break the install, never fail --step-zero, "
                "and never change an exit code. Action them on their own cadence, not as "
                "an install step.",
                file=sys.stderr,
            )
            for r in advisory:
                print(f"  - {r.probe}", file=sys.stderr)
        if not hard:
            print(
                "No REQUIRED probe failed in this run — nothing above is blocking.",
                file=sys.stderr,
            )
    except Exception:
        # Rendering is ancillary; a legend failure must never crash a probe run.
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="claude-klabauter-doctor-probe",
        description="claude-klabauter Tier-1 static diagnostic probe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default mode: emit full JSON verdict envelope (exit 0).\n"
            "--step-zero mode: emit five-key NDJSON per step-zero-emitter-contract.md "
            "(exit 1 if any REQUIRED probe fails).\n"
            "--triage: emit triage probe set (triage=true probes from doctor-probes.toml).\n"
            "--cluster NAME: emit probes in the named cluster (declared in doctor-probes.toml).\n"
            "--probe ID: emit exactly the named probe by manifest id."
        ),
    )

    # All four mode flags are mutually exclusive: only one may be supplied.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--step-zero",
        action="store_true",
        help=(
            "Emit five-key NDJSON probe lines per step-zero-emitter-contract.md. "
            "Exit 1 if any REQUIRED probe is BROKEN/DEGRADED/inconclusive; exit 0 otherwise."
        ),
    )
    mode_group.add_argument(
        "--triage",
        action="store_true",
        help=(
            "Emit triage probe set only (probes with triage=true in doctor-probes.toml). "
            "Emits the full JSON verdict envelope filtered to the triage set; exit 0."
        ),
    )
    mode_group.add_argument(
        "--cluster",
        metavar="NAME",
        help=(
            "Emit probes in the named cluster as declared in doctor-probes.toml. "
            "Unknown cluster name → exit 2."
        ),
    )
    mode_group.add_argument(
        "--probe",
        metavar="ID",
        help=(
            "Emit exactly the named probe by manifest id. "
            "Unknown id → exit 2. Declared-but-unimplemented id → INFO stub (never empty)."
        ),
    )

    # Not part of the mutually-exclusive mode group — orthogonal opt-in, combinable
    # with any of the above (e.g. --cluster warm --include-live-roundtrip). Default
    # OFF everywhere: the bare CLI invocation, --triage, --cluster warm, and the test
    # suite's default runs must all skip claude-klabauter.warm.roundtrip's live connection
    # attempt unless this flag is explicitly passed (§ C9).
    parser.add_argument(
        "--include-live-roundtrip",
        action="store_true",
        help=(
            "Opt in to the live warm round-trip probe (claude-klabauter.warm.roundtrip, "
            "weight=heavy). Without this flag the probe reports skipped=True and "
            "does not attempt a connection. Combine with --cluster warm to see it."
        ),
    )

    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Python version / tomllib gate — FIRST check after arg parsing.
    # Emits a well-formed verdict and returns 1 without running probes or
    # attempting to load the manifest (which itself requires tomllib).
    # -----------------------------------------------------------------------
    if not _TOMLLIB_AVAILABLE:
        # Use module-level constants; eliminates the DRY violation with
        # _python_version_broken_envelope().
        _detail = _PYVER_DETAIL
        _remediation = _PYVER_REMEDIATION
        if args.step_zero:
            sys.stdout.write(
                json.dumps(
                    {
                        "name": "claude-klabauter.python.version",
                        "status": _SZ_FAIL,
                        "severity": _SZ_HARD,
                        "detail": _detail,
                        "remediation": _remediation,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                json.dumps(_python_version_broken_envelope(), indent=2, default=str) + "\n"
            )
        sys.stdout.flush()
        return 1

    # -----------------------------------------------------------------------
    # Load the probe manifest early — needed for selector validation and
    # cluster enrichment.  Resolves the toml path via claude_klabauter_root (rung 3
    # auto-discovery used when COORDINATOR_ENGINE_ROOT env / machine-local are absent).
    # Exits 2 if the manifest is missing or unparseable.
    # -----------------------------------------------------------------------
    early_root, _ = _resolve_claude_klabauter_root()
    # Pass step_zero so _load_probe_manifest emits the correct stdout format
    # (NDJSON vs JSON envelope) on manifest-error paths.
    manifest = _load_probe_manifest(early_root, step_zero=args.step_zero)

    # Validate selector args against the manifest before running probes.
    if args.cluster:
        valid_clusters = sorted({
            m["cluster"] for m in manifest.values() if m.get("cluster")
        })
        if args.cluster not in valid_clusters:
            print(
                f"Error: unknown cluster {args.cluster!r}. "
                f"Valid clusters: {', '.join(valid_clusters)}",
                file=sys.stderr,
            )
            sys.exit(2)

    if args.probe and args.probe not in manifest:
        print(
            f"Error: {args.probe!r} is not a known probe id in the manifest. "
            f"Known ids: {', '.join(sorted(manifest.keys()))}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Derive the selected id set from the MANIFEST before running anything: the
    # selector is a PRE-run gate, so `--probe X` costs one probe rather than the
    # whole suite (docs/plans/2026-08-19-the-selector-gates-before-the-run.md).
    # None means "no selector — run everything", which is what a bare invocation
    # and --step-zero both take: --step-zero shares the mutually-exclusive mode
    # group with the three selectors, so it can never carry one.
    selected: set[str] | None
    if args.probe:
        selected = {args.probe}
    elif args.cluster:
        selected = {
            pid for pid, meta in manifest.items() if meta.get("cluster") == args.cluster
        }
    elif args.triage:
        selected = {pid for pid, meta in manifest.items() if meta.get("triage", False)}
    else:
        selected = None

    results, claude_klabauter_root, known_ids = run_probes(
        include_live_roundtrip=args.include_live_roundtrip,
        selected=selected,
    )
    suite_total_ms = sum(r.duration_ms for r in results if r.duration_ms is not None)

    # Shape what is emitted (INFO stubs for declared-but-unimplemented ids).
    results = _apply_selector(results, manifest, args, known_ids)

    if args.step_zero:
        return emit_step_zero(results)

    envelope = _build_enriched_envelope(results, claude_klabauter_root, manifest, suite_total_ms)

    # Sentinel write — ONLY --triage and full-run (no selector at all) write
    # state/doctor-last-run.json. --cluster and --probe are scalpel runs and must
    # not touch the fleet-facing sentinel. --step-zero already returned above.
    is_full_run = not (args.triage or args.cluster or args.probe)
    if (args.triage or is_full_run) and claude_klabauter_root is not None:
        _write_doctor_sentinel(envelope, claude_klabauter_root)

    sys.stdout.write(json.dumps(envelope, indent=2, default=str) + "\n")
    sys.stdout.flush()
    _emit_severity_legend(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())

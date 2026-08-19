"""
bin/claude-klabauter-doctor-probe.py — Tier-1 static diagnostic probe for the claude-klabauter install chain.

Purpose: Exercises the claude-klabauter bootstrap chain without depending on a live coordinator_core
process or any resident process.  Invoked directly as `python bin/claude-klabauter-doctor-probe.py`;
run by scripts/setup.py (guarantee-3 post-install verification) and by
the test suite.

DR-215: coordinator_core is a command-type, spawn-per-call engine — no resident process,
no UDS server, no MCP shim liveness to check.  All probes in this file are static checks.

Checks — seven ProbeResult objects, in dependency order:

  claude-klabauter.root.resolve    REQUIRED — CLAUDE_KLABAUTER_ROOT resolves via env/machine-local/git-root.
  claude-klabauter.registry.key    REQUIRED (OPTIONAL when machine-local absent) — repos.claude_klabauter
                         registered.  Skipped as advisory when coordinator-claude is not
                         installed (machine-local absent → key not needed).
  claude-klabauter.core.import     REQUIRED — import coordinator_core succeeds from CLAUDE_KLABAUTER_ROOT.
  claude-klabauter.resident.debris REQUIRED — detects stale paths from the retired daemon (INFO
                         when found; PASS when absent).
  claude-klabauter.version.sanity  REQUIRED — coordinator_core version helper resolves; retired
                         submodule coordinator_core.client correctly absent.
  claude-klabauter.invoke.smoke    OPTIONAL — spawn-per-call dispatch smoke via coordinator_core.invoke
                         ping; proves entrypoint can dispatch (not session liveness).
  claude-klabauter.strategic.draft_staleness  OPTIONAL — nudges when state/strategic/self-description
                         .draft.yaml is missing (SKIP, not a fault) or older than the newest
                         state/week-changelog/*.md entry (INFO nudge).
  claude-klabauter.schema.vendor_drift  OPTIONAL — every schema vendored under coordinator_core/
                         frontmatter/schemas/ still matches DoE HEAD (DEGRADED on drift,
                         DEGRADED-as-INDETERMINATE when the DoE clone is unreadable,
                         SKIP when no DoE clone exists on this machine).
  claude-klabauter.root.pointer    OPTIONAL — claude-klabauter-root pointer file present at
                         <settings-home>/machine-local/.claude-klabauter-root and matches the resolved
                         CLAUDE_KLABAUTER_ROOT (DEGRADED, not hard FAIL, on absence/mismatch — without it,
                         per-invoke resolution falls back to a bash subprocess with a 5 s
                         timeout on Windows).
  claude-klabauter.invoke.latency  OPTIONAL — measures a single coordinator_core.invoke round-trip
                         against a 2000 ms per-invoke budget (hooks share a ~3-5 s total
                         budget across multiple invokes); DEGRADED (not BROKEN) over budget
                         or on timeout.
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
  - Does NOT hardcode the claude-klabauter root path — resolves via CLAUDE_KLABAUTER_ROOT ladder.
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
import time
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
    "Install or select Python 3.11+ (coordinator:install provisions a supported "
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
# CLAUDE_KLABAUTER_ROOT resolution ladder
#
# Mirrors coordinator-claude-klabauter-root.sh and coordinator-core-shim._resolve_claude_klabauter_root():
#   Rung 1 — CLAUDE_KLABAUTER_ROOT env var (caller already exported it)
#   Rung 2 — machine-local get repos.claude_klabauter (authoritative coordinator-side key)
#   Rung 3 — git-root auto-discovery from this script's bin/ location
# ---------------------------------------------------------------------------


def _resolve_claude_klabauter_root() -> tuple[Path | None, str]:
    """Return (resolved_path, source_description) or (None, error_message).

    Tries three rungs in order; the first hit wins.
    """
    # Rung 1 — env var (idempotency gate: already exported by a parent shell)
    val = os.environ.get("CLAUDE_KLABAUTER_ROOT", "").strip()
    if val:
        return Path(val), "env CLAUDE_KLABAUTER_ROOT"

    # Rung 2 — machine-local registry (authoritative coordinator-side path).
    # Read in-process via coordinator_core.machine_resolver.registry_get (a
    # direct registry.local.toml/registry.toml tomllib read) rather than
    # shelling out to `machine-local get` -- this script lives inside the
    # very repo it is trying to locate (<claude-klabauter-root>/bin/
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
    # This script lives at <claude-klabauter-root>/bin/claude-klabauter-doctor-probe.py, so its
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
        "Cannot resolve CLAUDE_KLABAUTER_ROOT via (1) CLAUDE_KLABAUTER_ROOT env var, "
        "(2) machine-local get repos.claude_klabauter, "
        "(3) git-root auto-discovery from bin/ parent. "
        "Remediation: run scripts/setup.py to register repos.claude_klabauter, "
        "or: export CLAUDE_KLABAUTER_ROOT=/path/to/claude-klabauter"
    )


# ---------------------------------------------------------------------------
# Probe 1: CLAUDE_KLABAUTER_ROOT resolves
# ---------------------------------------------------------------------------


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
                probe="claude-klabauter.root.resolve",
                status=_BROKEN,
                detail=source,
                remediation=(
                    "Run scripts/setup.py to register repos.claude_klabauter, "
                    "or set CLAUDE_KLABAUTER_ROOT=/path/to/claude-klabauter in the environment."
                ),
            ), None

        if not root.exists():
            return _ProbeResult(
                probe="claude-klabauter.root.resolve",
                status=_BROKEN,
                detail=(
                    f"CLAUDE_KLABAUTER_ROOT resolved to {str(root)!r} (via {source}) "
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
                probe="claude-klabauter.root.resolve",
                status=_BROKEN,
                detail=(
                    f"CLAUDE_KLABAUTER_ROOT={str(root)!r} (via {source}) but coordinator_core/ "
                    "directory absent. Is this the correct claude-klabauter repo root?"
                ),
                remediation=(
                    "Verify the path is the claude-klabauter repo root (must contain coordinator_core/). "
                    "Update: machine-local set repos.claude_klabauter /correct/path"
                ),
            ), None

        return _ProbeResult(
            probe="claude-klabauter.root.resolve",
            status=_PASS,
            detail=(
                f"CLAUDE_KLABAUTER_ROOT={str(root)!r} (resolved via {source}); "
                "coordinator_core/ directory present."
            ),
            remediation="—",
            data={"claude_klabauter_root": str(root), "source": source},
        ), root
    except Exception as exc:
        return _ProbeResult(
            probe="claude-klabauter.root.resolve",
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
            probe="claude-klabauter.registry.key",
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
            probe="claude-klabauter.registry.key",
            status=_BROKEN,
            detail=f"Failed to read repos.claude_klabauter from the registry: {exc}",
            remediation="Verify machine-local is installed and the registry is readable.",
        )

    # machine-local is present — evaluate the registered value.
    if val:
        return _ProbeResult(
            probe="claude-klabauter.registry.key",
            status=_PASS,
            detail=f"repos.claude_klabauter → {val!r}",
            remediation="—",
            data={"registered_path": val},
        )

    return _ProbeResult(
        probe="claude-klabauter.registry.key",
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


def _run_probe_core_import(claude_klabauter_root: Path) -> _ProbeResult:
    """Probe claude-klabauter.core.import — REQUIRED.

    Adds claude_klabauter_root to sys.path and attempts to import coordinator_core +
    coordinator_core.lifecycle + coordinator_core.doctor_envelope.
    """
    root_str = str(claude_klabauter_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        import coordinator_core          # noqa: F401
        import coordinator_core.lifecycle  # noqa: F401
        import coordinator_core.doctor_envelope  # noqa: F401
        return _ProbeResult(
            probe="claude-klabauter.core.import",
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
            probe="claude-klabauter.core.import",
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
            probe="claude-klabauter.core.import",
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
    existing `_log_dialect_parser_unavailable` observability record. This
    probe reads that same record's path (`dialect_parser_unavailable_log_
    path()`) for its remediation, rather than inventing a second one.

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
            detail="Probe skipped — CLAUDE_KLABAUTER_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve CLAUDE_KLABAUTER_ROOT first (probe 1 remediation).",
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

    data = {"checked": checked, "durable_record": str(dialect_parser_unavailable_log_path())}

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
            detail="Probe skipped — CLAUDE_KLABAUTER_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve CLAUDE_KLABAUTER_ROOT first (probe 1 remediation).",
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
            detail=f"Probe skipped — coordinator_core.install.settings_home_report not importable: {exc}",
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
        "forwarder_derivation_error": report.forwarder_derivation_error,
    }

    if report.complete:
        return _ProbeResult(
            probe=_SETTINGS_HOME_COMPLETE_PROBE,
            status=_PASS,
            detail=f"Settings home complete at {settings_home_path} "
                   f"({report.forwarder_present}/{report.forwarder_expected} bin/ forwarders present).",
            remediation="—",
            required=False,
            data=data,
        )

    problems: list[str] = [m.label for m in report.fixed_missing]
    if report.forwarder_derivation_error is not None:
        problems.append(f"bin/ forwarder set undeterminable: {report.forwarder_derivation_error}")
    elif report.forwarder_missing:
        problems.append(
            f"{len(report.forwarder_missing)}/{report.forwarder_expected} bin/ forwarders missing "
            f"(e.g. {', '.join(report.forwarder_missing[:5])})"
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
    forwarders (a later install step than CLAUDE_KLABAUTER_ROOT resolution) have not
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
            detail="Probe skipped — CLAUDE_KLABAUTER_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve CLAUDE_KLABAUTER_ROOT first (probe 1 remediation).",
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
                probe="claude-klabauter.resident.debris",
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
            probe="claude-klabauter.resident.debris",
            status=_PASS,
            detail=(
                "No stale resident debris from the retired coordinator_core daemon found."
            ),
            remediation="—",
            data={"debris_paths": []},
        )
    except Exception as exc:
        return _ProbeResult(
            probe="claude-klabauter.resident.debris",
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
                probe="claude-klabauter.worktree.bloat",
                status=_PASS,
                detail=(
                    "Worktree bloat scan skipped — CLAUDE_KLABAUTER_ROOT unresolved "
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
            listing = "; ".join(
                f"{f['path']} ({_format_bytes_human(f['size_bytes'])})"
                for f in large_files
            )
            return _ProbeResult(
                probe="claude-klabauter.worktree.bloat",
                status=_INFO,
                detail=(
                    f"{len(large_files)} file(s) exceed the {threshold_human} worktree "
                    f"bloat threshold: {listing}. These are likely runaway/junk files "
                    "(e.g. from a mis-quoted shell redirect) worth inspecting."
                ),
                remediation=(
                    "Inspect each flagged file and `rm` it if confirmed junk: "
                    + "; ".join(f"inspect {f['path']!r}" for f in large_files)
                ),
                data={"large_files": large_files, "threshold_bytes": threshold_bytes},
            )

        return _ProbeResult(
            probe="claude-klabauter.worktree.bloat",
            status=_PASS,
            detail=f"No worktree files exceed the {threshold_human} bloat threshold.",
            remediation="—",
            data={"large_files": [], "threshold_bytes": threshold_bytes},
        )
    except Exception as exc:
        return _ProbeResult(
            probe="claude-klabauter.worktree.bloat",
            status=_BROKEN,
            detail=(
                f"Unexpected error in worktree bloat probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
        )


# ---------------------------------------------------------------------------
# Probe 6: version sanity
# ---------------------------------------------------------------------------


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
        # sys.path already set by _run_probe_core_import (runs before this probe).

        # Check 1: import coordinator_core.
        try:
            import coordinator_core  # noqa: F401
        except ImportError as exc:
            return _ProbeResult(
                probe="claude-klabauter.version.sanity",
                status=_BROKEN,
                detail=f"import coordinator_core failed: {exc}",
                remediation=(
                    "Verify coordinator_core/ is present in CLAUDE_KLABAUTER_ROOT and contains "
                    "__init__.py. See also claude-klabauter.core.import probe."
                ),
            )

        # Check 2: _compute_core_version() resolves.
        try:
            from coordinator_core.lifecycle import _compute_core_version  # Private: intentional — spec §C1b requires this specific version helper; update alongside lifecycle.py if it is renamed.
            version_hash = _compute_core_version()
        except (ImportError, Exception) as exc:
            return _ProbeResult(
                probe="claude-klabauter.version.sanity",
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
                probe="claude-klabauter.version.sanity",
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
            probe="claude-klabauter.version.sanity",
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
            probe="claude-klabauter.version.sanity",
            status=_BROKEN,
            detail=(
                f"Unexpected error in version sanity probe: {type(exc).__name__}: {exc}"
            ),
            remediation="Re-run the probe after investigating the error.",
        )


# ---------------------------------------------------------------------------
# Probe 7: invoke dispatch smoke (OPTIONAL)
# ---------------------------------------------------------------------------


def _run_probe_invoke_smoke(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.invoke.smoke — OPTIONAL (required=False).

    Runs the cheapest registered COMPUTE_ONLY op via the spawn-per-call
    command-type entrypoint:
        python3 -m coordinator_core.invoke ping '{}'

    PASS when the subprocess returns a well-formed result envelope
    (JSON dict with ok=True).  On spawn failure (interpreter/module absent),
    emits a SKIP result — never a bare crash.

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

    Spec backlink: pln-rebuild-claude-klabauter-doctor-as-a-pro-f6bd22 § C1b
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe="claude-klabauter.invoke.smoke",
                status=_INFO,
                detail="Cannot run invoke smoke — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        try:
            result = subprocess.run(
                [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(claude_klabauter_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            return _ProbeResult(
                probe="claude-klabauter.invoke.smoke",
                status=_INFO,
                detail="Python interpreter not found on PATH; invoke smoke skipped.",
                remediation="Ensure python3 is on PATH.",
                required=False,
                skipped=True,
            )
        except subprocess.TimeoutExpired:
            return _ProbeResult(
                probe="claude-klabauter.invoke.smoke",
                status=_BROKEN,
                detail=(
                    "python3 -m coordinator_core.invoke ping '{}' timed out after 30 s. "
                    "Spawn-per-call dispatch should complete in well under 1 s."
                ),
                remediation=(
                    "Run manually from CLAUDE_KLABAUTER_ROOT: "
                    "python3 -m coordinator_core.invoke ping '{}'. "
                    "Investigate hangs or missing dependencies."
                ),
                required=False,
            )

        if result.returncode != 0:
            return _ProbeResult(
                probe="claude-klabauter.invoke.smoke",
                status=_BROKEN,
                detail=(
                    f"coordinator_core.invoke ping '{{}}' exited {result.returncode}. "
                    f"stderr: {result.stderr.strip()!r}"
                ),
                remediation=(
                    "Run manually from CLAUDE_KLABAUTER_ROOT: "
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
                probe="claude-klabauter.invoke.smoke",
                status=_BROKEN,
                detail=(
                    f"coordinator_core.invoke ping returned non-JSON output: {stdout!r}"
                ),
                remediation=(
                    "Run manually: python3 -m coordinator_core.invoke ping '{}'. "
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
                probe="claude-klabauter.invoke.smoke",
                status=_BROKEN,
                detail=(
                    "invoke ping returned a malformed result envelope "
                    f"(expected result.ok=true): {str(envelope)[:200]!r}"
                ),
                remediation=(
                    "Invoke the coordinator_core.invoke ping op manually and verify it "
                    "emits a JSON-RPC envelope with result.ok=true. "
                    "Check coordinator_core/ops/ping.py is intact."
                ),
                required=False,
            )

        return _ProbeResult(
            probe="claude-klabauter.invoke.smoke",
            status=_PASS,
            detail=(
                "spawn-per-call dispatch smoke PASS: the coordinator_core.invoke ping op "
                f"returned a well-formed result envelope (ok=true, ts={payload.get('ts')!r}). "
                "GREEN proves the entrypoint CAN dispatch (cheapest registered "
                "COMPUTE_ONLY op: ping) — NOT that a live session IS connected. "
                "Post-DR-215: no resident process; session-binding is not a concept here."
            ),
            remediation="—",
            required=False,
            data={"ok": payload.get("ok"), "ts": payload.get("ts")},
        )
    except Exception as exc:
        # Probe-authoring invariant: optional probe unexpected failure → SKIP envelope.
        return _ProbeResult(
            probe="claude-klabauter.invoke.smoke",
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
                probe="claude-klabauter.strategic.draft_staleness",
                status=_INFO,
                detail="Cannot check draft staleness — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        draft_path = claude_klabauter_root / "state" / "strategic" / "self-description.draft.yaml"

        if not draft_path.exists():
            return _ProbeResult(
                probe="claude-klabauter.strategic.draft_staleness",
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
                probe="claude-klabauter.strategic.draft_staleness",
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
                probe="claude-klabauter.strategic.draft_staleness",
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
                probe="claude-klabauter.strategic.draft_staleness",
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
            probe="claude-klabauter.strategic.draft_staleness",
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
            probe="claude-klabauter.strategic.draft_staleness",
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
                detail="Cannot check vendored-schema drift — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
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
                detail="Cannot check generator/output staleness — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
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
            names = ", ".join(sorted(str(k) for k in stale))
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_DEGRADED,
                detail=f"Generator/output pair(s) STALE: {names}",
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
            names = ", ".join(sorted(str(k) for k in gap))
            return _ProbeResult(
                probe=_GENERATOR_STALENESS_PROBE,
                status=_INFO,
                detail=f"Generator/output staleness verdict incomplete for: {names}",
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
                f"{len(data)} generator/output pair(s) checked; none STALE."
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
                detail="Cannot recheck cross-repo commitments — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
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
            named = ", ".join(str(a.get("entry")) for a in actionable)
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
    Deregistration is ruled safe ONLY because the miss rate is currently 0% (10/10
    live + 354/354 archived sessions since 2026-08-10); the newest known miss is
    2026-08-08 at 64%. That 0% was, before this probe, completely unwatched.

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
                detail="Cannot check stable_pid capture — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
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
            return _ProbeResult(
                probe=_STABLE_PID_MISS_PROBE,
                status=_DEGRADED,
                detail=summary,
                remediation=(
                    "One or more sessions are missing stable_pid capture — the F0 hazard "
                    "K-006 ruled safe-to-deregister-heartbeat-because-0%% is live again. "
                    "Investigate the session-init capture path "
                    "(coordinator_core/session/core.py::init) for the named session(s) "
                    "before assuming this is transient."
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
# Probe 9: claude-klabauter-root pointer presence (Windows-portability, DEC-2)
# ---------------------------------------------------------------------------


def _resolve_settings_home() -> Path:
    """Resolve the coordinator settings-home root via env/home precedence.

    Mirrors the ladder used throughout the coordinator tri-plane
    (coordinator_core._settings_home.settings_home() and _resolve_machine_local()
    above): COORDINATOR_SETTINGS_HOME, falling back to CLAUDE_HOME, falling back
    to HOME, falling back to the platform home directory (expanduser —
    USERPROFILE on Windows, the passwd entry on POSIX), with
    `.coordinator-claude-settings` appended to the fall-back rungs. Reimplemented locally (rather than importing
    coordinator_core) so this probe works even when CLAUDE_KLABAUTER_ROOT/coordinator_core is unresolved —
    the pointer-presence check must be runnable independent of probe 1's outcome.
    """
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    home = os.environ.get("CLAUDE_HOME") or os.environ.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".coordinator-claude-settings"


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
    probe_id = "claude-klabauter.registry.engine_target_rollout"
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


def _run_probe_root_pointer(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.root.pointer — REQUIRED=False (WARN, not hard FAIL) on absence.

    Checks that the claude-klabauter-root pointer file exists at
    <settings-home>/machine-local/.claude-klabauter-root and that its content matches the
    resolved CLAUDE_KLABAUTER_ROOT.

    Rationale (DEC-2, F17): without the pointer, CLAUDE_KLABAUTER_ROOT resolution falls back to a
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
        resolves CLAUDE_KLABAUTER_ROOT, just slowly).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14
    """
    try:
        settings_home = _resolve_settings_home()
        pointer_path = settings_home / "machine-local" / ".claude-klabauter-root"

        if not pointer_path.exists():
            return _ProbeResult(
                probe="claude-klabauter.root.pointer",
                status=_DEGRADED,
                detail=(
                    f"claude-klabauter-root pointer absent at {str(pointer_path)!r}. Without it, "
                    "per-invoke CLAUDE_KLABAUTER_ROOT resolution falls back to a bash subprocess "
                    "with a 5 s timeout — this is the dominant latency cost on Windows "
                    "per-invoke/hook round-trips."
                ),
                remediation=(
                    "Run the install-time pointer writer (gen-claude-klabauter-root-pointer.py) to "
                    f"populate {str(pointer_path)!r} with the resolved CLAUDE_KLABAUTER_ROOT path."
                ),
                required=False,
                data={"pointer_path": str(pointer_path), "present": False},
            )

        try:
            pointer_content = pointer_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            return _ProbeResult(
                probe="claude-klabauter.root.pointer",
                status=_DEGRADED,
                detail=f"claude-klabauter-root pointer present but unreadable: {exc}",
                remediation=(
                    "Check permissions on the pointer file, or re-run the install-time "
                    f"pointer writer (gen-claude-klabauter-root-pointer.py) to regenerate {str(pointer_path)!r}."
                ),
                required=False,
                data={"pointer_path": str(pointer_path), "present": True},
            )

        if claude_klabauter_root is None:
            return _ProbeResult(
                probe="claude-klabauter.root.pointer",
                status=_PASS,
                detail=(
                    f"claude-klabauter-root pointer present at {str(pointer_path)!r} "
                    f"(content: {pointer_content!r}); content-match skipped — "
                    "CLAUDE_KLABAUTER_ROOT unresolved (see claude-klabauter.root.resolve)."
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
                    probe="claude-klabauter.root.pointer",
                    status=_DEGRADED,
                    detail=(
                        f"claude-klabauter-root pointer content {pointer_content!r} does not match "
                        f"resolved CLAUDE_KLABAUTER_ROOT {resolved_str!r} — stale pointer."
                    ),
                    remediation=(
                        "Re-run the install-time pointer writer (gen-claude-klabauter-root-pointer.py) "
                        f"to refresh {str(pointer_path)!r} with the current CLAUDE_KLABAUTER_ROOT."
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
            probe="claude-klabauter.root.pointer",
            status=_PASS,
            detail=(
                f"claude-klabauter-root pointer present at {str(pointer_path)!r} and matches "
                f"resolved CLAUDE_KLABAUTER_ROOT {resolved_str!r}."
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
            probe="claude-klabauter.root.pointer",
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
def _run_probe_publish_provenance(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.publish.provenance — REQUIRED=False (WARN, not hard FAIL).

    Reads the C1 record (`<settings-home>/machine-local/publish-provenance.json`)
    for a row whose recorded toplevel matches this claude-klabauter checkout, and
    compares its pinned sha against claude-klabauter's own current HEAD via a single
    `git rev-list --count <sha>..HEAD` (Anti-scope 5 — one git call for the
    whole answer, never per row; the amplification gate at
    coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py governs).

    Verdict shape:
      - Record absent, unreadable, or naming no row for this claude-klabauter toplevel
        -> DEGRADED, "unknown" (AC5 — never reported as current; the whole
        point of this plan is to stop asserting freshness that cannot be
        established).
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
    probe_id = "claude-klabauter.publish.provenance"
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe=probe_id,
                status=_INFO,
                detail="Cannot check publish provenance — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        settings_home = _resolve_settings_home()
        record_path = settings_home / "machine-local" / "publish-provenance.json"

        if not record_path.exists():
            return _ProbeResult(
                probe=probe_id,
                status=_DEGRADED,
                detail=(
                    f"publish provenance unknown — no record at {str(record_path)!r}. "
                    "Either no percolate round has completed on this machine yet, or "
                    "C1's write did not land — cannot tell which build is dispatched."
                ),
                remediation="Run a percolate round; publish.py writes this record at round end.",
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
                remediation="Verify CLAUDE_KLABAUTER_ROOT points at a git work tree.",
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
                status=_DEGRADED,
                detail=(
                    f"publish provenance unknown — no published row in {str(record_path)!r} "
                    f"names this claude-klabauter checkout ({claude_klabauter_toplevel!r})."
                ),
                remediation="Run a percolate round that publishes from this checkout.",
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
            remediation="Run a percolate round to publish claude-klabauter's current HEAD.",
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


# Hooks share a ~3-5 s total end-to-end budget across potentially multiple invokes
# (UserPromptSubmit / PreToolUse fan-out). A single invoke must stay well under that
# shared budget, hence 2000 ms (2 s) rather than the full 3-5 s window.
_INVOKE_LATENCY_BUDGET_MS = 2000


def _run_probe_invoke_latency(claude_klabauter_root: Path | None) -> _ProbeResult:
    """Probe claude-klabauter.invoke.latency — OPTIONAL (required=False); WARN over budget.

    Measures a single cold-ish `python -m coordinator_core.invoke ping '{}'` round-trip
    and compares the elapsed wall-clock time against _INVOKE_LATENCY_BUDGET_MS (2000 ms).

    Rationale (F17): hooks have a ~3-5 s total budget shared across multiple invokes on
    a single hook firing; a single invoke exceeding ~2 s risks blowing that shared budget
    on its own, before accounting for fan-out. This is the same failure mode as the
    per-invoke bash-fallback hang (claude-klabauter.root.pointer) surfaced as a latency measurement
    rather than a static presence check.

    Bounded-measurement invariant: the subprocess call is timeout-guarded (5 s) so this
    probe itself can never hang the doctor — a timeout IS the failure being detected and
    is reported as the over-budget (DEGRADED) case, not re-raised.

    Negative-spec:
      - Does NOT retry or average multiple invocations — a single measurement, kept
        cheap and bounded per the spec.
      - Does NOT emit BROKEN on over-budget or timeout — this is a WARN-class latency
        advisory (required=False, DEGRADED), not a correctness failure; the invoke
        still dispatched (or the timeout itself proves the latency problem).

    Probe-authoring invariant: wraps all logic so unexpected exceptions become
    a BROKEN verdict, never an unhandled crash.

    Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C14
    """
    try:
        if claude_klabauter_root is None:
            return _ProbeResult(
                probe="claude-klabauter.invoke.latency",
                status=_INFO,
                detail="Cannot measure invoke latency — CLAUDE_KLABAUTER_ROOT unresolved; skipping.",
                remediation="Resolve CLAUDE_KLABAUTER_ROOT first (see claude-klabauter.root.resolve probe).",
                required=False,
                skipped=True,
            )

        # Bounded timeout (5 s) so a hang IS the failure being measured, never a hang
        # of this probe itself.
        _TIMEOUT_SECONDS = 5
        start = time.perf_counter()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "coordinator_core.invoke", "ping", "{}"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                cwd=str(claude_klabauter_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            return _ProbeResult(
                probe="claude-klabauter.invoke.latency",
                status=_INFO,
                detail="Python interpreter not found on PATH; invoke latency probe skipped.",
                remediation="Ensure python3 is on PATH.",
                required=False,
                skipped=True,
            )
        except subprocess.TimeoutExpired:
            elapsed_ms = _TIMEOUT_SECONDS * 1000
            return _ProbeResult(
                probe="claude-klabauter.invoke.latency",
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip timed out after {_TIMEOUT_SECONDS * 1000} ms — "
                    f"exceeds the {_INVOKE_LATENCY_BUDGET_MS} ms budget. A timeout on this "
                    "bounded measurement IS the failure being detected (the invoke path is "
                    "hanging, e.g. a bash-fallback subprocess with its own 5 s timeout)."
                ),
                remediation=(
                    "Ensure the claude-klabauter-root pointer is present (see claude-klabauter.root.pointer "
                    "probe) so per-invoke resolution avoids the bash-fallback subprocess. "
                    "See the Windows-portability workstream: "
                    "docs/plans/2026-07-14-claude-klabauter-windows-portability.md"
                ),
                required=False,
                data={"elapsed_ms": elapsed_ms, "budget_ms": _INVOKE_LATENCY_BUDGET_MS, "timed_out": True},
            )
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if result.returncode != 0:
            return _ProbeResult(
                probe="claude-klabauter.invoke.latency",
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip completed in {elapsed_ms:.0f} ms but exited "
                    f"{result.returncode} (stderr: {result.stderr.strip()!r}); latency "
                    "measured but dispatch itself failed — see claude-klabauter.invoke.smoke."
                ),
                remediation=(
                    "Run manually: python3 -m coordinator_core.invoke ping '{}'. "
                    "Verify claude-klabauter.invoke.smoke passes first."
                ),
                required=False,
                data={"elapsed_ms": elapsed_ms, "budget_ms": _INVOKE_LATENCY_BUDGET_MS, "timed_out": False},
            )

        if elapsed_ms > _INVOKE_LATENCY_BUDGET_MS:
            return _ProbeResult(
                probe="claude-klabauter.invoke.latency",
                status=_DEGRADED,
                detail=(
                    f"invoke round-trip took {elapsed_ms:.0f} ms — exceeds the "
                    f"{_INVOKE_LATENCY_BUDGET_MS} ms per-invoke budget (hooks share a "
                    "~3-5 s total budget across multiple invokes; a single invoke this "
                    "slow risks blowing the shared budget)."
                ),
                remediation=(
                    "Ensure the claude-klabauter-root pointer is present (see claude-klabauter.root.pointer "
                    "probe) so per-invoke resolution avoids the bash-fallback subprocess. "
                    "See the Windows-portability workstream: "
                    "docs/plans/2026-07-14-claude-klabauter-windows-portability.md"
                ),
                required=False,
                data={"elapsed_ms": elapsed_ms, "budget_ms": _INVOKE_LATENCY_BUDGET_MS, "timed_out": False},
            )

        return _ProbeResult(
            probe="claude-klabauter.invoke.latency",
            status=_PASS,
            detail=(
                f"invoke round-trip took {elapsed_ms:.0f} ms — within the "
                f"{_INVOKE_LATENCY_BUDGET_MS} ms per-invoke budget."
            ),
            remediation="—",
            required=False,
            data={"elapsed_ms": elapsed_ms, "budget_ms": _INVOKE_LATENCY_BUDGET_MS, "timed_out": False},
        )
    except Exception as exc:
        return _ProbeResult(
            probe="claude-klabauter.invoke.latency",
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
                    "(pip install -e . from CLAUDE_KLABAUTER_ROOT), then re-run the doctor probe."
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
            pids_str = ", ".join(str(p) for p in orphaned_pids)
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
            "verify doctor-probes.toml exists at bin/doctor-probes.toml in CLAUDE_KLABAUTER_ROOT."
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
) -> list[_ProbeResult]:
    """Filter the probe results list per the selector flags in args.

    Purpose: Post-run filter so run_probes() always exercises the full probe set and
    the selector only shapes what is emitted.  This preserves existing probe diagnostics
    even when a subset is requested.

    For unimplemented probe IDs (in manifest but not in results), a single _ProbeResult
    with status INFO is synthesised so the selector never returns an empty array.

    Invariant: a valid manifest selector NEVER crashes and NEVER returns an empty list.
    """
    implemented_ids = {r.probe for r in results}

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
                filtered.append(_ProbeResult(
                    probe=pid,
                    status=_INFO,
                    detail=_INFO_STUB_DETAIL,
                    remediation="—",
                    required=meta.get("required", True),
                    skipped=False,
                ))
        return filtered

    if args.cluster:
        cluster_name = args.cluster
        filtered = [r for r in results if manifest.get(r.probe, {}).get("cluster") == cluster_name]
        # Synthesise INFO stubs for unimplemented probes declared in this cluster.
        for pid, meta in manifest.items():
            if meta.get("cluster") == cluster_name and pid not in implemented_ids:
                filtered.append(_ProbeResult(
                    probe=pid,
                    status=_INFO,
                    detail=_INFO_STUB_DETAIL,
                    remediation="—",
                    required=meta.get("required", True),
                    skipped=False,
                ))
        return filtered

    if args.probe:
        probe_id = args.probe
        if probe_id in implemented_ids:
            return [r for r in results if r.probe == probe_id]
        # Unimplemented but valid manifest id (validated in main()).
        meta = manifest[probe_id]
        return [_ProbeResult(
            probe=probe_id,
            status=_INFO,
            detail=_INFO_STUB_DETAIL,
            remediation="—",
            required=meta.get("required", True),
            skipped=False,
        )]

    # No selector — return all results unchanged.
    return results


# ---------------------------------------------------------------------------
# Probe orchestration
# ---------------------------------------------------------------------------


def run_probes() -> tuple[list[_ProbeResult], Path | None]:
    """Run the full static probe suite (seven probes) in dependency order.

    Returns (results, claude_klabauter_root_or_None).
    The returned claude_klabauter_root is the resolved path when probe 1 succeeds; None otherwise.

    DR-215: all live-probe machinery (UDS ping, shim handshake, shim harness-env) was
    retired — coordinator_core is a command-type engine with no resident process to probe.
    Retired under docs/plans/2026-07-06-claude-klabauter-doctor-prose-based-command-type.md § C1a.
    """
    results: list[_ProbeResult] = []

    # Probe 1: CLAUDE_KLABAUTER_ROOT resolves (REQUIRED)
    probe1, claude_klabauter_root = _run_probe_claude_klabauter_root()
    results.append(probe1)

    # Probe 2: registry key (REQUIRED when machine-local present; OPTIONAL otherwise)
    # Runs independently of probe 1 — it checks the machine-local registration path
    # directly, which may differ from the resolution path used in probe 1.
    results.append(_run_probe_registry_key())

    # Probe 3: import coordinator_core (REQUIRED; depends on probe 1)
    if claude_klabauter_root is not None:
        probe3 = _run_probe_core_import(claude_klabauter_root)
    else:
        probe3 = _ProbeResult(
            probe="claude-klabauter.core.import",
            # status is ignored when skipped=True (overridden to DEGRADED by the envelope
            # builder); _INFO is the least-misleading placeholder.
            status=_INFO,
            detail="Probe skipped — CLAUDE_KLABAUTER_ROOT unresolved (see claude-klabauter.root.resolve).",
            remediation="Resolve CLAUDE_KLABAUTER_ROOT first (probe 1 remediation).",
            skipped=True,
        )
    results.append(probe3)

    # Command-type static checks (DR-215 rebuild § C1b). Each accepts claude_klabauter_root
    # (Path | None) and self-handles the unresolved case. Manifest triage flags +
    # _apply_selector govern which appear in --triage / --cluster; run_probes runs all.
    results.append(_run_probe_dialect_guard_armed(claude_klabauter_root))
    results.append(_run_probe_settings_home_complete(claude_klabauter_root))
    results.append(_run_probe_entrypoints_path_resolved(claude_klabauter_root))
    results.append(_run_probe_resident_debris(claude_klabauter_root))
    results.append(_run_probe_worktree_bloat(claude_klabauter_root))
    results.append(_run_probe_version_sanity(claude_klabauter_root))
    results.append(_run_probe_invoke_smoke(claude_klabauter_root))
    results.append(_run_probe_strategic_draft_staleness(claude_klabauter_root))
    results.append(_run_probe_vendored_schema_drift(claude_klabauter_root))
    results.append(_run_probe_generator_output_staleness(claude_klabauter_root))
    results.append(_run_probe_commitments_recheck(claude_klabauter_root))
    results.append(_run_probe_stable_pid_miss(claude_klabauter_root))
    results.append(_run_probe_root_pointer(claude_klabauter_root))
    results.append(_run_probe_publish_provenance(claude_klabauter_root))
    results.append(_run_probe_engine_target_rollout())
    results.append(_run_probe_invoke_latency(claude_klabauter_root))
    results.append(_run_probe_orphaned_execnet_gateways())
    results.append(_run_probe_launch_chain())

    return results, claude_klabauter_root


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
        row["cluster"] = manifest_safe.get(pid, {}).get("cluster")

    return envelope


def emit_envelope(
    results: list[_ProbeResult],
    claude_klabauter_root: Path | None,
    manifest: dict[str, Any] | None = None,
) -> int:
    """Emit the full JSON verdict envelope; always exits 0."""
    envelope = _build_enriched_envelope(results, claude_klabauter_root, manifest)
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
        sentinel_path.write_text(json.dumps(sentinel, indent=2) + "\n", encoding="utf-8")
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
    # auto-discovery used when CLAUDE_KLABAUTER_ROOT env / machine-local are absent).
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

    # Run all probes (full set; selector is a post-run filter).
    results, claude_klabauter_root = run_probes()

    # Apply selector filter to the results.
    results = _apply_selector(results, manifest, args)

    if args.step_zero:
        return emit_step_zero(results)

    envelope = _build_enriched_envelope(results, claude_klabauter_root, manifest)

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

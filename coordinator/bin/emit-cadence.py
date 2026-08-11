#!/usr/bin/env python3
# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""emit-cadence.py — native Python trampoline for the emit.cadence transport op.

Purpose: routes emit.cadence — claude-klabauter's composite op (backlog.record THEN
artifact.emit aggregation, in that order, as a claude-klabauter-internal invariant)
through cc_invoke.route_mutation. This is the per-repo cadence TRIGGER fired
from `-complete` ceremonies: example-doctrine-repo owns the cadence (WHEN emission fires),
Claude-klabauter owns the op (backlog.record -> artifact.emit ordering, and everything
the op does internally).

2026-07-19 Windows de-bash campaign, Wave 1b. Replaces the bash strangler
facade with a native Python entry: `python3 emit-cadence.py`, no shell in
the middle, no #!/bin/sh polyglot header. Big-bang cutover — the legacy
bash body is NOT retained; legacy_cadence() below raises instead of
running a fallback bash emitter.

Spec backlink: docs/plans/2026-07-11-emission-cadence-trigger-rewire.md § C2 / D3
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md § Wave 1b

Gate flag (D2, RATIFIED PM 2026-07-11; SUPERSEDED 2026-08-10):
    COORDINATOR_EMISSION_CADENCE_LIVE — D2 set this default ON, disabled
    only by an explicit off value. PM ruling 2026-08-10 supersedes D2's
    default: cadence is now OFF by default, fleet-wide, pending answers
    from cockpit and rag on what purpose emit.cadence was actually
    serving (see the decision record superseding D2 for the evidence —
    24-46x/day fires, ~64% timeout rate, no downstream consumer noticing).
    Only an explicit ON value (1, true, on — case-insensitive) enables
    cadence emission; unset or any other value leaves it OFF. When OFF,
    this script logs once to stderr and exits 0 (benign skip — never
    wedges/errors the calling ceremony). D2's history is retained above,
    not deleted — its default-ON is superseded, not forgotten.

Exit codes:
    0 — success, OR gate-off benign skip
    1 — seam absent (no claude-klabauter control plane) OR op-level refusal
        (RouteMutationError) — no emission fired
    3 — native op: post-spawn transport failure
    4 — structural contract-pin failure (will not self-heal; remediation
        named in stderr)

Negative-spec (retired patterns — DO NOT reintroduce):
    - No bash fallback emitter body. The pre-campaign strangler facade's
      State-1 legacy_cadence() only ever printed a fail-loud message and
      returned 1 (example-doctrine-repo never carried a real bash emission body for this op);
      this port preserves that fail-loud contract by raising instead.
    - No local --bare/cc_invoke ladder. Imports route_mutation from
      coordinator/bin/lib/cc_invoke.py (Wave 1a) rather than re-inlining one.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent
_LIB_DIR = _BIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from cc_invoke import RouteMutationError, StructuralPinError, route_mutation  # noqa: E402


class _SeamAbsentError(RuntimeError):
    """Raised by legacy_cadence() on State-1 (seam absent) — no bash fallback."""


# --- Self-stamping HALTED marker (2026-08-10, DR-287 self-stamping follow-up) ---
#
# Purpose: when the gate is off, this script skips emission in whichever repo's
# ceremony invoked it — but the frozen `state/cockpit-emission.json` in THAT repo
# carries no in-band signal that it stopped advancing (the in-artifact field is
# blocked behind example-doctrine-repo's `additionalProperties: false` envelope schema gate). This
# writes/removes a repo-local `state/cockpit-emission.HALTED.md` beside the frozen
# artifact so cockpit's fleet-tier glob (`<fleet-parent>/*/state/cockpit-emission.json`)
# has a co-located signal in every repo whose cadence is skipping, without anyone
# remembering to copy a file by hand.
#
# Negative-spec: this never raises past its own boundary and never turns a skip
# (or a live emission) into a ceremony failure — every operation here is
# best-effort, contained to broad except clauses, and callable from both the
# gate-off skip path (write/refresh) and the gate-on path (remove-if-stale).

_HALT_DATE = "2026-08-10"
_HALT_REASON = (
    "PM ruling (DR-287): `emit.cadence` was firing 24-46x/day fleet-wide with "
    "~64% timeout rate and no downstream consumer required per-ceremony freshness."
)
_DR_POINTER = "docs/decisions/DR-287-emit-cadence-halted-pending-consumer-pur.md (claude-klabauter)"
_REENABLE_VAR = "COORDINATOR_EMISSION_CADENCE_LIVE=1"
_EMITTED_AT_RE = re.compile(r'"emitted_at"\s*:\s*"([^"]*)"')
_MARKER_READ_HEAD_BYTES = 4096


def _extract_emitted_at(artifact_path: Path) -> str | None:
    """Cheaply pull `emitted_at` out of a (possibly ~23MB) cockpit-emission.json
    without reading it whole — `emitted_at` sits in the top-level envelope, well
    within the first few KB, so a bounded head-read + regex suffices."""
    try:
        with open(artifact_path, "rb") as f:
            head_bytes = f.read(_MARKER_READ_HEAD_BYTES)
    except OSError:
        return None
    head = head_bytes.decode("utf-8", errors="replace")
    match = _EMITTED_AT_RE.search(head)
    return match.group(1) if match else None


def _build_halted_marker_content(emitted_at: str | None) -> str:
    emitted_line = (
        emitted_at
        if emitted_at
        else "unknown (state/cockpit-emission.json absent or unreadable at write time)"
    )
    return (
        "# state/cockpit-emission.json is HALTED — do not read as current\n\n"
        f"**Halted:** {_HALT_DATE}, {_HALT_REASON}\n\n"
        f"**Re-enable:** set `{_REENABLE_VAR}` in the environment the ceremony "
        "directives run under.\n\n"
        f"**Reference:** {_DR_POINTER}\n\n"
        "**Local artifact `emitted_at` at halt-marker-write time:** "
        f"`{emitted_line}`\n\n"
        "**Scope note:** this marker is repo-local, self-stamped by "
        "`coordinator/bin/emit-cadence.py` on each benign gate-off skip in "
        "*this* repo only — it is written/refreshed here and does NOT travel "
        "with a `git show <sha>:state/cockpit-emission.json` blob (a checked-out "
        "historical commit will not carry this file's sibling marker). This is "
        "exactly the limitation that motivated requesting an in-artifact halt "
        "field, which is blocked behind example-doctrine-repo's `additionalProperties: false` "
        "envelope schema gate; until that lands, this filesystem marker is the "
        "weaker signal.\n"
    )


def _resolve_repo_root_safe() -> str | None:
    """Best-effort twin of `_resolve_repo_root` that never exits the process —
    used only for the courtesy marker sync, which must never turn a skip into
    a failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _sync_halted_marker(repo_root: str) -> None:
    """Ensure `<repo_root>/state/cockpit-emission.HALTED.md` exists and is
    accurate. No-op if `state/` doesn't exist (never creates it), no-op if the
    marker already holds the correct content (no mtime churn on a shared tree),
    and swallows every error — a failure here degrades silently, it never
    propagates."""
    try:
        state_dir = Path(repo_root) / "state"
        if not state_dir.is_dir():
            return
        marker_path = state_dir / "cockpit-emission.HALTED.md"
        artifact_path = state_dir / "cockpit-emission.json"
        emitted_at = _extract_emitted_at(artifact_path) if artifact_path.is_file() else None
        content = _build_halted_marker_content(emitted_at)
        if marker_path.is_file():
            try:
                if marker_path.read_text(encoding="utf-8") == content:
                    return
            except OSError:
                pass
        tmp_path = marker_path.with_name(marker_path.name + f".tmp-{os.getpid()}")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, marker_path)
        finally:
            # os.replace consumed tmp_path on success; a survivor means the
            # swap failed, and an uncleaned one becomes untracked litter in
            # state/ that trips the next session's dirty-tree gate.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        return


def _remove_halted_marker(repo_root: str) -> None:
    """Delete a stale `state/cockpit-emission.HALTED.md` when the gate is ON —
    a marker beside a live artifact is the same lie in the other direction.
    Swallows every error; never propagates."""
    try:
        marker_path = Path(repo_root) / "state" / "cockpit-emission.HALTED.md"
        if marker_path.is_file():
            marker_path.unlink()
    except Exception:
        return


def _gate_is_off() -> bool:
    """True unless COORDINATOR_EMISSION_CADENCE_LIVE is an explicit on value.

    Default OFF per PM ruling 2026-08-10 (supersedes D2's default-ON,
    2026-07-11) — see the module docstring's Gate flag section.
    """
    raw = os.environ.get("COORDINATOR_EMISSION_CADENCE_LIVE", "")
    return raw.strip().lower() not in ("1", "true", "on")


def _resolve_repo_root() -> str:
    """Resolve the git repo root from cwd — mirrors strangle_route's
    `git -C "$PWD" rev-parse --show-toplevel` auto-resolution (standalone-repo
    assumption; no repo-root argument on this entry).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        print(
            f"emit-cadence: cannot resolve git repo root from {Path.cwd()}",
            file=sys.stderr,
        )
        sys.exit(1)
    return proc.stdout.strip()


def legacy_cadence() -> None:
    """State-1 fallback — fails loud. emit.cadence's composite
    backlog.record -> artifact.emit ordering lives entirely in claude-klabauter's
    engine; example-doctrine-repo never carried a bash body for this op. No fallback emission
    is fired when the claude-klabauter control plane is absent on disk.
    """
    raise _SeamAbsentError(
        "emit-cadence: cockpit emission cadence requires the claude-klabauter control "
        "plane, which is not present in this distribution. No emission fired."
    )


def main() -> int:
    if _gate_is_off():
        print(
            "emit-cadence: cadence emission halted per PM ruling "
            "2026-08-10 (supersedes D2's default-ON) — skipping. "
            "Set COORDINATOR_EMISSION_CADENCE_LIVE=1 to re-enable.",
            file=sys.stderr,
        )
        skip_repo_root = _resolve_repo_root_safe()
        if skip_repo_root:
            _sync_halted_marker(skip_repo_root)
        return 0

    repo_root = _resolve_repo_root()

    # Marker removal must happen ONLY after route_mutation returns success —
    # removing it up front (pre-emission) would leave a repo with cadence
    # conceptually live, the marker gone, and no fresh emission if the call
    # below fails; the calling ceremonies are best_effort: True, so that
    # non-zero exit is very likely swallowed and the operator gets zero
    # signal. On any failure path the existing marker (if any) is left
    # untouched — the artifact really is still frozen, so leaving it in
    # place is simplest and correct; no "attempted" rewrite is needed.
    #
    # emit.cadence takes no params. Requires only _origin_worktree, resolved
    # from cwd by the seam itself — zero new dependency vs artifact.emit.
    try:
        route_mutation("emit.cadence", {}, repo_root, legacy_cadence)
    except _SeamAbsentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RouteMutationError as exc:
        print(f"emit-cadence: {exc}", file=sys.stderr)
        return 1
    except StructuralPinError as exc:
        print(f"emit-cadence: {exc}", file=sys.stderr)
        print(
            "emit-cadence: structural contract-pin failure — will NOT "
            "self-heal on retry; apply the remediation named above before "
            "re-running.",
            file=sys.stderr,
        )
        return 4
    except RuntimeError as exc:
        print(f"emit-cadence: {exc}", file=sys.stderr)
        return 3

    # Emission succeeded — the marker (if any) is now genuinely stale;
    # removal itself stays failure-contained (see _remove_halted_marker's
    # own except Exception) so a delete failure can never turn this success
    # into a reported failure.
    _remove_halted_marker(repo_root)

    return 0


if __name__ == "__main__":
    sys.exit(main())

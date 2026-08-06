"""
coordinator_core.ops.doctor — the WS-9 out-of-harness health/repair command.

WHY THIS EXISTS
    Five occurrences of the same incident class in two days (2026-07-28/29): a
    hook registration or a synced config file goes bad, and the tools needed to
    repair it (Write, Edit, Bash) are exactly the tools the break disables. A
    guard that runs through the tool it guards can detect a break but never
    repair it — see `docs/plans/2026-07-29-windows-viability-stop-the-spawn-
    storms.md` row WS-9 and example-doctrine-repo `state/2026-07-29-deleted-hook-scripts-
    bricked-every-write.md`. This module's PRIMARY invocation is a plain
    terminal (`python3 <this-repo>/coordinator/bin/doctor.py`, or the paired
    `doctor.cmd` on Windows) with NO Claude Code process involved, so a session
    with every tool call dead can still be diagnosed and partially repaired.

AUDIENCE
    A nervous operator, often on a machine they have force-shut-down before.
    Output is quiet on a clean layer (`## Layers` below never print a line for
    a layer with nothing to report) and loud, with a plain remediation, on a
    broken one. This is not a wall of diagnostics — see `render_report()`.

LAYERS CHECKED (each is one `Layer` in `run_doctor()`'s return list)
    1. Sibling resolution     — can this machine resolve claude-klabauter's own
       root and example-doctrine-repo's root via the settings-home ladder? (the same
       resolution every coordinator bin/ trampoline depends on.)
    2. Hook registration      — every `coordinator/hooks/hooks.json` command
       (example-doctrine-repo side) resolves to a script that exists on disk, and reports
       whether it routes through the fail-open launcher
       (`coordinator/hooks/fail_open_launcher.py`, example-doctrine-repo 7e5b546a9) or is a bare
       command that would brick every tool call if its target ever goes
       missing. Also inspects a `hooks` block inside `~/.claude/settings.json`
       itself, if one is present — WS-12 has not yet decided whether the
       settings.json-generated copy survives, so this checks both surfaces
       rather than assuming one.
    3. Foreign-platform paths — `~/.claude/settings.json` /
       `settings.local.json` for a path or env-var-reference shaped for the
       OTHER platform (the 2026-07-28 four-brick incident). Delegates to the
       already-built, already-tested
       `coordinator_core.ops.session.guard_foreign_platform_paths` module —
       this file does not re-derive that detector.
    4. Hooks kill-switch      — reports whether
       `~/.claude/.coordinator-hooks-disabled` is armed. Detect-only: WS-12 is
       an open design fork over what this marker should mean going forward
       ("DO NOT RESOLVE THE FORK BY ASSUMPTION") and the marker's own file is
       gitignored per-machine state, not this doctor's to toggle on a guess.

REPAIR POSTURE — what this command fixes vs. only reports, and why
    Exactly one layer is auto-repairable, and only when `--fix` is passed
    (never on a bare read): layer 2's BARE (non-fail-open-wrapped) hook
    command entries in example-doctrine-repo's `coordinator/hooks/hooks.json`. This is
    safe because (a) `hooks.json` is not the bidirectionally-synced file —
    `~/.claude/settings.json` is, and this doctor never writes that one — and
    (b) the wrap is idempotent and already covered by the fail_open_launcher
    module's own tests; `--fix` only ever adds the wrapper, never removes or
    reorders anything. Every other layer is DETECT-ONLY BY CONSTRUCTION:
      - foreign-platform paths and the settings.json hooks block live in a
        file synced across machines via git; a same-host "repair" can
        localize the file to this machine's own form and clobber the other
        machine's next pull. Hand-fix only.
      - the kill-switch marker's correct state is an open design decision
        (WS-12), not a fact this command can derive.
    Absence of a check must not read as the check passing: a layer that could
    not be evaluated (unresolvable sibling root, unreadable hooks.json, etc.)
    reports itself as UNKNOWN, distinct from OK.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-storms.md WS-9 / AC-28
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from coordinator_core.session.declared_writes import declare_write

_HOOK_SEAM_MARKER = "COORDINATOR HOOK SEAM"
_PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}"
_HOOKS_DISABLED_MARKER_NAME = ".coordinator-hooks-disabled"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One concrete, actionable line item within a layer."""

    severity: str  # "broken" | "info" | "repaired"
    message: str


@dataclass
class Layer:
    name: str
    status: str  # "ok" | "broken" | "unknown"
    findings: List[Finding] = field(default_factory=list)


@dataclass
class DoctorReport:
    layers: List[Layer] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(layer.status == "ok" for layer in self.layers)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Mirrors `guard_settings_json_write._config_dir()` — the live
    `~/.claude`-analog directory, CLAUDE_CONFIG_DIR-overridable."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    from coordinator_core._settings_home import home_dir

    return home_dir() / ".claude"


def _extract_script_path(command: str) -> Optional[str]:
    """Return the underlying script path a hooks.json `command` string
    invokes, whether it is fail-open-wrapped (`python3 -c '<bootstrap>'
    <script> [args...]`) or bare (`python3 <script> [args...]`).

    Uses `shlex.split` rather than a regex: the wrapped bootstrap payload is
    guaranteed single-quoted with no embedded single quote (fail_open_launcher
    enforces this at wrap time), so shlex correctly treats it as one token
    regardless of the double quotes it contains, and correctly isolates the
    script-path token that follows it either way.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] not in ("python3", "python"):
        return None
    if len(tokens) >= 2 and tokens[1] == "-c":
        return tokens[3] if len(tokens) > 3 else None
    return tokens[1] if len(tokens) > 1 else None


def _resolve_plugin_root_token(path: str, doe_root: Optional[str]) -> str:
    if _PLUGIN_ROOT_TOKEN in path and doe_root:
        return path.replace(_PLUGIN_ROOT_TOKEN, f"{doe_root}/coordinator")
    return path


def _iter_hook_commands(hooks_doc: Any):
    """Yield every (event, matcher_index, hook_index, command) tuple in a
    hooks.json-shaped or settings.json `hooks`-block-shaped document."""
    hooks_block = hooks_doc.get("hooks") if isinstance(hooks_doc, dict) else None
    if not isinstance(hooks_block, dict):
        return
    for event, matcher_blocks in hooks_block.items():
        if not isinstance(matcher_blocks, list):
            continue
        for m_idx, block in enumerate(matcher_blocks):
            if not isinstance(block, dict):
                continue
            for h_idx, hook in enumerate(block.get("hooks", []) or []):
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if isinstance(command, str) and command:
                    yield event, m_idx, h_idx, command


# ---------------------------------------------------------------------------
# Layer 1 — sibling resolution
# ---------------------------------------------------------------------------


def _check_sibling_resolution() -> Layer:
    findings: List[Finding] = []
    status = "ok"

    from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

    try:
        claude_klabauter_root = coordinator_claude_klabauter_root()
    except RuntimeError as exc:
        status = "broken"
        findings.append(Finding("broken", f"claude-klabauter root did not resolve: {exc}"))
        claude_klabauter_root = None
    else:
        if not os.path.isdir(os.path.join(claude_klabauter_root, "coordinator_core")):
            status = "broken"
            findings.append(
                Finding(
                    "broken",
                    f"resolved claude-klabauter root '{claude_klabauter_root}' has no "
                    "coordinator_core/ — wrong path or partial checkout.",
                )
            )

    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    if not doe_root:
        status = "broken"
        findings.append(
            Finding(
                "broken",
                "example-doctrine-repo root did not resolve — set it via "
                "'machine-local set repos.example_doctrine_repo <path>', or repair the "
                "'.doe-root' pointer under the settings-home machine-local dir.",
            )
        )
    elif not os.path.isfile(os.path.join(doe_root, "coordinator", "hooks", "hooks.json")):
        status = "broken"
        findings.append(
            Finding(
                "broken",
                f"resolved example-doctrine-repo root '{doe_root}' has no "
                "coordinator/hooks/hooks.json — wrong path or partial checkout.",
            )
        )

    return Layer("Sibling repo resolution (claude-klabauter + example-doctrine-repo)", status, findings)


# ---------------------------------------------------------------------------
# Layer 2 — hook registration health
# ---------------------------------------------------------------------------


def _check_one_hooks_doc(
    doc_path: Path, doe_root: Optional[str], label: str
) -> "tuple[str, List[Finding], bool]":
    """Returns (status, findings, present). `present` is False only for the
    "file does not exist at all" case — the expected, silent, clean state for
    `~/.claude/settings.json` right now (plugin-side delivery from hooks.json
    is live; see module docstring). A present-but-unreadable/unparsable file
    is a real finding, not silence."""
    if not doc_path.is_file():
        return "unknown", [], False

    try:
        with doc_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return "unknown", [Finding("broken", f"{label}: unreadable/unparsable: {exc}")], True

    findings: List[Finding] = []
    missing = 0
    bare = 0
    total = 0

    for event, m_idx, h_idx, command in _iter_hook_commands(doc):
        total += 1
        wrapped = _HOOK_SEAM_MARKER in command
        script = _extract_script_path(command)
        if script is None:
            findings.append(
                Finding("broken", f"{label} [{event}/{m_idx}/{h_idx}]: command shape not understood.")
            )
            continue
        resolved = _resolve_plugin_root_token(script, doe_root)
        if not os.path.isfile(resolved):
            missing += 1
            findings.append(
                Finding(
                    "broken",
                    f"{label} [{event}/{m_idx}/{h_idx}]: registered script missing on disk: "
                    f"{resolved}"
                    + ("" if wrapped else " (NOT fail-open-wrapped — this would brick every tool call)"),
                )
            )
        if not wrapped:
            bare += 1

    if total == 0:
        return "ok", [], True

    if bare:
        findings.append(
            Finding(
                "info",
                f"{label}: {bare}/{total} hook command(s) are bare (not routed through "
                "fail_open_launcher) — run with --fix to wrap them.",
            )
        )

    status = "broken" if missing else "ok"
    return status, findings, True


def _check_hook_registration() -> Layer:
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    findings: List[Finding] = []
    statuses: List[str] = []

    if doe_root:
        hooks_json = Path(doe_root) / "coordinator" / "hooks" / "hooks.json"
        status, doc_findings, present = _check_one_hooks_doc(hooks_json, doe_root, "hooks.json")
        if present:
            statuses.append(status)
        findings.extend(doc_findings)
    else:
        statuses.append("unknown")
        findings.append(Finding("broken", "hooks.json: cannot check — example-doctrine-repo root unresolved."))

    settings_path = _config_dir() / "settings.json"
    status, doc_findings, present = _check_one_hooks_doc(
        settings_path, doe_root, "settings.json hooks block"
    )
    # A present-and-empty (no hooks key at all) or genuinely-absent
    # settings.json is the expected clean state right now (plugin-side
    # delivery from hooks.json is live) — deliberately silent, not folded
    # into the overall status. Only a present-but-broken settings.json
    # (unreadable, or entries that resolve to a missing script) surfaces.
    if doc_findings:
        statuses.append(status)
        findings.extend(doc_findings)

    if "broken" in statuses:
        overall = "broken"
    elif all(s == "unknown" for s in statuses):
        overall = "unknown"
    else:
        overall = "ok"

    return Layer("Hook registration (hooks.json + settings.json)", overall, findings)


def _fix_bare_hook_commands(fix_report: List[str]) -> None:
    """--fix action: wrap every bare (unwrapped) command in example-doctrine-repo's
    hooks.json via the real, already-tested fail_open_launcher.wrap_command —
    imported from its actual source location rather than re-derived here, so
    this command shares the exact same wrapping logic the rest of the fleet
    already relies on. Idempotent: is_wrapped() entries are left untouched.
    Does NOT touch ~/.claude/settings.json — that file is bidirectionally
    synced and out of scope for auto-repair (see module docstring)."""
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    if not doe_root:
        fix_report.append("--fix: skipped hooks.json wrap — example-doctrine-repo root unresolved.")
        return

    hooks_json_path = Path(doe_root) / "coordinator" / "hooks" / "hooks.json"
    if not hooks_json_path.is_file():
        fix_report.append(f"--fix: skipped hooks.json wrap — not found at {hooks_json_path}.")
        return

    hooks_lib_dir = str(Path(doe_root) / "coordinator" / "hooks")
    inserted = hooks_lib_dir not in sys.path
    if inserted:
        sys.path.insert(0, hooks_lib_dir)
    try:
        import fail_open_launcher
    except ImportError as exc:
        fix_report.append(f"--fix: skipped hooks.json wrap — fail_open_launcher unimportable: {exc}")
        return
    finally:
        if inserted:
            sys.path.remove(hooks_lib_dir)

    try:
        with hooks_json_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        fix_report.append(f"--fix: skipped hooks.json wrap — unreadable: {exc}")
        return

    wrapped_count = 0
    hooks_block = doc.get("hooks") if isinstance(doc, dict) else None
    if isinstance(hooks_block, dict):
        for matcher_blocks in hooks_block.values():
            if not isinstance(matcher_blocks, list):
                continue
            for block in matcher_blocks:
                if not isinstance(block, dict):
                    continue
                for hook in block.get("hooks", []) or []:
                    if not isinstance(hook, dict):
                        continue
                    command = hook.get("command")
                    if not isinstance(command, str) or not command:
                        continue
                    if fail_open_launcher.is_wrapped(command):
                        continue
                    try:
                        hook["command"] = fail_open_launcher.wrap_command(command)
                        wrapped_count += 1
                    except ValueError:
                        continue

    if wrapped_count == 0:
        fix_report.append("--fix: hooks.json — nothing to wrap (all entries already fail-open).")
        return

    with hooks_json_path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(hooks_json_path)
    fix_report.append(
        f"--fix: wrapped {wrapped_count} bare hook command(s) in {hooks_json_path} "
        "(left in the working tree — this command never commits)."
    )


# ---------------------------------------------------------------------------
# Layer 3 — foreign-platform paths
# ---------------------------------------------------------------------------


def _check_foreign_platform_paths() -> Layer:
    from coordinator_core.ops.session.guard_foreign_platform_paths import (
        evaluate_foreign_platform_paths,
    )

    config_dir = _config_dir()
    findings: List[Finding] = []
    for name in ("settings.json", "settings.local.json"):
        path = config_dir / name
        try:
            banner = evaluate_foreign_platform_paths(path, config_dir=config_dir)
        except Exception as exc:  # defensive: this layer must never crash the run
            findings.append(Finding("broken", f"{name}: foreign-path check raised {exc!r}."))
            continue
        if banner:
            findings.append(Finding("broken", f"{name}: foreign-platform path(s) detected.\n{banner}"))

    status = "broken" if findings else "ok"
    return Layer("Foreign-platform paths in settings.json", status, findings)


# ---------------------------------------------------------------------------
# Layer 4 — hooks kill-switch marker
# ---------------------------------------------------------------------------


def _check_kill_switch_marker() -> Layer:
    marker = _config_dir() / _HOOKS_DISABLED_MARKER_NAME
    if not marker.is_file():
        return Layer("Hooks generation kill-switch", "ok", [])
    findings = [
        Finding(
            "info",
            f"ARMED: {marker} is present — hook-generation regeneration is suppressed "
            "on this machine (detect-only: whether this marker should still exist is an "
            "open design decision, WS-12 — this command does not toggle it).",
        )
    ]
    return Layer("Hooks generation kill-switch", "ok", findings)


# ---------------------------------------------------------------------------
# Orchestration + rendering
# ---------------------------------------------------------------------------


def run_doctor(fix: bool = False) -> tuple[DoctorReport, List[str]]:
    report = DoctorReport()
    report.layers.append(_check_sibling_resolution())
    report.layers.append(_check_hook_registration())
    report.layers.append(_check_foreign_platform_paths())
    report.layers.append(_check_kill_switch_marker())

    fix_report: List[str] = []
    if fix:
        _fix_bare_hook_commands(fix_report)

    return report, fix_report


def render_report(report: DoctorReport, fix_report: List[str]) -> str:
    lines: List[str] = []
    lines.append("coordinator doctor — layer health")
    lines.append("=" * 34)

    any_broken = False
    any_unknown = False
    for layer in report.layers:
        marker = {"ok": "OK", "broken": "BROKEN", "unknown": "UNKNOWN"}[layer.status]
        lines.append(f"[{marker:7}] {layer.name}")
        if layer.status == "broken":
            any_broken = True
        if layer.status == "unknown":
            any_unknown = True
        for finding in layer.findings:
            prefix = {"broken": "  ! ", "info": "  - ", "repaired": "  + "}[finding.severity]
            for sub_line in finding.message.splitlines():
                lines.append(prefix + sub_line)

    if fix_report:
        lines.append("")
        lines.append("Repairs attempted (--fix):")
        for line in fix_report:
            lines.append(f"  + {line}")

    lines.append("")
    if not any_broken and not any_unknown:
        lines.append("All layers healthy. Nothing to fix.")
    elif any_broken:
        lines.append("BROKEN layer(s) found above — hand-fix per the remediation line, "
                      "or re-run with --fix for the one auto-repairable class (bare hook commands).")
    else:
        lines.append("Some layer(s) could not be evaluated (UNKNOWN) — see above; "
                      "absence of a check is not the same as a clean result.")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    fix = "--fix" in argv

    report, fix_report = run_doctor(fix=fix)
    print(render_report(report, fix_report))

    return 1 if not report.all_ok else 0


if __name__ == "__main__":
    sys.exit(main())

"""Verify the two console entrypoints (`coordinator-invoke`,
`coordinator-cockpit-emit-schema`) actually RESOLVE and EXECUTE on PATH
after a clean install.

docs/plans/2026-08-17-machine-first-install-surface.md § C3: the pip
console scripts from C2 land in the interpreter's Scripts/bin dir, which is
deliberately NOT added to PATH (it also carries bare `python3`/`pip3` —
adding it would silently repoint every hook on the box). Instead, C3 emits
`coordinator/bin/coordinator-invoke.py` and
`coordinator/bin/coordinator-cockpit-emit-schema.py` trampolines, picked up
by substrate's existing dynamic agent-helper forwarder derivation
(`_derive_agent_helper_target_map`) and forwarded into the already-PATH'd
settings-home `bin/`. This module is the shared oracle for whether that
chain actually worked, consumed by both entry points the plan names:

  - "the standalone script" — this module's own CLI (`python3 -m
    coordinator_core.install.path_resolution_report`, or a caller importing
    `check_entrypoint_path_resolution` directly).
  - "the chain-walk" — the `claude-klabauter.entrypoints.path_resolved` doctor probe
    (`bin/claude-klabauter-doctor-probe.py`, registered in `bin/doctor-probes.toml`),
    which `/coordinator:setup`'s agentic install chain dispatches. Named
    explicitly in the plan's Out-of-scope section as working AROUND the
    chain-walker's no-recursion gap (`claude_klabauter_seam_resolvable` self-confirms
    and never recurses into claude-klabauter's own manifest) rather than closing it —
    this probe is that workaround's landing spot.

FRESH-PROCESS REQUIREMENT (both platforms, different mechanisms): a
profile-file or registry PATH write affects neither the writer process nor
any already-running shell — only a shell/process STARTED AFTER the write
observes it. This module cannot satisfy that from inside an
already-running process by spawning an ordinary child (a child inherits its
parent's CURRENT environment block, not a live re-read of profile files or
the registry) — POSIX and Windows differ in how a fresh read is obtained:

  - POSIX: spawning the user's LOGIN shell with `-lc` (matching
    `prereq_probe.probe_shell_login_env`'s precedent) makes IT re-source the
    profile files fresh, regardless of what environment this Python process
    itself inherited. That satisfies the freshness requirement without
    needing the whole process tree relaunched.
  - Windows: `HKCU\\Environment` is broadcast via `WM_SETTINGCHANGE`, which
    only already-running top-level windows (e.g. an open Explorer/cmd
    window) observe by re-querying the registry — a plain child process
    spawned from THIS Python process still inherits THIS process's
    environment block verbatim, broadcast or not. There is no
    Windows equivalent of "spawn a login shell that re-sources everything";
    the only correct check is to run this probe from a NEW top-level
    process started after the broadcast (a fresh terminal/session). See
    `check_entrypoint_path_resolution`'s `platform_caveat` field, which
    names this explicitly rather than silently reporting a false pass/fail
    against a stale inherited environment.

MACOS-VERIFIED / WINDOWS-UNVERIFIED: every POSIX code path below was
exercised live on macOS (see this chunk's run-report sidecar). No line of
the Windows branch has been executed anywhere — there is no Windows box in
this session. It is written to the same fail-loud contract and is
naked-Python (no bash), so it is runnable unattended on a Windows box, but
its correctness is unverified. Do not read `platform.system() != "Darwin"
implies broken` — it implies untested.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

from coordinator_core.win_portability import no_console_creationflags
from dataclasses import dataclass, field
from pathlib import Path

_ENTRYPOINTS: "tuple[str, ...]" = ("coordinator-invoke", "coordinator-cockpit-emit-schema")

# Harmless, side-effect-free flags per entrypoint -- prove the resolved
# binary actually EXECUTES (not just that a name resolves to a path), per
# the plan's "resolve AND execute" acceptance criterion. Neither flag
# dispatches an op, writes a file, or spawns a further subprocess.
_EXEC_PROOF_ARGS: "dict[str, tuple[str, ...]]" = {
    "coordinator-invoke": ("--dump-op-timeouts",),
    "coordinator-cockpit-emit-schema": ("--help",),
}

_PROBE_TIMEOUT_SECS = 15.0


@dataclass
class EntrypointCheck:
    name: str
    resolved_path: "str | None"
    executed_ok: bool
    detail: str


@dataclass
class PathResolutionReport:
    platform: str
    method: str
    checks: "list[EntrypointCheck]" = field(default_factory=list)
    platform_caveat: "str | None" = None
    transport_error: "str | None" = None
    #: Bare-name shadows found beside a resolved entrypoint (Windows only; see
    #: `_detect_bare_name_shadows`). Deliberately NOT folded into `all_ok`: a
    #: shadowed door still resolves and still executes, so every check passes.
    #: What it costs is the door's whole reason to exist -- an interpreter start
    #: on the hot path -- which is a performance defect to report, not a
    #: resolution failure to fail the probe on.
    shadow_warnings: "list[str]" = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        if self.transport_error is not None:
            return False
        return bool(self.checks) and all(c.resolved_path and c.executed_ok for c in self.checks)


def _windows_caveat() -> str:
    return (
        "Windows: this check reflects the CURRENT process's inherited environment "
        "block, not a live re-read of HKCU\\Environment. It is a true post-install "
        "check ONLY when this probe itself is the first thing run in a NEW "
        "shell/session started after the installer's PATH write broadcast "
        "(WM_SETTINGCHANGE) -- a plain child spawned from an already-running "
        "process (e.g. the installer itself, or a long-lived agent session) will "
        "under-report even a successful install. Open a new terminal and re-run "
        "this probe there before trusting a FAIL."
    )


def _posix_login_shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"


#: Delimiter marking the start of each entrypoint's output block inside the single
#: combined -lc payload `_check_posix` builds -- lets one login-shell spawn report
#: on every entrypoint instead of one spawn per entrypoint. Chosen to be effectively
#: impossible for a resolved path or exec-proof output to collide with.
_POSIX_ENTRY_MARKER = "===coordinator-path-probe-entry==="


def _check_posix(names: "tuple[str, ...]") -> PathResolutionReport:
    shell = _posix_login_shell()
    checks: "list[EntrypointCheck]" = []

    # PATH is built once at login-shell startup (profile files re-sourced by `-lc`),
    # not per name looked up inside it -- so every entrypoint's `command -v` +
    # exec-proof block can share ONE login-shell spawn instead of one per name. Each
    # block is prefixed with a marker + the entrypoint name so the single combined
    # stdout can be split back apart per entrypoint below.
    blocks: "list[str]" = []
    for name in names:
        args = " ".join(_EXEC_PROOF_ARGS[name])
        blocks.append(
            f'echo "{_POSIX_ENTRY_MARKER}{name}"; '
            f'p="$(command -v {name} 2>/dev/null)"; '
            f'if [ -n "$p" ]; then echo "$p"; {name} {args} >/dev/null 2>&1; echo "RC=$?"; else echo "RC=NOTFOUND"; fi'
        )
    script = "; ".join(blocks)

    try:
        proc = subprocess.run(
            [shell, "-lc", script],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            # This caller consumes the output itself (`capture_output=True`),
            # so the creationflags-only helper is the correct one — and it
            # returns `{}` off Windows, leaving this POSIX-only login-shell
            # probe bit-for-bit unchanged.
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        for name in names:
            checks.append(EntrypointCheck(
                name=name, resolved_path=None, executed_ok=False,
                detail=f"login-shell probe failed to run ({shell} -lc): {exc}",
            ))
        return PathResolutionReport(
            platform=platform.system(),
            method=f"login shell ({shell} -lc), one combined subprocess for all entrypoints",
            checks=checks,
        )

    stderr_tail = (proc.stderr or "").strip()[:300]
    raw = (proc.stdout or "")
    per_entry: "dict[str, str]" = {}
    for chunk in raw.split(_POSIX_ENTRY_MARKER)[1:]:
        entry_name, _, rest = chunk.partition("\n")
        per_entry[entry_name.strip()] = rest

    for name in names:
        block = per_entry.get(name)
        lines = (block or "").strip().splitlines()
        if block is None or not lines or lines[0] == "" or lines[-1].strip() == "RC=NOTFOUND":
            checks.append(EntrypointCheck(
                name=name, resolved_path=None, executed_ok=False,
                detail=f"`command -v {name}` returned nothing in a fresh {shell} -lc shell "
                       f"(stderr: {stderr_tail})",
            ))
            continue

        resolved_path = lines[0].strip()
        rc_line = lines[-1].strip()
        executed_ok = rc_line == "RC=0"
        checks.append(EntrypointCheck(
            name=name, resolved_path=resolved_path, executed_ok=executed_ok,
            detail=(
                f"resolved at {resolved_path}; exec-proof ({' '.join(_EXEC_PROOF_ARGS[name])}) "
                f"{'succeeded' if executed_ok else 'FAILED (' + rc_line + ')'}"
            ),
        ))

    return PathResolutionReport(
        platform=platform.system(),
        method=f"login shell ({shell} -lc), one combined subprocess for all entrypoints",
        checks=checks,
    )


def _detect_bare_name_shadows(resolved: "list[str]") -> "list[str]":
    """Reports any `install_bin_forwarders`-written sibling sitting beside an
    INSTALLED door that would win bare-name resolution over it.

    The strip that prevents this (`door_install.claim_bare_name`) runs once, at
    install time, on the path that lands the door. Nothing re-checks a box
    afterwards, so a hand-edit or a partial re-install can reintroduce the
    sibling with no error and no exit code -- only a silent fall back to the
    cold path (measured 579ms vs 165ms). This is the surface that makes that
    state self-reporting; it does not repair it, deliberately. Repair has to be
    conditioned on whether a door is SUPPOSED to be there, which this probe
    cannot know and the installer already does.

    Windows-only by construction: PATHEXT-style bare-name resolution of a `.ps1`
    is a Windows behaviour, and on POSIX the door is the extensionless
    `coordinator-invoke` with no suffixed sibling able to outrank it.

    Reuses `door_install`'s own constants rather than restating the suffix list
    -- a second copy is how the two drift. Imported lazily: `door_install` pulls
    `coordinator_core.warm.door.build`, which this probe has no other reason to
    load. Non-raising, matching every other leg here: a detector that cannot run
    reports nothing rather than failing the probe it is advising on.
    """
    try:
        from coordinator_core.install import door_install
    except Exception:  # noqa: BLE001 -- advisory leg, never crashes the probe
        return []

    warnings: "list[str]" = []
    seen: "set[Path]" = set()
    for where in resolved:
        bin_dir = Path(where).parent
        if bin_dir in seen:
            continue
        seen.add(bin_dir)
        door = bin_dir / door_install.DOOR_INSTALLED_NAME
        if not door.exists():
            continue
        for suffix in door_install._SHADOWING_SIBLING_SUFFIXES:
            shadow = bin_dir / (door_install.BARE_FORWARDER_NAME + suffix)
            if shadow.exists():
                warnings.append(
                    f"{shadow} shadows the installed door at {door}: PowerShell ranks a "
                    f"same-directory {suffix} above .exe, so every bare-name "
                    f"`{door_install.BARE_FORWARDER_NAME}` call from PowerShell pays an "
                    f"interpreter start instead of the door's native relay. Re-run the "
                    f"installer, or remove the sibling, to restore the door."
                )
    return warnings


def _check_windows(names: "tuple[str, ...]") -> PathResolutionReport:
    checks: "list[EntrypointCheck]" = []
    for name in names:
        where = shutil.which(f"{name}.cmd") or shutil.which(name)
        if where is None:
            checks.append(EntrypointCheck(
                name=name, resolved_path=None, executed_ok=False,
                detail=f"shutil.which found neither {name}.cmd nor {name} on the inherited PATH",
            ))
            continue
        args = list(_EXEC_PROOF_ARGS[name])
        try:
            proc = subprocess.run(
                [where, *args], capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECS,
                stdin=subprocess.DEVNULL,
                # The Windows arm, and the one that actually needed this: an
                # exec-proof probe fired during install would otherwise flash a
                # conhost window per entrypoint. `capture_output=True` already
                # wires the handles, so the creationflags-only helper is correct.
                **no_console_creationflags(),
            )
            executed_ok = proc.returncode == 0
            detail = (
                f"resolved at {where}; exec-proof ({' '.join(args)}) "
                f"{'succeeded' if executed_ok else f'FAILED (rc={proc.returncode}): ' + (proc.stderr or '')[:300]}"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            executed_ok = False
            detail = f"resolved at {where}, but exec-proof failed to run: {exc}"
        checks.append(EntrypointCheck(name=name, resolved_path=where, executed_ok=executed_ok, detail=detail))

    return PathResolutionReport(
        platform="Windows", method="shutil.which() against the inherited PATH, then direct exec",
        checks=checks, platform_caveat=_windows_caveat(),
        shadow_warnings=_detect_bare_name_shadows([c.resolved_path for c in checks if c.resolved_path]),
    )


def check_entrypoint_path_resolution(
    names: "tuple[str, ...]" = _ENTRYPOINTS,
) -> PathResolutionReport:
    """Resolve-and-execute check for `names` on the current platform.

    Never raises -- any transport-level failure (no login shell resolvable,
    subprocess machinery itself broken) is folded into
    `PathResolutionReport.transport_error`, matching this install chain's
    other probes' "report, don't crash the caller" contract.
    """
    try:
        if platform.system() == "Windows":
            return _check_windows(names)
        return _check_posix(names)
    except Exception as exc:  # noqa: BLE001 -- probe-authoring invariant: never crash the caller
        return PathResolutionReport(
            platform=platform.system(), method="unavailable", transport_error=str(exc),
        )


def main(argv: "list[str] | None" = None) -> int:
    """Standalone-script entry point — the plan's "standalone script" leg,
    distinct from the doctor-probe registration (bin/claude-klabauter-doctor-probe.py
    :: _run_probe_entrypoints_path_resolved), which is the "chain-walk" leg.
    Prints a human-readable summary; exit 0 iff every checked entrypoint
    resolved AND executed cleanly.
    """
    report = check_entrypoint_path_resolution()
    print(f"[path-resolution-probe] platform={report.platform} method={report.method}")
    if report.platform_caveat:
        print(f"[path-resolution-probe] CAVEAT: {report.platform_caveat}", file=sys.stderr)
    if report.transport_error:
        print(f"[path-resolution-probe] TRANSPORT ERROR: {report.transport_error}", file=sys.stderr)
        return 1
    for warning in report.shadow_warnings:
        print(f"[path-resolution-probe] WARN shadowed-door: {warning}", file=sys.stderr)
    ok = True
    for check in report.checks:
        status = "PASS" if (check.resolved_path and check.executed_ok) else "FAIL"
        print(f"[path-resolution-probe] {status} {check.name}: {check.detail}")
        ok = ok and bool(check.resolved_path and check.executed_ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

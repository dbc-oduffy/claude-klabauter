"""
coordinator_core.ops.detect_hardware — ported from coordinator/lib/detect-hardware.sh
(DOE-PORT R2-R6, variant #1 — pristine, no registered op, DoE keeps the .sh filename as
a polyglot trampoline over this module).

Purpose: cross-platform hardware audit for the machine-local registry. Detects CPU
cores, RAM, and (best-effort) GPU/VRAM, then persists the values into
hardware.local.toml via the `machine-local set --concern hardware` writer. Idempotent:
re-run re-audits and upserts without losing other keys.

Platforms:
  Linux   — /proc/cpuinfo (nproc), /proc/meminfo
  macOS   — sysctl hw.ncpu, hw.memsize
  Windows — psutil (cores, RAM; no shell spawn) + the registry (GPU name/VRAM
            via HKLM\\...\\Class\\{Display GUID}, no shell spawn). Converted
            2026-08-06 off a `powershell.exe Get-CimInstance` spawn per the
            PM's no-shell-spawns ruling (2026-08-06) — see this module's own
            `_detect_cores_windows`/`_detect_ram_bytes_windows`/
            `_detect_gpu_windows` docstrings for the per-API rationale.

Platform detection uses `sys.platform`, NOT `$OSTYPE` (the bash oracle's signal) —
see `_platform()`'s own docstring for why the literal env-var port would silently
misclassify every platform when reached via the polyglot trampoline.

GPU/VRAM detection is best-effort and non-blocking: if the probe fails or the tool is
unavailable, the key is simply not written — no error is emitted.

Fail-loud policy: if a required probe (cores, RAM) cannot resolve a value, main()
returns non-zero with a specific remediation message. Never writes a placeholder or a
wrong number.

Prerequisites: `machine-local` must be on PATH (installed by install-substrate.sh
before this script is called), or resolvable at `~/.claude/bin/machine-local`.
MACHINE_LOCAL_REGISTRY_DIR may override the default registry location for testing
(consumed by the `machine-local` CLI itself, not read directly here — mirrors the bash
oracle, which never reads this var either).

Port source: coordinator/lib/detect-hardware.sh (DoE-claude)
Spec backlink: docs/plans/2026-06-23-coordinator-install-surface-dogfood-hardening.md §C4

Negative-spec:
    - Does NOT shell out to wmic OR PowerShell on Windows — cores/RAM go through
      psutil (already a declared engine dependency; see pyproject.toml), and
      GPU name/VRAM go through `winreg` reads against the Display device-setup
      class, replacing the bash oracle's `Get-CimInstance`-via-`powershell.exe`
      spawn entirely (no allowlist carve-out needed for this site).
    - Does NOT write to registry.local.toml — only shells out to the `--concern
      hardware` writer, exactly as the bash oracle did.
    - Does NOT reimplement the machine-local TOML writer — shells out to the
      `machine-local` CLI (PATH-resolved, then `~/.claude/bin/machine-local`
      fallback) exactly like the bash oracle, so the writer logic has exactly one
      implementation.
    - Reproduces a pre-existing oracle quirk verbatim: on macOS, the GPU name is
      parsed from `system_profiler SPDisplaysDataType` via a "Chipset Model:" label
      match that captures the FULL value after the label (not just the last word) —
      a prior reviewer comment in the bash oracle flagged this as correct-as-is (the
      comment reads "reviewer — $NF printed only last word (\"Pro\"); -F': ' captures
      full value after label", i.e. documenting that the current form is already the
      fix, not a residual bug). Ported unchanged.
    - macOS has no VRAM probe (system_profiler's AdapterRAM equivalent is not queried)
      — hardware.vram_gb is only ever written on the Windows path. This matches the
      bash oracle's platform branching exactly (not a porting gap).
    - Windows console popup: N/A as of the psutil/winreg conversion — neither path
      spawns a child process, so there is no console to suppress. (The prior
      PowerShell-era -WindowStyle Hidden / CREATE_NO_WINDOW suppression no longer
      applies to this module; see
      docs/plans/2026-06-19-windows-console-popup-coordinator-doctrine.md for the
      general doctrine this module used to lean on.)
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
from typing import List, Optional, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.win_portability import is_executable, no_console_creationflags, no_console_passthrough_kwargs

try:
    import psutil
except ImportError:  # psutil is a declared engine dependency (pyproject.toml);
    # the guard exists ONLY so this module stays importable on a host missing
    # it (mirrors coordinator_core/session/core.py's own psutil guard). Every
    # call site below that is actually load-bearing on it (Windows cores/RAM)
    # falls back to None rather than silently degrading past this module's
    # documented fail-loud contract.
    psutil = None  # type: ignore[assignment]

# Windows "Display" device-setup class GUID — stable across Windows versions,
# documented by Microsoft (docs.microsoft.com/windows-hardware/drivers/install/
# system-defined-device-setup-classes-available-to-vendors, GUID_DEVCLASS_DISPLAY).
# Reading DriverDesc / HardwareInformation.{qwMemorySize,MemorySize} under each
# numbered adapter subkey is the standard non-WMI way native tools (e.g.
# GPU-Z-class utilities) resolve GPU name + VRAM without shelling out.
_DISPLAY_CLASS_GUID = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
)

_PROG = "detect-hardware"  # literal program-name prefix, matches the DoE oracle's echo prefix

_BYTES_PER_GB = 1073741824  # 1024**3
_ROUND_HALF_GB = 536870912  # _BYTES_PER_GB // 2, for round-to-nearest-GB


def _resolve_machine_local() -> Optional[str]:
    """Locate the `machine-local` CLI: PATH first, then settings-home, then
    ~/.claude/bin fallback.

    Mirrors the bash oracle's three-tier resolution (PATH, $CLAUDE_HOME/.claude/bin,
    $HOME/.claude/bin) collapsed to two Python-side checks since CLAUDE_HOME and HOME
    resolve to the same fallback shape once CLAUDE_HOME is set. A settings-home rung
    is checked ahead of the legacy ~/.claude/bin fallbacks, mirroring
    coordinator_core.pyresolve._machine_local_impl's settings-home-first ordering.
    """
    on_path = shutil.which("machine-local")
    if on_path:
        return on_path

    settings_home_candidate = os.path.join(str(settings_home()), "bin", "machine-local")
    if os.path.isfile(settings_home_candidate) and is_executable(settings_home_candidate):
        return settings_home_candidate

    claude_home = os.environ.get("CLAUDE_HOME", "")
    if claude_home:
        candidate = os.path.join(claude_home, ".claude", "bin", "machine-local")
        if os.path.isfile(candidate) and is_executable(candidate):
            return candidate

    home = os.environ.get("HOME", "")
    if home:
        candidate = os.path.join(home, ".claude", "bin", "machine-local")
        if os.path.isfile(candidate) and is_executable(candidate):
            return candidate

    return None


def _platform() -> str:
    """Classify the running platform as 'windows', 'macos', or 'linux'.

    Deliberately does NOT read $OSTYPE (unlike the bash oracle) — OSTYPE is a
    bash-builtin variable, not an exported environment variable, so it is
    invisible to a Python process reached via the polyglot trampoline's `exec`
    (which replaces the bash process rather than forking a child that inherits
    bash's internal variable table). Using `sys.platform` instead is the
    process-boundary-safe equivalent: 'darwin' -> macos, 'win32'/'cygwin' ->
    windows, else -> linux, matching the bash oracle's OSTYPE classification
    outcomes on every real platform it targets. OS=Windows_NT (a genuinely
    exported env var on Windows, unlike OSTYPE) is kept as an additional
    windows signal for parity with the oracle's second branch.
    """
    if os.environ.get("OS", "") == "Windows_NT" or sys.platform.startswith("win") or sys.platform == "cygwin":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _run(cmd: List[str]) -> Optional[str]:
    """Run cmd, return raw stdout on success, None on any failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, **no_console_creationflags()
        )
    except OSError:
        print(f"skip: _run: result = subprocess.run(cmd, capture_output=True, text=True, check=Fal failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _detect_cores_windows() -> Optional[int]:
    """Windows logical-core count via psutil (no shell spawn). Falls back to the
    NUMBER_OF_PROCESSORS environment variable (always set by the OS on real
    Windows) if psutil is unavailable or returns nothing usable."""
    if psutil is not None:
        try:
            count = psutil.cpu_count(logical=True)
        except Exception:  # pragma: no cover - psutil internals, defensive only
            count = None
        if count:
            return int(count)
    env_val = os.environ.get("NUMBER_OF_PROCESSORS", "")
    return int(env_val) if env_val.isdigit() else None


def _detect_ram_bytes_windows() -> Optional[int]:
    """Windows total physical RAM in bytes via psutil (no shell spawn)."""
    if psutil is None:
        return None
    try:
        total = psutil.virtual_memory().total
    except Exception:  # pragma: no cover - psutil internals, defensive only
        return None
    return int(total) if total else None


def _coerce_registry_int(value: object) -> Optional[int]:
    """Coerce a `winreg.QueryValueEx` result to an int, or None if it can't be.

    Review: coordinator:code-reviewer P1 — HardwareInformation.qwMemorySize /
    .MemorySize are frequently stored as REG_BINARY (an 8-byte, or on older
    drivers 4-byte, little-endian blob) rather than REG_QWORD/REG_DWORD on
    real hardware, so `winreg.QueryValueEx` can legitimately return `bytes`
    here. A bare `int(value)` raises `TypeError` on `bytes`, which is not an
    `OSError` and was not caught by the enclosing handler — breaking this
    module's documented "never raises" contract. Handles the full type space
    (int, 8-byte bytes, 4-byte bytes, anything else) rather than broadening
    the except clause into a bare catch."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (bytes, bytearray)):
        if len(value) == 8:
            return struct.unpack("<Q", bytes(value))[0]
        if len(value) == 4:
            return struct.unpack("<I", bytes(value))[0]
        return None
    return None


def _adapter_vram_bytes(adapter_key) -> Optional[int]:
    """VRAM in bytes for one adapter registry key, or None.

    Prefers the 64-bit `HardwareInformation.qwMemorySize` (present on modern
    drivers) and falls back to the older 32-bit `HardwareInformation.MemorySize`
    (which under-reports on adapters with >4GB VRAM — a known limitation of this
    registry path, not a bug here)."""
    import winreg

    for value_name in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
        try:
            raw, _ = winreg.QueryValueEx(adapter_key, value_name)
        except FileNotFoundError:
            continue
        as_int = _coerce_registry_int(raw) if raw else None
        if as_int:
            return as_int
    return None


def _detect_gpu_windows() -> Tuple[Optional[str], Optional[int]]:
    """Windows GPU name + VRAM (GB) via the registry (no shell spawn, no WMI).

    Enumerates EVERY numbered adapter subkey under the Display device-setup
    class (`_DISPLAY_CLASS_GUID`) carrying a `DriverDesc`, and reports the one
    with the most VRAM. Best-effort, never raises.

    Why largest-VRAM and not first-found: the previous implementation mirrored
    the retired CIM query's `Select-Object -First 1`, returning whichever
    adapter the registry happened to enumerate first. On a laptop or a desktop
    with an iGPU that is the INTEGRATED adapter — on the reference box it
    reported `Intel(R) Graphics` / 2GB while an `NVIDIA GeForce RTX 5070 Ti`
    with 16GB sat at the next index. Example-retrieval-repo's embed sidecar sizes itself
    off `hardware.vram_gb`, so first-found silently provisions the whole fleet
    against the weakest adapter on the machine. Enumeration order is not a
    capability ranking; do not restore `-First 1` semantics for oracle parity.

    Adapters reporting no VRAM at all lose to any adapter that reports some.
    If NO adapter reports VRAM, the first one with a `DriverDesc` is returned
    with `None` VRAM — the pre-existing degraded shape, unchanged."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - stdlib on Windows only
        return None, None

    best_desc: Optional[str] = None
    best_bytes: Optional[int] = None

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS_GUID) as class_key:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(class_key, index)
                except OSError:
                    break
                index += 1
                if not subkey_name.isdigit():
                    continue
                try:
                    with winreg.OpenKey(class_key, subkey_name) as adapter_key:
                        try:
                            driver_desc, _ = winreg.QueryValueEx(adapter_key, "DriverDesc")
                        except FileNotFoundError:
                            continue
                        vram_bytes = _adapter_vram_bytes(adapter_key)
                        if best_desc is None or (vram_bytes or 0) > (best_bytes or 0):
                            best_desc = driver_desc
                            best_bytes = vram_bytes
                except OSError:
                    continue
    except OSError:
        return None, None

    if best_desc is None:
        return None, None
    vram_gb = (best_bytes + _ROUND_HALF_GB) // _BYTES_PER_GB if best_bytes else None
    return best_desc, vram_gb


def _detect_cores(platform: str) -> Optional[int]:
    """Detect CPU core count. Returns None if undetectable (fail-loud caller decides)."""
    if platform == "windows":
        return _detect_cores_windows()

    raw: Optional[str]
    if platform == "macos":
        if shutil.which("sysctl"):
            out = _run(["sysctl", "-n", "hw.ncpu"])
            raw = out.strip() if out is not None else None
        else:
            raw = None
    else:
        # Linux
        if shutil.which("nproc"):
            out = _run(["nproc"])
            raw = out.strip() if out is not None else None
        elif os.path.isfile("/proc/cpuinfo"):
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as f:
                    raw = str(sum(1 for line in f if line.startswith("processor")))
            except OSError:
                raw = None
        else:
            raw = None

    if raw is None or not raw.isdigit():
        return None
    return int(raw)


def _detect_ram_gb(platform: str) -> Optional[int]:
    """Detect total RAM, rounded to the nearest GB. Returns None if undetectable."""
    ram_bytes: Optional[int] = None

    if platform == "windows":
        ram_bytes = _detect_ram_bytes_windows()
    elif platform == "macos":
        if shutil.which("sysctl"):
            out = _run(["sysctl", "-n", "hw.memsize"])
            raw = out.strip() if out is not None else None
            if raw is not None and raw.isdigit():
                ram_bytes = int(raw)
    else:
        # Linux: /proc/meminfo reports kB
        if os.path.isfile("/proc/meminfo"):
            try:
                with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            parts = line.split()
                            if len(parts) >= 2 and parts[1].isdigit():
                                ram_bytes = int(parts[1]) * 1024
                            break
            except OSError:
                print(f"skip: _detect_ram_gb: with open(\"/proc/meminfo\", \"r\", encoding=\"utf-8\", errors=\"replace\") as failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass

    if ram_bytes is None:
        return None
    return (ram_bytes + _ROUND_HALF_GB) // _BYTES_PER_GB


def _detect_gpu(platform: str) -> Tuple[Optional[str], Optional[int]]:
    """Best-effort GPU name + VRAM (GB) detection. Never raises; returns (None, None)
    on any failure or unsupported platform (Linux VRAM is never detected, matching the
    bash oracle — only the name is probed there via lspci)."""
    gpu: Optional[str] = None
    vram_gb: Optional[int] = None

    if platform == "windows":
        name, vram_gb = _detect_gpu_windows()
        if name:
            # bash oracle: `tr -s ' '` collapses runs of spaces, `head -1` keeps first line.
            collapsed = re.sub(r" +", " ", name)
            lines = collapsed.splitlines()
            gpu = lines[0] if lines else collapsed
        if vram_gb is not None and vram_gb <= 0:
            vram_gb = None
    elif platform == "macos":
        if shutil.which("system_profiler"):
            out = _run(["system_profiler", "SPDisplaysDataType"])
            if out:
                for line in out.splitlines():
                    m = re.match(r"\s*Chipset Model:\s?(.*)", line)
                    if m:
                        gpu = m.group(1)
                        break
    else:
        # Linux
        if shutil.which("lspci"):
            out = _run(["lspci"])
            if out:
                for line in out.splitlines():
                    if re.search(r"vga|3d|display", line, re.IGNORECASE):
                        # bash oracle: sed 's/.*: //' then sed 's/ (.*//'
                        after_colon = re.sub(r"^.*: ", "", line, count=1)
                        gpu = re.sub(r" \(.*", "", after_colon, count=1)
                        break

    return gpu, vram_gb


def _ml_set(ml_bin: str, key: str, value: str) -> int:
    """Invoke `machine-local set --concern hardware <key> <value>`, returns exit code."""
    try:
        result = subprocess.run(
            [ml_bin, "set", "--concern", "hardware", key, value],
            check=False,
            **no_console_passthrough_kwargs(),
        )
    except OSError:
        print(f"skip: _ml_set: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 1
    return result.returncode


def main(argv: List[str]) -> int:
    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print(
            f"{_PROG}: machine-local not found on PATH or at ~/.claude/bin/machine-local",
            file=sys.stderr,
        )
        print(
            "  Remediation: run install-substrate.sh first, or ensure ~/.claude/bin is on PATH.",
            file=sys.stderr,
        )
        return 1

    platform = _platform()

    cores = _detect_cores(platform)
    if cores is None:
        print(f"{_PROG}: could not determine CPU core count.", file=sys.stderr)
        print("  Remediation: set hardware.cores manually:", file=sys.stderr)
        print("    machine-local set --concern hardware hardware.cores <n>", file=sys.stderr)
        return 1

    ram_gb = _detect_ram_gb(platform)
    if ram_gb is None:
        print(f"{_PROG}: could not determine total RAM.", file=sys.stderr)
        print("  Remediation: set hardware.ram_gb manually:", file=sys.stderr)
        print("    machine-local set --concern hardware hardware.ram_gb <n>", file=sys.stderr)
        return 1

    gpu, vram_gb = _detect_gpu(platform)

    # Flush after every print: the `machine-local` calls below are unbuffered
    # child-process writes to the SAME stdout, and stdout interleaving order
    # is part of the bash oracle's observable output contract (each
    # `[detect-hardware] ...` line precedes the `machine-local: set ...` line
    # it triggers) — without an explicit flush here, Python's buffered stdout
    # can reorder relative to the child's unbuffered writes.
    print(f"[{_PROG}] cores={cores} ram_gb={ram_gb}")
    sys.stdout.flush()
    _ml_set(ml_bin, "hardware.cores", str(cores))
    _ml_set(ml_bin, "hardware.ram_gb", str(ram_gb))

    if gpu:
        print(f"[{_PROG}] gpu={gpu}")
        sys.stdout.flush()
        _ml_set(ml_bin, "hardware.gpu", gpu)  # best-effort — `|| true` in the bash oracle

    if vram_gb is not None and vram_gb > 0:
        print(f"[{_PROG}] vram_gb={vram_gb}")
        sys.stdout.flush()
        _ml_set(ml_bin, "hardware.vram_gb", str(vram_gb))  # best-effort — `|| true` in the bash oracle

    print(f"[{_PROG}] hardware audit complete — values in hardware.local.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

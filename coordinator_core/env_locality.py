"""
coordinator_core.env_locality -- bootstrap-safe execution-environment primitive.

Answers two questions every load-sensitive decision in this engine implicitly
asks, and which were previously answered by ad-hoc `platform.system()` reads or
a shell-out:

  1. ``os_family()``   -- windows / darwin / linux / posix-other.
  2. ``locality(env)`` -- is this somebody's machine, or a disposable box?

WHY A PRIMITIVE AND NOT A CHECK AT EACH SITE. The governing facts differ per
environment and the wrong default is expensive in both directions: an op that is
fine to run on a dedicated ephemeral VM can wreck an attended box carrying ~50
peer sessions, and a Windows host pays >20x process-creation cost besides. The
guidance is only useful if reaching for it is cheaper than reasoning about it,
so this module is measured, memoized and spawn-free -- see COST below.

THE RUNG LADDER. Two rungs, and they are NOT peers:

  Rung 0 -- the harness. ``CLAUDE_CODE_REMOTE == "true"``. This is a DOCUMENTED
    Claude Code contract carrying an explicit negative guarantee -- Anthropic's
    cloud-environments docs state the session VM "carries that variable as
    `true`, it's never `true` locally". A contract, not an observed leak.
    ``CLAUDE_CODE_REMOTE_SESSION_ID`` and ``CLAUDECODE`` are likewise
    documented. Every other ``CLAUDE_CODE_*`` marker this module reads is an
    undocumented internal and may ONLY raise confidence, never be the sole
    basis for a call.

  Rung 1 -- harness-free inference. A conjunction of kernel identity, silicon
    class and firmware identity. Strictly weaker than rung 0: it answers "is
    this a headless VM", which is not the same set as "is this in the cloud".
    Where the two disagree, RUNG 0 WINS -- see ``cross_check``.

  There is a third state, ``suspect``, and it must survive. Collapsing it into
    either answer is what turns the one irreducible ambiguity (a QEMU/libvirt
    guest on the default ``-cpu`` model, which reports a generic brand string
    and so could be a datacentre or a homelab) into a wrong decision on
    somebody's desk. A caller taking an irreversible or expensive path is
    entitled to refuse a low-confidence answer rather than round it.

``env`` IS A PARAMETER, NEVER AN AMBIENT READ. The warm server's ``os.environ``
belongs to whoever spawned it, not to the caller of any given op
(``coordinator_core.ipc``: "The warm SERVER cannot read the caller's env").
Engine-side callers pass ``caller_context.env``. Rung 1 is machine-constant and
needs no forwarding; rung 0 is per-caller and does -- ``CLAUDE_CODE_REMOTE`` is
declared in ``warm.env_forwarding.FORWARDING_SET`` for exactly that reason.
Fail-safe by construction: if that entry is ever missing, rung 1 still answers
and the result degrades to a labelled confidence rather than a confident lie.

COST (measured 2026-09-05, K=20,000, this repo's spike verdict record
``DoE-claude docs/research/spike-verdicts/2026-09-05-environment-locality-and-
os-family-probe.md``):

    os_family()                     0 syscalls   0.00015 ms
    rung 0 alone                    0 syscalls   0.00063 ms
    rung 1 uncached                 4 syscalls   0.03660 ms
    locality() memoized             0 syscalls   0.00018 ms
    cross_check(), BOTH rungs       4 syscalls   0.03801 ms

  For comparison: ``systemd-detect-virt`` costs one spawn, 3.9 ms p50 and a
  127.8 ms tail. Running BOTH rungs here is 0.0076% of DR-344's 500 ms
  brightline, which is why this module corroborates rather than picking one.

NEGATIVE SPEC:
  - Does NOT spawn. No subprocess, no shell-out, no third-party import.
  - Does NOT call ``platform.system()``: ~28 ms on Windows first call resolving
    the uname/win32_ver triple (measured 2026-08-08; see
    ``coordinator_core.atomic_append``). ``os.name`` is a preset constant.
  - Does NOT decide policy. It reports an environment; what to do about it
    belongs to the caller (see ``coordinator_core.session.mode_resolution``).
  - Does NOT cache the harness rung. That rung is per-caller and already free;
    only the machine-constant rung 1 is memoized.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, Mapping, NamedTuple, Optional, Tuple

__all__ = ["Locality", "os_family", "locality", "cross_check",
           "IS_WINDOWS", "IS_DARWIN", "IS_LINUX"]

IS_WINDOWS = os.name == "nt"
IS_DARWIN = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

#: Closed vocabulary. `attended` = somebody's machine. `cloud` = a box that is
#: not the developer's own (Anthropic-hosted OR a self-hosted runner -- the
#: documented definition of a cloud session is "any session that runs somewhere
#: other than the developer's machine", which is precisely the governing
#: distinction). `suspect` = genuinely undecidable from available signals.
CALLS = ("attended", "cloud", "suspect")
CONFIDENCES = ("certain", "high", "medium", "low")


class Locality(NamedTuple):
    call: str
    confidence: str
    rung: str
    basis: str


# --------------------------------------------------------------- silicon
_CONSUMER = re.compile(r"Core\(TM\)\s*(i[3579]|Ultra)|Core\s+i[3579]|Ryzen|"
                       r"Apple M\d|Celeron|Pentium|Athlon|Snapdragon", re.I)
_EXPLICIT_VM = re.compile(r"QEMU Virtual CPU|Common KVM processor|"
                          r"Common 32-bit KVM|Virtual CPU|AMD QEMU", re.I)
_SERVER = re.compile(r"Xeon|EPYC|Graviton|Altra|Ampere|Neoverse|POWER\d", re.I)
#: A real server part always carries a SKU designator; QEMU's generic server
#: models do not. That absence, paired with a synthetic microcode revision, is
#: the masked-silicon tell that identifies a cloud hypervisor.
_SKU = re.compile(r"Gold|Silver|Bronze|Platinum|E[357]-|v[2-6]\b|W-|D-|"
                  r"\b\d{4}[A-Z]*\b", re.I)
_SYNTHETIC_MICROCODE = frozenset({"0x1", "0x0", "1", "0", ""})
_CLOUD_DMI_VENDORS = ("Amazon EC2", "Google", "Microsoft Corporation",
                      "OpenStack", "DigitalOcean", "Hetzner", "Alibaba Cloud",
                      "Oracle", "Scaleway")


def _silicon_class(brand: str) -> str:
    """consumer | server-masked | server-real | explicit-vm | unknown.

    `consumer` is the load-bearing one: hypervisors on developer machines run
    host-passthrough or host-model, so a guest sees the HOST's CPU. Consumer
    silicon is therefore a direct statement that a person's machine is
    underneath, however many virtualisation layers sit between -- which is what
    rescues Docker Desktop, WSL2 and VirtualBox from being called cloud.
    """
    if not brand:
        return "unknown"
    if _EXPLICIT_VM.search(brand):
        return "explicit-vm"
    if _CONSUMER.search(brand):
        return "consumer"
    if _SERVER.search(brand):
        return "server-real" if _SKU.search(brand) else "server-masked"
    return "unknown"


def _cpu_brand_linux(proc_cpuinfo: str = "/proc/cpuinfo") -> Tuple[str, str]:
    """One open + one bounded read + one close. ``/proc/cpuinfo`` is a seq_file
    that repeats a block per core, so a full read makes the kernel format
    every core's block -- 4.9 KB on a 4-core box, ~6x that on a 24-core one.
    Both fields live in the FIRST block, so a bounded read keeps this
    core-count-independent."""
    try:
        fd = os.open(proc_cpuinfo, os.O_RDONLY)
    except OSError:
        return ("", "")
    try:
        blob = os.read(fd, 2048).decode("utf-8", "replace")
    except OSError:
        return ("", "")
    finally:
        os.close(fd)
    brand = micro = ""
    for line in blob.split("\n"):
        if not brand and line.startswith("model name"):
            brand = line.partition(":")[2].strip()
        elif not micro and line.startswith("microcode"):
            micro = line.partition(":")[2].strip()
        if brand and micro:
            break
    return (brand, micro)


def _cpu_brand_windows(env: Mapping[str, str]) -> str:
    """``PROCESSOR_IDENTIFIER`` is a family/model string ("Intel64 Family 6
    Model 183 Stepping 1"), NOT the marketing brand string, so the consumer
    patterns never match it. The brand string lives in the registry; ``winreg``
    is stdlib and spawns nothing. The env var is the fallback so the field is
    never empty."""
    try:
        import winreg
        key = "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return env.get("PROCESSOR_IDENTIFIER", "")


def os_family() -> str:
    if IS_WINDOWS:
        return "windows"
    if IS_DARWIN:
        return "darwin"
    if IS_LINUX:
        return "linux"
    return "posix-other"


# ---------------------------------------------------------------- rung 0
def harness_rung(env: Optional[Mapping[str, str]] = None) -> Optional[Locality]:
    """The documented-contract rung. ``None`` when the harness says nothing.

    Order is by strength of guarantee, not convenience: the two documented
    variables first, the undocumented corroborators after, and the
    "Claude Code is here but silent about remoteness" inference last.
    """
    g = (os.environ if env is None else env).get
    if g("CLAUDE_CODE_REMOTE") == "true":
        return Locality("cloud", "certain", "harness", "CLAUDE_CODE_REMOTE=true")
    if str(g("CLAUDE_CODE_REMOTE_SESSION_ID", "")).startswith("cse_"):
        return Locality("cloud", "certain", "harness",
                        "CLAUDE_CODE_REMOTE_SESSION_ID=cse_*")
    if "--claude_code_remote--" in str(g("CLAUDE_CODE_CONTAINER_ID", "")):
        return Locality("cloud", "high", "harness", "CLAUDE_CODE_CONTAINER_ID")
    if str(g("CLAUDE_CODE_ENTRYPOINT", "")).startswith("remote_"):
        return Locality("cloud", "high", "harness", "CLAUDE_CODE_ENTRYPOINT=remote_*")
    if g("CLAUDECODE") == "1":
        return Locality("attended", "high", "harness",
                        "CLAUDECODE=1 with no remote marker")
    return None


# ---------------------------------------------------------------- rung 1
def _machine_rung_uncached(env: Mapping[str, str]) -> Locality:
    """Short-circuiting, cheapest decisive signal first. The peripheral probes
    are the expensive part and only run for hosts nothing else settled."""
    if IS_DARWIN:
        # Anthropic-hosted images are documented Ubuntu 24.04 x86_64, so darwin
        # is settled at zero syscalls.
        return Locality("attended", "high", "machine",
                        "darwin -- Anthropic-hosted images are Ubuntu x86_64")
    if IS_WINDOWS:
        brand = _cpu_brand_windows(env)
        cls = _silicon_class(brand)
        if cls == "consumer":
            return Locality("attended", "high", "machine",
                            "windows, consumer silicon (%s)" % brand[:44])
        if cls in ("server-real", "server-masked"):
            return Locality("suspect", "low", "machine",
                            "windows on server silicon (%s) -- not an "
                            "Anthropic-hosted image; possibly a self-hosted "
                            "runner" % brand[:36])
        return Locality("attended", "medium", "machine",
                        "windows -- not an Anthropic-hosted image")
    if not IS_LINUX:
        return Locality("suspect", "low", "machine",
                        "unhandled platform %s" % sys.platform)

    # ONE syscall yields the WSL discriminator, the architecture and the host
    # name. It replaces an open/read/close on /proc/sys/kernel/osrelease.
    release = os.uname().release
    low = release.lower()
    if "microsoft" in low or "wsl" in low:
        # Highest-stakes case: presents as Linux, needs the WINDOWS guards.
        return Locality("attended", "certain", "machine",
                        "WSL kernel (%s) -- Windows host underneath" % release)

    brand, micro = _cpu_brand_linux()
    cls = _silicon_class(brand)
    if cls == "consumer":
        return Locality("attended", "high", "machine",
                        "host silicon is consumer (%s)" % brand[:44])
    if cls == "server-masked":
        synthetic = micro in _SYNTHETIC_MICROCODE
        return Locality("cloud", "high" if synthetic else "medium", "machine",
                        "masked server silicon (%s), microcode=%s"
                        % (brand[:36], micro or "absent"))

    vendor = ""
    try:
        with open("/sys/class/dmi/id/sys_vendor", encoding="utf-8") as fh:
            vendor = fh.read().strip()
    except OSError:
        pass
    if vendor.startswith(_CLOUD_DMI_VENDORS):
        return Locality("cloud", "high", "machine", "DMI sys_vendor=%s" % vendor)
    if cls == "explicit-vm":
        # The irreducible band: a hypervisor's own generic brand string
        # confirms a VM and says nothing about whether a desk or a datacentre
        # is underneath. `suspect` is the honest answer, not a rounding error.
        return Locality("suspect", "low", "machine",
                        "hypervisor generic brand (%s); host unknown" % brand[:36])

    # Nothing decisive: fall back to whether a human could be sitting here.
    absent = [p for p in ("/dev/input/event0", "/sys/class/drm/card0")
              if not os.path.exists(p)]
    if len(absent) == 2 and not vendor:
        return Locality("cloud", "medium", "machine",
                        "headless: no input device, no display pipeline, no DMI")
    if absent:
        return Locality("suspect", "low", "machine",
                        "partially headless (%s absent)" % ", ".join(absent))
    return Locality("attended", "medium", "machine", "peripherals present")


#: Rung 1 is a fact about the MACHINE, not about a caller, so it is constant
#: for the life of the process and memoized. Rung 0 is per-caller and is never
#: cached -- it is already free.
_MACHINE_CACHE: Dict[str, Locality] = {}


def machine_rung(env: Optional[Mapping[str, str]] = None,
                 force: bool = False) -> Locality:
    if not force and "v" in _MACHINE_CACHE:
        return _MACHINE_CACHE["v"]
    value = _machine_rung_uncached(os.environ if env is None else env)
    _MACHINE_CACHE["v"] = value
    return value


def locality(env: Optional[Mapping[str, str]] = None,
             force: bool = False) -> Locality:
    """The one call sites should use. Rung 0 when the harness speaks, else
    rung 1. ``env`` is the caller's environment -- engine-side that is
    ``caller_context.env``, never the warm server's own ``os.environ``."""
    hit = harness_rung(env)
    if hit is not None:
        return hit
    return machine_rung(env, force=force)


def cross_check(env: Optional[Mapping[str, str]] = None) -> dict:
    """Force BOTH rungs and report whether they agree -- 4 syscalls, 0.038 ms.

    DISAGREEMENT IS THE INTERESTING SIGNAL. Rung 0 is a contract and wins, but
    a mismatch means either the harness contract moved or this box is shaped
    unusually, and both are worth surfacing rather than silently resolving.
    """
    h = harness_rung(env)
    m = machine_rung(env, force=True)
    return {
        "os": os_family(),
        "harness": h,
        "machine": m,
        "agree": None if h is None else (h.call == m.call),
        "effective": h if h is not None else m,
    }

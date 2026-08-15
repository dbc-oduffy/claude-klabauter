"""coordinator_core.install.prereq_probe — Python-native port of
coordinator/scripts/lib/prereq_probe.sh's SSOT functional-prerequisite probe
suite for the coordinator install Step Zero gate.

Port source: coordinator/scripts/lib/prereq_probe.sh [DoE-claude repo] — the
bash oracle, and all of `coordinator/scripts/lib/` alongside it (including
its self-sourced siblings manifest_reader.sh + step_zero_emit.sh), is RETIRED
and no longer exists in DoE-claude. This module is now the SSOT
functional-prerequisite probe suite for every consumer, Python-side callers
and vendoring siblings alike — not a parallel implementation shadowing a
still-live bash original. Composes with
coordinator_core/install/manifest_reader.py and
coordinator_core/install/step_zero_emit.py, the two sibling ports it reuses
already-landed find_python()/emit_line() from rather than re-deriving
either.

Vendor closure for sibling repos re-vendoring this suite (e.g.
Example-retrieval-repo-ue-addon): `prereq_probe.py` + `manifest_reader.py` +
`step_zero_emit.py` + `win_portability.py` (the `no_console_creationflags`
import below) — all four stdlib-only once `coordinator_core.ipc` is treated
as optional (see the guarded `register_op` import below). Verified, not
assumed: `step_zero_emit.py` has no imports beyond stdlib; `manifest_reader.py`
is stdlib plus `from coordinator_core.win_portability import
no_console_creationflags` — itself inside this closure, which is why
`win_portability.py` is required rather than incidental; `win_portability.py`
itself is stdlib-only (`os`, `stat`, `pathlib`, `typing`). Caveat:
`probe_all()` reaches `probe_skill_frontmatter_valid`, whose two
function-local imports (`coordinator_core.ops.coordinator_doe_root`,
`coordinator_core.frontmatter.schema_validate`) sit outside this closure —
a vendor calling `probe_all()` wholesale needs those two modules too.

Spec backlink: docs/plans/2026-06-22-coordinator-env-normalization-step-zero.md
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Consumer status: coordinator_core.ops.normalize_env is wired to this module's
probe_all/probe_longpaths/probe_uv/probe_python/shell_login_env_reconstruction_source
(2026-07-21 de-bash cutover — see git log; previously bridged to the bash
oracle via a `bash -c 'source ...; <fn>'` subprocess). setup_chain_walker.py
(via prereq_probe_all_via_bash, name kept for call-site stability) and
plugin_health/sentinel.py (via _run_prereq_probe_function's native dispatch,
DR-079) now likewise call this module in-process; no consumer bridges to the
bash oracle via a subprocess any longer. See
coordinator_core/install/test_prereq_probe.py for unit coverage.

Each probe function returns the NDJSON line (string, trailing "\\n"
included, via step_zero_emit.emit_line) for exactly one probe, mirroring
the bash original's one-_co_pp_emit-call-per-probe shape. probe_all()
calls all thirteen (the original eleven plus probe_skill_frontmatter_valid
and probe_windows_terminal_presence, added 2026-07-22 — see the op-key
extension note above) in the SAME order as _co_prereq_probe_all plus the
two claude-klabauter-native additions, and returns the list of lines. main(argv) is
a thin CLI parity aid (mirrors the bash
file's own standalone `if [[ BASH_SOURCE == 0 ]]` entrypoint) — NOT a
registered op; this module has no claude-klabauter-mutating side effect (read-only
machine probes only), so there is nothing to route through cc_invoke.

Negative-spec — deliberate scope narrowings vs. the bash original:
  1. The bash-3.2-version guard (top of the .sh) has no analog here — this
     module runs inside an already->=3.11 CPython process, so the guard's
     precondition ("can this even be sourced by an old bash") cannot apply.
  2. The vendor-collision self-source guards (manifest_reader.sh /
     step_zero_emit.sh "wrong file shadowed" detection) have no analog —
     Python's normal import mechanism (package-qualified `from
     coordinator_core.install import ...`) cannot suffer the bash
     flat-vendor generic-filename collision the guards defend against.
  3. `shell_login_env_reconstruction_source` (the intact-zsh-PATH helper
     consumed by normalize_env.py's Darwin repair path) is ported below but
     lives outside `_PROBE_ORDER` — it is not a probe (no NDJSON emission,
     not part of `probe_all()`'s probe aggregate).
  4. `probe_pwsh`'s major-version parse (review: code-reviewer) is a
     different algorithm than the bash oracle's leading-digit-run
     extraction (`sed 's/[^0-9]*\\([0-9]*\\).*/\\1/'`): this port
     concatenates ALL digit/dot characters in the version string and takes
     the segment before the first dot, rather than the FIRST leading digit
     run. Converges on the expected "PowerShell 7.4.1" shape but is not
     byte-identical algorithm parity — a version string with digits
     appearing before the version proper (unlikely) could diverge.
  5. `probe_shell_login_env`'s dscl read (review: code-reviewer) gates on
     `returncode == 0` before reading stdout, stricter than the bash
     oracle's unconditional `dscl ... | awk ...` pipe (which parses
     stdout regardless of dscl's own exit status). Practically converges
     — a failing dscl almost always also produces empty stdout — but is a
     stricter gate than the oracle.
  6. `probe_clone_auth`'s SSH-absent path (review: code-reviewer) relies on
     `_run` returning `None` for each host (no `ssh` binary) rather than an
     explicit `shutil.which("ssh") is None` gate mirroring the bash
     oracle's `if command -v ssh` wrapper. Control flow converges (both
     fall through to the GCM check) but `ssh_probe_count` does not
     distinguish "ssh absent" from "ssh ran, no diagnostic output".

Op-key / contract extension (state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv, rows `probe-skill-frontmatter-valid` and
`probe-windows-terminal-presence`) — 2026-07-22, chunk w2-prereq-probe:

  install.probe_skill_frontmatter_valid   params: {} -> {ok: bool, error: str|None}
  install.probe_windows_terminal_presence params: {} -> {present: bool|None, method: str}

Both EXTEND this module (the one sanctioned `probe_*` grouping precedent, per
docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 2) rather
than minting a new module: each ships (a) a plain-dict core function carrying the
op's JSON-RPC contract shape, (b) a `probe_*() -> str` bash-parity wrapper that folds
the core result into an `emit_line()` NDJSON record and joins `_PROBE_ORDER` (now
thirteen probes), and (c) a `@register_op(...)`-decorated plain sync handler
returning the core dict unwrapped (the engine auto-offloads sync handlers to
asyncio.to_thread — AC-3 Gap-3; review: code-reviewer, Finding 1), matching the
split already used by `coordinator_core.install.clone_sibling_repo` (pure function
+ thin sync wrapper) rather than this module's own emit_line-only shape, since the
registered-op contract and the bash-parity NDJSON shape are genuinely different
response envelopes for the same underlying check.

DEC-7 idempotency/platform note (`probe_windows_terminal_presence`, oracle-rated
idempotency-hazard: none, platform-hazard: high — DEC-7 discipline applies because
platform-hazard clears the medium/high bar even though idempotency-hazard alone does
not): the check is read-only (PATH lookup, then a `winget list` query) so a second
invocation is trivially idempotent by construction — nothing it does can change
between calls absent an actual install/uninstall of Windows Terminal in between.
The platform hazard the oracle names is the bash oracle's own `$OSTYPE` msys/cygwin
string-match gate, itself a bash-ism; this port closes that by branching natively on
`sys.platform == "win32"` and returning a clean not-applicable result
(`{"present": None, "method": "not-applicable-non-windows"}`) on macOS/Linux BEFORE
any `winget` invocation is attempted, rather than porting the shell-gating check.
`probe_skill_frontmatter_valid` is oracle-rated idempotency-hazard/platform-hazard
`none`/`none` — no DEC-7 note required; it is a pure read (frontmatter parse) with no
platform branch.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.install.step_zero_emit import emit_line
from coordinator_core.win_portability import no_console_creationflags

try:
    from coordinator_core.ipc import register_op
except ModuleNotFoundError as exc:
    # Narrowed to "coordinator_core.ipc itself is absent" (the vendor
    # case), not any ImportError: a broken import *inside* ipc.py's own
    # chain (e.g. coordinator_core.lifecycle or coordinator_core.op_scopes
    # failing) must propagate as a real defect, not silently fall through
    # to the shim below and drop install.probe_skill_frontmatter_valid /
    # install.probe_windows_terminal_presence from _REGISTRY with no
    # diagnostic. (review: code-reviewer — Finding P1)
    if exc.name != "coordinator_core.ipc":
        raise
    # coordinator_core.ipc is a ~1700 LOC module that pulls in
    # coordinator_core.lifecycle and coordinator_core.op_scopes on import —
    # weight this module has no need of outside the full in-repo engine.
    # This module is also vendored byte-stable by sibling repos (e.g.
    # example-retrieval-repo-ue-addon) whose only interest is the probe functions
    # themselves, never op dispatch; a hard `ipc` import would drag the
    # whole engine into a vendor tree that has no engine to dispatch into.
    # The fallback below mirrors the real register_op(name, handler=None)
    # signature — parametrized-decorator form and direct-call form both
    # dispatch identically, returning the decorated/passed function
    # unchanged — a vendored copy routes no ops, so registering nothing is
    # the correct behaviour, not a degraded one.
    def register_op(name, handler=None):  # type: ignore[misc]
        def _decorator(fn):
            return fn

        if handler is not None:
            return _decorator(handler)
        return _decorator

_PROBE_TIMEOUT_SECS = 10.0
_NETWORK_PROBE_TIMEOUT_SECS = 8.0


def _run(argv: List[str], *, timeout: float = _PROBE_TIMEOUT_SECS, input_text: Optional[str] = None):
    """Uniform subprocess.run wrapper: stdin guard (DEVNULL unless input_text
    is supplied), timeout, CREATE_NO_WINDOW on Windows, text-mode capture.
    Returns a CompletedProcess-like object with .returncode/.stdout/.stderr,
    or None on any transport-level failure (binary absent, timeout, OSError)
    — callers treat None as "could not run", matching the bash `command -v`
    + functional-check two-step pattern collapsed into one call.

    Deliberate isolation boundary, not a candidate for an in-process
    import — this probes arbitrary external prerequisite binaries (not
    necessarily Python at all); a crashing or misbehaving prereq must not
    take down the probing process, which only a separate process boundary
    guarantees. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict.
    """
    try:
        return subprocess.run(
            argv,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return None


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text.splitlines() else text


def _os_name() -> str:
    """uname -s equivalent used throughout the bash original for OS
    branching (Darwin/Linux/MINGW*/MSYS*/CYGWIN*)."""
    system = platform.system()
    if system == "Windows":
        return "MINGW64_NT"  # any MINGW*/MSYS*/CYGWIN*-matching sentinel
    return system


# ---------------------------------------------------------------------------
# _co_probe_git
# ---------------------------------------------------------------------------
def probe_git() -> str:
    result = _run(["git", "--version"])
    remediation = (
        "install git: brew install git (macOS) / apt-get install git (Debian/Ubuntu) / "
        "winget install Git.Git (Windows)"
    )
    if result is None or result.returncode != 0 or not (result.stdout or "").strip():
        return emit_line("git", "fail", "hard", "git not found on PATH or `git --version` failed", remediation)
    return emit_line("git", "pass", "hard", _first_line(result.stdout), "")


# ---------------------------------------------------------------------------
# _co_probe_python
# ---------------------------------------------------------------------------
def probe_python() -> str:
    from coordinator_core.install.manifest_reader import NoPythonInterpreterError, find_python

    try:
        python_bin = find_python()
    except NoPythonInterpreterError as exc:
        detail = f"No functional Python 3.11+ found (tried python3, python, py launcher): {exc}"
        return emit_line(
            "python", "fail", "hard", detail,
            "Disable WindowsApps python/python3 App Execution aliases (Settings > Apps > "
            "App execution aliases) then install Python 3.11+ from https://www.python.org/downloads/",
        )
    result = _run([python_bin, "--version"])
    detail = _first_line(result.stdout or result.stderr or "") if result else "python (version unknown)"
    return emit_line("python", "pass", "hard", detail or "python (version unknown)", "")


# ---------------------------------------------------------------------------
# _co_probe_uv
# ---------------------------------------------------------------------------
def probe_uv() -> str:
    result = _run(["uv", "--version"])
    remediation = "install uv: `py -m pip install uv` (Windows) / `brew install uv` (macOS) / `pipx install uv` (pipx)"
    if result is None:
        return emit_line("uv", "warn", "advisory", "uv not found on PATH", remediation)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return emit_line(
            "uv", "warn", "advisory",
            "uv found but `uv --version` failed or produced empty output",
            "reinstall uv: `pip install --upgrade uv` or `brew reinstall uv`",
        )
    return emit_line("uv", "pass", "advisory", (result.stdout or "").strip(), "")


# ---------------------------------------------------------------------------
# _co_probe_gh
# ---------------------------------------------------------------------------
def probe_gh() -> str:
    remediation = "install GitHub CLI: `winget install GitHub.cli` (Windows) / `brew install gh` (macOS)"

    ver = _run(["gh", "--version"])
    if ver is None:
        return emit_line("gh", "fail", "hard", "gh (GitHub CLI) not found on PATH", remediation)
    if ver.returncode != 0 or not (ver.stdout or "").strip():
        return emit_line(
            "gh", "fail", "hard",
            "gh found but `gh --version` failed or produced empty output", remediation,
        )

    auth = _run(["gh", "auth", "status"])
    if auth is None or auth.returncode != 0:
        return emit_line(
            "gh", "fail", "hard",
            "gh found and functional but not authenticated (gh auth status: failed)",
            "authenticate: `gh auth login`",
        )

    probe_repo = os.environ.get("COORDINATOR_GH_PROBE_REPO")
    if probe_repo:
        repo_view = _run(["gh", "repo", "view", probe_repo])
        if repo_view is None or repo_view.returncode != 0:
            out = (repo_view.stdout if repo_view else "") + (repo_view.stderr if repo_view else "")
            return emit_line(
                "gh", "fail", "hard",
                f"gh authed but repo read probe failed for COORDINATOR_GH_PROBE_REPO={probe_repo}: {out}",
                "authenticate: `gh auth login`",
            )

    return emit_line("gh", "pass", "hard", _first_line(ver.stdout), "")


# ---------------------------------------------------------------------------
# _co_probe_node
# ---------------------------------------------------------------------------
def probe_node() -> str:
    result = _run(["node", "--version"])
    remediation = "install Node.js LTS: `winget install OpenJS.NodeJS.LTS` (Windows) / `brew install node` (macOS)"
    if result is None:
        return emit_line("node", "fail", "hard", "node (Node.js) not found on PATH", remediation)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return emit_line(
            "node", "fail", "hard",
            "node found but `node --version` failed or produced empty output", remediation,
        )
    return emit_line("node", "pass", "hard", (result.stdout or "").strip(), "")


# ---------------------------------------------------------------------------
# _co_probe_pwsh
# ---------------------------------------------------------------------------
def probe_pwsh() -> str:
    result = _run(["pwsh", "--version"])
    if result is not None:
        if result.returncode == 0:
            out = (result.stdout or "").strip()
            digits = "".join(ch for ch in out if ch.isdigit() or ch == ".")
            major_str = digits.split(".")[0] if digits else ""
            if not major_str:
                return emit_line(
                    "pwsh", "warn", "advisory",
                    f"pwsh found but version string undetectable (got: {out})",
                    "upgrade PowerShell: https://aka.ms/install-powershell",
                )
            try:
                major = int(major_str)
            except ValueError:
                major = -1
            if major >= 7:
                return emit_line("pwsh", "pass", "advisory", out, "")
            return emit_line(
                "pwsh", "warn", "advisory",
                f"pwsh found but version < 7 (got: {out})",
                "upgrade PowerShell: https://aka.ms/install-powershell",
            )
        return emit_line(
            "pwsh", "warn", "advisory",
            "pwsh found but `pwsh --version` failed",
            "reinstall PowerShell 7: https://aka.ms/install-powershell",
        )

    _os = _os_name()
    if _os.startswith(("MINGW", "MSYS", "CYGWIN")):
        legacy = _run(["powershell", "-Command", "$PSVersionTable.PSVersion.ToString()"])
        if legacy is not None:
            return emit_line(
                "pwsh", "warn", "advisory",
                "PowerShell 7 (pwsh) absent; Windows PowerShell 5.1 fallback detected in PATH",
                "install PowerShell 7: winget install Microsoft.PowerShell OR https://aka.ms/install-powershell",
            )

    if _os == "Darwin":
        remediation = "brew install powershell"
    elif _os == "Linux":
        remediation = "see https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-linux"
    elif _os.startswith(("MINGW", "MSYS", "CYGWIN")):
        remediation = "winget install Microsoft.PowerShell"
    else:
        remediation = "see https://aka.ms/install-powershell"

    return emit_line(
        "pwsh", "warn", "advisory",
        "PowerShell (pwsh) not found; coordinator does not require it", remediation,
    )


# ---------------------------------------------------------------------------
# _co_pp_ue_check_dir / _co_probe_ue
# ---------------------------------------------------------------------------
def _ue_check_dir(engine_root: str) -> Optional[str]:
    base = Path(engine_root)
    for rel in (
        "Engine/Binaries/Win64/UnrealEditor.exe",
        "Engine/Binaries/Linux/UnrealEditor",
        "Engine/Binaries/Mac/UnrealEditor",
    ):
        candidate = base / rel
        if candidate.is_file():
            return str(candidate)
    return None


def probe_ue() -> str:
    ue_root = os.environ.get("EXAMPLE_GAME_REPO_UE_ROOT")
    if ue_root:
        found = _ue_check_dir(ue_root)
        if found:
            return emit_line("ue", "pass", "advisory", f"UnrealEditor found via EXAMPLE_GAME_REPO_UE_ROOT: {found}", "")
        return emit_line(
            "ue", "warn", "advisory",
            f"EXAMPLE_GAME_REPO_UE_ROOT is set but UnrealEditor not found under it: {ue_root}",
            "verify EXAMPLE_GAME_REPO_UE_ROOT points to a valid UE install root (parent of Engine/)",
        )

    _os = _os_name()
    if _os.startswith(("MINGW", "MSYS", "CYGWIN")):
        found_engines: List[str] = []
        for drive in ("C:", "D:", "E:"):
            pf = Path(f"{drive}/Program Files/Epic Games")
            if not pf.is_dir():
                continue
            for ue_dir in sorted(pf.glob("UE_5.*")):
                if not ue_dir.is_dir():
                    continue
                found = _ue_check_dir(str(ue_dir))
                if found:
                    found_engines.append(found)
        if found_engines:
            return emit_line("ue", "pass", "advisory", f"UnrealEditor found: {'; '.join(found_engines)}", "")
        return emit_line(
            "ue", "warn", "advisory",
            "UnrealEditor not found in Program Files/Epic Games on C:, D:, E:",
            "install Unreal Engine via Epic Games Launcher, or set EXAMPLE_GAME_REPO_UE_ROOT to your install root",
        )

    import shutil

    ue_bin = shutil.which("UnrealEditor")
    if ue_bin:
        return emit_line("ue", "pass", "advisory", f"UnrealEditor found on PATH: {ue_bin}", "")
    return emit_line(
        "ue", "warn", "advisory",
        "UnrealEditor not found on PATH; coordinator does not require UE",
        "install Unreal Engine via Epic Games Launcher, or set EXAMPLE_GAME_REPO_UE_ROOT to your UE install root",
    )


# ---------------------------------------------------------------------------
# _co_probe_clone_auth
# ---------------------------------------------------------------------------
_SSH_NETWORK_ERROR_MARKERS = (
    "Network is unreachable",
    "Connection refused",
    "Operation timed out",
    "timed out",
    "Could not resolve",
    "Failed to connect",
    "Couldn't connect",
)
_LS_REMOTE_NETWORK_ERROR_MARKERS = (
    "Could not resolve host",
    "Failed to connect",
    "Network is unreachable",
    "Connection refused",
    "Operation timed out",
    "Couldn't connect to server",
    "timed out",
    "SSL",
    "proxy",
    "Proxy",
)


def probe_clone_auth() -> str:  # noqa: C901 — faithful port of a genuinely branchy oracle
    remediation_configure = (
        "configure git-host auth — recommended: gh auth login (GitHub) or glab auth login (GitLab); "
        "or add an SSH key / Git Credential Manager for your host"
    )

    # 1. gh auth status.
    gh_status = _run(["gh", "auth", "status"])
    if gh_status is not None and gh_status.returncode == 0:
        return emit_line("clone_auth", "pass", "advisory", "GitHub CLI authenticated (gh auth status: ok)", "")

    # 2. glab auth status.
    glab_status = _run(["glab", "auth", "status"])
    if glab_status is not None and glab_status.returncode == 0:
        return emit_line("clone_auth", "pass", "advisory", "GitLab CLI authenticated (glab auth status: ok)", "")

    # 3. SSH BatchMode probe against github.com, gitlab.com, + optional COORDINATOR_AUTH_PROBE_URL.
    hosts = ["git@github.com", "git@gitlab.com"]
    probe_url = os.environ.get("COORDINATOR_AUTH_PROBE_URL", "")
    if probe_url.startswith("git@"):
        hosts.append(probe_url)

    ssh_network_error = True
    ssh_probe_count = 0
    for host in hosts:
        ssh_probe_count += 1
        result = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-T", host])
        combined = ((result.stdout or "") + (result.stderr or "")) if result is not None else ""
        if "successfully authenticated" in combined:
            return emit_line(
                "clone_auth", "pass", "advisory",
                f"SSH key authenticated with {host} (successfully authenticated)", "",
            )
        if any(marker in combined for marker in _SSH_NETWORK_ERROR_MARKERS):
            continue
        ssh_network_error = False

    if ssh_probe_count > 0 and ssh_network_error:
        return emit_line(
            "clone_auth", "inconclusive", "advisory",
            "SSH probe(s) returned network-level errors; cannot determine clone auth state (offline machine?)",
            "ensure network connectivity, then re-run the preflight check",
        )

    # 4. Git Credential Manager / credential helper check.
    git_present = _run(["git", "--version"]) is not None
    if git_present:
        for gcm_host in ("github.com", "gitlab.com"):
            fill = _run(
                ["git", "credential", "fill"],
                input_text=f"protocol=https\nhost={gcm_host}\n\n",
            )
            if fill is not None and "password=" in (fill.stdout or ""):  # noqa: secrets
                return emit_line(
                    "clone_auth", "pass", "advisory",
                    f"Git credential manager configured for {gcm_host} (git credential fill: credentials found)", "",
                )

    # 5. Optional network probe via COORDINATOR_AUTH_PROBE_URL (HTTPS-shaped only; SSH handled in step 3).
    if probe_url and git_present:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
        try:
            ls_remote = subprocess.run(
                [
                    "git", "ls-remote", "--exit-code",
                    "-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=10",
                    probe_url, "HEAD",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_NETWORK_PROBE_TIMEOUT_SECS,
                encoding="utf-8",
                errors="replace",
                env=env,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            ls_remote = None

        if ls_remote is not None and ls_remote.returncode == 0:
            return emit_line(
                "clone_auth", "pass", "advisory",
                "git ls-remote authenticated against COORDINATOR_AUTH_PROBE_URL", "",
            )

        out = ((ls_remote.stdout or "") + (ls_remote.stderr or "")) if ls_remote is not None else ""
        if any(marker in out for marker in _LS_REMOTE_NETWORK_ERROR_MARKERS):
            return emit_line(
                "clone_auth", "inconclusive", "advisory",
                "Network unreachable or proxy/SSL error; cannot determine clone auth state",
                "ensure network connectivity and proxy/SSL config, then re-run the preflight check",
            )
        return emit_line(
            "clone_auth", "warn", "semi-hard",
            "git ls-remote failed (likely auth); no git-host authentication configured",
            remediation_configure,
        )

    if not git_present:
        return emit_line(
            "clone_auth", "inconclusive", "advisory",
            "git not found on PATH; cannot probe clone authentication",
            "install git, then configure gh auth login (GitHub), glab auth login (GitLab), or an SSH key",
        )

    return emit_line(
        "clone_auth", "warn", "semi-hard",
        "No git-host auth method detected (gh CLI, glab CLI, SSH key, or Git Credential Manager)",
        remediation_configure,
    )


# ---------------------------------------------------------------------------
# _co_probe_longpaths
# ---------------------------------------------------------------------------
def probe_longpaths() -> str:
    _os = _os_name()
    if not _os.startswith(("MINGW", "MSYS", "CYGWIN")):
        return emit_line("longpaths", "pass", "advisory", "n/a (non-Windows)", "")

    result = _run(["git", "config", "--get", "core.longpaths"])
    value = (result.stdout or "").strip() if result is not None else ""
    if value == "true":
        return emit_line(
            "longpaths", "pass", "advisory",
            "git core.longpaths=true (Windows long path support enabled)", "",
        )
    return emit_line(
        "longpaths", "warn", "advisory",
        f"git core.longpaths is not set to true (current: '{value or '<unset>'}'); "
        "Windows MAX_PATH limit may break coordinator file trees",
        "git config --global core.longpaths true",
    )


# ---------------------------------------------------------------------------
# _co_probe_git_lfs
# ---------------------------------------------------------------------------
def probe_git_lfs() -> str:
    remediation = (
        "enable Git LFS so LFS-backed dependent repos clone real objects: "
        "brew install git-lfs (macOS) / winget install GitHub.GitLFS (Windows), then git lfs install"
    )
    if _run(["git", "--version"]) is None:
        return emit_line("git_lfs", "warn", "advisory", "git not found on PATH; cannot probe git-lfs", remediation)

    lfs_ver = _run(["git", "lfs", "version"])
    if lfs_ver is None or lfs_ver.returncode != 0 or not (lfs_ver.stdout or "").strip():
        return emit_line("git_lfs", "warn", "advisory", "git-lfs not found or `git lfs version` failed", remediation)

    lfs_clean = _run(["git", "config", "--global", "--get", "filter.lfs.clean"])
    clean_val = (lfs_clean.stdout or "").strip() if lfs_clean is not None else ""
    if not clean_val:
        return emit_line(
            "git_lfs", "warn", "advisory",
            f"git-lfs present ({(lfs_ver.stdout or '').strip()}) but not configured "
            "(filter.lfs.clean missing; run: git lfs install)",
            remediation,
        )
    return emit_line("git_lfs", "pass", "advisory", (lfs_ver.stdout or "").strip(), "")


# ---------------------------------------------------------------------------
# _co_probe_shell_login_env
# ---------------------------------------------------------------------------
def probe_shell_login_env() -> str:
    if _os_name() != "Darwin":
        return emit_line("shell_login_env", "pass", "advisory", "non-macOS: login-shell orphan check N/A", "")

    dscl = _run(["dscl", ".", "-read", os.path.expanduser("~"), "UserShell"])
    login_shell = ""
    if dscl is not None and dscl.returncode == 0:
        out = (dscl.stdout or "").strip()
        if ":" in out:
            login_shell = out.split(":", 1)[1].strip()
    if not login_shell:
        login_shell = os.environ.get("SHELL", "")
    if not login_shell:
        return emit_line(
            "shell_login_env", "inconclusive", "advisory",
            "could not determine login shell (dscl returned empty and SHELL is unset)", "",
        )

    combined = _run([login_shell, "-lc", 'printf "%s\\n" "$PATH"; command -v claude 2>/dev/null'])
    combined_text = (combined.stdout or "") if combined is not None else ""
    lines = combined_text.split("\n", 1)
    fresh_path = lines[0] if lines else ""
    claude_in_shell = lines[1].strip() if len(lines) > 1 else ""

    if not fresh_path:
        return emit_line(
            "shell_login_env", "inconclusive", "advisory",
            f"could not probe login shell PATH (shell: {login_shell}; -lc returned empty)", "",
        )

    login_basename = os.path.basename(login_shell)
    if login_basename != "bash":
        return emit_line(
            "shell_login_env", "pass", "advisory",
            f"login shell is {login_shell} (not bash); orphan check N/A", "",
        )

    home = os.environ.get("HOME", os.path.expanduser("~"))
    local_bin_present = f":{fresh_path}:".find(f":{home}/.local/bin:") != -1

    if not local_bin_present or not claude_in_shell:
        if not local_bin_present:
            fail_detail = "bash login shell: $HOME/.local/bin absent from login PATH"
        else:
            fail_detail = "bash login shell: $HOME/.local/bin present in PATH but claude not found"
        return emit_line(
            "shell_login_env", "fail", "advisory", fail_detail,
            "bash login shell orphaned ~/.local/bin — run coordinator:install or normalize-env.sh "
            "to reconstruct ~/.bash_profile",
        )

    return emit_line(
        "shell_login_env", "pass", "advisory",
        f"bash login shell: ~/.local/bin present in PATH and claude resolves at {claude_in_shell}", "",
    )


# ---------------------------------------------------------------------------
# _co_shell_login_env_reconstruction_source (NOT a probe -- no NDJSON, no
# _PROBE_ORDER membership) -- repair-flow helper consumed by
# coordinator_core.ops.normalize_env's Darwin bash-profile repair path.
# ---------------------------------------------------------------------------
def shell_login_env_reconstruction_source() -> str:
    """Return the INTACT macOS-default zsh login shell's effective PATH.

    Reconstruction SOURCE consumed by normalize_env.py's Darwin repair path
    when rebuilding ~/.bash_profile to restore the coordinator PATH contract.

    WHY zsh and NOT the (already-broken) bash login shell: the bash login
    shell whose PATH is orphaned is the very shell being repaired -- spawning
    it returns the corrupted value and yields a circular reference. The zsh
    login shell config (~/.zprofile, /etc/zprofile, /etc/paths) is still on
    disk and untouched on macOS Ventura+, giving a correct anchor for
    reconstruction. This function invokes ``zsh`` directly -- it is not a
    bash bridge and is not touched by the bash-retirement sweep.

    Returns "" if zsh is absent or unprobeable (never raises), matching the
    bash oracle's `zsh -lc 'printf %s "$PATH"' 2>/dev/null || true`.
    """
    result = _run(["zsh", "-lc", 'printf %s "$PATH"'])
    if result is None:
        return ""
    return result.stdout or ""


# ---------------------------------------------------------------------------
# probe_skill_frontmatter_valid — op-key install.probe_skill_frontmatter_valid
#
# Verifies a representative skill file (coordinator-claude's own
# coordinator/skills/setup/SKILL.md) has parseable YAML frontmatter with a
# non-empty description field. Reads the OPERATOR's coordinator-claude
# installation tree (resolved via coordinator_doe_root()), not the caller's
# own dispatching repo — "none" scope-verdict, same class as probe_ue's
# EXAMPLE_GAME_REPO_UE_ROOT read.
# ---------------------------------------------------------------------------
_SKILL_FRONTMATTER_CHECK_REL_PATH = ("coordinator", "skills", "setup", "SKILL.md")


def _check_skill_frontmatter_valid() -> dict:
    """Core check: {"ok": bool, "error": str|None}.

    ok=False cases (error explains which): coordinator-claude root
    unresolvable, the representative skill file is missing, its frontmatter
    block does not parse, or its "description" field is empty/absent. Never
    raises — read failures fold into an ok=False/error result.
    """
    from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

    doe_root = coordinator_doe_root()
    if not doe_root:
        return {"ok": False, "error": "coordinator-claude root unresolvable (coordinator_doe_root() returned None)"}

    skill_path = Path(doe_root).joinpath(*_SKILL_FRONTMATTER_CHECK_REL_PATH)
    if not skill_path.is_file():
        return {"ok": False, "error": f"representative skill file not found: {skill_path}"}

    try:
        content = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"could not read {skill_path}: {exc}"}

    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    parsed = parse_frontmatter(content)
    frontmatter = parsed.get("frontmatter")
    if not frontmatter:
        return {"ok": False, "error": f"no parseable frontmatter block found in {skill_path}"}

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        return {"ok": False, "error": f"frontmatter 'description' field is empty or missing in {skill_path}"}

    return {"ok": True, "error": None}


def probe_skill_frontmatter_valid() -> str:
    check = _check_skill_frontmatter_valid()
    if check["ok"]:
        return emit_line(
            "skill_frontmatter_valid", "pass", "advisory",
            f"{'/'.join(_SKILL_FRONTMATTER_CHECK_REL_PATH)} frontmatter has a non-empty description", "",
        )
    return emit_line(
        "skill_frontmatter_valid", "warn", "advisory",
        check["error"] or "skill frontmatter check failed",
        "verify coordinator-claude's coordinator/skills/setup/SKILL.md exists with a "
        "--- frontmatter block carrying a non-empty description field",
    )


@register_op("install.probe_skill_frontmatter_valid")
def _probe_skill_frontmatter_valid_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.probe_skill_frontmatter_valid" handler.

    Params: none. `repo_root` is unused (scope-verdict "none" — see module
    docstring); kept for dispatch-shape consistency with other "none"-scoped
    ops (ping, install.clone_idempotent).

    Returns: {"ok": bool, "error": str|None} — see _check_skill_frontmatter_valid().

    Plain sync `def`, not `async def`: `_check_skill_frontmatter_valid()`
    does a blocking `Path.read_text()`. Lower-impact than Finding 1 (local
    file read, not a subprocess), but the same AC-3 Gap-3 contract gap —
    converted to sync so the engine's existing to_thread offload applies.
    (review: code-reviewer — Finding 4, blocking-file-read-in-async-handler)
    """
    return _check_skill_frontmatter_valid()


# ---------------------------------------------------------------------------
# probe_windows_terminal_presence — op-key install.probe_windows_terminal_presence
#
# Windows-only real check: wt.exe on PATH, else ask winget whether it lists
# Microsoft.WindowsTerminal. Branches natively on sys.platform=="win32" and
# returns a clean not-applicable result on macOS/Linux BEFORE any winget
# invocation — see module-top DEC-7 note.
# ---------------------------------------------------------------------------
def _check_windows_terminal_presence() -> dict:
    """Core check: {"present": bool|None, "method": str}.

    method values: "not-applicable-non-windows" (present=None, non-Windows
    host), "path" (present=True, wt.exe found on PATH), "winget"
    (present=True/False, determined via `winget list`), "winget-unavailable"
    (present=None, winget itself not resolvable), "winget-error" (present=None,
    winget invocation failed/timed out).
    """
    if sys.platform != "win32":
        return {"present": None, "method": "not-applicable-non-windows"}

    import shutil

    if shutil.which("wt.exe") or shutil.which("wt"):
        return {"present": True, "method": "path"}

    result = _run(["winget", "list", "--id", "Microsoft.WindowsTerminal", "--exact"])
    if result is None:
        return {"present": None, "method": "winget-unavailable"}
    if result.returncode != 0:
        return {"present": None, "method": "winget-error"}
    if "Microsoft.WindowsTerminal" in (result.stdout or ""):
        return {"present": True, "method": "winget"}
    return {"present": False, "method": "winget"}


def probe_windows_terminal_presence() -> str:
    check = _check_windows_terminal_presence()
    present = check["present"]
    method = check["method"]

    if method == "not-applicable-non-windows":
        return emit_line("windows_terminal_presence", "pass", "advisory", "n/a (non-Windows)", "")

    remediation = "install Windows Terminal: winget install Microsoft.WindowsTerminal"
    if present is True:
        return emit_line(
            "windows_terminal_presence", "pass", "advisory",
            f"Windows Terminal detected (method: {method})", "",
        )
    if present is False:
        return emit_line(
            "windows_terminal_presence", "warn", "advisory",
            f"Windows Terminal not detected (method: {method})", remediation,
        )
    return emit_line(
        "windows_terminal_presence", "inconclusive", "advisory",
        f"could not determine Windows Terminal presence (method: {method})", remediation,
    )


@register_op("install.probe_windows_terminal_presence")
def _probe_windows_terminal_presence_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "install.probe_windows_terminal_presence" handler.

    Params: none. `repo_root` is unused (scope-verdict "none" — see module
    docstring); kept for dispatch-shape consistency with other "none"-scoped
    ops.

    Returns: {"present": bool|None, "method": str} — see
    _check_windows_terminal_presence().

    Plain sync `def`, not `async def`: the Windows-only path calls a
    blocking `winget list` subprocess via `_run()`. ipc.py's register_op
    contract (AC-3 Gap-3) requires this be either sync (auto-offloaded to
    asyncio.to_thread by the engine) or the blocking call explicitly
    wrapped in asyncio.to_thread if kept async.
    (review: code-reviewer — Finding 1, async-handler-blocking-subprocess)
    """
    return _check_windows_terminal_presence()


# ---------------------------------------------------------------------------
# probe_all / main — aggregator + CLI parity aid.
# ---------------------------------------------------------------------------
_PROBE_ORDER = (
    probe_git,
    probe_python,
    probe_uv,
    probe_gh,
    probe_node,
    probe_pwsh,
    probe_ue,
    probe_clone_auth,
    probe_longpaths,
    probe_git_lfs,
    probe_shell_login_env,
    probe_skill_frontmatter_valid,
    probe_windows_terminal_presence,
)


def probe_all() -> List[str]:
    """Run all thirteen probes (the bash oracle's original eleven plus
    probe_skill_frontmatter_valid and probe_windows_terminal_presence) in
    `_PROBE_ORDER` and return the NDJSON lines (each already
    newline-terminated)."""
    return [probe() for probe in _PROBE_ORDER]


def main(argv: Optional[List[str]] = None) -> int:
    """CLI parity aid mirroring the bash file's standalone entrypoint
    (`if [[ BASH_SOURCE == 0 ]]; then _co_prereq_probe_all; fi`). No flags;
    argv is accepted for main(argv)->int convention symmetry with other
    ported ops but is otherwise unused. Always returns 0 — this is a
    read-only reporting probe suite, not a gate; pass/fail is encoded in
    each NDJSON line's own "status" field for the caller to interpret."""
    del argv
    for line in probe_all():
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":  # pragma: no cover — parity smoke aid, not a CLI contract
    sys.exit(main(sys.argv[1:]))

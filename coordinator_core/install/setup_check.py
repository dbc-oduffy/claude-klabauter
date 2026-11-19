"""coordinator_core.install.setup_check -- the verification behind `scripts/setup.py --check`.

Every item is a live read of the box (settings-home, registry, settings.json); none
consults a self-reported manifest. One `CheckItem` per verified fact; `exit_code`
is non-zero when any item failed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional


@dataclass(frozen=True)
class CheckItem:
    name: str
    ok: bool
    detail: str
    warn: bool = False

    def line(self) -> str:
        label = "FAIL" if not self.ok else ("WARN" if self.warn else "PASS")
        return f"{label} [{self.name}] {self.detail}"


def exit_code(items: Iterable[CheckItem]) -> int:
    return 0 if all(i.ok for i in items) else 1


_STATUS_LINE_LAUNCHER = "statusline-launch.py"
_LEADING_SUBSTITUTION = re.compile(r'^\s*[A-Za-z_]\w*="\$\(.*?\)[^"]*"\s*;\s*(\S.*)$', re.DOTALL)


def command_head(command: str) -> Optional[str]:
    """First token of a hook/statusline command after any leading `VAR=val` assignments.

    A leading `VAR="$(...)..."; rest` shell assignment (the breadcrumb statusLine form) is skipped
    whole: its command substitution is not a program, the interpreter after `;` is.
    """
    substitution = _LEADING_SUBSTITUTION.match(command)
    if substitution:
        return command_head(substitution.group(1))
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    for tok in tokens:
        name, eq, _ = tok.partition("=")
        if eq and name.replace("_", "").isalnum() and not name[:1].isdigit():
            continue
        return tok.strip('"')
    return None


def _resolves(head: str) -> bool:
    return Path(head).exists() or shutil.which(head) is not None


def check_forwarders(settings_home_path: Path, claude_klabauter_root: Path) -> CheckItem:
    from coordinator_core.install.settings_home_report import check_settings_home

    report = check_settings_home(settings_home_path, claude_klabauter_root)
    if report.forwarder_derivation_error is not None:
        return CheckItem("forwarders", False, f"cannot derive expected forwarders: {report.forwarder_derivation_error}")
    problems = []
    if report.fixed_missing:
        problems.append("missing members: " + ", ".join(m.label for m in report.fixed_missing))
    if report.forwarder_missing:
        problems.append(f"{len(report.forwarder_missing)} forwarder(s) missing (e.g. {report.forwarder_missing[0]})")
    if report.forwarder_unverified:
        problems.append(f"{len(report.forwarder_unverified)} forwarder(s) unverified (e.g. {report.forwarder_unverified[0]})")
    if report.door_image_stale:
        problems.append(f"{len(report.door_image_stale)} stale door image(s)")
    if problems:
        return CheckItem("forwarders", False, "; ".join(problems) + " -- re-run scripts/setup.py")
    return CheckItem("forwarders", True, f"{report.forwarder_present}/{report.forwarder_expected} current at {settings_home_path}")


def check_door(settings_home_path: Path) -> CheckItem:
    from coordinator_core.install import door_install

    bin_dst = settings_home_path / "bin"
    if not door_install.is_door_installed(bin_dst):
        return CheckItem("door", False, f"door or its engine-root sidecar missing in {bin_dst} -- re-run scripts/setup.py")
    verdict = door_install.verify_installed_provenance(bin_dst)
    if verdict.status != "ok":
        return CheckItem("door", False, f"provenance {verdict.status}: {verdict.detail}")
    return CheckItem("door", True, f"built and current at {bin_dst}")


_FORWARDER_SUFFIXES = (".exe", ".cmd") if os.name == "nt" else (".exe", ".cmd", "")


def _bare_forwarder(bin_dst: Path) -> Optional[Path]:
    """The bare `coordinator-invoke` a caller's PATH lookup reaches first: `.exe`, `.cmd`, then extensionless
    (the extensionless shim is not launchable on Windows, so it is not a candidate there)."""
    for suffix in _FORWARDER_SUFFIXES:
        candidate = bin_dst / f"coordinator-invoke{suffix}"
        if candidate.is_file():
            return candidate
    return None


def check_forwarder_launch(
    settings_home_path: Path,
    *,
    probe: "Optional[Callable[[Path], tuple[bool, str]]]" = None,
) -> CheckItem:
    """Launches the bare `coordinator-invoke` once; a blocked launch FAILs with the probe's diagnosis.

    `probe(image) -> (ok, detail)` is injectable; the default is `door_launch_probe.probe_launch`.
    An absent forwarder is `check_forwarders`' finding, so it is a WARN here.
    """
    image = _bare_forwarder(settings_home_path / "bin")
    if image is None:
        return CheckItem("forwarder-launch", True, f"no coordinator-invoke in {settings_home_path / 'bin'} to launch", warn=True)
    if probe is None:
        from coordinator_core.install.door_launch_probe import probe_launch as probe
    ok, detail = probe(image)
    if ok:
        return CheckItem("forwarder-launch", True, f"{image.name}: {detail}")
    return CheckItem("forwarder-launch", False, f"{image.name}: {detail}")


def _settings_json(claude_dir: Path) -> "tuple[dict, Optional[str]]":
    path = claude_dir / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return {}, f"{path} not found"
    except (OSError, ValueError) as exc:
        return {}, f"{path} unreadable: {exc}"


def _hook_commands(settings: dict) -> "list[str]":
    out: "list[str]" = []
    for groups in (settings.get("hooks") or {}).values():
        for group in groups or []:
            for hook in (group or {}).get("hooks") or []:
                cmd = hook.get("command")
                if isinstance(cmd, str):
                    out.append(cmd)
    return out


def check_guards(claude_klabauter_root: Path, claude_dir: Path) -> CheckItem:
    dispatch = claude_klabauter_root / "coordinator_core" / "bash_guards" / "dispatch.py"
    if not dispatch.is_file():
        return CheckItem("guards", False, f"guard dispatcher missing: {dispatch}")
    settings, _ = _settings_json(claude_dir)
    commands = _hook_commands(settings)
    unresolved = []
    for cmd in commands:
        head = command_head(cmd)
        if head is None or not _resolves(head):
            unresolved.append(head or cmd)
    if unresolved:
        return CheckItem("guards", False, f"{len(unresolved)} hook command(s) do not resolve: {unresolved[0]}")
    wired = f"{len(commands)} settings.json hook command(s) resolve" if commands else "hooks delivered by the plugin"
    return CheckItem("guards", True, f"dispatcher present; {wired}")


def check_repo_pointers(required_keys: "list[str]", registry_get: "Callable[[str], Optional[str]]") -> "list[CheckItem]":
    items = []
    for key in required_keys:
        value = registry_get(key)
        if not value:
            items.append(CheckItem(key, False, f"unset -- machine-local set {key} <path>"))
        elif not Path(value).is_dir():
            items.append(CheckItem(key, False, f"points at {value}, which is not a directory"))
        else:
            items.append(CheckItem(key, True, value))
    return items


# derive_defaults' key set does not depend on its RAM argument.
_KEY_SET_PROBE_MB = 16 * 1024


def check_heavy_admission(registry_get: "Callable[[str], Optional[str]]") -> CheckItem:
    """Every heavy_admission.* key the admission guard reads is a positive integer; an absent one
    denies every heavy launch once the guard enforces, and reads as healthy until then."""
    from coordinator_core.bash_guards._heavy_admission_seed import derive_defaults

    bad = []
    for key in sorted(derive_defaults(_KEY_SET_PROBE_MB)):
        value = registry_get(key)
        try:
            ok = int(str(value)) > 0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            bad.append(key)
    if bad:
        return CheckItem("heavy_admission", False, f"{len(bad)} key(s) unseeded, e.g. {bad[0]} -- re-run scripts/setup.py")
    return CheckItem("heavy_admission", True, "every heavy_admission.* key seeded")


def check_statusline(claude_dir: Path) -> CheckItem:
    settings, err = _settings_json(claude_dir)
    if err:
        return CheckItem("statusline", False, err)
    status = settings.get("statusLine")
    command = status.get("command") if isinstance(status, dict) else None
    if not command:
        return CheckItem("statusline", False, f"no statusLine.command in {claude_dir / 'settings.json'}")
    normalized = command.replace("\\", "/")
    if _STATUS_LINE_LAUNCHER in normalized:
        # The command names `python`/`python3` and a home-relative launcher; the shell resolves
        # both at run time, so verify the same two facts here.
        if not (claude_dir / _STATUS_LINE_LAUNCHER).is_file():
            return CheckItem("statusline", False,
                             f"statusLine launcher {claude_dir / _STATUS_LINE_LAUNCHER} is missing -- re-run scripts/setup.py")
        if not any(shutil.which(name) for name in ("python", "python3")):
            return CheckItem("statusline", False, f"statusLine command needs python or python3 on PATH: {command}")
        return CheckItem("statusline", True, command)
    if ("coordinator" in normalized and "/bin/statusline.py" in normalized
            and ".coordinator-plugin-root" not in normalized):
        return CheckItem("statusline", False,
                         "statusLine runs a pinned coordinator copy, not the live plugin -- re-run scripts/setup.py")
    head = command_head(command)
    if head is None or not _resolves(head):
        return CheckItem("statusline", False, f"statusLine command does not resolve: {command}")
    return CheckItem("statusline", True, command)


_PROFILE_SHELLS = ("pwsh", "powershell")
_BLOCKING_POLICIES = frozenset({"restricted", "undefined"})
_POLICY_QUERY_TIMEOUT_S = 10


def _winreg_value(hive: int, path: str, name: str) -> "Optional[object]":
    import winreg

    try:
        with winreg.OpenKey(hive, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _windows_powershell_policy(read: "Callable[[int, str, str], Optional[object]]" = _winreg_value) -> str:
    """Windows PowerShell 5.1 effective policy from the registry, in precedence order (group policy
    machine then user, CurrentUser, LocalMachine, SKU default). Windows PowerShell costs ~1s to start."""
    import winreg

    gpo = r"Software\Policies\Microsoft\Windows\PowerShell"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        enabled = read(hive, gpo, "EnableScripts")
        if enabled == 0:
            return "Restricted"
        if enabled == 1:
            value = read(hive, gpo, "ExecutionPolicy")
            if isinstance(value, str) and value:
                return value
    shell = r"Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        value = read(hive, shell, "ExecutionPolicy")
        if isinstance(value, str) and value:
            return value
    product = read(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\ProductOptions", "ProductType")
    return "Restricted" if product == "WinNT" else "RemoteSigned"


def _query_execution_policy(exe: str) -> str:
    """Effective execution policy `exe` would apply to a profile, with the env override cleared."""
    import subprocess
    import sys

    if sys.platform == "win32" and Path(exe).stem.lower() == "powershell":
        return _windows_powershell_policy()

    # PSModulePath is dropped too: one inherited from pwsh 7 stops Windows PowerShell loading its Security module.
    cleared = {"PSEXECUTIONPOLICYPREFERENCE", "PSMODULEPATH"}
    env = {k: v for k, v in os.environ.items() if k.upper() not in cleared}
    proc = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
        capture_output=True,
        text=True,
        timeout=_POLICY_QUERY_TIMEOUT_S,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise OSError(f"exit {proc.returncode}: {proc.stderr.strip()[:120]}")
    return proc.stdout.strip()


def engine_shell(which: "Callable[[str], Optional[str]]" = shutil.which) -> Optional[str]:
    """The PowerShell the engine launches for a Windows hook. `wrap_hook_command_guarded(windows=True)`
    emits PowerShell syntax naming no binary, so the shell is the first `_PROFILE_SHELLS` entry on
    PATH (PowerShell 7 ahead of Windows PowerShell). None when neither is installed."""
    return next((s for s in _PROFILE_SHELLS if which(s)), None)


def check_execution_policy(
    platform: Optional[str] = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    query: "Callable[[str], str]" = _query_execution_policy,
) -> "list[CheckItem]":
    """One item per installed profile shell. Only the engine shell (`engine_shell`) FAILs, when its
    effective policy is Restricted or Undefined or it cannot be queried; any other shell's block or
    query failure is a WARN naming the engine shell. Reports; never sets a policy."""
    import sys

    if (platform or sys.platform) != "win32":
        return [CheckItem("execution_policy", True, "not applicable off Windows")]
    items: "list[CheckItem]" = []
    engine = engine_shell(which)
    for shell in _PROFILE_SHELLS:
        exe = which(shell)
        if not exe:
            continue
        name = f"execution_policy[{shell}]"
        is_engine = shell == engine
        advisory = "" if is_engine else f" (advisory: the engine launches {engine})"
        try:
            policy = query(exe)
        except Exception as exc:  # noqa: BLE001 -- an unqueryable shell is a finding, not a crash
            items.append(CheckItem(name, not is_engine, f"could not query {shell}: {exc}{advisory}", warn=not is_engine))
            continue
        if policy.lower() in _BLOCKING_POLICIES:
            fix = f'{shell} -NoProfile -Command "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"'
            items.append(CheckItem(
                name, not is_engine,
                f"{shell} policy is {policy}; its profile will not load{advisory} -- run: {fix}",
                warn=not is_engine,
            ))
        else:
            items.append(CheckItem(name, True, f"{shell} policy is {policy}"))
    return items or [CheckItem("execution_policy", True, "no PowerShell profile shell on PATH")]


def check_python3(
    platform: Optional[str] = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
) -> CheckItem:
    """`python3` must resolve on Windows, where the shim from `substrate` is the only provider."""
    import sys

    if (platform or sys.platform) != "win32":
        return CheckItem("python3", True, "not applicable off Windows")
    found = which("python3")
    if found is None:
        return CheckItem("python3", False, "python3 does not resolve on PATH -- re-run scripts/setup.py")
    return CheckItem("python3", True, found)


def run_checks(
    claude_klabauter_root: Path,
    settings_home_path: Path,
    claude_dir: Path,
    required_repo_keys: "list[str]",
    registry_get: "Callable[[str], Optional[str]]",
    launch_probe: "Optional[Callable[[Path], tuple[bool, str]]]" = None,
) -> "list[CheckItem]":
    return [
        check_forwarders(settings_home_path, claude_klabauter_root),
        check_door(settings_home_path),
        check_forwarder_launch(settings_home_path, probe=launch_probe),
        check_guards(claude_klabauter_root, claude_dir),
        *check_repo_pointers(required_repo_keys, registry_get),
        check_heavy_admission(registry_get),
        check_statusline(claude_dir),
        *check_execution_policy(),
        check_python3(),
    ]

"""coordinator_core.install.setup_check -- the verification behind `scripts/setup.py --check`.

Every item is a live read of the box (settings-home, registry, settings.json); none
consults a self-reported manifest. One `CheckItem` per verified fact; `exit_code`
is non-zero when any item failed.
"""

from __future__ import annotations

import json
import os
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

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} [{self.name}] {self.detail}"


def exit_code(items: Iterable[CheckItem]) -> int:
    return 0 if all(i.ok for i in items) else 1


def command_head(command: str) -> Optional[str]:
    """First token of a hook/statusline command after any leading `VAR=val` assignments."""
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


def check_heavy_admission(registry_get: "Callable[[str], Optional[str]]") -> CheckItem:
    """Every heavy_admission.* key the admission guard reads is a positive integer; an absent one
    denies every heavy launch once the guard enforces, and reads as healthy until then."""
    from coordinator_core.bash_guards._heavy_admission_seed import derive_defaults

    bad = []
    for key in sorted(derive_defaults(16 * 1024)):
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
    head = command_head(command)
    if head is None or not _resolves(head):
        return CheckItem("statusline", False, f"statusLine command does not resolve: {command}")
    return CheckItem("statusline", True, command)


def run_checks(
    claude_klabauter_root: Path,
    settings_home_path: Path,
    claude_dir: Path,
    required_repo_keys: "list[str]",
    registry_get: "Callable[[str], Optional[str]]",
) -> "list[CheckItem]":
    return [
        check_forwarders(settings_home_path, claude_klabauter_root),
        check_door(settings_home_path),
        check_guards(claude_klabauter_root, claude_dir),
        *check_repo_pointers(required_repo_keys, registry_get),
        check_heavy_admission(registry_get),
        check_statusline(claude_dir),
    ]

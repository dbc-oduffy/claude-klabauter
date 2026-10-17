"""Read-only check of the coordinator record in ``installed_plugins.json``.

Plain ``claude`` is vanilla on a DoE dev box (a ``.coordinator-dev-repo`` sentinel
beside the plugin root): the coordinator shell function's ``--plugin-dir`` is the
only coordinator entry there, so a coordinator record found there is drift and is
reported, never repointed or removed. A consumer box keeps the marketplace record
``claude plugin install/update`` wrote; a missing one is reported, never created.
This module writes nothing. Ruling: DoE ``DR-plain-claude-is-vanilla-on-a-doe-box``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from coordinator_core.install import plugin_cache_sessions

_INSTALLED_PLUGINS_REL = ("plugins", "installed_plugins.json")

_PLUGIN_CACHE_REL = ("plugins", "cache")

STATUS_NO_ENTRY = "no-entry"
STATUS_UNREADABLE = "unreadable"
STATUS_LIVE_ROOT_MISSING = "live-root-missing"
STATUS_NOT_DEV_CLONE = "not-dev-clone"
STATUS_VANILLA = "vanilla"
STATUS_DRIFT = "drift"
STATUS_NO_COORDINATOR_RECORD = "no-coordinator-record"

#: Fleet-wide dev-vs-consumer discriminant, at the repo root one level above the
#: plugin root. A consumer box (plugin-cache install, or a published mirror clone)
#: lacks it and keeps the record `claude plugin install/update` wrote.
DEV_REPO_SENTINEL = ".coordinator-dev-repo"


def installed_plugin_paths(claude_home: Path) -> dict[str, str]:
    """Every plugin the platform records as installed, as
    ``{plugin-name: installPath}``.

    Keys are the bare plugin name -- the manifest keys it as
    ``<name>@<marketplace>``, and the marketplace half is not what a caller
    matching on a repo identity has to hand.

    Best-effort by contract: an absent, unreadable, or malformed manifest
    yields ``{}``, because every caller is advisory and a box with no plugins
    installed yet is the normal fresh-machine state, not a fault.

    Later records for one name win, matching how the platform itself reads a
    list whose last entry is the live one.
    """
    record_path = claude_home.joinpath(*_INSTALLED_PLUGINS_REL)
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    found: dict[str, str] = {}
    for key, records in plugins.items():
        if not isinstance(key, str) or not isinstance(records, list):
            continue
        name = key.split("@", 1)[0]
        if not name:
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            path = record.get("installPath")
            if isinstance(path, str) and path:
                found[name] = path
    return found


def read_plugin_name(live_plugin_root: Path) -> Optional[str]:
    manifest = live_plugin_root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def assert_live_plugin_registration(claude_home: Path, live_plugin_root: Path) -> dict[str, Any]:
    """Report whether a ``<plugin-name>@*`` record exists, and what that means on this box.

    Answers: is there a coordinator record in installed_plugins.json, and is
    this a dev-sentinel box? Never writes."""
    record_path = claude_home.joinpath(*_INSTALLED_PLUGINS_REL)
    sentinel = live_plugin_root.parent / DEV_REPO_SENTINEL
    report: dict[str, Any] = {
        "path": str(record_path),
        "entries": [],
        "live_path": str(live_plugin_root),
    }

    data: Any = {}
    if record_path.is_file():
        try:
            data = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["status"] = STATUS_UNREADABLE
            report["evidence"] = f"{type(exc).__name__}: {exc}"
            return report

    if not live_plugin_root.is_dir():
        report["status"] = STATUS_LIVE_ROOT_MISSING
        report["evidence"] = str(live_plugin_root)
        return report

    plugin_name = read_plugin_name(live_plugin_root)
    if plugin_name is None:
        report["status"] = STATUS_NO_ENTRY
        report["evidence"] = f"no readable .claude-plugin/plugin.json under {live_plugin_root}"
        return report

    plugins = data.get("plugins") if isinstance(data, dict) else None
    if isinstance(plugins, dict):
        for key, records in plugins.items():
            if (
                isinstance(key, str)
                and key.startswith(f"{plugin_name}@")
                and isinstance(records, list)
                and any(isinstance(r, dict) for r in records)
            ):
                install_path = next(
                    (r.get("installPath") for r in records if isinstance(r, dict) and r.get("installPath")),
                    None,
                )
                report["entries"].append({"key": key, "install_path": install_path})

    report["plugin_name"] = plugin_name
    if sentinel.is_file():
        report["status"] = STATUS_DRIFT if report["entries"] else STATUS_VANILLA
        if report["status"] == STATUS_DRIFT:
            for entry in report["entries"]:
                if entry["install_path"]:
                    entry["bound_sessions"] = plugin_cache_sessions.sessions_bound_to(
                        Path(entry["install_path"]), claude_home=claude_home
                    )
    elif report["entries"]:
        report["status"] = STATUS_NOT_DEV_CLONE
        report["evidence"] = str(sentinel)
    else:
        report["status"] = STATUS_NO_COORDINATOR_RECORD
    return report


def format_report(report: dict[str, Any]) -> list[str]:
    """The one-to-few lines stating the outcome."""
    status = report.get("status")
    if status == STATUS_VANILLA:
        return ["PASS [plugin] no coordinator record, plain `claude` is vanilla on a dev box"]
    if status == STATUS_DRIFT:
        lines: list[str] = []
        for e in report["entries"]:
            lines.append(
                f"WARN [plugin] {e['key']} registered at {e['install_path']} on a dev box; "
                "removal is the PM's call"
            )
            bound = e.get("bound_sessions")
            if bound:
                lines.extend(
                    plugin_cache_sessions.relaunch_lines(
                        Path(e["install_path"]), bound, step="the removal"
                    )
                )
        return lines
    if status == STATUS_NO_COORDINATOR_RECORD:
        name = report.get("plugin_name") or "coordinator"
        return [
            f"WARN [plugin] no {name} record in {report.get('path')}; "
            f"run `claude plugin install {name}@<marketplace>`"
        ]
    if status == STATUS_NO_ENTRY:
        return ["SKIP [plugin] no installed record for this plugin"]
    if status == STATUS_UNREADABLE:
        return [f"WARN [plugin] {report['path']} unreadable ({report.get('evidence')}); repair it and re-run"]
    if status == STATUS_LIVE_ROOT_MISSING:
        return [f"SKIP [plugin] live plugin root {report.get('evidence')} does not exist"]
    if status == STATUS_NOT_DEV_CLONE:
        return [
            f"SKIP [plugin] not a dev clone (no {report.get('evidence')}); "
            "keeping the record `claude plugin install/update` manages"
        ]
    return []

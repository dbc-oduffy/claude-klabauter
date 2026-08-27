"""Register the coordinator plugin AT the live clone, never as a copy of it.

THE REQUIREMENT (PM, 2026-08-26, via doe-claude-em): plain ``claude`` must run
the live coordinator surface on a DoE box. Not ``claude-doe`` — the muscle
memory is ``claude``, and a second binary name is a workaround, not an install
shape.

WHAT BLOCKED IT: ``claude plugin install`` COPIES a directory-source plugin into
``<claude-home>/plugins/cache/<marketplace>/<plugin>/<version>/`` and pins a
``gitCommitSha`` at install time — for a directory source exactly as for a git
one. Plain ``claude`` then serves a frozen snapshot of the clone. Measured on
the fleet-floor Mac: the snapshot was 4 commits stale 20 minutes after install
(51 ``coordinator:`` commands against 53 live). ``claude-doe`` was unaffected
because it injects ``--plugin-dir <clone>/coordinator`` and never consults the
cache — which is precisely why the gap went unnoticed: every agent-launched
session was live, and only a human typing ``claude`` got the snapshot.

THE SHAPE, AND WHY THIS ONE: point the installed-plugin record's ``installPath``
at the clone itself and carry no ``gitCommitSha``. Verified end-to-end on this
box, 2026-08-26: with the cache copy MOVED ASIDE ENTIRELY, plain ``claude``
still resolved ``coordinator:`` commands, and ``claude plugin list --json``
reported the clone as ``installPath``. Nothing is copied, so nothing can go
stale — the staleness property is definitional, not maintained.

This is not a new shape we invented for the problem. Our own OSS installer
(``coordinator/dist/publish-repo-setup/install.py::register_installed_plugins``)
already writes exactly this record — a real directory, no SHA — and
``example-retrieval-repo@example-retrieval-repo`` has run that way on the fleet-floor Mac since
2026-08-22.

NEGATIVE SPEC — NOT A SYMLINK. The shape proved from outside first was to
replace the cache directory with a symlink to the clone. It works, and it was
the right probe. It is the wrong install: it leaves a record claiming a cache
path that is not one, keeps a ``gitCommitSha`` pinned to a commit the session is
not running (a manifest that is a false witness), and leaves a displaced real
copy beside it for someone to find. Naming the clone directly makes the record
say what is true.

NEGATIVE SPEC — DOES NOT DELETE THE DISPLACED COPY. A copy left in the cache is
inert once nothing points at it; removing it is not this function's business and
would put a user's bytes on the line for a cosmetic gain. It is REPORTED so the
caller can say so.

WHY IT MUST RE-ASSERT EVERY RUN: any later ``claude plugin install`` or plugin
update rewrites the record back to a fresh copy, silently restoring snapshot
behaviour — nothing warns. Idempotence is therefore the whole design: this is
cheap, it is a no-op when already correct, and it is wired into the install
sequence rather than offered as a repair someone must remember to run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

#: The record file the platform reads to resolve an installed plugin.
_INSTALLED_PLUGINS_REL = ("plugins", "installed_plugins.json")

#: Where a COPIED plugin lands. Only a previous ``installPath`` under this
#: subtree is reported as a displaced copy — an installPath somewhere else
#: entirely is someone's deliberate choice and is reported as such, not as
#: cache residue.
_PLUGIN_CACHE_REL = ("plugins", "cache")

STATUS_ABSENT = "absent"
STATUS_NO_ENTRY = "no-entry"
STATUS_ALREADY_LIVE = "already-live"
STATUS_REPOINTED = "repointed"
STATUS_UNREADABLE = "unreadable"


def read_plugin_name(live_plugin_root: Path) -> Optional[str]:
    """The plugin's own declared name, read from the clone it lives in.

    Never hardcoded: the record's key is ``<name>@<marketplace>`` and the name
    half is the plugin's to declare. A hardcoded 'coordinator' would survive
    exactly until the plugin is renamed, and then repoint nothing while
    reporting success.
    """
    manifest = live_plugin_root / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def assert_live_plugin_registration(
    claude_home: Path, live_plugin_root: Path, *, dry_run: bool = False
) -> dict[str, Any]:
    """Point every installed record for this plugin at `live_plugin_root`.

    Idempotent by construction — a record already naming the clone with no
    pinned SHA is left byte-identical and reported ``already-live``.

    Returns a report dict: ``status`` (one of the module's STATUS_* values),
    ``entries`` (one dict per record acted on, each carrying ``key``,
    ``previous_path``, ``displaced_copy`` and ``dropped_sha``), and ``path``
    (the record file). Never raises on a missing or unreadable record file: a
    box that has never installed a plugin has nothing to assert, and that is an
    ordinary outcome, not a failure.
    """
    record_path = claude_home.joinpath(*_INSTALLED_PLUGINS_REL)
    report: dict[str, Any] = {"path": str(record_path), "entries": []}

    if not record_path.is_file():
        report["status"] = STATUS_ABSENT
        return report

    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report["status"] = STATUS_UNREADABLE
        report["evidence"] = f"{type(exc).__name__}: {exc}"
        return report

    plugin_name = read_plugin_name(live_plugin_root)
    if plugin_name is None:
        report["status"] = STATUS_NO_ENTRY
        report["evidence"] = (
            f"no readable .claude-plugin/plugin.json under {live_plugin_root}"
        )
        return report

    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        report["status"] = STATUS_NO_ENTRY
        return report

    live_str = str(live_plugin_root)
    cache_root = str(claude_home.joinpath(*_PLUGIN_CACHE_REL))
    changed = False

    for key, records in plugins.items():
        if not key.startswith(f"{plugin_name}@") or not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            previous = record.get("installPath")
            has_sha = "gitCommitSha" in record
            if isinstance(previous, str) and _same_path(previous, live_str) and not has_sha:
                continue
            displaced = (
                previous
                if isinstance(previous, str)
                and not _same_path(previous, live_str)
                and _same_path(os.path.commonpath([previous, cache_root]), cache_root)
                else None
            )
            report["entries"].append({
                "key": key,
                "previous_path": previous,
                "displaced_copy": displaced,
                "dropped_sha": record.get("gitCommitSha"),
            })
            record["installPath"] = live_str
            record.pop("gitCommitSha", None)
            changed = True

    if not report["entries"]:
        report["status"] = STATUS_ALREADY_LIVE if plugins else STATUS_NO_ENTRY
        return report

    report["status"] = STATUS_REPOINTED
    if changed and not dry_run:
        _atomic_write_json(record_path, data)
    return report


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write through a sibling temp + replace.

    The record file is read by every ``claude`` process on the box, and this
    install runs on a machine carrying dozens of live sessions — a torn read of
    a half-written record would strip coordinator from whichever session read
    it.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def format_report(report: dict[str, Any]) -> list[str]:
    """Installer-facing lines. One fact each, no reassurance."""
    status = report.get("status")
    if status == STATUS_ALREADY_LIVE:
        return ["PASS [plugin] coordinator plugin already registered at its live clone"]
    if status == STATUS_ABSENT:
        return ["SKIP [plugin] no installed_plugins.json — nothing registered on this box yet"]
    if status == STATUS_NO_ENTRY:
        return ["SKIP [plugin] no installed record for this plugin"]
    if status == STATUS_UNREADABLE:
        return [
            f"WARN [plugin] {report['path']} unreadable ({report.get('evidence')}) — "
            "plain `claude` may serve a frozen copy; re-run after repairing it"
        ]
    lines = []
    for entry in report.get("entries", []):
        lines.append(
            f"PASS [plugin] {entry['key']} now resolves live (was {entry['previous_path']})"
        )
        if entry.get("displaced_copy"):
            lines.append(
                f"  displaced copy left in place, now inert: {entry['displaced_copy']}"
            )
    return lines

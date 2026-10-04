"""Stdlib-only derivation of the hook-plane verdict shared by both installers.

`scripts/cloud_setup.py` loads this file by path on an interpreter that has no
`coordinator_core` installed, and `scripts/setup.py` imports it as a package
module. It therefore imports the standard library only and does no I/O at import
time. `tomllib` is imported inside `read_registry` so a pre-3.11 interpreter still
imports the module, and the missing `tomllib` is reported as an unreadable
registry rather than as an absent value.

Every function is a read of disk with zero spawns and no writes; each
installer lands the verdict through its own rule-surface writer.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

#: Where the platform records each installed plugin; `${CLAUDE_PLUGIN_ROOT}`
#: expands from its `installPath`, and a dead path silently disables all hooks.
PLUGIN_RECORD_REL = ("plugins", "installed_plugins.json")

#: A resolvable plugin root carries its own manifest; the directory alone is not enough.
PLUGIN_MANIFEST_REL = (".claude-plugin", "plugin.json")

#: The marketplace manifest, read for the marketplace's declared name.
MARKETPLACE_MANIFEST_REL = (".claude-plugin", "marketplace.json")

#: The plugin's own hook manifest, relative to its install root.
PLUGIN_HOOKS_REL = ("hooks", "hooks.json")

#: Script extensions recognised inside a hook command or argument; checked
#: against an already shlex-isolated piece, never scanned over a raw string.
_SCRIPT_EXTENSIONS = (".py", ".sh", ".mjs", ".js")

_PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}/"

#: Registry key naming the content repo. A legacy-named root is migrated into it
#: by `content_root.migrate_legacy_config` before the installer derives a verdict;
#: this stdlib-only module never reads the legacy spelling itself.
_REGISTRY_KEY = "repos.content_root"

#: Content-root pointer file, under `<settings-home>/machine-local/` and `<claude_home>/`.
_POINTER_NAME = ".coordinator-content-root"

#: One TCP connect, never a request: the forwarder either accepts or it is dark.
_PORT_PROBE_TIMEOUT_S = 0.25


def script_tail(piece: str) -> str | None:
    """The `<dir>/<file>` tail of `piece` if it names a script file, else None."""
    normalized = piece.replace("\\", "/").strip("'\"")
    if not normalized.endswith(_SCRIPT_EXTENSIONS):
        return None
    tail = "/".join(normalized.split("/")[-2:])
    return tail or None


def shlex_pieces(token: str) -> list[str]:
    """`token` split the way a shell would, so a quoted path with a space stays
    one piece. Falls back to a whitespace split on an unbalanced quote."""
    try:
        return shlex.split(token, posix=True)
    except ValueError:
        return token.split()


def hook_identities(hook: dict) -> set[str]:
    """What a hook entry runs, independent of the surface that registers it:
    `url:<url>` for an http hook, else the `<dir>/<file>` tail of each script
    it names. Tails, because the two surfaces spell the root differently."""
    if hook.get("type") == "http":
        url = hook.get("url")
        return {f"url:{url}"} if isinstance(url, str) and url else set()
    args = hook.get("args") if isinstance(hook.get("args"), list) else []
    identities: set[str] = set()
    for token in [hook.get("command"), *args]:
        if not isinstance(token, str):
            continue
        for piece in shlex_pieces(token):
            tail = script_tail(piece.replace("\\", "/"))
            if tail:
                identities.add(tail)
    return identities


def plugin_record_key(plugin_root: Path) -> str:
    """``<plugin>@<marketplace>``, both halves read from the plugin root's manifests.

    Raises on an unreadable manifest or a missing/empty name.
    """
    manifest = plugin_root.joinpath(*PLUGIN_MANIFEST_REL)
    marketplace = plugin_root.joinpath(*MARKETPLACE_MANIFEST_REL)
    plugin_name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
    marketplace_name = json.loads(marketplace.read_text(encoding="utf-8")).get("name")
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ValueError(f"{manifest} declares no usable plugin name")
    if not isinstance(marketplace_name, str) or not marketplace_name:
        raise ValueError(f"{marketplace} declares no usable marketplace name")
    return f"{plugin_name}@{marketplace_name}"


def read_registry(machine_local_dir: Path, key: str) -> tuple[str | None, list[str]]:
    """One flat `"<key>" = '<value>'` registry value via tomllib, no subprocess,
    plus every reason a registry file could not be read.

    `registry.local.toml` wins over `registry.toml`. A file that does not exist is
    simply absent (no error). A file that exists but cannot be read, an
    unparseable one, or a missing `tomllib` (<3.11) is an error entry: an
    unreadable registry must never degrade to "this rung says nothing".
    """
    errors: list[str] = []
    value: str | None = None
    for fname in ("registry.local.toml", "registry.toml"):
        path = machine_local_dir / fname
        try:
            import tomllib

            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except ImportError as e:
            errors.append(
                f"{fname}: tomllib unavailable on Python {sys.version_info[0]}.{sys.version_info[1]} "
                f"({sys.executable}); {type(e).__name__}: {e}"
            )
            break  # no later file is readable either
        except Exception as e:  # noqa: BLE001 - unreadable or unparseable is a recorded error
            errors.append(f"{fname}: {type(e).__name__}: {e}")
            continue
        found = data.get(key)
        if value is None and isinstance(found, str) and found:
            value = found
    return value, errors


def first_line_or_none(path: Path) -> str | None:
    """A pointer file's stripped content, or None when absent, unreadable, or blank."""
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def plugin_hook_delivery(settings: dict, *, claude_home: Path, plugin_root: Path | None) -> dict:
    """Whether the coordinator plugin's hook manifest delivers hooks to a session
    launched here, read off disk the way the runtime reads it.

    Three links: the plugin is enabled in `settings["enabledPlugins"]`;
    `<claude_home>/plugins/installed_plugins.json` records an `installPath`; and
    `<installPath>/hooks/hooks.json` registers at least one event with every
    `${CLAUDE_PLUGIN_ROOT}`-relative file it names present.

    Never raises: each broken link is a recorded `reason` with `armed` False.
    `_delivered` maps event -> script identities the manifest delivers; an
    identity counts only when every token naming it is `${CLAUDE_PLUGIN_ROOT}`-
    prefixed. Callers pop `_delivered` before storing the result in a report.
    """
    result: dict = {
        "armed": False,
        "event_count": 0,
        "missing_files": [],
        "http_ports": [],
        "reason": "",
        "_delivered": {},
    }
    delivered: dict[str, set[str]] = result["_delivered"]
    if plugin_root is None:
        result["reason"] = "no plugin root resolved"
        return result
    try:
        key = plugin_record_key(Path(plugin_root))
    except Exception as e:  # noqa: BLE001 - an unreadable manifest is a recorded miss
        result["reason"] = f"plugin key unreadable: {type(e).__name__}: {e}"
        return result
    result["key"] = key
    enabled = settings.get("enabledPlugins")
    if not (isinstance(enabled, dict) and enabled.get(key) is True):
        result["reason"] = f"{key} is not enabled in settings.json"
        return result
    try:
        records = json.loads(
            Path(claude_home).joinpath(*PLUGIN_RECORD_REL).read_text(encoding="utf-8")
        )["plugins"][key]
        if not isinstance(records, list):
            raise TypeError(f"expected a list of plugin records, got {type(records).__name__}")
        install_path = next(
            r["installPath"] for r in records if isinstance(r, dict) and r.get("installPath")
        )
    except Exception as e:  # noqa: BLE001 - an absent record is a recorded miss
        result["reason"] = f"no installed-plugin record names an installPath for {key} ({type(e).__name__}: {e})"
        return result
    result["install_path"] = install_path
    manifest = Path(install_path).joinpath(*PLUGIN_HOOKS_REL)
    try:
        hooks = json.loads(manifest.read_text(encoding="utf-8")).get("hooks")
    except Exception as e:  # noqa: BLE001 - an unreadable manifest is a recorded miss
        result["reason"] = f"{manifest} unreadable: {type(e).__name__}"
        return result
    if not isinstance(hooks, dict) or not hooks:
        result["reason"] = f"{manifest} registers no hook event"
        return result
    result["event_count"] = len(hooks)
    ports: set[int] = set()
    referenced: set[str] = set()
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") == "http":
                    url = hook.get("url")
                    if isinstance(url, str) and url:
                        delivered.setdefault(event, set()).add(f"url:{url}")
                        try:
                            port = urlparse(url).port
                        except ValueError:
                            port = None
                        if port:
                            ports.add(port)
                    continue
                args = hook.get("args") if isinstance(hook.get("args"), list) else []
                ids: set[str] = set()
                all_prefixed = True
                for token in [hook.get("command"), *args]:
                    if not isinstance(token, str):
                        continue
                    for piece in shlex_pieces(token):
                        normalized = piece.replace("\\", "/")
                        prefixed = normalized.startswith(_PLUGIN_ROOT_VAR)
                        if prefixed:
                            referenced.add(normalized[len(_PLUGIN_ROOT_VAR):])
                        tail = script_tail(normalized)
                        if tail:
                            ids.add(tail)
                            all_prefixed = all_prefixed and prefixed
                if ids and all_prefixed:
                    delivered.setdefault(event, set()).update(ids)
    missing = sorted(rel for rel in referenced if not Path(install_path, rel).is_file())
    result["missing_files"] = missing
    result["http_ports"] = sorted(ports)
    if missing:
        result["reason"] = f"{manifest} names {len(missing)} absent file(s), e.g. {missing[0]}"
        return result
    result["armed"] = True
    result["reason"] = f"{key} delivers {len(hooks)} hook event(s) from {manifest}"
    return result


def derive_hook_plane(
    *, claude_home: Path, plugin_root: Path | None, settings_home: str | Path | None
) -> dict:
    """Read, against disk, whether a session launched here will run hooks.

    Two facts: at least one delivery surface registers hooks (`settings.json`'s
    `hooks` block or the plugin's `hooks/hooks.json`), and the content root resolves
    through at least one rung (registry, `<settings-home>/machine-local/<pointer>`,
    `<claude_home>/<pointer>`). Never raises; returns the eleven-key dict.
    """
    claude_home = Path(claude_home)
    settings_path = claude_home / "settings.json"
    settings: dict = {}
    hook_event_count = 0
    read_error = None
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        settings = loaded if isinstance(loaded, dict) else {}
        hooks = settings.get("hooks") or {}
        hook_event_count = len(hooks) if isinstance(hooks, (dict, list)) else 0
    except Exception as e:  # noqa: BLE001 - an unreadable settings file is a verdict
        read_error = f"{type(e).__name__}: {e}"
    plugin = plugin_hook_delivery(settings, claude_home=claude_home, plugin_root=plugin_root)
    plugin.pop("_delivered", None)
    settings_armed = hook_event_count > 0
    if settings_armed and plugin["armed"]:
        delivery = "both"
    elif settings_armed:
        delivery = "settings"
    elif plugin["armed"]:
        delivery = "plugin"
    else:
        delivery = "none"

    settings_home_str = str(settings_home) if settings_home else ""
    rungs: dict[str, str | None] = {}
    registry_errors: list[str] = []
    if settings_home_str:
        machine_local = Path(settings_home_str) / "machine-local"
        value, errors = read_registry(machine_local, _REGISTRY_KEY)
        rungs[f"registry {_REGISTRY_KEY}"] = value
        registry_errors.extend(e for e in errors if e not in registry_errors)
        rungs[f"{settings_home_str}/machine-local/{_POINTER_NAME}"] = first_line_or_none(
            machine_local / _POINTER_NAME
        )
    else:
        rungs[f"registry {_REGISTRY_KEY}"] = None
        rungs[f"<settings-home>/machine-local/{_POINTER_NAME}"] = None
    home_pointer = claude_home / _POINTER_NAME
    rungs[str(home_pointer)] = first_line_or_none(home_pointer)
    content_root_resolves = any(value for value in rungs.values())

    resolved_content_root = next((value for value in rungs.values() if value), None)
    # Inline `content_root_or_private`: this module is stdlib-only by contract.
    content_root_bin_dir = None
    if resolved_content_root:
        base = Path(resolved_content_root)
        flat = not (base / "coordinator").is_dir() and (base / ".claude-plugin" / "plugin.json").is_file()
        content_root_bin_dir = str((base if flat else base / "coordinator") / "bin")
    content_root_bin_resolves = bool(content_root_bin_dir and Path(content_root_bin_dir).is_dir())

    return {
        "settings_path": str(settings_path),
        "hooks_registered": delivery != "none",
        "hook_delivery": delivery,
        "hook_event_count": hook_event_count,
        "settings_read_error": read_error,
        "plugin_hooks": plugin,
        "content_root_resolves": content_root_resolves,
        "content_root_rungs": rungs,
        "registry_read_errors": registry_errors,
        "content_root_bin_dir": content_root_bin_dir,
        "content_root_bin_resolves": content_root_bin_resolves,
    }


def hook_plane_problems(hook_plane: dict) -> list[str]:
    """The reasons `hook_plane` is not armed; empty when it is."""
    problems: list[str] = []
    if not hook_plane.get("hooks_registered"):
        read_error = hook_plane.get("settings_read_error")
        reason = (hook_plane.get("plugin_hooks") or {}).get("reason", "")
        problems.append(
            f"`hooks` in {hook_plane.get('settings_path')} is empty"
            + (f" ({read_error})" if read_error else "")
            + f" and the coordinator plugin delivers none ({reason})"
        )
    registry_errors = hook_plane.get("registry_read_errors") or []
    if registry_errors:
        problems.append("registry unreadable: " + "; ".join(registry_errors))
    elif not hook_plane.get("content_root_resolves"):
        # An unreadable registry is its own problem above: the registry rungs were
        # never consulted, so "no rung resolves" would misreport the cause.
        problems.append("the content root resolves through no rung the no-launcher fences read")
    return problems


def live_session() -> bool:
    """True inside a cloud boot or a running Claude Code session, the only places
    a listener is owed; a bare install has none and must not read as dark."""
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true" or bool(os.environ.get("CLAUDECODE"))


def forwarder_dark_reason(hook_plane: dict | None) -> str | None:
    """Why a registered `type: http` hook reaches nothing, or None.

    One TCP connect per registered port, 0.25s cap. None when no http hook is
    registered: there is nothing to answer. A snapshot, not a promise: the
    listener can still die after the probe.
    """
    ports = ((hook_plane or {}).get("plugin_hooks") or {}).get("http_ports") or []
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=_PORT_PROBE_TIMEOUT_S):
                pass
        except OSError:
            return f"http hook port {port} accepts no connection"
    return None


def hook_plane_status_line(hook_plane: dict | None, *, extra_failure: str | None = None) -> str:
    """`HOOK PLANE: ARMED|UNARMED (delivery: <surface>)`, the first line of a verdict.

    ARMED needs a registered hook surface, a resolving content root, an answering
    listener on every registered http-hook port (probed only in a live session;
    otherwise ARMED carries a not-probed qualifier), and no `extra_failure`; a
    failure forces UNARMED and is appended after `; `. An absent `hook_plane` reports UNARMED with delivery `unknown`.
    """
    plane = hook_plane or {}
    delivery = plane.get("hook_delivery", "unknown")
    ports = (plane.get("plugin_hooks") or {}).get("http_ports") or []
    qualifier = None
    if not extra_failure and ports:
        if live_session():
            extra_failure = forwarder_dark_reason(hook_plane)
        else:
            qualifier = "forwarder starts at session start, not probed"
    armed = bool(
        hook_plane
        and plane.get("hooks_registered")
        and plane.get("content_root_resolves")
        and not extra_failure
    )
    suffix = f"; {extra_failure or qualifier}" if (extra_failure or qualifier) else ""
    return f"HOOK PLANE: {'ARMED' if armed else 'UNARMED'} (delivery: {delivery}{suffix})"


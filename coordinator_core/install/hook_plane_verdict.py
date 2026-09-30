"""Stdlib-only derivation of the hook-plane verdict shared by both installers.

`scripts/cloud_setup.py` loads this file by path on an interpreter that has no
`coordinator_core` installed, and `scripts/setup.py` imports it as a package
module. It therefore imports the standard library only and does no I/O at import
time. `tomllib` is imported inside `registry_value_or_none` so a 3.10
interpreter loses that one rung instead of the module.

Every function is a read of disk with zero spawns. `write_rule_surface`
duplicates six lines of mechanics kept in cloud_setup.py on purpose: the cloud
verdict must still land when this module failed to load.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

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


def registry_value_or_none(machine_local_dir: Path, key: str) -> str | None:
    """One flat `"<key>" = '<value>'` registry value via tomllib, no subprocess.

    `registry.local.toml` wins over `registry.toml`. Absent, unparseable, or no
    tomllib (<3.11) means this rung says nothing.
    """
    for fname in ("registry.local.toml", "registry.toml"):
        try:
            import tomllib

            data = tomllib.loads((machine_local_dir / fname).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - absent, unreadable, unparseable, or no tomllib
            continue
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
    `hooks` block or the plugin's `hooks/hooks.json`), and `.coordinator-content-root` resolves
    through at least one rung (registry, `<settings-home>/machine-local/.coordinator-content-root`,
    legacy `<claude_home>/.coordinator-content-root`). Never raises; returns the ten-key dict.
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
    if settings_home_str:
        machine_local = Path(settings_home_str) / "machine-local"
        rungs["registry repos.content_root"] = registry_value_or_none(machine_local, "repos.content_root")
        rungs[f"{settings_home_str}/machine-local/.coordinator-content-root"] = first_line_or_none(
            machine_local / ".coordinator-content-root"
        )
    else:
        rungs["registry repos.content_root"] = None
        rungs["<settings-home>/machine-local/.coordinator-content-root"] = None
    legacy = claude_home / ".coordinator-content-root"
    rungs[str(legacy)] = first_line_or_none(legacy)
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
    if not hook_plane.get("content_root_resolves"):
        problems.append("`.coordinator-content-root` resolves through no rung the no-launcher fences read")
    return problems


def hook_plane_status_line(hook_plane: dict | None, *, extra_failure: str | None = None) -> str:
    """`HOOK PLANE: ARMED|UNARMED (delivery: <surface>)`, the first line of a verdict.

    ARMED needs a registered hook surface, a resolving `.coordinator-content-root`, and no
    `extra_failure`; a given `extra_failure` forces UNARMED and is appended
    after `; `. An absent `hook_plane` reports UNARMED with delivery `unknown`.
    """
    plane = hook_plane or {}
    delivery = plane.get("hook_delivery", "unknown")
    armed = bool(
        hook_plane
        and plane.get("hooks_registered")
        and plane.get("content_root_resolves")
        and not extra_failure
    )
    suffix = f"; {extra_failure}" if extra_failure else ""
    return f"HOOK PLANE: {'ARMED' if armed else 'UNARMED'} (delivery: {delivery}{suffix})"


def write_rule_surface(claude_home: Path, basename: str, body: str | None) -> bool:
    """Land (or, for a None body, clear) `<claude_home>/rules/<basename>`.

    Returns whether a file was written.
    """
    rule_path = Path(claude_home) / "rules" / basename
    if body is None:
        rule_path.unlink(missing_ok=True)
        return False
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    rule_path.write_text(body, encoding="utf-8", newline="\n")
    return True

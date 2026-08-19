"""
coordinator_core.hooks.platform_localize -- naked-Python install-time localizer
(DOE-PORT Wave B, id: platform-localize). NOT a live SessionStart hook --
despite this module's name and some historical comment framing elsewhere,
the trampoline's own header is explicit that it is install-time-only (grepped
against `coordinator/hooks/hooks.json` at review time: no SessionStart
registration exists) and FAIL-LOUD, invoked from first-run.sh / install-
maximalist.sh / uninstall_legs.py -- see the trampoline's header for the
current exit-code contract.

Purpose: localize per-machine settings for THIS filesystem on every session
start. Writes/patches three gitignored tri-file-contract artifacts under
CLAUDE_HOME (default `~/.claude`):
  - settings.local.json      -- extraKnownMarketplaces (absolute paths for
                                 this machine) + enabledPlugins gating (a
                                 plugin whose registry.local.toml repo key is
                                 empty or points at a missing dir is disabled)
  - known_marketplaces.json  -- Claude Code's own plugin-discovery cache
                                 (CC reads THIS, not extraKnownMarketplaces --
                                 upstream bug #51806); also self-heals a
                                 clone-bound coordinator-claude directory
                                 source that has gone missing (see
                                 `_self_heal_coordinator_marketplace`).
  - registry.local.toml /
    registry.toml            -- READ ONLY here (per-machine repo paths);
                                 written by `machine-local set`, not by this
                                 hook. Resolved under SETTINGS-HOME
                                 (COORDINATOR_SETTINGS_HOME when set, else
                                 `.coordinator-claude-settings` under CLAUDE_HOME
                                 or, failing that, the platform home directory),
                                 NOT under CLAUDE_HOME -- this module
                                 originally read `<claude_home>/machine-local/
                                 registry.local.toml`, the pre-migration legacy
                                 location, silently missing the real registry
                                 (the same claude-home-vs-settings-home trap
                                 documented as a fixed defect in
                                 `ops/coordinator_setup_state._machine_local_dir`
                                 and `install/check_install_singularity.
                                 _registry_live_path`). Precedence is per-key:
                                 registry.local.toml wins, registry.toml fills
                                 gaps, empty-string values are treated as
                                 not-declared (machine_resolver.registry_get
                                 semantics). The bash oracle this module ports
                                 is retired (no platform-localize.sh remains in
                                 DoE or this repo), so oracle parity no longer
                                 pins the legacy location.

Idempotent: every write compares before touching disk (no churn if already
correct). Unexpected errors are recorded via
`coordinator_core.async_hook_status.record_failure` (mirrors the bash
oracle's `ahs_record_failure` ERR trap) and reported on stderr; `main()`
returns a non-zero-but-non-fatal code and the CALLER (the polyglot
trampoline) propagates it VERBATIM -- the trampoline is fail-loud (install/
config-writer posture, not the never-block posture used for hot-path hooks
like coordinator-auto-push): a non-zero exit here is treated as fatal to the
whole install chain by first-run.sh and install-maximalist.sh's
`run_required`. See the trampoline's own header for its full exit-code
contract (0/1/3).

Port of: platform-localize.sh (DoE 6fb5fb37, 2026-07-22).
Byte-oracle for the JSON/TOML manipulation logic is that script's embedded
Python heredoc -- this module is a 1:1 behavioral port of that
heredoc, restructured into testable functions; the surrounding bash (Python
resolver, async-hook-status ERR trap, path plumbing) is reimplemented in
Python-native terms per DR-059.

Unit decomposition (matches the DOE-PORT brief):
  unit1 -- read_registry_local_toml, repo_available, discover_marketplace_dirs
  unit2 -- settings.local.json read/patch, atomic_write, known_marketplaces.json
           patch + self-heal coordinator-claude registration

Negative-spec (faithfully-reproduced oracle behavior -- do NOT "fix" these):
    - The registry.local.toml parser is a deliberately dumb single-line regex
      matcher (`^"?([^"=]+)"?\\s*=\\s*"([^"]*)"`) that only understands
      `key = "value"` rows. Bare values, arrays, and `[table]` headers are
      silently skipped, exactly as the oracle's own comment says -- this is
      NOT a bug to replace with a real TOML parser (the oracle explicitly
      avoids the Python-3.11 `tomllib` version dependency for a hook that
      "must run everywhere").
    - `external_marketplaces` and `plugin_infra_requirements` are extension
      points the oracle ships EMPTY (no gated plugins declared today) -- kept
      empty here too; do not pre-populate speculative entries.
    - `known_marketplaces.json` self-heal only rewrites a directory-source
      `coordinator-claude` entry when its path is missing AND the plugin
      payload is still cached AND the path is not already under CLAUDE_HOME
      -- conservative-by-design, matching the oracle's own three-way guard.
    - Stale directory-source marketplace entries are pruned only when
      `source.source == "directory"` and the marketplace no longer appears
      on disk; `coordinator-claude` is excluded from blanket pruning (owned
      by the self-heal block instead). URL-sourced (git/github) entries are
      never touched by the prune pass.

Spec: docs/plans/2026-06-30-async-hook-failure-surfacing.md (async-hook
failure-surfacing wiring this module participates in via record_failure).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    from coordinator_core.async_hook_status import record_failure as _ahs_record_failure
except ImportError:  # pragma: no cover - degrade gracefully if module moves
    _ahs_record_failure = None  # type: ignore[assignment]

HOOK_NAME = "platform-localize"

# Extension points -- kept empty to match the oracle 1:1 (see negative-spec).
EXTERNAL_MARKETPLACES: Dict[str, str] = {
    # Example: "my-addon": "repos.my_addon",
}
PLUGIN_INFRA_REQUIREMENTS: Dict[str, str] = {
    # Example: "my-plugin@my-marketplace": "repos.my_repo",
}

COORDINATOR_MP = "coordinator-claude"
COORDINATOR_GITHUB_REPO = "dbc-oduffy/coordinator-claude"

_REGISTRY_LINE_RE = re.compile(r'^"?([^"=]+)"?\s*=\s*"([^"]*)"')

#: Generator-provenance declaration: this install-time localizer patches
#: settings.local.json / known_marketplaces.json under CLAUDE_HOME (the
#: operator's ~/.claude), entirely outside this repo's tracked tree.
GENERATES: list = []


# ---------------------------------------------------------------------------
# unit1 -- registry.local.toml read + marketplace discovery
# ---------------------------------------------------------------------------

def resolve_registry_paths(env: Optional[Dict[str, str]] = None) -> List[str]:
    """Resolve the machine-local registry file paths, precedence-ordered
    (registry.local.toml first, tracked registry.toml second) under
    SETTINGS-HOME. Pure env/home read mirroring
    `coordinator_core._settings_home.machine_local_dir()`'s precedence:
    COORDINATOR_SETTINGS_HOME, falling back to CLAUDE_HOME, falling back to the
    platform home directory (expanduser — USERPROFILE on Windows, HOME or the
    passwd entry on POSIX). Kept inline (not imported) to preserve this module's
    dependency-light hook posture."""
    env = env if env is not None else dict(os.environ)
    settings_home = env.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        env.get("CLAUDE_HOME") or os.path.expanduser("~"), ".coordinator-claude-settings"
    )
    machine_local = os.path.join(settings_home, "machine-local")
    return [
        os.path.join(machine_local, "registry.local.toml"),
        os.path.join(machine_local, "registry.toml"),
    ]


def read_registry(registry_paths: List[str]) -> Dict[str, str]:
    """Per-key precedence merge over the precedence-ordered registry files:
    an earlier file's non-empty value wins; later files fill keys the earlier
    ones left absent or empty (empty-string-is-miss, matching
    `machine_resolver.registry_get`)."""
    merged: Dict[str, str] = {}
    for path in registry_paths:
        for key, val in read_registry_local_toml(path).items():
            if not merged.get(key, ""):
                merged[key] = val
    return merged


def read_registry_local_toml(registry_local_path: str) -> Dict[str, str]:
    """Simple line parser mirroring the oracle: only `key = "value"` rows."""
    registry: Dict[str, str] = {}
    if not os.path.isfile(registry_local_path):
        return registry
    try:
        with open(registry_local_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("["):
                    continue
                m = _REGISTRY_LINE_RE.match(line)
                if m:
                    registry[m.group(1).strip()] = m.group(2).strip()
    except OSError as exc:
        # isfile() above already confirmed the path exists, so a failure
        # here (permission, race deletion, I/O error) is a genuine anomaly
        # rather than the ordinary "no registry yet" case -- worth naming.
        sys.stderr.write(f"[{HOOK_NAME}] registry read failed for {registry_local_path}: {exc}\n")
    return registry


def repo_available(registry: Dict[str, str], key: str) -> bool:
    """Check if a registry repo key points to a directory that exists on disk."""
    val = registry.get(key, "").strip()
    return bool(val and os.path.isdir(val))


def discover_marketplace_dirs(plugins_dir: str, registry: Dict[str, str]) -> Dict[str, str]:
    """Discover marketplace dirs under CLAUDE_HOME/plugins/ plus any
    external ones declared in EXTERNAL_MARKETPLACES via registry.local.toml.
    """
    marketplace_dirs: Dict[str, str] = {}
    if os.path.isdir(plugins_dir):
        for entry in sorted(os.listdir(plugins_dir)):
            if os.path.isdir(os.path.join(plugins_dir, entry, ".claude-plugin")):
                marketplace_dirs[entry] = os.path.join(plugins_dir, entry)

    for mp_name, reg_key in EXTERNAL_MARKETPLACES.items():
        repo_path = registry.get(reg_key, "").strip()
        if repo_path and os.path.isdir(repo_path):
            marketplace_dirs[mp_name] = repo_path

    return marketplace_dirs


# ---------------------------------------------------------------------------
# unit2 -- settings.local.json + known_marketplaces.json patch
# ---------------------------------------------------------------------------

def read_json_file(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def atomic_write(path: str, content: str) -> None:
    """Write content to path atomically via tmp + os.replace.

    Preserves the target's prior permission bits (A5 hardened rule) when a
    file already exists at `path` -- os.replace does not carry mode bits
    from a freshly-`open(...,"w")`-ed temp file, so a bare os.replace would
    silently strip any non-default mode on rewrite.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    if os.path.isfile(path):
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError as exc:
            print(f"WARNING: atomic_write: could not preserve mode bits for {path}: {exc}", file=sys.stderr)
    os.replace(tmp, path)


def build_local_settings(
    marketplace_dirs: Dict[str, str],
    existing_local: Dict[str, Any],
    registry: Dict[str, str],
) -> Dict[str, Any]:
    local_settings: Dict[str, Any] = {}

    extra_mp: Dict[str, Any] = {}
    for mp_name, mp_path in sorted(marketplace_dirs.items()):
        extra_mp[mp_name] = {"source": {"source": "directory", "path": mp_path}}
    if extra_mp:
        local_settings["extraKnownMarketplaces"] = extra_mp

    plugin_overrides: Dict[str, Any] = {}
    for plugin_key, reg_key in PLUGIN_INFRA_REQUIREMENTS.items():
        if not repo_available(registry, reg_key):
            plugin_overrides[plugin_key] = False

    existing_plugins = existing_local.get("enabledPlugins", {})
    for k, v in existing_plugins.items():
        if k not in plugin_overrides:
            plugin_overrides[k] = v

    if plugin_overrides:
        local_settings["enabledPlugins"] = plugin_overrides

    for k, v in existing_local.items():
        if k not in local_settings and k not in ("extraKnownMarketplaces", "enabledPlugins"):
            local_settings[k] = v

    return local_settings


def write_settings_local_if_changed(settings_local_path: str, local_settings: Dict[str, Any]) -> bool:
    """Returns True if a write occurred."""
    new_content = json.dumps(local_settings, indent=2) + "\n"
    existing_content = ""
    if os.path.isfile(settings_local_path):
        try:
            with open(settings_local_path, encoding="utf-8") as f:
                existing_content = f.read()
        except OSError as exc:
            # isfile() above already confirmed the path exists, so this is
            # a genuine anomaly (permission, race deletion, I/O error), not
            # ordinary "no settings.local.json yet" -- existing_content
            # stays "" and the write below self-heals by overwriting, but
            # the anomaly is still worth naming.
            sys.stderr.write(f"[{HOOK_NAME}] settings.local.json read failed for {settings_local_path}: {exc}\n")
    if new_content != existing_content:
        atomic_write(settings_local_path, new_content)
        return True
    return False


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _backfill_last_updated(existing_km: Dict[str, Any], now_iso: str) -> bool:
    changed = False
    for mp_name, entry in list(existing_km.items()):
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("lastUpdated"), str):
            entry["lastUpdated"] = now_iso
            existing_km[mp_name] = entry
            changed = True
    return changed


def _update_discovered_marketplaces(
    existing_km: Dict[str, Any], marketplace_dirs: Dict[str, str], now_iso: str
) -> bool:
    changed = False
    for mp_name, mp_path in marketplace_dirs.items():
        entry = existing_km.get(mp_name, {})
        current_src = entry.get("source", {})
        current_path = current_src.get("path", "") if isinstance(current_src, dict) else ""
        has_last_updated = isinstance(entry.get("lastUpdated"), str)
        if current_path != mp_path or entry.get("installLocation", "") != mp_path or not has_last_updated:
            existing_km[mp_name] = {
                "source": {"source": "directory", "path": mp_path},
                "installLocation": mp_path,
                "lastUpdated": entry.get("lastUpdated") if has_last_updated else now_iso,
            }
            changed = True
    return changed


def self_heal_coordinator_marketplace(
    existing_km: Dict[str, Any],
    plugins_dir: str,
    claude_home: str,
    now_iso: str,
) -> Tuple[bool, Optional[str]]:
    """Rewrite a clone-bound coordinator-claude directory-source entry to the
    public GitHub source when the clone path is gone but the payload is
    still cached. Conservative: leaves an existing/reachable directory
    source and any CLAUDE_HOME-relative source untouched.

    Returns (changed, warning_message_or_None).
    """
    coord_entry = existing_km.get(COORDINATOR_MP)
    if not isinstance(coord_entry, dict):
        return False, None

    coord_src = coord_entry.get("source", {})
    if not (isinstance(coord_src, dict) and coord_src.get("source") == "directory"):
        return False, None

    coord_path = coord_src.get("path", "")
    cache_present = os.path.isdir(os.path.join(plugins_dir, "cache", COORDINATOR_MP))
    claude_home_prefix = os.path.abspath(claude_home) + os.sep
    path_under_claude_home = bool(coord_path) and os.path.abspath(coord_path).startswith(claude_home_prefix)

    if coord_path and not os.path.exists(coord_path) and cache_present and not path_under_claude_home:
        existing_km[COORDINATOR_MP] = {
            "source": {"source": "github", "repo": COORDINATOR_GITHUB_REPO},
            "installLocation": os.path.join(plugins_dir, "marketplaces", COORDINATOR_MP),
            "lastUpdated": now_iso,
        }
        warning = (
            "[platform-localize] repaired coordinator-claude marketplace: "
            "clone-bound directory source '%s' is missing; rewrote to GitHub "
            "source (run /reload-plugins to reload)\n" % coord_path
        )
        return True, warning

    return False, None


def _prune_stale_directory_entries(existing_km: Dict[str, Any], marketplace_dirs: Dict[str, str]) -> bool:
    stale_keys: List[str] = []
    for mp_name, entry in existing_km.items():
        if mp_name == COORDINATOR_MP:
            continue
        if not isinstance(entry, dict):
            continue
        src = entry.get("source", {})
        if isinstance(src, dict) and src.get("source") == "directory":
            if mp_name not in marketplace_dirs:
                stale_keys.append(mp_name)
    for k in stale_keys:
        del existing_km[k]
    return bool(stale_keys)


def patch_known_marketplaces(
    known_mp_path: str,
    marketplace_dirs: Dict[str, str],
    plugins_dir: str,
    claude_home: str,
) -> Tuple[bool, List[str]]:
    """Patch known_marketplaces.json in place on disk. Returns
    (write_occurred, warning_messages)."""
    existing_km = read_json_file(known_mp_path)
    now_iso = _now_iso()
    warnings: List[str] = []

    changed = _update_discovered_marketplaces(existing_km, marketplace_dirs, now_iso)
    changed = _backfill_last_updated(existing_km, now_iso) or changed

    heal_changed, heal_warning = self_heal_coordinator_marketplace(
        existing_km, plugins_dir, claude_home, now_iso
    )
    changed = heal_changed or changed
    if heal_warning:
        warnings.append(heal_warning)

    changed = _prune_stale_directory_entries(existing_km, marketplace_dirs) or changed

    if changed:
        atomic_write(known_mp_path, json.dumps(existing_km, indent=2) + "\n")

    return changed, warnings


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def run(claude_home: str, plugins_dir: str, settings_local_path: str, known_mp_path: str, registry_paths: List[str]) -> None:
    registry = read_registry(registry_paths)
    marketplace_dirs = discover_marketplace_dirs(plugins_dir, registry)

    existing_local = read_json_file(settings_local_path)
    local_settings = build_local_settings(marketplace_dirs, existing_local, registry)
    write_settings_local_if_changed(settings_local_path, local_settings)

    _changed, warnings = patch_known_marketplaces(known_mp_path, marketplace_dirs, plugins_dir, claude_home)
    for w in warnings:
        sys.stderr.write(w)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint. Takes no flags (mirrors the bash oracle, which is a
    plain env-driven install-time localizer with no argument parsing).

    Exit-code contract (business codes; the trampoline's transport-failure
    code 3 is minted at the trampoline level, not here):
        0 -- ran to completion (including the no-op case of nothing to do).
        1 -- an unexpected exception occurred; the failure is recorded via
             async_hook_status.record_failure (mirrors the bash ERR trap)
             and printed to stderr. Unlike a never-block SessionStart hook,
             the caller (`platform-localize.sh`) propagates this code
             VERBATIM and treats it as fatal to the whole install chain
             (first-run.sh, install-maximalist.sh's `run_required`) -- see
             the trampoline's own header for the full fail-loud posture.
    """
    claude_home = os.path.join(
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~"),
        ".claude",
    )
    settings_local_path = os.path.join(claude_home, "settings.local.json")
    known_marketplaces_path = os.path.join(claude_home, "plugins", "known_marketplaces.json")
    plugins_dir = os.path.join(claude_home, "plugins")
    registry_paths = resolve_registry_paths()

    try:
        run(claude_home, plugins_dir, settings_local_path, known_marketplaces_path, registry_paths)
    except Exception as exc:  # noqa: BLE001 - hook must never propagate a raw traceback
        detail = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(f"[{HOOK_NAME}] FAILED: {detail}\n")
        if _ahs_record_failure is not None:
            try:
                _ahs_record_failure(HOOK_NAME, 1, detail, "")
            except Exception:  # noqa: BLE001 - recording the failure must not itself crash the hook
                pass
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

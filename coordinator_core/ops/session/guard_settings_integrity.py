"""
coordinator_core.ops.session.guard_settings_integrity — SessionStart clobber-guard op.

Purpose: detect and auto-recover a clobbered user ``settings.json`` — the
recurring failure mode where ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json``
is silently truncated to a near-empty stub, dropping the entire
``enabledPlugins``/``extraKnownMarketplaces`` blocks and running the session dark
(no coordinator doctrine, no MCP, no domain agents). Direct Python port of the retired
``coordinator/hooks/scripts/guard-settings-integrity.sh`` (deleted 2026-07-16,
DoE ``d39ab164``; naked-Python direct-port disposition, W4a-sessionstart-recipe.md
§ 2.3) — pure JSON read/diff/atomic-copy logic, no bash-specific behavior relied on.

Callers: the DoE thin Shape-P1 stub
(``coordinator/hooks/scripts/guard-settings-integrity.py``) imports
``evaluate_settings_integrity`` DIRECTLY and calls it in-process — same shape as
``preuse-write-dispatch.py`` importing ``evaluate_payload_json`` directly rather
than round-tripping through the async IPC op-registry. The ``@register_op(...)``
async handler below exists for IPC-daemon completeness / test-harness parity; it
is not the hot path the SessionStart hook itself exercises.

Preserved-exactly semantics (do not "clean up" without re-reading the oracle):
  - ``is_healthy``: JSON parses AND ``enabledPlugins`` is a non-empty **dict**.
    A non-object (e.g. a truthy string/int/list) is UNHEALTHY even though it is
    "truthy" by a naive ``bool(...)`` check — mirrors the oracle's jq
    ``objects | length > 0`` filter, which is load-bearing.

Hook-layer reachability (added 2026-07-28, DoE-claude dispatch
``state/subagent-share/78b683cd-1b62-4a25-904d-954cb3c69412/
coordinatorexecutor-8cc51fd5.md``): ``_is_healthy`` used to classify a machine
as healthy solely on ``enabledPlugins`` shape, with zero awareness of whether
the hook layer itself was reachable at all — a machine whose
``settings.json`` carried no ``hooks`` key AND whose plugin-side
``hooks.json`` was unresolvable (broken ``${CLAUDE_PLUGIN_ROOT}``, missing
scripts) still passed as "healthy", and the ``.settings-last-good.json``
snapshot such a machine would write/restore-from was equally hookless.
``_is_healthy`` now ALSO requires ``_hook_layer_reachable(data)``: healthy
iff the hook layer is deliverable via EITHER live path —
  - **plugin-side**: ``hooks.json`` under the resolved coordinator content
    root (``coordinator_core.resolve_coordinator_clone.resolve_content_root``
    — the SAME resolver every other ``${CLAUDE_PLUGIN_ROOT}`` caller uses;
    this module does not add a second one), with every script-shaped
    ``command`` it declares resolving to an existing file, or
  - **settings-side**: ``settings.json``'s own top-level ``hooks`` block
    (the pre-``gen_settings_hooks`` baked-absolute-path delivery form), with
    every script-shaped ``command`` it declares resolving to an existing
    file.
A machine with NO ``hooks`` key in ``settings.json`` is NOT by itself
unhealthy — that is this machine's normal, working plugin-side-only
configuration (see the "Important context" note in the originating
dispatch). Only a machine reachable via NEITHER path is unhealthy under this
addition. Because every existing ``_is_healthy`` call site (top-level
settings check, snapshot-as-restore-source check, git-HEAD-as-restore-source
check) funnels through this one predicate, the same augmentation
transparently fixes both the "write a hookless snapshot as last-known-good"
and "restore from a hookless snapshot" failure modes — no separate gating
code was needed at either call site.
  - ``is_inline_install``: reads ``{config_dir}/.doe-root`` directly (NOT the
    shared ``read-doe-root-pointer.sh``-equivalent helper, because this guard's
    ``CONFIG_DIR`` is caller-supplied and differs from that helper's fixed
    ``${CLAUDE_HOME:-$HOME}/.claude``). Strips only a trailing CR/LF
    (embedded spaces preserved — Windows "OneDrive - Company Name" paths).
  - Precedence: inline-install short-circuit runs BEFORE the restore/recovery
    block, on the UNHEALTHY branch only (healthy branch snapshots-and-returns
    first, unconditionally, before inline-install is even checked).
  - Restore priority (2026-07-31, third-rung addition — see "Restore rungs"
    section docstring below for the full ladder, ordering rationale, and the
    fall-through restructure): (1) ``.settings-last-good.json`` snapshot if
    itself healthy, (2) ``git HEAD`` of ``config_dir`` materialized via
    ``git show HEAD:./settings.json`` if healthy, (3) the newest
    ``settings.json.known-good-<TIMESTAMP>`` backup under the resolved
    settings home (``coordinator_core._settings_home.settings_home()``) that
    passes ``_is_healthy``. All three rungs are now TRUE fall-through: each
    is attempted whenever the ones before it did not actually restore
    (not merely "did not apply" — see below), never short-circuited by an
    ``else`` on a prior rung's precondition.
  - Atomic same-dir write: PID-suffixed temp + ``os.replace`` (atomic on the
    same filesystem, mirroring the bash ``mv -f``).
  - The clobber-backup (``.settings-clobbered.bak``) write is best-effort and
    must NOT gate the restore attempt (``|| true`` semantics in the oracle).
  - All 4 output banners are exact-text golden-diff targets — byte-for-byte,
    including the box-drawing borders.

Intentional divergence from the oracle (flagged, not silently dropped): the
oracle early-skips with a stderr sentinel when ``jq`` is absent from PATH.
Python has native JSON, so this port has no ``jq`` dependency
and therefore no jq-absent skip branch — it runs unconditionally. This is a
genuine behavior IMPROVEMENT (works on machines without jq) but is a parity
DIVERGENCE from the bash oracle, per W4a-sessionstart-recipe.md 2.3 (L168-170).

Spec backlink: DoE repo scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md 2.3
Source: retired coordinator/hooks/scripts/guard-settings-integrity.sh
  (DoE repo, deleted 2026-07-16, DoE ``d39ab164``)
"""

from __future__ import annotations

GENERATES = []  # writes settings.json/.settings-last-good.json/.settings-clobbered.bak/known-good backups under the operator's ~/.claude config dir, never a tracked path in claude-klabauter's own tree

import sys

import asyncio
import filecmp
import json
import datetime as _dt
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.resolve_coordinator_clone import (
    ResolveCoordinatorCloneError,
    resolve_content_root,
)
from coordinator_core._settings_home import machine_local_dir, settings_home
from coordinator_core.ops.session.hook_delivery_manifest import (
    HookDeliveryManifest,
    RetiredGuard,
    read_hook_delivery_manifest,
)

_SNAPSHOT_NAME = ".settings-last-good.json"
_CLOBBER_BAK_NAME = ".settings-clobbered.bak"

# Third restore rung's naming convention (2026-07-31 addition — see the
# "Restore rungs" section docstring below the health/inline-install
# predicates for the full ladder rationale). No existing code globbed
# `known-good` prior to this addition (repo-wide grep confirmed), so this
# module is DEFINING the convention, not reusing one: a file named
# `settings.json.known-good-<TIMESTAMP>` under the resolved settings home,
# where `<TIMESTAMP>` is a sortable `YYYYMMDDTHHMMSS` suffix (the exact shape
# of the real ad-hoc backup that made the 2026-07-31 recovery possible:
# `settings.json.known-good-20260728T211900`). Sortable-string comparison is
# used deliberately over `mtime` — a copied/restored file's mtime reflects
# when it was LAST TOUCHED ON THIS FILESYSTEM, not when the content was
# actually known-good, so mtime would silently pick the wrong candidate on a
# machine where an older backup was copied/touched more recently than a
# newer one.
_KNOWN_GOOD_BACKUP_GLOB = "settings.json.known-good-*"
_KNOWN_GOOD_BACKUP_RE = re.compile(r"^settings\.json\.known-good-(\d{8}T\d{6})$")
_DOEROOT_NAME = ".doe-root"
_INSTALLED_PLUGINS_REL = ("plugins", "installed_plugins.json")
_HOOKS_JSON_REL = ("hooks", "hooks.json")
_SCRIPT_TOKEN_RE = re.compile(r"(\S*\.(?:py|sh|mjs|js))\b")
_TAIL_KEY_RE = re.compile(r"(?:^|/)([^/]+/[^/]+)$")

# Box-drawing banner border width -- single source of truth for the five
# `top`/`bot` construction sites in this module (Review: coordinator:
# code-reviewer, P3). Pure hoist: `_banner_top()`/`_banner_bottom()` produce
# byte-identical output to the prior per-site `"╔" + ("═" * 66) + "╗"`
# literal at every call site -- no rendered banner text changes.
_BANNER_BORDER_WIDTH = 66


def _banner_top() -> str:
    return "╔" + ("═" * _BANNER_BORDER_WIDTH) + "╗"


def _banner_bottom() -> str:
    return "╚" + ("═" * _BANNER_BORDER_WIDTH) + "╝"


# ---------------------------------------------------------------------------
# Health / inline-install predicates
# ---------------------------------------------------------------------------


def _is_healthy(settings_path: Path) -> bool:
    """settings.json parses as JSON, .enabledPlugins is a non-empty object,
    AND the hook layer is reachable via at least one live delivery path.

    Mirrors the oracle's `jq -e '(.enabledPlugins | objects | length) > 0'` —
    the `objects` filter is load-bearing: a non-dict value (string/int/list/
    bool/null) is classified UNHEALTHY even when "truthy" by a naive
    bool()/len() check on the raw value.

    The hook-layer-reachability conjunct (`_hook_layer_reachable`, see its
    own docstring) is the 2026-07-28 addition: a completely hook-dead
    machine can still carry a perfectly intact `enabledPlugins` block, and
    the old enabledPlugins-only check classified it healthy anyway. See the
    module docstring's "Hook-layer reachability" section for the full
    rationale and the failure mode this closes.
    """
    try:
        with settings_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(f"skip: _is_healthy: with settings_path.open(\"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    if not isinstance(data, dict):
        return False
    enabled = data.get("enabledPlugins")
    if not (isinstance(enabled, dict) and len(enabled) > 0):
        return False
    return _hook_layer_reachable(data)


# ---------------------------------------------------------------------------
# Hook-layer reachability lens — distinct from the enabledPlugins-shape check
# above. Asks "is a hook actually going to fire from this configuration on
# THIS machine?", not merely "does a hooks-shaped key exist somewhere".
# ---------------------------------------------------------------------------


def _extract_script_token(command: str) -> Optional[str]:
    """Best-effort extraction of the script-path token from a hook `command`
    string, e.g. `"python3 ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/foo.py --x"`
    yields `"${CLAUDE_PLUGIN_ROOT}/hooks/scripts/foo.py"`. Returns None for a
    command with no recognizable script-file token — not this guard's
    concern; it only asserts reachability of the SCRIPT hooks it can
    identify, never blocks on an inline shell one-liner it can't parse.
    """
    if not isinstance(command, str):
        return None
    match = _SCRIPT_TOKEN_RE.search(command)
    return match.group(1) if match else None


def _resolve_script_token(token: str, content_root: Optional[str]) -> Optional[Path]:
    """Substitute a literal `${CLAUDE_PLUGIN_ROOT}` in `token` with
    `content_root` (when known) and return the resulting Path — or None if
    the token needs substitution but no content root is known (cannot
    resolve, cannot claim reachable)."""
    if "${CLAUDE_PLUGIN_ROOT}" in token:
        if not content_root:
            return None
        token = token.replace("${CLAUDE_PLUGIN_ROOT}", content_root)
    return Path(token)


def _iter_hook_commands(node) -> List[str]:
    """Recursively collect every string value at a `command` key anywhere
    under `node` — tolerant of both the hooks.json wrapper shape
    (`{"hooks": {EVENT: [...]}}`) and settings.json's bare `hooks` value
    (`{EVENT: [...]}`), since both nest `command` at the same relative depth
    (`EVENT -> [entry] -> hooks -> [{"type", "command"}]`) and this walk
    does not care which wrapper it started from."""
    found: List[str] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            cmd = value.get("command")
            if isinstance(cmd, str):
                found.append(cmd)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    return found


def _script_commands_resolve(commands: List[str], content_root: Optional[str]) -> bool:
    """True iff `commands` is non-empty AND every script-shaped command in
    it resolves to an existing file. A command with no recognizable script
    token is skipped (not counted against reachability, but also not
    counted for it) — this guard only asserts the scripts it CAN identify
    actually exist; it does not second-guess a non-script hook command."""
    if not commands:
        return False
    script_tokens = [tok for tok in (_extract_script_token(c) for c in commands) if tok]
    if not script_tokens:
        # Every command in this block is a non-script one-liner: nothing to
        # verify, but the shape is present and non-empty, so there is no
        # negative signal to act on.
        return True
    for token in script_tokens:
        resolved = _resolve_script_token(token, content_root)
        if resolved is None or not resolved.is_file():
            return False
    return True


def _plugin_side_reachable() -> bool:
    """Plugin-declared `hooks.json`, delivered via the resolved coordinator
    content root — reuses `coordinator_core.resolve_coordinator_clone.
    resolve_content_root`, the SAME resolver every other
    `${CLAUDE_PLUGIN_ROOT}` caller in this codebase uses (no competing
    resolver added here). Reachable iff a content root resolves,
    `hooks/hooks.json` exists and parses under it, and every script-shaped
    command it declares resolves to an existing file on THIS machine.
    """
    try:
        content_root = resolve_content_root()
    except ResolveCoordinatorCloneError:
        return False
    hooks_json = Path(content_root, *_HOOKS_JSON_REL)
    if not hooks_json.is_file():
        return False
    try:
        with hooks_json.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return _script_commands_resolve(_iter_hook_commands(data), content_root)


def _settings_side_reachable(settings_data: dict) -> bool:
    """settings.json's own top-level `hooks` block (the pre-
    `gen_settings_hooks` baked-absolute-path delivery form, or any future
    re-adoption of it) — reachable iff it is a non-empty mapping and every
    script-shaped command it declares resolves to an existing file.
    Content-root substitution is attempted best-effort in case a stray
    `${CLAUDE_PLUGIN_ROOT}` token survives baking, but this delivery form is
    normally already-absolute."""
    hooks = settings_data.get("hooks") if isinstance(settings_data, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return False
    try:
        content_root = resolve_content_root()
    except ResolveCoordinatorCloneError:
        content_root = None
    return _script_commands_resolve(_iter_hook_commands(hooks), content_root)


def _hook_layer_reachable(settings_data: dict) -> bool:
    """The augmented reachability predicate: healthy iff the hook layer is
    deliverable by EITHER live path — plugin-side (`hooks.json` resolvable
    via the coordinator content-root resolver) or settings-side
    (`settings.json`'s own `hooks` block, with every referenced script
    resolvable) — never merely "a hooks-shaped key exists somewhere". A
    `settings.json` with NO `hooks` key at all is NOT by itself unhealthy —
    plugin-side-only delivery with a hookless settings.json is this
    machine's normal, currently-working configuration (see module
    docstring's "Hook-layer reachability" section). Only a machine
    unreachable via BOTH paths is unhealthy under this predicate.
    """
    return _plugin_side_reachable() or _settings_side_reachable(settings_data)


def is_inline_install(config_dir: Path) -> bool:
    """A DoE `--plugin-dir` install has no `enabledPlugins` legitimately.

    Reads the `.doe-root` pointer off one of two rungs, migrated rung
    first:

      1. `<settings-home>/machine-local/.doe-root`
         (`coordinator_core._settings_home.machine_local_dir()`) — the
         canonical home since the 2026-08-01 migration off `{config_dir}/
         .doe-root`, which was machine-local state living in a git-tracked
         cross-machine repo (a Mac writing `/Users/...` and a Windows box
         writing `X:\\...` committing over each other for weeks). Consulted
         ONLY when `_settings_home_scoped_to(config_dir, settings_home())`
         is True — i.e. `config_dir` is a direct sibling of the resolved
         settings home, the shape `settings_home()`'s own default
         construction produces. This function's `config_dir` is
         CALLER-supplied and can legitimately differ from the machine's
         real settings home (a test's `tmp_path`, a non-default install
         layout); consulting the migrated rung unconditionally would make
         the answer for an arbitrary `config_dir` partly determined by
         ambient machine state — the exact scope-escape hazard this
         module's rung-3 known-good-backup lookup was fixed for on
         2026-08-01 (see `_settings_home_scoped_to` and the "Restore
         rungs" section's 2026-08-01 scope-escape note). When not scoped,
         the migrated rung is treated as absent, not consulted at all.
      2. `{config_dir}/.doe-root` (legacy) — read only when the migrated
         rung's file is not present (regardless of the migrated rung's
         content: an empty/blank migrated pointer FILE still suppresses
         this rung, it does not fall through).

    Strips ONLY a trailing CR/LF from whichever rung answers — NOT a
    blanket whitespace strip, which would clobber embedded spaces in a
    Windows path like "C:\\Users\\me\\OneDrive - Company Name\\DoE-claude".

    Public because it is a cross-module seam, not an internal helper:
    `guard_hook_generation_self_probe` imports it at module scope to
    discriminate "plugin-only, content root legitimately absent" from
    "content root genuinely broken". That import runs on the SessionStart
    path, OUTSIDE the probe's fail-open try/except, so a rename here would
    surface as an ImportError at session boot rather than as silence — the
    public name is what pins that contract for a future renamer.

    Negative spec: this is a LIVE existence probe, not a pointer read. A
    stale `.doe-root` naming a directory that no longer exists returns
    False, which is what keeps the probe's true-positive (destroyed clone)
    detection intact — on EITHER rung.

    Spec backlink: DR-117 (DoE-claude, maintainer signals may classify,
    never diagnose) — this predicate CLASSIFIES an install shape
    (`.doe-root` present and live); the caller's job, not this function's,
    is to never read its `False` branch as a health verdict.
    """
    doeroot_file = config_dir / _DOEROOT_NAME
    try:
        home = settings_home()
    except Exception:
        home = None
    if home is not None and _settings_home_scoped_to(config_dir, home):
        # Review: coordinator:code-reviewer (P3) — build the migrated candidate
        # from the already-bound `home` rather than calling machine_local_dir(),
        # which internally re-resolves settings_home() a second time for the
        # same path; on this SessionStart boot path resolution cost is a
        # first-order concern (see module docstring).
        migrated_candidate = home / "machine-local" / _DOEROOT_NAME
        if migrated_candidate.is_file():
            doeroot_file = migrated_candidate

    if not doeroot_file.is_file():
        return False
    try:
        with doeroot_file.open("r", encoding="utf-8", newline="") as fh:
            first_line = fh.readline()
    except OSError:
        print(f"skip: is_inline_install: with doeroot_file.open(\"r\", encoding=\"utf-8\", newline=\"\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    doe = first_line.rstrip("\r\n")
    if not doe:
        return False
    return (Path(doe) / "coordinator").is_dir()


# ---------------------------------------------------------------------------
# Reconciliation lens: declared-true-but-unreachable plugins
# ---------------------------------------------------------------------------
#
# Distinct from the clobber lens above. The clobber lens asks "did the
# enabledPlugins block survive intact?" — it cannot detect a block that
# survived intact while describing plugins that were never actually
# installed (the 2026-07-28 example-game-repo/example-game-repo-control/game-dev incident:
# all three read `true` in settings.json for days while loading nothing —
# no commands, skills, agents, or SessionStart hooks — because MCP tools
# kept working via the separate `.mcp.json` path and camouflaged the gap).
# This lens asks "is each declared-true key actually reachable?"


def _load_json_dict(path: Path) -> Optional[dict]:
    """Parse `path` as a JSON object; return None on any read/parse failure
    or if the top-level value is not a dict. Never raises."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_installed_plugin_records(config_dir: Path) -> Optional[dict]:
    """Return the raw `plugins` mapping (key -> list of install records) from
    `<config_dir>/plugins/installed_plugins.json` — the harness's own
    installed-plugins registry, e.g. ``{"coordinator@coordinator-claude":
    [{"installPath": "...", "scope": "user", ...}]}``.

    Returns None when the file is missing, unreadable, or malformed —
    callers MUST treat None as "cannot verify" and stay silent (fail-open
    is mandatory here: a false "your plugin is broken"/"kill switch armed"
    banner every boot is worse than the bug it detects).

    Public: a deliberate cross-module seam.
    `guard_hook_generation_self_probe`'s marketplace/OSS-install carve-out
    imports this at MODULE SCOPE (not inside its own fail-open try/except),
    mirroring `is_inline_install`'s contract — a rename here would surface
    as an ImportError at SessionStart boot rather than as silence, so the
    public name is what pins that contract for a future renamer.

    Negative spec: this returns the record AS-IS, unvalidated beyond JSON
    shape — it does NOT stat `installPath`, does NOT check
    `enabledPlugins`, and does NOT confirm the plugin is actually reachable
    on disk. A caller that treats a non-None return alone as "plugin is
    live" has reintroduced the exact false-negative this reader's callers
    exist to avoid (the record persists on disk long after the tree it
    points at is destroyed) — every caller MUST additionally stat the
    recorded `installPath` before trusting it.
    """
    path = config_dir.joinpath(*_INSTALLED_PLUGINS_REL)
    data = _load_json_dict(path)
    if data is None:
        return None
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return None
    return plugins


def _read_installed_plugin_keys(config_dir: Path) -> Optional[set]:
    """Return the set of plugin keys with a non-empty install record in
    `<config_dir>/plugins/installed_plugins.json`.

    Returns None when the file is missing, unreadable, or malformed —
    callers MUST treat None as "cannot verify" and stay silent (fail-open
    is mandatory here: a false "your plugin is broken" banner every boot
    is worse than the bug it detects).
    """
    plugins = read_installed_plugin_records(config_dir)
    if plugins is None:
        return None
    return {
        key
        for key, records in plugins.items()
        if isinstance(records, list) and len(records) > 0
    }


def _reconcile_enabled_plugins(settings_data: dict, config_dir: Path) -> list[str]:
    """Return the sorted list of declared-true `enabledPlugins` keys that are
    NOT actually reachable (no installed_plugins.json record, and this is not
    a dev-source inline install).

    Keys set to `false` are opted out deliberately and are never flagged.
    Fail-open at every uncertain step: an unreadable/malformed
    installed_plugins.json, or an inline (`--plugin-dir`) dev install, both
    return `[]` (silent) rather than guessing.
    """
    enabled = settings_data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    declared_true = [key for key, value in enabled.items() if value is True]
    if not declared_true:
        return []

    # A DoE inline (--plugin-dir) install legitimately serves its plugin live
    # from a dev-source checkout with no marketplace/installed_plugins.json
    # registration — same carve-out precedent as the clobber lens above.
    # There is no per-key way to tell "this key IS the inline plugin" apart
    # from "this key is unrelated and genuinely broken" from here, so on an
    # inline-install machine the whole reconciliation lens stays silent
    # (fail-open over a false positive).
    if is_inline_install(config_dir):
        return []

    installed_keys = _read_installed_plugin_keys(config_dir)
    if installed_keys is None:
        return []

    return sorted(key for key in declared_true if key not in installed_keys)


_BANNER_UNREACHABLE_HEADER = (
    "║  ⚠  Plugin(s) declared enabled but NOT actually loading:"
)


def _marketplace_dirs(settings_data: dict) -> dict:
    """Map marketplace name -> declared directory, from `extraKnownMarketplaces`.

    Best-effort and shape-tolerant: any entry that is not a directory-source
    with a string path is simply absent from the result, and the banner falls
    back to a placeholder for that marketplace. Never raises — this feeds a
    fail-open guard.
    """
    out: dict = {}
    extra = settings_data.get("extraKnownMarketplaces")
    if not isinstance(extra, dict):
        return out
    for name, entry in extra.items():
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if not isinstance(source, dict):
            continue
        path = source.get("path")
        if isinstance(path, str) and path:
            out[name] = path
    return out


def _banner_unreachable_plugins(keys: list[str], settings_data: dict | None = None) -> str:
    top = _banner_top()
    bot = _banner_bottom()
    lines = ["", top, _BANNER_UNREACHABLE_HEADER, "║"]
    for key in keys:
        lines.append(f"║      {key}")
    lines.append("║")
    lines.append("║  Each is `true` in settings.json's `enabledPlugins` but has no")
    lines.append("║  installed_plugins.json record — no commands, skills, agents, or")
    lines.append("║  hooks from it are running this session. A separate MCP-server")
    lines.append("║  connection (.mcp.json) can keep working and mask this gap.")
    lines.append("║")
    # One `marketplace add` per DISTINCT marketplace, resolved to its declared
    # directory where settings.json names one — repeating an unresolved <dir>
    # placeholder per plugin reads as boilerplate, and a banner that looks like
    # boilerplate is one operators learn to skim past.
    dirs = _marketplace_dirs(settings_data or {})
    seen_marketplaces: list[str] = []
    for key in keys:
        marketplace = key.rsplit("@", 1)[-1] if "@" in key else ""
        if marketplace and marketplace not in seen_marketplaces:
            seen_marketplaces.append(marketplace)
    lines.append("║  Action: register each marketplace (once), then install each")
    lines.append("║  plugin:")
    for marketplace in seen_marketplaces:
        target = dirs.get(marketplace) or f"<{marketplace} checkout dir>"
        lines.append(f"║      claude plugin marketplace add {target}")
    for key in keys:
        lines.append(f"║      claude plugin install {key}")
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hook-delivery duplication detector (DETECT-ONLY) -- a different question
# from `_hook_layer_reachable` above. That predicate asks "is the hook layer
# reachable via AT LEAST ONE path" (healthy iff true, either path suffices).
# This one asks "is it reachable via BOTH paths for the SAME hooks", which
# means every one of those hooks fires TWICE per session -- a silent 2x
# process-spawn tax, worst on the platform (Windows) where spawn is most
# expensive.
#
# Root cause: `gen_settings_hooks.py` reads `coordinator/hooks/hooks.json` as
# its sole input and writes a filtered rewrite into settings.json's own
# `hooks` block, baking each `${CLAUDE_PLUGIN_ROOT}` token to an absolute
# path. All 44 entries in hooks.json carry `${CLAUDE_PLUGIN_ROOT}`, so the
# filter is effectively identity -- everything the settings-side write can
# emit, the plugin-side path (`_plugin_side_reachable` above) ALREADY
# delivers. The two resulting command strings are textually distinct post-
# bake (`${CLAUDE_PLUGIN_ROOT}/hooks/scripts/foo.py` vs an already-baked
# `/Users/x/.claude/.../hooks/scripts/foo.py`), so the harness has no way to
# dedupe them itself -- if both surfaces are live, both fire.
#
# DETECT-ONLY, same discipline as `guard_foreign_platform_paths.py` (this
# module's sibling): `settings.json` is shared/synced across machines, and a
# same-host "fix" -- deleting the `hooks` key, arming/disarming the
# `.coordinator-hooks-disabled` kill-switch -- can silently strand a peer
# machine mid-sync in the opposite direction. This detector reports; it
# NEVER writes to `settings.json`, NEVER touches the kill-switch, and NEVER
# regenerates anything. A human/PM call is required to pick which surface to
# retire on which machine.
#
# Spec backlink: DoE-claude dispatch state/subagent-share/
# 78b683cd-1b62-4a25-904d-954cb3c69412/coordinatorexecutor-8166967b.md
# (2026-07-28).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResurrectedDecision:
    """A settings.json-side entry naming a guard the manifest marks
    deliberately `retired` -- distinct from both an ordinary duplicate and
    an ordinary settings-only entry, because the settings.json config
    predates the retirement ruling and silently outranks it. `script` is
    the resolved absolute path when one is available, else the raw tail
    key (same display-fallback convention as `duplicated_scripts`/
    `settings_only_scripts`)."""

    id: str
    script: str
    reason: str


@dataclass(frozen=True)
class HookDeliveryReport:
    """Per-surface reachability + entry counts for the two hook-delivery
    paths, plus the resolved-script overlap between them (the actual
    double-fire signal -- two live surfaces with disjoint entry sets is NOT
    double-fire, see `double_fire` below). `settings_only_scripts` is a
    separate hazard signal: resolved script paths declared on the
    settings.json surface ONLY -- the scripts that would be silently lost
    (never erroring, never logged) if the `hooks` key were deleted on the
    assumption the two surfaces are fully redundant.

    `manifest_state`/`manifest_detail`/`manifest_unaccounted` carry the
    hook-delivery manifest's (C1/C2) read result for this boot --
    `"absent"` when no manifest was consulted at all (e.g. the plugin side
    is not present, or `hooks.json` carries no `x-effective-delivery`
    block), mirroring `HookDeliveryManifest.state`'s five-state contract.
    `resurrected_decisions` is the third comparator outcome (AC6),
    distinct from `duplicated_scripts` and `settings_only_scripts`."""

    plugin_present: bool
    plugin_resolvable: bool
    plugin_entry_count: int
    settings_present: bool
    settings_resolvable: bool
    settings_entry_count: int
    duplicated_scripts: List[str]
    settings_only_scripts: List[str] = field(default_factory=list)
    content_root_error: Optional[str] = None
    manifest_state: str = "absent"
    manifest_detail: str = ""
    manifest_unaccounted: Tuple[str, ...] = ()
    resurrected_decisions: Tuple[ResurrectedDecision, ...] = ()

    @property
    def degraded(self) -> bool:
        """True iff BOTH delivery surfaces are live AND the hook-delivery
        manifest is not in state `ok` -- mirrors `indeterminate`'s exact
        discriminator shape (`content_root_error is not None and
        settings_present`), reusing the same "both surfaces must be live
        or the verdict is noise" posture rather than inventing a second
        one. With only one surface live there is nothing that can
        double-fire, so a degraded banner there is pure noise on every
        machine in the fleet that has not taken an emitter yet; with both
        live and no usable (`ok`) manifest, the check genuinely cannot
        answer whether a script delivered behind a fan-in carrier
        collides with a settings-side entry, and staying silent there is
        the exact false-green conflation this property exists to kill."""
        return (
            self.plugin_present
            and self.settings_present
            and self.manifest_state != "ok"
        )

    @property
    def double_fire(self) -> bool:
        """True iff both delivery surfaces are live (present + resolvable)
        AND at least one script-shaped hook command resolves to the SAME
        script (see `duplicated_scripts` -- matched by root-independent tail
        key, not exact absolute path alone; see `_tail_key`'s docstring for
        why exact-path-only under-detects). Two live surfaces with zero
        actual command overlap (e.g. a partial migration) are NOT
        double-fire -- report honestly rather than assuming full
        duplication from mere presence. Never True while `indeterminate`
        is True (`indeterminate` implies `plugin_present` is False)."""
        return (
            self.plugin_present
            and self.plugin_resolvable
            and self.settings_present
            and self.settings_resolvable
            and len(self.duplicated_scripts) > 0
        )

    @property
    def indeterminate(self) -> bool:
        """True iff the coordinator content root could not be resolved at
        all (`content_root_error` set) while settings.json still declares a
        `hooks` block -- in this shape the plugin-side hooks.json could not
        even be LOCATED (its path is `<content_root>/hooks/hooks.json`), so
        a `plugin_present=False` / zero-overlap result here is a resolution
        FAILURE, not evidence the two surfaces are actually disjoint.
        `format_hook_delivery_banner` must render an explicit indeterminate
        banner in this case, never the confident "nothing is firing twice
        today" disjoint verdict nor silence -- see this predicate's spec
        backlink for the confirmed incident this closes: a SessionStart call
        and an interactive re-run resolved the coordinator content root to
        two different (each individually valid) rungs of
        `resolve_content_root`'s ladder, and the resulting false "TWO
        HOOK-DELIVERY SURFACES...DECLARING DIFFERENT SCRIPTS" banner hid a
        real 22-script double-fire. `duplicated_scripts`'s tail-key matching
        (see `_tail_key`) independently closes the "two different but BOTH
        resolvable roots" half of that incident; this predicate closes the
        "root does not resolve at all" half, which tail-key matching cannot
        by itself fix (an unresolvable root means plugin-side hooks.json
        can't be read, so there are no plugin-side commands to match
        against at any granularity)."""
        return self.content_root_error is not None and self.settings_present


def _tail_key(token: str) -> Optional[str]:
    """Root-independent join key for a script-shaped hook command token --
    its last two path segments (e.g. `scripts/foo.py`, lower-cased),
    taken from the RAW token text BEFORE any `${CLAUDE_PLUGIN_ROOT}`
    substitution or filesystem resolution.

    Review: coordinator:code-reviewer (P3) -- the `scripts/foo.py` example
    above corrects a stale three-segment example (`hooks/scripts/foo.py`)
    that never matched `_TAIL_KEY_RE`'s actual two-segment capture. Doc-only
    fix: the join width (last two segments) did not change.

    Confirmed defect this closes (see `HookDeliveryReport.indeterminate`'s
    docstring for the sibling half): exact absolute-path comparison alone
    (the pre-fix `_resolved_script_paths`-only approach) reported ZERO
    overlap for a live incident with a real 22-script double-fire, because
    the plugin-side and settings-side resolutions had landed on two
    different (each individually valid) rungs of `resolve_content_root`'s
    ladder -- e.g. a dev registry `live_path` clone vs. a `CLAUDE_PLUGIN_ROOT`
    the harness sets at SessionStart pointing at a distinct, equally-live
    mirror of the SAME hooks tree. Both sides enumerate the same script
    NAMES under the same `hooks/scripts/` layout even when their absolute
    roots differ, so the tail is the invariant this detector actually needs
    to compare on; the absolute path remains useful only for DISPLAY once a
    match is found (a resolvable absolute path reads better in the banner
    than a raw tail string).

    Returns None for a token with no recognizable two-segment tail (should
    not happen for anything `_extract_script_token` matched, but never
    guessed at -- same "identify what we can, never guess" posture as
    every other helper in this section)."""
    normalized = token.replace("\\", "/")
    match = _TAIL_KEY_RE.search(normalized)
    return match.group(1).lower() if match else None


def _tail_keys_map(commands: List[str], content_root: Optional[str]) -> dict:
    """tail-key -> set of resolved absolute paths (when resolvable) for
    every script-shaped command in `commands`. Pairs the root-independent
    grouping key (`_tail_key`) with whatever absolute resolution happens to
    be available, so a match found via the tail fallback can still be
    reported with a real path in the banner rather than the bare tail
    string. A key maps to an empty set (never absent) when no command
    sharing that tail resolved to an absolute path -- callers fall back to
    the tail string itself for display in that case."""
    out: dict = {}
    for command in commands:
        token = _extract_script_token(command)
        if token is None:
            continue
        key = _tail_key(token)
        if key is None:
            continue
        bucket = out.setdefault(key, set())
        resolved = _resolve_script_token(token, content_root)
        if resolved is not None:
            try:
                bucket.add(str(resolved.resolve()))
            except OSError:
                bucket.add(str(resolved))
    return out


def detect_hook_delivery_duplication(config_dir: Optional[Path] = None) -> HookDeliveryReport:
    """Inspect both hook-delivery surfaces on THIS machine: is each present
    and resolvable, how many entries does each carry, and how many entries
    are actually duplicated across both (by resolved script path, not raw
    command text -- see module section docstring for why raw text can't be
    compared). Never raises -- any unresolvable path degrades to
    `present=False`/`resolvable=False` for that surface, never a crash (this
    must be safe to run at every session start). Reuses
    `resolve_content_root` (the same `${CLAUDE_PLUGIN_ROOT}` resolver every
    other caller in this codebase uses) rather than adding a second one.
    """
    if config_dir is None:
        raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        config_dir = Path(raw)

    content_root_error: Optional[str] = None
    try:
        content_root = resolve_content_root()
    except ResolveCoordinatorCloneError as exc:
        content_root = None
        content_root_error = str(exc)

    # --- plugin side: coordinator/hooks/hooks.json under the resolved
    # content root, same resolver `_plugin_side_reachable` uses above. ---
    plugin_present = False
    plugin_commands: List[str] = []
    plugin_data: Optional[dict] = None
    if content_root is not None:
        hooks_json = Path(content_root, *_HOOKS_JSON_REL)
        if hooks_json.is_file():
            plugin_present = True
            try:
                with hooks_json.open("r", encoding="utf-8") as fh:
                    plugin_data = json.load(fh)
            except (OSError, ValueError):
                plugin_data = None
            if plugin_data is not None:
                plugin_commands = _iter_hook_commands(plugin_data)
    plugin_resolvable = plugin_present and _script_commands_resolve(
        plugin_commands, content_root
    )

    # --- settings side: settings.json's own top-level `hooks` block (the
    # `gen_settings_hooks.py` baked-absolute-path delivery form). ---
    settings_present = False
    settings_commands: List[str] = []
    settings_path = config_dir / "settings.json"
    if settings_path.is_file():
        settings_data = _load_json_dict(settings_path)
        if settings_data is not None:
            hooks_block = settings_data.get("hooks")
            if isinstance(hooks_block, dict) and hooks_block:
                settings_present = True
                settings_commands = _iter_hook_commands(hooks_block)
    settings_resolvable = settings_present and _script_commands_resolve(
        settings_commands, content_root
    )

    # Hook-delivery manifest (C1/C2): read the `x-effective-delivery` block
    # out of the ALREADY-PARSED `plugin_data` -- no second resolve, no
    # second file read. `declared_script_keys` is the plugin-side's own
    # script-shaped commands, tail-key normalized, so the manifest's
    # exhaustiveness/`stale` check (C1) sees exactly what `hooks.json`
    # itself declares.
    declared_script_keys: List[str] = []
    for command in plugin_commands:
        token = _extract_script_token(command)
        if token is None:
            continue
        key = _tail_key(token)
        if key is not None:
            declared_script_keys.append(key)
    manifest: HookDeliveryManifest = read_hook_delivery_manifest(
        plugin_data, declared_script_keys
    )

    # Overlap is computed on the root-independent tail key (`_tail_key`),
    # not exact absolute path alone -- see `_tail_key`'s docstring for the
    # confirmed incident this closes: two individually-valid but distinct
    # content-root resolutions make exact-path comparison under-detect real
    # duplication. An exact-path match is a subset of a tail-key match (same
    # absolute path implies same tail), so this is a strict widening, not a
    # behavior change for the common (single content root) case.
    #
    # Join step (C3, Review: staff-eng finding 0): with the manifest in
    # state `ok`, BOTH sides' tail keys are further normalized into an
    # EFFECTIVE GUARD SET before differencing -- a carrier tail key expands
    # to its declared guard ids, a non-carrier tail key that matches a
    # guard's `script` field (the manifest's `script_index`, probed for
    # BOTH sides, not settings-only) resolves to that guard's id, and
    # anything else is identity. AC4 pins state != `ok` (including
    # `absent`) to identity-only -- byte-identical to the pre-manifest
    # filename comparison.
    duplicated: List[str] = []
    settings_only: List[str] = []
    resurrected: List[ResurrectedDecision] = []
    if plugin_present and settings_present:
        plugin_tail_map = _tail_keys_map(plugin_commands, content_root)
        settings_tail_map = _tail_keys_map(settings_commands, content_root)

        retired_by_script: Dict[str, RetiredGuard] = {
            r.script: r for r in manifest.retired
        }
        # Resurrected-decision entries (AC6): a settings-side tail key that
        # names a guard the manifest marks `retired` -- distinct from both
        # a duplicate and a settings-only entry, pulled out of the
        # settings-side map BEFORE the duplicate/settings-only difference
        # below so it is not double-counted into either.
        resurrected_settings_keys: Set[str] = set()
        for key, paths in settings_tail_map.items():
            retired_guard = retired_by_script.get(key)
            if retired_guard is None:
                continue
            resurrected_settings_keys.add(key)
            display = sorted(paths)[0] if paths else key
            resurrected.append(
                ResurrectedDecision(
                    id=retired_guard.id, script=display, reason=retired_guard.reason
                )
            )
        settings_tail_map = {
            key: paths
            for key, paths in settings_tail_map.items()
            if key not in resurrected_settings_keys
        }

        def _expand_guard_map(tail_map: Dict[str, set]) -> Dict[str, set]:
            expanded: Dict[str, set] = {}
            for tail_key, paths in tail_map.items():
                if manifest.state == "ok" and tail_key in manifest.carriers:
                    guard_ids = {g.id for g in manifest.carriers[tail_key]}
                elif manifest.state == "ok" and tail_key in manifest.script_index:
                    guard_ids = {manifest.script_index[tail_key]}
                else:
                    guard_ids = {tail_key}
                for guard_id in guard_ids:
                    bucket = expanded.setdefault(guard_id, set())
                    bucket.update(paths)
            return expanded

        plugin_guard_map = _expand_guard_map(plugin_tail_map)
        settings_guard_map = _expand_guard_map(settings_tail_map)
        overlap_keys = set(plugin_guard_map) & set(settings_guard_map)

        duplicated_set: set = set()
        for key in overlap_keys:
            paths = settings_guard_map[key] or plugin_guard_map[key]
            duplicated_set.update(paths or {key})
        duplicated = sorted(duplicated_set)

        settings_only_set: set = set()
        for key, paths in settings_guard_map.items():
            if key in overlap_keys:
                continue
            settings_only_set.update(paths or {key})
        settings_only = sorted(settings_only_set)

    return HookDeliveryReport(
        plugin_present=plugin_present,
        plugin_resolvable=plugin_resolvable,
        plugin_entry_count=len(plugin_commands),
        settings_present=settings_present,
        settings_resolvable=settings_resolvable,
        settings_entry_count=len(settings_commands),
        duplicated_scripts=duplicated,
        settings_only_scripts=settings_only,
        content_root_error=content_root_error,
        manifest_state=manifest.state,
        manifest_detail=manifest.detail,
        manifest_unaccounted=manifest.unaccounted,
        resurrected_decisions=tuple(resurrected),
    )


def _standalone_degraded_banner(report: HookDeliveryReport) -> str:
    """Rendered only when BOTH surfaces are live, the manifest is not `ok`
    (`report.degraded`), and there is otherwise nothing else to say
    (no duplicate/settings-only/resurrected finding) -- the exact shape
    that would previously have returned `""`, which is the false-green
    conflation AC4 exists to kill. `stale` gets its own, more specific
    rendering naming the unaccounted-for command (C1's exhaustiveness
    contract); every other non-`ok` state (`absent`, `malformed`,
    `version-unsupported`) shares the generic degraded rendering."""
    top = _banner_top()
    bot = _banner_bottom()
    if report.manifest_state == "stale":
        unaccounted = (
            report.manifest_unaccounted[0]
            if report.manifest_unaccounted
            else "(unknown)"
        )
        return "\n".join(
            [
                "",
                top,
                "║  ?  HOOK-DELIVERY MANIFEST IS STALE",
                "║",
                f"║  hooks.json declares {unaccounted!r}, which the delivery",
                "║  manifest does not account for as a carrier, direct, or retired",
                "║  entry -- this check's verdict is not confident in either",
                "║  direction.",
                "║",
                "║  This is DETECT-ONLY -- nothing was changed.",
                bot,
                "",
            ]
        )
    return "\n".join(
        [
            "",
            top,
            "║  ?  HOOK-DELIVERY DUPLICATION CHECK IS DEGRADED (no usable manifest)",
            "║",
            "║  Both hook-delivery surfaces are live on this machine, but no",
            "║  usable delivery manifest is present -- this check compared",
            "║  filenames only and cannot see guards delivered behind a fan-in",
            "║  carrier, so a clean result here is not evidence nothing fires",
            "║  twice.",
            "║",
            "║  This is DETECT-ONLY -- nothing was changed.",
            bot,
            "",
        ]
    )


def _resurrected_decisions_banner(report: HookDeliveryReport) -> str:
    """Rendered STANDALONE when `report.resurrected_decisions` is non-empty
    and neither `double_fire` nor `settings_only_scripts` also fires (see
    `format_hook_delivery_banner`'s early-return branch). When a resurrected
    finding co-occurs with `double_fire`/`settings_only_scripts`, it is
    rendered ADDITIVELY inside `format_hook_delivery_banner`'s own pinned
    body instead of via this function (EM decision, C4 item 3) -- a
    resurrected decision is worse than an ordinary duplicate or
    settings-only entry (a config frozen before a retirement ruling
    silently outranks the ruling), so it must not be suppressed behind a
    less severe finding dominating the banner. Distinct from both a
    duplicate and a settings-only finding: names each resurrected entry,
    quotes the manifest's (already-sanitized, per C1/C2) reason, and states
    plainly that the action is removing that specific stale entry -- NOT
    the kill-switch, since these guards were deliberately retired, not
    accidentally duplicated."""
    top = _banner_top()
    bot = _banner_bottom()
    decisions = report.resurrected_decisions
    k = len(decisions)
    noun = "entry" if k == 1 else "entries"
    lines = [
        "",
        top,
        f"║  ⛔ {k} settings.json {noun} name a guard the manifest marks RETIRED",
        "║",
        "║  A settings.json config frozen before a retirement ruling silently",
        "║  outranks it -- each entry below is running a guard that was",
        "║  deliberately stood down, not accidentally duplicated:",
        "║",
    ]
    for decision in decisions:
        lines.append(f"║      {decision.script}  (id: {decision.id})")
        lines.append(f"║          reason: {decision.reason}")
    lines.append("║")
    lines.append("║  This is DETECT-ONLY -- nothing was changed.")
    lines.append(
        "║  Action: remove that specific stale settings.json entry (NOT the"
    )
    lines.append(
        "║  kill-switch -- these guards were deliberately retired, not"
    )
    lines.append("║  accidentally duplicated).")
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


def format_hook_delivery_banner(report: HookDeliveryReport) -> str:
    """Render the hook-delivery banner -- empty string (silent) unless
    `report.double_fire` is True, `report.settings_only_scripts` is
    non-empty, `report.resurrected_decisions` is non-empty, or
    `report.degraded` is True (the false-green-preventing standalone
    degraded/stale rendering, see `_standalone_degraded_banner`). Leads
    with the actionable conclusion, not a data dump, and names the exact
    remediation rather than merely the condition (dispatch brief
    requirements #2 and #3).

    Review: code-reviewer (Finding 1) -- the settings-only danger section
    used to be nested entirely inside the `double_fire`-only early return,
    so it never rendered in the disjoint case it exists to catch: both
    delivery surfaces live, ZERO script overlap, but settings.json still
    declares scripts hooks.json doesn't. That case's lead line cannot be
    "HOOKS ARE FIRING TWICE" (false -- nothing overlaps), so it gets its own
    lead line describing what IS true. The `double_fire` PREDICATE itself is
    untouched -- it still means "overlap exists" and nothing here changes
    that; this is a rendering fix, not a semantics change. When both
    conditions hold (overlap AND settings-only scripts), today's behaviour
    -- the double-fire lead line plus the danger section -- is unchanged.

    EM decision (C4, item 3): a resurrected-decision finding renders
    ADDITIVELY when it co-occurs with `double_fire`/`settings_only_scripts`
    -- appended after the existing pinned body, never replacing it -- rather
    than being suppressed behind the less-severe finding the way C3
    originally scoped it. Resurrected-alone still renders via
    `_resurrected_decisions_banner` in the early-return branch above.

    Golden-diff scope, narrowed (C3, Review: staff-eng finding 2): under
    manifest state `ok`, every rendering below is byte-for-byte identical
    to before this chunk. Under any other manifest state, exactly two
    lines become hedged (the disjoint-case lead line and the
    settings-only removal-instruction line) -- every other line, including
    the `double_fire=True` banner and the `indeterminate` banner, is
    untouched.
    """
    top = _banner_top()
    bot = _banner_bottom()
    if report.indeterminate:
        # Required fix #2 (see `HookDeliveryReport.indeterminate`'s
        # docstring): a machine where the coordinator content root does not
        # resolve at all must never render a confident negative -- neither
        # silence (the pre-fix behavior: `plugin_present=False` made both
        # `double_fire` and `settings_only_scripts` empty, so the old early
        # return above fired and said nothing) nor the disjoint "nothing is
        # firing twice today" banner. Report the check as unable to run and
        # name the unresolved variable/error verbatim.
        error_line = (report.content_root_error or "").splitlines()[0] if report.content_root_error else "(no detail)"
        return "\n".join(
            [
                "",
                top,
                "║  ?  HOOK-DELIVERY DUPLICATION CHECK IS INDETERMINATE",
                "║",
                "║  settings.json declares a `hooks` block, but the coordinator",
                "║  content root could not be resolved on this machine -- so",
                "║  plugin-side hooks.json could not even be located, and this check",
                f"║  cannot tell you whether hooks are firing twice or not:",
                f"║      settings.json's `hooks` block: {report.settings_entry_count} entries",
                "║",
                f"║  Unresolved: {error_line}",
                "║",
                "║  This is DETECT-ONLY -- nothing was changed.",
                "║  Action: fix coordinator content-root resolution on this machine",
                "║  (re-run the installer or /coordinator:setup), then re-check --",
                "║  this could be masking a real hooks-firing-twice condition.",
                bot,
                "",
            ]
        )
    degraded_applicable = report.degraded
    resurrected = report.resurrected_decisions
    if not report.double_fire and not report.settings_only_scripts:
        # Golden-diff scope, narrowed (C3): the double_fire=True and
        # settings-only banners below are byte-for-byte pinned except for
        # the two named hedged lines -- resurrected-decision and standalone
        # degraded/stale findings therefore get their OWN dedicated
        # rendering here, ahead of the empty-string fallback, rather than
        # being spliced into a pinned banner's body.
        if resurrected:
            return _resurrected_decisions_banner(report)
        if degraded_applicable:
            return _standalone_degraded_banner(report)
        return ""
    n = len(report.duplicated_scripts)
    if report.double_fire:
        lines = [
            "",
            top,
            f"║  ⚠  HOOKS ARE FIRING TWICE -- {n} duplicated hook script(s)",
            "║",
            "║  Both hook-delivery surfaces are live on this machine and declare",
            "║  overlapping scripts:",
            f"║      plugin-side hooks.json:        {report.plugin_entry_count} entries",
            f"║      settings.json's `hooks` block: {report.settings_entry_count} entries",
            "║  Every duplicated hook below runs TWICE per session -- double the",
            "║  process-spawn cost, worst on Windows where spawn is most expensive.",
            "║",
        ]
        for script in report.duplicated_scripts:
            lines.append(f"║      {script}")
        lines.append("║")
    else:
        # Disjoint case (Review: code-reviewer, Finding 1): both surfaces are
        # live but declare no overlapping scripts, so nothing is firing
        # twice today -- the lead line says so instead of the false
        # "FIRING TWICE" claim. Golden-diff scope, narrowed (C3): this
        # unqualified claim is a real, verified absence of duplication only
        # under manifest state `ok` -- a non-`ok` state hedges instead,
        # since the check compared filenames only and cannot see guards
        # delivered behind a fan-in carrier.
        if report.manifest_state == "ok":
            disjoint_lead_line = "║  -- nothing is firing twice today:"
        else:
            disjoint_lead_line = (
                "║  -- no two files collide; guards delivered behind a fan-in"
                " carrier were not compared:"
            )
        lines = [
            "",
            top,
            "║  ⚠  TWO HOOK-DELIVERY SURFACES ARE LIVE, DECLARING DIFFERENT SCRIPTS",
            "║",
            "║  Both the plugin-side hooks.json and settings.json's `hooks` block",
            "║  are live on this machine, but they declare no overlapping scripts",
            disjoint_lead_line,
            f"║      plugin-side hooks.json:        {report.plugin_entry_count} entries",
            f"║      settings.json's `hooks` block: {report.settings_entry_count} entries",
            "║",
        ]
    if report.settings_only_scripts:
        k = len(report.settings_only_scripts)
        lines.append(
            f"║  ⛔ {k} script(s) are declared ONLY in settings.json:"
        )
        for script in report.settings_only_scripts:
            lines.append(f"║      {script}")
        lines.append("║")
        if report.manifest_state == "ok":
            lines.append(
                "║  Deleting the `hooks` key would stop these running entirely --"
            )
        else:
            # Golden-diff scope, narrowed (C3): hedged under any non-`ok`
            # manifest state -- this check cannot rule out that a
            # "settings-only" script is actually delivered behind a
            # fan-in carrier the manifest failed to describe.
            lines.append(
                "║  Deleting the `hooks` key would stop these running entirely,"
                " AS FAR AS THIS CHECK CAN TELL WITHOUT A DELIVERY MANIFEST --"
            )
        lines.append(
            "║  silently, with nothing erroring and nothing logged -- because"
        )
        lines.append(
            "║  the plugin-side hooks.json does not declare them. Verify each is"
        )
        lines.append(
            "║  covered elsewhere (or re-land it on the plugin side) BEFORE the"
        )
        lines.append("║  `hooks` key is removed.")
        lines.append("║")
    lines.append(
        "║  This is DETECT-ONLY -- settings.json is shared/synced across"
    )
    lines.append(
        "║  machines; a same-host repair here can strand a peer machine mid-sync"
    )
    lines.append("║  in the opposite direction. Nothing was changed.")
    lines.append("║")
    if report.double_fire:
        lines.append(
            "║  Action: arm the kill-switch on THIS machine -- create (touch) an"
        )
        lines.append(
            "║      ${CLAUDE_CONFIG_DIR:-~/.claude}/.coordinator-hooks-disabled"
        )
        lines.append(
            "║  file (any content) so `gen_settings_hooks.py` stops regenerating"
        )
        if report.settings_only_scripts:
            lines.append(
                "║  settings.json's `hooks` block. DO NOT DELETE the `hooks` key yet:"
            )
            lines.append(
                "║  removing it now would lose the settings-only scripts listed"
            )
            lines.append(
                "║  above. Resolve their coverage first, then remove the key."
            )
        else:
            lines.append(
                "║  settings.json's `hooks` block, THEN hand-remove the existing"
            )
            lines.append(
                "║  `hooks` key from settings.json so only the plugin-side path"
            )
            lines.append("║  remains live.")
    else:
        # Disjoint-but-settings-only case: no double-fire, so no kill-switch
        # action is needed -- just verify coverage before removing the key.
        lines.append(
            "║  Action: verify coverage of the settings-only script(s) above before"
        )
        lines.append(
            "║  removing settings.json's `hooks` key -- no kill-switch action is"
        )
        lines.append("║  needed here, since nothing is double-firing today.")
    if resurrected:
        # EM decision (C4, item 3): resurrected decisions are worse than a
        # duplicate or a settings-only entry -- a config frozen before a
        # retirement ruling silently outranks the ruling -- so a co-
        # occurring resurrected finding must not be suppressed behind
        # `double_fire`/`settings_only_scripts` dominating the banner body.
        # ADDITIVE ONLY: appended after the existing pinned body above,
        # never replacing or rewording it, so every golden-diff-pinned line
        # above (the `double_fire=True` banner, the disjoint banner, the
        # settings-only danger section) stays byte-identical. The
        # standalone case (resurrected alone, nothing else firing) still
        # renders via `_resurrected_decisions_banner` above and is
        # untouched by this block.
        k = len(resurrected)
        noun = "entry" if k == 1 else "entries"
        lines.append("║")
        lines.append(
            f"║  ⛔ Additionally, {k} settings.json {noun} name a guard the"
        )
        lines.append(
            "║  manifest marks RETIRED -- a config frozen before the ruling"
        )
        lines.append("║  silently outranks it:")
        lines.append("║")
        for decision in resurrected:
            lines.append(f"║      {decision.script}  (id: {decision.id})")
            lines.append(f"║          reason: {decision.reason}")
        lines.append("║")
        lines.append(
            "║  Action: remove that specific stale settings.json entry (NOT the"
        )
        lines.append(
            "║  kill-switch -- these guards were deliberately retired, not"
        )
        lines.append("║  accidentally duplicated).")
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


def evaluate_hook_delivery_duplication(config_dir: Optional[Path] = None) -> str:
    """Convenience wrapper -- detect + format in one call, mirroring the
    `evaluate_settings_integrity` / `evaluate_foreign_platform_paths` shape
    every other SessionStart-hook-shaped guard in this module/its sibling
    uses. NOT registered into any live hook chain by this dispatch -- see
    this dispatch's report for the exact registration step a future
    dispatch would take (a shared `settings.json`-adjacent surface is
    actively being worked by a concurrent session; registration is a
    separate, deliberate call).
    """
    return format_hook_delivery_banner(detect_hook_delivery_duplication(config_dir))


# ---------------------------------------------------------------------------
# Atomic same-dir copy
# ---------------------------------------------------------------------------


def _atomic_copy(src: Path, dst: Path) -> bool:
    """Copy src -> dst atomically within dst's directory (PID-suffixed temp + replace).

    Mirrors the oracle's `cp` into a `${dst}.tmp.$$` sibling followed by
    `mv -f` (same-filesystem rename is atomic). Returns False (never raises)
    on any I/O failure — mirrors the oracle's `|| return 1`.
    """
    tmp_path = dst.parent / f"{dst.name}.tmp.{os.getpid()}"
    try:
        with src.open("rb") as sfh:
            data = sfh.read()
    except OSError:
        print(f"skip: _atomic_copy: with src.open(\"rb\") as sfh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    try:
        with tmp_path.open("wb") as tfh:
            tfh.write(data)
        os.replace(tmp_path, dst)
    except OSError:
        print(f"skip: _atomic_copy: with tmp_path.open(\"wb\") as tfh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            print(f"skip: _atomic_copy: tmp_path.unlink(missing_ok=True) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        return False
    return True


# ---------------------------------------------------------------------------
# Banner text — exact-text golden-diff targets (byte-for-byte vs oracle)
# ---------------------------------------------------------------------------

_BANNER_INLINE_INSTALL = """
╔══════════════════════════════════════════════════════════════════╗
║  ℹ  settings.json has no `enabledPlugins` — but this is an INLINE
║     (--plugin-dir) install (`.doe-root` present, coordinator loads live
║     from the clone). This is EXPECTED, not a clobber. No action needed.
╚══════════════════════════════════════════════════════════════════╝

"""


def _banner_restored(restored_from: str, clobber_bak: Path, *, least_trusted: bool = False) -> str:
    """Render the restore banner. `least_trusted` (Review: code-reviewer,
    Finding 3) labels a rung-3 restore explicitly as operator-placed/
    unauthenticated -- rungs 1-2 restore from sources this guard or git
    itself wrote/tracked, while rung 3 restores from any file matching a
    guessable naming convention with no provenance check (see the "Restore
    rungs" section docstring's accepted-risk note above
    `_known_good_backup_candidates`). The operator must be able to tell
    which rung fired from the banner alone, not have to go read source."""
    top = _banner_top()
    bot = _banner_bottom()
    provenance_note = (
        "║\n"
        "║  NOTE: this restore came from an operator-placed backup file (matched\n"
        "║  by filename convention only, not verified/authenticated) -- the\n"
        "║  least-trusted of this guard's three restore sources. Confirm its\n"
        "║  contents are what you expect.\n"
        if least_trusted
        else ""
    )
    return (
        "\n"
        f"{top}\n"
        f"║  ✓  settings.json WAS CLOBBERED — AUTO-RESTORED from {restored_from}\n"
        "║\n"
        "║  The user settings.json had lost its `enabledPlugins` block (the\n"
        "║  recurring stub-truncation / CRLF-race / harness-drop failure mode),\n"
        "║  which silently disables ALL plugins for the session. It has been\n"
        "║  restored on disk, and the clobbered copy saved to:\n"
        f"║      {clobber_bak}\n"
        f"{provenance_note}"
        "║\n"
        "║  This session already loaded the BROKEN settings at boot, so the\n"
        "║  plugins are still down RIGHT NOW.\n"
        "║  Action: surface this to the PM as your first message and recommend\n"
        "║  running  /reload-plugins  (or restarting the session) to pick up the\n"
        "║  restored config before doing any plugin-dependent work.\n"
        f"{bot}\n"
        "\n"
    )


_BANNER_NO_RESTORE_SOURCE = """
╔══════════════════════════════════════════════════════════════════╗
║  ⚠  settings.json LOOKS CLOBBERED (no `enabledPlugins`) — and there
║     is NO known-good snapshot or git HEAD to restore from.
║
║  Action: if plugins SHOULD be enabled, surface this to the PM — the
║  settings.json needs a hand-restore, after which this guard will
║  snapshot it on the next healthy boot. If the install genuinely has no
║  plugins enabled, this is expected; no action needed.
╚══════════════════════════════════════════════════════════════════╝

"""


# ---------------------------------------------------------------------------
# Restore rungs — the third rung (known-good backup under the settings home)
# and the fall-through restructure of the ladder as a whole.
#
# Fresh-clone gap this closes: on a fresh clone of the `~/.claude` repo,
# neither of the first two rungs resolves. `settings.json` is untracked
# there (machine-local, cannot be synced — 2026-07-28), so rung 2's
# `git show HEAD:./settings.json` exits non-zero; and `.settings-last-good.json`
# is gitignored, so a fresh clone carries no snapshot either. The clobber
# guard therefore had NO recovery path in exactly the machine state where it
# is most needed. This rung reads the same kind of ad-hoc timestamped backup
# that manually saved a real incident on 2026-07-31.
#
# Rung ordering (deliberate, not arbitrary):
#   1. snapshot (`.settings-last-good.json`) — this guard's OWN continuously
#      refreshed record of the last-seen-healthy settings.json on THIS
#      machine. Freshest and most-trusted: written on every healthy boot.
#   2. git HEAD — a repo-tracked value. Trusted (it is what was committed),
#      but on any machine where settings.json is untracked/gitignored
#      (increasingly the norm, see the module docstring's "Hook-layer
#      reachability" note and the 2026-07-28 untracking) it is stale or
#      simply absent, so it sits behind the live snapshot.
#   3. known-good backup — an ad-hoc, OPERATOR-PLACED file with no
#      freshness guarantee at all beyond its own filename timestamp. Least
#      automated, least trusted of the three, so it is the rung of last
#      resort — but it is also the ONLY rung that resolves in the fresh-
#      clone gap above, which is precisely why it exists.
#
#      Accepted risk (Review: code-reviewer, Finding 3), stated explicitly
#      rather than left implicit: this file is UNAUTHENTICATED, matched by
#      filename convention only -- there is no ownership/permission check on
#      the candidate before it is trusted and copied over settings.json.
#      A file-ownership/permission-bits check was deliberately NOT added,
#      because `os.stat().st_uid` is meaningless on Windows and this
#      codebase treats Windows as first-class (a POSIX-only trust check on
#      the SessionStart boot path would itself be a portability defect).
#      The restore banner (`_banner_restored`'s `least_trusted` flag) labels
#      a rung-3 restore explicitly so the operator can tell which rung
#      fired without reading source.
#
#      NO LONGER accepted (2026-08-01 scope-escape fix, see
#      `_settings_home_scoped_to`): `settings_home()` CAN still resolve to a
#      directory distinct from `config_dir` -- different
#      COORDINATOR_SETTINGS_HOME/CLAUDE_HOME vs. CLAUDE_CONFIG_DIR roots is
#      an intentional, supported shape -- but rung 3 no longer trusts that
#      resolution blindly for whatever `config_dir` the caller happened to
#      pass in. A resolved settings home is now searched only when
#      `config_dir` and `home` are direct siblings under the same parent
#      directory (the shape `settings_home()`'s own default construction
#      produces, and the shape every existing test fixture uses when it
#      sets `COORDINATOR_SETTINGS_HOME` explicitly). This check applies
#      EVEN WHEN the override is set -- an explicit
#      `COORDINATOR_SETTINGS_HOME` decides WHICH directory resolves as the
#      settings home, it does not by itself license restoring into an
#      arbitrary `config_dir` the override's own placement says nothing
#      about (an earlier draft of this fix trusted the override
#      unconditionally regardless of `config_dir`, which reintroduced the
#      exact ambient-host-state dependency being closed here -- caught in
#      review before landing). A scoped `config_dir` pointing at an
#      unrelated tree -- a test's `tmp_path`, a non-default install layout
#      -- while ambient env vars still resolve `home` to THIS machine's
#      real settings home no longer restores from it; rung 3 simply
#      reports no candidate. This was a real defect, not a hypothetical:
#      two DoE-claude tests
#      (`test_case_a2_bogus_doe_root_falls_through_to_clobber`,
#      `test_case_b_genuine_clobber_no_sentinel`) ran the hook with only
#      `CLAUDE_CONFIG_DIR` pointed at a tmp dir and got AUTO-RESTORED from a
#      real operator-placed backup on the host machine's actual settings
#      home, instead of the no-restore-source banner the fixture intends.
#
# Fall-through restructure: the pre-2026-07-31 ladder bound rung 2 to an
# `else` on rung 1's PRECONDITION (`snapshot.is_file() and _is_healthy(...)`),
# not on whether rung 1 actually RESTORED. That happened to still try git
# whenever the snapshot was absent/unhealthy (the `else` fires then) — but a
# snapshot that passed `_is_healthy` and then failed at the actual
# `_atomic_copy` (e.g. a permission error, a race) left `restored_from`
# empty with NO fallback attempted, silently landing on
# `_BANNER_NO_RESTORE_SOURCE` even though git HEAD or a known-good backup may
# have been perfectly good. The three rungs below are now independent: each
# is attempted iff `restored_from` is still empty after the ones before it,
# regardless of why the earlier rung didn't restore. `test_snapshot_copy_
# failure_falls_through_to_git` pins this behaviour change.
# ---------------------------------------------------------------------------


def _known_good_backup_candidates(home: Path) -> List[Path]:
    """Every `settings.json.known-good-<TIMESTAMP>` file directly under
    `home`, newest-timestamp-first (string-sorted on the `<TIMESTAMP>`
    suffix, NOT mtime — see `_KNOWN_GOOD_BACKUP_RE`'s comment for why).
    Never raises: an unreadable directory degrades to an empty list."""
    candidates: List[Tuple[str, Path]] = []
    try:
        for candidate in home.glob(_KNOWN_GOOD_BACKUP_GLOB):
            if not candidate.is_file():
                continue
            match = _KNOWN_GOOD_BACKUP_RE.match(candidate.name)
            if not match:
                continue
            candidates.append((match.group(1), candidate))
    except OSError:
        return []
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in candidates]


def _settings_home_scoped_to(config_dir: Path, home: Path) -> bool:
    """True iff `home` (the just-resolved settings home) is genuinely THIS
    `config_dir`'s own settings home, rather than an ambient one that
    merely happens to resolve on this machine for a `config_dir` the
    caller never scoped it to.

    One structural relatedness test, applied UNIFORMLY regardless of how
    `home` was resolved -- default resolution or an explicit
    `COORDINATOR_SETTINGS_HOME` override: `config_dir` and `home` must be
    direct siblings under the same parent directory. This is exactly the
    shape `settings_home()`'s own default construction produces
    (`home_dir()/".coordinator-claude-settings"` alongside the default
    `config_dir` of `home_dir()/".claude"`), and it is also the shape
    every rung-3 test fixture in this suite uses when it sets
    `COORDINATOR_SETTINGS_HOME` explicitly (the settings home and
    `config_dir` are created as siblings under the same tmp root).

    Deliberately NOT special-cased on whether `COORDINATOR_SETTINGS_HOME`
    is set: an explicit override decides WHICH directory resolves as the
    settings home, but does not by itself license restoring into an
    arbitrary `config_dir` the override's own placement says nothing
    about -- honouring the override unconditionally, independent of
    `config_dir`, would reintroduce the exact ambient-host-state
    dependency this predicate exists to close (an operator's real,
    populated settings home leaking into an unrelated scoped `config_dir`
    that merely happens to run on the same host). Never raises: any
    resolution failure (either path missing, a home-resolution error
    surfaced by the caller before this is even reached) degrades to
    untrusted.
    """
    try:
        return config_dir.resolve().parent == home.resolve().parent
    except Exception:
        return False


def _find_known_good_backup(config_dir: Path) -> Optional[Path]:
    """Resolve the settings home (reusing
    `coordinator_core._settings_home.settings_home()` — the same
    `COORDINATOR_SETTINGS_HOME`/`CLAUDE_HOME`/platform-home resolver every
    other settings-home caller in this codebase uses, no second resolver
    added here), confirm it is actually scoped to `config_dir`
    (`_settings_home_scoped_to` — see that docstring and the "Restore
    rungs" section docstring's updated accepted-risk note for why this
    check exists), and return the newest known-good backup under it that
    itself passes `_is_healthy`, skipping any unhealthy candidate in favour
    of the next-newest. Returns None if the settings home can't be
    resolved, isn't scoped to `config_dir`, has no candidates, or every
    candidate is unhealthy."""
    try:
        home = settings_home()
    except Exception:
        # Review: code-reviewer (Finding 2) -- settings_home() reaches
        # Path.home(), which is documented to raise RuntimeError (not
        # ValueError/OSError) when no home directory resolves at all (no
        # HOME/USERPROFILE, no resolvable passwd/user-profile entry -- a
        # real container/CI shape). Every rung in evaluate_settings_integrity
        # is individually responsible for never raising; a narrow tuple here
        # is exactly what let this rung violate that contract, and the next
        # helper added upstream of settings_home() will have its own
        # exception surface. Broad on purpose, matching the self-probe's own
        # top-level `except Exception` posture in run_self_probe.
        return None
    if not _settings_home_scoped_to(config_dir, home):
        return None
    for candidate in _known_good_backup_candidates(home):
        if _is_healthy(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Core logic — pure function, direct-importable (no IPC round-trip required)
# ---------------------------------------------------------------------------


def evaluate_settings_integrity(config_dir: Optional[Path] = None) -> str:
    """Run the guard against `config_dir`; return the additionalContext text.

    Parameters
    ----------
    config_dir:
        Directory containing settings.json. Defaults to
        `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` (env-read at call time, matching
        the oracle's own env resolution — never cached at import time).

    Returns
    -------
    str
        The banner text to emit as additionalContext (empty string == silent,
        the healthy-no-op and settings-json-absent cases).

    Never raises: any unexpected filesystem/subprocess failure degrades to the
    closest safe outcome (typically "no banner" / "no restore"), matching the
    oracle's `|| true` / fail-open posture — this hook must NEVER block
    SessionStart.
    """
    if config_dir is None:
        raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        config_dir = Path(raw)

    settings = config_dir / "settings.json"
    snapshot = config_dir / _SNAPSHOT_NAME
    clobber_bak = config_dir / _CLOBBER_BAK_NAME

    if not settings.is_file():
        return ""

    if _is_healthy(settings):
        # Healthy: keep the snapshot fresh, but only write on actual change
        # (avoid mtime churn + needless disk writes every boot). Concurrent
        # identical writes are harmless (same content, atomic replace).
        try:
            needs_write = not snapshot.is_file() or not filecmp.cmp(
                settings, snapshot, shallow=False
            )
        except OSError:
            needs_write = True
        if needs_write:
            _atomic_copy(settings, snapshot)  # best-effort; `|| true` in oracle

        # Reconciliation lens (separate from the clobber lens above): the
        # block survived intact, but does it describe plugins that are
        # actually installed? Fail-open on any uncertainty (see
        # _reconcile_enabled_plugins docstring).
        settings_data = _load_json_dict(settings)
        if settings_data is not None:
            unreachable = _reconcile_enabled_plugins(settings_data, config_dir)
            if unreachable:
                return _banner_unreachable_plugins(unreachable, settings_data)
        return ""

    # --- Inline-install short-circuit: MUST run before any restore attempt. ---
    if is_inline_install(config_dir):
        return _BANNER_INLINE_INSTALL

    # --- Unhealthy: settings.json is a stub / corrupt / lost its plugins. ---
    # Three-rung ladder, TRUE fall-through: each rung is attempted iff
    # `restored_from` is still empty after the ones before it — see the
    # "Restore rungs" section docstring above `_known_good_backup_candidates`
    # for the ordering rationale and the fall-through restructure this
    # replaces (pre-2026-07-31: rung 2 was `else`-bound to rung 1's
    # precondition, not to whether rung 1 actually restored).
    restored_from = ""

    # Rung 1: snapshot (.settings-last-good.json).
    if snapshot.is_file() and _is_healthy(snapshot):
        # Best-effort forensic backup — must NOT gate the restore attempt.
        _atomic_copy(settings, clobber_bak)
        if _atomic_copy(snapshot, settings):
            restored_from = "snapshot (.settings-last-good.json)"

    # Rung 2: git HEAD of config_dir.
    if not restored_from:
        try:
            is_git_repo = (
                subprocess.run(
                    ["git", "-C", str(config_dir), "rev-parse", "--git-dir"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    # popup-intentional-last-resort
                ).returncode
                == 0
            )
        except OSError:
            is_git_repo = False

        if is_git_repo:
            head_tmp_fd, head_tmp_name = tempfile.mkstemp(
                dir=str(config_dir), prefix="settings.json.head."
            )
            head_tmp = Path(head_tmp_name)
            try:
                with os.fdopen(head_tmp_fd, "wb") as tfh:
                    proc = subprocess.run(
                        ["git", "-C", str(config_dir), "show", "HEAD:./settings.json"],
                        stdout=tfh,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        # popup-intentional-last-resort
                    )
                if proc.returncode == 0 and _is_healthy(head_tmp):
                    _atomic_copy(settings, clobber_bak)  # best-effort, never blocks restore
                    try:
                        os.replace(head_tmp, settings)
                        restored_from = "git HEAD"
                    except OSError:
                        head_tmp.unlink(missing_ok=True)
                else:
                    head_tmp.unlink(missing_ok=True)
            except OSError:
                head_tmp.unlink(missing_ok=True)

    # Rung 3: newest healthy known-good backup under the settings home.
    restored_from_rung3 = False
    if not restored_from:
        backup = _find_known_good_backup(config_dir)
        if backup is not None:
            _atomic_copy(settings, clobber_bak)  # best-effort, never blocks restore
            if _atomic_copy(backup, settings):
                restored_from = f"known-good backup ({backup.name})"
                restored_from_rung3 = True

    if restored_from:
        return _banner_restored(restored_from, clobber_bak, least_trusted=restored_from_rung3)
    return _BANNER_NO_RESTORE_SOURCE


# ---------------------------------------------------------------------------
# Kill-switch marker loudness -- a third question, distinct from both lenses
# above. `_hook_layer_reachable`/`detect_hook_delivery_duplication` ask "is a
# hook actually going to fire"; this section asks "is the OPERATOR marker
# that suppresses `gen_settings_hooks.py` regeneration (`.coordinator-hooks-
# disabled`) still announcing itself, and does its own stated reason still
# hold" -- the gap that let the marker sit armed and SILENT for two weeks
# (2026-07-14 -> 2026-07-28) with a disarm condition that had since been met
# and nothing said so.
#
# THE INVERSION THAT MATTERS: this detector must never disarm the marker,
# not even past its own stated expiry. On THIS fleet, `gen_settings_hooks.py`
# reads the exact same `hooks/hooks.json` the plugin-side path already
# delivers -- a machine with both live runs every hook twice (see
# `detect_hook_delivery_duplication` above). Deleting the kill-switch on a
# machine that also has plugin-side delivery working does not "restore
# hooks" -- it manufactures the double-fire regression, worst on the
# platform (Windows) this marker exists to protect. So expiry here escalates
# the announcement and demands a human re-confirm (by hand-editing the
# marker's own `Expires:` field forward) -- it is structurally incapable of
# writing to the marker itself.
#
# Recommended marker schema (a NEW convention this detector expects; the
# live marker on the machine this was authored on predates it and therefore
# parses as "missing Expires -- escalate now", which is the correct, honest
# read of an un-migrated marker, not a bug in the detector):
#
#     Since: YYYY-MM-DD
#     Expires: YYYY-MM-DD
#     Reason: <free text, one line or a short paragraph>
#     Disarm condition: <free text -- what must be true before deleting>
#
# `Since`/`Expires` are the only two lines this parser requires; `Reason`/
# `Disarm condition` are surfaced verbatim when present, never required.
#
# Spec backlink: DoE-claude dispatch state/subagent-share/
# 78b683cd-1b62-4a25-904d-954cb3c69412/coordinatorexecutor-dcbed68d.md
# (2026-07-28/29).
# ---------------------------------------------------------------------------

_KILL_SWITCH_MARKER_NAME = ".coordinator-hooks-disabled"
_KS_SINCE_RE = re.compile(r"(?im)^\s*since:\s*(\d{4}-\d{2}-\d{2})\s*$")
_KS_EXPIRES_RE = re.compile(r"(?im)^\s*expires:\s*(\d{4}-\d{2}-\d{2})\s*$")
_KS_REASON_RE = re.compile(r"(?im)^\s*reason:\s*(.+)$")
_KS_DISARM_RE = re.compile(r"(?im)^\s*disarm condition:\s*(.+)$")

# Historical fact, not derived from the marker's own (possibly un-migrated)
# text: the disarm condition this marker carried when first armed
# (2026-07-14, per DoE-claude archive/daily-summaries/2026-07-14-machine-a.md
# and state/2026-07-28-machine-a-install-dogfood-friction-log.md) was "delete
# once the naked-Python hook migration lands". That migration is COMPLETE
# (37/37 hook scripts are Python, verified 2026-07-28 on the machine-a
# machine — see the friction log above; `debash-scorecard.py`'s FLOOR is
# empty). Surfaced unconditionally in the loud banner below so a human sees
# the historical condition was met WITHOUT this detector having to guess it
# out of free-text prose that may or may not still contain it.
_HISTORICAL_DISARM_CONDITION = "delete once the naked-Python hook migration lands"
_HISTORICAL_DISARM_STATUS = (
    "MET as of 2026-07-28 (37/37 hook scripts are Python)"
)


@dataclass(frozen=True)
class KillSwitchMarkerInfo:
    """Parsed view of `.coordinator-hooks-disabled`'s own content. `parse_ok`
    is False whenever the file cannot be read as UTF-8 text, is empty/
    whitespace-only, or lacks a well-formed `Expires:` line -- ANY of those
    is treated as malformed, never as "no expiry means never expires" (see
    module section docstring: absence of a parseable Expires line must
    escalate, not silently pass)."""

    present: bool
    parse_ok: bool
    raw_text: str
    since: Optional[str]
    expires: Optional[str]
    reason: Optional[str]
    disarm_condition: Optional[str]


def _kill_switch_marker_path(config_dir: Path) -> Path:
    return config_dir / _KILL_SWITCH_MARKER_NAME


def _read_kill_switch_marker(marker: Path) -> Optional[KillSwitchMarkerInfo]:
    """Read-only parse of `marker`. Returns None iff the file does not exist
    (the caller's quiet path). Never raises, never writes -- an unreadable
    (permission error, bad encoding) or empty marker still returns a
    `KillSwitchMarkerInfo` with `parse_ok=False`, not None, so the caller's
    fail-loud escalation path fires rather than the silent "absent" one."""
    if not marker.is_file():
        return None
    try:
        raw_text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return KillSwitchMarkerInfo(
            present=True, parse_ok=False, raw_text="", since=None,
            expires=None, reason=None, disarm_condition=None,
        )
    if not raw_text.strip():
        return KillSwitchMarkerInfo(
            present=True, parse_ok=False, raw_text=raw_text, since=None,
            expires=None, reason=None, disarm_condition=None,
        )

    since_m = _KS_SINCE_RE.search(raw_text)
    expires_m = _KS_EXPIRES_RE.search(raw_text)
    reason_m = _KS_REASON_RE.search(raw_text)
    disarm_m = _KS_DISARM_RE.search(raw_text)

    expires = expires_m.group(1) if expires_m else None
    parse_ok = expires is not None and _valid_iso_date(expires)

    return KillSwitchMarkerInfo(
        present=True,
        parse_ok=parse_ok,
        raw_text=raw_text,
        since=since_m.group(1) if since_m else None,
        expires=expires if parse_ok else None,
        reason=reason_m.group(1).strip() if reason_m else None,
        disarm_condition=disarm_m.group(1).strip() if disarm_m else None,
    )


def _valid_iso_date(text: str) -> bool:
    try:
        _dt.date.fromisoformat(text)
        return True
    except ValueError:
        return False


def _double_fire_summary(config_dir: Path) -> str:
    """One line honestly describing the CURRENT double-fire status on this
    machine, reusing `detect_hook_delivery_duplication` (no second resolver)
    -- feeds "what disarming would actually do today" so the announcement
    never has to guess."""
    try:
        report = detect_hook_delivery_duplication(config_dir)
    except Exception:  # never let a diagnostic sub-check break the banner
        return "double-fire status: could not be checked this session."
    if report.double_fire:
        return (
            f"double-fire status: ALREADY firing twice right now "
            f"({len(report.duplicated_scripts)} duplicated script(s)) -- "
            "disarming would not introduce the regression, it is already live."
        )
    if report.settings_present and report.settings_resolvable:
        return (
            "double-fire status: settings.json already carries a live `hooks` "
            "block distinct from the plugin-side one -- disarming risks "
            "regenerating on top of it without first checking for overlap."
        )
    if report.plugin_present and report.plugin_resolvable:
        return (
            "double-fire status: plugin-side delivery is live and healthy on "
            "its own -- disarming today would start regenerating settings.json's "
            "`hooks` block ALONGSIDE it, i.e. cause double-fire, not restore "
            "normal operation."
        )
    return "double-fire status: neither delivery surface currently resolves on this machine."


def _ks_banner_body(info: KillSwitchMarkerInfo, marker: Path, config_dir: Path) -> List[str]:
    lines = [
        f"║  Since:  {info.since or '(not stated in marker)'}",
    ]
    if info.reason:
        lines.append(f"║  Reason: {info.reason}")
    lines.append("║")
    lines.append(f"║  Historical disarm condition: \"{_HISTORICAL_DISARM_CONDITION}\"")
    lines.append(f"║      status: {_HISTORICAL_DISARM_STATUS}")
    if info.disarm_condition:
        lines.append(f"║  Marker's own stated disarm condition: \"{info.disarm_condition}\"")
    lines.append("║")
    lines.append(f"║  {_double_fire_summary(config_dir)}")
    lines.append("║")
    lines.append(
        "║  Met disarm condition or not, deleting this marker on THIS machine"
    )
    lines.append(
        "║  today would very likely cause hooks to fire TWICE, not restore them."
    )
    lines.append(
        "║  Consult `evaluate_hook_delivery_duplication` before deleting."
    )
    return lines


# ---------------------------------------------------------------------------
# Route vs answer -- the not-yet-expired ("armed, healthy") case is the one
# that fires on EVERY boot for months at a stretch (this machine's live
# marker: Expires 2026-09-30) and was, until this change, ~40 lines of full
# disarm-condition/double-fire analysis re-rendered every single time. Per
# the governing finding of the spinoff this closes -- "boot injection can
# carry an index; it cannot usefully carry answers" -- that branch alone
# collapses to a ROUTER: one line naming what's armed, since when, that it
# is not yet expired, and the exact command that prints the full detail on
# demand. The malformed and expired branches are unaffected: both are
# operator-actionable defects and stay loud and full every time, matching
# the module section docstring's "silence while armed is the exact defect
# this closes" invariant -- LOUD, not SILENT, is the floor; only the
# *volume* of the healthy-armed case changes, never its presence.
#
# `_render_kill_switch_banner` is the ONE renderer for all three cases,
# parameterised by `verbose`: `format_kill_switch_announcement` (routine
# boot path) calls it with `verbose=False`; `format_kill_switch_full_detail`
# (on-demand path, see `evaluate_hooks_kill_switch_full_detail` /
# `session.guard_hooks_kill_switch_detail` op below) calls it with
# `verbose=True`. `verbose` only changes the SHAPE of the not-expired
# branch's output -- the malformed/expired branches render identically
# either way, and `_ks_banner_body` is reused unchanged as the on-demand
# body-builder, never copy-pasted into a second short-form renderer.
#
# Spec backlink: state/handoffs/2026-07-30-boot-context-bloat-non-orientation-surfaces.md
# item 4 ("Retire the hooks-disabled banner to a doctor probe") / AC4.
# ---------------------------------------------------------------------------

_KS_DETAIL_OP = "session.guard_hooks_kill_switch_detail"
_KS_DETAIL_COMMAND = f"python3 -m coordinator_core.invoke {_KS_DETAIL_OP} --bare"


def _ks_router_line(info: KillSwitchMarkerInfo) -> str:
    """The one-line routine-boot summary for the not-expired-armed case:
    what's armed, since when, that it isn't expired yet (with the date),
    and the exact invocable command for the full detail. A routing fact
    (what exists, where to look), never an answering one (the disarm
    history, the double-fire status) -- those stay behind the command this
    line names."""
    since = info.since or "(not stated in marker)"
    return (
        f"Coordinator hook generation is DISABLED on this machine "
        f"(since {since}; Expires: {info.expires}, not yet reached). "
        f"Full detail (disarm history, double-fire diagnostics): `{_KS_DETAIL_COMMAND}`"
    )


def _render_kill_switch_banner(
    info: Optional[KillSwitchMarkerInfo], marker: Path, config_dir: Path, *, verbose: bool,
) -> str:
    """The single renderer behind both `format_kill_switch_announcement`
    (routine boot, `verbose=False`) and `format_kill_switch_full_detail`
    (on-demand, `verbose=True`). Non-empty (loud) whenever the marker is
    PRESENT, regardless of expiry or verbosity -- silence while armed is
    the exact defect this closes. Returns "" only when `info is None`
    (marker absent -- nothing to announce, at any verbosity).

    `verbose` affects ONLY the not-expired branch's shape (full box vs one
    router line) -- malformed and expired both always render their full
    loud detail, because both are operator-actionable defects, not routine
    "everything's fine, here's the marker" status.
    """
    if info is None:
        return ""

    top = "╔" + ("═" * 72) + "╗"
    bot = "╚" + ("═" * 72) + "╝"

    if not info.parse_ok:
        lines = [
            "",
            top,
            "║  ⚠  Coordinator hook generation is DISABLED on this machine, and its",
            "║     kill-switch marker is MALFORMED (no parseable `Expires:` line, or",
            "║     the file is empty/unreadable).",
            "║",
            f"║  Marker: {marker}",
            "║",
            "║  This is being reported LOUD and the marker is being left ARMED --",
            "║  a malformed marker is never treated as \"no marker\" / disarmed.",
            "║  Action: add an `Expires: YYYY-MM-DD` line (see this module's own",
            "║  section docstring for the recommended schema) so this detector can",
            "║  track the marker's own stated shelf life.",
            "║",
        ]
        lines.extend(_ks_banner_body(info, marker, config_dir))
        lines.append(bot)
        lines.append("")
        return "\n".join(lines)

    today = _dt.date.today()
    expires_date = _dt.date.fromisoformat(info.expires)
    expired = today >= expires_date

    if not expired:
        if not verbose:
            return "\n" + _ks_router_line(info) + "\n"
        lines = [
            "",
            top,
            "║  ℹ  Coordinator hook generation is DISABLED on this machine.",
            "║",
            f"║  Marker: {marker}",
            f"║  Expires: {info.expires} (not yet reached)",
        ]
        lines.extend(_ks_banner_body(info, marker, config_dir))
        lines.append(bot)
        lines.append("")
        return "\n".join(lines)

    # Expired: escalate. Never disarms -- only ever reports, at any verbosity.
    lines = [
        "",
        top,
        "║  ⚠⚠  Coordinator hook generation kill-switch has EXPIRED and is",
        "║      STILL ARMED -- it does not disarm itself, on purpose.",
        "║",
        f"║  Marker:  {marker}",
        f"║  Expires: {info.expires} (REACHED — {today.isoformat()})",
        "║",
        "║  ACTION REQUIRED: this marker cannot re-confirm itself. A human must",
        "║  deliberately edit the marker file and move its `Expires:` line",
        "║  forward, OR delete it once double-fire is confirmed safe (see the",
        "║  double-fire status line below) -- there is no automatic path in",
        "║  either direction.",
    ]
    lines.extend(_ks_banner_body(info, marker, config_dir))
    lines.append(bot)
    lines.append("")
    return "\n".join(lines)


def format_kill_switch_announcement(
    info: Optional[KillSwitchMarkerInfo], marker: Path, config_dir: Path,
) -> str:
    """Routine boot-path rendering -- thin wrapper over
    `_render_kill_switch_banner(..., verbose=False)`. Non-empty (loud)
    whenever the marker is PRESENT, regardless of expiry -- silence while
    armed is the exact defect this closes. The not-expired branch collapses
    to one router line (see `_ks_router_line`); malformed/expired stay full
    and loud, unaffected by this wrapper's verbosity choice."""
    return _render_kill_switch_banner(info, marker, config_dir, verbose=False)


def format_kill_switch_full_detail(
    info: Optional[KillSwitchMarkerInfo], marker: Path, config_dir: Path,
) -> str:
    """On-demand rendering -- thin wrapper over
    `_render_kill_switch_banner(..., verbose=True)`. Byte-identical to the
    pre-2026-07-30 `format_kill_switch_announcement` output for every case,
    including the not-expired case's full disarm-condition/double-fire
    body -- this is the "reachable on demand" half of AC4, not a reduced
    view."""
    return _render_kill_switch_banner(info, marker, config_dir, verbose=True)


def evaluate_hooks_kill_switch_announcement(config_dir: Optional[Path] = None) -> str:
    """Convenience wrapper -- read + format (routine, `verbose=False`) in
    one call, mirroring `evaluate_settings_integrity`/
    `evaluate_hook_delivery_duplication`'s own shape. This is the hot path
    the DoE SessionStart stub (`coordinator/hooks/scripts/guard-settings-
    integrity.py`) imports directly. Never writes to the marker or to
    `settings.json` under any circumstance, including an expired marker --
    see module section docstring."""
    if config_dir is None:
        raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        config_dir = Path(raw)
    marker = _kill_switch_marker_path(config_dir)
    info = _read_kill_switch_marker(marker)
    return format_kill_switch_announcement(info, marker, config_dir)


def evaluate_hooks_kill_switch_full_detail(config_dir: Optional[Path] = None) -> str:
    """On-demand counterpart to `evaluate_hooks_kill_switch_announcement` --
    read + format (`verbose=True`) in one call. This is what
    `_KS_DETAIL_COMMAND` (the command the routine router line names) and the
    `session.guard_hooks_kill_switch_detail` op below both resolve to. Never
    writes to the marker or to `settings.json`, same as its routine sibling."""
    if config_dir is None:
        raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        config_dir = Path(raw)
    marker = _kill_switch_marker_path(config_dir)
    info = _read_kill_switch_marker(marker)
    return format_kill_switch_full_detail(info, marker, config_dir)


# ---------------------------------------------------------------------------
# Registered op handler — IPC-daemon completeness / test-harness parity.
# NOT the DoE stub's hot path (it imports evaluate_settings_integrity directly).
# ---------------------------------------------------------------------------


@register_op("session.guard_settings_integrity")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'session.guard_settings_integrity' handler.

    Parameters (params dict):
        config_dir (str, optional) — overrides CLAUDE_CONFIG_DIR/$HOME/.claude
            resolution. Primarily for test-harness use.

    Returns:
        {"text": str}  — additionalContext text (empty string == silent).
    """
    config_dir_param = params.get("config_dir")
    config_dir = Path(config_dir_param) if config_dir_param else None
    text = await asyncio.to_thread(evaluate_settings_integrity, config_dir)
    return {"text": text}


@register_op("session.guard_hooks_kill_switch_detail")
def _handler_kill_switch_detail(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'session.guard_hooks_kill_switch_detail' handler -- the
    on-demand door named by the routine boot router line's `_KS_DETAIL_COMMAND`
    (`python3 -m coordinator_core.invoke session.guard_hooks_kill_switch_detail
    --bare`). Read-only; never writes to the marker or to `settings.json`.

    Parameters (params dict):
        config_dir (str, optional) — overrides CLAUDE_CONFIG_DIR/$HOME/.claude
            resolution. Primarily for test-harness use.

    Returns:
        {"text": str} — full-detail kill-switch text (empty string == no
        marker present, nothing to report).
    """
    config_dir_param = params.get("config_dir")
    config_dir = Path(config_dir_param) if config_dir_param else None
    text = evaluate_hooks_kill_switch_full_detail(config_dir)
    return {"text": text}

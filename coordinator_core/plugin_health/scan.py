"""
coordinator_core.plugin_health.scan — reader/consumer of the plugin_health.sentinel
schema; emits operator-facing addon-health notices.

Three sequential passes over `<plugin>/data/doctor-last-run.json` sentinel files:
  1. main verdict loop — scans BOTH `$PLUGINS_ROOT/*/data/doctor-last-run.json`
     (backup/snapshot dirs excluded, see `_is_backup_dir`) and
     `$CONSUMER_ROOT/*/data/doctor-last-run.json` (disjoint roots; non-plugin
     consumer repos such as example-cockpit-repo get verdict-only surfacing here),
     PLUS the settings-home mirror of both lanes (see "Dual-read" below).
  2. absent-sentinel detection — PLUGINS_ROOT-scoped ONLY, by design: a
     plugin-identity concern (walks every installed plugin dir for a declared-
     but-never-run doctor). Extending to CONSUMER_ROOT would file-scan every
     top-level dir under the consumer home for `commands/doctor.md`, manufacturing
     false-nag exposure. It IS settings-home-plugins-leg-aware (DR-072): a
     plugin whose sentinel migrated to the settings-home plugins lane while
     its install stays under legacy PLUGINS_ROOT must not be reported as
     never-run just because its legacy sentinel copy is gone — that would
     contradict pass 1's correct settings-home-sourced verdict in the same
     invocation. This adds only the settings-home *plugins* leg to pass 2's
     check, not a consumer leg — the "don't file-scan every consumer dir"
     rationale above still stands unchanged.
  3. SessionStart hook-script existence probe — also PLUGINS_ROOT-scoped ONLY,
     same rationale (a plugin hook-integrity probe, not a verdict surface).
     Genuinely sentinel-independent (checks a hooks.json-declared script path
     exists on disk under the plugin's OWN install tree, never reads sentinel
     data at all) — no settings-home leg applies here, unlike passes 1/2.

All three passes exclude backup/snapshot dirs from their PLUGINS_ROOT-scoped
enumeration (pass 1's consumer_root leg is not filtered — it is not a plugin
identity concern in the first place).

A separate `--check-sentinel-presence` mode (`check_sentinel_presence`,
outside the three-pass `--red-and-stale`/`--red-only` flow above) is also
settings-home-plugins-leg-aware (DR-072), for the same reason as pass 2: it
counts sentinels across BOTH the legacy plugins lane and the settings-home
plugins lane before deciding "no sentinel written anywhere," so a fully
DR-072-migrated machine does not trigger a false bootstrap nag.

Home resolution (§4a): CLAUDE_HOME is a $HOME SUBSTITUTE, not the `.claude`
dir itself — resolved via `sentinel._resolve_claude_home`, the single
implementation shared with this module's sibling `sentinel.py` (no
copy-pasted second implementation). Explicit `COORDINATOR_PLUGINS_ROOT` /
`COORDINATOR_CONSUMER_HEALTH_ROOT` overrides still win unconditionally.

Dual-read (settings-home lane): a consumer that has migrated its sentinel
to the settings-home root per DR-072 is still discovered — `scan_verdicts`
additionally globs `settings_home()/plugins/*/data/...` and
`settings_home()/*/data/...` alongside the legacy `~/.claude` lane. On a
same-plugin-name collision between the two lanes, the settings-home entry
wins UNCONDITIONALLY (not by recency) — per DR-072 a `~/.claude` copy is
permissible only as a disposable mirror, never authoritative, so authority
ordering (not freshness) decides. Collision dedup is scoped per leg
(plugins-leg names vs. consumer-leg names never cross-conflated) — see
`_gather_sentinels`. The settings-home lane is suppressed entirely when the
corresponding override env var is set, so an explicit override still wins
over both lanes. `scan_absent_sentinels` (pass 2) additionally consults the
settings-home *plugins* leg only, so it does not contradict pass 1 on a
migrated plugin — see pass 2 above.

Sentinel schema is produced by coordinator_core.plugin_health.sentinel and is an
unofficial-but-load-bearing cross-plugin contract (consumed by every OTHER
plugin's own doctor writing to the same `data/doctor-last-run.json` path, not
just this repo's own sentinel writer) — field names/types are read here
byte-for-byte, never coerced beyond `dict.get(..., default)`.

Port collapses the bash oracle's up-to-3-per-sentinel `python -c` subprocess
spawns (verdict-fields parse, `ran_at` epoch parse, hooks.json regex-parse) into
direct in-process dict access — no subprocess boundary remains inside this
module; the "no python3/python on PATH" advisory branch the bash oracle carried
is dropped entirely (moot once this logic runs as Python itself, not a bash
script probing for one).

Self-registration: importing this module calls register_op("plugin_health.scan", ...)
as a side-effect (same pattern as plugin_health.drift / ops/engine_drift.py).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g2/T3b
Port of: scan-addon-health.sh (coordinator-claude b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator, List, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.ipc import register_op
from coordinator_core.plugin_health.sentinel import _resolve_claude_home

_PROG = "scan-addon-health.sh"

_MODES = ("--red-only", "--red-and-stale", "--check-sentinel-presence")
_DEFAULT_MODE = "--red-and-stale"
_DEFAULT_STALE_SEC = 86400
_SENTINEL_REL = "data/doctor-last-run.json"

# Non-plugin residue excluded from all PLUGINS_ROOT-scoped enumeration — snapshot/backup dirs that carry
# a real plugin's commands/doctor.md only because they ARE a snapshot of one
# (e.g. `_pre-refresh-snapshots/` written by refresh-plugin-live-install.sh).
_BACKUP_DIR_PATTERNS = ("_*", "*.bak", "*-bak-*", "*.preisource-bak-*")

# ${CLAUDE_PLUGIN_ROOT}/<path> token extraction. Capture must start with '/'
# and exclude whitespace, quotes, and shell punctuation (; & | > <) so a
# trailing token on a shape the probe author didn't write (e.g. `...x.sh;` or
# `...x.sh"` or `&&`-chained) is not folded into the path.
_HOOK_SCRIPT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[^\s;\"'&|<>]+)")

_USAGE = f"""\
{_PROG} [--red-only | --red-and-stale | --check-sentinel-presence]

Read addon-health sentinel files and emit operator notices.
Exit 0 always (advisory, never gating). Silent when nothing to report.
"""


def _resolve_roots() -> "tuple[Path, Path, Optional[Path], Optional[Path]]":
    """Resolve the four sentinel-discovery roots.

    Returns (plugins_root, consumer_root, sh_plugins_root, sh_consumer_root).

    plugins_root/consumer_root are the legacy `~/.claude` lane, home-resolved
    via `_resolve_claude_home` (§4a: CLAUDE_HOME is a $HOME substitute, NOT
    the `.claude` dir itself) — reusing sentinel.py's single implementation
    rather than a second copy-pasted `Path.home()` call, which is what
    previously diverged this module from its sibling.

    sh_plugins_root/sh_consumer_root are the settings-home mirror of the same
    two lanes (DR-072 dual-read), each set to None (meaning "do not glob this
    lane") when the corresponding COORDINATOR_PLUGINS_ROOT /
    COORDINATOR_CONSUMER_HEALTH_ROOT override is present — an explicit
    override replaces its lane entirely rather than adding to it, so it wins
    over both the legacy and settings-home candidates.
    """
    plugins_override = os.environ.get("COORDINATOR_PLUGINS_ROOT")
    consumer_override = os.environ.get("COORDINATOR_CONSUMER_HEALTH_ROOT")

    claude_home = _resolve_claude_home(os.environ.get("CLAUDE_HOME"))
    plugins_root = Path(plugins_override) if plugins_override else claude_home / "plugins"
    consumer_root = Path(consumer_override) if consumer_override else claude_home

    sh = settings_home()
    sh_plugins_root = None if plugins_override else sh / "plugins"
    sh_consumer_root = None if consumer_override else sh

    return plugins_root, consumer_root, sh_plugins_root, sh_consumer_root


def _iter_maxdepth(root: Path, maxdepth: int) -> Iterator[Path]:
    """Yield files under root, bounded to `find -maxdepth maxdepth` semantics.

    root itself is depth 0; a direct child file is depth 1; a file N
    directories below root is depth N+1. Directories at depth == maxdepth are
    not descended into further (their own children would exceed maxdepth).
    """
    if not root.is_dir():
        return
    root_parts = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        depth = len(cur.parts) - root_parts
        if depth >= maxdepth:
            dirnames[:] = []
        for name in filenames:
            file_depth = depth + 1
            if file_depth <= maxdepth:
                yield cur / name


def _find_doctor_md(plugin_dir: Path, plugin: str) -> Optional[Path]:
    """First `commands/doctor.md` or `commands/<plugin>:doctor.md` within 4 levels.

    Matches the flat (`<plugin>/commands/doctor.md`), example-retrieval-repo
    (`<plugin>/plugin/commands/doctor.md`), and namespaced
    (`<plugin>/**/commands/<plugin>:doctor.md`) declaration shapes.
    """
    names = {"doctor.md", f"{plugin}:doctor.md"}
    candidates = [
        p for p in _iter_maxdepth(plugin_dir, 4) if p.parent.name == "commands" and p.name in names
    ]
    if not candidates:
        return None
    candidates.sort(key=str)
    return candidates[0]


def _is_backup_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _BACKUP_DIR_PATTERNS)


def _sorted_glob(root: Path, pattern: str) -> List[Path]:
    if not root.is_dir():
        return []
    return sorted(root.glob(pattern), key=str)


def _sorted_glob_excluding_backup(root: Path, pattern: str) -> List[Path]:
    """`_sorted_glob` filtered to drop entries whose first path segment under
    `root` is a backup/snapshot dir (see `_is_backup_dir`). Used for every
    PLUGINS_ROOT-scoped enumeration so pass 1's plugins_root leg and pass 3
    both honor the same non-plugin-residue exclusion pass 2 already applies.
    """
    return [
        p for p in _sorted_glob(root, pattern) if not _is_backup_dir(p.relative_to(root).parts[0])
    ]


def _gather_sentinels(
    plugins_root: Path,
    consumer_root: Path,
    sh_plugins_root: Optional[Path],
    sh_consumer_root: Optional[Path],
) -> List[Path]:
    """Merge sentinel discovery across the legacy `~/.claude` lane and the
    settings-home lane (DR-072 dual-read), settings-home winning unconditionally
    on a same-plugin-name collision.

    `sh_plugins_root`/`sh_consumer_root` are None when the caller's explicit
    COORDINATOR_PLUGINS_ROOT/COORDINATOR_CONSUMER_HEALTH_ROOT override
    suppressed that lane (see `_resolve_roots`) — that lane is simply skipped.

    Rationale for settings-home-wins-unconditionally (not a freshness/recency
    heuristic): per DR-072 a `~/.claude` copy is permissible only as a
    disposable mirror, never authoritative — so authority ordering, not
    recency, decides which copy is read when both lanes have written a
    sentinel for the same plugin name.

    Collision dedup is scoped PER LEG (plugins-leg names vs. consumer-leg
    names dedup'd separately) — the plugins leg and consumer leg are disjoint
    namespaces (a plugin install vs. an arbitrary consumer repo), so a
    settings-home plugin name must not suppress an unrelated same-named
    legacy CONSUMER-leg entry, and vice versa.
    """
    legacy_plugins = _sorted_glob_excluding_backup(plugins_root, f"*/{_SENTINEL_REL}")
    legacy_consumer = _sorted_glob(consumer_root, f"*/{_SENTINEL_REL}")

    sh_plugins_sentinels: List[Path] = []
    if sh_plugins_root is not None:
        sh_plugins_sentinels = _sorted_glob_excluding_backup(sh_plugins_root, f"*/{_SENTINEL_REL}")
    sh_consumer_sentinels: List[Path] = []
    if sh_consumer_root is not None:
        sh_consumer_sentinels = _sorted_glob(sh_consumer_root, f"*/{_SENTINEL_REL}")

    # Dedup key is scoped PER LEG (plugins vs. consumer), not a single flat
    # name set spanning both — a settings-home entry that only exists in the
    # plugins leg must not suppress an unrelated legacy CONSUMER-leg entry
    # that happens to share the same bare directory name (and vice versa).
    # The two legs are disjoint namespaces (a plugin install vs. an arbitrary
    # consumer repo); collapsing them into one dedup set would erase a
    # same-named-but-unrelated legacy entry from the other leg with nothing
    # left to replace it.
    sh_plugins_names = {p.parent.parent.name for p in sh_plugins_sentinels}
    sh_consumer_names = {p.parent.parent.name for p in sh_consumer_sentinels}

    legacy_plugins_filtered = [
        p for p in legacy_plugins if p.parent.parent.name not in sh_plugins_names
    ]
    legacy_consumer_filtered = [
        p for p in legacy_consumer if p.parent.parent.name not in sh_consumer_names
    ]

    return (
        legacy_plugins_filtered
        + legacy_consumer_filtered
        + sh_plugins_sentinels
        + sh_consumer_sentinels
    )


def scan_verdicts(
    plugins_root: Path,
    consumer_root: Path,
    mode: str,
    now: float,
    stale_sec: int,
    sh_plugins_root: Optional[Path] = None,
    sh_consumer_root: Optional[Path] = None,
) -> List[str]:
    """Pass 1 — verdict/staleness lines across both plugin and consumer sentinels.

    `sh_plugins_root`/`sh_consumer_root` (both default None) add the
    settings-home dual-read lane on top of the legacy `~/.claude` lane — see
    `_gather_sentinels` for the merge/conflict rule. Callers that only care
    about the legacy lane (e.g. existing unit tests) may omit them entirely.
    """
    lines: List[str] = []
    # Bash oracle iterates `for sentinel in "$PLUGINS_ROOT"/*/... "$CONSUMER_ROOT"/*/...`
    # — a two-pattern glob for-loop, NOT `find plugins_root consumer_root | sort`.
    # Bash glob expansion sorts matches WITHIN each pattern but does not merge
    # across patterns, so all plugins_root sentinels are emitted (in sorted
    # order) before any consumer_root sentinels, regardless of how the two
    # root path strings themselves compare. Reproduce that grouped-then-sorted
    # shape here — a global cross-root string-sort silently reorders output
    # whenever consumer_root's path string happens to sort ahead of
    # plugins_root's (e.g. ".../consumer" < ".../plugins"). The settings-home
    # lane (if any) is appended after the legacy lane, minus any legacy
    # entries it supersedes — see `_gather_sentinels`.
    sentinels = _gather_sentinels(plugins_root, consumer_root, sh_plugins_root, sh_consumer_root)

    for sentinel in sentinels:
        plugin_dir = sentinel.parent.parent
        plugin = plugin_dir.name

        try:
            data = json.loads(sentinel.read_text(encoding="utf-8"))
        except Exception as exc:
            if mode == "--red-and-stale":
                lines.append(
                    f"[health] {plugin}: sentinel unreadable at {sentinel} "
                    f"(malformed JSON?). Run /{plugin}:doctor."
                )
            else:
                print(
                    f"[health] {plugin}: sentinel unreadable at {sentinel} ({exc}) "
                    f"— suppressed in mode {mode}",
                    file=sys.stderr,
                )
            continue

        ran_at = str(data.get("ran_at", "") or "")
        verdict = str(data.get("verdict", "") or "")
        hint = str(data.get("hint", "") or "")
        plugin_field = str(data.get("plugin", "") or "")
        red_probes = data.get("red_probes") or []
        red_probes_str = ",".join(red_probes)

        if plugin_field:
            plugin = plugin_field

        age_days: Optional[int] = None
        stale = True
        if ran_at:
            try:
                epoch = int(datetime.fromisoformat(ran_at.strip().replace("Z", "+00:00")).timestamp())
            except Exception:
                epoch = None
            if epoch is not None:
                age_sec = int(now) - epoch
                age_days = age_sec // 86400
                stale = age_sec > stale_sec

        if verdict == "RED":
            probe_clause = f" ({red_probes_str})" if red_probes_str else ""
            hint_clause = f" — {hint}." if hint else ""
            lines.append(f"[health] {plugin}: doctor RED{probe_clause}{hint_clause} Run /{plugin}:doctor for details.")
        elif verdict == "AMBER":
            if mode == "--red-and-stale":
                hint_clause = f" — {hint}." if hint else ""
                if stale and age_days is not None:
                    lines.append(
                        f"[health] {plugin}: doctor AMBER ({age_days}d old){hint_clause} Run /{plugin}:doctor to re-probe."
                    )
                else:
                    lines.append(f"[health] {plugin}: doctor AMBER{hint_clause} Run /{plugin}:doctor to re-probe.")
        elif verdict in ("GREEN", ""):
            if mode == "--red-and-stale" and stale:
                if age_days is None:
                    lines.append(f"[health] {plugin}: doctor sentinel ran_at unparseable. Run /{plugin}:doctor.")
                else:
                    lines.append(f"[health] {plugin}: doctor stale (last run {age_days}d ago). Run /{plugin}:doctor.")
        else:
            if mode == "--red-and-stale":
                lines.append(f"[health] {plugin}: doctor unknown verdict '{verdict}'. Run /{plugin}:doctor.")

    return lines


def scan_absent_sentinels(plugins_root: Path, sh_plugins_root: Optional[Path] = None) -> List[str]:
    """Pass 2 — plugins declaring a doctor command with no sentinel ever written.

    PLUGINS_ROOT-scoped by design — see module docstring. Do not extend to a
    consumer root.

    `sh_plugins_root` (DR-072 dual-read, optional, default None) is consulted
    before declaring a sentinel absent — mirroring pass 1's
    settings-home-wins precedence (see `_gather_sentinels`). Without this, a
    plugin whose sentinel migrated to the settings-home lane while its
    install (`commands/doctor.md`) stays under legacy `plugins_root` would
    have pass 1 correctly read its settings-home verdict AND pass 2 falsely
    report it as never-run (legacy path absent because the sentinel moved),
    contradicting pass 1 in the same invocation's output.
    """
    lines: List[str] = []
    if not plugins_root.is_dir():
        return lines

    for plugin_dir in sorted((p for p in plugins_root.iterdir() if p.is_dir()), key=lambda p: p.name):
        plugin = plugin_dir.name
        if _is_backup_dir(plugin):
            continue

        doctor_md = _find_doctor_md(plugin_dir, plugin)
        if doctor_md is None:
            continue

        legacy_present = (plugins_root / plugin / _SENTINEL_REL).is_file()
        sh_present = (
            sh_plugins_root is not None
            and (sh_plugins_root / plugin / _SENTINEL_REL).is_file()
        )
        if not legacy_present and not sh_present:
            lines.append(f"[health] {plugin}: doctor has never run (sentinel absent). Run /{plugin}:doctor to bootstrap.")

    return lines


def _extract_hook_script_paths(hooks_json: Path, plugin_dir: Path) -> List[str]:
    """Missing-on-disk ${CLAUDE_PLUGIN_ROOT}-relative script paths in one hooks.json."""
    try:
        cfg = json.loads(hooks_json.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"[health] {plugin_dir.name}: {hooks_json} unreadable/malformed ({exc}) "
            "— skipping missing-hook-script check for this plugin",
            file=sys.stderr,
        )
        return []

    hooks = (cfg.get("hooks", {}) or {}).get("SessionStart", []) or []
    seen: List[str] = []
    missing: List[str] = []
    for group in hooks:
        for h in group.get("hooks", []) or []:
            cmd = h.get("command", "") or ""
            m = _HOOK_SCRIPT_RE.search(cmd)
            if not m:
                continue
            rel = m.group(1).lstrip("/")
            if rel in seen:
                continue
            seen.append(rel)
            target = plugin_dir / rel
            if not target.exists():
                missing.append(rel)
    return missing


def scan_missing_hook_scripts(plugins_root: Path) -> List[str]:
    """Pass 3 — SessionStart hooks.json entries whose referenced script is absent.

    PLUGINS_ROOT-scoped by design — see module docstring. Matches both the flat
    (`<plugin>/hooks/hooks.json`) and example-retrieval-repo-shape nested
    (`<plugin>/plugin/hooks/hooks.json`) layouts.
    """
    lines: List[str] = []
    if not plugins_root.is_dir():
        return lines

    hooks_jsons = _sorted_glob_excluding_backup(
        plugins_root, "*/hooks/hooks.json"
    ) + _sorted_glob_excluding_backup(plugins_root, "*/plugin/hooks/hooks.json")

    for hooks_json in hooks_jsons:
        rest = hooks_json.relative_to(plugins_root)
        plugin = rest.parts[0]
        plugin_dir = Path(str(hooks_json)[: -len("/hooks/hooks.json")])

        for rel in _extract_hook_script_paths(hooks_json, plugin_dir):
            lines.append(
                f"[health] {plugin}: SessionStart hook references missing script '{rel}' "
                f"(declared in hooks/hooks.json, not on disk — Claude Code silently "
                f"skips it). Re-run the plugin's installer or /coordinator:install."
            )

    return lines


def check_sentinel_presence(plugins_root: Path, sh_plugins_root: Optional[Path] = None) -> Optional[str]:
    """`--check-sentinel-presence` bootstrap notice for fresh installs.

    Returns a one-line notice when plugins are installed but no sentinel has
    ever been written anywhere — anywhere meaning across BOTH the legacy
    `plugins_root` lane and the settings-home plugins lane (DR-072
    dual-read), not the legacy lane alone; None when there is nothing to
    report (no plugins installed, or at least one sentinel exists in either
    lane).

    `sh_plugins_root` (optional, default None) is the settings-home mirror of
    `plugins_root` — see `_gather_sentinels`/`scan_absent_sentinels` for the
    same DR-072-awareness pattern applied to passes 1/2. Without it, a
    machine whose plugins all migrated their sentinels to the settings-home
    lane would have `sentinel_count == 0` in the legacy lane alone despite
    every plugin being freshly doctored, producing a false "no doctor
    sentinels found — run /coordinator:install" bootstrap nag on a
    correctly-configured machine.
    """
    if not plugins_root.is_dir():
        return None

    plugin_dirs = [p for p in plugins_root.iterdir() if p.is_dir()]
    installed_count = len(plugin_dirs)
    if installed_count == 0:
        return None

    sentinel_count = sum(1 for _ in plugins_root.glob(f"*/{_SENTINEL_REL}"))
    if sh_plugins_root is not None and sh_plugins_root.is_dir():
        sentinel_count += sum(1 for _ in sh_plugins_root.glob(f"*/{_SENTINEL_REL}"))
    if sentinel_count == 0:
        return (
            f"addon-health: no doctor sentinels found across {installed_count} "
            f"installed plugin(s) — run /coordinator:install and your plugin "
            f"doctors to bootstrap"
        )
    return None


def _run(mode: str) -> "tuple[List[str], int]":
    plugins_root, consumer_root, sh_plugins_root, sh_consumer_root = _resolve_roots()
    stale_sec = int(os.environ.get("COORDINATOR_HEALTH_STALE_SEC") or _DEFAULT_STALE_SEC)

    any_root_present = (
        plugins_root.is_dir()
        or consumer_root.is_dir()
        or (sh_plugins_root is not None and sh_plugins_root.is_dir())
        or (sh_consumer_root is not None and sh_consumer_root.is_dir())
    )
    if not any_root_present:
        return [], 0

    if mode == "--check-sentinel-presence":
        # settings-home-plugins-leg-aware (DR-072) — passing sh_plugins_root
        # so a fully-migrated machine (all sentinels in the settings-home
        # lane, none in legacy plugins_root) does not trigger a false
        # bootstrap nag; see check_sentinel_presence's docstring. Still
        # PLUGINS_ROOT-scoped in the CONSUMER sense — no consumer-root leg is
        # added here, matching pass 2's scope discipline.
        msg = check_sentinel_presence(plugins_root, sh_plugins_root)
        return ([msg] if msg else []), 0

    lines = scan_verdicts(
        plugins_root, consumer_root, mode, time.time(), stale_sec, sh_plugins_root, sh_consumer_root
    )
    if mode == "--red-and-stale":
        lines.extend(scan_absent_sentinels(plugins_root, sh_plugins_root))
        lines.extend(scan_missing_hook_scripts(plugins_root))
    return lines, 0


def main(argv: List[str]) -> int:
    mode = _DEFAULT_MODE
    if argv and argv[0]:
        if argv[0] in _MODES:
            mode = argv[0]
        else:
            print(
                f"{_PROG}: unknown mode '{argv[0]}' (expected --red-only, --red-and-stale, or --check-sentinel-presence)",
                file=sys.stderr,
            )
            return 2

    lines, exit_code = _run(mode)
    for line in lines:
        print(line)
    return exit_code


@register_op("plugin_health.scan")
async def _plugin_health_scan(params: dict, repo_root=None) -> dict:
    """JSON-RPC "plugin_health.scan" handler.

    Params: mode (optional str, one of --red-only/--red-and-stale/
    --check-sentinel-presence; defaults to --red-and-stale). repo_root is
    accepted for handler-signature parity but IGNORED — this op inspects the
    operator's OWN machine-local plugin/consumer roots (env-resolved), not the
    caller's repo (same "none"-scope class as plugin_health.drift).

    Returns {"exit_code": int, "lines": [...]}.
    """
    params = params or {}
    mode = str(params.get("mode") or _DEFAULT_MODE)
    if mode not in _MODES:
        raise ValueError(
            f"plugin_health.scan: unknown mode '{mode}' (expected one of {_MODES})"
        )
    lines, exit_code = _run(mode)
    return {"exit_code": exit_code, "lines": lines}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

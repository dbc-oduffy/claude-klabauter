"""
coordinator_core.ops.dev_sync — plugin dev-loop cache sync (source → CC cache).

Purpose: dev-tool port of `coordinator/dist/publish-repo-setup/dev-sync.sh`
(publish-repo `setup/dev-sync.sh`). Plugin developers run this after editing
plugin source files to make edits visible to a locally-installed Claude Code
without restarting or bumping the plugin version — Claude Code caches
installed plugins by version under `~/.claude/plugins/cache/<marketplace>/
<plugin>/<version>/`, and edits to source don't propagate there automatically.
This module bridges that gap by wiping and re-copying the per-plugin cache
directory tree from the repo's `plugins/` source tree.

Repo-root discovery: `REPO_ROOT` is the parent of the directory containing
the trampoline's `.sh` (i.e. `dirname(script)/..`) — mirrors the bash
oracle's `$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)`. Because this
module is a separate importable file (its own `__file__` lives in the
Claude-klabauter tree, not the trampoline's), the trampoline resolves its OWN
`REPO_ROOT` before importing and forwards it via the `DEV_SYNC_REPO_ROOT`
env var; this module falls back to `Path.cwd()` when the env var is absent
(direct/test invocation, matching the bash oracle's own `cd`-relative
resolution when sourced ad hoc).

Port source: coordinator/dist/publish-repo-setup/dev-sync.sh (coordinator-claude)
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Exit codes (parity-critical, preserved from the bash oracle):
    0 — sync completed (including all-SKIP runs — missing source dir, missing
        plugin.json, or unreadable version are non-fatal per-plugin SKIPs)
    1 — safety-guard trip only: `cache_target` computed empty, or resolved
        outside `$HOME/.claude/plugins/cache` — refuses to `rm -rf` and aborts

Negative-spec (retired/never-had patterns — do NOT reintroduce):
    - Does NOT touch `~/.claude/plugins/marketplaces/` — this module writes
      the CACHE tree only, never the install-manifest tree.
    - Does NOT bump `plugin.json` `version` — that's a separate, human-driven
      step; this script only mirrors whatever version is already declared.
    - Does NOT recurse into `plugins/*/` beyond one level — each immediate
      child of `plugins/` is treated as exactly one plugin directory,
      mirroring the bash oracle's `for dir in "$SOURCE_DIR"/*/`.
    - `.orphaned_at` marker preservation is a straight read-before-wipe /
      write-after-copy round trip — this module does NOT interpret or
      validate the marker's contents, only preserves the byte string.
    - Does NOT reproduce the bash oracle's `find ... | wc -l` leading-space
      padding on the SYNC line's file count — BSD-vs-GNU-`wc`-width-
      dependent cosmetic noise, never parsed downstream; see the coordinator-claude-side
      trampoline's matching negative-spec note.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

MARKETPLACE = "coordinator-claude"


def _read_version(plugin_json: Path) -> Optional[str]:
    """Read `.version` from plugin.json — mirrors the bash oracle's jq-then-sed
    fallback ladder (jq preferred when present, regex-scalar-extract fallback
    otherwise); here we always parse JSON proper since we're already in
    Python (a strictly more correct superset of both, same observable result
    on well-formed plugin.json)."""
    try:
        data = json.loads(plugin_json.read_text())
    except (OSError, json.JSONDecodeError):
        print(f"skip: _read_version: data = json.loads(plugin_json.read_text()) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    version = data.get("version")
    if not isinstance(version, str) or not version:
        return None
    return version


def _sync_plugin(name: str, source_dir: Path, cache_dir: Path) -> str:
    """Sync one plugin's source tree into its cache dir. Returns the report
    line (already prefixed, ready to print) — mirrors the bash oracle's
    per-plugin echo lines exactly (SKIP/NEW/SYNC/ERROR)."""
    src = source_dir / name
    if not src.is_dir():
        return f"  SKIP: {name} (source dir not found)"

    plugin_json = src / ".claude-plugin" / "plugin.json"
    if not plugin_json.is_file():
        return f"  SKIP: {name} (no .claude-plugin/plugin.json)"

    version = _read_version(plugin_json)
    if not version:
        return f"  SKIP: {name} (couldn't read version)"

    cache_target = cache_dir / name / version

    lines: List[str] = []
    if not cache_target.is_dir():
        lines.append(f"  NEW:  {name} ({version}) — creating cache dir")
        cache_target.mkdir(parents=True, exist_ok=True)

    orphaned_at_path = cache_target / ".orphaned_at"
    orphaned_at = orphaned_at_path.read_text() if orphaned_at_path.is_file() else None

    # Safety guard (issue #13): never rm -rf an empty path or a path outside
    # the expected plugins cache root.
    cache_target_str = str(cache_target)
    if not cache_target_str:
        print("  ERROR: cache_target is empty — refusing to clean. Aborting.", file=sys.stderr)
        sys.exit(1)
    expected_prefix = str(Path.home() / ".claude" / "plugins" / "cache")
    if not (cache_target_str == expected_prefix or cache_target_str.startswith(expected_prefix + os.sep)):
        print(
            f"  ERROR: cache_target ({cache_target_str}) is not under {expected_prefix} — "
            "refusing to clean. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)

    if cache_target.is_dir():
        for child in cache_target.iterdir():
            if child.name == ".orphaned_at":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    shutil.copytree(src, cache_target, dirs_exist_ok=True)

    if orphaned_at is not None:
        orphaned_at_path.write_text(orphaned_at)

    file_count = sum(1 for p in cache_target.rglob("*") if p.is_file())
    lines.append(f"  SYNC: {name} ({version}) — {file_count} files")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    repo_root = Path(os.environ.get("DEV_SYNC_REPO_ROOT") or Path.cwd()).resolve()
    source_dir = repo_root / "plugins"
    cache_dir = Path.home() / ".claude" / "plugins" / "cache" / MARKETPLACE

    target = argv[0] if argv else ""

    print(f"dev-sync: {source_dir} → cache")
    print("")

    if target:
        print(_sync_plugin(target, source_dir, cache_dir))
    else:
        if source_dir.is_dir():
            for entry in sorted(source_dir.iterdir()):
                if entry.is_dir():
                    print(_sync_plugin(entry.name, source_dir, cache_dir))

    print("")
    print("Done. Changes take effect on next Claude Code session (or next hook invocation for hooks).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

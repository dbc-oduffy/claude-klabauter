#!/usr/bin/env python3
# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""claude-ue-bootstrap.py <project-dir>

Drops <project-dir>/.claude/settings.json with the UE plugin enable block.
Idempotent: skips write if the override is already present; merges with
existing settings if a non-UE settings.json is already there.

Usage:
    python3 bin/claude-ue-bootstrap.py /x/example-sim-repo
    python3 bin/claude-ue-bootstrap.py /x/example-retrieval-repo
    python3 bin/claude-ue-bootstrap.py /x/example-game-workbench-repo
    python3 bin/claude-ue-bootstrap.py ~/.claude

Manual, example-doctrine-repo-owned per-project UE plugin gating helper — see
docs/wiki/per-project-plugin-gating.md. NOT auto-invoked by any hook or
ceremony (coordinator_core.hooks.ue_knowledge_distrust ported the SessionStart
auto-bootstrap write/merge logic natively into claude-klabauter's Python -- see that
module's `_run_bootstrap`, which retired the bash `["bash", script, cwd]`
subprocess spawn this script used to receive on the session-hot-path). This
script survives as the deliberate manual entrypoint the docs point users at.

Part of the 2026-07-19 Windows de-bash campaign
(docs/plans/2026-07-19-debash-coordinator-windows.md), chunk I-d. Fix-in-port
(DR-059): the prior bash oracle needed a jq-or-node ladder (with a raw error
exit when neither was on PATH) to merge JSON -- Python's stdlib `json` module
merges natively, so that entire fallback ladder is dead weight here and is
dropped. The `cygpath`-based MSYS-to-native path translation is also dropped:
it existed only to hand a Windows-form path to `jq`/`node`, external
processes this script no longer shells out to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ENABLED_PLUGINS = {
    "example-game-repo-control@example-game-workbench-repo": True,
    "example-game-repo@example-game-workbench-repo": True,
    "game-dev@example-game-workbench-repo": True,
    "game-dev@coordinator-claude": True,
    "example-retrieval-repo@example-retrieval-repo": True,
    "example-retrieval-repo-ue-addon@example-retrieval-repo-ue-addon": True,
}


def _already_overridden(settings: dict) -> bool:
    enabled = settings.get("enabledPlugins", {})
    return all(enabled.get(key) is True for key in ENABLED_PLUGINS)


def bootstrap(project_dir: str) -> tuple[bool, str]:
    """Write or merge the UE plugin override into <project_dir>/.claude/settings.json.

    Returns (changed, message) -- changed is False when the override was
    already fully present (message explains the no-op), True after a
    write/merge (message names the settings.json path and write mode).
    """
    project = Path(project_dir)
    claude_dir = project / ".claude"
    settings_path = claude_dir / "settings.json"
    claude_dir.mkdir(parents=True, exist_ok=True)

    if not settings_path.exists():
        payload = {"enabledPlugins": dict(ENABLED_PLUGINS)}
        tmp_path = settings_path.with_name(settings_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(settings_path)
        return True, f"wrote UE override to {settings_path}"

    existing = json.loads(settings_path.read_text(encoding="utf-8"))
    if _already_overridden(existing):
        return False, f"{settings_path} already carries UE override — no change"

    existing.setdefault("enabledPlugins", {})
    existing["enabledPlugins"].update(ENABLED_PLUGINS)
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(settings_path)
    return True, f"merged UE override into {settings_path}"


_USAGE = (
    "usage: claude-ue-bootstrap [PROJECT_DIR]\n"
    "\n"
    "Write (or merge) the UE plugin-override block into PROJECT_DIR/.claude/\n"
    "settings.json. PROJECT_DIR must already exist; defaults to the current\n"
    "directory when omitted. Surfaced as the remediation for doctor probe P-9."
)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Negative-spec: this CLI writes a directory tree at whatever it is handed,
    # so an unvalidated argv is a filesystem-pollution bug, not a usability one.
    # `--help` used to be taken as a project path: it created ./--help/.claude/
    # settings.json in the caller's cwd and exited 0 reporting success (observed
    # 2026-08-12, inside claude-klabauter). Reject flag-shaped and non-existent
    # targets rather than materialising them.
    if any(a in ("-h", "--help") for a in argv):
        print(_USAGE)
        return 0
    if len(argv) > 1:
        print(f"ERROR: expected at most one project directory, got {len(argv)}: "
              f"{argv}\n\n{_USAGE}", file=sys.stderr)
        return 2
    if argv and argv[0].startswith("-"):
        print(f"ERROR: unknown option {argv[0]!r}\n\n{_USAGE}", file=sys.stderr)
        return 2

    project_dir = argv[0] if argv else str(Path.cwd())
    if argv and not Path(project_dir).is_dir():
        # Only guard an explicitly-named target. The no-argv default is cwd,
        # which exists by construction.
        print(f"ERROR: not a directory: {project_dir}\n\n{_USAGE}", file=sys.stderr)
        return 2
    try:
        _changed, message = bootstrap(project_dir)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())

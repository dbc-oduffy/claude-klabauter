# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.

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
    project = Path(project_dir)
    claude_dir = project / ".claude"
    settings_path = claude_dir / "settings.json"
    claude_dir.mkdir(parents=True, exist_ok=True)

    if not settings_path.exists():
        payload = {"enabledPlugins": dict(ENABLED_PLUGINS)}
        tmp_path = settings_path.with_name(settings_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        tmp_path.replace(settings_path)
        return True, f"wrote UE override to {settings_path}"

    existing = json.loads(settings_path.read_text(encoding="utf-8"))
    if _already_overridden(existing):
        return False, f"{settings_path} already carries UE override — no change"

    existing.setdefault("enabledPlugins", {})
    existing["enabledPlugins"].update(ENABLED_PLUGINS)
    tmp_path = settings_path.with_name(settings_path.name + ".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8", newline="\n")
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

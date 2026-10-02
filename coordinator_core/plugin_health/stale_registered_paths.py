"""P-24 stale-registered-paths: report-only sweep of the absolute paths this
machine has registered (user LaunchAgents, `~/.claude.json`, registered repos'
`.mcp.json`, the coordinator registry) that no longer exist.

Zero spawns, no git, no writes: every source is read once with the stdlib.
"""

from __future__ import annotations

import json
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

from coordinator_core.machine_resolver import load_flat_registry_file

#: Registry keys whose values name a repo root that may carry a `.mcp.json`.
_REPO_KEY_PREFIXES = ("repos.", "engine.working_repos.")

#: Where a moved repo is looked for, relative to HOME.
_RELOCATION_ROOT = "X"


@dataclass(frozen=True)
class Finding:
    source: str
    key: str
    path: str
    hint: Optional[str] = None

    def render(self) -> str:
        text = f"{self.source} :: {self.key} -> {self.path} (missing)"
        return f"{text}; likely relocated to {self.hint}" if self.hint else text


def _path_shaped(value: object) -> bool:
    """Absolute, so a bare command resolved by PATH (`npx`, `uvx`) is never one."""
    return isinstance(value, str) and bool(value) and os.path.isabs(value)


def _relocation_hint(missing: str, home: Path) -> Optional[str]:
    """`<home>/X/<name>[/<rest>]` when an ancestor of `missing` was moved there.

    Prefers the deepest relocated path that exists, so a moved repo reports the
    file inside it rather than just its root.
    """
    path = Path(missing)
    root = home / _RELOCATION_ROOT
    for ancestor in (path, *path.parents):
        if len(ancestor.parts) < 3 or ancestor == home or ancestor == root:
            continue
        candidate = root / ancestor.name
        if candidate == ancestor:
            continue
        moved = candidate / path.relative_to(ancestor)
        if moved.exists():
            return str(moved)
    return None


def _read_json(path: Path, findings: List[Finding]) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        findings.append(Finding(str(path), "(unreadable)", str(path), f"{type(exc).__name__}"))
        return None
    return data if isinstance(data, dict) else None


def _mcp_candidates(servers: object, prefix: str) -> Iterator[tuple]:
    if not isinstance(servers, dict):
        return
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        base = f"{prefix}.{name}"
        yield f"{base}.command", spec.get("command")
        yield f"{base}.cwd", spec.get("cwd")
        args = spec.get("args")
        for i, arg in enumerate(args if isinstance(args, list) else []):
            yield f"{base}.args[{i}]", arg


def _plist_candidates(plist: dict) -> Iterator[tuple]:
    args = plist.get("ProgramArguments")
    for i, arg in enumerate(args if isinstance(args, list) else []):
        yield f"ProgramArguments[{i}]", arg
    yield "WorkingDirectory", plist.get("WorkingDirectory")
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = plist.get(key)
        if _path_shaped(value):
            yield f"{key}.parent", str(Path(value).parent)


def _candidates(home: Path, ml_dir: Path, platform: str, findings: List[Finding]) -> Iterator[tuple]:
    """Yield `(source file, key path, value)` for every registered path-valued field."""
    if platform == "darwin":
        agents = home / "Library" / "LaunchAgents"
        for plist_path in sorted(agents.glob("*.plist")) if agents.is_dir() else []:
            try:
                plist = plistlib.loads(plist_path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException) as exc:
                findings.append(Finding(str(plist_path), "(unreadable)", str(plist_path), type(exc).__name__))
                continue
            for key, value in _plist_candidates(plist):
                yield str(plist_path), key, value

    claude_json = home / ".claude.json"
    data = _read_json(claude_json, findings)
    if data is not None:
        yield from ((str(claude_json), k, v) for k, v in _mcp_candidates(data.get("mcpServers"), "mcpServers"))
        projects = data.get("projects")
        for project, body in (projects.items() if isinstance(projects, dict) else []):
            yield str(claude_json), f"projects.{project}", project
            if isinstance(body, dict):
                prefix = f"projects.{project}.mcpServers"
                yield from ((str(claude_json), k, v) for k, v in _mcp_candidates(body.get("mcpServers"), prefix))

    # The local file overrides the shared one, so a key is attributed to the file it came from.
    registry: dict = {}
    for name in ("registry.toml", "registry.local.toml"):
        for key, value in load_flat_registry_file(ml_dir / name).items():
            registry[key] = (ml_dir / name, value)
    for key, (origin, value) in registry.items():
        if _path_shaped(value):
            yield str(origin), key, value
            if key.startswith(_REPO_KEY_PREFIXES) and Path(value).is_dir():
                mcp_json = Path(value) / ".mcp.json"
                body = _read_json(mcp_json, findings)
                if body is not None:
                    yield from ((str(mcp_json), k, v) for k, v in _mcp_candidates(body.get("mcpServers"), "mcpServers"))


def find_stale(home: Path, ml_dir: Path, platform: Optional[str] = None) -> List[Finding]:
    """Every registered absolute path that does not exist, in source order."""
    findings: List[Finding] = []
    seen: set = set()
    for source, key, value in list(_candidates(home, ml_dir, platform or sys.platform, findings)):
        if not _path_shaped(value) or (source, key) in seen:
            continue
        seen.add((source, key))
        if not os.path.exists(value):
            findings.append(Finding(source, key, value, _relocation_hint(value, home)))
    return findings


def main(argv: Optional[List[str]] = None) -> int:
    """Print each finding; exit 1 when any path is stale, else 0."""
    from coordinator_core._settings_home import home_dir, machine_local_dir

    findings = find_stale(home_dir(), machine_local_dir())
    for finding in findings:
        print(finding.render())
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

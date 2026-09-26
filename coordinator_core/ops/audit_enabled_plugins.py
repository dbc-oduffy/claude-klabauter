from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

_UNIVERSAL_PLUGINS = frozenset(
    {"coordinator", "coordinator-claude", "deep-research", "deep-research-claude"}
)


def _tag_present(tags_padded: str, tag: str) -> bool:
    return f" {tag} " in tags_padded


def _is_justified(plugin: str, project_type: str, stack_tags: str) -> bool:
    if project_type == "meta":
        return True

    tags_padded = f" {stack_tags} "

    if plugin in _UNIVERSAL_PLUGINS:
        return True
    if plugin in {"game-dev", "example-game-repo", "example-game-repo-control", "example-game-repo-docs"}:
        return (
            _tag_present(tags_padded, "unreal-engine")
            or _tag_present(tags_padded, "game")
            or project_type == "game"
        )
    if plugin == "web-dev":
        return _tag_present(tags_padded, "web") or project_type == "web"
    if plugin == "data-science":
        return _tag_present(tags_padded, "data-science") or project_type == "data-science"
    if plugin == "mcp-server-dev":
        return _tag_present(tags_padded, "mcp-server") or _tag_present(tags_padded, "mcp-plugin")
    if plugin == "plugin-dev":
        return _tag_present(tags_padded, "claude-plugin")
    return False


def _extract_enabled_plugins(settings_path: str) -> List[str]:
    try:
        with open(settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        print(f"skip: _extract_enabled_plugins: with open(settings_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []
    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    return [key for key, value in enabled.items() if value is True]


def _extract_frontmatter_block(local_md_path: str) -> Optional[str]:
    try:
        with open(local_md_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        print(f"skip: _extract_frontmatter_block: with open(local_md_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    delim_count = 0
    body: List[str] = []
    for line in lines:
        if line.rstrip("\n") == "---":
            delim_count += 1
            if delim_count == 2:
                break
            continue
        if delim_count == 1:
            body.append(line.rstrip("\n"))
    if delim_count < 2:
        return None
    return "\n".join(body)


def _parse_project_type(fm: str) -> str:
    for line in fm.splitlines():
        m = re.match(r"^project_type:\s*(.*)$", line)
        if m:
            val = m.group(1).strip()
            val = val.strip("\"").strip("'")
            return val.strip()
    return ""


def _parse_stack_tags(fm: str) -> str:
    lines = fm.splitlines()
    prints: List[str] = []
    in_block = False
    for line in lines:
        if re.match(r"^stack_tags:", line):
            in_block = True
            prints.append(re.sub(r"^stack_tags:\s*", "", line))
            continue
        if in_block and re.match(r"^\s*-\s*", line):
            prints.append(re.sub(r"^\s*-\s*", "", line))
            continue
        if in_block and re.match(r"^\S", line):
            in_block = False
    joined = "".join(p + "\n" for p in prints)
    joined = joined.translate(str.maketrans("", "", '[]"'))
    joined = joined.replace(",", " ")
    joined = joined.replace("\n", " ")
    return joined


def _parse_frontmatter(local_md_path: str) -> Tuple[str, str]:
    if not os.path.isfile(local_md_path):
        return "", ""
    fm = _extract_frontmatter_block(local_md_path)
    if fm is None:
        return "", ""
    return _parse_project_type(fm), _parse_stack_tags(fm)


def _audit(repo_root: str) -> str:
    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    if not os.path.isfile(settings_path):
        return ""

    enabled_keys = _extract_enabled_plugins(settings_path)
    if not enabled_keys:
        return ""

    local_md_path = os.path.join(repo_root, "coordinator.local.md")
    project_type, stack_tags = _parse_frontmatter(local_md_path)

    findings: List[str] = []
    for key in enabled_keys:
        if not key:
            continue
        plugin, _, _marketplace = key.partition("@")
        if not _is_justified(plugin, project_type, stack_tags):
            pt_disp = project_type or "unset"
            tags_disp = stack_tags or "unset"
            findings.append(
                f"  - {key}: enabled, but not justified by "
                f"project_type='{pt_disp}' stack_tags='{tags_disp}'"
            )

    if not findings:
        return ""

    lines = [f"enabledPlugins drift advisory (repo: {repo_root}):"]
    lines.extend(findings)
    lines.append(
        "  Full uninstall = (1) remove enabledPlugins line from every project's settings.json,"
    )
    lines.append(
        "                   (2) remove entry from ~/.claude/plugins/installed_plugins.json,"
    )
    lines.append("                   (3) rm -rf ~/.claude/plugins/cache/<marketplace>/<plugin>/.")
    return "\n".join(lines) + "\n"


def main(argv: List[str]) -> int:
    repo_root = argv[0] if argv else os.getcwd()
    output = _audit(repo_root)
    if output:
        sys.stdout.write(output)
    return 0

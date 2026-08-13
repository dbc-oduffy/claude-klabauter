"""
coordinator_core.ops.audit_enabled_plugins

Port of: audit-enabled-plugins.sh (coordinator-claude b5a4192c, 2026-07-20).

Purpose: drift-checks a repo's `.claude/settings.json` `enabledPlugins` object against
that repo's `coordinator.local.md` frontmatter (`project_type` / `stack_tags`), per
state/lessons.md:302 (claude-central, 2026-05-14) — project-scoped `enabledPlugins: true`
entries drift silently across repos as plugin installs write a `true` line into the
active project's settings.json and never review it, and cross-contamination compounds
over months. Wired into `/workweek-complete` Step 4f as an advisory (never blocks the
ceremony).

Reads:
    <repo_root>/.claude/settings.json    (enabledPlugins object)
    <repo_root>/coordinator.local.md     (YAML frontmatter: project_type, stack_tags)

Returns (main(argv)):
    Prints one advisory block (or nothing) to stdout; ALWAYS returns 0 (advisory,
    never propagated to ceremony exit) — unchanged from the bash oracle's `exit 0` at
    every path including the "no settings.json" / "no enabled plugins" early-outs.

Justification rule table (`_is_justified`) is a faithful line-for-line port of the bash
oracle's `is_justified()` case statement — this is a deliberately hardcoded tuning
surface (comment in the oracle: "extend as new plugins land or drift cases surface"),
not a config file; extend the dict literal in place as new plugins land, mirroring the
bash oracle's own maintenance convention.

Negative-spec (faithfully reproduced bash-oracle quirks — do NOT "fix" mid-port):
    - `stack_tags` normalization can leave a double space between an inline `[a, b]`
      list's tags and any following block-list continuation lines (the bash oracle's
      `awk` prints the inline line, THEN appends each block-list `- tag` line, then
      joins with `tr '\n' ' '` — for pure inline lists this leaves a trailing space
      before the closing string comparison; the `" $tags "` substring-match convention
      in `_is_justified` absorbs this, so it is a display-only artifact, not a bug this
      port needs to close).
    - `meta` short-circuit (`project_type == "meta"` -> always justified, no per-plugin
      check) is preserved verbatim — this is documented is-by-design in the oracle's own
      comment ("~/.claude is the orchestration brain ... audit on meta is a no-op by
      design"), not something to special-case away.
    - No settings.json, or an unparseable/empty enabledPlugins, is a SILENT no-op
      (prints nothing, exits 0) — not an error. Faithfully reproduced.

Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

# plugin -> justification predicate. Each predicate takes (project_type, stack_tags_str)
# and returns True iff the plugin's presence is justified for this repo. `stack_tags_str`
# is a single space-joined, space-padded string (matches the bash oracle's `" $tags "`
# substring-match convention) so callers here use the same `" tag "` substring test.
_UNIVERSAL_PLUGINS = frozenset(
    {"coordinator", "coordinator-claude", "deep-research", "deep-research-claude"}
)


def _tag_present(tags_padded: str, tag: str) -> bool:
    return f" {tag} " in tags_padded


def _is_justified(plugin: str, project_type: str, stack_tags: str) -> bool:
    """Faithful port of the bash oracle's is_justified() case statement."""
    # Meta short-circuit: ~/.claude is the orchestration brain where cross-cutting
    # work happens; all plugins are intentionally enabled here per global CLAUDE.md.
    # Audit on meta is a no-op by design.
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
        # Note: project_type=meta is handled by the short-circuit above; the only
        # remaining justification path here is the claude-plugin stack tag.
        return _tag_present(tags_padded, "claude-plugin")
    # Unknown plugin — surface for triage rather than silently OK.
    return False


def _extract_enabled_plugins(settings_path: str) -> List[str]:
    """Return enabled plugin '<plugin>@<marketplace>' keys, or [] on any read/parse failure."""
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
    """Return the text between the first two '---' lines, or None if absent/malformed."""
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
    """Mirror the bash oracle's awk block byte-for-byte, including its `print`-per-
    matched-line behavior (each match appends its transformed content PLUS a
    newline, awk's implicit ORS) before the `tr -d '[]"' | tr ',' ' ' | tr '\\n' ' '`
    pipeline runs. This deliberately reproduces the oracle's leading/trailing-space
    quirks for both the inline-list and block-list forms (see module negative-spec)
    — do NOT "clean up" the spacing here without also verifying the bash oracle
    still produces the ugly version (it does, as of this port)."""
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
    """Return (project_type, stack_tags); ("", "") if local_md_path missing or unparseable."""
    if not os.path.isfile(local_md_path):
        return "", ""
    fm = _extract_frontmatter_block(local_md_path)
    if fm is None:
        return "", ""
    return _parse_project_type(fm), _parse_stack_tags(fm)


def _audit(repo_root: str) -> str:
    """Return the advisory text (possibly empty) for repo_root, mirroring the bash oracle."""
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
    """CLI entrypoint: audit-enabled-plugins [repo-root] (default repo-root = cwd).

    Always returns 0 — advisory only, never propagated to ceremony exit (matches the
    bash oracle's unconditional `exit 0` at every path).
    """
    repo_root = argv[0] if argv else os.getcwd()
    output = _audit(repo_root)
    if output:
        sys.stdout.write(output)
    return 0

"""coordinator_core/agent_frontmatter.py — shared parser for Claude agent
`.md` definition files: frontmatter/body split, `tools:` frontmatter
parsing, declared-mcp-tool extraction, and ToolSearch `select:` bootstrap
detection.

WHY THIS EXISTS. DoE-claude's `coordinator/tests/test_agent_mcp_tool_adoption.py` and
`coordinator/tests/test_agent_tools_no_phantom_tools.py` both parsed the
same agent-frontmatter shape independently — two hand-rolled copies of one
parser, in one repo. Example-retrieval-repo has already landed a third hand-rolled
copy as an interim measure for its own `plugin/agents/` gate, explicitly
marked replaceable; example-game-repo would make a fourth independent copy the next
time it needs the same gate coverage. This module is the shared
implementation every consumer gate imports, ordinarily now (`from
coordinator_core.agent_frontmatter import ...`) rather than by
`importlib.util.spec_from_file_location` — this file lives inside the
engine package, which DoE-claude's own copy of it never did.

Negative-spec: no CLI, no path resolution, no `__file__`-derived lookup — this module is pure
data-in, data-out over text a caller already read, which is why it moved verbatim
(`docs/plans/2026-09-18-doe-holds-no-scripts.md` § Path resolution, engine class).

Spec backlink: cross-repo/inbox/2026-07-28-example-retrieval-repo-em-agent-frontmatter-parse-gate-duplication.md,
docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W3-C2.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

FRONTMATTER_DELIM = "---"

TOOLS_LINE = re.compile(r"^tools:\s*(.+)$", re.MULTILINE)

# Matches the value portion of a ToolSearch `select:` bootstrap query, e.g.
# `select:mcp__foo__bar,mcp__foo__baz` inside a quoted string. Tool names use
# word chars, hyphens, and underscores; commas separate multiple entries.
SELECT_RE = re.compile(r"select:([A-Za-z0-9_,\-]+)")


def agent_files(dirs: Iterable[Path]) -> list[Path]:
    """Every `*.md` file across a directory list, sorted per-directory,
    de-duplicated across directories, silently skipping any directory that
    doesn't exist. The directory-LIST parameterization is the seam a
    consumer repo uses to register its own agent directory (e.g.
    `plugin/agents/`) alongside — or instead of — coordinator's own
    `coordinator/agents/`."""
    files: dict[Path, None] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            files.setdefault(f)
    return list(files)


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split a `.md` agent file into (frontmatter, body) on the `---` fence.

    Returns `("", text)` when the file has no opening `---` fence, so callers
    degrade to "no frontmatter" rather than raising.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return "", text
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            frontmatter = "".join(lines[: i + 1])
            body = "".join(lines[i + 1 :])
            return frontmatter, body
    # Unterminated fence — treat the whole file as frontmatter-less body
    # rather than matching against its own frontmatter as body.
    return "", text


def parse_tools_line(raw: str) -> list[str]:
    """Parse the value portion of a `tools:` frontmatter line into tool
    names. Handles both shapes seen across agent definitions: a quoted YAML
    array (`["Read", "Bash"]`) and an unquoted comma-separated string
    (`Read, Write, Bash`)."""
    value = raw.strip()
    if value.startswith("["):
        inner = value.strip("[]")
        entries = [e.strip().strip("'\"") for e in inner.split(",")]
    else:
        entries = [e.strip().strip("'\"") for e in value.split(",")]
    return [e for e in entries if e]


def declared_tools(frontmatter: str) -> list[str]:
    """All `tools:` frontmatter entries. `[]` when the file has no
    `tools:` key (a valid, unrestricted-surface shape — not an error)."""
    match = TOOLS_LINE.search(frontmatter)
    if match is None:
        return []
    return parse_tools_line(match.group(1))


def declared_mcp_tools(frontmatter: str) -> list[str]:
    """The `mcp__*`-prefixed subset of `declared_tools`."""
    return [t for t in declared_tools(frontmatter) if t.startswith("mcp__")]


def bootstrap_tool_names(body: str) -> set[str]:
    """Every tool name named in a ToolSearch `select:` bootstrap string
    anywhere in the BODY (never the frontmatter — see `split_frontmatter`)."""
    names: set[str] = set()
    for match in SELECT_RE.finditer(body):
        names.update(name for name in match.group(1).split(",") if name)
    return names

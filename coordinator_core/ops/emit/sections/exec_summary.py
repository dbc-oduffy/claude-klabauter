"""Section porter — ExecSummary (envelope key: ``exec_summaries``).

Emits at most one ExecSummary record from the singleton markdown artifact
``$ROOT/docs/exec-summary.md``. Collection is a direct file read (NOT query-records.js —
this is a single frontmatter-bearing markdown file, not a frontmatter-indexed directory).
The record carries the two MANAGED fence bodies (``identity``, ``progress``) and the two
HAND fence bodies (``special`` → ``what_makes_it_special``, ``goals`` → ``near_term_goals``),
plus flat frontmatter fields (``project``, ``generated``, ``generator``). ``id`` and ``repo``
are always the injected META-REPO slug (``ctx.repo_name``); a read/parse failure quarantines
into ``malformed_records.exec_summaries``. Graceful absent — no ``docs/exec-summary.md`` →
``([], [])``, never a failure.

Port of: emit-cockpit-snapshot.sh (coordinator-claude 07eedcfb, 2026-07-19) — § SECTION 8.17,
  ExecSummary (the embedded python3 heredoc). Byte/semantic parity port — the heredoc's
  frontmatter parse, fence extraction, record shape, and quarantine rule are reproduced
  in-process here.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P19
"""

from __future__ import annotations

import re

from coordinator_core.ops.emit.context import EmitContext

_REL_PATH = "docs/exec-summary.md"


def _parse_frontmatter(content: str) -> dict:
    """Extract flat YAML frontmatter between the first two ``---`` lines (bash:2508-2534).

    Returns a dict of flat ``key: value`` pairs — no PyYAML dependency. Quoted scalars are
    unwrapped; the sentinels ``null`` / ``~`` / empty become ``None``.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    result: dict = {}
    for line in lines[1:end]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        if len(val) >= 2 and (
            (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
        ):
            val = val[1:-1]
        if val in ("null", "~", ""):
            val = None
        result[key] = val
    return result


def _extract_section(content: str, fence_type: str, name: str):
    """Extract the body between HTML-comment fences (bash:2536-2558).

    ``fence_type`` is ``MANAGED`` or ``HAND``; ``name`` is ``identity`` / ``progress`` /
    ``special`` / ``goals``. Returns the stripped body, or ``None`` when the fences are
    absent OR present-but-empty.
    """
    begin_marker = f"<!-- BEGIN {fence_type}: {name} -->"
    end_marker = f"<!-- END {fence_type}: {name} -->"
    in_section = False
    body_lines: list[str] = []
    for line in content.splitlines():
        if line == begin_marker:
            in_section = True
            continue
        if line == end_marker:
            in_section = False
            continue
        if in_section:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return body or None


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the ExecSummary record + quarantine list (parity: bash SECTION 8.17 heredoc)."""
    valid: list[dict] = []
    malformed: list[dict] = []

    fpath = ctx.repo_root / "docs" / "exec-summary.md"
    # Graceful absent (bash: `if [[ -f "$_EXEC_SUMMARY_FILE" ]]`).
    if not fpath.is_file():
        return valid, malformed

    try:
        content = fpath.read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        malformed.append({"path": _REL_PATH, "reason": f"read/parse error: {e}"})
        return valid, malformed

    # id and repo are always derivable from the injected META-REPO slug (bash:2568-2569).
    id_val = ctx.repo_name
    repo_val = ctx.repo_name

    project_val = fm.get("project")
    generated_val = fm.get("generated")
    generator_val = fm.get("generator")

    identity_val = _extract_section(content, "MANAGED", "identity")
    progress_val = _extract_section(content, "MANAGED", "progress")
    what_makes_it_special = _extract_section(content, "HAND", "special")
    near_term_goals = _extract_section(content, "HAND", "goals")

    valid.append({
        "repo": repo_val,
        "coordinator_root_path": ".",
        "id": id_val,
        "provenance": ctx.provenance("coordinator_artifact", path=_REL_PATH, derivation="parsed"),
        "project": project_val if isinstance(project_val, str) else None,
        "generated": generated_val if isinstance(generated_val, str) else None,
        "generator": generator_val if isinstance(generator_val, str) else None,
        "identity": identity_val,
        "progress": progress_val,
        "what_makes_it_special": what_makes_it_special,
        "near_term_goals": near_term_goals,
    })

    return valid, malformed

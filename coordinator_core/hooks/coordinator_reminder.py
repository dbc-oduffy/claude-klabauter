"""
coordinator_core.hooks.coordinator_reminder -- SessionStart(startup|clear|compact) reminder text.

Purpose: renders the light-touch "Quick Orient" heredoc + capability-catalog for
all project repos. Ported 1:1 from the retired
coordinator/hooks/scripts/coordinator-reminder.sh (deleted 2026-07-16, example-doctrine-repo
``d39ab164``), with ONE intentional deletion:
the dead PROJECT_TYPE/PROJECT_SUBTYPES coordinator.local.md frontmatter parse
-- verified dead by full-file read; neither variable is
referenced anywhere later in the bash source (the heredoc and the
capability-catalog read use neither). See
X:/example-doctrine-repo/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md
Sec 2.4.

Transport: plain synchronous function, imported directly and called in-process by
the example-doctrine-repo-side thin stub (coordinator/hooks/scripts/coordinator-reminder.py),
mirroring the preuse-write-dispatch.py -> write_guards.engine transport shape --
NOT the register_op()/IPC resident-dispatch path used by coordinator_core.hooks'
Pre/PostToolUse siblings (this hook is not on that resident-engine dispatch path;
Example-doctrine-repo resolves claude-klabauter-root and imports this module directly, same as
write_guards.engine.evaluate_payload_json).

Contract:
    render_reminder(catalog_path) -> str
        catalog_path: path to capability-catalog.md (str | Path | None). If the
        file is missing, unreadable, or catalog_path is falsy, only the static
        heredoc section is returned -- mirrors the bash `if [ -f "$CATALOG" ]`
        guard (silent skip, no error).

        HTML-comment lines (lines whose first character is "<!--", anchored at
        column 0, no leading-whitespace tolerance) are dropped from the catalog
        output -- mirrors grep -v '^<!--' exactly. A line that merely CONTAINS
        "<!--" after leading whitespace is NOT stripped (matches grep's anchored
        regex, not a substring test).

Output is byte-identical to the bash oracle for a given catalog_path input --
this hook has zero state, zero git calls, zero frontmatter parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Union

_QUICK_ORIENT = "## Quick Orient (always, before first tool call)\nBefore responding to the user's opening message, silently read `state/orientation_cache.md` if it exists and isn't already in context. Don't announce it. Do NOT read `state/lessons.md` at boot — it's a capture queue processed by `/learn-lessons`, not a must-see memo (load-bearing lessons live in `docs/wiki/` and are surfaced on demand by the prior-art-checker). If the user's message is vague/strategic/implies continuation, invoke `/workstream-start` for the full ceremony (which deliberately surveys lessons — PM-invoked, not EM-judged). If it's a specific actionable request, quick orient and go.\n\n## Coordinator Infrastructure\nAvailable for complex work:\n- /review (plan artifacts) or /review-code (code artifacts) — route artifacts to domain + architecture reviewers\n- /enrich-and-review — enrich specs with codebase research\n- For executor dispatch, follow docs/wiki/delegate-execution.md\nUse these when they add value. For direct requests, just do the work.\n"


def render_reminder(catalog_path: Optional[Union[str, "Path"]] = None) -> str:
    """Render the reminder text: static heredoc + optional stripped capability catalog.

    Mirrors coordinator-reminder.sh exactly (post dead-code deletion of
    the PROJECT_TYPE/PROJECT_SUBTYPES frontmatter loop -- confirmed dead,
    see module docstring).
    """
    out = [_QUICK_ORIENT]
    if catalog_path:
        p = Path(catalog_path)
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"coordinator_reminder: cannot read catalog {p}: {exc}", file=sys.stderr)
                text = None
            if text is not None:
                kept = [
                    line
                    for line in text.splitlines(keepends=True)
                    if not line.startswith("<!--")
                ]
                out.append("".join(kept))
    return "".join(out)

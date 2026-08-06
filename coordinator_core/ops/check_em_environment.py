"""
coordinator_core.ops.check_em_environment — EM effort/model drift banner.

Purpose: invoked as a STEP inside the start ceremonies (/workstream-start,
/workday-start, /workweek-start), NOT as a hook. Verifies the EM is running on
the expected EFFORT (medium) and, best-effort, MODEL (Opus), and prints a loud
banner on drift. Silent on a clean env. Always returns 0.

Why a skill step, not a hook: a UserPromptSubmit hook would fire before every
prompt (overhead + the documented Windows/Git-Bash stdin-hang that moved
context-pressure-advisory off that event in 2026-03). A SessionStart hook fires
on every session start — noise. Baking the check into the three "start"
ceremonies runs it exactly when the EM is deliberately orienting, with zero
per-prompt cost. The skill relays any banner this prints; clean env prints
nothing.

Division of labour (why model is "best-effort" but effort is authoritative):
  - EFFORT is the token pit: Anthropic periodically bumps the *default*
    effort, so an unpinned/bumped effort silently inflates cost. The EM
    CANNOT observe effort — it shows only in the CLI startup banner ("Opus 4.8
    with medium effort"), never in the system prompt or transcript.
    settings.json `effortLevel` is the only mechanically-readable anchor, so
    this module is the SOLE guard.
  - MODEL the EM CAN self-check from its own system prompt, so the skill
    carries a one-line EM-side confirmation as the reliable path. This module
    also reads the model from the session transcript as a best-effort second
    line of defence, but degrades silently if it can't resolve the transcript.

Port of: check-em-environment.sh (example-doctrine-repo 894d4bc6, 2026-07-22)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - Effort resolution reads the FIRST matching settings file in Claude
      Code's precedence order (project-local > project > user-local > user),
      then stops — it does NOT merge/overlay settings across files, mirroring
      the bash oracle's `read_effort` + `break`-on-first-hit loop.
    - Model check is best-effort only: an unresolvable transcript (empty
      CLAUDE_CODE_SESSION_ID, missing file) degrades to SILENT for the model
      axis, never an error — matches the bash oracle's `latest_model` guard.
    - Always returns 0, even on drift — this check must never block a
      ceremony. Reproduces the bash oracle's unconditional `exit 0`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional


def _read_effort(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"skip: _read_effort: data = json.loads(path.read_text(encoding=\"utf-8\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("effortLevel")
    if isinstance(value, str) and value:
        return value
    return None


def _resolve_effort(proj: Path, user_claude: Path) -> tuple[Optional[str], Optional[Path]]:
    candidates = [
        proj / ".claude" / "settings.local.json",
        proj / ".claude" / "settings.json",
        user_claude / "settings.local.json",
        user_claude / "settings.json",
    ]
    for candidate in candidates:
        value = _read_effort(candidate)
        if value:
            return value, candidate
    return None, None


_MODEL_RE = re.compile(r'"model":"([^"]*)"')


def _latest_model(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _latest_model: text = path.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    matches = _MODEL_RE.findall(text)
    return matches[-1] if matches else ""


def _resolve_transcript(explicit: str, user_claude: Path, session_id: str) -> str:
    if explicit:
        return explicit
    if not session_id:
        return ""
    projects_dir = user_claude / "projects"
    if not projects_dir.is_dir():
        return ""
    name = f"{session_id}.jsonl"
    # maxdepth 2 mirrors the bash oracle's `find ... -maxdepth 2 -name`: the
    # projects dir itself is depth 0, so matches live at depth 1 (directly
    # under projects/) or depth 2 (one subdir down) — `head -1` picks the
    # first hit in find's (unordered) traversal; sorted() here is a
    # deterministic stand-in for that "first hit" semantic.
    hits: List[Path] = []
    try:
        hits.extend(projects_dir.glob(name))
        hits.extend(projects_dir.glob(f"*/{name}"))
    except OSError:
        print(f"skip: _resolve_transcript: hits.extend(projects_dir.glob(name)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    for cand in sorted(hits):
        if cand.is_file():
            return str(cand)
    return ""


def _parse_argv(argv: List[str]) -> str:
    transcript = ""
    i = 0
    while i < len(argv):
        if argv[i] == "--transcript" and i + 1 < len(argv):
            transcript = argv[i + 1]
            i += 2
        else:
            i += 1
    return transcript


def main(argv: List[str]) -> int:
    explicit_transcript = _parse_argv(argv)

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    user_claude = Path(home) / ".claude" if home else Path(".claude")
    proj = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or os.getcwd())

    effort, effort_source = _resolve_effort(proj, user_claude)

    effort_warn = ""
    if not effort:
        effort_warn = "unpinned"
    elif effort != "medium":
        effort_warn = effort

    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    transcript = _resolve_transcript(explicit_transcript, user_claude, session_id)

    current_model = _latest_model(Path(transcript)) if transcript else ""

    model_warn = ""
    if current_model and "opus" not in current_model:
        model_warn = current_model

    if not model_warn and not effort_warn:
        return 0

    lines = []
    lines.append("")
    lines.append("╔══════════════════════════════════════════════════════════════════╗")
    lines.append("║  ⚠  EM ENVIRONMENT DRIFT")
    if model_warn:
        lines.append("║")
        lines.append(f"║  MODEL: transcript shows {model_warn}, not Opus.")
        lines.append("║    → WARN the PM in your first response; recommend /model back to Opus.")
    if effort_warn:
        lines.append("║")
        if effort_warn == "unpinned":
            lines.append("║  EFFORT: not pinned in any settings.json — runtime follows Anthropic's")
            lines.append("║    default, which drifts upward (a token pit). Expected: medium.")
            lines.append('║    → Recommend pinning "effortLevel": "medium" in settings.json.')
        else:
            source_display = str(effort_source) if effort_source else ""
            if home and source_display.startswith(home):
                source_display = "~" + source_display[len(home):]
            lines.append(f"║  EFFORT: pinned to '{effort_warn}', not medium  ({source_display}).")
            lines.append("║    → WARN the PM; medium is the cost-calibrated default for EM work.")
    lines.append("╚══════════════════════════════════════════════════════════════════╝")
    lines.append("")

    print("\n".join(lines))
    return 0


# Review: code-reviewer — add standalone-CLI guard for consistency with every
# other module in this port batch (Finding 5).
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
coordinator_core.hooks.nudge_unauthorized_handoff — PostToolUse advisory hook op.

Purpose: When a new file is Written into state/handoffs/ or tasks/spinoffs/ WITHOUT
an authoring skill active, surface an offer-shaped nudge to the model via
additionalContext (PostToolUse — the file is already on disk; nothing is blocked).

Translation notes:
    - bash PostToolUse exit-2+stderr → post_advisory(msg) (D2 shape d).
    - COORDINATOR_HANDOFF_NUDGE_OFF env-hatch → session-scoped sentinel (D6 re-plumb);
      session_id not yet in pinned inputs — silence check skipped pending pcore-04 D6.
    - Transcript-tail authoring-skill check is best-effort noise-reducer (fail-open):
      a missed suppress costs one extra nudge; the hook never blocks, so fail-open is
      the correct posture (design-as-offers doctrine).
    - kind:recovery and install-leg spinoff suppression use first-class frontmatter
      signals (the artifact's own content), not the proxy scrape.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C6
"""

from __future__ import annotations

import re
import sys

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks._payload import field

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# All <command-name>...</command-name> tags — extract the last active skill (check 1).
_ALL_CMD_TAG_RE = re.compile(r"<command-name>[^<]*</command-name>")

# Authoring-skill <command-name> tag filter (bash check 1 in source:159).
_AUTHORING_CMD_TAG_RE = re.compile(
    r"<command-name>/?(coordinator:)?(handoff|workstream-complete|spinoff)</command-name>"
)

# Generous-tail authoring-skill pattern (bash check 2, source:179).
# Matches: /handoff, /coordinator:handoff, coordinator:handoff, Skill(coordinator:handoff)
# and the workstream-complete / spinoff variants of each.
_AUTHORING_TAIL_RE = re.compile(
    r"(^|[^a-z])/(coordinator:)?(handoff|workstream-complete|spinoff)([^a-z]|$)"
    r"|(^|[^a-z:/])coordinator:(handoff|workstream-complete|spinoff)([^a-z]|$)"
    r"|Skill\((coordinator:)?(handoff|workstream-complete|spinoff)\)",
    re.MULTILINE,
)

# Frontmatter: kind: recovery  (source:130)
_KIND_RECOVERY_RE = re.compile(r"^kind:\s*recovery\s*$", re.MULTILINE)

# Frontmatter: kind: spinoff  (source:143)
_KIND_SPINOFF_RE = re.compile(r"^kind:\s*spinoff\s*$", re.MULTILINE)

# Frontmatter: install_chain_order: <digit>  (source:144)
_INSTALL_CHAIN_ORDER_RE = re.compile(r"^install_chain_order:\s*[0-9]", re.MULTILINE)

# ---------------------------------------------------------------------------
# Nudge message template (mirrors nudge-unauthorized-handoff.sh).
# ---------------------------------------------------------------------------

_NUDGE_MSG_TEMPLATE = """\
[nudge] File written under {parent_dir}/, no /handoff, /workstream-complete, or /spinoff
[nudge] active. Nudge, not a block — proceed if deliberate.
[nudge] "Done"? /workstream-complete (or commit+stop) — /handoff is IN-FLIGHT only.
[nudge] Another repo's EM? Use cross-repo-memo.
[nudge] Genuine handoff/fork? /handoff or /spinoff (PM-gated).
[nudge] Correct as-is (recovery, /pickup, review)? Ignore this."""

# ---------------------------------------------------------------------------
# Blocking helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _authoring_skill_active_sync(transcript_path: str) -> bool:
    """Blocking check: is a handoff-authoring skill currently active in the transcript?

    Two-pass scrape (mirrors nudge-unauthorized-handoff.sh):
      (1) Locate the most-recent <command-name> tag in the full transcript; if it names
          an authoring skill (handoff / workstream-complete / spinoff), suppress.
      (2) Generous-tail fallback (last 2000 lines): catches Skill-tool JSON invocations
          and user-typed slash commands that emit no <command-name> tag.

    Returns False on any I/O error — fail-open is the correct posture here.
    """
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            full_content = fh.read()
    except OSError as exc:
        print(f"nudge_unauthorized_handoff: cannot read transcript {transcript_path}: {exc} (fail-open)", file=sys.stderr)
        return False

    if not full_content:
        return False

    # (1) Most-recent <command-name> tag (bash: grep -oE '...' | tail -1 | grep -qE '...').
    all_cmd_tags = _ALL_CMD_TAG_RE.findall(full_content)
    if all_cmd_tags:
        last_cmd = all_cmd_tags[-1]
        if _AUTHORING_CMD_TAG_RE.match(last_cmd):
            return True

    # (2) Generous-tail fallback — read the last 2000 lines.
    # Using in-memory splitlines avoids the `tail | grep -q` SIGPIPE trap from the bash
    # original (nudge-unauthorized-handoff.sh — fixed by reading into a variable).
    all_lines = full_content.splitlines()
    tail_lines = all_lines[-2000:] if len(all_lines) > 2000 else all_lines
    tail = "\n".join(tail_lines)
    if _AUTHORING_TAIL_RE.search(tail):
        return True

    return False


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


async def advisory_text(
    tool_name: str,
    file_path: str,
    content: str,
    transcript_path: str,
) -> str:
    """Produce the unauthorized-handoff nudge text, or "" when the nudge stays silent.

    The single source of truth for this advisory's predicate and message. Two
    callers: this module's own `hooks.nudge_unauthorized_handoff` op (envelope
    wrapper below), and `postuse_advisory_dispatch`'s merged PostToolUse
    dispatcher, which folds this text in as a fourth advisory rather than paying
    a second interpreter start for a separate hooks.json registration.

    Never raises — every suppression path returns "", and the transcript scrape
    is itself fail-open.

    Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C6
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    # Only fires on Write (belt-and-braces; matcher is Write-only in hooks.json,
    # and the merged dispatcher is universal — this gate is what narrows it).
    if tool_name != "Write":
        return ""

    if not file_path:
        return ""

    # Normalize path separators (Windows backslash → forward slash).
    file_path_norm = file_path.replace("\\", "/")

    # Only fire on state/handoffs/ and tasks/spinoffs/ paths (bash case:117-122).
    is_handoff = (
        "/state/handoffs/" in file_path_norm
        or file_path_norm.startswith("state/handoffs/")
    )
    is_spinoff = (
        "/tasks/spinoffs/" in file_path_norm
        or file_path_norm.startswith("tasks/spinoffs/")
    )
    if not (is_handoff or is_spinoff):
        return ""

    # TODO(pcore-04 D6): needs session_id for the handoff-nudge-off sentinel.
    # COORDINATOR_HANDOFF_NUDGE_OFF env-hatch is re-plumbed to a session-scoped sentinel;
    # session_id is not yet in pinned inputs — skip the silence check until D6 lands.

    # First-class suppression: kind:recovery in leading frontmatter (bash:130-132).
    # Uses the artifact's OWN content — a reliable positive signal, not a proxy scrape.
    if content:
        leading = "\n".join(content.splitlines()[:20])
        if _KIND_RECOVERY_RE.search(leading):
            return ""

        # First-class suppression: install-leg spinoff (bash:143-146).
        # kind:spinoff AND install_chain_order:<digit> in the leading frontmatter.
        if _KIND_SPINOFF_RE.search(leading) and _INSTALL_CHAIN_ORDER_RE.search(leading):
            return ""

    # Best-effort: suppress when an authoring skill is active (bash:153-182).
    # NOISE-REDUCER only — fail-open (a missed suppress → one extra nudge, harmless).
    if transcript_path:
        active = await asyncio.to_thread(_authoring_skill_active_sync, transcript_path)
        if active:
            return ""

    # Derive parent directory for the nudge message (mirrors bash: dirname "$FILE_PATH_NORM").
    slash_idx = file_path_norm.rfind("/")
    parent_dir = file_path_norm[:slash_idx] if slash_idx >= 0 else file_path_norm

    return _NUDGE_MSG_TEMPLATE.format(parent_dir=parent_dir)


@register_op("hooks.nudge_unauthorized_handoff")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse advisory: nudge when a handoff/spinoff file is Written without a skill.

    Envelope wrapper over `advisory_text` — predicate, suppressions and message
    all live there, shared with `postuse_advisory_dispatch`'s merged dispatcher.

    Fires on:   Write tool with file_path under state/handoffs/ or tasks/spinoffs/.
    Returns:    post_advisory(msg) — feeds context to the model; does NOT block.
    Suppressed: kind:recovery frontmatter; install-leg spinoff frontmatter;
                active authoring skill detected in transcript;
                session-scoped nudge-off sentinel (skipped — see D6 TODO).

    Negative-spec:
        DOES NOT block — PostToolUse means the file is already written.
        DOES NOT read COORDINATOR_HANDOFF_NUDGE_OFF from os.environ directly;
        that env-hatch is re-plumbed to a session-scoped sentinel (D6). Because
        session_id is not yet in the pinned inputs, the silence check is skipped
        entirely until D6 lands.
        STAYS REGISTERED after the postuse fold — the standalone op is the
        contract for any caller dispatching this method directly; only DoE's
        separate hooks.json registration retires.

    Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C6
    """
    text = await advisory_text(
        field(params, "tool_name"),
        field(params, "file_path"),
        field(params, "content"),
        field(params, "transcript_path"),
    )
    return post_advisory(text) if text else no_advisory()

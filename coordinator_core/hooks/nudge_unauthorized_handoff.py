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
from coordinator_core.hooks._envelope import no_advisory, payload_of, post_advisory
from coordinator_core.hooks._payload import field


_ALL_CMD_TAG_RE = re.compile(r"<command-name>[^<]*</command-name>")

_AUTHORING_CMD_TAG_RE = re.compile(
    r"<command-name>/?(coordinator:)?(handoff|workstream-complete|spinoff)</command-name>"
)

_AUTHORING_TAIL_RE = re.compile(
    r"(^|[^a-z])/(coordinator:)?(handoff|workstream-complete|spinoff)([^a-z]|$)"
    r"|(^|[^a-z:/])coordinator:(handoff|workstream-complete|spinoff)([^a-z]|$)"
    r"|Skill\((coordinator:)?(handoff|workstream-complete|spinoff)\)",
    re.MULTILINE,
)

_KIND_RECOVERY_RE = re.compile(r"^kind:\s*recovery\s*$", re.MULTILINE)

_KIND_SPINOFF_RE = re.compile(r"^kind:\s*spinoff\s*$", re.MULTILINE)

_INSTALL_CHAIN_ORDER_RE = re.compile(r"^install_chain_order:\s*[0-9]", re.MULTILINE)


_NUDGE_MSG_TEMPLATE = (
    "[nudge] {parent_dir}/: no /handoff|/workstream-complete|/spinoff active. "
    "Done: /workstream-complete. Fork: /handoff|/spinoff (PM-gated). "
    "Cross-repo: cross-repo-memo. Recovery/pickup: ignore."
)


def _authoring_skill_active_sync(transcript_path: str) -> bool:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            full_content = fh.read()
    except OSError as exc:
        print(f"nudge_unauthorized_handoff: cannot read transcript {transcript_path}: {exc} (fail-open)", file=sys.stderr)
        return False

    if not full_content:
        return False

    all_cmd_tags = _ALL_CMD_TAG_RE.findall(full_content)
    if all_cmd_tags:
        last_cmd = all_cmd_tags[-1]
        if _AUTHORING_CMD_TAG_RE.match(last_cmd):
            return True

    all_lines = full_content.splitlines()
    tail_lines = all_lines[-2000:] if len(all_lines) > 2000 else all_lines
    tail = "\n".join(tail_lines)
    if _AUTHORING_TAIL_RE.search(tail):
        return True

    return False


async def advisory_text(
    tool_name: str,
    file_path: str,
    content: str,
    transcript_path: str,
) -> str:
    import asyncio

    if tool_name != "Write":
        return ""

    if not file_path:
        return ""

    file_path_norm = file_path.replace("\\", "/")

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

    # COORDINATOR_HANDOFF_NUDGE_OFF env-hatch is re-plumbed to a session-scoped sentinel;

    if content:
        leading = "\n".join(content.splitlines()[:20])
        if _KIND_RECOVERY_RE.search(leading):
            return ""

        if _KIND_SPINOFF_RE.search(leading) and _INSTALL_CHAIN_ORDER_RE.search(leading):
            return ""

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
    params = payload_of(params)
    text = await advisory_text(
        field(params, "tool_name"),
        field(params, "file_path"),
        field(params, "content"),
        field(params, "transcript_path"),
    )
    return post_advisory(text) if text else no_advisory()

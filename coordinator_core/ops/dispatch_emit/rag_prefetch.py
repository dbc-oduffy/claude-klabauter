"""Centralised example-retrieval-repo prefetch: the block every stage brief carries and the per-run rag-vs-grep receipt.

Tier, gated roles and the per-agent halt are `rag_use`'s; this module only consumes them so the per-agent
digest verdict and the per-agent halt agree. Fleet-wide gates (`ue_corpus_gap`, `example_retrieval_repo_not_serving`) are
the halt's alone: the receipt scores each agent on attempts and cadence with `rag_empty` zeroed. A verdict is
reported here, never enforced.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from coordinator_core.ops.dispatch_emit import rag_use

_LOG = logging.getLogger(__name__)

PREFETCH_BLOCK_MARK = "## Centralised example-retrieval-repo citations (consume these before searching)"
# Matches emit._EXECUTION_NOTES_CHAR_CAP: the block rides every brief, so its cap is the per-brief section cap.
PREFETCH_BLOCK_CHAR_CAP = 4000

TOOL_FAMILIES = ("project-rag", "context7", "lsp", "none")
_SENTENCE_END = re.compile(r"[.!?](?=\s)|\n")


def prefetch_applies(repo_root: Any) -> bool:
    """True when example-retrieval-repo serves `repo_root` (the `emit._example_retrieval_repo_brief_line` predicate); never raises."""
    if repo_root is None:
        return False
    try:
        from coordinator_core.bash_guards._rag_serving import serves_repo

        return bool(serves_repo(Path(repo_root).as_posix()))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("rag prefetch skipped: serves_repo failed for %s: %r", repo_root, exc)
        return False


def citation_block(text: Optional[str], cap: int = PREFETCH_BLOCK_CHAR_CAP) -> str:
    """`PREFETCH_BLOCK_MARK` plus `text`, the whole block at most `cap` characters, cut at a whole sentence.

    Blank `text` yields "" so a brief never carries an empty heading.
    """
    body = (text or "").strip()
    if not body:
        return ""
    room = cap - len(PREFETCH_BLOCK_MARK) - 2
    if room <= 0:
        return PREFETCH_BLOCK_MARK[:cap] if cap > 0 else ""
    if len(body) > room:
        head = body[:room]
        ends = [m.end() for m in _SENTENCE_END.finditer(head)]
        body = (head[: ends[-1]] if ends else head).rstrip()
    return f"{PREFETCH_BLOCK_MARK}\n\n{body}"


def tool_family(
    repo_root: Any,
    *,
    mcp_names: Optional[Iterable[str]] = None,
    plugin_roots: Optional[Iterable[Path]] = None,
    tier: Optional[str] = None,
) -> str:
    """Detected code-intel family: "project-rag" when installed, else "context7", "lsp" or "none". Never raises."""
    try:
        tier = tier if tier is not None else rag_use.rag_tier(repo_root, mcp_names=mcp_names, plugin_roots=plugin_roots)
        if tier != rag_use.TIER_NONE:
            return "project-rag"
        if mcp_names is None:
            from coordinator_core._settings_home import home_dir

            names = rag_use._mcp_names_in(home_dir() / ".claude.json", nested=True) | rag_use._mcp_names_in(
                Path(repo_root) / ".mcp.json", nested=False
            )
        else:
            names = frozenset(str(n).lower() for n in mcp_names)
        if plugin_roots is None:
            from coordinator_core.ops.dispatch_emit.plugin_review_overlay import installed_plugin_roots

            plugin_roots = installed_plugin_roots()
        from coordinator_core.ops.dispatch_emit.plugin_review_overlay import _plugin_name

        every = set(names) | {_plugin_name(Path(r)).lower() for r in plugin_roots}
        for family in ("context7", "lsp"):
            if any(family in n for n in every):
                return family
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("tool_family detection failed for %s: %r", repo_root, exc)
    return "none"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def rag_use_receipt(
    scan: Optional[dict],
    *,
    repo_root: Any,
    roles: Optional[dict] = None,
    tier: Optional[str] = None,
    family: Optional[str] = None,
) -> dict:
    """The `rag_use` field of the doe-wake-digest 1.6.0 schema from an `agent_usage.scan_run` result.

    verdict: "exempt" when the agent is not gated or the tier is none; "fail" when `rag_use.agent_rag_halt`
    names it (tier rag: zero attempts; tier ue: also cadence); else "pass". Fleet-wide gates
    (not_serving, ue_corpus_gap) are the halt's, never a per-agent verdict.
    """
    tier = tier if tier is not None else rag_use.rag_tier(repo_root)
    roles = roles if roles is not None else rag_use.load_gated_roles()
    family = family if family is not None else tool_family(repo_root, tier=tier)
    agents_out: list[dict] = []
    rag_total = grep_total = 0
    rows = (scan or {}).get("agents") if isinstance(scan, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rag, grep = _count(row.get("rag_calls")), _count(row.get("grep_calls"))
        rag_total += rag
        grep_total += grep
        gated = rag_use.is_gated(row.get("agent_type"), roles)
        verdict = "exempt"
        if gated and tier != rag_use.TIER_NONE:
            halt = rag_use.agent_rag_halt([{**row, "rag_empty": 0}], tier=tier, roles=roles)
            named = {a.get("agent_id") for a in (halt or {}).get("agents") or []}
            verdict = "fail" if str(row.get("agent_id") or "") in named else "pass"
        agents_out.append({
            "agent_id": str(row.get("agent_id") or "")[:100],
            "agent_type": str(row.get("agent_type") or "")[:100],
            "label": str(row.get("label") or "")[:200],
            "gated": gated,
            "rag_calls": rag,
            "rag_errors": _count(row.get("rag_errors")),
            "grep_calls": grep,
            "verdict": verdict,
        })
    return {
        "installed": tier != rag_use.TIER_NONE,
        "tier": tier,
        "tool_family": family,
        "totals": {"rag_calls": rag_total, "grep_calls": grep_total},
        "agents": agents_out[:100],
    }

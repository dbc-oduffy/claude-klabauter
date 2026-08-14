"""
coordinator_core.ops.scan_content_leakage — JSON-RPC
"percolate.scan_content_leakage_tiers" operation: three-tier (HIGH/MEDIUM/LOW)
content-leakage regex sweep over an about-to-publish tree.

Purpose: replaces the percolate Step 2c fence (DoE skills/percolate/SKILL.md
§ Step 2c), which ran three ``grep -nIE`` passes over the about-to-publish
file set. The fence's own in-file rationale for its two-detector split — that
macOS/BSD ``grep -E`` lacks reliable ``\\b`` word-boundary support — dissolves
here: Python's ``re`` has real ``\\b``, which is exactly the platform-risk
closure the Wave-3 settlement ratifies (B7 — RATIFIES).

Tier patterns (ported from the fence's shapes, one tier per regex set):
    HIGH   — credential / secret shapes (API keys, tokens, private-key PEM
             headers). Any hit sets ``blocked: true`` (blocks publish).
    MEDIUM — internal-path / peer-repo / email leakage shapes (~/.claude
             substrate paths, /x/ and drive-letter peer-repo paths, email
             addresses). Surfaces to the PM gate; does not block.
    LOW    — informational only (40-char hex commit SHAs, "First Officer
             Doctrine" outside doctrine trees). Renders in the panel.

**Two-detector split preserved (settlement B7, binding):** operator-identity
tokens (machine codenames, home path, org slug — the fence's
``setup/.percolate-identity`` → ``PERSONAL_REVIEW_PATTERNS`` leg) stay in DoE
``publish.py``'s Phase-4 audit. This op does NOT fold that logic in — it is
not this repo's surface. Only the generic shapes above ship here; no
operator-specific literal appears in this module.

Contract (op-classification.tsv row scan-content-leakage-tiers):
    params: {target_root: str}
        target_root — the about-to-publish tree to sweep (explicit
                      caller-supplied param; percolate.run precedent).
    -> {high: list[dict], medium: list[dict], low: list[dict], blocked: bool,
        skipped: list[str]}
        Each finding dict: {path: str (POSIX rel-path under target_root),
        line: int (1-based), match: str (first matching substring on the
        line)}. One finding per (file, line, tier) — a line matching in two
        tiers appears once in each, mirroring the fence's three independent
        grep passes. ``blocked`` is true iff ``high`` is non-empty.
        ``skipped`` carries the settlement-mandated per-file notes: POSIX
        rel-paths of files skipped as binary (UnicodeDecodeError) rather
        than crashing mid-sweep — the native analog of grep's ``-I``.

Keying scope: none — ``target_root`` is an explicit caller-supplied param
(the percolate.run ``target_root`` precedent); no injected-worktree read,
``repo_root`` is unused.

Idempotency (DEC-7 note, per settlement B7): inherent — a read-only regex
sweep with zero writes and zero spawns; two runs over identical content
produce identical tier findings (findings are deterministically sorted by
path then line).

Self-registration: importing this module calls
register_op("percolate.scan_content_leakage_tiers", ...) as a side-effect.
Registration across the remaining three surfaces (_EAGER_OP_MODULES /
_OP_KEY_SCOPE / _registry_map.py) lands in the separate EM-serial
registration pass (CC-3).

Spec backlink: pln-wave-3-design-settlements-15-d-76fdbd § B7
Port source: DoE-claude coordinator/skills/percolate/SKILL.md § Step 2c (bash fence)

Negative-spec (hard-won):
  - Does NOT scan for operator-identity tokens (PERSONAL_REVIEW_PATTERNS) —
    that detector stays in DoE publish.py by settlement; folding it in here
    would collapse the ratified two-detector split.
  - Does NOT spawn grep, xargs, or any subprocess — pure Python ``re``.
  - Does NOT crash on binary content — UnicodeDecodeError skips the file
    with a per-file note in ``skipped`` (the fence's ``grep -I`` analog).
  - Does NOT descend into ``.git`` directories (never part of a publish set).
  - Does NOT write anything, anywhere — read-only by construction.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.ipc import register_op

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier regex sets — ported from the Step 2c fence's grep -nIE patterns.
# Python `re` supplies real \b (the fence's documented BSD-grep gap).
# ---------------------------------------------------------------------------

#: Tier HIGH — credential / secret shapes. Any hit blocks publish.
_HIGH_RE = re.compile(
    r"sk-[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|xox[bpars]-[A-Za-z0-9-]{10,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN [A-Z ]+PRIVATE KEY-----"
)

#: Tier MEDIUM — internal paths, peer-repo paths, email addresses.
#: Generic shapes only — no operator-specific literals (see module docstring).
_MEDIUM_RE = re.compile(
    r"~/\.claude/(?:tasks|projects|memory|plans)/"
    r"|/x/[a-z-]+"
    r"|[XxCc]:/[a-z-]+"
    r"|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

#: Tier LOW — informational: bare 40-hex commit SHAs, "First Officer Doctrine".
_LOW_RE = re.compile(r"\b[0-9a-f]{40}\b|First Officer Doctrine")

_TIERS = (("high", _HIGH_RE), ("medium", _MEDIUM_RE), ("low", _LOW_RE))


def _iter_scan_files(root: Path):
    """Yield regular files under *root*, lexically sorted, .git trees excluded."""
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            yield path


@register_op("percolate.scan_content_leakage_tiers")
def _scan_content_leakage_tiers(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "percolate.scan_content_leakage_tiers" handler (sync —
    offloaded to a worker thread by dispatch; file I/O never parks the loop).

    See module docstring for the full contract. Raises ValueError (structured
    fail-loud) on a missing param or a target_root that is not a directory —
    absence of the about-to-publish tree is a premise failure, not an empty
    (falsely green) scan result.
    """
    target_root_raw = (params.get("target_root") or "").strip()
    if not target_root_raw:
        raise ValueError(
            "percolate.scan_content_leakage_tiers: missing required param "
            "'target_root' (the about-to-publish tree to sweep — explicit "
            "caller-supplied, percolate.run precedent)"
        )

    root = Path(target_root_raw)
    if not root.is_dir():
        raise ValueError(
            "percolate.scan_content_leakage_tiers: target_root "
            f"{target_root_raw!r} is not a directory — the about-to-publish "
            "tree must exist; an empty scan over a missing tree would be a "
            "falsely green publish gate"
        )

    findings: Dict[str, List[dict]] = {"high": [], "medium": [], "low": []}
    skipped: List[str] = []

    for path in _iter_scan_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file — skip with a per-file note (settlement B7; the
            # native analog of the fence's `grep -I`), never crash mid-sweep.
            skipped.append(rel)
            continue

        for line_no, line in enumerate(text.splitlines(), start=1):
            for tier, pattern in _TIERS:
                m = pattern.search(line)
                if m is not None:
                    findings[tier].append(
                        {"path": rel, "line": line_no, "match": m.group(0)}
                    )

    blocked = bool(findings["high"])
    if blocked:
        logger.warning(
            "percolate.scan_content_leakage_tiers: %d HIGH-tier finding(s) "
            "under %s — publish blocked",
            len(findings["high"]),
            root,
        )

    return {
        "high": findings["high"],
        "medium": findings["medium"],
        "low": findings["low"],
        "blocked": blocked,
        "skipped": skipped,
    }

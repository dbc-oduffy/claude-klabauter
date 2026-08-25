"""
coordinator_core.ops.measure_token_envelope — token-cost oracle for the boot
envelope.

Purpose: report per-file and total token cost for a NAMED SET of "boot
envelope" surfaces — files a Claude Code session (or subagent) loads
automatically at start, such as the CLAUDE.md-class files enumerated in
`coordinator_core.claude_md_budget`. Every pre-existing gate (DoE's
`check-claude-md-size.py` hook, makima's Check 7 in
`coordinator_core.bash_guards.dispatch_checks`) counts BYTES only — nothing in
either repo answers "how many tokens does this cost," even though
`docs/wiki/tiered-context-loading.md`'s Tier-0 budget is stated in TOKENS
("<=2K tokens, always loaded"). This module is that missing oracle.

Token counting here is an ESTIMATE, not an exact tokenizer count: no tokenizer
library (`tiktoken` or similar) is a declared dependency of this package (see
`pyproject.toml`'s `[project] dependencies`) — pinning one is an
add-a-dependency decision this op does not make unilaterally on a chunk this
narrow. The estimator uses the widely-cited ~4-characters-per-token heuristic
for English prose (OpenAI's own guidance: "a helpful rule of thumb is that one
token generally corresponds to ~4 characters of text"). This is directionally
correct and stable enough to compare against a fixed budget, but a caller
needing exact parity with a specific model's tokenizer must not treat this as
authoritative.

Consumed by (or intended to be consumed by):
    - DoE-claude `coordinator/hooks/scripts/check-claude-md-size.py`
    - project-makima `coordinator_core.bash_guards.dispatch_checks.check_validate_commit`
      ("Check 7")

Spec backlink: DoE-claude:pln-always-loaded-doctrine-envelop-cd5932 § C1(a)
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Union

_CHARS_PER_TOKEN: float = 4.0


def estimate_tokens(text: str) -> int:
    """Estimate a token count for `text` via the ~4-chars-per-token heuristic.

    Returns 0 for empty text. Rounds UP (ceil) so any non-empty text reports
    at least 1 token rather than truncating to 0 on short strings.
    """
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def measure_surface(path: Union[str, Path]) -> Dict[str, Any]:
    """Measure one file's byte size and estimated token cost.

    Returns ``{"path": str(path), "bytes": int, "tokens": int, "exists": bool}``.

    A missing or unreadable file reports ``exists=False, bytes=0, tokens=0``
    rather than raising — a caller measuring a NAMED SURFACE SET expects
    partial coverage (not every governed surface exists on every machine,
    e.g. no dev-repo sentinel on this box) to degrade gracefully, not abort
    the whole report.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError:
        return {"path": str(path), "bytes": 0, "tokens": 0, "exists": False}

    text = data.decode("utf-8", errors="replace")
    return {
        "path": str(path),
        "bytes": len(data),
        "tokens": estimate_tokens(text),
        "exists": True,
    }


def measure_surfaces(paths: List[Union[str, Path]]) -> Dict[str, Any]:
    """Measure a NAMED SET of surfaces, per-file and total.

    Returns::

        {
            "surfaces": [ {path, bytes, tokens, exists}, ... ],  # input order preserved
            "total_bytes": int,
            "total_tokens": int,
        }

    A missing surface contributes 0 to both totals (via `measure_surface`'s
    degrade-gracefully behaviour) — it is still listed in ``surfaces`` with
    ``exists: False`` so a caller can tell "0 tokens because absent" apart
    from "0 tokens because genuinely empty".
    """
    rows = [measure_surface(p) for p in paths]
    return {
        "surfaces": rows,
        "total_bytes": sum(r["bytes"] for r in rows),
        "total_tokens": sum(r["tokens"] for r in rows),
    }


def main(argv: List[str]) -> int:
    """CLI entrypoint: print a JSON per-file + total token/byte report.

    Usage: ``measure_token_envelope.py <path> [<path> ...]``

    This is a REPORTING oracle, not a gate — it always exits 0 given at least
    one argument (2 on missing-args usage error). Callers that need block/warn
    behaviour against a budget (DoE's hook, makima's Check 7) apply their own
    thresholds from `coordinator_core.claude_md_budget` on top of this report;
    this module has no opinion on what counts as "too large".
    """
    if not argv:
        print("usage: measure_token_envelope.py <path> [<path> ...]", file=sys.stderr)
        return 2

    result = measure_surfaces(argv)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

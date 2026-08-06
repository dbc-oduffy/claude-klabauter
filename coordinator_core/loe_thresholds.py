"""
coordinator_core.loe_thresholds — shared t-shirt LoE sizing loader + calculator.

Purpose: load a ``loe-thresholds.yaml``-shaped document (example-doctrine-repo
``coordinator/config/loe-thresholds.yaml``) and compute the "any-criterion,
highest-tier-first" t-shirt classification shared by
``coordinator/bin/coordinator-session-loe.py`` and
``coordinator/bin/aggregate-chain-loe.py``/
``coordinator_core.session_ledger.aggregate_chain_loe``.
Centralising this loader in one Python module — rather than each script's
independent hand-copied bash ``TSHIRT_TABLE`` — eliminates a live sync-drift risk:
a 2026-05-19 dogfood-fix already had to be hand-repropagated across the two bash
scripts that predated the port (see ``config/loe-thresholds.yaml``'s own
changelog comment).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 4
Port of (fallback table values only): aggregate-chain-loe.sh TSHIRT_TABLE
    (example-doctrine-repo b644d5a9, 2026-07-22) / coordinator/config/loe-thresholds.yaml (example-doctrine-repo)

Negative-spec:
  - Does NOT hardcode a config file path. Callers supply ``path`` to
    ``load_thresholds`` (or accept the ``DEFAULT_THRESHOLDS`` fallback) — this
    module has no opinion on which repo's coordinator/config/loe-thresholds.yaml
    is canonical; that's a caller/trampoline concern (mirrors the bash script's
    own SCRIPT_DIR-relative CONFIG_FILE derivation).
  - Does NOT mutate or write the YAML file — read-only loader.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

# Tier ordering, highest to lowest — mirrors loe-thresholds.yaml's tshirt_thresholds
# map insertion order and the bash TSHIRT_TABLE array order. "Any-criterion"
# semantics: a session qualifies for a tier if ANY of its three metrics meets or
# exceeds that tier's threshold; the first (highest) qualifying tier wins.
_TIER_ORDER = ["XL", "L", "M", "S", "XS"]

# Fallback table — verbatim copy of coordinator/config/loe-thresholds.yaml's values
# as of 2026-07-15 (post dogfood-fix: S.opus_dispatches=1, M.opus_dispatches=2).
# Used only when a caller does not supply thresholds (or the file at the supplied
# path is unreadable/malformed) — an explicit, documented degrade, not a silent
# divergence risk: callers that care about staying in sync with the live config
# should always pass a resolved thresholds_path through to load_thresholds().
DEFAULT_THRESHOLDS: List[Dict[str, Any]] = [
    {"tier": "XL", "agent_dispatches": 50, "opus_dispatches": 6, "em_tokens": 1000000},
    {"tier": "L", "agent_dispatches": 30, "opus_dispatches": 3, "em_tokens": 600000},
    {"tier": "M", "agent_dispatches": 15, "opus_dispatches": 2, "em_tokens": 300000},
    {"tier": "S", "agent_dispatches": 5, "opus_dispatches": 1, "em_tokens": 150000},
    {"tier": "XS", "agent_dispatches": 0, "opus_dispatches": 0, "em_tokens": 50000},
]


def load_thresholds(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Load and tier-order the ``tshirt_thresholds`` map from a loe-thresholds.yaml file.

    Returns a list of ``{"tier": str, "agent_dispatches": int, "opus_dispatches": int,
    "em_tokens": int}`` dicts ordered highest-to-lowest tier — the shape
    ``compute_tshirt`` expects.

    Raises ``OSError``/``yaml.YAMLError``/``KeyError`` on a missing, malformed, or
    empty file — callers that want graceful degradation should catch and fall back
    to ``DEFAULT_THRESHOLDS`` themselves (see
    ``coordinator_core.session_ledger.aggregate_chain_loe``).
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    raw = doc["tshirt_thresholds"]
    ordered: List[Dict[str, Any]] = []
    for tier in _TIER_ORDER:
        if tier not in raw:
            continue
        row = raw[tier]
        ordered.append(
            {
                "tier": tier,
                "agent_dispatches": int(row["agent_dispatches"]),
                "opus_dispatches": int(row["opus_dispatches"]),
                "em_tokens": int(row["em_tokens"]),
            }
        )
    if not ordered:
        raise KeyError(f"loe-thresholds.yaml at {path} has no recognised tshirt_thresholds entries")
    return ordered


def compute_tshirt(
    agent_dispatches: int,
    opus_dispatches: int,
    em_tokens: Optional[int],
    thresholds: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Any-criterion t-shirt sizing — highest tier for which >=1 metric qualifies.

    Mirrors ``compute_tshirt()`` (ported at
    ``coordinator/bin/aggregate-chain-loe.py``) / the equivalent inline
    TSHIRT_TABLE walk in ``coordinator/bin/coordinator-session-loe.py``:
    iterates tiers highest-to-lowest, returns the first tier where
    ``agent_dispatches`` OR
    ``opus_dispatches`` OR ``em_tokens`` meets or exceeds that tier's threshold.
    ``em_tokens=None`` is treated as "unknown" — mirrors the bash ``[[ -n "$tok" ]]``
    guard, i.e. an unknown token count never qualifies a tier on its own (the other
    two criteria still can). Falls through to "XS" if no row is supplied or reached
    (the XS row's all-zero thresholds mean this fallback is normally unreachable —
    every session trivially has agent_dispatches >= 0 — but is kept as a defensive
    floor for a caller-supplied ``thresholds`` list that omits an XS row).
    """
    rows = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    for row in rows:
        if agent_dispatches >= row["agent_dispatches"]:
            return row["tier"]
        if opus_dispatches >= row["opus_dispatches"]:
            return row["tier"]
        if em_tokens is not None and em_tokens >= row["em_tokens"]:
            return row["tier"]
    return "XS"


def compute_tshirt_nullable(
    agent_dispatches: Optional[int],
    opus_dispatches: Optional[int],
    em_tokens: Optional[int],
    thresholds: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Nullable-aware sibling of ``compute_tshirt`` sharing the same
    any-criterion, highest-tier-first walk.

    Review: code-reviewer — hand-rolled duplicate of this walk previously
    lived inline in ``coordinator_core.ops.coordinator_complete_entry``
    because ``compute_tshirt``'s ``int`` signature can't distinguish
    "unset" from "0" (a present-but-empty dispatch log legitimately
    renders 0; an absent one renders ``null``). This variant treats an
    unresolved ``agent_dispatches``/``opus_dispatches`` as absent rather
    than coercing to ``int``, and returns the literal string ``"null"``
    (mirroring the bash oracle's quoted ``TSHIRT="null"`` sentinel) only
    when ALL THREE metrics are unresolved — otherwise it returns the first
    qualifying tier, exactly as ``compute_tshirt`` does. Exists so callers
    needing the null/zero distinction reuse this module's table walk
    instead of hand-copying it (the exact drift class this module exists
    to prevent).
    """
    rows = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    for row in rows:
        if agent_dispatches is not None and agent_dispatches >= row["agent_dispatches"]:
            return row["tier"]
        if opus_dispatches is not None and opus_dispatches >= row["opus_dispatches"]:
            return row["tier"]
        if em_tokens is not None and em_tokens >= row["em_tokens"]:
            return row["tier"]
    return "null"

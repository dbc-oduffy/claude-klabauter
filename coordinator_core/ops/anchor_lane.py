"""The anchor-premise evaluation lane: one in-process evaluator for a plan's census-form anchors,
the same at every t-shirt size. No spawn, no walk (``_anchor_check`` reads only the file each
stage names).

Contract: ``evaluate`` returns a ``run_truth_contract.AnchorVerdict`` whose outcome is ``held`` or
``broke`` for a command in the ``_anchor_check`` grammar and ``unevaluable`` otherwise; it never
reads ``unchecked`` as a default. A single ``expect`` over an ``&&`` chain applies to every stage.
"""

from pathlib import Path
from typing import Any, Mapping

from coordinator_core.ops import _anchor_check

_OUTCOME = {"MATCH": "held", "MISMATCH": "broke", "NOT_EVALUABLE": "unevaluable"}


def require_lane(size: Any) -> str:
    """``size`` when the lane runs at it; a ``ValueError`` for a size the lane is not wired at, so
    a gate at an unknown size is a refused configuration, never an unchecked pass."""
    from coordinator_core.ops.dispatch_emit.run_truth_contract import TSHIRTS

    if size not in TSHIRTS:
        raise ValueError(f"anchor lane is not wired at size {size!r}; sizes: {', '.join(TSHIRTS)}")
    return size


def _expect_per_stage(command: str, expect: str) -> str:
    """``expect`` repeated once per ``&&`` stage when it is a single bound over a chain, else as given."""
    text = str(expect)
    if " then " in text:
        return text
    try:
        stages = len(_anchor_check._parse_stages(command))
    except _anchor_check._NotEvaluable:
        return text
    return " then ".join([text.strip()] * stages) if stages > 1 else text


def evaluate(anchor: Mapping[str, Any], repo_root: Path):
    """The ``AnchorVerdict`` for one ``{id, check: {command, expect}}`` anchor under ``repo_root``."""
    from coordinator_core.ops.dispatch_emit.run_truth_contract import AnchorVerdict

    check = anchor.get("check") or {}
    command, expect = str(check.get("command") or ""), str(check.get("expect") or "")
    result = _anchor_check.evaluate(command, _expect_per_stage(command, expect), repo_root)
    outcome = _OUTCOME[result["state"]]
    observed = result.get("detail") if outcome == "unevaluable" else result.get("observed")
    return AnchorVerdict(
        id=str(anchor.get("id") or ""), outcome=outcome, observed=str(observed or ""), expect=expect
    )

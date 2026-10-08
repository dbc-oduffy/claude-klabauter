"""plan_chain final digest: assemble the kind plan/close doe-wake-digest from ChainState.

Caps are read from the vendored ``doe-wake-digest.schema.json``; ``jsonschema`` is imported
lazily by ``validate_final_digest``.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from coordinator_core.ops.plan_chain.contract import ChainManifest, ChainState, STAGES

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contract" / "doe-wake-digest.schema.json"
_PLAN_OUTCOMES = ("pulled", "replan", "surfaced")
_CRITERION_STATUSES = ("met", "not_met", "indeterminate", "not_run", "unstructured")


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _cap(text: str | None, max_length: int) -> str | None:
    return None if text is None else text[:max_length]


def _caps() -> dict[str, int]:
    props = load_schema()["properties"]
    chain = props["chain"]["properties"]
    anchor = load_schema()["$defs"]["line_anchor"]["properties"]
    return {
        "plan_path": props["plan"]["properties"]["path"]["maxLength"],
        "deliverable": props["plan"]["properties"]["deliverable_id"]["maxLength"],
        "observation": props["criterion"]["properties"]["observation"]["maxLength"],
        "sidecar": props["criterion"]["properties"]["sidecar"]["maxLength"],
        "decision": props["decision_required"]["maxLength"],
        "halt_reason": chain["halt_reason"]["maxLength"],
        "sha": chain["commit"]["properties"]["sha"]["maxLength"],
        "receipt": chain["commit"]["properties"]["receipt_path"]["maxLength"],
        "line": anchor["line"]["maxLength"],
        "anchor": anchor["anchor"]["maxLength"],
        "list_items": props["deviations"]["maxItems"],
    }


def _criterion(execute_digest: Mapping[str, Any] | None, caps: Mapping[str, int]) -> dict[str, Any]:
    src = (execute_digest or {}).get("criterion")
    if not isinstance(src, Mapping):
        return {"status": "not_run", "observation": None, "sidecar": None}
    status = src.get("status")
    return {
        "status": status if status in _CRITERION_STATUSES else "unstructured",
        "observation": _cap(src.get("observation"), caps["observation"]),
        "sidecar": _cap(src.get("sidecar"), caps["sidecar"]),
    }


def _anchors(items: Any, caps: Mapping[str, int], *, from_execute_deviations: bool = False) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        if from_execute_deviations:
            line = f"{item.get('chunk', '')}: {item.get('kind', '')}"
        else:
            line = str(item.get("line", ""))
        out.append({"line": line[: caps["line"]], "anchor": str(item.get("anchor", ""))[: caps["anchor"]]})
    return out[: caps["list_items"]]


def _counts(execute_digest: Mapping[str, Any] | None) -> dict[str, int]:
    review = (execute_digest or {}).get("review")
    review = review if isinstance(review, Mapping) else {}

    def _n(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {
        "reviewers": _n(review.get("slices")),
        "findings_applied": _n(review.get("fixes_applied")),
        "escalations": 0,
        "preflight_findings": 0,
    }


def assemble_final_digest(
    state: ChainState,
    manifest: ChainManifest,
    *,
    plan_digest: Mapping[str, Any] | None,
    execute_digest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the doe-wake-digest for a finished or halted chain; call ``validate_final_digest`` next."""
    caps = _caps()
    halt = state.halt
    early = halt is not None and halt.halted_at in ("plan", "ready-gate")
    decision: str | None = None
    if early:
        kind = "plan"
        outcome = (plan_digest or {}).get("outcome")
        if plan_digest is None or outcome not in _PLAN_OUTCOMES:
            outcome = "surfaced"
            decision = "plan stage produced no usable digest"
        else:
            decision = _cap((plan_digest or {}).get("decision_required"), caps["decision"])
        next_action = {"kind": "none", "op": None, "params": None}
    else:
        kind = "close"
        outcome = "indeterminate" if halt is not None else "complete"
        next_action = {"kind": "none", "op": None, "params": None}

    review = (execute_digest or {}).get("review")
    review = review if isinstance(review, Mapping) else {}
    commit = None
    if halt is None and state.commit:
        commit = {
            "sha": str(state.commit["sha"])[: caps["sha"]],
            "receipt_path": str(state.commit["receipt_path"])[: caps["receipt"]],
        }
    stages_run = [s for s in dict.fromkeys(state.stages_run) if s in STAGES]
    if halt is None:
        stages_run = list(STAGES)

    return {
        "schema": "doe-wake-digest",
        "version": 1,
        "kind": kind,
        "plan": {
            "path": _cap(state.plan_path, caps["plan_path"]),
            "deliverable_id": _cap(manifest.deliverable_id, caps["deliverable"]),
        },
        "outcome": outcome,
        "criterion": _criterion(execute_digest, caps),
        "decision_required": decision,
        "em_may_think_differently": _anchors(review.get("em_may_think_differently"), caps),
        "overflow": review.get("overflow") if isinstance(review.get("overflow"), int) and review.get("overflow") >= 0 else 0,
        "deviations": _anchors((execute_digest or {}).get("deviations"), caps, from_execute_deviations=True),
        "counts": _counts(execute_digest),
        "assembled_by": "code",
        "chain": {
            "stages_run": stages_run,
            "halted_at": halt.halted_at if halt else None,
            "halt_reason": _cap(halt.reason, caps["halt_reason"]) if halt else None,
            "commit": commit,
        },
        "next_action": next_action,
    }


def validate_final_digest(digest: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` listing schema errors when ``digest`` is invalid."""
    import jsonschema

    schema = load_schema()
    errors = [e.message for e in jsonschema.validators.validator_for(schema)(schema).iter_errors(digest)]
    if errors:
        raise ValueError("final digest invalid: " + "; ".join(errors))


def write_final_digest(digest: Mapping[str, Any], trail_dir: str | Path, chain_id: str) -> Path:
    """Validate, then write ``<trail>/chain-<id>.final-digest.json``; invalid digests are never written."""
    validate_final_digest(digest)
    path = Path(trail_dir) / f"chain-{chain_id}.final-digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest, indent=2, sort_keys=True), encoding="utf-8")
    return path

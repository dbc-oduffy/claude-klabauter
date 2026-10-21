"""Re-check and batched cascade predicate for `baton.close_commit`.

`recheck` answers "does the caller's evidence still hold for this baton now". `select` answers
"may this baton become terminal": the cascade's own legs (c), (a), (d), with leg (b) answered
from one reverse-edge index built once per call over the corpus texts the caller read.
No write, no spawn. The only I/O is leg (a)'s claim-ledger read inside `_legs_c_a_refusal`
and `dag._read_meta` on the corpus texts that can reference a candidate.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from coordinator_core import dag
from coordinator_core.archival import reverse_membership
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.baton_close_commit_contract import (
    AlreadyTerminalItem,
    FlipRequest,
    RefusalReason,
    RefusedItem,
)
from coordinator_core.ops.baton_close_report_contract import (
    BatonHeader,
    Evidence,
    ResidueReason,
    Verdict,
)
from coordinator_core.ops.baton_close_report_decide import decide
from coordinator_core.ops.deliverable_cascade import (
    _HANDOFF_KIND,
    _leg_d_refusal,
    _legs_c_a_refusal,
)
from coordinator_core.session import record_homes

_CONTINUED = "continued"
_DELIVERABLE_PREFIX = "dlv-"


def _key(item: Any) -> tuple[Any, Any]:
    if isinstance(item, dict):
        return item.get("class"), item.get("target_path")
    return None, None


def recheck(
    row_evidence: list[Evidence], fresh_verdict: tuple
) -> tuple[RefusalReason, str] | None:
    """None when the caller's evidence still holds, else (reason, detail).

    `fresh_verdict` is `baton_close_report_decide.decide`'s output for this baton now.
    """
    kind = fresh_verdict[0] if fresh_verdict else None
    if kind == Verdict.CLOSABLE.value:
        fresh_done = {_key(e) for e in fresh_verdict[1] if isinstance(e, dict) and e.get("done")}
        for item in row_evidence:
            if isinstance(item, dict) and item.get("done") and _key(item) not in fresh_done:
                cls, target = _key(item)
                return (
                    RefusalReason.EVIDENCE_STALE,
                    f"cited {cls} evidence {target} is no longer done",
                )
        return None
    if kind == Verdict.RESIDUE.value:
        reason = fresh_verdict[1]
        if reason == ResidueReason.CLAIMED.value:
            return RefusalReason.CLAIMED, "baton is claimed; refusing to close it out from under its claimant"
        if reason == ResidueReason.LIVE_SUCCESSOR.value:
            return RefusalReason.LIVE_SUCCESSOR, "a live baton names this one as its predecessor"
        return RefusalReason.EVIDENCE_STALE, f"close_report now reads this baton as residue: {reason}"
    return RefusalReason.EVIDENCE_STALE, "close_report now finds no done evidence for this baton"


def _needle_regex(paths: list[str], fms: dict[str, dict]) -> re.Pattern[str] | None:
    needles: set[str] = set()
    for rel in paths:
        needles.add(Path(rel).stem)
        hid = (fms.get(rel) or {}).get("handoff_id")
        if isinstance(hid, str) and hid.strip():
            needles.add(hid.strip())
    needles.discard("")
    if not needles:
        return None
    return re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)))


def _corpus_metas(
    texts: dict[str, str], candidates: dict[str, dict], pattern: re.Pattern[str] | None
) -> dict[str, dict]:
    """Real frontmatter only where a referencer is possible; every other text is an edgeless stub."""
    metas: dict[str, dict] = {}
    for path_str, text in texts.items():
        if path_str in candidates:
            metas[path_str] = candidates[path_str]
        elif pattern is not None and pattern.search(text):
            metas[path_str] = dag._read_meta(path_str)
        else:
            metas[path_str] = {}
    return metas


def _leg_b_refusals(
    survivors: list[str],
    worktree_root: Path,
    texts: dict[str, str],
    fms: dict[str, dict],
) -> dict[str, str]:
    """{rel: refusal detail} for survivors that fail leg (b); fail-closed on any error."""
    abs_of = {rel: os.path.abspath(worktree_root / rel) for rel in survivors}
    try:
        if not texts:
            raise ValueError("empty corpus: cannot determine children")
        candidates = {abs_of[rel]: fms[rel] for rel in survivors}
        metas = _corpus_metas(texts, candidates, _needle_regex(survivors, fms))
        index = dag.build_reverse_edge_index(
            sorted(texts),
            record_homes.home_dir(str(worktree_root), "handoffs"),
            None,
            metas,
        )
        live_paths = list(texts)
        out: dict[str, str] = {}
        for rel in survivors:
            children = reverse_membership(
                abs_of[rel],
                live_paths,
                edge_kinds=set(dag.CONTINUATION_EDGE_KINDS),
                index=index,
            )
            if children:
                out[rel] = f"has a live successor/continuation (referenced by: {sorted(children)})"
        return out
    except Exception as exc:  # noqa: BLE001 - an undecidable successor check refuses
        detail = f"live-successor check indeterminate: {exc} - refusing (fail-closed)"
        return {rel: detail for rel in survivors}


def select(
    rows: list[Any],
    headers: dict[str, BatonHeader],
    successors: set[str],
    evidence: dict[str, list[Evidence]],
    fms: dict[str, dict],
    texts: dict[str, str],
    repo_root: Path,
    worktree_root: Path,
    session_id: str = "",
) -> tuple[list[FlipRequest], list[RefusedItem], list[AlreadyTerminalItem]]:
    """Sort rows into (flips, refused, already_terminal), each in input order.

    `headers`, `successors` and `evidence` are close_report's fresh readings; `fms` maps a
    repo-relative baton path to its parsed frontmatter ({} when unparseable, absent when not in
    the corpus); `texts` is the raw handoff corpus keyed by absolute path. `repo_root` is the git
    common dir (leg (a)'s claim ledger), `worktree_root` the checkout. A duplicate row is
    processed once. A `FlipRequest.path` is the repo-relative POSIX path of the row. Leg (b)
    judges the pre-flip corpus, so a baton whose only live successor is another row of this call
    is refused `live_successor`.
    """
    outcome: dict[int, Any] = {}
    survivors: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    def refuse(i: int, rel: str, reason: RefusalReason, detail: str, ev: list) -> None:
        outcome[i] = RefusedItem(handoff_path=rel, reason=reason.value, detail=detail, evidence=ev)

    for i, row in enumerate(rows):
        rel = row.get("handoff_path") if isinstance(row, dict) else None
        row_ev = row.get("evidence") if isinstance(row, dict) else None
        if not isinstance(rel, str) or not rel or not isinstance(row_ev, list):
            refuse(i, rel if isinstance(rel, str) else "", RefusalReason.BAD_ROW,
                   "row needs a handoff_path string and an evidence list", [])
            continue
        if rel in seen:
            continue
        seen.add(rel)

        header = headers.get(rel)
        fm = fms.get(rel)
        if header is None:
            state = fm.get("deployment_state") if fm else None
            if state == _CONTINUED:
                refuse(i, rel, RefusalReason.CONTINUED, "baton is continued", row_ev)
            elif state in HANDOFF_TERMINAL_DEPLOYMENT:
                outcome[i] = AlreadyTerminalItem(handoff_path=rel)
            else:
                refuse(i, rel, RefusalReason.NOT_LIVE, "baton is not an open or claimed baton in the corpus", row_ev)
            continue
        if not fm:
            refuse(i, rel, RefusalReason.UNREADABLE_FRONTMATTER, "baton frontmatter did not parse", row_ev)
            continue
        if fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            if fm.get("deployment_state") == _CONTINUED:
                refuse(i, rel, RefusalReason.CONTINUED, "baton is continued", row_ev)
            else:
                outcome[i] = AlreadyTerminalItem(handoff_path=rel)
            continue

        fresh_ev = evidence.get(rel, [])
        stale = recheck(row_ev, decide(header, fresh_ev, rel in successors))
        if stale is not None:
            refuse(i, rel, stale[0], stale[1], fresh_ev)
            continue

        raw_id = fm.get("deliverable_id")
        did = raw_id.strip() if isinstance(raw_id, str) else ""
        if not did.startswith(_DELIVERABLE_PREFIX):
            refuse(i, rel, RefusalReason.NO_DELIVERABLE_ID,
                   "baton carries no dlv- deliverable_id for advanced_by to join on", fresh_ev)
            continue

        early = _legs_c_a_refusal(
            worktree_root / rel, fm, repo_root, _HANDOFF_KIND, session_id
        )
        if early is not None:
            refuse(i, rel, RefusalReason.PREDICATE_REFUSED, early, fresh_ev)
            continue
        survivors.append((i, rel, did))

    leg_b: dict[str, str] = {}
    if survivors and _HANDOFF_KIND.predicate_legs["b"].applies:
        leg_b = _leg_b_refusals([rel for _, rel, _ in survivors], worktree_root, texts, fms)

    flips_by_index: dict[int, FlipRequest] = {}
    for i, rel, did in survivors:
        fresh_ev = evidence.get(rel, [])
        if rel in leg_b:
            reason = (
                RefusalReason.PREDICATE_REFUSED
                if leg_b[rel].startswith("live-successor check indeterminate")
                else RefusalReason.LIVE_SUCCESSOR
            )
            refuse(i, rel, reason, leg_b[rel], fresh_ev)
            continue
        late = _leg_d_refusal(fms[rel], _HANDOFF_KIND)
        if late is not None:
            refuse(i, rel, RefusalReason.PREDICATE_REFUSED, late, fresh_ev)
            continue
        flips_by_index[i] = FlipRequest(path=rel, deliverable_id=did)

    flips = [flips_by_index[i] for i in sorted(flips_by_index)]
    refused = [outcome[i] for i in sorted(outcome) if "reason" in outcome[i]]
    already = [outcome[i] for i in sorted(outcome) if "reason" not in outcome[i]]
    return flips, refused, already

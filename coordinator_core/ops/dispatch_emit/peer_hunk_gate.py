"""Landing holds for a shared path: a live peer run's hunks, and a reviewer's out-of-ask hunks.

`co_declared_peer_holds` names the landing paths this run declares that another unlanded run also
claims (a path-level commit cannot split the peer's hunks off). `out_of_ask_holds` names the
landing paths a findings-ledger row marks `out_of_ask: true` (the structured marker; reviewer prose
is never read) whose status is still unresolved.
`refusal_message` words both for the operator.

Invariants: in-process file reads only (no spawn), except the
`halted_criterion_met.met_at_head` reads (a status and a log) when a surviving holder is halted (it is recorded as
`released` on its marker before its hold is dropped; an error keeps every hold); a halted holder
whose owner session is not live and whose withheld paths carry no hunk of its own is released first
(`RELEASE_PHANTOM`, sharing the one status read; no extra spawn); an unreadable
record contributes nothing and nothing raises; two halted runs withholding one path against each
other hold nothing (the path lands whole, carrying both); a halted run that
`halted_hold_release.release_reason` frees holds nothing; a junior fresh-token sibling claims
nothing against a senior committer; a clean landing path is not contention (callers pass only the
paths the commit carries). Stdlib
only at module top (land_run cold-import pin); the readers are imported lazily.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

_LOG = logging.getLogger(__name__)

PEER_HUNKS_CO_DECLARED = "peer-hunks-co-declared"
OUT_OF_ASK_HUNKS = "out-of-ask-hunks"

_RESOLVED = frozenset({"applied", "em-rejected", "em_rejected"})


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _spine_writes(root: Path, run_id: str) -> set:
    """`writes` of every row in the plan spine the run's sizing names; empty when unreadable."""
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops import warp_run_deliverable

    out: set = set()
    sizing_rel = warp_run_deliverable.record_sizing_rel(root, run_id)
    if not sizing_rel:
        return out
    try:
        sizing = yaml.safe_load((root / sizing_rel).read_text(encoding="utf-8")) or {}
    except OSError:
        return out
    plan_rel = _norm(sizing.get("plan")) if isinstance(sizing, dict) else ""
    if not plan_rel:
        return out
    try:
        located = locate_fenced_block((root / plan_rel).read_text(encoding="utf-8"))
    except OSError:
        return out
    if located.status is not LocateStatus.LOCATED:
        return out
    rows = yaml.safe_load(located.body) or []
    for row in rows if isinstance(rows, list) else []:
        writes = row.get("writes") if isinstance(row, dict) else None
        if isinstance(writes, list):
            out.update(_norm(w) for w in writes if isinstance(w, str))
    out.discard("")
    return out


def co_declared_peer_holds(
    worktree_root: Path,
    own_run_id: Optional[str],
    landing_paths: Iterable[str],
    own_declared: Iterable[str],
    *,
    own_plan: Optional[str] = None,
    minted_before: Optional[tuple] = None,
) -> list:
    """`[(path, sibling_run_id)]` for each landing path in `own_declared` that another unlanded
    run claims: the case `sibling_claims.filter_own` lets through. A halted sibling claims what
    its halt record withholds plus the manifest or plan-spine paths it declares that are dirty now;
    a live sibling with no stage manifest is also read through its plan spine's `writes`. A
    fresh-token sibling minted at or after `minted_before=(minted_at, run_id)` is junior and
    claims nothing. Sorted by path. Never raises. Side effect: a halted holder that is phantom (owner
    gone, no own hunk) or whose criterion is met at HEAD gets `released` written to its halt marker
    and is dropped from the result."""
    root = Path(worktree_root)
    wanted = sorted({_norm(p) for p in landing_paths if p} - {""})
    own = {_norm(p) for p in own_declared if p}
    if not wanted or not own:
        return []
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import halted_hold_release, sibling_claims

        with halted_hold_release.blob_scope(root):
            halted_hold_release.prefetch_kept_blobs(root, sibling_claims.halted_records(root, own_run_id))
            unlanded = sibling_claims._unlanded_run_ids(root, own_run_id, own_plan, minted_before)
            claims = dict(sibling_claims.sibling_run_claims(
                root, own_run_id, wanted, own_plan=own_plan, minted_before=minted_before, own_declared=own,
                unlanded=unlanded,
            ))
            for run_id in unlanded:
                if warp_run_deliverable._halted(root, run_id):
                    continue
                paths, prefixes, _unreadable = warp_run_deliverable._manifest_declared(run_id, root)
                if paths or prefixes:
                    continue
                try:
                    spine = _spine_writes(root, run_id)
                except Exception:  # noqa: BLE001 -- this sibling's spine unknown: claims nothing
                    continue
                for path in wanted:
                    if path in spine:
                        claims.setdefault(path, run_id)
            holders = {p: [r] for p, r in claims.items()}
            if holders:
                for run_id in unlanded:
                    for path in wanted:
                        if path in holders and run_id not in holders[path] and _also_claims(root, run_id, path):
                            holders[path].append(run_id)
            pairs = [
                (p, r) for p in wanted if p in holders and p in own
                for r in sorted(holders[p]) if not _mutual_halted_hold(root, own_run_id, r, p)
            ]
            holds = pairs
            if not holds:
                return holds
            return _release_criterion_met(root, holds, landing_run_id=own_run_id, landing_plan=own_plan)
    except Exception:  # noqa: BLE001 -- contest unknown: hold nothing
        return []


def _also_claims(root: Path, run_id: str, path: str) -> bool:
    """Whether an unlanded `run_id` claims an already-contested `path` without a status read: a halted
    run when it withholds the path; a live run when its manifest, extra records or plan spine declare
    it. Dirt-only halted claims name only the first claimant (they need the shared status read)."""
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import sibling_claims

    try:
        if warp_run_deliverable._halted(root, run_id):
            return path in {_norm(w) for w in warp_run_deliverable.halted_withheld_paths(root, run_id)}
        if sibling_claims._declares(root, run_id, path) or path in sibling_claims._extra_declared(root, run_id):
            return True
        paths, prefixes, _unreadable = warp_run_deliverable._manifest_declared(run_id, root)
        return not (paths or prefixes) and path in _spine_writes(root, run_id)
    except Exception:  # noqa: BLE001 -- this run's claim unknown: it adds no holder
        return False


def _release_criterion_met(
    root: Path, holds: list, *, landing_run_id: Optional[str] = None, landing_plan: Optional[str] = None,
) -> list:
    """`holds` without the halted holders released on the would-refuse path; each release is recorded
    on its marker (`released`) before its hold is dropped. Two kinds, both read through one shared
    dirty reader: a phantom holder (owner session not live, no hunk of its own in any withheld path:
    `RELEASE_PHANTOM`), then a holder whose landing commit is reachable from HEAD with its withheld
    paths clean (`RELEASE_CRITERION_MET`). Up to two git spawns (one `git status`, one `git log`),
    only when a halted holder survives the baseline check (the spawns the budgeted-path gate must
    count). Any error keeps every hold."""
    try:
        from coordinator_core.git import git_state
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import (
            halted_criterion_met, halted_hold_release, halted_hunk_attribution, halted_phantom_release,
            halted_run, sibling_claims,
        )
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        halted = sorted({s for _p, s in holds if warp_run_deliverable._halted(root, s)})
        own_withheld = {s: {_norm(p) for p in halted_run.halted_withheld(record_dir(root, s))} for s in halted}
        halted = [s for s in halted if not _holds_baselined_path(root, s, holds, own_withheld[s])]
        if not halted:
            return holds
        withheld = set().union(*(own_withheld[s] for s in halted))
        dirty = sibling_claims._dirty_reader(root, withheld)
        released = set()
        attr = halted_hunk_attribution.reader(root, landing_run_id, landing_plan=landing_plan)
        only_withheld = [
            s for s in halted if all(_norm(p) in own_withheld[s] for p, sib in holds if sib == s)
        ]
        phantom = halted_phantom_release.phantom_holders(root, only_withheld, own_withheld, dirty, attr)
        head = git_state.head_sha(root) if phantom else None
        for run_id in sorted(phantom) if head else ():
            if halted_run.record_released(record_dir(root, run_id), head, halted_hold_release.RELEASE_PHANTOM):
                _LOG.info(
                    "halted run %s released: %s at %s", run_id, halted_hold_release.RELEASE_PHANTOM, head,
                )
                released.add(run_id)
        remaining = [s for s in halted if s not in released]
        met = halted_criterion_met.met_at_head(root, remaining, dirty) if remaining else {}
        for run_id, sha in met.items():
            if halted_run.record_released(
                record_dir(root, run_id), sha, halted_hold_release.RELEASE_CRITERION_MET,
            ):
                _LOG.info(
                    "halted run %s released: %s at %s", run_id, halted_hold_release.RELEASE_CRITERION_MET, sha,
                )
                released.add(run_id)
        return [(p, s) for p, s in holds if s not in released]
    except Exception:  # noqa: BLE001 -- release unknown: every hold stands
        return holds


def _holds_baselined_path(root: Path, run_id: str, holds: list, withheld: set) -> bool:
    """True when `run_id` holds a path it did not withhold and recorded a `write_baseline` for:
    sibling_claims claims such a path only after attribution failed to explain its dirt as the
    landing run's, so the run has hunks of its own and its criterion cannot be met. A file read,
    no spawn; a run with no baseline is never excluded."""
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution

    baseline = halted_hunk_attribution._baseline(root, run_id)
    if not baseline:
        return False
    return any(_norm(p) not in withheld and _norm(p) in baseline for p, s in holds if s == run_id)


def _mutual_halted_hold(root: Path, own_run_id: Optional[str], holder: str, path: str) -> bool:
    """True when `holder` is halted, withholds `path`, and its own claim view names `own_run_id` as the
    path's first claimant: two halted runs holding one path against each other. Landing the path whole
    carries both runs' hunks (the shared-tree blob), so neither run is superseded; the holder's
    `dispatch.record_halted_run_landed` follows the landing."""
    if not own_run_id or holder == own_run_id:
        return False
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit import halted_run, sibling_claims
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    if not warp_run_deliverable._halted(root, holder):
        return False
    if path not in {_norm(w) for w in halted_run.halted_withheld(record_dir(root, holder))}:
        return False
    mirror = sibling_claims.sibling_run_claims(root, holder, [path])
    return mirror.get(path) == own_run_id


def _names_path(finding: str, path: str, basename_unique: bool) -> bool:
    text = finding.replace("\\", "/")
    if path in text:
        return True
    return basename_unique and path.rpartition("/")[2] in text


def _unresolved_out_of_ask(text: str) -> list:
    """`(label, naming_text)` for each findings-ledger row carrying the structured `out_of_ask: true`
    marker whose status is not applied or em-rejected. Prose is never read: a row with no explicit
    status is unresolved."""
    from coordinator_core.ops.review_findings_ledger import LedgerError, _parse_ledger_rows

    try:
        rows = _parse_ledger_rows(text) or []
    except LedgerError:
        return []
    out = []
    for row in rows:
        if row.get("out_of_ask") is not True or row.get("status") in _RESOLVED:
            continue
        files = row.get("files") if isinstance(row.get("files"), list) else [row.get("file")]
        named = " ".join(str(f) for f in files if isinstance(f, str))
        label = str(row.get("summary") or row.get("title") or row.get("id") or "out-of-ask")
        out.append((label, named))
    return out


def out_of_ask_holds(
    worktree_root: Path, session_id: str, run_id: str, landing_paths: Iterable[str],
) -> list:
    """`[(path, sidecar_stem, label)]` for each landing path an unresolved `out_of_ask: true` ledger
    row in one of the run's integrated wave sidecars names in `file`/`files`, by path or by a
    basename unique within `landing_paths`. Sorted by path. Never raises."""
    root = Path(worktree_root)
    paths = sorted({_norm(p) for p in landing_paths if p} - {""})
    if not paths or not session_id:
        return []
    try:
        import yaml

        from coordinator_core.frontmatter.primitives import split_frontmatter
        from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem

        share = root / ".coordinator-local" / "subagent-share" / session_id
        record = (share / f"{review_wave_bookkeeping_stem(run_id, None)}.md").read_text(encoding="utf-8")
        split = split_frontmatter(record.replace("\r\n", "\n"))
        fm = (yaml.safe_load(split.fm_text) or {}) if split else {}
        stems = fm.get("integrated_from") if isinstance(fm, dict) else None
        if not isinstance(stems, list):
            return []
        names = [p.rpartition("/")[2] for p in paths]
        holds = set()
        for stem in (s for s in stems if isinstance(s, str) and s):
            try:
                text = (share / f"{stem}.md").read_text(encoding="utf-8")
            except OSError:
                continue
            for label, named in _unresolved_out_of_ask(text):
                for path, name in zip(paths, names):
                    if _names_path(named, path, names.count(name) == 1):
                        holds.add((path, stem, label))
    except Exception:  # noqa: BLE001 -- record unknown: hold nothing
        return []
    return sorted(holds)


def refusal_message(holds: Iterable[tuple], run_id: str, root: Optional[Path] = None) -> str:
    """The refusal text for `(path, sibling_run_id)` holds: names every sibling run, and the
    withhold and peer-commits routes (`peer_claim_contract.refusal` wording). With `root`, each
    halted holder also gets its `halted_hold_release.release_calls`; the text is otherwise unchanged."""
    from coordinator_core.ops.dispatch_emit import peer_claim_contract

    pairs = list(holds)
    message, _fields = peer_claim_contract.refusal(
        [(p, f"run {s}") for p, s in pairs], run_id,
    )
    text = f"{PEER_HUNKS_CO_DECLARED}: {message}"
    if root is None:
        return text
    try:
        from coordinator_core.ops import warp_run_deliverable
        from coordinator_core.ops.dispatch_emit import halted_hold_release

        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        for holder in sorted({s for _p, s in pairs}):
            if warp_run_deliverable._halted(Path(root), holder):
                calls = "; ".join(halted_hold_release.release_calls(Path(root), holder))
                text += f" Release run {holder}: {calls}."
                withheld = halted_run.halted_withheld(record_dir(Path(root), holder))
                mutual = sorted({p for p, s in pairs if s == holder and _norm(p) in {_norm(w) for w in withheld}})
                if mutual:
                    text += (
                        f" Mutual hold: run {holder} withholds {', '.join(mutual)} against this run too."
                        " Neither run is superseded: land the path whole in one commit (the tree blob, which"
                        " carries both runs' hunks), then call dispatch.record_halted_run_landed"
                        f" {{run_id: {holder}, sha: <that commit>}}."
                    )
    except Exception:  # noqa: BLE001 -- release wording unknown: the base refusal stands
        return text
    return text

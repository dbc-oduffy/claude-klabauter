"""Settle co-declared peer holds at hunk level: a landing carries only its own hunks.

`peer_hunk_gate.co_declared_peer_holds` names each landing path another unlanded run also claims.
`settle` sorts those holds: a path co-declared by a live sibling of the same parent, any per-run
artifact (`is_per_run_artifact`), or an additive registry file (`is_additive_registry`), is read
through `own_hunk_stage.read_diff` and split by `run_hunk_claims`; the own hunks go to `stage` and `settled_blob` builds their blob. Every other
hold, and every unknown, stays in `still_held`.

Invariants: no spawn (in-process reads only); a hold whose sibling is `UNREADABLE_PEER_CLAIMS`
never settles; for a per-run artifact or additive registry file ONLY, a pure insertion at the same HEAD
anchor as a sibling's insertion is not overlap and stages just the own inserted lines (a line both runs
changed still holds; a registry file's modified or deleted line holds too); an unattributed hunk holds.
Stdlib only at module top (land_run cold-import pin); readers are imported lazily.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

_LOG = logging.getLogger(__name__)

#: Basenames written once per run into a shared location; every run's copy is a distinct claim on one path.
PER_RUN_ARTIFACTS = frozenset({"DELIVERY.md"})

#: Directory prefixes whose every file is a per-run artifact (`run_artifacts` "delivery-note").
PER_RUN_ARTIFACT_DIRS = ("docs/delivery/",)


#: Registry files every run appends rows to; a pure insertion by an unrelated run settles at hunk level.
ADDITIVE_REGISTRY_FILES = frozenset({"coordinator_core/ops/_registry_map.py", "coordinator_core/ops/__init__.py"})


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def is_per_run_artifact(path: str) -> bool:
    """Whether `path` is a per-run artifact: a `DELIVERY.md` at any depth or a file under `docs/delivery/`."""
    text = _norm(path)
    return text.rpartition("/")[2] in PER_RUN_ARTIFACTS or text.startswith(PER_RUN_ARTIFACT_DIRS)


def is_additive_registry(path: str) -> bool:
    """Whether `path` is one of `ADDITIVE_REGISTRY_FILES`."""
    return _norm(path) in ADDITIVE_REGISTRY_FILES


def _settles_by_insertion(path: str) -> bool:
    return is_per_run_artifact(path) or is_additive_registry(path)


def _parent_of(root: Path, run_id: str) -> Optional[str]:
    """Parent id recorded in the run's tracked parent copy; None when absent or unreadable."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    try:
        data = json.loads((root / contract.run_parent_copy_rel(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    parent = data.get("parent_id") if isinstance(data, dict) else None
    return parent if isinstance(parent, str) and parent else None


def sibling_halted(root: Path, sibling: str) -> bool:
    """Whether `sibling` is a halted run (public face of the halt predicate `_same_parent_live` uses)."""
    from coordinator_core.ops import warp_run_deliverable

    return bool(warp_run_deliverable._halted(root, sibling))


def _same_parent_live(root: Path, run_id: str, sibling: str) -> bool:
    """A live (not halted) sibling recorded under the same parent as `run_id`."""
    own = _parent_of(root, run_id)
    if own is None or _parent_of(root, sibling) != own:
        return False
    return not sibling_halted(root, sibling)


def _lines(root: Path, path: str) -> list:
    return (root / path).read_bytes().splitlines(keepends=True)


def _covered(spans: list) -> set:
    """Worktree line numbers (1-based) inside doubled-slot `spans` (deletion slots carry no line)."""
    out: set = set()
    for lo, hi in spans:
        if lo % 2 == 0:
            out.update(range(lo // 2, hi // 2 + 1))
    return out


def _runs(lines: Iterable[int]) -> list:
    """Contiguous `(first, count)` runs of sorted line numbers."""
    out: list = []
    for n in sorted(lines):
        if out and n == out[-1][0] + out[-1][1]:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((n, 1))
    return out


def _settle_artifact(
    root: Path, run_id: str, path: str, diff, siblings: list, *, insertions_only: bool = False
) -> Optional[list]:
    """Own hunks of a per-run artifact, narrowed to the own inserted lines where a sibling inserts at
    the same anchor; None when any hunk is unattributed, shared on a changed line, or unknowable.
    `insertions_only` also returns None for any hunk that modifies or deletes a HEAD line."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    common = resolve_git_common_dir(root)
    tree = _lines(root, path)
    own_spans = rhc._claimant_delta(root, common, run_id, path, tree)
    if own_spans is None:
        return None
    sib_lines: set = set()
    for sib in siblings:
        spans = rhc._claimant_delta(root, common, sib, path, tree, coarse_ok=True)
        if spans is None:
            return None
        sib_lines |= _covered(spans)
    own_lines = _covered(own_spans)
    out: list = []
    for hunk in diff.hunks:
        span = set(range(hunk.new_start, hunk.new_start + hunk.new_len))
        mine = span & (own_lines - sib_lines)
        theirs = span & sib_lines
        if hunk.old_len == 0 and hunk.new_len:
            if not span <= (own_lines | sib_lines):
                return None
            if not mine and theirs:
                continue
            if not mine:
                return None
            out.extend(rhc.Hunk(path, hunk.old_start, 0, first, count) for first, count in _runs(mine))
            continue
        if insertions_only or not span:
            return None
        if span & sib_lines and not span & own_lines:
            continue
        if not span <= own_lines or span & sib_lines:
            return None
        out.append(hunk)
    return out


def _settle_path(root: Path, run_id: str, path: str, diff, siblings: list) -> Optional[list]:
    """Own hunks of `path` that land now, or None when the path stays held."""
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    if _settles_by_insertion(path):
        return _settle_artifact(root, run_id, path, diff, siblings, insertions_only=is_additive_registry(path))
    split = rhc.own_hunks(root, run_id, path, list(diff.hunks), {s: True for s in siblings})
    if split.overlapping or not split.own:
        return None
    if split.unattributed and not _junior_siblings_hold(root, path, diff, split, siblings):
        return None
    return list(split.own)


def _junior_siblings_hold(root: Path, path: str, diff, split, siblings: list) -> bool:
    """True when every unattributed hunk is a sibling's alone: each sibling's baseline is exactly HEAD plus
    this run's own hunks (it started after they were written, so it is not a competing claimant on its own
    delta, which the run's older baseline also spans) and its delta covers the hunk. Any unknown is False."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
    from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc

    if split.nonrun or not siblings:
        return False
    common = resolve_git_common_dir(root)
    expected = settled_blob(root, path, diff.head_bytes, split.own)
    tree = _lines(root, path)
    covered: list = []
    for sib in siblings:
        base = hha._baseline(root, sib)
        sha = base.get(path) if isinstance(base, dict) else None
        if sha != expected:
            return False
        spans = rhc._claimant_delta(root, common, sib, path, tree, coarse_ok=True)
        if spans is None:
            return False
        covered.extend(spans)
    for hunk in split.unattributed:
        lo, hi = rhc._new_span(hunk)
        if not any(a <= hi and lo <= b for a, b in covered):
            return False
    return True


def _qualifies(root: Path, run_id: str, path: str, sibs: list) -> bool:
    """The one settle rule: no unreadable claim, and a per-run artifact, an additive registry file, or every
    sibling live on the same parent. Any unknown (parentage, halt state) disqualifies."""
    from coordinator_core.ops.dispatch_emit.peer_hunk_gate import UNREADABLE_PEER_CLAIMS

    try:
        return UNREADABLE_PEER_CLAIMS not in sibs and (
            _settles_by_insertion(path) or all(_same_parent_live(root, run_id, s) for s in sibs)
        )
    except Exception:  # noqa: BLE001 -- unknown parentage holds
        return False


def settle_eligible(
    root: Path,
    run_id: Optional[str],
    paths: Iterable[str],
    *,
    own_declared: Iterable[str],
    own_plan: Optional[str] = None,
    minted_before: Optional[tuple] = None,
) -> frozenset:
    """The subset of `paths` held by at least one co-declaring run, every hold of which `settle` would qualify
    (same rule via `_qualifies`). A path with no hold, or any unknown, is not eligible. Never raises."""
    try:
        if not run_id:
            return frozenset()
        root = Path(root)
        from coordinator_core.ops.dispatch_emit import halted_hold_release, land_deadline
        from coordinator_core.ops.dispatch_emit.peer_hunk_gate import co_declared_peer_holds

        if land_deadline.expired():
            return frozenset()  # advisory leg: the next checked leg ends the landing under its own name

        holds = co_declared_peer_holds(
            root, run_id, list(paths), list(own_declared), own_plan=own_plan, minted_before=minted_before
        )
        pairs = halted_hold_release.without_released_paths(root, [(str(p), str(s)) for p, s in holds])
        by_path: dict = {}
        for path, sibling in pairs:
            by_path.setdefault(_norm(path), []).append(sibling)
        return frozenset(p for p, sibs in by_path.items() if _qualifies(root, run_id, p, sibs))
    except Exception:  # noqa: BLE001 -- unknown is not eligible
        return frozenset()


def settle(
    root: Path,
    run_id: Optional[str],
    holds: Iterable[tuple],
    *,
    own_plan: Optional[str] = None,
    minted_before: Optional[tuple] = None,
) -> tuple:
    """`(stage, still_held)` for `holds` (`peer_hunk_gate.co_declared_peer_holds` pairs).

    `stage` is `{path: [Hunk]}` of own hunks that land now; `still_held` the pairs that stay held,
    in input order. A pair whose holder released that path (`dispatch.release_halted_path`) is dropped
    from both: the path lands whole. A path settles only when every one of its holds qualifies (a live same-parent
    sibling, or the path is a per-run artifact) and the hunk split is clean. Never raises: any
    unknown (parentage, diff, claims) keeps the hold. `own_plan` and `minted_before` are accepted for the call shape of
    `co_declared_peer_holds`; claims are read from the recorded baselines."""
    root = Path(root)
    pairs = [(str(p), str(s)) for p, s in holds]
    if not run_id or not pairs:
        return {}, pairs
    from coordinator_core.ops.dispatch_emit import halted_hold_release, own_hunk_stage

    pairs = halted_hold_release.without_released_paths(root, pairs)
    by_path: dict = {}
    for path, sibling in pairs:
        by_path.setdefault(_norm(path), []).append(sibling)
    candidates = [path for path, sibs in by_path.items() if _qualifies(root, run_id, path, sibs)]
    stage: dict = {}
    if candidates:
        try:
            diffs = own_hunk_stage.read_diff(root, candidates)
        except Exception:  # noqa: BLE001 -- unreadable diff holds every candidate
            diffs = {}
        for path in candidates:
            diff = diffs.get(path)
            if diff is None:
                continue
            try:
                hunks = _settle_path(root, run_id, path, diff, by_path[path])
            except Exception:  # noqa: BLE001 -- unknown holds
                hunks = None
            if hunks:
                stage[path] = hunks
    still = [(p, s) for p, s in pairs if _norm(p) not in stage]
    return stage, still


def settled_blob(root: Path, path: str, head_bytes: bytes, hunks: Iterable) -> str:
    """Blob sha of `head_bytes` with only `hunks` applied (worktree side from disk), written to the object
    store. A hunk may be an own-lines narrowing of a current diff hunk (same HEAD anchor, a sub-range of
    its worktree lines); one outside every current diff hunk raises `own_hunk_stage.OwnHunkRefused`."""
    from coordinator_core.git import git_objects
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import own_hunk_stage as ohs

    root = Path(root)
    path = _norm(path)
    head_lines = head_bytes.splitlines(keepends=True)
    new_lines = ohs._worktree_lines(root, path)
    actual = ohs._hunks_of(path, head_lines, new_lines)
    chosen = sorted({(h.old_start, h.old_len, h.new_start, h.new_len) for h in hunks})
    for old_start, old_len, new_start, new_len in chosen:
        if not any(
            a.old_start == old_start and a.old_len == old_len
            and a.new_start <= new_start and new_start + new_len <= a.new_start + a.new_len
            for a in actual
        ):
            raise ohs.OwnHunkRefused(f"{path}: hunk {(old_start, old_len, new_start, new_len)} is not a current difference from HEAD")
    out: list = []
    pos = 0
    for old_start, old_len, new_start, new_len in chosen:
        i1 = old_start if old_len == 0 else old_start - 1
        if i1 < pos:
            raise ohs.OwnHunkRefused(f"{path}: overlapping hunks")
        out.extend(head_lines[pos:i1])
        if new_len:
            out.extend(new_lines[new_start - 1:new_start - 1 + new_len])
        pos = i1 + old_len
    out.extend(head_lines[pos:])
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", b"".join(out))

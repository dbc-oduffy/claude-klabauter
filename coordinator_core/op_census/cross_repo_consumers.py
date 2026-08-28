"""One-pass memo scan answering *"which memos name this op?"* — the read
`kill_ledger_inventory` names in its own docstring as a gap (`WITHDRAWN`
population, K-040) and does not itself perform.

`cross-repo/inbox/` (354 open memos, measured at plan time) is prose written
by another repo's session, naming ops this repo cut or is nominating to cut.
A census that only greps this repo's own code, skills, hooks and ceremonies
cannot see that evidence, so "no consumer found" silently means "no consumer
*in this repo*". This module gives a caller that evidence for a set of op
names in ONE pass over the corpus: every memo is opened and read exactly
once, and every requested op name is tested against that one read — never a
pass per op name, which at 354 memos times N nominations is the per-item
amplification shape this repo deletes on sight.

TWO match shapes only, per `coordinator_core/ops/_registry_map.py`:

1. the dotted op name as written (``records.history``);
2. that op's module path in slash-and-dot-py form, derived from
   ``coordinator_core.ops._registry_map.OP_MODULE_MAP`` by translating dots
   to slashes and appending ``.py`` (``records.history`` ->
   ``coordinator_core/ops/record_history.py``). A map value of ``None``
   contributes only the first shape and is not an error.

Negative spec — what this module does NOT do:

- It does not classify ops. It reports which memos name a name; whether that
  constitutes a live consumer, a stale reference, or noise is a human
  adjudication this module has no opinion on.
- It does not decide dispositions. No verdict, no population, no
  recommendation — that is the caller's join against its own populations.
- It is not an op and must never be registered as one. It walks a
  potentially large markdown corpus; that is a dev-time census cost,
  deliberately outside the 500ms brightline that governs dispatched ops.
- It does not write anything. It returns data; rendering it is the caller's
  job, same as `kill_ledger_inventory.render` does for the ledger join.
- It does not infer a match by fuzzy or substring reasoning beyond the two
  named shapes above. A looser match fires on every memo mentioning `state`
  or `session`; that is explicitly out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]

SHAPE_OP_NAME = "op_name"
SHAPE_MODULE_PATH = "module_path"


@dataclass(frozen=True)
class ConsumerHit:
    """One memo naming one op, under one of the two match shapes."""

    memo_path: str
    box: str
    shape: str


def _needles_for(op_name: str) -> List[Tuple[str, str]]:
    """The (shape, needle) pairs to test for `op_name` — always the dotted
    name, plus the module-path shape when `OP_MODULE_MAP` has a non-None
    value for it. Imported locally so this module carries no import-time
    dependency on the registry map, matching the reference implementation's
    house style of confining registry imports to the call site that needs
    them (`kill_ledger_inventory._live_op_names`)."""
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    needles = [(SHAPE_OP_NAME, op_name)]
    module = OP_MODULE_MAP.get(op_name)
    if module:
        needles.append((SHAPE_MODULE_PATH, module.replace(".", "/") + ".py"))
    return needles


def scan_cross_repo_consumers(
    op_names: Iterable[str],
    *,
    include_archive: bool = False,
    repo_root: Path = REPO_ROOT,
) -> Dict[str, List[ConsumerHit]]:
    """For every name in `op_names`, the `ConsumerHit`s naming it across the
    memo corpus under `repo_root / "cross-repo"`.

    Default corpus is `cross-repo/inbox/` only; `cross-repo/archive/` is
    scanned only when `include_archive=True` — an archived memo was actioned,
    so it is reachable but never a default read (Anti-scope). A missing
    `cross-repo/` directory (no memo channel in this checkout) yields every
    requested name mapped to an empty list, never an exception.

    One pass: each memo file is opened and read once, and every requested
    name is tested against that single read.
    """
    names = list(dict.fromkeys(op_names))
    hits: Dict[str, List[ConsumerHit]] = {name: [] for name in names}
    needles = {name: _needles_for(name) for name in names}

    cross_repo_root = repo_root / "cross-repo"
    boxes: List[Tuple[str, Path]] = [("inbox", cross_repo_root / "inbox")]
    if include_archive:
        boxes.append(("archive", cross_repo_root / "archive"))

    for box, box_dir in boxes:
        if not box_dir.is_dir():
            continue
        for memo_path in sorted(box_dir.glob("*.md")):
            text = memo_path.read_text(encoding="utf-8", errors="replace")
            memo_rel = memo_path.relative_to(repo_root).as_posix()
            for name, shape_needles in needles.items():
                for shape, needle in shape_needles:
                    if needle in text:
                        hits[name].append(
                            ConsumerHit(memo_path=memo_rel, box=box, shape=shape)
                        )
    return hits

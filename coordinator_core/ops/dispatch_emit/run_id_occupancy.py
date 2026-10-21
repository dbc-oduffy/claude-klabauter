"""Run-id occupancy: which tranche number an inventory family may mint next, and whether an
emitted script's run id has already run or landed.

Both answers come from the files already beside the record (one directory listing, one
receipt read); no git spawn, no corpus scan.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


_SCRIPT_SUFFIX = ".workflow.mjs"
_OUTPUT_TAIL = r"(-p\d+)?(-(land|resume)[A-Za-z0-9]*)?(\.spine\.md|\.spine\.workflow\.mjs|\.workflow\.mjs)(\.emitted\.json)?"


def next_tranche_number(parent: Path, base_stem: str, base_run: str) -> int:
    """1 + the highest tranche N among `<base_stem>-tN.md` records and every
    `<base_run>-tN(-pM)?` spine, script or receipt in `parent`; 1 when none.
    A gap is never reused."""
    record_re = re.compile(rf"^{re.escape(base_stem)}-t(\d+)\.md$")
    output_re = re.compile(rf"^{re.escape(base_run)}-t(\d+){_OUTPUT_TAIL}$")
    highest = 0
    for child in parent.iterdir():
        match = record_re.match(child.name) or output_re.match(child.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _script_stem(script_path: Path) -> str:
    name = script_path.name
    for suffix in (".spine" + _SCRIPT_SUFFIX, _SCRIPT_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return script_path.stem


def spent_run_reason(script_path: Path) -> Optional[str]:
    """A reason naming the evidence file when the emitted script's run id has landed
    (receipt carries `landed`) or has run (a `<stem>-land*` / `<stem>-resume*` sibling
    script exists); None otherwise, including an unlanded receipt with no such sibling."""
    # Lazy: emission_receipt reaches inventory_mint through pathspec, which imports this module.
    from coordinator_core.ops.dispatch_emit.emission_receipt import _read_receipt

    receipt = _read_receipt(script_path)
    if receipt and receipt.get("landed"):
        return f"{script_path.name}.emitted.json carries a landed stamp"
    stem = _script_stem(script_path)
    parent = script_path.parent
    if not parent.is_dir():
        return None
    for kind in ("land", "resume"):
        sibling_re = re.compile(rf"^{re.escape(stem)}-{kind}[A-Za-z0-9]*\.workflow\.mjs$")
        for child in sorted(parent.iterdir()):
            if sibling_re.match(child.name):
                return f"{child.name} exists beside {script_path.name}"
    return None


def family_spent_reason(parent: Path, run_id: str) -> Optional[str]:
    """`spent_run_reason` over `<run_id>.workflow.mjs` and each `<run_id>-pM.workflow.mjs`
    script in `parent`; the first reason found, else None."""
    script_re = re.compile(rf"^{re.escape(run_id)}(-p\d+)?\.workflow\.mjs$")
    for child in sorted(parent.iterdir()):
        if script_re.match(child.name):
            reason = spent_run_reason(child)
            if reason:
                return reason
    return None

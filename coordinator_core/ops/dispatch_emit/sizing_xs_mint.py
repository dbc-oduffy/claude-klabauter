"""XS arm of `emit-dispatch-workflow --sizing`: an XS sizing -> one-row plan-tasks spine text.

Pure: returns the spine markdown and the path it belongs at; the caller (`op.py`) writes it. The
frontmatter cites `sizing_object:`, which is what `emit.derive_review_tier` keys the review wave on.
Writes come from the operator because a sizing carries no footprint.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import yaml

from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _dump_rows,
    _infer_change_kind,
    _refuse_if_directory_shaped,
    _refuse_if_glob,
)

XS_ROW_ID = "X1"


def xs_spine_plan_id(stem: str) -> str:
    """`pln-<slug>-<6hex>`, the real-plan form, derived from the sizing stem so a re-mint of the
    same sizing keeps its id."""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:30].rstrip("-") or "xs"
    return f"pln-{slug}-{hashlib.sha1(stem.encode('utf-8')).hexdigest()[:6]}"


def mint_xs_spine(
    sizing: Mapping,
    *,
    sizing_rel: str,
    writes: Sequence[str],
    out_dir: Path,
    gated: Sequence[Mapping] = (),
) -> tuple[str, Path]:
    """Return `(spine_text, out_dir / "<sizing-stem>.spine.md")`; raises the inventory_mint
    footprint errors for a glob or directory-shaped `writes` entry, `ValueError` for empty writes.

    Each `gated` entry (`title`, `owner_repo`, optional `requires`/`id`) is minted as a pathless
    row carrying an uncleared `external_gate`, so a declared external deliverable surfaces as
    withheld instead of vanishing."""
    sizing_rel = str(sizing_rel).replace("\\", "/")
    stem = PurePosixPath(sizing_rel).stem
    paths = [str(w).replace("\\", "/") for w in writes]
    if not paths:
        raise ValueError("XS arm needs at least one --writes path")
    for path in paths:
        _refuse_if_glob(XS_ROW_ID, path, path)
        _refuse_if_directory_shaped(XS_ROW_ID, path, path)

    criterion = sizing.get("exit_criterion")
    statement = str(criterion.get("statement") or "").strip() if isinstance(criterion, Mapping) else ""
    intent = str(sizing.get("intent") or "").strip()
    title = next((ln.strip() for ln in intent.splitlines() if ln.strip()), stem)

    frontmatter: dict = {
        "plan_id": xs_spine_plan_id(stem),
        "run_id": stem,
        "derived_from": "sizing object",
        "sizing_object": sizing_rel,
    }
    if sizing.get("deliverable_id"):
        frontmatter["deliverable_id"] = str(sizing["deliverable_id"])
    frontmatter["prime_exit_criterion"] = {"statement": statement}

    row = {
        "id": XS_ROW_ID,
        "title": title,
        "change_kind": _infer_change_kind(paths),
        "surface": paths[0],
        "body": (
            f"Spec: {sizing_rel} ({XS_ROW_ID})\n"
            f"Intent:\n{intent}\n"
            f"Verification (this row is DONE only when this holds): {statement}\n"
        ),
        "writes": paths,
    }

    rows = [row]
    for n, g in enumerate(gated, start=2):
        gid = str(g.get("id") or "").strip() or f"X{n}"
        if gid == XS_ROW_ID or any(r["id"] == gid for r in rows):
            gid = f"G{n}"
        gate = {"owner_repo": str(g.get("owner_repo") or "external"), "requires": str(g.get("requires") or "landed-work")}
        rows.append(
            {
                "id": gid,
                "title": str(g.get("title") or gid),
                "change_kind": "code-edit",
                "surface": sizing_rel,
                "body": f"Spec: {sizing_rel} ({gid})\nDeclared by the ask as owned by {gate['owner_repo']}; withheld.\n",
                "writes": [],
                "external_gate": [gate],
            }
        )

    text = (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False)
        + "---\n\n"
        + f"# Minted dispatch spine — {stem}\n\n"
        + f"Derived from `{sizing_rel}` by `dispatch.emit --sizing`.\n"
        + "**Do not hand-edit** — the next mint overwrites this file in place.\n\n"
        + "## Tasks\n\n"
        + "```yaml plan-tasks\n"
        + _dump_rows(rows)
        + "```\n"
    )
    return text, Path(out_dir) / f"{stem}.spine.md"

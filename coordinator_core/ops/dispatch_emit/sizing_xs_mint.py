"""XS arm of `emit-dispatch-workflow --sizing`: an XS sizing -> one-row plan-tasks spine text.

Pure: returns the spine markdown and the path it belongs at; the caller (`op.py`) writes it. The
frontmatter cites `sizing_object:`, which is what `emit.derive_review_tier` keys the review wave on.
Writes come from the operator because a sizing carries no footprint.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import yaml

from coordinator_core.ops.gate_liveness.emit_discharge import CLOSURE_KEY_KINDS
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


_RUN_STAMP = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}$")


def recorded_xs_writes(repo_root: Path, sizing_rel: str) -> list[str]:
    """The `X1` writes of the newest earlier run of this sizing whose `manifest.json` recorded
    them, else `[]`. Reads record files only: no tree walk beyond the two run-dir roots, no spawn."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    stem = PurePosixPath(str(sizing_rel).replace("\\", "/")).stem
    prefix = f"warp-{stem}-"
    runs: list[Path] = []
    for base in (RECORD_DIR_ROOT, RUN_DIR_ROOT):
        root = Path(repo_root) / base
        if root.is_dir():
            runs.extend(
                d for d in root.iterdir()
                if d.name.startswith(prefix) and _RUN_STAMP.fullmatch(d.name[len(prefix):]) and d.is_dir()
            )
    for run in sorted(runs, key=lambda d: d.name[len(prefix):], reverse=True):
        try:
            rows = json.loads((run / "manifest.json").read_text(encoding="utf-8")).get("rows") or []
        except (OSError, ValueError, AttributeError):
            continue
        for row in rows:
            if isinstance(row, Mapping) and row.get("id") == XS_ROW_ID:
                writes = [w for w in row.get("writes") or () if isinstance(w, str) and w]
                if writes:
                    return writes
    return []


def _landed_work_closure_key(gate: Mapping, sizing: Mapping, stem: str, gid: str) -> dict:
    """The `closure_key` for a minted `landed-work` gate: the ask's own well-formed key, else a
    deterministic `deliverable` key so the discharging repo has an id to stamp and a re-mint keeps it.

    A gate with no key has no clearance path (`gate_liveness.resolve` -> `no-closure-key`)."""
    given = gate.get("closure_key")
    if isinstance(given, Mapping):
        kind, ident = given.get("kind"), str(given.get("id") or "").strip()
        if kind in CLOSURE_KEY_KINDS and ident:
            return {"kind": kind, "id": ident}
    base = str(sizing.get("deliverable_id") or "").strip() or xs_spine_plan_id(stem)
    return {"kind": "deliverable", "id": f"{base}-{gid.lower()}"}


def _route_plan_bodies(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """`(paths, [(body, sidecar)])` with each `docs/plans/*.md` body replaced by its evidence sidecar
    when the write set also holds a non-body path.

    Trap: an executor never writes a plan body, so on a mixed set the sidecar is the only meaning
    the declaration can have. A plans-only set and any `docs/problems/*.md` body stay as declared."""
    from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path
    from coordinator_core.write_guards.block_subagent_plan_body_write import _PLAN_BODY_RE

    def norm(p: str) -> str:
        return posixpath.normpath(p.replace("\\", "/"))

    def is_body(p: str) -> bool:
        return bool(_PLAN_BODY_RE.search(norm(p).lower()))

    def is_plan_body(p: str) -> bool:
        return is_body(p) and "/plans/" in "/" + norm(p).lower()

    if all(is_body(p) for p in paths) or not any(is_plan_body(p) for p in paths):
        return paths, []
    routed: list[tuple[str, str]] = []
    out: list[str] = []
    for p in paths:
        if is_plan_body(p):
            sidecar = evidence_sidecar_path(Path(norm(p))).as_posix()
            routed.append((p, sidecar))
            out.append(sidecar)
        else:
            out.append(p)
    return list(dict.fromkeys(out)), routed


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

    paths, routed = _route_plan_bodies(paths)

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
            + "".join(
                f"{body} is routed to {sidecar}: record plan evidence with coordinator-invoke "
                "plan.tasks.mutate evidence-append; an executor row never edits a plan body.\n"
                for body, sidecar in routed
            )
        ),
        "writes": paths,
    }

    rows = [row]
    for n, g in enumerate(gated, start=2):
        gid = str(g.get("id") or "").strip() or f"X{n}"
        if gid == XS_ROW_ID or any(r["id"] == gid for r in rows):
            gid = f"G{n}"
        gate = {"owner_repo": str(g.get("owner_repo") or "external"), "requires": str(g.get("requires") or "landed-work")}
        if gate["requires"] == "landed-work":
            gate["closure_key"] = _landed_work_closure_key(g, sizing, stem, gid)
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

"""M arm of the ask gate: a sizing whose `acceptance_rows` are all concrete -> a plan-tasks spine.

Pure: returns the spine markdown and the path it belongs at; the caller writes it. A row is
concrete when it names its write paths and a runnable `check`, so it can be dispatched without a
planner. The sizing key is `acceptance_rows`, never `acceptance` (`GateVerdict.acceptance` is the
exit-criterion discharge record).
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

import yaml

from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _dump_rows,
    _infer_change_kind,
    DirectoryShapedFootprintError,
    _refuse_if_glob,
    is_glob_pathspec,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import s_plan_path
from coordinator_core.ops.dispatch_emit.source_memo_ref import sizing_source_memo
from coordinator_core.ops.dispatch_emit.sizing_xs_mint import (
    _route_plan_bodies,
    xs_spine_plan_id,
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _row_writes(row: Mapping) -> list[str] | None:
    writes = row.get("writes")
    if not isinstance(writes, (list, tuple)) or not writes:
        return None
    out: list[str] = []
    for w in writes:
        path = _text(w).replace("\\", "/")
        if not path or path.endswith("/") or is_glob_pathspec(path):
            return None
        out.append(path)
    return out


def concrete_acceptance(sizing: Mapping) -> list | None:
    """The sizing's `acceptance_rows` when the list is non-empty and every row has a non-empty
    `statement`, a non-empty `writes` list free of glob and directory-shaped entries, and a
    non-empty `check`; otherwise None."""
    rows = sizing.get("acceptance_rows") if isinstance(sizing, Mapping) else None
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        if not _text(row.get("statement")) or not _text(row.get("check")) or _row_writes(row) is None:
            return None
    return list(rows)


def mint_m_spine(
    sizing: Mapping,
    *,
    sizing_rel: str,
    rows: Sequence[Mapping],
    repo_root: Path | None = None,
) -> tuple[str, PurePosixPath]:
    """Return `(spine_text, s_plan_path(sizing_rel))`: one `M<n>` row per acceptance row, carrying
    its `writes`, with the statement and the check (its done condition) in the body.

    `repo_root` is where an existing-directory write is detected; None skips that check.
    Raises the inventory_mint footprint errors for a glob or directory-shaped write and
    `ValueError` for empty `rows`."""
    sizing_rel = str(sizing_rel).replace("\\", "/")
    stem = PurePosixPath(sizing_rel).stem
    if not rows:
        raise ValueError("M arm needs at least one acceptance row")

    criterion = sizing.get("exit_criterion")
    statement = _text(criterion.get("statement")) if isinstance(criterion, Mapping) else ""

    frontmatter: dict = {
        "plan_id": xs_spine_plan_id(stem),
        "run_id": stem,
        "derived_from": "sizing object",
        "sizing_object": sizing_rel,
    }
    source_memo = sizing_source_memo(sizing)
    if source_memo is not None:
        frontmatter["source_memo"] = source_memo
    if sizing.get("deliverable_id"):
        frontmatter["deliverable_id"] = str(sizing["deliverable_id"])
    frontmatter["prime_exit_criterion"] = {"statement": statement}

    out_rows: list[dict] = []
    for n, acc in enumerate(rows, start=1):
        rid = f"M{n}"
        paths = _row_writes(acc) or []
        if not paths:
            raise ValueError(f"acceptance row {rid} names no writes")
        for path in paths:
            _refuse_if_glob(rid, path, path)
            if repo_root is not None and (Path(repo_root) / path).is_dir():
                raise DirectoryShapedFootprintError(
                    f"acceptance row {rid}: write {path!r} is an existing directory; name a concrete file"
                )
        paths, routed = _route_plan_bodies(paths)
        claim = _text(acc.get("statement"))
        check = _text(acc.get("check"))
        title = next((ln.strip() for ln in claim.splitlines() if ln.strip()), rid)
        out_rows.append(
            {
                "id": rid,
                "title": title,
                "change_kind": _infer_change_kind(paths),
                "surface": paths[0],
                "body": (
                    f"Spec: {sizing_rel} ({rid})\n"
                    f"Acceptance:\n{claim}\n"
                    f"Verification (this row is DONE only when this holds): {check}\n"
                    + "".join(
                        f"{body} is routed to {sidecar}: record plan evidence with coordinator-invoke "
                        "plan.tasks.mutate evidence-append; an executor row never edits a plan body.\n"
                        for body, sidecar in routed
                    )
                ),
                "writes": paths,
            }
        )

    text = (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False)
        + "---\n\n"
        + f"# Minted dispatch spine — {stem}\n\n"
        + f"Derived from `{sizing_rel}` by the ask gate's spine-direct arm.\n\n"
        + "## Tasks\n\n"
        + "```yaml plan-tasks\n"
        + _dump_rows(out_rows)
        + "```\n"
    )
    return text, PurePosixPath(s_plan_path(sizing_rel))


_TASKS_BLOCK = re.compile(r"```yaml plan-tasks\n(.*?)```", re.DOTALL)


def _spine_rows(text: str) -> dict[str, dict]:
    match = _TASKS_BLOCK.search(text)
    try:
        loaded = yaml.safe_load(match.group(1)) if match else None
    except yaml.YAMLError:
        return {}
    return {str(r["id"]): r for r in loaded if isinstance(r, dict) and "id" in r} if isinstance(loaded, list) else {}


def spine_mismatch(existing_text: str, fresh_text: str) -> list[str]:
    """Row ids where `existing_text` differs from `fresh_text` in `writes` or in the check line;
    `["<unparseable>"]` when the existing spine has no readable plan-tasks block."""
    existing, fresh = _spine_rows(existing_text), _spine_rows(fresh_text)
    if not existing:
        return ["<unparseable>"]
    bad = [rid for rid in fresh if rid not in existing]
    bad += [rid for rid in existing if rid not in fresh]
    for rid, row in fresh.items():
        got = existing.get(rid)
        if got is None:
            continue
        check = next((ln for ln in str(row.get("body", "")).splitlines() if ln.startswith("Verification")), "")
        if got.get("writes") != row.get("writes") or check not in str(got.get("body", "")):
            bad.append(rid)
    return bad

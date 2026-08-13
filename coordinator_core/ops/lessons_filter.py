"""
coordinator_core.ops.lessons_filter — learn-lessons routing-set filters.

Purpose: two pure, payload-to-payload filters used by the learn-lessons
skill's central/local routing pipeline (`coordinator-claude coordinator/skills/
learn-lessons/SKILL.md`), collapsed into one module per the buildout plan's
chunk design (`docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md` § Wave 2 unclustered pairs):

  - ``lessons.filter_undated_universal`` — from a full lessons extraction
    YAML payload, keep only records where ``undated`` and ``tag_universal``
    are both true (the central-mode undated-pass input: entries a `--since`
    delta extraction excludes, per the SKILL's own "Why two extractions"
    note). Pure payload transform — params/response are YAML text, not file
    paths; no repo state is read (scope-verdict `none`).
  - ``lessons.reject_orphan_strip_entries`` — given a routed-records YAML and
    a strip-list YAML (both on-disk paths in this repo's lessons/state tree,
    scope-verdict `common_dir`), compute the routed-id set (every record
    whose ``change_kind`` is set and is not ``discard``) and flag any
    strip-list entry whose ``id`` is not in that set. Guards against the
    2026-06-14-shape mistake — stripping a lesson from a project's
    `state/lessons/` without a corresponding routed destination, which is
    silent doctrine loss recoverable only from git history.

Both fences already stream via stdin/argv specifically to dodge Windows/
Git-Bash msys path-resolution breakage in Python's stdlib ``open()`` on
`/c/Users/...` paths (per each fence's own comment) — porting to in-process
Python functions over parsed data structures removes that hazard entirely
rather than reproducing it: there is no stdin/env-var boundary left to cross.

Idempotency (AC7): both ops are pure reads with no side effects — re-running
either against unchanged inputs yields byte-identical output. No DEC-7
docstring note is required (manifest rates both `none` on the idempotency-
hazard column).

Spec backlink: pln-coordinator-ops-buildout-from--903224 § Wave 2
Port source: coordinator-claude coordinator/skills/learn-lessons/SKILL.md (undated-pass fence ~L601,
             strip-list orphan-rejection fence ~L649)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.ipc import register_op


def filter_undated_universal(extraction_yaml: str) -> Dict[str, Any]:
    """Filter a lessons-extraction YAML payload to undated+universal records.

    Faithful port of the SKILL's verbatim fence: parse the full extraction,
    keep only records where both ``undated`` and ``tag_universal`` are
    truthy, and re-serialize with the same field order (``sort_keys=False``,
    matching the oracle's ``yaml.safe_dump(..., sort_keys=False)``).

    Returns the filtered YAML text and the count of records kept.
    """
    data = yaml.safe_load(extraction_yaml) or {}
    records = [
        r for r in data.get("records", []) if r.get("undated") and r.get("tag_universal")
    ]
    data["records"] = records
    filtered_yaml = yaml.safe_dump(data, sort_keys=False)
    return {"filtered_yaml": filtered_yaml, "kept_count": len(records)}


def _routed_ids(records_data: Dict[str, Any]) -> set:
    """Ids of every record whose `change_kind` is set and not `discard`."""
    routed = set()
    for r in records_data.get("records", []) or []:
        change_kind = r.get("change_kind")
        if change_kind and change_kind != "discard":
            routed.add(r.get("id"))
    return routed


def reject_orphan_strip_entries(records_path: str, strip_list_path: str) -> Dict[str, Any]:
    """Flag strip-list entries with no corresponding routed record.

    Faithful port of the SKILL's two-block verbatim fence (``routed_ids`` +
    ``orphans``), collapsed into one in-process function operating on parsed
    data structures directly — no env-var (`ROUTED_IDS`) or stdin-pipe
    boundary between the two halves.

    Returns the list of orphan strip-list ids (empty if none) and an `ok`
    flag (True iff no orphans were found).
    """
    records_text = Path(records_path).read_text(encoding="utf-8")
    strip_text = Path(strip_list_path).read_text(encoding="utf-8")

    records_data = yaml.safe_load(records_text) or {}
    strip_data = yaml.safe_load(strip_text) or {}

    routed = _routed_ids(records_data)
    orphans: List[str] = [
        entry.get("id")
        for entry in (strip_data.get("strip", []) or [])
        if entry.get("id") not in routed
    ]
    return {"orphans": orphans, "ok": len(orphans) == 0}


@register_op("lessons.filter_undated_universal")
def _lessons_filter_undated_universal(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `lessons.filter_undated_universal` handler.

    Params:
        extraction_yaml (str, required) — a full lessons-extraction YAML payload.

    Returns:
        {"filtered_yaml": str, "kept_count": int}
    """
    extraction_yaml = params.get("extraction_yaml")
    if extraction_yaml is None:
        raise ValueError("lessons.filter_undated_universal requires params.extraction_yaml")
    return filter_undated_universal(extraction_yaml)


@register_op("lessons.reject_orphan_strip_entries")
def _lessons_reject_orphan_strip_entries(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `lessons.reject_orphan_strip_entries` handler.

    Params:
        records_path (str, required) — path to a routed-records YAML file.
        strip_list_path (str, required) — path to a strip-list YAML file.

    Returns:
        {"orphans": list[str], "ok": bool}
    """
    records_path = params.get("records_path")
    strip_list_path = params.get("strip_list_path")
    if not records_path or not strip_list_path:
        raise ValueError(
            "lessons.reject_orphan_strip_entries requires params.records_path "
            "and params.strip_list_path"
        )
    return reject_orphan_strip_entries(records_path, strip_list_path)

"""The census-shaped payload the atlas workflow's census-skip branch consumes.

One shape for both producers: the cartography chunk-table path and the tree-walk
fallback used when example-retrieval-repo is absent. Stdlib only; pure build and validate.
"""
from __future__ import annotations

import re
from typing import Any

SOURCES = ("cartography", "tree")
REQUIRED_KEYS = (
    "ok",
    "source",
    "censusShapedResults",
    "chunkTablePath",
    "counts",
    "oversizedSignalAvailable",
    "oversizedCount",
    "unbucketedCount",
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def build(
    results: list[dict[str, Any]],
    *,
    source: str,
    chunk_table_path: str | None,
    counts: dict[str, Any] | None,
    unbucketed: int,
    oversized_signal_available: bool = True,
) -> dict[str, Any]:
    """Wrap bucket results ({bucket_id, files[{path, loc, oversized}], anomalies}) in the payload.

    `oversizedCount` is derived from the files' `oversized` flags.
    """
    oversized = sum(1 for bucket in results for f in bucket.get("files") or [] if f.get("oversized"))
    return {
        "ok": True,
        "source": source,
        "censusShapedResults": results,
        "chunkTablePath": chunk_table_path,
        "counts": counts,
        "oversizedSignalAvailable": oversized_signal_available,
        "oversizedCount": oversized,
        "unbucketedCount": unbucketed,
    }


def _bad_path(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return True
    return "\\" in path or path.startswith("/") or bool(_DRIVE_RE.match(path))


def validate(obj: Any) -> list[str]:
    """Return one finding per violation; an empty list means the payload is well-formed."""
    if not isinstance(obj, dict):
        return ["payload is not an object"]
    findings = [f"missing key: {key}" for key in REQUIRED_KEYS if key not in obj]
    if "source" in obj and obj["source"] not in SOURCES:
        findings.append(f"unknown source: {obj['source']!r} (expected one of {', '.join(SOURCES)})")
    if "ok" in obj and obj["ok"] is not True:
        findings.append("ok is not true")
    for key in ("oversizedCount", "unbucketedCount"):
        if key in obj and (isinstance(obj[key], bool) or not isinstance(obj[key], int)):
            findings.append(f"{key} is not an integer")
    if "oversizedSignalAvailable" in obj and not isinstance(obj["oversizedSignalAvailable"], bool):
        findings.append("oversizedSignalAvailable is not a boolean")
    results = obj.get("censusShapedResults")
    if "censusShapedResults" in obj and not isinstance(results, list):
        findings.append("censusShapedResults is not a list")
        return findings
    for i, bucket in enumerate(results or []):
        if not isinstance(bucket, dict):
            findings.append(f"censusShapedResults[{i}] is not an object")
            continue
        for key in ("bucket_id", "files", "anomalies"):
            if key not in bucket:
                findings.append(f"censusShapedResults[{i}] missing key: {key}")
        if not isinstance(bucket.get("files", []), list):
            findings.append(f"censusShapedResults[{i}].files is not a list")
            continue
        for j, entry in enumerate(bucket.get("files") or []):
            if not isinstance(entry, dict):
                findings.append(f"censusShapedResults[{i}].files[{j}] is not an object")
                continue
            for key in ("path", "loc", "oversized"):
                if key not in entry:
                    findings.append(f"censusShapedResults[{i}].files[{j}] missing key: {key}")
            if "path" in entry and _bad_path(entry["path"]):
                findings.append(f"non-POSIX path: {entry['path']!r}")
    return findings

"""coordinator_core.ops.signoff_digest — "signoff.digest" op (COMPUTE_ONLY).

Lists the sign-offs on the four approval surfaces over a dated window, bucketed by who signed:
``pm_verified`` (the PM personally), ``delegated`` (the APM, a G-EM or Uhura, or another
delegate: signed without human verification) and ``unrecorded`` (approved with no provenance,
legacy).

Params: ``since`` (YYYY-MM-DD, default today-7). Files are selected by filename date >= since
(``docs/plans/*.md``, ``state/sizings/*.yaml``) and regex-prefiltered before any parse.

Item: ``{surface, source, path, target, words_or_ruling_ref, on}``. Every surface read goes through
``signoff_provenance``. Spawns nothing and writes nothing.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ipc import register_op
from coordinator_core.ops import signoff_provenance as sp

_PREFILTER = re.compile(r"pm_approved|approved|accepted|execution_authorized_by")
_FILE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_BUCKETS = ("pm_verified", "delegated", "unrecorded")
# The pure-Python loader is most of this op's cost; libyaml keeps it under the bar.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _bucket(s: sp.Signoff) -> str:
    if s.unrecorded:
        return "unrecorded"
    return "pm_verified" if s.source == sp.SOURCE_PM else "delegated"


def _item(s: sp.Signoff, path: str, target: str, fallback_on: str) -> tuple[str, dict]:
    return _bucket(s), {
        "surface": s.surface,
        "source": s.source,
        "path": path,
        "target": target,
        "words_or_ruling_ref": s.words or s.ruling_ref,
        "on": s.on or fallback_on,
    }


def _frontmatter(text: str) -> dict:
    """Full-YAML frontmatter; `dag._parse_frontmatter` keeps inline flow maps as strings."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        fm = yaml.load(text[3:end], Loader=_YAML_LOADER)
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _plan_signoffs(text: str, rel: str, file_on: str):
    fm = _frontmatter(text)
    stamp = sp.read_exec_stamp(fm)
    if stamp is not None:
        yield _item(stamp, rel, str(fm.get("plan_id") or rel), file_on)
    groupings = fm.get("grouping_approvals")
    if isinstance(groupings, dict):
        for name, block in groupings.items():
            s = sp.read_grouping(block)
            if s is not None:
                yield _item(s, rel, f"grouping:{name}", file_on)
    if "pm_approved" not in text:
        return
    found = locate_fenced_block(text)
    if found.status in (LocateStatus.ABSENT, LocateStatus.MALFORMED):
        return
    try:
        rows = yaml.load(found.body, Loader=_YAML_LOADER) or []
    except yaml.YAMLError:
        return
    for row in rows if isinstance(rows, list) else []:
        s = sp.read_row(row)
        if s is not None:
            yield _item(s, rel, f"row:{row.get('id')}", file_on)


def _sizing_signoffs(text: str, rel: str, file_on: str):
    try:
        data = yaml.load(text, Loader=_YAML_LOADER)
    except yaml.YAMLError:
        return
    crit = data.get("exit_criterion") if isinstance(data, dict) else None
    if not isinstance(crit, dict):
        return
    s = sp.read_sizing_accepted(crit.get("accepted"))
    if s is not None:
        yield _item(s, rel, Path(rel).stem, file_on)


def _summary(name: str, items: list[dict]) -> str:
    by_surface: dict[str, int] = {}
    for it in items:
        by_surface[it["surface"]] = by_surface.get(it["surface"], 0) + 1
    detail = ", ".join(f"{n} {k}" for k, n in sorted(by_surface.items()))
    return f"{name}: {len(items)}" + (f" ({detail})" if detail else "")


def _scan(root: Path, sub: str, glob: str, since: str, reader, out: dict) -> None:
    base = root / sub
    if not base.is_dir():
        return
    for path in sorted(base.glob(glob)):
        m = _FILE_DATE.match(path.name)
        if not m or m.group(1) < since:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _PREFILTER.search(text):
            continue
        rel = f"{sub}/{path.name}"
        for bucket, item in reader(text, rel, m.group(1)):
            out[bucket].append(item)


@register_op("signoff.digest")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{exit_code, since, pm_verified, delegated, unrecorded, summary}``."""
    if repo_root is None:
        return {"exit_code": 1, "error": "signoff.digest: repo_root is required"}
    raw = params.get("since")
    try:
        since = date.fromisoformat(str(raw)) if raw else date.today() - timedelta(days=7)
    except ValueError:
        return {"exit_code": 1, "error": f"signoff.digest: since must be YYYY-MM-DD, got {raw!r}"}
    out: dict[str, Any] = {b: [] for b in _BUCKETS}
    iso = since.isoformat()
    _scan(repo_root, "docs/plans", "*.md", iso, _plan_signoffs, out)
    _scan(repo_root, "state/sizings", "*.yaml", iso, _sizing_signoffs, out)
    return {
        "exit_code": 0,
        "since": iso,
        **out,
        "summary": {b: _summary(b, out[b]) for b in _BUCKETS},
    }

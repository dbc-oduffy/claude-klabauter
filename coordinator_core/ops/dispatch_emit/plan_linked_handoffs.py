"""Live handoffs that link a plan, closable once every plan they link is implemented.

A handoff links a plan through `governing_plan` (a path) or `plan_ids` (plan_id values). Frontmatter
only, no git spawn: files are prefiltered on a raw head substring before any YAML parse.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.deliverable_cascade import _plan_fk_matches, _plan_origin, _strip_root_prefix
from coordinator_core.session.record_homes import home_dir

_HEAD_BYTES = 16384
_IMPLEMENTED = "implemented"
_FM_END = re.compile(r"\n---[ \t]*(?:\r?\n|\Z)")
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _parse_head(text: str) -> dict:
    """Frontmatter mapping of `text`, `{}` when absent or malformed."""
    if not text.startswith("---"):
        return {}
    m = _FM_END.search(text, 3)
    block = text[3:m.start()] if m else text[3:]
    try:
        data = yaml.load(block, Loader=_LOADER)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_head(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _plan_files(root: Path, plan_rel: str) -> list[Path]:
    """Candidate on-disk locations of a plan: live path, origin path, then archive copies."""
    origin = _plan_origin(plan_rel.strip().replace("\\", "/"))
    out = [root / origin]
    arch = root / "archive" / "specs"
    if arch.is_dir():
        name = origin.rsplit("/", 1)[-1]
        out.extend(sorted(arch.glob(f"*/{name}")))
    return out


def _plan_fm(root: Path, plan_rel: str) -> Optional[dict]:
    for p in _plan_files(root, plan_rel):
        if p.is_file():
            return _parse_head(_read_head(p))
    return None


def _plan_implemented(root: Path, plan_rel: str) -> bool:
    fm = _plan_fm(root, plan_rel)
    return bool(fm) and str(fm.get("status", "")).strip() == _IMPLEMENTED


def _plan_id_index(root: Path) -> dict[str, dict]:
    """plan_id -> frontmatter, over docs/plans and archived specs (one pass)."""
    index: dict[str, dict] = {}
    dirs = [root / "docs" / "plans"]
    arch = root / "archive" / "specs"
    if arch.is_dir():
        dirs.extend(sorted(d for d in arch.iterdir() if d.is_dir()))
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            head = _read_head(f)
            if "plan_id" not in head:
                continue
            fm = _parse_head(head)
            pid = fm.get("plan_id")
            if isinstance(pid, str) and pid.strip():
                index.setdefault(pid.strip(), fm)
    return index


def _own_plan_id(root: Path, plan_rel: str) -> str:
    fm = _plan_fm(root, plan_rel) or {}
    pid = fm.get("plan_id")
    return pid.strip() if isinstance(pid, str) else ""


def _ids_of(fm: dict) -> list[str]:
    raw = fm.get("plan_ids")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [x.strip() for x in raw if isinstance(x, str) and x.strip()]


def _links(fm: dict, plan_rel: str, plan_id: str) -> bool:
    if _plan_fk_matches(fm.get("governing_plan"), plan_rel):
        return True
    return bool(plan_id) and plan_id in _ids_of(fm)


def _all_implemented(fm: dict, root: Path, index_box: list) -> bool:
    gp = fm.get("governing_plan")
    if isinstance(gp, str) and gp.strip():
        if not _plan_implemented(root, _strip_root_prefix(gp)):
            return False
    ids = _ids_of(fm)
    if ids:
        if not index_box:
            index_box.append(_plan_id_index(root))
        for pid in ids:
            target = index_box[0].get(pid)
            if target is None or str(target.get("status", "")).strip() != _IMPLEMENTED:
                return False
    return True


def _live(fm: dict) -> bool:
    state = fm.get("deployment_state")
    return not (isinstance(state, str) and state.strip() in HANDOFF_TERMINAL_DEPLOYMENT)


def links_and_all_implemented(fm: dict, plan_rel: str, root: Any) -> bool:
    """True when the handoff frontmatter `fm` is live, links `plan_rel`, and every plan it links is implemented."""
    root = Path(root)
    if not isinstance(fm, dict) or not _live(fm):
        return False
    if not _links(fm, plan_rel, _own_plan_id(root, plan_rel)):
        return False
    return _all_implemented(fm, root, [])


def linked_handoffs(root: Path, plan_rel: str) -> list[str]:
    """Repo-relative POSIX paths of live handoffs linking `plan_rel` whose every linked plan is implemented."""
    root = Path(root)
    hdir = Path(home_dir(str(root), "handoffs"))
    if not hdir.is_dir():
        return []
    plan_rel = plan_rel.strip().replace("\\", "/")
    plan_id = _own_plan_id(root, plan_rel)
    needles = [plan_rel.rsplit("/", 1)[-1]]
    if plan_id:
        needles.append(plan_id)
    index_box: list = []
    out: list[str] = []
    for f in sorted(hdir.glob("*.md")):
        head = _read_head(f)
        if not any(n in head for n in needles):
            continue
        fm = _parse_head(head)
        if not fm or not _live(fm) or not _links(fm, plan_rel, plan_id):
            continue
        if _all_implemented(fm, root, index_box):
            out.append(f.relative_to(root).as_posix())
    return out

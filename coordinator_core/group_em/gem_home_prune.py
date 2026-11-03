"""G-EM home prune: verified-backup removal of residue and old audits, and baton archive.

A candidate is deleted only after its cold-storage copy hashes (sha256) equal to the source; a
mismatch, or no cold dir, keeps the file and lists it under `refused`. Consumed batons move to
`<home>/archive/handoffs/`. The cold dir is a machine-local key, never a tracked path.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import re
import shutil
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.session import record_homes

COLD_DIR_KEY = "coordinator.group_em_home_cold_dir"
RESIDUE_DIR = "state/residue/inbox-blitz"
AUDITS_DIR = Path(record_homes.home_dir(".", "audits")).as_posix().removeprefix("./")
HANDOFFS_DIR = Path(record_homes.home_dir(".", "handoffs")).as_posix().removeprefix("./")
ARCHIVE_DIR = "archive/handoffs"
# `claimed` alone is not terminal here: the claimer may still be running.
_TERMINAL_STATUS = frozenset({"consumed", "superseded"})
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_FM_LINE = re.compile(r"^([A-Za-z_]+):\s*(.*?)\s*(?:#.*)?$")


def cold_dir() -> Path | None:
    """The machine-local cold-storage dir, else None."""
    val = machine_resolver.registry_get(COLD_DIR_KEY)
    return Path(val) if val else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _audit_candidates(home: Path) -> list[Path]:
    """Dated audit files older than the newest run date; undated files are never candidates."""
    dated = [(m.group(1), p) for p in _files(home / AUDITS_DIR) if (m := _DATE_PREFIX.match(p.name))]
    if not dated:
        return []
    newest = max(d for d, _ in dated)
    return [p for d, p in dated if d < newest]


def _backup_and_remove(home: Path, cands: list[Path], cold: Path | None, out: dict) -> None:
    for src in cands:
        rel = src.relative_to(home).as_posix()
        if cold is None:
            out["refused"].append({"path": rel, "reason": "no-cold-dir"})
            continue
        dst = cold / "group-em-home" / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if _sha256(dst) != _sha256(src):
                out["refused"].append({"path": rel, "reason": "sha256-mismatch"})
                continue
            src.unlink()
        except OSError as exc:
            out["refused"].append({"path": rel, "reason": f"io-error: {exc.__class__.__name__}"})
            continue
        out["removed"].append(rel)
        out["backup"].append(str(dst))


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = _FM_LINE.match(line)
        if m:
            fm[m.group(1)] = m.group(2).strip("'\"")
    return fm


def is_consumed_baton(path: Path) -> bool:
    fm = _frontmatter(path)
    return fm.get("status") in _TERMINAL_STATUS or fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT


def _archive_batons(home: Path, out: dict) -> None:
    hdir = home / HANDOFFS_DIR
    for src in sorted(hdir.glob("*.md")) if hdir.is_dir() else []:
        if not is_consumed_baton(src):
            continue
        m = _DATE_PREFIX.match(src.name)
        month = m.group(1)[:7] if m else "undated"
        dst = home / ARCHIVE_DIR / month / src.name
        rel = src.relative_to(home).as_posix()
        if dst.exists():
            out["refused"].append({"path": rel, "reason": "archive-target-exists"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        out["archived"].append(dst.relative_to(home).as_posix())


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def clean_paths(home: Path, apply: bool = False) -> dict:
    """Run fix-concrete-path-citations over the home's tracked-shape markdown (dry-run by default)."""
    from coordinator_core.ops.session import fix_concrete_path_citations as fcp

    paths = [str(p) for sub in ("state", "docs") for p in _files(home / sub) if p.suffix == ".md"]
    if not paths:
        return {"applied": apply, "rc": 0, "files": 0}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fcp.main((["--apply"] if apply else []) + paths)
    return {"applied": apply, "rc": rc, "files": len(paths)}


def prune(home, cold: Path | str | None = None, fix_paths: bool = False) -> dict:
    """Prune `home`; returns {removed, archived, backup, refused[, path_cleanup]}.

    `cold` defaults to the machine-local `coordinator.group_em_home_cold_dir`. With no cold dir,
    nothing is deleted (every candidate lands in `refused`) but baton archiving still runs.
    `fix_paths` is set by `group-em-home prune --fix-paths`."""
    home = Path(home)
    cold_path = Path(cold) if cold else cold_dir()
    out: dict = {"removed": [], "archived": [], "backup": [], "refused": []}
    cands = _files(home / RESIDUE_DIR) + _audit_candidates(home)
    _backup_and_remove(home, cands, cold_path, out)
    _prune_empty_dirs(home / RESIDUE_DIR)
    _archive_batons(home, out)
    if fix_paths:
        out["path_cleanup"] = clean_paths(home, apply=True)
    return out

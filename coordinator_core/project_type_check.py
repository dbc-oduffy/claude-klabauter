"""Verdict and seed for a repo's `project_type` declaration against its UE markers.

Invariant: `declared_project` and `declared_ue` are the only declaration readers; this module
parses no frontmatter, it only edits the two keys when seeding. Stdlib only, no subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.ue_project_detection import (
    LOCAL_MD_RELPATHS,
    declared_project,
    declared_ue,
    is_skipped_path,
)

CANONICAL_TYPES: frozenset[str] = frozenset({"general", "game-dev", "web-dev", "data-science"})
LEGACY_HINT = "map to {general, game-dev, web-dev, data-science}"
SEED_UE: dict = {"project_type": "game-dev", "project_subtypes": ["unreal"]}
SEED_GENERAL: dict = {"project_type": "general"}


def ue_markers(root: str | os.PathLike) -> list[str]:
    """Repo-relative posix paths of every `*.uproject` at `root` or one level down, skip rule applied."""
    base = Path(root)
    found: list[str] = []
    for pattern in ("*.uproject", "*/*.uproject"):
        for hit in base.glob(pattern):
            rel = hit.relative_to(base).as_posix()
            if hit.is_file() and not is_skipped_path(rel):
                found.append(rel)
    return sorted(found, key=lambda r: (r.count("/"), r))


def check_root(root: str | os.PathLike) -> dict:
    """The verdict dict (contract JSON shape, `applied` false, `after` None) for one repo root."""
    root_s = str(root)
    markers = ue_markers(root)
    ue = declared_ue(root)
    decl = declared_project(root)
    dtype = decl.project_type if decl is not None else None
    cmd = f"check-project-type {root_s} --apply"
    out: dict = {
        "root": root_s,
        "declared_ue": ue,
        "declared_type": dtype,
        "markers": markers,
        "repair": None,
        "applied": False,
        "after": None,
    }
    if ue is False and dtype is not None and dtype not in CANONICAL_TYPES:
        out.update(verdict="conflict", reason="legacy-value", hint=LEGACY_HINT)
    elif markers and ue is None:
        out.update(verdict="seedable", reason="markers-undeclared",
                   repair={"command": cmd, "writes": dict(SEED_UE)})
    elif markers and ue is False:
        out.update(verdict="conflict", reason="markers-declared-non-ue",
                   hint=f"add unreal to project_subtypes in {decl.source_file if decl else root_s}")
    elif ue is None:
        out.update(verdict="missing", reason="no-markers-undeclared",
                   repair={"command": cmd, "writes": dict(SEED_GENERAL)})
    else:
        out.update(verdict="ok", reason="consistent")
    return out


def seed_target(root: Path) -> Path:
    for rel in LOCAL_MD_RELPATHS:
        if (root / rel).is_file():
            return root / rel
    return root / LOCAL_MD_RELPATHS[1]


def _splice_keys(text: str, writes: dict) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    new = [f"project_type: {writes['project_type']}{eol}"]
    if "project_subtypes" in writes:
        new.append(f"project_subtypes: [{', '.join(writes['project_subtypes'])}]{eol}")
    marks = [i for i, ln in enumerate(lines) if ln.rstrip("\r\n") == "---"]
    if not marks:
        return f"---{eol}{''.join(new)}---{eol}{text}"
    close = marks[1] if len(marks) > 1 else len(lines)
    insert_at = marks[0] + 1
    kept = [ln for ln in lines[insert_at:close] if not ln.startswith(("project_type:", "project_subtypes:"))]
    out = lines[:insert_at] + new + kept
    if len(marks) < 2:
        out.append(f"---{eol}")
    return "".join(out + lines[close:])


def seed(root: str | os.PathLike, writes: dict) -> Path:
    """Insert the seed keys into the file `declared_ue` reads (undeclared repos only); returns its path."""
    target = seed_target(Path(root))
    text = ""
    if target.is_file():
        with open(target, encoding="utf-8", newline="") as fh:
            text = fh.read()
    tmp = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(_splice_keys(text, writes), encoding="utf-8", newline="")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target


def check_and_apply(root: str | os.PathLike, apply: bool = False) -> dict:
    """`check_root`, then seed a seedable or missing repo when `apply`; `after` holds the re-run verdict."""
    result = check_root(root)
    if apply and result["repair"] is not None:
        seed(root, result["repair"]["writes"])
        result["applied"] = True
        result["after"] = check_root(root)["verdict"]
    return result

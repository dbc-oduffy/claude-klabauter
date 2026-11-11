"""One-shot, idempotent carry-over of the legacy ``<common>/coordinator-sessions`` hub.

Merge-copies the pre-resolver hub under ``.git`` into ``session_hub_path.hub_for_common``.
Never deletes the legacy tree. Plain files: copied when the destination is missing or
older. Append-only ``*.jsonl``: never newer-wins; the legacy lines the destination lacks
are appended, so appends made through the old path during a mixed-version window survive.
Bare repos (common dir not named ``.git``) are a no-op: the resolver hub is the legacy hub.

Run: ``python -m coordinator_core.session.migrate_hub [--repo <root>]``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Optional

from coordinator_core.session_hub_path import HUB_NAME, hub_for_common

GENERATES: list = []


def _union_jsonl(src: Path, dst: Path) -> bool:
    """Append *src* lines absent from *dst*; return whether *dst* changed."""
    src_lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
    existing = dst.read_text(encoding="utf-8", errors="replace")
    have = set(existing.splitlines())
    missing = [ln for ln in src_lines if ln and ln not in have]
    if not missing:
        return False
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with open(dst, "a", encoding="utf-8", newline="") as fh:
        fh.write(prefix + "\n".join(missing) + "\n")
    return True


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy via a sibling temp name: an interrupted copy must never leave a truncated *dst*, because
    copy2 keeps the source mtime and a rerun would then skip the truncated file as current."""
    tmp = dst.with_name(dst.name + ".migrate-tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def migrate_common(common: Path) -> Dict[str, int]:
    """Merge the legacy hub of git common dir *common*; return per-outcome counts."""
    counts = {"copied": 0, "merged": 0, "skipped": 0}
    common = Path(common)
    if common.name != ".git":
        return counts
    # Deliberate old-location read: this op exists to carry the pre-migration hub under .git forward.
    legacy = common / HUB_NAME
    if not legacy.is_dir():
        return counts
    hub = hub_for_common(common)
    for src in sorted(p for p in legacy.rglob("*") if p.is_file()):
        dst = hub / src.relative_to(legacy)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            _atomic_copy(src, dst)
            counts["copied"] += 1
        elif src.suffix == ".jsonl":
            counts["merged" if _union_jsonl(src, dst) else "skipped"] += 1
        elif src.stat().st_mtime > dst.stat().st_mtime:
            _atomic_copy(src, dst)
            counts["copied"] += 1
        else:
            counts["skipped"] += 1
    return counts


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=None, help="repo root (default: cwd)")
    args = ap.parse_args(argv)
    from coordinator_core.git.repo_root import git_common_dir

    common = git_common_dir(args.repo)
    if not common:
        print("migrate_hub: not a git repo; nothing to do")
        return 0
    c = migrate_common(Path(common))
    print(f"migrate_hub: copied={c['copied']} merged={c['merged']} skipped={c['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

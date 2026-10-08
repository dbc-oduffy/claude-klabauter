"""A `process_target` stand-in for tests that drive `publish.main` end to end.

The fake stages the row's destination tree as the row's payload, so the round
unions, gates and lands it exactly as it would a real row. On first sight the
destination is normalised to LF and committed whole, so `land_diff` has a clean
HEAD that already matches the staged bytes.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(dest: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(dest), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


def _tree_files(dest: Path) -> "list[Path]":
    return [
        p
        for p in dest.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(dest).parts
    ]


def seed_repo(dest: Path) -> None:
    """Fixtures written with `write_text` on Windows are CRLF; a real source tree is LF."""
    for path in _tree_files(dest):
        data = path.read_bytes()
        if b"\r\n" in data:
            path.write_bytes(data.replace(b"\r\n", b"\n"))
    if not (dest / ".git" / "HEAD").exists():
        subprocess.run(["git", "init", "-q", str(dest)], check=True, creationflags=_NO_WINDOW)
        _git(dest, "add", "-A")
        _git(dest, "commit", "-q", "--allow-empty", "-m", "seed")


def make_stage_fake(publish, *, changed_known: bool = True):
    """Build the fake against the loaded `publish` module (for `StagedRowResult`)."""

    def _stage_dest_tree_as_row(target, setup_dir, totals, **kwargs):
        if kwargs.get("dry_run"):
            totals.processed += 1
            return None
        dest = Path(target.dest_dir)
        staging = Path(tempfile.mkdtemp(prefix=".publish-staging-"))
        rels: "set[str]" = set()
        if dest.is_dir():
            seed_repo(dest)
            for path in _tree_files(dest):
                rel = path.relative_to(dest)
                (staging / rel).parent.mkdir(parents=True, exist_ok=True)
                (staging / rel).write_bytes(path.read_bytes())
                rels.add(rel.as_posix())
        totals.processed += 1
        return publish.StagedRowResult(
            staging_dir=staging,
            row_visited={Path(rel) for rel in rels},
            row_changed_files=set(rels) if changed_known else None,
            row_removed_files=set(),
            row_published_files={Path(rel) for rel in rels},
            report_text="",
            synced=len(rels),
            deleted=0,
        )

    return _stage_dest_tree_as_row

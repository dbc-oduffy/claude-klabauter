#!/usr/bin/env python3
"""Relative markdown links in published docs must resolve inside this repo.

The docs shipped here were authored in a meta-repo with a different layout, and
some of their links point at sibling repositories that do not exist in the
mirror. Failing on those would make the gate unusable; skipping every failure
would make it worthless. The rule that separates the two:

  A link is CHECKED when its first path component names a top-level entry that
  exists in this repo. Otherwise it is a cross-repo reference and is skipped.

That way a broken link into `docs/` fails, while a link into a sibling-repo tree
that was never published is ignored.

MID-BOOTSTRAP DEGRADATION
  A top-level directory that exists but is still empty (the engine tree lands
  after the first publish) is treated as not-yet-present: links into it are
  skipped rather than failed, and they start being enforced the moment its
  contents arrive.

EXIT CONTRACT
  0 — every checkable relative link resolves (or there are no .md files)
  1 — at least one broken link
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import read_text, repo_files, repo_root  # noqa: E402

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

SKIP_PREFIXES = ("http://", "https://", "#", "mailto:", "/", "tel:", "data:")


def lines_outside_code_fences(text: str):
    in_fence = False
    for line_num, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line_num, line


def present_top_level(root: pathlib.Path) -> set[str]:
    """Top-level entries that are actually populated in this tree."""
    present = set()
    for entry in root.iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir():
            if any(p.is_file() for p in entry.rglob("*")):
                present.add(entry.name)
        else:
            present.add(entry.name)
    return present


def main() -> int:
    root = repo_root()
    md_files = [p for p in repo_files(root) if p.endswith(".md")]

    if not md_files:
        print("Reference validation: no .md files in tree — nothing to validate.")
        return 0

    top_level = present_top_level(root)
    errors: list[str] = []
    checked = 0

    for rel in md_files:
        text = read_text(root / rel)
        if text is None:
            continue
        base_dir = (root / rel).parent

        for line_num, line in lines_outside_code_fences(text):
            for match in LINK_RE.finditer(line):
                target = match.group(1)
                if target.startswith(SKIP_PREFIXES):
                    continue
                target_path = target.split("#", 1)[0]
                if not target_path:
                    continue

                resolved = (base_dir / target_path).resolve()
                try:
                    inside = resolved.relative_to(root.resolve())
                except ValueError:
                    continue  # escapes the repo — cross-repo reference
                parts = inside.parts
                if not parts or parts[0] not in top_level:
                    continue  # names nothing published here — cross-repo or mid-bootstrap

                checked += 1
                if not resolved.exists():
                    errors.append(f"{rel}:{line_num}: broken link '{target}' — target not found")

    if errors:
        print("Reference validation FAILED:")
        for err in errors:
            print(f"  {err}")
        return 1

    print(f"Reference validation passed ({len(md_files)} files, {checked} in-repo links checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

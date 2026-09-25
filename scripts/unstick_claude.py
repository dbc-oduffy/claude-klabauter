#!/usr/bin/env python3
"""Give the bare `claude` command back to the real Claude Code.

    python3 scripts/unstick_claude.py        (Windows: python scripts\\unstick_claude.py)

For a box where `claude` fails before Claude Code starts because a coordinator
shell shim shadows it. Removes every rendered shim and every rc/profile block
that sources one, then says so in plain words. Safe to run on any box, any
number of times: with nothing to remove it changes nothing.

Stdlib only and self-contained by design. It must work on the box where the
engine, the registry, and every coordinator surface are broken, since that is
the box that needs it; never import `coordinator_core` here.

Negative-spec: touches only the two shim files and text between the generator's
own sentinel lines (`coordinator_core.ops.gen_claude_doe_shim.SENTINEL_BEGIN` /
`SENTINEL_END`, copied below because this file cannot import them). Never edits
an operator's own rc lines, the `.doe-root` pointer, the registry, or the plugin.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SENTINEL_BEGIN = "# --- coordinator claude-doe shim [generated] ---"
SENTINEL_END = "# --- end coordinator claude-doe shim ---"
SHIM_NAMES = ("claude-doe-shim.sh", "claude-doe-shim.ps1")


def _homes() -> list[Path]:
    homes = [os.environ.get(k) for k in ("CLAUDE_HOME", "HOME", "USERPROFILE")]
    homes.append(os.path.expanduser("~"))
    unique: list[Path] = []
    for h in homes:
        if h and Path(h) not in unique:
            unique.append(Path(h))
    return unique


def _rc_files(home: Path) -> list[Path]:
    names = [".zshrc", ".bashrc", ".bash_profile", ".profile", ".zprofile"]
    paths = [home / n for n in names]
    for docs in ("PowerShell", "WindowsPowerShell"):
        paths.append(home / "Documents" / docs / "Microsoft.PowerShell_profile.ps1")
    return paths


def _strip_block(text: str) -> str | None:
    """`text` without the generated block, or None when it carries none. An
    unterminated block keeps everything after BEGIN, since a missing END means
    the lines below were not written by the generator."""
    lines = text.split("\n")
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        if lines[i].strip() == SENTINEL_BEGIN and SENTINEL_END in (l.strip() for l in lines[i:]):
            while lines[i].strip() != SENTINEL_END:
                i += 1
            i += 1
            changed = True
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out) if changed else None


def main() -> int:
    removed: list[str] = []
    problems: list[str] = []
    for home in _homes():
        for name in SHIM_NAMES:
            shim = home / ".claude" / "shell" / name
            if shim.is_file():
                try:
                    shim.unlink()
                    removed.append(str(shim))
                except OSError as exc:
                    problems.append(f"{shim}: {exc}")
        for rc in _rc_files(home):
            if not rc.is_file():
                continue
            try:
                stripped = _strip_block(rc.read_text(encoding="utf-8", errors="replace"))
                if stripped is not None:
                    tmp = rc.with_name(rc.name + ".unstick.tmp")
                    tmp.write_text(stripped, encoding="utf-8", newline="")
                    os.replace(tmp, rc)
                    removed.append(f"the coordinator block in {rc}")
            except OSError as exc:
                problems.append(f"{rc}: {exc}")

    for line in removed:
        print(f"Removed {line}")
    for line in problems:
        print(f"Could not change {line}", file=sys.stderr)
    if problems:
        print("\nSome files could not be changed (listed above). Until then, start Claude Code with:  command claude")
        return 1
    if removed:
        print("\nFixed. Close this terminal window, open a new one, and run  claude  as usual.")
    else:
        print("Nothing to fix: no coordinator shim is installed on this computer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

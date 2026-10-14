"""Advisory fleet lint: project CLAUDE.md files that teach hand-authored handoffs.

Flags any fleet repo's CLAUDE.md naming the retired `.claude/handoffs` path, or telling the agent
to write a handoff file manually. Repos come from an in-process registry read (`repos.*`), so the
leg spawns no process. One line per offending repo and line number, plus the remedy. Always
returns 0; unreadable repos are skipped.

Port of coordinator-content-repo coordinator/hooks/scripts/check-fleet-claude-md-handoff-convention.py.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

CLAUDE_MD_RELPATHS = ("CLAUDE.md", ".claude/CLAUDE.md")
REMEDY = "replace with: run /handoff; handoffs live in state/handoffs/"
RETIRED_PATH = re.compile(r"\.claude(?:/|\\)handoffs")
MANUAL_WRITE = re.compile(
    r"\b(write|writes|author|create|save|hand-?author)\w*\s+(?:a|the|your|an)?\s*(?:session\s+)?handoff"
    r"(?:\s+(?:file|doc\w*|baton))?\s+(?:to|in|at|under|manually|by hand)\b"
    r"|\bhandoff\s+file\s+(?:manually|by hand)\b",
    re.I,
)
NEGATED = re.compile(r"\b(never|do not|don't|retired|instead of)\b", re.I)
RETIRED_MARK = re.compile(r"\b(retired|never|do not)\b", re.I)


def fleet_repos(raw: Optional[Dict[str, object]] = None) -> Dict[str, Path]:
    """`repos.*` registry entries that are existing directories, de-duplicated by resolved path.

    `raw` is a flat registry mapping the caller already holds; omitted, the merged registry is read
    in process.
    """
    if raw is None:
        from coordinator_core.machine_resolver import merged_flat_registry

        raw = merged_flat_registry()
    out: Dict[str, Path] = {}
    seen: set = set()
    for key, val in raw.items():
        if not key.startswith("repos.") or not isinstance(val, str) or not val:
            continue
        path = Path(val)
        if path.is_dir() and path.resolve() not in seen:
            seen.add(path.resolve())
            out[key[len("repos."):]] = path
    return out


def _offends(line: str) -> bool:
    """A retired path flags unless "retired"/"never"/"do not" precedes it; a manual-write phrase
    flags unless a negator precedes it."""
    path_hit = RETIRED_PATH.search(line)
    if path_hit and not RETIRED_MARK.search(line[: path_hit.start()]):
        return True
    write_hit = MANUAL_WRITE.search(line)
    return bool(write_hit and not NEGATED.search(line[: write_hit.start()]))


def scan_repo(root: Path) -> Iterator[Tuple[str, int, str]]:
    """Yield (relpath, line number, stripped line) for each offending CLAUDE.md line."""
    for rel in CLAUDE_MD_RELPATHS:
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _offends(line):
                yield rel, n, line.strip()[:120]


def report(repos: Dict[str, Path]) -> List[str]:
    """One line per offence, remedy included."""
    return [
        f"{name}: {rel}:{n}: hand-authored handoff convention ({snippet!r}) -- {REMEDY}"
        for name, root in repos.items()
        for rel, n, snippet in scan_repo(root)
    ]


def main(argv: Optional[List[str]] = None) -> int:
    """Print offences; always return 0."""
    try:
        repos = fleet_repos()
    except Exception as exc:  # advisory lint: a registry failure skips, never fails
        print(f"lint skipped -- {exc or type(exc).__name__}")
        return 0
    for line in report(repos):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

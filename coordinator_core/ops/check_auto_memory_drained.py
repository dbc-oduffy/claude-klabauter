"""
coordinator_core.ops.check_auto_memory_drained — closure-ceremony drain gate.

Discharges AC15 of example-doctrine-repo's
``docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md`` (chunk C13).
C12's ``guard_memory_store_cap`` bounds how big the auto-memory store gets
WITHIN a day (a spatial bound); this gate bounds how long anything survives
in it AT ALL (a temporal bound). The two are complementary, not redundant —
see ``coordinator/docs/wiki/coordinator-tripwires.md``'s
``MEMORY-NOT-DURABLE-CAPTURE`` entry. Neither supersedes the other.

THE POINT: auto-memory is ephemeral by definition. A closure ceremony must
drain it to zero — every entry either earns a durable home (promoted, via
the ceremony's own disposition pass) or is dropped, with no "carry this one
over" exemption. This module is the enforcing half of that; the disposition
pass itself lives in ceremony prose (`coordinator/commands/workday-complete.md`
and its siblings), never here — this gate has no semantic access to a row's
meaning and makes no promote/drop decision.

ZERO MEANS THE DIRECTORY, NOT THE INDEX. A store with a truncated
``MEMORY.md`` but surviving sibling body files is NOT drained — the bodies
are what recall injects on a match; an orphaned body with no index row is
strictly worse than one with (invisible to inspection, still live in
retrieval). This gate fails on ANY ``*.md`` file directly under the memory
dir, ``MEMORY.md`` included, not just the index.

Home resolution: copied verbatim (fidelity rule, C13 body's explicit
instruction) from
``coordinator_core.write_guards.guard_memory_store_cap._guarded_project_roots``,
itself copied from ``coordinator_core.write_guards.block_home_dir_memo_delivery``
— union of ``USERPROFILE``, ``HOME``, ``Path.home()``, plus ``CLAUDE_HOME``
treated as a direct ``.claude`` root, never a first-wins chain (a Git-Bash
``HOME``/``USERPROFILE`` divergence on Windows must never leave a real
memory dir unchecked). ALL resolved roots are checked, not just the first
match — the drain must be complete across every candidate home, not merely
the one this process happens to resolve first.

Project-slug resolution: the target repo's absolute path with OS path
separators replaced by ``-``, mirroring Claude Code's own
``~/.claude/projects/<slug>/`` naming (verified against this repo's own
live slug, ``-Users-example-operator-X-claude-klabauter``, and example-doctrine-repo's,
``-Users-example-operator-X-example-doctrine-repo`` — neither contains a character this
replacement does not cover). This is deliberately the same shape as
``check_harvest_debt``'s ``--root`` resolution (explicit flag, else
``git rev-parse --show-toplevel``) — no new resolution idiom introduced.

Exit codes:
    0 — memory dir absent (nothing to drain), OR present and holding zero
        ``*.md`` files (drained), OR ``--root`` unresolvable (consistent
        with ``check_harvest_debt``'s no-op-on-unresolvable-root
        convention — this gate has no repo to check).
    1 — memory dir holds at least one ``*.md`` file. Every offending path is
        printed to stderr, one per line, so the ceremony's disposition pass
        has a concrete worklist rather than a bare failure.

Spec backlink: example-doctrine-repo
  docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md § C13, AC15.

Negative-spec:
  - Does NOT decide promote vs. drop for any entry — that is the ceremony
    prose's disposition pass (a real authoring act), not this gate's
    concern. This module only detects non-zero residue and reports it.
  - Does NOT delete, truncate, or otherwise mutate any file. Read-only,
    matching every other closure-ceremony gate op in this package.
  - Does NOT walk into subdirectories of the memory dir — AC15's universe
    is direct children only (the index file and its sibling body files),
    matching ``guard_memory_store_cap``'s own three-part relative-path
    shape (``<slug>/memory/<filename>``).
  - Does NOT invent a new home-resolution idiom — copied verbatim from
    ``guard_memory_store_cap`` per the C13 body's explicit instruction.
  - Never raises: any unexpected input or filesystem error on a single
    candidate root is skipped (treated as "nothing found under this root"),
    not fatal to the whole check — matching every guard this idiom is
    copied from.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import List

_CLAUDE_DIRNAME = ".claude"
_PROJECTS_DIRNAME = "projects"
_MEMORY_DIRNAME = "memory"


def _guarded_project_roots() -> "List[Path]":
    """``<home>/.claude/projects`` roots to check, union of USERPROFILE /
    HOME / Path.home(), plus CLAUDE_HOME treated as a direct ``.claude``
    root. Verbatim copy (shape-for-shape) of
    ``coordinator_core.write_guards.guard_memory_store_cap._guarded_project_roots``
    — see that module's own docstring for why this cannot be a first-wins
    chain."""
    homes: "List[str]" = []
    for env_key in ("USERPROFILE", "HOME"):
        val = os.environ.get(env_key, "")
        if val and val.strip():
            homes.append(val.strip())
    try:
        homes.append(str(Path.home()))
    except Exception:
        pass

    roots: "List[Path]" = []
    for home in homes:
        try:
            roots.append(Path(home) / _CLAUDE_DIRNAME / _PROJECTS_DIRNAME)
        except Exception:
            continue

    claude_home = os.environ.get("CLAUDE_HOME", "")
    if claude_home and claude_home.strip():
        try:
            roots.append(Path(claude_home.strip()) / _PROJECTS_DIRNAME)
        except Exception:
            pass

    seen: "set[str]" = set()
    out: "List[Path]" = []
    for r in roots:
        key = os.path.normcase(str(r))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _slugify_repo_root(root: str) -> str:
    """Encode an absolute repo path the way Claude Code names its
    ``~/.claude/projects/<slug>/`` directory: every OS path separator
    becomes ``-``. Mirrors the decode direction documented in
    ``discover_working_repos._decode_projects_dir_name`` (dashes -> path
    separators); this is that mapping's forward encode."""
    normalized = str(root).replace("\\", "/")
    return normalized.replace("/", "-")


def _resolve_root(explicit_root: str | None) -> str | None:
    if explicit_root:
        return explicit_root
    if which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _residue_md_files(memory_dir: Path) -> "List[Path]":
    try:
        if not memory_dir.is_dir():
            return []
        return sorted(
            p for p in memory_dir.iterdir() if p.is_file() and p.suffix == ".md"
        )
    except OSError:
        return []


def main(argv: "List[str]") -> int:
    explicit_root: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        else:
            i += 1

    root = _resolve_root(explicit_root)
    if not root:
        return 0

    slug = _slugify_repo_root(os.path.abspath(root))

    residue: "List[Path]" = []
    for projects_root in _guarded_project_roots():
        memory_dir = projects_root / slug / _MEMORY_DIRNAME
        residue.extend(_residue_md_files(memory_dir))

    if not residue:
        return 0

    print(
        "auto-memory drain gate: BLOCKED -- the following file(s) remain under "
        "the auto-memory store at close. Every entry must be PROMOTEd to a "
        "durable home or explicitly DROPped -- silence is not a disposition. "
        "See coordinator/docs/wiki/coordinator-tripwires.md's "
        "MEMORY-NOT-DURABLE-CAPTURE entry.",
        file=sys.stderr,
    )
    for path in residue:
        print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
coordinator_core.ops.check_auto_memory_drained — closure-ceremony drain gate.

Discharges AC15 of DoE-claude's
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

ZERO MEANS THE CLOSING SESSION'S OWN ROWS, NOT THE WHOLE STORE. On a
multi-session repo the memory dir routinely holds bodies written by OTHER
live sessions sharing the same store; those are not this closer's residue
and must never be reported, let alone ordered deleted. The gate's universe
is therefore narrowed to row granularity, attributed by identity:

  - A body file (any ``*.md`` other than ``MEMORY.md``) counts as residue
    only when its YAML frontmatter's ``metadata.originSessionId`` equals
    the closing session's own resolved id. A body with no frontmatter, no
    ``metadata`` block, or no ``originSessionId`` is never this closer's
    and never counts.
  - ``MEMORY.md`` itself carries no frontmatter and can never carry an
    ``originSessionId`` directly — it is counted PER ROW instead. Each of
    its ``- [Title](file.md) — hook`` rows resolves to a body file whose
    ``originSessionId`` IS readable; the index fails the gate if AND ONLY
    IF at least one of its rows still resolves to a body owned by the
    closing session. A row whose link target does not exist on disk is a
    dangling pointer (a different problem) and does not count; a row that
    is not a parseable markdown link is skipped, not counted either.

This resolved row-level contract replaces an earlier, single-session-era
design that failed on every ``*.md`` direct child with no ownership filter
at all — on a busy multi-session repo that read as either permanently red,
or worse, an instruction to delete peers' in-flight memory. See this
module's fail-closed branch below for the companion half of that fix.

Home resolution: copied verbatim (fidelity rule, C13 body's explicit
instruction) from
``coordinator_core.write_guards.guard_memory_store_cap._guarded_project_roots``,
itself copied from ``coordinator_core.write_guards.block_home_dir_memo_delivery``
— union of ``USERPROFILE``, ``HOME``, ``Path.home()``, plus ``CLAUDE_HOME``
treated as a direct ``.claude`` root, never a first-wins chain (a Git-Bash
``HOME``/``USERPROFILE`` divergence on Windows must never leave a real
memory dir unchecked). ALL resolved roots are checked, not just the first
match — the drain must be complete across every candidate home, not merely
the one this process happens to resolve first. This is a deliberately
different idiom from identity resolution below — home resolution stays a
verbatim copy by explicit prior instruction; identity resolution is a
shared, importable seam instead (see next paragraph) precisely because a
second divergent copy of identity resolution is the failure class this fix
is cleaning up.

Identity resolution: ``coordinator_core.session.worktree_safety.
resolve_self_session_id`` (env ``CLAUDE_CODE_SESSION_ID``; returns ``""``
when unset — the ``.current-session-id`` sentinel tier it used to fall back
to was removed 2026-08-07, unsound under this fleet's concurrency and its
sole writer deleted 2026-07-15) — the same proven seam
``history_rewrite_verdict`` and
``branch_mutation_verdict`` already use for shared-worktree self-identity.
``--session-id`` overrides it for testability, mirroring
``history_rewrite_verdict``'s ``self_session_id`` parameter.

Project-slug resolution: the target repo's absolute path with OS path
separators replaced by ``-``, mirroring Claude Code's own
``~/.claude/projects/<slug>/`` naming (verified against this repo's own
live slug, ``-Users-example-operator-X-claude-klabauter``, and DoE-claude's,
``-Users-example-operator-X-DoE-claude`` — neither contains a character this
replacement does not cover). This is deliberately the same shape as
``check_harvest_debt``'s ``--root`` resolution (explicit flag, else
``git rev-parse --show-toplevel``) — no new resolution idiom introduced.

Exit codes:
    0 — memory dir absent (nothing to drain), OR present and holding zero
        rows/bodies attributable to the closing session (drained), OR
        ``--root`` unresolvable (consistent with ``check_harvest_debt``'s
        no-op-on-unresolvable-root convention — this gate has no repo to
        check), OR the closing session's own identity is unresolvable (see
        fail-closed branch below — this reports zero residue rather than
        guessing).
    1 — at least one body file, or at least one ``MEMORY.md`` row, is
        attributable to the closing session. Every offending path is
        printed to stderr, one per line, so the ceremony's disposition
        pass has a concrete worklist rather than a bare failure.

Fail-closed identity branch: when the closing session's own id cannot be
resolved (neither ``--session-id``, ``CLAUDE_CODE_SESSION_ID``, nor the
sentinel file), this gate CANNOT honestly attribute any row or body to it
— so it reports zero residue and exits 0, printing a one-line advisory to
stderr rather than silently passing. Exiting 1 here would resurrect
exactly the destructive "delete everything you can't identify" instruction
this fix exists to remove: a gate that cannot tell whose rows these are
must not order a deletion.

Spec backlink: DoE-claude
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
  - Does NOT fail on a body or index row owned by a PEER session — only
    the closing session's own rows count as residue.
  - Does NOT fail the gate on a dangling ``MEMORY.md`` row (a link target
    missing on disk) — that is a different problem, not repaired or
    reported here.
  - Does NOT order a deletion when the closing session's own identity is
    unresolvable — reports zero residue and exits 0 with a stderr
    advisory instead of falling through to the old everything-is-residue
    behaviour.
  - Never raises: any unexpected input or filesystem error on a single
    candidate root is skipped (treated as "nothing found under this root"),
    not fatal to the whole check — matching every guard this idiom is
    copied from.
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from shutil import which
from typing import List, Optional

import yaml

from coordinator_core.session.worktree_safety import resolve_self_session_id

_CLAUDE_DIRNAME = ".claude"
_PROJECTS_DIRNAME = "projects"
_MEMORY_DIRNAME = "memory"
_INDEX_FILENAME = "MEMORY.md"

_INDEX_ROW_RE = re.compile(r"^-\s*\[[^\]]*\]\(([^)]+)\)")


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
    separators); this is that mapping's forward encode.

    Negative-spec: a separator-only encoding (no colon handling) was the
    original defect here -- it silently mismatched every real Windows
    drive-letter root (e.g. a ``D:`` drive path -> a ``D:-``-prefixed slug  # abs-path-ok: illustrative drive letter, not a resolved path
    on disk vs. the actual ``D--``-prefixed directory Claude Code
    creates), making the drain gate a permanent no-op on Windows. A test
    Windows path with no drive-letter colon (e.g. a UNC-shaped or
    colon-free fixture) does NOT exercise this path and must not be
    mistaken for Windows coverage."""
    normalized = str(root).replace("\\", "/").replace(":", "-").replace(".", "-")
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
            **no_console_creationflags(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _body_origin_session_id(path: Path) -> Optional[str]:
    """``metadata.originSessionId`` from ``path``'s YAML frontmatter, or
    ``None`` if the file has no frontmatter, no ``metadata`` block, no
    ``originSessionId`` field, or cannot be parsed at all."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        parsed = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    metadata = parsed.get("metadata")
    if not isinstance(metadata, dict):
        return None
    origin = metadata.get("originSessionId")
    if isinstance(origin, str) and origin:
        return origin
    return None


def _own_body_files(memory_dir: Path, self_sid: str) -> "List[Path]":
    """Direct-child ``*.md`` body files (excluding the index) under
    ``memory_dir`` whose frontmatter ``originSessionId`` matches
    ``self_sid``."""
    try:
        if not memory_dir.is_dir():
            return []
        candidates = sorted(
            p
            for p in memory_dir.iterdir()
            if p.is_file() and p.suffix == ".md" and p.name != _INDEX_FILENAME
        )
    except OSError:
        return []
    return [p for p in candidates if _body_origin_session_id(p) == self_sid]


def _index_has_own_row(memory_dir: Path, self_sid: str) -> bool:
    """True iff at least one ``MEMORY.md`` row resolves to a body file
    owned (per ``originSessionId``) by the closing session. A row whose
    link target is missing on disk (dangling) or that is not a parseable
    markdown link does not count, per this module's docstring."""
    index_path = memory_dir / _INDEX_FILENAME
    try:
        if not index_path.is_file():
            return False
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    for line in lines:
        match = _INDEX_ROW_RE.match(line.strip())
        if not match:
            continue
        target = match.group(1).strip()
        if not target:
            continue
        target_path = memory_dir / target
        try:
            if not target_path.is_file():
                continue
        except OSError:
            continue
        if _body_origin_session_id(target_path) == self_sid:
            return True
    return False


def main(argv: "List[str]") -> int:
    explicit_root: str | None = None
    explicit_session_id: str | None = None
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        elif argv[i] == "--session-id" and i + 1 < len(argv):
            explicit_session_id = argv[i + 1]
            i += 2
        else:
            i += 1

    root = _resolve_root(explicit_root)
    if not root:
        return 0

    self_sid = (
        explicit_session_id
        if explicit_session_id is not None
        else resolve_self_session_id(root)
    )
    if not self_sid:
        print(
            "auto-memory drain gate: SKIPPED -- could not resolve this session's own "
            "identity (no --session-id, no CLAUDE_CODE_SESSION_ID in the "
            "environment), so the drain check cannot honestly "
            "attribute any residue to it and was skipped rather than ordering a "
            "deletion it cannot justify.",
            file=sys.stderr,
        )
        return 0

    slug = _slugify_repo_root(os.path.abspath(root))

    residue: "List[Path]" = []
    for projects_root in _guarded_project_roots():
        memory_dir = projects_root / slug / _MEMORY_DIRNAME
        residue.extend(_own_body_files(memory_dir, self_sid))
        if _index_has_own_row(memory_dir, self_sid):
            residue.append(memory_dir / _INDEX_FILENAME)

    if not residue:
        return 0

    print(
        "auto-memory drain gate: BLOCKED -- the following file(s) remain under "
        "the auto-memory store at close, attributed to this closing session. "
        "Every entry must be PROMOTEd to a durable home or explicitly DROPped -- "
        "silence is not a disposition. See "
        "coordinator/docs/wiki/coordinator-tripwires.md's "
        "MEMORY-NOT-DURABLE-CAPTURE entry.",
        file=sys.stderr,
    )
    for path in residue:
        print(f"  {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
coordinator_core.ops.check_no_monolith_completion_append — static-grep tripwire
port of coordinator-claude's check-no-monolith-completion-append.sh trampoline.

Purpose: detects unauthorized writes to the legacy monolith completion-log path
`archive/completed/YYYY-MM.md` on the coordinator surface (skills/, commands/,
agents/, pipelines/, hooks/). The new per-entry completion-log format writes to
`archive/completed/YYYY-MM/<slug>.md`; any new write path targeting the
root-monolith shape is a regression to the old format and must be caught
before it ships.

Six call shapes are all caught by one literal-path grep (they all include the
`archive/completed/YYYY-MM.md` substring): markdown-body literal, shell
redirect (`>>`/`>`), here-doc body, `tee` invocation, Python/Node
`open()`/`writeFileSync`, and `git mv` source argument.

Six allowed exceptions (excluded from firing), evaluated per matched line:
  1. Lines containing `archive/completed/legacy/` — post-migration canonical home.
  2. Lines mentioning `migrate-completion-log-legacy.sh` — the migration
     helper's own `git mv` source-path use is intentional and authorized.
  3. Files whose *path* contains a `/docs/wiki/` segment — reference docs, not
     write calls.
  4. Lines containing `COORDINATOR_OVERRIDE_LEGACY_MONOLITH` — override
     documentation, not a write call.
  5. HTML/Markdown comment tripwire marker lines (`<!-- TRIPWIRE: ...` or
     `# TRIPWIRE: ...`) — self-describing prohibition comments, not writes.
  6. Files whose *path* contains a `/tests/` segment — the tripwire's own
     self-test fixtures (and any other test file exercising the legacy
     monolith shape as fixture input) carry literal monolith-path strings as
     test INPUT, not write calls. Widened from the bash-era
     `/hooks/scripts/tests/` (that dir stayed coordinator-claude-resident post-2026-07-22
     executable-surface migration and no longer holds this tripwire's own
     Python/JS self-tests, which now live at `coordinator/tests/` and
     `coordinator/bin/tests/` under claude-klabauter) to the general `/tests/` segment
     so it still matches both post-migration locations without hardcoding
     either one by name. This is deliberately a broad, path-only check —
     evaluated against the file's own path, never the matched line's content
     — and matches ANY nested `tests/` directory at any depth under the
     scanned subdirs (e.g. a vendored dependency's own `tests/` dir), not
     only the two locations named above; this breadth is an intentional
     "any nested tests/ dir is fixture territory" policy, not a claim of
     exact two-location scoping.

Not a JSON-RPC op — a plain module, NOT @register_op'd, called by direct
import from the coordinator-claude polyglot trampoline (template-variant #1, mirrors
coordinator-auto-push / handoff-gate-aging).

Port of: check-no-monolith-completion-append.sh (coordinator-claude 894d4bc6, 2026-07-22)
Spec backlink: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 10
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - File-extension filter mirrors the bash oracle's `grep --include` set
      EXACTLY: `*.md`, `*.sh`, `*.js`, `*.py` — deliberately NOT extended to
      other languages even though the coordinator surface may contain other
      file types; widening this filter is an intentional-behavior-change, not
      a bug fix.
    - Exception matching splits by exemption kind (2026-07-22 fix): the two
      path-based exemptions (`/tests/`, `/docs/wiki/`) are evaluated against
      the file's own path and therefore apply per-file — every match inside
      a `/tests/` or `/docs/wiki/` file is exempted. The remaining four
      exemptions (`archive/completed/legacy/`, the migration-helper filename,
      `COORDINATOR_OVERRIDE_LEGACY_MONOLITH`, and the tripwire comment
      marker) are content-based and evaluated per-*match-line content* only
      — a file can contain both excepted and non-excepted matches for these,
      each line judged independently, exactly as the bash `while read -r
      line` loop does. (Previously ALL six exemptions were evaluated against
      a combined `path:lineno:text` string, which let path-based exemptions
      1 and 3 be falsely satisfied by line *content* containing those
      substrings — see 2026-07-22 fix below.)
    - Directory scan is the bash oracle's original five discovery-surface
      subdirs (`skills`, `commands`, `agents`, `pipelines`, `hooks`) PLUS the
      four executable-surface subdirs the 2026-07-22 executable-surface
      migration actually populated under a claude-klabauter-side `--root`
      (`bin`, `lib`, `scripts`, `tests`) — each only included if it exists,
      mirroring the bash SEARCH_DIRS population loop. The discovery-surface
      five stayed coordinator-claude-resident (coordinator-claude) post-migration and so
      never exist under claude-klabauter's own `coordinator/`; the four
      executable-surface dirs are where a monolith-append write would
      actually land on THIS side of the split. Both sets are named so a
      caller pointing `--root` at either a coordinator-claude-resident (co-located, pre-
      split) tree or a claude-klabauter-resident (post-split) tree gets a live scan
      either way — this is additive, not a replacement of the original list.
    - Exit code 2 fires on: unknown/malformed CLI arg, `--root` given no
      value, or a resolved root that isn't a directory. Exit 2 ALSO fires
      when the root exists but none of the nine subdirs are present (mirrors
      the bash "no search directories found" branch) — this is a
      pre-existing bash-oracle quirk (an otherwise-valid root with no
      recognized subdirs is treated as an invocation error, not a clean
      pass), faithfully reproduced here rather than "fixed" to exit 0.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional, Tuple

_SEARCH_SUBDIRS = (
    "skills", "commands", "agents", "pipelines", "hooks",
    "bin", "lib", "scripts", "tests",
)
_SCAN_EXTENSIONS = (".md", ".sh", ".js", ".py")

# Matches the literal root-monolith path: archive/completed/YYYY-MM.md
_PATTERN = re.compile(r"archive/completed/[0-9]{4}-[0-9]{2}\.md")

_TRIPWIRE_COMMENT = re.compile(r"<!--\s*TRIPWIRE:|#\s*TRIPWIRE:")


def _is_excepted(path: str, text: str) -> bool:
    """Evaluate the six allowed-exception patterns.

    Review: code-reviewer (2026-07-22, Finding 1) — path-based exemptions
    (`/tests/`, `/docs/wiki/`) test against `path` only; content-based
    exemptions test against `text` (the matched line itself) only. Previously
    all six were tested against a combined `path:lineno:text` string, which
    let a line's own *content* falsely satisfy a path-based exemption (e.g. a
    doc line whose text happens to contain the literal substring `/tests/`).
    """
    if "/tests/" in path:
        return True
    if "/docs/wiki/" in path:
        return True
    if "archive/completed/legacy/" in text:
        return True
    if "migrate-completion-log-legacy.sh" in text:
        return True
    if "COORDINATOR_OVERRIDE_LEGACY_MONOLITH" in text:
        return True
    if _TRIPWIRE_COMMENT.search(text):
        return True
    return False


def _iter_matching_files(search_dir: str):
    for dirpath, _dirnames, filenames in os.walk(search_dir):
        for name in filenames:
            if name.endswith(_SCAN_EXTENSIONS):
                yield os.path.join(dirpath, name)


def scan(coordinator_root: str) -> Tuple[List[str], List[str], int]:
    """Top-level driver — mirrors the bash grep-then-filter pipeline.

    Returns (hit_lines, unreadable_files, rc):
      rc 0 — no unauthorized hits found, every candidate file was scanned.
      rc 1 — one or more unauthorized hits found (hit_lines populated,
             `file:line:content` shaped, matching grep -n output), OR one or
             more candidate files could not be read (unreadable_files
             populated) — see BEHAVIOUR CHANGE note below.
      rc 2 — invocation error (root not a directory, or no recognized
             search subdirs present under root).

    BEHAVIOUR CHANGE (2026-07-22, break-class fix): an unreadable file was
    previously silently excluded from the scan via a bare `continue`. This is
    a static tripwire meant to catch a monolith-write regression before it
    ships — a regression hiding in an unreadable file must not go unreported.
    """
    if not os.path.isdir(coordinator_root):
        return ([], [], 2)

    search_dirs = [
        os.path.join(coordinator_root, d)
        for d in _SEARCH_SUBDIRS
        if os.path.isdir(os.path.join(coordinator_root, d))
    ]
    if not search_dirs:
        return ([], [], 2)

    hits: List[str] = []
    unreadable: List[str] = []
    for search_dir in search_dirs:
        for path in sorted(_iter_matching_files(search_dir)):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError as exc:
                print(
                    f"WARNING: unreadable file excluded from monolith-completion-append scan: {path}: {exc}",
                    file=sys.stderr,
                )
                unreadable.append(path)
                continue
            for lineno, text in enumerate(lines, start=1):
                if not _PATTERN.search(text):
                    continue
                # Exceptions 1 and 2 (`/tests/`, `/docs/wiki/`) match
                # path-segment substrings built on the forward-slash
                # convention of the bash oracle's grep output. os.path.join
                # on Windows renders `path` with backslashes, which silently
                # defeats both segment checks — normalize to forward slashes
                # before evaluating _is_excepted's path-based exemptions.
                posix_path = path.replace(os.sep, "/")
                if _is_excepted(posix_path, text):
                    continue
                rendered = f"{posix_path}:{lineno}:{text.rstrip(chr(10))}"
                hits.append(rendered)

    if hits or unreadable:
        return (hits, unreadable, 1)
    return ([], [], 0)


def _default_root() -> str:
    # Mirrors the bash script's own-two-levels-up default: this module has no
    # analogous "own path" to derive from (it's a claude-klabauter-side import, not the
    # coordinator-claude trampoline file), so callers MUST pass --root explicitly. Kept as a
    # named function for parity documentation, not a real fallback.
    raise RuntimeError("check-no-monolith-completion-append: --root is required")


def main(argv: List[str]) -> int:
    """CLI entry: arg validation, scan, print, return rc."""
    root: Optional[str] = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            if i + 1 >= len(argv):
                print("Error: --root requires a path argument", file=sys.stderr)
                return 2
            root = argv[i + 1]
            i += 2
            continue
        if arg in ("--help", "-h"):
            print(__doc__)
            return 0
        print(f"Unknown option: {arg}", file=sys.stderr)
        return 2

    if root is None:
        print(
            "Error: --root is required (no script-relative default in the "
            "claude-klabauter-side port)",
            file=sys.stderr,
        )
        return 2

    if not os.path.isdir(root):
        print(f"Error: coordinator root not found at: {root}", file=sys.stderr)
        return 2

    hits, unreadable, rc = scan(root)

    if rc == 2:
        print(f"Error: no search directories found under: {root}", file=sys.stderr)
        return 2

    if rc == 0:
        print(
            "check-no-monolith-completion-append: CLEAN — no unauthorized "
            "monolith writes found."
        )
        return 0

    if unreadable:
        print(
            "check-no-monolith-completion-append: SCAN INCOMPLETE — "
            f"{len(unreadable)} file(s) could not be read (fail-closed):",
            file=sys.stderr,
        )
        for path in unreadable:
            print(f"  {path}", file=sys.stderr)
        if not hits:
            return 1
        print("", file=sys.stderr)

    if not hits:
        return 1

    print(
        "check-no-monolith-completion-append: UNAUTHORIZED MONOLITH WRITE DETECTED",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "The following file:line references contain a write path targeting",
        file=sys.stderr,
    )
    print(
        "archive/completed/YYYY-MM.md outside the allowed exceptions.",
        file=sys.stderr,
    )
    print(
        "New completion-log writes must use archive/completed/YYYY-MM/<slug>.md.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for hit in hits:
        print(f"  {hit}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Allowed exceptions: archive/completed/legacy/, migrate-completion-log-legacy.sh,",
        file=sys.stderr,
    )
    print(
        "docs/wiki/ reference docs, COORDINATOR_OVERRIDE_LEGACY_MONOLITH lines,",
        file=sys.stderr,
    )
    print(
        "<!-- TRIPWIRE: comment markers, and hooks/scripts/tests/ self-test fixtures.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

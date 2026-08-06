"""
coordinator_core.ops.verify_no_console_flash — guard against console-window
flashes on Windows.

Purpose: on Windows (git-bash/mintty), spawning any native console-subsystem
.exe (python.exe, node.exe, powershell.exe) allocates a fresh conhost.exe
window that briefly flashes. The only reliable suppression is
CREATE_NO_WINDOW / windowsHide:true at the CreateProcess call — not
`-WindowStyle Hidden`, which is create-then-hide. See:
docs/wiki/claude-code-platform-gotchas.md § Windows console window flash
(example-doctrine-repo).

This guard statically scans a coordinator-claude tree (`*.sh`, `*.json`,
`coordinator-auto-push`) for spawn shapes that lack an explicit suppression
flag or routing through `lib/spawn-hidden.sh`, and reports each unsuppressed
site. It performs NO subprocess spawns of its own — detection is pure
text/regex scanning (os.walk + re), never a `grep`/`sed` shell-out, so this
module itself cannot flash a console window on the very platform it guards.

Port of: verify-no-console-flash.sh (example-doctrine-repo 894d4bc6, 2026-07-22)
Spec backlink: docs/plans/2026-05-29-windows-console-flash-elimination.md § Chunk 3
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec (fixed 2026-07-21 — was a live Windows regression, NOT a faithful
oracle-parity repro; see below):
    - `_in_inline_comment`'s prefix-strip regex was originally a direct,
      non-drive-letter-safe port of the bash oracle (`^[^:]*:[0-9][0-9]*:`),
      whose `[^:]*` assumes the path portion contains zero colons. On a
      Windows path like `C:/path/file.sh:42:content` that assumption is
      false — the drive-letter colon breaks the match at the `^` anchor,
      so the strip silently no-ops and leaves the full `path:lineno:`
      prefix glued onto `content`. That widened text can shift a real
      spawn token (e.g. `pwsh`) so it no longer sits at a word/line
      boundary the spawn-shape regexes require (a leading whitespace or
      slash before "pwsh", followed by trailing whitespace),
      causing a genuine unsuppressed spawn to be silently reclassified as
      "comment-shaped" and dropped — this DID mask real violations on this
      repo's actual Windows scan target (`$HOME/.claude/plugins/
      coordinator-claude`, itself always a drive-letter path on Windows),
      so the original "not a functional regression" claim here was wrong.
      Fixed by switching to a non-greedy, colon-permissive prefix
      (`^.*?:[0-9][0-9]*:`) — mirrors the technique `_is_suppressed`'s
      file-allow path extraction already used correctly
      (`:[0-9]+:.*$`, matches the FIRST lineno-colon regardless of colons
      earlier in the path).
    - hooks.json `command`-field spawns are ARCHITECTURALLY EXEMPT (not
      scanned at all) — Claude Code itself is the CreateProcess parent for
      hook spawns; shell-level suppression cannot influence it. Preserved
      verbatim from the oracle (the *.json include only applies to the
      powershell/pwsh scan, never the python/node scan — an intentional
      asymmetry in the oracle, not a port gap).

Deliberate (non-regression) deviation from the bash oracle:
    - Violation-report LINE ORDER is `os.walk` traversal order (directory
      names sorted, then filenames sorted) rather than the bash oracle's
      `grep -r` directory-entry (readdir) order. The oracle's order was
      never a documented contract — it's an accident of filesystem
      traversal, itself non-portable (ext4/APFS/NTFS readdir order all
      differ) — and no caller (`coordinator/commands/workweek-complete.md`
      Step "Console-flash guard", the bats/sh test suite) parses or asserts
      on report ORDER, only on exit code and per-shape content presence.
      Verified: same violation SET, same COUNT, same per-line CONTENT byte-
      for-byte against the oracle on both seeded fixtures and the live
      coordinator-claude tree (161/161 lines, zero diff after sort).
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from typing import Iterator, List, Sequence, Tuple

# ---------------------------------------------------------------------------
# File discovery — mirrors `grep -r --include=... $COORD_ROOT`
# ---------------------------------------------------------------------------

_PS_INCLUDE_EXACT = {"coordinator-auto-push"}
_PS_INCLUDE_GLOBS = ("*.sh", "*.json")

_PY_NODE_INCLUDE_EXACT = {"coordinator-auto-push"}
_PY_NODE_INCLUDE_GLOBS = ("*.sh",)


def _iter_candidate_files(coord_root: str, globs: Sequence[str], exact: set) -> Iterator[str]:
    if not os.path.isdir(coord_root):
        return
    for dirpath, dirnames, filenames in os.walk(coord_root):
        dirnames.sort()
        for fname in sorted(filenames):
            if fname in exact or any(fnmatch.fnmatch(fname, g) for g in globs):
                yield os.path.join(dirpath, fname)


def _grep_file(path: str, match_res: Sequence["re.Pattern[str]"]) -> List[str]:
    """Return 'path:lineno:content' for every line matching ANY pattern.

    Mirrors `grep -rEn -e P1 -e P2 ...` (OR of alternatives, unanchored
    substring search, one output line per matching source line).

    Raises OSError (e.g. PermissionError) on a read failure — a caller MUST
    NOT treat that as "0 matches"; an unreadable file was never scanned, so
    reporting it as clean would be a silent false-negative on the gate. See
    `_scan` for how the caller folds this into the overall gate verdict.
    """
    hits: List[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.rstrip("\n").rstrip("\r")
            if any(p.search(line) for p in match_res):
                hits.append(f"{path}:{lineno}:{line}")
    return hits


def _scan(coord_root: str, globs: Sequence[str], exact: set,
          match_res: Sequence["re.Pattern[str]"]) -> Tuple[List[str], List[str]]:
    """Returns (hits, unreadable_paths). A file that raised on read is NOT
    silently dropped — its path is collected so the caller can fail the gate
    rather than report a false-clean result for content it never scanned."""
    hits: List[str] = []
    unreadable: List[str] = []
    for path in _iter_candidate_files(coord_root, globs, exact):
        try:
            hits.extend(_grep_file(path, match_res))
        except OSError as exc:
            print(
                f"_grep_file: cannot read {path}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            unreadable.append(path)
    return hits, unreadable


def _filter_out(hits: List[str], filter_res: Sequence["re.Pattern[str]"]) -> List[str]:
    """Mirrors a chain of `grep -v PATTERN` — drop any line matching any filter."""
    return [h for h in hits if not any(p.search(h) for p in filter_res)]


# ---------------------------------------------------------------------------
# (6) powershell / pwsh without -WindowStyle Hidden  (legacy shape)
# ---------------------------------------------------------------------------

_PS_MATCH = [
    re.compile(r"powershell\.exe\s"),
    re.compile(r"(^|\s|/)pwsh\s"),
]

_PS_FILTERS = [
    re.compile(r"verify-no-console-flash|verify-no-powershell-flash"),
    re.compile(r":\s*[0-9]+:\s*#"),
    re.compile(r"command\s+-v\s+(pwsh|powershell\.exe|powershell)\b"),
    re.compile(r'(^|[\s;|&(])echo\s+"[^"]*powershell\.exe[^"]*"'),
]


def _ps_hits(coord_root: str) -> Tuple[List[str], List[str]]:
    hits, unreadable = _scan(coord_root, _PS_INCLUDE_GLOBS, _PS_INCLUDE_EXACT, _PS_MATCH)
    return _filter_out(hits, _PS_FILTERS), unreadable


# ---------------------------------------------------------------------------
# (1-4) python / node spawn shapes in shell scripts and helper bins
# ---------------------------------------------------------------------------

_PY_NODE_MATCH = [
    re.compile(r"\$\{PYTHON[^}]*\}"),
    re.compile(r"\$PYTHON[A-Z_0-9]*"),
    re.compile(r"\$\{NODE[^}]*\}"),
    re.compile(r"\$NODE[A-Z_0-9]*"),
    re.compile(r"(^|\s)python3?\s"),
    re.compile(r"(^|\s)node\s"),
    re.compile(r"python3?\s+-[ce]\s"),
    re.compile(r"node\s+-e\s"),
]

_PY_NODE_FILTERS = [
    re.compile(r"verify-no-console-flash|verify-no-powershell-flash"),
    re.compile(r"command\s+-v\s+(python|node)"),
    re.compile(r'command\s+-v\s+"?\$\{?(PYTHON|NODE)'),
    re.compile(r":\s*[0-9]+:\s*#"),
    re.compile(r'\[\[\s+-[zn]\s+"?\$\{?(PYTHON|NODE)'),
    re.compile(r'(^|[\s;|&(])\[\s+-[zn]\s+"?\$\{?(PYTHON|NODE)'),
    re.compile(r"\bfor\s+[A-Za-z_][A-Za-z_0-9]*\s+in\s+[^|;&]*\b(python3?|node)\b"),
    re.compile(r"Get-Command\s+(python3?|node|py)\b"),
    re.compile(r'(^|[\s;|&(])echo\s+"[^"]*\b(python3?|node)\b[^"]*"'),
    re.compile(r'(^|[\s;|&(])echo\s+"[^"]*(Python|Node\.js|Nodejs)\b[^"]*"'),
    re.compile(
        r"\$\{?(NODE|PYTHON)_(VERSION|MIN_VERSION|MAJOR|MINOR|HOME|MODULES|JS|EXEC|TAG"
        r"|REQUIREMENT|TARGET|MIN|SHIM|SHIM_WIN|CMD|REL|DIR|TYPE)\b"
    ),
    re.compile(r'\[\[?\s+"?\$\{?[a-zA-Z_][a-zA-Z_0-9]*\}?"?\s+-(lt|gt|le|ge|eq|ne)\b'),
    re.compile(r"\$\{PYTHON_ARGS\["),
    re.compile(r"\$\{NODE_ARGS\["),
    re.compile(r"^[^:]*:[0-9]+:\s*(local\s+|export\s+)?(NODE|PYTHON)[A-Z_0-9]*="),
    re.compile(r'^[^:]*:[0-9]+:\s*(local\s+|export\s+)?[a-z][a-zA-Z_0-9]*="\$\('),
    re.compile(r'(^|[\s;|&(])(fail|warn|info|die|log|error|print|printf)\s+"[^"]*\b(python3?|node|Python|Node)\b'),
    re.compile(r'^[^:]*:[0-9]+:\s*[A-Z_][A-Z_0-9]*="[^"]*\b(python3?|node|Python|Node)\b'),
    re.compile(r'^[^:]*:[0-9]+:\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*"'),
    re.compile(r"\bread\s+-ra?\s"),
]


def _py_node_hits(coord_root: str) -> Tuple[List[str], List[str]]:
    hits, unreadable = _scan(
        coord_root, _PY_NODE_INCLUDE_GLOBS, _PY_NODE_INCLUDE_EXACT, _PY_NODE_MATCH
    )
    return _filter_out(hits, _PY_NODE_FILTERS), unreadable


# ---------------------------------------------------------------------------
# Helper: does a matched line carry an explicit suppression or allowlist?
# ---------------------------------------------------------------------------

_ALLOW_RE = re.compile(r"verify-no-console-flash:\s*allow")
_FILE_ALLOW_RE = re.compile(r"verify-no-console-flash:\s*file-allow")
_FALINE_PATH_STRIP_RE = re.compile(r":[0-9]+:.*$")
_SPAWN_HIDDEN_RE = re.compile(r"spawn[-_]hidden")
_SUPPRESSION_FLAG_RE = re.compile(r"CREATE_NO_WINDOW|windowsHide|WindowStyle\s+Hidden")


def _is_suppressed(line: str) -> bool:
    """Mirrors bash `_is_suppressed`: allowlist marker, file-level allow,
    spawn-hidden routing, or an explicit suppression flag on the line."""
    if _ALLOW_RE.search(line):
        return True

    # File-level allow: a `verify-no-console-flash: file-allow` marker in the
    # file's first 10 lines suppresses the whole file. Extract the path
    # drive-letter-safely — strip the `:lineno:content` suffix from the END,
    # NOT split on the first ':' (which would eat a Windows `C:`).
    faline_path = _FALINE_PATH_STRIP_RE.sub("", line, count=1)
    if os.path.isfile(faline_path):
        try:
            with open(faline_path, "r", encoding="utf-8", errors="replace") as fh:
                head_lines = fh.readlines()[:10]
        except OSError:
            head_lines = []
        if any(_FILE_ALLOW_RE.search(hl) for hl in head_lines):
            return True

    if _SPAWN_HIDDEN_RE.search(line):
        return True

    if _SUPPRESSION_FLAG_RE.search(line):
        return True

    return False


# ---------------------------------------------------------------------------
# Inline-comment context: drop hits where the matched interpreter token only
# appears after a `#` on the same line (it's prose, not a spawn).
# ---------------------------------------------------------------------------

_INLINE_STRIP_RE = re.compile(r"^.*?:[0-9][0-9]*:")

_INLINE_SPAWN_RES = [
    re.compile(r"\$\{(PYTHON|NODE)[^}]*\}"),
    re.compile(r"\$(PYTHON|NODE)[A-Z_0-9]*"),
    re.compile(r"(^|\s)python3?\s"),
    re.compile(r"(^|\s)node\s"),
    re.compile(r"python3?\s+-[ce]\s"),
    re.compile(r"node\s+-e\s"),
    re.compile(r"powershell\.exe\s"),
    re.compile(r"(^|[\s/])pwsh\s"),
]


def _in_inline_comment(line: str) -> bool:
    """Mirrors bash `_in_inline_comment`: True if the matched token only
    occurs after a `#` (prose), False if a real spawn shape precedes it."""
    # Strip the file:lineno: prefix so we look at source content only. This
    # regex is NOT drive-letter safe (see module negative-spec) — faithful
    # port of the oracle's own asymmetry.
    content = _INLINE_STRIP_RE.sub("", line, count=1)
    code_part = content.split("#", 1)[0]
    if any(p.search(code_part) for p in _INLINE_SPAWN_RES):
        return False
    return True


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    """CLI entry.

    Usage: verify_no_console_flash <ROOT>
      ROOT defaults to $HOME/.claude/plugins.

    Scans ROOT/coordinator-claude for unsuppressed python/node/powershell
    console-window-flash spawn shapes (see module docstring).

    Exit codes:
      0 — clean: no unsuppressed spawn sites found AND every candidate file
          was successfully read.
      1 — violations found, and/or one or more candidate files could not be
          read (report printed; an unread file is never treated as clean).
    This op performs no claude-klabauter-transport call and has no distinct
    transport-failure code of its own; a caller-side import/resolution
    failure is the TRAMPOLINE's own concern, with its own dedicated exit
    code for that case.
    """
    root = argv[0] if argv else os.path.join(os.path.expanduser("~"), ".claude", "plugins")
    coord_root = os.path.join(root, "coordinator-claude")

    fail = 0
    unreadable_all: List[str] = []

    ps_lines, ps_unreadable = _ps_hits(coord_root)
    unreadable_all.extend(ps_unreadable)
    for line in ps_lines:
        if _in_inline_comment(line):
            continue
        if not _is_suppressed(line):
            print(f"MISSING -WindowStyle Hidden (powershell/pwsh): {line}")
            fail = 1

    py_node_lines, py_node_unreadable = _py_node_hits(coord_root)
    unreadable_all.extend(py_node_unreadable)
    for line in py_node_lines:
        if _in_inline_comment(line):
            continue
        if not _is_suppressed(line):
            print(f"UNSUPPRESSED python/node spawn: {line}")
            fail = 1

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Previously: _grep_file swallowed OSError (e.g. PermissionError) on a
    # candidate file and returned [] — indistinguishable from "read fine,
    # zero matches". A file that could not be opened was silently treated as
    # scanned-and-clean, when in truth it was never scanned at all. Fail
    # closed: any unreadable candidate file fails the overall gate, and each
    # one is named explicitly so the human knows what was never scanned.
    if unreadable_all:
        for path in unreadable_all:
            print(f"UNREADABLE (not scanned -- gate cannot certify clean): {path}", file=sys.stderr)
        fail = 1
    # --- end Tier 2 ---

    if fail == 0:
        print("OK: all console-spawning invocations are suppressed or explicitly allowlisted")

    return fail


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

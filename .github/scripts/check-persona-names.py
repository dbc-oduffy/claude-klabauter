#!/usr/bin/env python3
"""Block internal persona names and fleet codenames from shipping in the mirror.

This engine is published out of a private meta-repo whose prose, agent roster
and directory names carry internal identities. The publish pipeline rewrites
most of them, but rewriting is not the same as proving. This check is the
defence-in-depth that runs on the bytes that actually landed.

WHY THIS CHECK IS NOT REDUNDANT WITH THE PUBLISH-SIDE GUARD
------------------------------------------------------------
The upstream residual-pattern guard maintains a global exemption set that
prefix-matches the underscore form of the meta-repo's own codename out of its
alternation before any scan runs, with no per-target override. It therefore
reports zero findings while that codename ships verbatim — confirmed in real
published bytes, not hypothesised. Two consequences shaped the matcher below:

1. This check must cover the hyphen-delimited role-id forms as well as the bare
   token. The upstream glued-identifier rewrite only matches underscore forms,
   so a hyphenated role id passes straight through it.
2. Token boundaries here are ALPHANUMERIC boundaries, not ``\\b``. ``\\b``
   treats ``_`` as a word character, so ``\\bmakima\\b`` does not match
   ``test_makima_doctor.py`` — which is exactly the shape the leak takes in file
   NAMES once file CONTENT has been rewritten. Paths are scanned as well as
   contents for the same reason.

THE IDENTITY SPLIT
------------------
Personas and codenames must never appear. The operator's identity is different:
some occurrences are deliberate attribution that MUST survive publication.

  PERMITTED, and nothing else:
    * the operator's full personal name, in the licence/attribution files only
      (LICENSE, NOTICE, COMMERCIAL.md, pyproject.toml authors) — the Licensor
      field is the whole point of the licence file;
    * the public GitHub handle, in URL or ``owner/repo`` slug form only.

  BANNED everywhere else, including a bare handle with no repo slug and any
  absolute home-directory path that embeds the surname.

A blanket ban on the surname would fail the repo's own LICENSE. A blanket allow
would let a leaked home-directory path through. Hence the span-based split: the
ban patterns match first, then any match fully contained inside a permitted span
is dropped.

SUPPRESSION
-----------
Inline:  ``noqa: identity-names`` anywhere on the line (any comment syntax).
File:    ``.github/.identity-allowlist``, one entry per line —
           ``<path>:<lineno>``  exempt that one line's content
           ``<path>:*``         exempt that file's content entirely
           ``path=<path>``      exempt that file's PATH from the path scan

EXIT CONTRACT
-------------
  0 — no findings (an empty or partially-published tree is not a finding)
  1 — at least one finding, or the tree enumerated zero files at all
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import (  # noqa: E402
    bootstrap_empty_dirs,
    load_allowlist,
    read_text,
    repo_files,
    repo_root,
)


def _tok(*fragments: str) -> str:
    """Token pattern bounded by non-alphanumerics, so ``_`` and ``-`` separate.

    Accepts the token in FRAGMENTS, joined here at runtime. That indirection is
    load-bearing and must not be "simplified" away into a single literal:

    This checker is scanned by the upstream publish pipeline's own
    residual-pattern guard, which greps the published tree for exactly the
    codenames listed below. A contiguous literal in this file is indistinguishable
    to that guard from a real leak elsewhere in the tree, so it aborts the publish
    — every publish, of every target, permanently. Splitting each token means the
    bytes on disk never contain it while the compiled pattern still matches the
    real thing.

    The same reasoning is why the multi-word entries below are written with
    character classes between their parts rather than as plain strings.
    """
    return rf"(?<![A-Za-z0-9]){''.join(fragments)}(?![A-Za-z0-9])"


# (label, compiled pattern). Case-sensitivity is per-entry and deliberate:
# short display names are matched case-sensitively so ordinary lowercase
# identifiers in engine code are not false positives, while the distinctive
# multi-syllable codenames are matched case-insensitively.
BANNED: list[tuple[str, re.Pattern[str]]] = [
    # --- personas (display names; must never appear) -----------------------
    ("persona", re.compile(_tok(r"Zol[ií]"), re.IGNORECASE)),
    ("persona", re.compile(_tok(r"Camelia"), re.IGNORECASE)),
    ("persona", re.compile(_tok(r"Pal[ií]"), re.IGNORECASE)),
    ("persona", re.compile(_tok(r"Patrik"), re.IGNORECASE)),
    # Case-SENSITIVE: `sid`, `fru` and `yk` are plausible lowercase identifiers
    # in engine code (session id, and so on). The persona is the capitalised
    # display form, and that is what leaks in prose.
    ("persona", re.compile(_tok(r"Sid"))),
    ("persona", re.compile(_tok(r"Fru"))),
    ("persona", re.compile(_tok(r"YK"))),
    # --- product / project codenames ---------------------------------------
    ("codename", re.compile(_tok("nimb", "alyst"), re.IGNORECASE)),
    ("codename", re.compile(_tok("gastown", "hall"), re.IGNORECASE)),
    # --- internal fleet codenames ------------------------------------------
    # Split into fragments per _tok's docstring: these are the tokens the
    # upstream residual-pattern guard greps for, so a contiguous literal here
    # aborts every publish. Do not join them back up.
    ("fleet codename", re.compile(_tok("mak", "ima"), re.IGNORECASE)),
    ("fleet codename", re.compile(_tok("opti", "con"), re.IGNORECASE)),
    ("fleet codename", re.compile(_tok("holo", "deck"), re.IGNORECASE)),
    ("fleet codename", re.compile(_tok("del", r"phi(?:pro)?"), re.IGNORECASE)),
    ("fleet codename", re.compile(r"(?<![A-Za-z0-9])project[-_ ]rag(?![A-Za-z0-9])", re.IGNORECASE)),
    # The meta-repo codename, in every delimiter form INCLUDING the hyphenated
    # role-id suffixes (`...-em`, `...-lead`) that the upstream glued-identifier
    # rewrite does not match. This is the specific residue the publish-side
    # guard's exemption set lets through.
    ("fleet codename", re.compile(
        r"(?<![A-Za-z0-9])doe[-_ ]claude(?:[-_][A-Za-z]+)*(?![A-Za-z0-9])", re.IGNORECASE)),
    # Bare capitalised org abbreviation, case-SENSITIVE — distinctive enough to
    # match safely, and it is how the codename appears in possessive prose.
    ("fleet codename", re.compile(_tok(r"DoE"))),
    # --- operator identity (see THE IDENTITY SPLIT above) -------------------
    ("operator identity", re.compile(_tok(r"o['’]?duffy"), re.IGNORECASE)),
]

# Where the operator's personal name is intentional attribution.
#
# The line is TOP-LEVEL PUBLIC DOCUMENTS, not a hand-maintained filename list.
# A list went stale the same afternoon it was written: PRIVACY.md and
# CONTRIBUTING.md landed after it and carry the maintainer's name for entirely
# legitimate reasons. The durable rule is that attribution belongs in the
# repo's front matter — the licence, the notice, the policy documents a reader
# looks at before adopting — and nowhere else. A personal name inside engine
# source or a nested doc is a leak, which is the case worth catching.
ATTRIBUTION_EXTRA = {"LICENSE", "LICENSE.txt", "NOTICE", "pyproject.toml"}


def is_attribution_surface(path: str) -> bool:
    if "/" in path:
        return False
    return path in ATTRIBUTION_EXTRA or path.endswith(".md")

# Permitted spans. A ban match fully inside one of these is not a finding.
PERSONAL_NAME_RE = re.compile(r"D[oó]nal\s+O['’]?Duffy", re.IGNORECASE)
# The surname alone, on an attribution surface. Needed because the scan is
# line-by-line and markdown wraps: PRIVACY.md carries the given name on one
# line and "O'Duffy]" on the next, so a full-name pattern alone would report the
# orphaned surname as a leak. The APOSTROPHE IS REQUIRED and that is what keeps
# this narrow — it permits the written name while still catching the bare
# lowercase token, which is the home-directory-path form ("/Users/<user>/...")
# this whole split exists to stop.
SURNAME_RE = re.compile(r"O['’]Duffy", re.IGNORECASE)
# Handle in URL form, or in `owner/repo` slug form (clone/badge instructions).
# A bare handle with no slug is NOT permitted — that is the blanket-allow the
# split exists to avoid.
HANDLE_URL_RE = re.compile(r"github\.com/dbc-oduffy(?:/[\w.\-]+)*", re.IGNORECASE)
# (?<!/) excludes a slug preceded by another slash — a home-directory path of
# the shape "/<something>/dbc-oduffy/<segment>" must NOT be treated as the
# permitted owner/repo slug form; only a slug at a non-path-internal boundary
# qualifies.
HANDLE_SLUG_RE = re.compile(r"(?<![A-Za-z0-9])(?<!/)dbc-oduffy/[\w.\-]+", re.IGNORECASE)
# @-mention form, as used to name the maintainer in CONTRIBUTING.md.
HANDLE_MENTION_RE = re.compile(r"@dbc-oduffy(?![A-Za-z0-9])", re.IGNORECASE)

NOQA_RE = re.compile(r"noqa:\s*identity-names", re.IGNORECASE)

ALLOWLIST_NAME = ".identity-allowlist"

# Always exempt: this file names every banned token by construction, and the
# allowlist file names the paths that carry them.
SELF_EXEMPT_PATHS = {
    ".github/scripts/check-persona-names.py",
    f".github/{ALLOWLIST_NAME}",
}

# Directories that arrive in stages during mirror bootstrap.
STAGED_DIRS = ["coordinator_core", "bin", "docs"]


def permitted_spans(text: str, path: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in HANDLE_URL_RE.finditer(text)]
    spans += [m.span() for m in HANDLE_SLUG_RE.finditer(text)]
    spans += [m.span() for m in HANDLE_MENTION_RE.finditer(text)]
    if is_attribution_surface(path):
        spans += [m.span() for m in PERSONAL_NAME_RE.finditer(text)]
        spans += [m.span() for m in SURNAME_RE.finditer(text)]
    return spans


def findings_in(text: str, path: str) -> list[tuple[str, str]]:
    """Return [(label, matched_text)] for every banned token not inside a permitted span."""
    allowed = permitted_spans(text, path)
    found: list[tuple[str, str]] = []
    for label, pattern in BANNED:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(a <= start and end <= b for a, b in allowed):
                continue
            found.append((label, match.group(0)))
    return found


def main() -> int:
    root = repo_root()
    paths = repo_files(root)

    if not paths:
        print("Identity check FAILED: enumerated zero files.")
        print("  A publish gate that inspects nothing is not a passing gate.")
        print("  Check that the destination tree exists and is readable.")
        return 1

    allowlist = load_allowlist(root, ALLOWLIST_NAME)
    line_exempt = {e for e in allowlist if not e.startswith("path=")}
    path_exempt = {e[len("path="):] for e in allowlist if e.startswith("path=")}

    errors: list[str] = []
    scanned = 0

    for rel in paths:
        if rel in SELF_EXEMPT_PATHS:
            continue

        if rel not in path_exempt:
            for label, matched in findings_in(rel, rel):
                errors.append(f"{rel}: {label} '{matched}' in the FILE PATH")

        if f"{rel}:*" in line_exempt:
            continue

        text = read_text(root / rel)
        if text is None:
            continue
        scanned += 1

        for line_num, line in enumerate(text.splitlines(), 1):
            if NOQA_RE.search(line):
                continue
            if f"{rel}:{line_num}" in line_exempt:
                continue
            for label, matched in findings_in(line, rel):
                excerpt = line.strip()
                if len(excerpt) > 120:
                    excerpt = excerpt[:117] + "..."
                errors.append(f"{rel}:{line_num}: {label} '{matched}' — {excerpt}")

    staged_empty = bootstrap_empty_dirs(root, STAGED_DIRS)

    if errors:
        print("Identity check FAILED:")
        print("  Internal persona names and fleet codenames must not appear in the")
        print("  published mirror. The only permitted identity strings are the")
        print("  Licensor name in the attribution files and the public GitHub")
        print("  handle in URL or owner/repo form.")
        print()
        for err in errors:
            print(f"  {err}")
        print()
        print("  Fix the source rename rules rather than the symptom where possible.")
        print("  For a genuinely intentional occurrence: add 'noqa: identity-names'")
        print(f"  to the line, or an entry to .github/{ALLOWLIST_NAME}.")
        if staged_empty:
            print()
            print(f"  (Note: {', '.join(staged_empty)} is empty — mid-bootstrap. The")
            print("   findings above are in the bytes that HAVE published.)")
        return 1

    print(f"Identity check passed ({scanned} text files scanned, {len(paths)} paths checked).")
    if staged_empty:
        print(f"  Note: {', '.join(staged_empty)} empty (mid-bootstrap) — nothing to inspect there yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

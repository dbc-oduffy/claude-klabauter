r"""
coordinator_core.ops.name_personas — persona-name binding for a fresh publish-repo install.

Purpose: engine-tree twin of `coordinator/dist/publish-repo-setup/name-personas.sh`
(publish-repo `setup/name-personas.sh`). Binds a user-chosen name to each role-distinct
reviewer persona ("the Staff Engineer" -> "Alex") by rewriting the articulated
role-label sentinel in installed-copy prose (`*.md` / `*.sh` under `plugins/`, plus
`docs/customization.md`), skipping YAML frontmatter.

NOT imported by the DoE-side trampoline. The trampoline lives at
`coordinator/dist/publish-repo-setup/name-personas.sh`, which percolates verbatim
(`sync_flat_mirror`) into the standalone OSS `coordinator-claude` publish repo as
`setup/name-personas.sh` -- a bootstrap-time script that runs BEFORE (and with no
guarantee of) a `claude-klabauter` sibling clone, `coordinator_core`, or a
`.claude-klabauter-live-root` machine-local pointer. A `from coordinator_core.ops.name_personas
import main` trampoline would resolve the engine root on a Claude-Central/DoE
developer machine but raise/no-op for every OSS installer -- silently defeating the
"customize persona names" install step for every real end user. This module exists
for engine-tree pytest coverage / potential future reuse from Claude-Central tooling
only; the trampoline reimplements the same logic natively (stdlib-only) and the two
must be kept in sync by hand if either changes. Mirrors the deliberate-self-contained
precedent already established for `dev-sync.sh` / `coordinator_core.ops.dev_sync`
in the same `dist/publish-repo-setup/` family.

Port source: coordinator/dist/publish-repo-setup/name-personas.sh (DoE-claude, 291 lines)
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292

Exit codes (parity-critical, preserved from the bash oracle):
    0 -- dry-run report printed, or live substitutions applied and reported.
    1 -- usage/validation error: odd ROLE/NAME arg count, zero pairs provided,
         or an unrecognized role label.

Negative-spec (retired/never-had patterns -- do NOT reintroduce):
    - Does NOT require `perl` or `bash >= 4.0` -- the bash oracle's preflight
      guards existed only to protect its own `perl`-shelling / associative-array
      implementation; this module reimplements the same logic natively in
      Python stdlib (`re`, `unicodedata`) with zero external-process or
      bash-version dependency, so those guards have no equivalent here.
    - Does NOT import coordinator_core.ipc / cc_invoke -- see rationale above.
    - Frontmatter reconstruction always emits a bare `\n` for the `---`
      delimiter lines regardless of the source file's original line-ending
      convention (LF vs CRLF) -- this is a faithful reproduction of the bash
      oracle's own `perl` reassembly (`$_ = "---\n$fm---\n$body"`), not a new
      bug introduced by this port.
    - Live-run substitution creates a `<file>.bak` sibling containing the
      pre-substitution content for every file it modifies -- a faithful
      reproduction of the oracle's `perl -i.bak` in-place-edit convention.
      This is a known, carried-forward install-time artifact, not a defect
      introduced by this port; installers who don't want the `.bak` litter
      should clean it up themselves (matches the oracle's existing behavior).
    - Replacement-string handling: the oracle's `perl -0777 -i.bak -pe` uses
      `s/\Q$old\E/$new/g`, where `$new` is spliced into the *replacement* side
      of a Perl substitution without `\Q...\E` -- a persona NAME containing a
      literal backslash or `$N` sequence could trigger Perl's replacement-side
      interpolation and mis-substitute. This module instead performs a plain
      literal `str.replace(old, new)`, which never mis-interprets any
      character in `new`. For any normal human name (the entire intended use
      case) this is behavior-identical to the oracle; for the pathological
      backslash/`$digit` NAME edge case it is a strict fix of accidental Perl
      fragility, not a semantic port decision, so it is not preserved.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import List, Optional, Tuple

PROG = "name-personas.sh"

# Rewrites plugins/*.md,*.sh and docs/customization.md under the resolved
# repo root; neither exists as a tracked path in claude-klabauter's own tree (no
# tracked plugins/ dir, no tracked docs/customization.md), so a default
# in-repo run writes nothing here. Real production use is via
# NAME_PERSONAS_REPO_ROOT pointed at an external publish-repo checkout.
GENERATES = []

# Articulated role label    | Subagent slug                  | Plugin
# the Staff Engineer          | coordinator:staff-eng          | coordinator
# the Director of Engineering | coordinator:eng-director       | coordinator
# the VP-Product Reviewer     | coordinator:vp-product         | coordinator
# the Game Dev Reviewer       | game-dev:staff-game-dev        | game-dev
# the Front-End Reviewer      | coordinator:senior-front-end   | coordinator
# the UX Reviewer             | coordinator:staff-ux           | coordinator
# the Data Science Reviewer   | coordinator:staff-data-sci     | coordinator
KNOWN_ROLES: List[str] = [
    "the Staff Engineer",
    "the Director of Engineering",
    "the VP-Product Reviewer",
    "the Game Dev Reviewer",
    "the Front-End Reviewer",
    "the UX Reviewer",
    "the Data Science Reviewer",
]

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n(.*)", re.DOTALL)


def to_slug(name: str) -> str:
    """Lowercase + strip combining diacritics (NFD-decompose, drop category-M, NFC-recompose).

    Mirrors the bash oracle's `perl -CS -MUnicode::Normalize -ne
    'print lc(NFC(NFD($_) =~ s/\\p{M}//gr))'` exactly: `\\p{M}` is the Unicode
    Mark general-category (Mn+Mc+Me), matched here via `unicodedata.category(c)
    .startswith("M")`, not the narrower `unicodedata.combining()` canonical-
    combining-class check (which under-covers spacing marks, category Mc).
    """
    decomposed = unicodedata.normalize("NFD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.category(c).startswith("M"))
    return unicodedata.normalize("NFC", stripped).lower()


def _split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    """Return (frontmatter_incl_trailing_nl_or_None, body). Frontmatter-safe split."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    return None, text


def count_prose_matches(old: str, text: str) -> int:
    """Count non-overlapping literal occurrences of `old` in prose (frontmatter skipped)."""
    _, body = _split_frontmatter(text)
    return body.count(old)


def replace_in_prose(old: str, new: str, text: str) -> str:
    """Literal-replace all occurrences of `old` with `new` in prose (frontmatter untouched)."""
    fm, body = _split_frontmatter(text)
    new_body = body.replace(old, new)
    if fm is not None:
        return f"---\n{fm}---\n{new_body}"
    return new_body


def _resolve_repo_root() -> Path:
    override = os.environ.get("NAME_PERSONAS_REPO_ROOT")
    if override:
        return Path(override)
    script_path = Path(os.path.abspath(__file__))
    return script_path.parent.parent


def _collect_files(repo_root: Path) -> List[Path]:
    plugins_dir = repo_root / "plugins"
    files: List[Path] = []
    if plugins_dir.is_dir():
        for pattern in ("*.md", "*.sh"):
            files.extend(plugins_dir.rglob(pattern))
    customization = repo_root / "docs" / "customization.md"
    if customization.is_file():
        files.append(customization)
    return sorted(set(files))


def main(argv: List[str]) -> int:  # noqa: C901 - mirrors the oracle's single-pass CLI shape
    args = list(argv)

    dry_run = False
    if args and args[0] == "--dry-run":
        dry_run = True
        args = args[1:]

    if len(args) % 2 != 0:
        print("Error: arguments after [--dry-run] must be ROLE NAME pairs.", file=sys.stderr)
        print(f"Usage: {PROG} [--dry-run] ROLE NAME [ROLE NAME ...]", file=sys.stderr)
        print(
            f'Example: {PROG} "the Staff Engineer" "Alex" "the Game Dev Reviewer" "Jordan"',
            file=sys.stderr,
        )
        return 1

    if len(args) == 0:
        print("Error: no ROLE NAME pairs provided.", file=sys.stderr)
        print("Known roles:", file=sys.stderr)
        for r in KNOWN_ROLES:
            print(f"  {r}", file=sys.stderr)
        return 1

    roles: List[str] = []
    names: List[str] = []
    new_slugs: List[str] = []
    for i in range(0, len(args), 2):
        role, name = args[i], args[i + 1]
        roles.append(role)
        names.append(name)
        new_slugs.append(to_slug(name))
        if role not in KNOWN_ROLES:
            print(f"Error: '{role}' is not a known role label.", file=sys.stderr)
            print("Use one of:", file=sys.stderr)
            for r in KNOWN_ROLES:
                print(f"  {r}", file=sys.stderr)
            return 1

    sub_olds: List[str] = []
    sub_news: List[str] = []
    for role, name in zip(roles, names):
        sub_olds.append(role)
        sub_news.append(name)
        if role[:4] == "the ":
            sub_olds.append(f"The {role[4:]}")
            sub_news.append(name)

    print("Naming plan:")
    for role, name, slug in zip(roles, names, new_slugs):
        print(f"  {role} → {name} (slug: {slug})")
    print(
        "  (sentence-initial capitalization handled automatically: "
        "'The Staff Engineer' as well as 'the Staff Engineer')"
    )
    print("")

    repo_root = _resolve_repo_root()
    files = _collect_files(repo_root)
    file_texts = {f: f.read_text(encoding="utf-8", errors="surrogateescape") for f in files}

    for role in roles:
        if role[:4] != "the ":
            continue
        unarticulated = role[4:]
        pattern = re.compile(r"\b" + re.escape(unarticulated) + r"\b")
        found_files = []
        for f in files:
            _, body = _split_frontmatter(file_texts[f])
            if pattern.search(body):
                found_files.append(f)
        if found_files:
            print(
                f"Note: '{unarticulated}' (unarticulated form of '{role}') "
                f"appears in {len(found_files)} file(s)."
            )
            print("  These will NOT be substituted — only the articulated form is replaced.")
            print(
                "  This is usually correct (generic prose like 'a staff engineer' "
                "should not be renamed)."
            )
            print("")

    if dry_run:
        print("Dry-run mode — no files will be modified.")
        print("")
        for old, new in zip(sub_olds, sub_news):
            total_replacements = 0
            total_files = 0
            for f in files:
                count = count_prose_matches(old, file_texts[f])
                if count > 0:
                    print(f"  [{old} → {new}] {f}: {count} match(es)")
                    total_replacements += count
                    total_files += 1
            print(f"  {old} → {new}: {total_replacements} replacement(s) across {total_files} file(s)")
            print("")
        return 0

    pair_replacements = {old: 0 for old in sub_olds}
    pair_files = {old: 0 for old in sub_olds}
    total_files_modified = 0

    for f in files:
        text = file_texts[f]
        file_modified = False
        for old, new in zip(sub_olds, sub_news):
            count = count_prose_matches(old, text)
            if count > 0:
                bak = f.parent / (f.name + ".bak")
                bak.write_text(text, encoding="utf-8", errors="surrogateescape", newline="\n")
                text = replace_in_prose(old, new, text)
                pair_replacements[old] += count
                pair_files[old] += 1
                file_modified = True
        if file_modified:
            f.write_text(text, encoding="utf-8", errors="surrogateescape", newline="\n")
            file_texts[f] = text
            total_files_modified += 1

    print("Persona names bound:")
    for old, new in zip(sub_olds, sub_news):
        print(f"  {old} → {new}: {pair_replacements[old]} substitutions across {pair_files[old]} files")
    print("")
    print(f"Total files modified: {total_files_modified}")
    print("")
    print("Infrastructure unchanged: agent filenames (staff-eng.md, etc.), YAML name: fields,")
    print("and subagent_type dispatch keys are role-based and not affected by naming.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

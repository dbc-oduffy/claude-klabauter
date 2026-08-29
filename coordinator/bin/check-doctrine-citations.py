# Unix shebang — see resolve-repo-path.py's header note: gen-launcher-shim.py's
# --ensure-unix mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD); this
# line is no longer regenerated but is kept for parity with its bin/ siblings.
"""check-doctrine-citations.py — refuse a doctrine citation that resolves to
nothing, or to more than one doctrine tree.

A doctrine document cited by repo-relative path (`docs/wiki/foo.md`) resolves
to nothing when the ceremony that reads it runs with cwd = a consumer repo,
and resolves to DIFFERENT content once both repos hold a same-named file. The
origin incident: two analysts concluded a cited wiki page "does not exist",
invented its content from sibling files, and reported success — nothing
errored, because nothing was asked to.

Mechanism (settled by the spike this CLI implements —
docs/research/spike-verdicts/2026-08-29-doctrine-document-citation-resolution.md,
prior art sphinx-doc/sphinx#7280 / intersphinx `nitpicky` mode): an explicit,
never-inferred prefix -> tree map. A citation carrying one of the recognized
disambiguating prefixes (`coordinator/`, `~/.claude/`) resolves ONLY against
the tree that prefix names — missing there is unresolvable, full stop, never
a fallback search of the other trees. A citation with no recognized prefix
(the common case — most citations in the wild are bare `docs/...` paths) is
checked against every registered tree; resolving in exactly one is fine,
resolving in zero is unresolvable, and resolving in more than one is
AMBIGUOUS. Ambiguity is a refusal, never a tiebreak — a reference that
silently resolves to the wrong project's document is worse than one that
fails outright, which is the whole argument upstream's #7280 already made.

Usage:
    check-doctrine-citations.py --corpus <dir> [--corpus <dir> ...]
        [--tree NAME=PATH ...] [--no-default-trees]

Exit 0: every citation found resolved to exactly one tree (or none were
  found). Exit 1: at least one citation is unresolvable or ambiguous — every
  offending citation is named, once, with its source file and the reason.

Illustrative forms (a glob metacharacter, a `{...}` template slot, a literal
`YYYY-MM-DD-` or `path/to/` segment, or an `<...>` angle placeholder) are
excluded from resolution, not counted as dangling — the census
(state/audits/2026-07-23-doctrine-doc-reference-resolution-census.md) broke
these out for the same reason: folding them into the dangling bucket
inflated it by ~40%, and at that false-positive rate the lint gets
suppressed rather than trusted. Excluded citations are counted and reported
in the summary line so the exclusion stays visible.

`--consumer-root PATH` answers the question this tool exists for: not
"does this citation resolve given the whole map" but "does it resolve from
where the agent actually stands." A citation is reported dead-from-consumer
when its literal text (prefix + core path) does not exist under PATH, but
DOES exist in a DoE tree — the 21-across-11-unique-paths bucket the spike
measured, and the list C4's memo carries downstream.

Usage:
    check-doctrine-citations.py --corpus <dir> [--corpus <dir> ...]
        [--tree NAME=PATH ...] [--no-default-trees]
        [--consumer-root PATH]

Exit 0: every citation found resolved to exactly one tree (or none were
  found). Exit 1: at least one citation is unresolvable or ambiguous — every
  offending citation is named, once, with its source file and the reason.

Negative-spec: does NOT spawn a subprocess per citation or per file (a single
`os.walk` + in-process regex scan of the corpus, and at most one
`resolve-repo-path.py` subprocess call per default tree, memoized). Does NOT
infer a winner when a bare citation resolves in more than one tree — it
reports both and exits non-zero. Does NOT silently skip a citation that
matches the scan pattern but sits in a fenced code block or comment; scope is
plain-text substring matching, matching the spike's own probe methodology.
Does NOT count an illustrative/placeholder form as dangling or ambiguous —
it is excluded and separately tallied, never silently dropped. Does NOT
silently narrow the candidate tree set when a default tree fails to resolve
(P1 fix) — a failed `doe-claude` resolution (unregistered shortname, stale
`repos.*` key, a transient subprocess failure) is named and forces a
non-zero exit, even with zero citation findings, rather than degrading to a
false-clean scan of whatever trees happened to resolve. Does NOT perform a
real filesystem `..` traversal when checking a `../../`-prefixed citation
against `--consumer-root` (P-nit fix) — such a citation always falls
through to the DoE-tree check instead of an unsafe path join.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BOOTSTRAP_DONE = False


def _bootstrap_engine() -> None:
    """See resolve-repo-path.py's identical helper: puts `_REPO_ROOT` on
    `sys.path` for the deferred `coordinator_core.*` import below, on first
    use only — never at module scope, so importing this file cannot mutate a
    warm server's shared `sys.path`."""
    global _BOOTSTRAP_DONE
    if _BOOTSTRAP_DONE:
        return
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    _BOOTSTRAP_DONE = True


# Citation shapes measured live by the spike's probe 3 regex, reproduced
# here verbatim as the recognized prefix set. Each entry maps the LITERAL
# leading text of a citation to the single tree name it disambiguates to.
# `_tree_for_prefix` does an exact `.get()` against the prefix text
# `_CITATION_RE` already extracted, so THIS dict's insertion order is
# irrelevant. If longest-alternative-first care is ever needed, it belongs
# to `_CITATION_RE`'s own alternation, not this map.
_PREFIX_TREE_MAP: dict[str, str] = {
    "coordinator/": "doe_coordinator",
    "~/.claude/": "doe_root",
}

_CITATION_RE = re.compile(
    r"""(?P<prefix>coordinator/|~/\.claude/|\.\./\.\./|/)?
        (?P<core>docs/(?:wiki|decisions|plans|problems|research)/[^\s\)\]"'<>]+\.md)
    """,
    re.VERBOSE,
)

# Matches the census's own definition (state/audits/2026-07-23-doctrine-doc-
# reference-resolution-census.md § headline: "Illustrative placeholders
# (YYYY-MM-DD-, foo.md, path/to/... )") — a glob metacharacter, a `{...}`
# template slot, a literal `YYYY-MM-DD-`/`path/to/` segment, or an `<...>`
# angle placeholder. Tested against the full matched text (prefix + core).
_ILLUSTRATIVE_RE = re.compile(r"[*?{}<>]|YYYY-MM-DD-|path/to/", re.IGNORECASE)


def _is_illustrative(full_text: str) -> bool:
    return bool(_ILLUSTRATIVE_RE.search(full_text))


_DOE_TREE_NAMES = ("doe_root", "doe_coordinator")

_DEFAULT_TREE_SHORTNAMES: dict[str, tuple[str, str]] = {
    # tree name -> (repo shortname for resolve-repo-path.py, subpath under it)
    "doe_root": ("doe-claude", ""),
    "doe_coordinator": ("doe-claude", "coordinator"),
    "claude-klabauter": ("claude-klabauter", ""),
}


@dataclass
class Citation:
    core_path: str
    prefix: str
    source_file: str
    line_no: int


@dataclass
class Finding:
    citation: Citation
    reason: str  # "unresolvable" | "ambiguous" | "dead-from-consumer"
    candidate_trees: list[str] = field(default_factory=list)


def _resolve_repo_path_shortname(shortname: str) -> tuple[str, str]:
    """Subprocess call to the sibling resolver, memoized by the caller.
    Returns (resolved_path, error_message) — error_message is "" on success.

    resolve-repo-path.py's own contract is FAIL-LOUD-SKIP: an unregistered
    shortname is a legitimate empty-stdout/exit-0 skip, distinct from a
    genuine spawn failure or non-zero exit. From THIS tool's perspective
    that distinction does not matter — either way a default tree this run
    was supposed to scan did not resolve, and P1's fix is to make that
    visible rather than silently narrowing the candidate set. So every
    failure mode here (spawn OSError, non-zero exit, empty stdout) is
    surfaced with whatever stderr resolve-repo-path.py wrote, never
    swallowed via `capture_output=True` the way the pre-fix version did."""
    _bootstrap_engine()
    from coordinator_core.win_portability import no_console_creationflags

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolve-repo-path.py")
    try:
        result = subprocess.run(
            [sys.executable, script, shortname],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError as exc:
        return "", f"resolve-repo-path.py spawn failed: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        detail = f": {stderr}" if stderr else ""
        return "", f"resolve-repo-path.py exited {result.returncode}{detail}"
    resolved = result.stdout.strip()
    if not resolved:
        detail = f": {stderr}" if stderr else " (unregistered shortname or empty repos.* key)"
        return "", f"resolve-repo-path.py resolved empty{detail}"
    return resolved, ""


def _default_tree_roots() -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Returns (roots, failures) — failures is [(tree_name, error_message)]
    for every default tree that did not resolve. The caller decides whether
    a failure is fatal (it is, unless a --tree override fills the gap)."""
    roots: dict[str, str] = {}
    failures: list[tuple[str, str]] = []
    resolved_shortnames: dict[str, tuple[str, str]] = {}
    for tree_name, (shortname, subpath) in _DEFAULT_TREE_SHORTNAMES.items():
        if shortname == "claude-klabauter":
            base, err = _REPO_ROOT, ""
        else:
            if shortname not in resolved_shortnames:
                resolved_shortnames[shortname] = _resolve_repo_path_shortname(shortname)
            base, err = resolved_shortnames[shortname]
        if err or not base:
            failures.append((tree_name, err or "resolved empty"))
            continue
        roots[tree_name] = os.path.join(base, subpath) if subpath else base
    return roots, failures


def _parse_tree_overrides(pairs: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--tree expects NAME=PATH, got: {pair}")
        name, path = pair.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"--tree expects NAME=PATH, got: {pair}")
        overrides[name] = path
    return overrides


def _unscannable_corpus_dirs(corpus_dirs: list[str]) -> list[tuple[str, str]]:
    """Return (path, reason) for every corpus argument that cannot be walked.

    Negative spec: a corpus path that does not exist, or names a file rather than a
    directory, must NEVER reach the scan as an empty contribution.  os.walk() yields
    nothing for both and raises nothing, so the run would report a clean corpus it
    never opened -- the same silent-skip this tool exists to refuse, committed by the
    tool itself.  Callers treat a non-empty return as fatal before any scanning.
    """
    unscannable: list[tuple[str, str]] = []
    for corpus_dir in corpus_dirs:
        if not os.path.exists(corpus_dir):
            unscannable.append((corpus_dir, "does not exist"))
        elif not os.path.isdir(corpus_dir):
            unscannable.append((corpus_dir, "is a file, not a directory"))
    return unscannable


def _iter_corpus_files(corpus_dirs: list[str]):
    for corpus_dir in corpus_dirs:
        for root, _dirs, files in os.walk(corpus_dir):
            for name in files:
                if name.endswith(".md"):
                    yield os.path.join(root, name)


def _extract_citations(path: str) -> tuple[list[Citation], int]:
    """Returns (citations, illustrative_excluded_count)."""
    citations: list[Citation] = []
    excluded = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return citations, excluded
    for line_no, line in enumerate(lines, start=1):
        for match in _CITATION_RE.finditer(line):
            prefix = match.group("prefix") or ""
            core = match.group("core")
            if _is_illustrative(prefix + core):
                excluded += 1
                continue
            citations.append(Citation(core_path=core, prefix=prefix, source_file=path, line_no=line_no))
    return citations, excluded


def _tree_for_prefix(prefix: str) -> str | None:
    return _PREFIX_TREE_MAP.get(prefix)


def resolve_citations(citations: list[Citation], tree_roots: dict[str, str]) -> list[Finding]:
    findings: list[Finding] = []
    for citation in citations:
        mapped_tree = _tree_for_prefix(citation.prefix)
        if mapped_tree is not None:
            root = tree_roots.get(mapped_tree)
            exists = bool(root) and os.path.isfile(os.path.join(root, citation.core_path))
            if not exists:
                findings.append(Finding(citation, "unresolvable", [mapped_tree]))
            continue

        matches = [
            tree_name
            for tree_name, root in tree_roots.items()
            if os.path.isfile(os.path.join(root, citation.core_path))
        ]
        if not matches:
            findings.append(Finding(citation, "unresolvable", list(tree_roots)))
        elif len(matches) > 1:
            findings.append(Finding(citation, "ambiguous", sorted(matches)))
    return findings


def _contains_dotdot_segment(literal_path: str) -> bool:
    return any(part == ".." for part in re.split(r"[\\/]", literal_path))


def find_dead_from_consumer(citations: list[Citation], tree_roots: dict[str, str], consumer_root: str) -> list[Finding]:
    """The consumer-cwd leg: not "does this resolve given the whole map" but
    "does it resolve from where the agent actually stands." A citation's
    LITERAL text (prefix + core path, exactly as an agent at `consumer_root`
    would read it) is checked against `consumer_root` first — resolving
    there means it is correct as written and not reported. Failing there, it
    is reported dead-from-consumer only if it resolves in a DoE tree (never
    against the claude-klabauter/consumer tree entry, which is the question already
    answered by the first check).

    A `../../`-prefixed citation (the census counted 5 of this shape) is
    NEVER joined onto `consumer_root` for the literal-existence check: doing
    so performs a genuine filesystem `..` traversal that can escape
    `consumer_root` and match an unrelated file, silently flipping the
    verdict. Such a citation always falls through to the DoE-tree check
    below instead — the traversal is real on disk but this tool never
    resolves relative to a citing file's own directory, so there is no
    correct join to perform; skipping the literal check is the safe read,
    not a coincidental one."""
    findings: list[Finding] = []
    doe_roots = {name: tree_roots[name] for name in _DOE_TREE_NAMES if tree_roots.get(name)}
    for citation in citations:
        literal_path = citation.prefix + citation.core_path
        if not _contains_dotdot_segment(literal_path) and os.path.isfile(os.path.join(consumer_root, literal_path)):
            continue
        doe_matches = [
            tree_name for tree_name, root in doe_roots.items() if os.path.isfile(os.path.join(root, citation.core_path))
        ]
        if doe_matches:
            findings.append(Finding(citation, "dead-from-consumer", sorted(doe_matches)))
    return findings


def _format_finding(finding: Finding) -> str:
    loc = f"{finding.citation.source_file}:{finding.citation.line_no}"
    cited = f"{finding.citation.prefix}{finding.citation.core_path}"
    if finding.reason == "ambiguous":
        trees = ", ".join(finding.candidate_trees)
        return f"{loc}: ambiguous citation '{cited}' resolves in trees: {trees}"
    if finding.reason == "dead-from-consumer":
        trees = ", ".join(finding.candidate_trees)
        return f"{loc}: dead-from-consumer citation '{cited}' (resolves only in: {trees})"
    return f"{loc}: unresolvable citation '{cited}'"


def run(
    corpus_dirs: list[str],
    tree_overrides: dict[str, str],
    use_default_trees: bool,
    consumer_root: str | None = None,
) -> tuple[int, list[str], int, int]:
    """Returns (exit_code, finding_lines, illustrative_excluded_count,
    unresolved_default_tree_count).

    P1 fix: a default tree that failed to resolve (unregistered shortname,
    stale repos.* key, a transient resolve-repo-path.py subprocess failure)
    is NEVER silently dropped from the candidate set — that degraded mode
    is exactly the failure this lint exists to catch, reproduced inside the
    lint itself (fewer trees to check against -> fewer possible misses/
    ambiguities -> a false-clean exit 0). A `--tree` override for the same
    name still fills the gap deliberately; only a tree left genuinely absent
    after overrides are applied is reported and forces a non-zero exit,
    even when zero citations were found."""
    tree_roots: dict[str, str] = {}
    default_tree_failures: list[tuple[str, str]] = []
    if use_default_trees:
        default_roots, default_tree_failures = _default_tree_roots()
        tree_roots.update(default_roots)
    tree_roots.update(tree_overrides)

    unresolved_defaults = [
        (name, err) for name, err in default_tree_failures if name not in tree_roots
    ] if use_default_trees else []

    unscannable = _unscannable_corpus_dirs(corpus_dirs)
    if unscannable:
        return (
            2,
            [f"corpus path {path!r} {reason} — nothing was scanned" for path, reason in unscannable],
            0,
            len(unresolved_defaults),
        )

    citations: list[Citation] = []
    excluded_total = 0
    for path in _iter_corpus_files(corpus_dirs):
        file_citations, file_excluded = _extract_citations(path)
        citations.extend(file_citations)
        excluded_total += file_excluded

    if consumer_root is not None:
        findings = find_dead_from_consumer(citations, tree_roots, consumer_root)
    else:
        findings = resolve_citations(citations, tree_roots)

    lines = [_format_finding(f) for f in findings]
    for tree_name, err in unresolved_defaults:
        lines.append(f"default tree '{tree_name}' unresolved: {err}")

    exit_code = 1 if (findings or unresolved_defaults) else 0
    return exit_code, lines, excluded_total, len(unresolved_defaults)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="check-doctrine-citations.py",
        description="Refuse a doctrine citation that resolves to nothing or to more than one tree.",
    )
    parser.add_argument("--corpus", action="append", default=[], help="Directory to scan for citations; repeatable.")
    parser.add_argument(
        "--tree",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Override or add a tree root (doe_root, doe_coordinator, claude-klabauter); repeatable.",
    )
    parser.add_argument(
        "--no-default-trees",
        action="store_true",
        help="Skip resolve-repo-path.py default tree resolution; use only --tree overrides.",
    )
    parser.add_argument(
        "--consumer-root",
        default=None,
        metavar="PATH",
        help=(
            "Answer 'does this citation resolve from where the agent actually stands', "
            "not 'does it resolve given the whole map': report every citation whose "
            "literal text does not exist under PATH but DOES exist in a DoE tree."
        ),
    )
    args = parser.parse_args(argv[1:])

    corpus_dirs = args.corpus or [_REPO_ROOT]
    try:
        tree_overrides = _parse_tree_overrides(args.tree)
    except ValueError as exc:
        print(f"check-doctrine-citations.py: {exc}", file=sys.stderr)
        return 2

    # P2 fix: --no-default-trees with zero --tree flags configures nothing
    # to scan against, which is a usage error, not "every citation failed."
    if args.no_default_trees and not tree_overrides:
        print(
            "check-doctrine-citations.py: --no-default-trees given with no --tree "
            "override — nothing is configured to resolve against",
            file=sys.stderr,
        )
        return 2

    exit_code, lines, excluded, unresolved_defaults = run(
        corpus_dirs,
        tree_overrides,
        use_default_trees=not args.no_default_trees,
        consumer_root=args.consumer_root,
    )
    for line in lines:
        print(line, file=sys.stderr)
    finding_count = len(lines) - unresolved_defaults
    if exit_code == 2 and any("nothing was scanned" in line for line in lines):
        print(
            "check-doctrine-citations.py: refused before scanning — "
            "no citation was examined, so a clean result would have been a false one",
            file=sys.stderr,
        )
        return exit_code
    if exit_code != 0:
        reason = "resolve from consumer cwd" if args.consumer_root else "resolve unambiguously"
        print(
            f"check-doctrine-citations.py: {finding_count} citation(s) failed to {reason} "
            f"({excluded} illustrative citation(s) excluded, "
            f"{unresolved_defaults} default tree(s) unresolved)",
            file=sys.stderr,
        )
    else:
        print(
            f"check-doctrine-citations.py: 0 citation(s) failed "
            f"({excluded} illustrative citation(s) excluded, "
            f"{unresolved_defaults} default tree(s) unresolved)",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

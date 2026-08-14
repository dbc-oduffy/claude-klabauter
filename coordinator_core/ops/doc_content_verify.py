"""
coordinator_core.ops.doc_content_verify — content verification for human-facing docs.

Purpose: answers a different question than `doc_staleness.py`. Staleness asks
"has this doc moved recently"; this module asks "is what this doc SAYS still
TRUE" — does every repo-relative path it cites (in a fenced shell/py command, an
inline code span, or a relative markdown link) actually resolve on disk. This is
the mechanism that would have caught the 2026-07-28 incident: the root README's
cold-bootstrap command cited `coordinator/scripts/install-maximalist.py` six days
after commit `b644d5a9` emptied that path — the doc had been touched recently
(so staleness alone would not have fired) but said something false.

DISTINCT MECHANISM from `doc_staleness.py` — separate op, separate tests,
separate verdict. Do not fold the two together.

Spec backlink: DoE-claude:pln-human-facing-doc-staleness-det-d9c047 § C6b, AC12, AC13
               (DoE-claude)

v1 positive surface (what gets extracted and checked):
    - path tokens inside shell/py fenced code blocks that look repo-relative
    - inline code-span paths containing a directory separator
    - relative markdown links (target file existence, and anchor existence if
      an anchor is given)

v1 negative surface — the acceptance bar, and it matters more than the positive
surface. A naive "cited path absent from disk" predicate is not shippable: this
repo's own doc citations resolve against sibling repos (`CLAUDE_KLABAUTER_ROOT`, the
settings-home `bin/`) as often as against the local tree, and a plugin-root
doc (e.g. `coordinator/README.md`) cites its own paths relative to ITS OWN
directory, not the repo root — a bare absence check cannot separate a real
defect from either shape of correct citation. Four mechanical pieces close
that gap, applied in this order (`resolve_token`), stopping at the first hit:
    (a) an exclusion set applied BEFORE any resolution (`is_excluded`) —
        slash commands, URLs/bare domains, globs/placeholders,
        `$`/`${`/`~`-prefixed tokens are never path citations;
    (b) repo-root resolution — the token as a direct repo-root-relative path;
    (c) doc-relative resolution — the token resolved against the CITING
        DOC'S OWN DIRECTORY (e.g. a citation in `coordinator/README.md`
        tried at `<repo_root>/coordinator/<token>`). A hit here is `ok`,
        never a finding — this is the fix for the plugin-root false-positive
        class (measured: 26 spurious findings against DoE-claude's own
        `coordinator/README.md` before this piece existed, 24 of them this
        exact shape). A doc at the repo root makes this identical to (b), so
        root-level docs are unaffected. Resolution is containment-checked —
        a `../`-escaping doc-relative token cannot resolve outside the repo
        root (reuses the containment logic `_contained_doc_path` uses for
        CLI argv);
    (d) cross-repo resolution — a token still unresolved is re-resolved
        against declared sibling roots, and a token that resolves in a
        sibling root is `resolves-cross-repo`, never `absent`;
    (e) a per-doc ignore list (`doc_verify_ignore:` in `coordinator.local.md`,
        see C4) for genuinely-unresolvable residual citations.

Anti-scope for v1 (deliberate, recorded in the plan's C8 wiki chunk, not a
silent omission): no prose-claim checking (a README asserting "118 .py
files"), no count re-derivation, no diffing against
`state/strategic/self-description.yaml`. Those are judgment-shaped or
expensive; v1 is the mechanically falsifiable subset only. Also anti-scope:
Windows-style backslash-separated paths (e.g. coordinator\\scripts\\install-
maximalist.cmd) are never extracted — the path-token regex only recognizes
`/` as a separator, a deliberate decision (backslash recognition risks new
false positives against ordinary prose) rather than an oversight.

Negative-spec:
    - Never reports `resolves-cross-repo` as a `Finding` — only `absent` and
      `moved` are failures. A citation that resolves in a sibling root is a
      correct cross-repo reference, not a defect; conflating the two is
      exactly the false-positive shape this module exists to avoid (measured
      against this repo's own docs, a naive absence check fires on the large
      majority of citations, most of them correct-as-written cross-repo refs).
    - Does not check prose claims, counts, or fenced blocks whose language is
      not shell/py-shaped — see anti-scope above.
    - Does not mutate any file on disk and does not shell out to git — callers
      that need historical-tree resolution (AC13's replay) supply their own
      `repo_exists`/`sibling_checkers`/`read_target_text` callables; this
      module's core (`extract_citations`, `is_excluded`, `verify_doc`) is
      pure and works equally against a live filesystem or a `git show`/`git
      ls-tree`-backed callable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One path- or link-shaped token extracted from a human-facing doc."""

    doc: str
    line: int
    token: str
    kind: str  # "fenced-code-path" | "code-span-path" | "md-link"
    anchor: Optional[str] = None


@dataclass(frozen=True)
class Finding:
    """A citation that failed verification against disk.

    `reason` is one of:
        "absent" — the token resolves nowhere (not locally, not cross-repo)
        "moved"  — the linked file exists but the given anchor does not
    `resolves-cross-repo` is deliberately never a Finding reason — see the
    module docstring's negative-spec.
    """

    doc: str
    line: int
    token: str
    reason: str


ExistenceChecker = Callable[[str], bool]
TargetTextReader = Callable[[str], Optional[str]]

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

# Review: code-reviewer — Finding 4 (P2). CommonMark/GFM permit tilde fences
# (`~~~`) as well as backtick fences; a tilde-fenced block was previously
# silently invisible to extraction (total silence, not degraded coverage).
# Group 1 captures which marker opened the fence so the tracking loop below
# can require a matching close (a backtick fence is closed only by a
# backtick fence, and vice versa) rather than mixing the two.
_FENCE_RE = re.compile(r"^(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_SHELL_FENCE_LANGS = {"bash", "sh", "shell", "zsh", "python", "python3", "py"}
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# Review: code-reviewer — Finding 1 (P1). Boundary chars for fenced-code-path
# word-splitting: whitespace + shell quoting/grouping metachars. Deliberately
# does NOT include `$`/`~`/glob chars — those must survive into the word so
# `is_excluded()` sees them (see extract_citations' in_fence branch).
_SHELL_WORD_SPLIT_RE = re.compile(r"""[\s()"'`]+""")


def extract_citations(doc_rel_path: str, text: str) -> List[Citation]:
    """Extract the three v1 citation classes, line-numbered, from a doc's text.

    Fence detection tracks open/close state across the whole doc so that only
    tokens genuinely INSIDE a shell/py fence are treated as fenced-code-path
    citations; a fence in any other language (or no language) is skipped —
    v1's positive surface is deliberately narrow (see module docstring).
    """
    citations: List[Citation] = []
    in_fence = False
    fence_lang = ""
    fence_marker = ""

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        fence_match = _FENCE_RE.match(raw_line.strip())
        if fence_match:
            marker = fence_match.group(1)
            if in_fence:
                if marker == fence_marker:
                    in_fence = False
                    fence_lang = ""
                    fence_marker = ""
                    continue
                # A differently-marked fence line inside an open fence is
                # literal fenced content (CommonMark), not a close — fall
                # through to the in-fence handling below.
            else:
                in_fence = True
                fence_lang = fence_match.group(2).lower()
                fence_marker = marker
                continue

        if in_fence:
            # Review: code-reviewer — Finding 1 (P1). Split into whole shell
            # words (on whitespace and non-truncation-risk shell metachars —
            # quotes/parens/backticks — but NOT on `$`/`~`/glob chars, which
            # are the exclusion-relevant prefix) and test each whole word
            # against is_excluded() BEFORE regex-matching, mirroring the
            # code-span-path branch below (fullmatch, not finditer). An
            # unanchored finditer() over the raw line can start a match
            # immediately after a `$`, `~`, `*`, `?`, or `[` — silently
            # truncating the exact prefix is_excluded() exists to see,
            # defeating AC12(a)'s applied-before-resolution guarantee for
            # this citation class. A leading `/` left over after stripping a
            # quote/paren boundary (e.g. `)/coordinator/scripts/foo.py`
            # inside a `$(...)` command substitution) is not a truncation
            # risk — `/` is never an exclusion-trigger char — so it is
            # stripped rather than rejected, preserving genuine embedded
            # path citations.
            if fence_lang in _SHELL_FENCE_LANGS:
                for word in _SHELL_WORD_SPLIT_RE.split(raw_line):
                    if not word or is_excluded(word):
                        continue
                    candidate = word.lstrip("/")
                    if candidate and _PATH_TOKEN_RE.fullmatch(candidate):
                        citations.append(
                            Citation(doc_rel_path, lineno, candidate, "fenced-code-path")
                        )
            continue

        link_spans = []
        for m in _MD_LINK_RE.finditer(raw_line):
            link_spans.append((m.start(1), m.end(1)))
            target = m.group(1)
            if target.startswith("#"):
                continue  # in-page anchor only, no file to resolve
            path_part, _, anchor_part = target.partition("#")
            if not path_part:
                continue
            citations.append(
                Citation(doc_rel_path, lineno, path_part, "md-link", anchor_part or None)
            )

        for m in _CODE_SPAN_RE.finditer(raw_line):
            start, end = m.span(1)
            if any(start >= ls and end <= le for ls, le in link_spans):
                continue  # already captured as a markdown link target
            token = m.group(1)
            if "/" not in token:
                continue
            if not _PATH_TOKEN_RE.fullmatch(token):
                continue
            citations.append(Citation(doc_rel_path, lineno, token, "code-span-path"))

    return citations


# ---------------------------------------------------------------------------
# Exclusion set — applied BEFORE resolution (v1 negative surface, piece (a))
# ---------------------------------------------------------------------------

_SLASH_COMMAND_RE = re.compile(r"^/[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*$")
_URL_SCHEME_RE = re.compile(r"://")
_BARE_DOMAIN_FIRST_SEGMENT_RE = re.compile(r"^[A-Za-z0-9-]+\.[A-Za-z]{2,}$")
_GLOB_CHARS = frozenset("*?[]")
_PLACEHOLDER_RE = re.compile(r"<[^>]*>|\.\.\.|YYYY-MM")


def is_excluded(token: str) -> bool:
    """True if `token` is not a path citation and must never be resolved.

    Applied BEFORE resolution — an excluded token never becomes a Finding,
    regardless of whether it happens to resolve or not. Categories:
    slash-command shapes, URLs/bare domains, glob/placeholder shapes, and
    `$`/`${`/`~`-prefixed tokens (env-var- or home-relative, not repo-relative).
    """
    if _SLASH_COMMAND_RE.match(token):
        return True
    if _URL_SCHEME_RE.search(token):
        return True
    if "/" in token:
        first_segment = token.split("/", 1)[0]
        if _BARE_DOMAIN_FIRST_SEGMENT_RE.match(first_segment):
            return True
    if any(ch in _GLOB_CHARS for ch in token):
        return True
    if _PLACEHOLDER_RE.search(token):
        return True
    if token.startswith("$") or token.startswith("~"):
        return True
    return False


# ---------------------------------------------------------------------------
# Resolution — v1 negative surface, piece (b): cross-repo re-resolution
# ---------------------------------------------------------------------------


def resolve_token(
    token: str,
    *,
    repo_exists: ExistenceChecker,
    sibling_checkers: Sequence[ExistenceChecker] = (),
    doc_relative_checker: Optional[ExistenceChecker] = None,
) -> str:
    """Resolve one token to "ok" / "resolves-cross-repo" / "absent".

    Resolution ladder, stopping at the first hit: repo-root (`repo_exists`),
    then doc-relative (`doc_relative_checker` — the citing doc's OWN
    directory, e.g. `coordinator/README.md` resolving `docs/wiki/x.md`
    against `coordinator/docs/wiki/x.md`; a hit here is `ok`, same as a
    repo-root hit, never `resolves-cross-repo`), then `sibling_checkers` in
    order. Only a miss against ALL three is `absent` — this is the mechanism
    that separates a true positive (absent everywhere) from a correct
    plugin-root-relative citation OR a correct cross-repo citation (absent
    locally, present in a declared sibling root).
    """
    if repo_exists(token):
        return "ok"
    if doc_relative_checker is not None and doc_relative_checker(token):
        return "ok"
    for checker in sibling_checkers:
        if checker(token):
            return "resolves-cross-repo"
    return "absent"


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")


def _slugify_heading(heading: str) -> str:
    """GitHub-style heading-to-anchor slug (lowercase, strip punctuation, spaces->hyphens)."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug


def _anchors_in(text: str) -> set:
    """All valid in-doc anchor slugs, including GitHub's dedup-suffix forms.

    Review: code-reviewer — Finding 3 (P2). GitHub appends `-1`, `-2`, ... to
    repeated slugs in document order (e.g. two `## Overview` headings produce
    `#overview` and `#overview-1`). A plain set collapses same-slug headings
    to one entry, so a correct link to the SECOND occurrence's anchor was
    wrongly reported "moved". Walk headings in order, tracking a per-slug
    occurrence counter, to replicate GitHub's suffixing.
    """
    anchors: set = set()
    seen_counts: dict = {}
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if not m:
            continue
        base_slug = _slugify_heading(m.group(2))
        count = seen_counts.get(base_slug, 0)
        slug = base_slug if count == 0 else f"{base_slug}-{count}"
        seen_counts[base_slug] = count + 1
        anchors.add(slug)
    return anchors


def _default_checker(root: Path) -> ExistenceChecker:
    def _check(token: str) -> bool:
        try:
            return (root / token).exists()
        except OSError:
            return False

    return _check


def _doc_relative_checker(root: Path, doc_rel_path: str) -> ExistenceChecker:
    """Checker for a token resolved against `doc_rel_path`'s OWN directory.

    This is the fix for the plugin-root false-positive class: a doc like
    `coordinator/README.md` cites paths relative to `coordinator/`, not the
    repo root, so `coordinator/README.md`'s citation of
    `docs/wiki/x.md` must be tried at `<root>/coordinator/docs/wiki/x.md`.
    A doc at the repo root has an empty relative directory, so this collapses
    to the same candidate `_default_checker(root)` would try — root-level
    docs are unaffected by this piece.

    Containment-checked (`.resolve()` + `relative_to`) so a `../`-escaping
    token cannot resolve outside `root` — mirrors `_contained_doc_path`'s
    CLI-argv containment check below, applied here to citation TOKENS rather
    than argv doc paths.
    """
    resolved_root = root.resolve()
    doc_dir = (root / doc_rel_path).parent

    def _check(token: str) -> bool:
        try:
            candidate = (doc_dir / token).resolve()
            candidate.relative_to(resolved_root)
            return candidate.exists()
        except (OSError, ValueError):
            return False

    return _check


def _default_target_text_reader(root: Path) -> TargetTextReader:
    def _read(token: str) -> Optional[str]:
        try:
            return (root / token).read_text(encoding="utf-8")
        except OSError:
            return None

    return _read


# ---------------------------------------------------------------------------
# Doc-level verification (pure — no filesystem/git assumptions of its own)
# ---------------------------------------------------------------------------


def verify_doc(
    doc_rel_path: str,
    text: str,
    *,
    repo_exists: ExistenceChecker,
    sibling_checkers: Sequence[ExistenceChecker] = (),
    doc_relative_checker: Optional[ExistenceChecker] = None,
    read_target_text: Optional[TargetTextReader] = None,
    ignore: Iterable[str] = (),
) -> List[Finding]:
    """Verify every extracted citation in `text`, returning only failures.

    `repo_exists`/`sibling_checkers`/`doc_relative_checker`/`read_target_text`
    are injected so this function works identically against a live
    filesystem (`verify_doc_on_disk`) and against a historical tree resolved
    via read-only git plumbing (see the AC13 historical-replay test) — this
    module never shells out itself. `doc_relative_checker` is already scoped
    to `doc_rel_path`'s own directory by the caller (see
    `verify_doc_on_disk`) — this function does not derive a directory from
    `doc_rel_path` itself, it only threads the pre-scoped checker through to
    `resolve_token`.
    """
    ignore_set = set(ignore)
    findings: List[Finding] = []

    for citation in extract_citations(doc_rel_path, text):
        token = citation.token
        if is_excluded(token):
            continue
        if token in ignore_set:
            continue

        status = resolve_token(
            token,
            repo_exists=repo_exists,
            sibling_checkers=sibling_checkers,
            doc_relative_checker=doc_relative_checker,
        )
        if status == "absent":
            findings.append(Finding(doc_rel_path, citation.line, token, "absent"))
            continue

        if (
            citation.kind == "md-link"
            and citation.anchor
            and status == "ok"
            and read_target_text is not None
        ):
            target_text = read_target_text(token)
            if target_text is not None and citation.anchor not in _anchors_in(target_text):
                findings.append(Finding(doc_rel_path, citation.line, token, "moved"))

    return findings


def verify_doc_on_disk(
    repo_root: Path,
    doc_rel_path: str,
    *,
    sibling_roots: Sequence[Path] = (),
    ignore: Iterable[str] = (),
) -> List[Finding]:
    """Convenience wrapper: read `doc_rel_path` off disk under `repo_root` and verify it."""
    repo_root = Path(repo_root)
    doc_path = repo_root / doc_rel_path
    text = doc_path.read_text(encoding="utf-8")

    sibling_checkers = [_default_checker(Path(root)) for root in sibling_roots]

    return verify_doc(
        doc_rel_path,
        text,
        repo_exists=_default_checker(repo_root),
        sibling_checkers=sibling_checkers,
        doc_relative_checker=_doc_relative_checker(repo_root, doc_rel_path),
        read_target_text=_default_target_text_reader(repo_root),
        ignore=ignore,
    )


def build_findings_report_from_registry(repo_root: str | Path, **kwargs: Any) -> dict:
    """Registry-driven convenience wrapper -- reads the per-repo doc registry
    (`coordinator_core.ops.doc_registry.resolve_doc_registry_config`) for
    `human_facing_docs` and `doc_verify_ignore`, then calls `verify_doc_on_disk`
    once per declared doc that exists on disk. Returns the same
    `{"findings": [...]}` JSON shape `main()`'s CLI prints, so a caller that
    used to parse the CLI's stdout can consume this instead.

    Named/shaped to play the same convenience-wrapper role for THIS signal
    that `doc_staleness.build_doc_staleness_report_from_registry` plays for
    the sibling staleness signal (see this module's own docstring for why the
    two signals are a DISTINCT MECHANISM, not delegation) -- so a caller
    consuming both ops reads them consistently. `**kwargs` is accepted and
    ignored for exact positional/keyword-shape parity with that sibling
    wrapper's own `**kwargs: Any` signature; this wrapper has no forwarding
    use for it today.

    `repo_root` is the consumer repo's root, never a cwd-implicit default --
    same contract `resolve_doc_registry_config` and `verify_doc_on_disk`
    already require of their own callers.

    Negative-spec: does NOT delegate to `doc_staleness.
    build_doc_staleness_report_from_registry` -- both wrappers independently
    call `resolve_doc_registry_config(repo_root) -> DocRegistryConfig` (the
    real public API on `doc_registry`) directly, rather than one delegating
    to the other; each stays a self-contained, separately-testable read of
    the same per-repo registry.
    """
    from coordinator_core.ops.doc_registry import (  # noqa: PLC0415
        resolve_doc_registry_config,
    )

    root = Path(repo_root)
    config = resolve_doc_registry_config(str(root))
    sibling_roots = _sibling_roots()

    all_findings: List[Finding] = []
    for doc_rel_path in config.human_facing_docs:
        doc_path = root / doc_rel_path
        if not doc_path.exists():
            continue
        all_findings.extend(
            verify_doc_on_disk(
                root,
                doc_rel_path,
                sibling_roots=sibling_roots,
                ignore=config.doc_verify_ignore,
            )
        )

    return {
        "findings": [
            {"doc": f.doc, "line": f.line, "token": f.token, "reason": f.reason}
            for f in all_findings
        ]
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _sibling_roots() -> List[Path]:
    """Best-effort resolution of the declared sibling roots (CLAUDE_KLABAUTER_ROOT, settings-home bin).

    Never raises — an unresolvable sibling root just means cross-repo
    resolution against it is skipped; the CLI still runs and reports local
    (`absent` vs `ok`) results.
    """
    roots: List[Path] = []
    try:
        from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

        roots.append(Path(coordinator_claude_klabauter_root()))
    except Exception:
        pass
    try:
        from coordinator_core._settings_home import settings_home

        roots.append(settings_home() / "bin")
    except Exception:
        pass
    return roots


def _parse_flat_list(raw: str) -> List[str]:
    """Parse a `[a, b, c]`-shaped frontmatter scalar into a list of strings.

    Absent/empty input -> []. Not a YAML parser — mirrors the bracket-list
    shape `human_facing_docs`/`doc_verify_ignore` (C4) are documented to use.
    """
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    items = [item.strip().strip("'\"") for item in raw.split(",")]
    return [item for item in items if item]


def _read_local_md_list(repo_root: Path, key: str) -> List[str]:
    try:
        from coordinator_core.resolve_validation_cmd import cs_read_local_md_key
    except ImportError:
        return []
    return _parse_flat_list(cs_read_local_md_key(str(repo_root), key))


_DEFAULT_HUMAN_FACING_DOCS = ["README.md", "INSTALL.md", "CONTEXT.md", "CONTRIBUTING.md"]


def _contained_doc_path(root: Path, doc_rel_path: str) -> Optional[Path]:
    """Resolve `root / doc_rel_path` and confirm the result stays under `root`.

    Review: code-reviewer — Finding 5 (P2). `main()` previously joined
    `--doc`/`--root` argv values with no traversal/allowlist check — a
    `--doc ../../../etc/passwd`-shaped argument would attempt to read
    outside the intended repo root. Returns `None` (and prints a clear
    message to stderr) rather than silently resolving wherever the join
    happens to land.
    """
    resolved_root = root.resolve()
    candidate = (root / doc_rel_path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        print(
            f"doc-content-verify: refusing to read '{doc_rel_path}' — "
            f"resolves outside root '{resolved_root}'",
            file=sys.stderr,
        )
        return None
    return candidate


def _build_parser() -> argparse.ArgumentParser:
    # Review: code-reviewer — Finding 3. Was a hand-rolled `while i <
    # len(argv)` loop with no `-h`/`--help` handling and a silent no-op on
    # any unrecognized token (a typo'd flag ran at full cost against the
    # wrong root with no error). argparse matches the sibling
    # `workweek-complete-doc-staleness.py` CLI's conventions: auto `-h`/
    # `--help`, and a loud exit-2 error on an unrecognized flag.
    parser = argparse.ArgumentParser(
        prog="doc-content-verify",
        description="Human-facing doc content verification report for the calling repo.",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Repo root to check (default: current working directory).",
    )
    parser.add_argument(
        "--doc",
        dest="docs",
        action="append",
        default=None,
        help="Explicit doc path to verify (repeatable). Overrides the registry/default list.",
    )
    parser.add_argument(
        "--ignore",
        dest="ignore",
        action="append",
        default=None,
        help="Citation token to ignore (repeatable), merged with the registry's doc_verify_ignore.",
    )
    return parser


def main(argv: List[str]) -> int:
    """CLI entrypoint: `doc-content-verify [--root PATH] [--doc PATH ...] [--ignore TOKEN ...]`.

    Prints a JSON payload (`{"findings": [...]}`) to stdout. Always exits 0 —
    this is an advisory/informational gate (see C6c); callers that need a
    hard-block signal inspect the JSON, not the exit code, matching the
    non-blocking PM ruling this plan's ceremony wiring (C6c) records.

    `-h`/`--help` prints usage and exits 0; an unrecognized flag errors
    loudly and exits 2 — matching the sibling `doc-staleness` CLI's argparse
    conventions.
    """
    args = _build_parser().parse_args(argv)
    root = Path(args.root) if args.root else Path.cwd()
    explicit_docs: List[str] = args.docs or []
    explicit_ignore: List[str] = args.ignore or []

    docs = explicit_docs or _read_local_md_list(root, "human_facing_docs") or list(
        _DEFAULT_HUMAN_FACING_DOCS
    )
    ignore = set(explicit_ignore) | set(_read_local_md_list(root, "doc_verify_ignore"))
    sibling_roots = _sibling_roots()

    all_findings: List[Finding] = []
    for doc_rel_path in docs:
        doc_path = _contained_doc_path(root, doc_rel_path)
        if doc_path is None or not doc_path.exists():
            continue
        all_findings.extend(
            verify_doc_on_disk(root, doc_rel_path, sibling_roots=sibling_roots, ignore=ignore)
        )

    payload = {
        "findings": [
            {"doc": f.doc, "line": f.line, "token": f.token, "reason": f.reason}
            for f in all_findings
        ]
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

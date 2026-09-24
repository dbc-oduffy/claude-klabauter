"""coordinator_core.citation_graph — citation extraction and four-class
resolution over a doctrine wiki corpus.

Purpose: `check-citation-integrity` (DoE-claude) is a DoE-only script; DoE's
`coordinator/lib` is not published, so DoE's mirror sources this module from
Claude-klabauter instead. This is the engine-hosted twin of DoE-claude's
`coordinator/lib/citation_graph.py` (`docs/plans/2026-08-30-citation-
integrity-tier-1.md`, chunk C1) — ported near-verbatim per
`docs/plans/2026-09-18-doe-holds-no-scripts.md` chunk W2-C3 (DoE's tree
tracks no executable outside `archive/`; this module and its DoE-only import
path stay, but the source of truth for the shared library moves here).

The doctrine corpus cites a sibling page overwhelmingly by a backticked bare
filename in prose (`` `some-page.md` ``), not a markdown link -- 3,933 such
bare-backtick `.md` citations were measured against 874 resolved markdown
page links (DoE-claude `docs/research/2026-08-30-17h00-llm-wiki-doctrine-
corpus-workdir/own-side-audit.md`). Nothing today can tell a live bare
citation from one whose target was renamed or deleted out from under it
("rename rot") -- this module is the extraction + resolution library, so a
caller can build a ratchet report on top of pure data rather than
re-deriving the regexes.

Extraction grammar -- extension allowlist, decided deliberately, not
inherited by accident. This module allowlists **`.md` only**: every one of
its four resolution classes, its rot ratchet, and its ground-truth sample
are all defined over the doctrine wiki corpus specifically -- a
`.py`/`.yaml`/`.json` backticked reference is a different citation-integrity
question this module was not asked to fold in. Widening the allowlist
without a stated resolution rule for the added extensions would silently
produce unclassifiable citations; narrowing it to what this module actually
resolves is the correct-by-construction choice.

Resolution -- FOUR classes, each resolved by its own function, never one
shared code path (Anti-scope: a pathed reference resolves REPO-ROOT-RELATIVE,
never joined against the citing file's directory -- the prototype's
inherited bug):
  - `resolve_bare_basename` -- a bare basename (`some-page.md`), resolved by
    basename against the stated wiki file set (`WIKI_ROOT`).
  - `resolve_cross_surface` -- folded into `resolve_bare_basename`'s verdict:
    a bare basename absent from the wiki set but present elsewhere in the
    repo is CROSS_SURFACE, not ROT -- a real citation into a different
    tracked surface (a plan, handoff, lesson), out of scope for the wiki rot
    ratchet.
  - `resolve_pathed` -- a pathed reference (`docs/wiki/some-page.md`),
    resolved REPO-ROOT-RELATIVE.
  - `resolve_markdown_link` -- a real `[text](path.md)` markdown link,
    resolved relative to the CITING file's own directory -- the one form for
    which that join is correct.

Negative-spec (Anti-scope): no CLI, no output formatting, no exit codes --
pure functions over a root path returning structured (dataclass) results, so
a caller's tests can assert on data. No ratchet baseline, no storage of a
resolved verdict (a resolution is a fact about OTHER files and goes stale on
any change that doesn't touch the citing file -- recompute per run, never
persist). No per-line cross-line-split or de-extensioned-citation handling.

Spec: DoE-claude `docs/plans/2026-08-30-citation-integrity-tier-1.md`,
chunk C1. Port spec: `docs/plans/2026-09-18-doe-holds-no-scripts.md`,
chunk W2-C3.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

#: This module's own repo root: coordinator_core/citation_graph.py ->
#: coordinator_core/ -> <repo root>.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: claude-klabauter's own doctrine wiki corpus lives at `docs/wiki/` (unlike
#: DoE-claude, where the corpus is nested under `coordinator/docs/wiki/`) --
#: used only as this module's own default; a caller (DoE-claude's
#: check-citation-integrity) always passes its own `wiki_root`/`repo_root`
#: explicitly when scanning its own corpus.
WIKI_ROOT = REPO_ROOT / "docs" / "wiki"

#: The plugin root candidate for a pathed-citation resolution -- kept for
#: parity with the DoE-claude corpus convention this module's callers author
#: against ("Both trees are named 'coordinator-claude'; they are NOT the
#: same tree" -- DoE-claude `CLAUDE.md` § Architecture). A pathed citation in
#: that corpus is authored relative to the PLUGIN root, not the repo root,
#: so a caller resolving that corpus must try the plugin root candidate
#: first, falling back to the repo root, or the overwhelming majority of
#: pathed citations there misresolve as rot.
PLUGIN_ROOT = REPO_ROOT / "coordinator"

#: Ordered candidate roots for `resolve_pathed` -- plugin root first (see
#: `PLUGIN_ROOT`), repo root second. First hit wins.
PATHED_RESOLUTION_ROOTS: "tuple[Path, ...]" = (PLUGIN_ROOT, REPO_ROOT)

#: Deliberate, narrow allowlist -- see module docstring for why `.md` only.
TRACKED_EXTENSIONS = (".md",)

#: Characters that mark a token as a directory-CONVENTION placeholder, not a
#: specific file being cited.
_PLACEHOLDER_CHARS = ("<", ">", "*", "{", "}", "$", "…", "!")

#: A literal ASCII ellipsis (`...`) used the same way as the unicode `…` char
#: above -- "a placeholder/elided span, not a specific file" -- but it is
#: three ordinary characters, not one, so it can't live in `_PLACEHOLDER_CHARS`
#: (which is checked one character at a time) and needs its own substring
#: check.
_PLACEHOLDER_ELLIPSIS = "..."

_FENCE = re.compile(r"^\s*```")
_HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.DOTALL)
_SPEC_BACKLINK_FRONTMATTER_KEY = re.compile(r"^\s*spec_backlink:\s")

#: Structural HTML-comment markers this corpus uses as routing/fence text
#: (`<!-- BEGIN ... -->`, `<!-- consumers: ... -->` etc.) -- a citation-shaped
#: string inside one of these is machinery, not prose asking a reader to
#: resolve a file. Kept narrow and explicit rather than importing a
#: dynamically-loaded set from a hooks-private module.
_STRUCTURAL_MARKERS = (
    re.compile(r"^\s*BEGIN\b"),
    re.compile(r"^\s*END\b"),
    re.compile(r"^\s*consumers:"),
    re.compile(r"^\s*canonical source"),
)

#: A backticked citation ending `.md`, optionally with a trailing `:LINE`
#: line-reference (`writing-plans.md:51`, matching a form seen in the
#: corpus) before the closing backtick.
_BACKTICK_CITATION = re.compile(r"`([^`\n]*?\.md)(?::\d+)?`")

#: A real markdown link `[text](target.md)`, optional `#anchor`.
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n#]+\.md)(?:#[^)\n]*)?\)")

#: A wikilink `[[page-slug]]` -- a fourth citation shape this corpus uses: a
#: bare slug, no `.md` extension, no backticks, resolving to `<slug>.md` in
#: the wiki by the same basename lookup as a bare-backtick citation
#: (`resolve_bare_basename`). Emitted as `kind="bare_basename"` -- the
#: surface grammar differs (no extension, doubled brackets), but it resolves
#: through the identical lookup with no distinguishable status.
#:
#: The slug charset is deliberately narrow -- lowercase-hyphenated-slug
#: characters only (`[A-Za-z0-9_-]`), no spaces/quotes/`$`/`{`/`:` -- because
#: this corpus's bash prose is FULL of unrelated double-bracket syntax that
#: is not a wikilink at all: POSIX character classes (`` `[[:space:]]` ``),
#: bash conditional tests (`` `[[ -z "$VAR" ]]` ``, `` `if [[ "$OSTYPE" ==
#: "darwin"* ]]` ``). Every one of those contains a space, `$`, `"`, or a
#: leading/embedded `:` that a real slug never does; restricting the
#: charset excludes the whole class without an explicit backtick-context
#: check.
#:
#: A slug carrying NO separator (`[[array]]`) is likewise not a wikilink:
#: every page slug in this corpus is multi-word and separated. Requiring a
#: `-` or `_` excludes that class without a backtick-context check, which
#: could not tell it apart from a genuine backticked wikilink anyway.
_WIKILINK = re.compile(r"\[\[([A-Za-z0-9_][A-Za-z0-9_-]*[_-][A-Za-z0-9_-]*)\]\]")


def _normalize_wikilink_slug(slug: str) -> str:
    """Normalize a `[[slug]]` wikilink target before resolution: lowercase,
    `_` -> `-`. This corpus uses TWO wikilink conventions side by side --
    a page's own lowercase-hyphenated filename slug (`[[some-page]]`), and a
    tripwire page's uppercase GREPPABLE_TOKEN (`[[A_COUNTED_FAILURE_IS_NOT_
    A_FAILED_TEST]]`, which names `a-counted-failure-is-not-a-failed-test.md`)
    -- both separators (`_` and `-`) appear in the wild, so normalization is
    unconditional rather than gated on an all-uppercase check: a lowercase
    slug is a no-op under this transform, so applying it always never
    changes an already-correct citation."""
    return slug.lower().replace("_", "-")

#: Shell-command verbs whose presence as the FIRST word of a backticked span
#: means the whole span is invoking a tool, not naming a file: `` `grep -r
#: foo docs/x.md` `` is a command, not a reference, even though it ends
#: `.md`. A real citation is a bare path and never begins with a recognized
#: verb followed by more text, so checking only the first whitespace-split
#: word cannot misclassify a genuinely named file like `git-workflow.md` (no
#: internal space, so it never reaches this check at all -- see
#: `interpret_backtick_span`).
#:
#: Known open risk, not a live bug: `find`, `cat`, `diff`, `wc`, `ls`, `sh`
#: are also ordinary English verbs that could open a prose sentence citing
#: multiple `.md` files (e.g. "find the renamed page in a.md, see also
#: b.md"), which would silently drop the whole span rather than surfacing it
#: as rot. Do not widen or narrow this set without re-running a full-corpus
#: grep first.
_SHELL_COMMAND_VERBS = frozenset(
    {
        "grep", "git", "python", "python3", "sed", "awk", "find", "cat",
        "ls", "tee", "cp", "mv", "rm", "diff", "wc", "dd", "chmod", "bash",
        "sh",
    }
)


#: A single backtick span can name more than one file in prose
#: (`` `-> a.md, b.md` ``) -- each whitespace/comma-separated `.md`-ending
#: run is an independent citation, not one target formed by concatenating
#: both. Excludes `,` and whitespace from the run so a comma-joined or
#: space-joined list splits at its real boundaries instead of swallowing the
#: separator into the first target.
_CONCATENATED_TARGET_RUN = re.compile(r"[^\s,]+\.md")


def interpret_backtick_span(token: str) -> "tuple[str, ...]":
    """The one decision point for what a backticked `.md` span means: `()`
    for a command-shaped span (a tool invocation, one of whose arguments
    happens to end `.md`, not a citation at all), else the independent
    path-shaped targets it names. Two independent checks, not one: the
    command-shaped check only fires on multi-word input (a bare path is
    never whitespace-separated from a leading verb), but the concatenated-
    target split below runs on EVERY input regardless of word count -- a
    single-word, comma-joined, no-whitespace span (`` `bin,lib,x.md,y.md` ``,
    single-word by whitespace-split) still reaches and correctly splits at
    the concatenated-target check."""
    words = token.split(None, 1)
    if len(words) >= 2 and words[0].strip("\"'") in _SHELL_COMMAND_VERBS:
        return ()
    runs = _CONCATENATED_TARGET_RUN.findall(token)
    if len(runs) <= 1:
        return (token,)
    return tuple(runs)


@dataclass(frozen=True)
class Citation:
    """One extracted citation-shaped token."""

    citing_file: Path
    line_no: int
    kind: str  # "bare_basename" | "pathed" | "markdown_link"
    raw_target: str
    excerpt: str
    #: True only for a `[[slug]]` wikilink -- `raw_target` here already
    #: passed through `_normalize_wikilink_slug`'s lossy fold, unlike every
    #: other `bare_basename` citation. `resolve_bare_basename` uses this to
    #: check the fold-collision guard WITHOUT applying it to an exact
    #: backtick citation, which is never folded and stays unambiguous on
    #: its own.
    is_wikilink: bool = False


@dataclass(frozen=True)
class Verdict:
    """One resolution outcome for a single `Citation`."""

    citation: Citation
    status: str  # "live" | "cross_surface" | "rot" | "ambiguous" | "dead_link" | "home_relative"
    matches: "tuple[Path, ...]" = field(default_factory=tuple)
    #: Which candidate root resolved a `pathed` citation (`resolve_pathed`
    #: only) -- e.g. `PLUGIN_ROOT` or `REPO_ROOT` -- so the verdict is
    #: auditable rather than a bare boolean. `None` for every other
    #: resolver, and for `pathed` citations that resolved under no root.
    resolved_root: "Path | None" = None


def _excerpt(line: str) -> str:
    return " ".join(line.split())[:120]


def _is_placeholder(token: str) -> bool:
    """True if `token` is a directory-convention placeholder (trailing
    glob/no filename) or a bare extension mention (a generic `` `.md` ``
    used to talk ABOUT the extension, e.g. "an agent `.md`") -- neither is a
    specific file being cited."""
    if not token or token.endswith("/"):
        return True
    if any(ch in token for ch in _PLACEHOLDER_CHARS):
        return True
    if _PLACEHOLDER_ELLIPSIS in token:
        return True
    basename = token.rsplit("/", 1)[-1]
    stem = basename[: -len(".md")] if basename.endswith(".md") else basename
    if not stem:
        return True
    return False


def _neutralize_structural_comments(text: str) -> str:
    """Blank out (preserving length/newlines) any HTML comment body matching
    `_STRUCTURAL_MARKERS`, so an incidental substring inside a routing/fence
    marker never extracts as a citation."""

    def _replace(match: "re.Match[str]") -> str:
        body = match.group(1)
        if any(marker.search(body.strip()) for marker in _STRUCTURAL_MARKERS):
            return re.sub(r"[^\n]", " ", match.group(0))
        return match.group(0)

    return _HTML_COMMENT.sub(_replace, text)


def extract_citations(text: str, citing_file: Path) -> "list[Citation]":
    """Every citation-shaped token in `text`, in line order.

    Skips fenced code blocks, the frontmatter `spec_backlink:` line, and
    directory-convention placeholders -- the reused false-positive allowlist
    (see module docstring). Pure function over already-loaded text; the
    caller decides what `text`/`citing_file` are (this makes the function
    trivially testable without touching disk)."""
    text = _neutralize_structural_comments(text)
    lines = text.split("\n")
    citations: "list[Citation]" = []
    in_fence = False
    in_frontmatter = False

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()

        if stripped == "---" and line_no <= 2:
            in_frontmatter = True
            continue
        if stripped == "---" and in_frontmatter:
            in_frontmatter = False
            continue

        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if in_frontmatter and _SPEC_BACKLINK_FRONTMATTER_KEY.match(line):
            continue

        for m in _BACKTICK_CITATION.finditer(line):
            token = m.group(1)
            if _is_placeholder(token):
                continue
            # A backticked span immediately followed by `](` is the LABEL of
            # a markdown link, not a second, independent citation --
            # `[\`some-page.md\`](../other/some-page.md)` is one citation,
            # already captured below by `_MARKDOWN_LINK`. Counting the label
            # too double-counts every such link as two citations instead of
            # one, and can classify the label half as rot for a link whose
            # target half resolves live.
            if line[m.end() : m.end() + 2] == "](":
                continue
            for sub_token in interpret_backtick_span(token):
                kind = "pathed" if "/" in sub_token else "bare_basename"
                citations.append(
                    Citation(citing_file, line_no, kind, sub_token, _excerpt(line))
                )

        for m in _MARKDOWN_LINK.finditer(line):
            token = m.group(1)
            if _is_placeholder(token) or token.startswith(("http://", "https://")):
                continue
            citations.append(
                Citation(citing_file, line_no, "markdown_link", token, _excerpt(line))
            )

        for m in _WIKILINK.finditer(line):
            slug = _normalize_wikilink_slug(m.group(1).strip())
            target = f"{slug}.md"
            if _is_placeholder(target):
                continue
            citations.append(
                Citation(
                    citing_file,
                    line_no,
                    "bare_basename",
                    target,
                    _excerpt(line),
                    is_wikilink=True,
                )
            )

    return citations


# ---------------------------------------------------------------------------
# Resolution -- four classes, four functions, never one shared code path.
# ---------------------------------------------------------------------------


#: Namespace prefix for a wikilink fold-collision entry in the dict
#: `load_wiki_index` returns -- kept out of the plain basename keyspace
#: (`\0` can never appear in a filename) so it is visible only to a
#: wikilink-origin lookup (`Citation.is_wikilink`) and never shadows an
#: exact basename key used by a literal backtick citation.
_WIKI_FOLD_COLLISION_PREFIX = "\0fold:"


def load_wiki_index(wiki_root: Path = WIKI_ROOT) -> "dict[str, tuple[Path, ...]]":
    """`basename -> (path, ...)` for every `*.md` file under `wiki_root`.
    A basename with more than one entry is the ambiguous case a caller of
    `resolve_bare_basename` must check for.

    Also records the wikilink fold-collision guard: `_normalize_wikilink_slug`
    unconditionally lowercases and folds `_` -> `-` before a `[[slug]]`
    lookup, which is lossy -- if the corpus ever gains two DISTINCT on-disk
    basenames that fold to the same normalized slug (e.g. `a_b.md` and
    `a-b.md`), a `[[a_b]]` wikilink would otherwise silently resolve to
    whichever raw basename happens to equal the folded string instead of
    surfacing `ambiguous`. Any such collision is recorded under
    `_WIKI_FOLD_COLLISION_PREFIX + <folded name>`, checked by
    `resolve_bare_basename` only for `is_wikilink` citations -- an exact
    backtick citation to one of the colliding basenames is never folded and
    stays unambiguous on its own, so this must not (and does not) touch the
    plain basename keys already in the index."""
    index: "dict[str, list[Path]]" = {}
    for path in sorted(wiki_root.rglob("*.md")):
        index.setdefault(path.name, []).append(path)

    fold_groups: "dict[str, list[str]]" = {}
    for name in index:
        folded_name = _normalize_wikilink_slug(name[: -len(".md")]) + ".md"
        fold_groups.setdefault(folded_name, []).append(name)
    for folded_name, raw_names in fold_groups.items():
        if len(raw_names) > 1:
            merged: "list[Path]" = []
            for raw_name in sorted(raw_names):
                merged.extend(index[raw_name])
            index[_WIKI_FOLD_COLLISION_PREFIX + folded_name] = merged

    return {name: tuple(paths) for name, paths in index.items()}


def load_repo_index(repo_root: Path = REPO_ROOT) -> "dict[str, tuple[Path, ...]]":
    """`basename -> (path, ...)` for every `*.md` file in the whole repo
    (used only for the cross-surface class -- a basename resolving here but
    not in the wiki index is live-elsewhere, not rot)."""
    index: "dict[str, list[Path]]" = {}
    for path in sorted(repo_root.rglob("*.md")):
        if ".git" in path.parts:
            continue
        index.setdefault(path.name, []).append(path)
    return {name: tuple(paths) for name, paths in index.items()}


def resolve_bare_basename(
    citation: Citation,
    wiki_index: "dict[str, tuple[Path, ...]]",
    repo_index: "dict[str, tuple[Path, ...]]",
) -> Verdict:
    """Bare basename resolved against `wiki_index` first. Exactly one match
    there is `"live"`; more than one is `"ambiguous"`; zero there but a match
    in `repo_index` (a plan, handoff, lesson, or other tracked surface) is
    `"cross_surface"` -- live, out-of-scope for the wiki rot ratchet; zero
    anywhere is `"rot"`.

    For a wikilink-origin citation (`Citation.is_wikilink`), the
    fold-collision guard is checked FIRST: if `raw_target` (itself already
    folded) names a normalized key two or more distinct on-disk basenames
    fold to, that is `"ambiguous"` regardless of which one an exact key
    lookup would otherwise hit -- see `load_wiki_index`."""
    name = citation.raw_target
    if citation.is_wikilink:
        fold_matches = wiki_index.get(_WIKI_FOLD_COLLISION_PREFIX + name)
        if fold_matches is not None:
            return Verdict(citation, "ambiguous", fold_matches)
    wiki_matches = wiki_index.get(name, ())
    if len(wiki_matches) == 1:
        return Verdict(citation, "live", wiki_matches)
    if len(wiki_matches) > 1:
        return Verdict(citation, "ambiguous", wiki_matches)
    repo_matches = repo_index.get(name, ())
    if repo_matches:
        return Verdict(citation, "cross_surface", repo_matches)
    return Verdict(citation, "rot", ())


def resolve_pathed(
    citation: Citation, roots: "tuple[Path, ...]" = PATHED_RESOLUTION_ROOTS
) -> Verdict:
    """Pathed reference (`docs/wiki/some-page.md`), resolved against each of
    `roots` in order -- never joined against the citing file's own
    directory. First hit wins; the winning root is recorded on the returned
    `Verdict.resolved_root` so the verdict is auditable. Zero hits across
    every root is `"rot"`. See `PLUGIN_ROOT`/`PATHED_RESOLUTION_ROOTS` for
    why plugin root must be tried before repo root -- the inherited-bug
    correction was about the JOIN (never against the citing file's
    directory), not about which absolute root the join lands on; this fixes
    the latter without reopening the former.

    A `~`-prefixed target (`~/.claude/CLAUDE.md`) is a HOME-relative path,
    not a repo citation at all -- it names a file on the reader's own
    machine, outside this repo's tree entirely, so joining it against any
    repo-rooted candidate can never be meaningful. Classified
    `"home_relative"` rather than `"rot"`: "citation target renamed or
    deleted" and "citation was never a repo path to begin with" are
    different facts, and folding the second into the rot ratchet would both
    inflate the rot count and hide a class no wiki edit can fix."""
    if citation.raw_target.startswith("~"):
        return Verdict(citation, "home_relative", ())
    for root in roots:
        candidate = root / citation.raw_target
        if candidate.is_file():
            return Verdict(citation, "live", (candidate,), resolved_root=root)
    return Verdict(citation, "rot", ())


def resolve_markdown_link(citation: Citation) -> Verdict:
    """Real markdown link, resolved relative to the CITING file's own
    directory -- the one citation form for which that join is correct."""
    candidate = (citation.citing_file.parent / citation.raw_target).resolve()
    if candidate.is_file():
        return Verdict(citation, "live", (candidate,))
    return Verdict(citation, "dead_link", ())


def resolve_citation(
    citation: Citation,
    wiki_index: "dict[str, tuple[Path, ...]]",
    repo_index: "dict[str, tuple[Path, ...]]",
    roots: "tuple[Path, ...]" = PATHED_RESOLUTION_ROOTS,
) -> Verdict:
    """Dispatch a single `Citation` to the resolver matching its `kind`."""
    if citation.kind == "bare_basename":
        return resolve_bare_basename(citation, wiki_index, repo_index)
    if citation.kind == "pathed":
        return resolve_pathed(citation, roots)
    if citation.kind == "markdown_link":
        return resolve_markdown_link(citation)
    raise ValueError(f"unknown citation kind: {citation.kind!r}")


# ---------------------------------------------------------------------------
# Corpus-wide scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusReport:
    """The full measured output of one scan: every extracted citation
    together with its resolved verdict, plus the file count and git SHA the
    numbers are pinned to (state the file count and the git SHA alongside
    every measured figure, so every number is pinned to one tree)."""

    wiki_file_count: int
    git_sha: "str | None"
    verdicts: "tuple[Verdict, ...]"

    def by_status(self, status: str) -> "tuple[Verdict, ...]":
        return tuple(v for v in self.verdicts if v.status == status)

    def counts(self) -> "dict[str, int]":
        out: "dict[str, int]" = {}
        for v in self.verdicts:
            out[v.status] = out.get(v.status, 0) + 1
        return out


def git_head_sha(repo_root: Path = REPO_ROOT) -> "str | None":
    """The repo's current HEAD SHA, or `None` if git is unavailable or the
    tree isn't a git checkout -- fail-open rather than raising, since this is
    metadata pinning a report, not a precondition for resolution itself.

    `git_head_sha` is the sole source of the SHA a `CorpusReport` pins its
    measured figures to -- without it a report cannot be attributed to a
    specific tree state at all.

    Read from `.git` with no process, exactly as
    `engine_version._engine_head_sha` does: `repo_root._walk_for_repo` finds
    the git dir and `git_state.head_sha` follows HEAD's ref hop, falling back
    to `packed-refs`."""
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.git.repo_root import _walk_for_repo

    try:
        found = _walk_for_repo(Path(repo_root).resolve())
        return head_sha(found[1]) if found is not None else None
    except OSError:
        return None


def scan_corpus(
    wiki_root: Path = WIKI_ROOT,
    repo_root: Path = REPO_ROOT,
) -> CorpusReport:
    """Extract and resolve every citation across `wiki_root`'s markdown
    files. Recomputes from scratch on every call (Anti-scope: never resolve
    at write time and persist the verdict -- a verdict is a fact about OTHER
    files and goes stale on any change that doesn't touch the citing file)."""
    wiki_index = load_wiki_index(wiki_root)
    repo_index = load_repo_index(repo_root)
    wiki_files = sorted(wiki_root.rglob("*.md"))

    verdicts: "list[Verdict]" = []
    for path in wiki_files:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue  # wiki file unreadable; skip it, not fatal to the sweep
        roots = (repo_root / "coordinator", repo_root)
        for citation in extract_citations(text, path):
            verdicts.append(resolve_citation(citation, wiki_index, repo_index, roots))

    return CorpusReport(
        wiki_file_count=len(wiki_files),
        git_sha=git_head_sha(repo_root),
        verdicts=tuple(verdicts),
    )


# ---------------------------------------------------------------------------
# Seeded, reproducible sampler -- for a recomputable precision figure, not to
# clear a threshold (a 30-sample draw's ~95% CI is roughly +/-11pp, wide
# enough that it cannot itself decide anything).
# ---------------------------------------------------------------------------


def sample_verdicts(
    verdicts: "Iterable[Verdict]", *, seed: int, sample_size: int
) -> "tuple[Verdict, ...]":
    """A deterministic, seeded sample of `sample_size` verdicts drawn from
    `verdicts` (order-independent -- sorted onto a stable key first so the
    same `seed` reproduces the same draw regardless of input ordering).
    `sample_size` larger than the population returns the whole population,
    unsampled and in stable order (never raises)."""
    population = sorted(
        verdicts,
        key=lambda v: (str(v.citation.citing_file), v.citation.line_no, v.citation.raw_target),
    )
    if sample_size >= len(population):
        return tuple(population)
    rng = random.Random(seed)
    return tuple(rng.sample(population, sample_size))

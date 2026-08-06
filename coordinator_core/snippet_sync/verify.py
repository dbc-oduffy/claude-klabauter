"""coordinator_core.snippet_sync.verify — the shared verify/--fix/--list engine.

CONSOLIDATION of 7 example-doctrine-repo-side `coordinator/bin/verify-<name>-sync.sh` scripts
(~1489 LoC total, ~85-95% byte-identical) into one parameterized engine.
Per-snippet behavioral divergence is DATA read from `snippets/registry.toml`
metadata (`snippet_sync.registry.get_snippet_meta`), not per-script code
branches — the actual point of consolidation.

The snippet set is registry-driven and has GROWN since that consolidation —
13 snippets carry registry.toml entries as of 2026-07-25, not the original 7.
Read `registry.toml` for the live set; the 7 below are the originals only,
retained because the per-dialect notes on them are the worked examples of
each `header_style`:
  reviewer-calibration              — registry, header comment-block
  docs-checker-consumption          — registry, header sentinel-embedded
  plan-coverage-check-consumption   — registry, header sentinel-embedded
  prior-art-check-consumption       — registry, header sentinel-embedded
  quota-self-detect-preamble        — registry, header fixed-2-line-strip-end-sentinel,
                                       allow_insert=True (DR-6 placement; registry-driven,
                                       not the only member — see Insert-when-absent below)
  meta-ask-preamble                 — scan (plugin-root), fence_aware=True,
                                       header fixed-2-line
  text-only-recovery-preamble       — scan (parent-of-plugin-root),
                                       header fixed-2-line

Consumer-source axis (Q13 EM ruling): meta-ask/text-only stay grep-scan
consumer-discovery rather than being force-migrated into the registry's
per-plugin consumer-list model — text-only's SEARCH_ROOT genuinely spans
sibling plugin trees (deep-research pipelines, now folded into coordinator/
per the quota migration note, but the scope itself is real and declared here
as `search_scope`, not silently narrowed to plugin-root). Both scan-based
snippets DO carry registry.toml entries (empty `consumers = []`, since
consumer resolution is scan-driven, not registry-driven) so `list-snippets`
enumerates every registered snippet uniformly, scan-based ones included.

--list semantics (Q15 EM ruling, bugfix): standardized to "canonical
universe" for registry-driven snippets — the FULL registry consumer list,
unconditionally, regardless of on-disk existence or current sentinel
presence (previously only quota-self-detect-preamble did this; the other
4 registry-driven scripts filtered to exists+sentinel-present, silently
hiding drifted/missing consumers from --list). verify/--fix mode's *active*
consumer set is unaffected by this — it still gates on exists+sentinel
(except allow_insert=True snippets, where exists-without-sentinel is
actionable MISSING/INSERT, not silent exclusion).

Insert-when-absent (DR-6, allow_insert=True): a registry consumer that
exists but lacks the BEGIN sentinel is MISSING (verify) / auto-INSERTED
(--fix) rather than silently excluded. `allow_insert` is a per-snippet
registry property (`snippets/registry.toml`), not a fixed single-snippet
special case — as of 2026-07-25 three snippets carry it, each for the same
"fixed, dangerous-if-silently-missing consumer set" rationale: originally
quota-self-detect-preamble (self-detection block for every dispatched
agent), joined 2026-07-20 by guard-encounter-preamble (guard-denial
behavioral companion) and 2026-07-25 by do-not-commit (F3 fail-open safety
finding, RESIDENT-not-injected). Describe this property by reading the
registry's `allow_insert` column, not by restating a count here — the count
will go stale again the next time a snippet enrolls. Placement: immediately
after the first *other* sentinel's END line found in the file; else before a
`## Examples`/`## example` heading or a `done...preamble` BEGIN sentinel;
else EOF.

In-fence shell-comment dialect (in_fence_consumers=True, resolve-claude-klabauter-bin
ONLY): consumer copies that live INSIDE bash code fences cannot carry the
HTML sentinel form (an HTML comment inside a fence is executed text), so
they carry the mechanically-derived shell-comment dialect instead:
`<!-- BEGIN X -->` → `# BEGIN X`, `<!-- END X -->` → `# END X`. When the
flag is set, discovery greps for BOTH dialects; a standalone shell-comment
BEGIN sitting INSIDE an open ``` fence is a live consumer block (one file
may hold MANY such blocks — every one is verified/fixed), while a
shell-comment sentinel OUTSIDE a fence is prose, never a consumer, and the
canonical snippet file itself is excluded from discovery outright (its own
fence QUOTES the sentinel dialect — the exact quoted-mention shape
fence_aware exists to exclude for the HTML form, inverted for this
dialect). The canonical body an in-fence block syncs against is the
interior of the FIRST ``` fence inside the header_style-extracted snippet
body — NOT the whole body, whose prose/fence-markers belong to the
canonical doc, not to a consumer's bash fence. HTML-form consumers outside
fences are unaffected and sync against the full body as today.

Declared-universe completeness (registry schema_version 4): a row may name its
full candidate universe with `eligible_glob`, in which case every glob member
must land in `consumers` or in a reasoned `excluded_consumer` entry. Members in
neither are reported (never auto-repaired — see `_eligible_glob_report`)
alongside the orphan scan, against the same `effective_content_root` every other
tree-walking surface here reads.

Spec backlink: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 6
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.sentinel_blocks import extract_block as _extract_block
from coordinator_core.frontmatter.sentinel_blocks import replace_block as _replace_block
from coordinator_core.session.claims import self_claim as _self_claim
from coordinator_core.snippet_sync import registry as _registry
from coordinator_core.text.normalize_snippet import normalize_snippet

logger = logging.getLogger(__name__)

_VALID_MODES = ("verify", "--fix", "--list", "--check")


@dataclass
class SyncOutcome:
    """Return value of `run()` — mirrors the bash scripts' stdout-lines + exit-code contract."""

    exit_code: int
    lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)


def _sentinel_pair(name: str) -> tuple[str, str]:
    return (
        f"<!-- BEGIN {name} (synced from snippets/{name}.md) -->",
        f"<!-- END {name} -->",
    )


_HTML_COMMENT_RE = re.compile(r"^<!--\s*(.*?)\s*-->$")


def _shell_sentinel_pair(begin_sentinel: str, end_sentinel: str) -> tuple[str, str]:
    """Mechanically derive the shell-comment sentinel dialect from the HTML
    form: `<!-- BEGIN X -->` → `# BEGIN X`, `<!-- END X -->` → `# END X`
    (in_fence_consumers snippets — the registry states only the HTML form)."""
    out: list[str] = []
    for sentinel in (begin_sentinel, end_sentinel):
        m = _HTML_COMMENT_RE.match(sentinel)
        if m is None:  # pragma: no cover — _sentinel_pair always emits HTML form
            raise ValueError(f"not an HTML-comment sentinel: {sentinel!r}")
        out.append(f"# {m.group(1)}")
    return out[0], out[1]


def _is_standalone_sentinel_line(text: str, sentinel: str, *, fence_aware: bool) -> bool:
    """A file is a "consumer" only if the trimmed sentinel is its own line.

    fence_aware=True additionally skips occurrences inside ``` fenced code
    blocks (meta-ask-preamble ONLY — "a fenced occurrence is a documentation
    example, not a live consumer").
    """
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if fence_aware and stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if fence_aware and in_fence:
            continue
        if stripped == sentinel:
            return True
    return False


def _has_in_fence_shell_block(text: str, shell_begin: str) -> bool:
    """A file consumes the shell-comment dialect iff a standalone trimmed
    `shell_begin` line sits INSIDE an open ``` fence (same simple fence
    toggle as `_is_standalone_sentinel_line`). Outside a fence, the shell
    form is prose/quoted mention, never a consumer.
    """
    in_fence = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
        elif in_fence and stripped == shell_begin:
            return True
    return False


def _extract_fence_body(body: str) -> Optional[str]:
    """Interior of the FIRST ``` fenced code block in the canonical snippet
    body — the text an in-fence consumer block syncs against (the prose and
    fence markers around it belong to the canonical doc, not to a consumer's
    bash fence). Returns None if the body holds no closed fence.
    """
    lines = body.split("\n")
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            if start is None:
                start = i + 1
            else:
                return "\n".join(lines[start:i])
    return None


def _sync_in_fence_blocks(
    text: str, shell_begin: str, shell_end: str, body_norm: str, body: str, *, fix: bool
) -> tuple[str, dict[str, int]]:
    """Verify (and in fix mode rewrite) EVERY shell-comment sentinel block
    sitting inside a ``` fence — one consumer file may hold many such blocks.

    A block's END sentinel must appear before its own fence closes; a fence
    that closes (or a file that ends) first counts as missing_end and the
    block is left untouched — position-aware by construction, so a stray END
    in a LATER fence can never be mistaken for this block's closer (the same
    wrong-occurrence class the HTML path's `_fence_aware_slice` gate fixes).

    Indent-aware: in-fence copies sit at their fence's own indentation (e.g.
    nested under a markdown list — real consumer state on disk, not drift).
    The BEGIN sentinel line's leading whitespace is the block's indent;
    comparison strips it from each body line first, and a fix re-applies it
    to every non-blank canonical line so the rewrite preserves the context.

    Returns (updated_text, counts) with counts keys ok/mismatched/fixed/
    missing_end. updated_text == text unless fix rewrote at least one block.
    """
    lines = text.split("\n")
    out: list[str] = []
    counts = {"ok": 0, "mismatched": 0, "fixed": 0, "missing_end": 0}
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not (in_fence and stripped == shell_begin):
            out.append(line)
            i += 1
            continue

        end_idx: Optional[int] = None
        j = i + 1
        while j < len(lines):
            inner = lines[j].strip()
            if inner.startswith("```"):
                break
            if inner == shell_end:
                end_idx = j
                break
            j += 1

        if end_idx is None:
            counts["missing_end"] += 1
            out.append(line)
            i += 1
            continue

        indent = line[: len(line) - len(line.lstrip())]
        block = "\n".join(
            bl[len(indent):] if indent and bl.startswith(indent) else bl
            for bl in lines[i + 1 : end_idx]
        )
        if normalize_snippet(block) == body_norm:
            counts["ok"] += 1
            out.extend(lines[i : end_idx + 1])
        elif fix:
            counts["fixed"] += 1
            out.append(line)
            out.extend(indent + bl if bl.strip() else bl for bl in body.split("\n"))
            out.append(lines[end_idx])
        else:
            counts["mismatched"] += 1
            out.extend(lines[i : end_idx + 1])
        i = end_idx + 1

    return "\n".join(out), counts


def _strip_leading_html_comments(lines: list[str], *, snippet_name: str = "<snippet>") -> list[str]:
    """Consume every LEADING HTML comment block from `lines`, comment-aware
    rather than line-aware (Defect A fix — the prior implementation only
    dropped the single line a comment *started* on, leaking multi-line
    comment continuations and the dangling `-->` into the body).

    header_style == "comment-block" ONLY (do-not-commit's variable-length
    header — the current snippet whose leading comment spans multiple
    lines; used to be single-line-only for calibration's header).

    Handling, one named helper, no inline state machine at the call site:
      - single-line `<!-- x -->`: strips the whole line (unchanged from
        the prior behaviour).
      - a comment spanning N lines (`<!-- x\\n  y\\n  z -->`): every line
        of the block is consumed, not just the first.
      - MULTIPLE consecutive leading blocks, blank-line separated: every
        block before the first real body line is consumed.
      - a closing `-->` followed by content on the SAME line: that
        trailing content is body, not comment — it is kept (on its own
        output line), and leading-comment consumption stops there (body
        has started).
      - an UNTERMINATED leading `<!--` (no closing `-->` anywhere in the
        rest of the file): fails loud with `ValueError` naming the
        snippet and the offending line, rather than silently swallowing
        the entire body into nothing — a hard failure someone can act on
        beats a body that silently loses all its content.
      - a `<!--` occurring AFTER body content has already started is
        NEVER treated as a leading comment — the skip state is one-way.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    skipping = True
    while i < n:
        line = lines[i]
        stripped = line.lstrip()
        if skipping and stripped == "":
            i += 1
            continue
        if skipping and stripped.startswith("<!--"):
            close_idx = stripped.find("-->")
            if close_idx != -1:
                trailing = stripped[close_idx + 3 :]
                if trailing.strip() != "":
                    out.append(trailing)
                    skipping = False
                i += 1
                continue
            j = i + 1
            closed = False
            while j < n:
                idx = lines[j].find("-->")
                if idx != -1:
                    trailing = lines[j][idx + 3 :]
                    if trailing.strip() != "":
                        out.append(trailing)
                        skipping = False
                    closed = True
                    break
                j += 1
            if not closed:
                raise ValueError(
                    f"snippet {snippet_name!r}: unterminated leading HTML comment "
                    f"(no closing '-->' found for the '<!--' opened at header line "
                    f"{i + 1}) — refusing to silently swallow the snippet body."
                )
            i = j + 1
            continue
        skipping = False
        out.append(line)
        i += 1
    return out


class SnippetCommentResidueError(ValueError):
    """Raised when an extracted snippet body still contains a residual raw
    HTML-comment fragment (Defect C companion assertion — see
    `_assert_no_residual_comment_fragments`)."""


def _assert_no_residual_comment_fragments(body: str, snippet_name: str) -> None:
    """Fail loud if the header-EXTRACTED canonical body still contains a
    residual raw HTML-comment fragment: a bare `-->` with no matching
    `<!--`, or an `<!--` with no matching `-->` before EOF.

    Defect C: the sync verifier's contract is byte-equality against the
    canonical source, so it reports OK both before and after a malformed
    header leaks into the body — it faithfully syncs whatever the
    canonical body IS, and a consistency check cannot detect a defect
    that lives in the very thing it checks against. This assertion
    instead tests a property of the extracted body itself, independent of
    the header_style dialect that produced it, so it covers every
    snippet — not just comment-block ones — and would have caught the
    2026-07-25 example-doctrine-repo incident (three orphaned prose lines + a dangling
    `-->` leaked into five consumers) at extraction time, before any
    consumer file was ever touched.

    Well-formed `<!-- ... -->` comments legitimately present IN the body
    (e.g. a documented HTML-comment example) are not flagged — only a
    genuinely dangling opener or closer is an error.
    """
    depth = 0
    for lineno, line in enumerate(body.split("\n"), start=1):
        pos = 0
        while True:
            open_idx = line.find("<!--", pos)
            close_idx = line.find("-->", pos)
            if open_idx == -1 and close_idx == -1:
                break
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                if depth == 0:
                    raise SnippetCommentResidueError(
                        f"snippet {snippet_name!r}: residual HTML-comment fragment — "
                        f"bare '-->' with no matching '<!--' at extracted-body line "
                        f"{lineno}: {line!r}"
                    )
                depth -= 1
                pos = close_idx + 3
            else:
                depth += 1
                pos = open_idx + 4
    if depth > 0:
        raise SnippetCommentResidueError(
            f"snippet {snippet_name!r}: residual HTML-comment fragment — an unclosed "
            f"'<!--' remains open in the extracted body (missing matching '-->')."
        )


def _extract_snippet_body(
    snippet_text: str,
    header_style: str,
    begin_sentinel: str,
    end_sentinel: str,
    *,
    snippet_name: str = "<snippet>",
) -> str:
    """Header-shape-aware extraction of the canonical body from the snippet source file.

    The 4 dialects (recipe item 5, taxonomy confirmed against all 7 snippet
    files at build time — docs-checker/plan-coverage/prior-art slot into
    "sentinel-embedded", not a 5th dialect):
      comment-block                      — calibration
      fixed-2-line                       — meta-ask, text-only
      fixed-2-line-strip-end-sentinel    — quota
      sentinel-embedded                  — docs-checker, plan-coverage, prior-art
    """
    lines = snippet_text.split("\n")

    if header_style == "comment-block":
        out = _strip_leading_html_comments(lines, snippet_name=snippet_name)
        return "\n".join(out)

    if header_style == "fixed-2-line":
        return "\n".join(lines[2:])

    if header_style == "fixed-2-line-strip-end-sentinel":
        return "\n".join(line for line in lines[2:] if line != end_sentinel)

    if header_style == "sentinel-embedded":
        result = _extract_block(snippet_text, begin_sentinel, end_sentinel)
        return result.block if result is not None else ""

    raise ValueError(f"unknown header_style: {header_style!r}")  # pragma: no cover — registry validates


def _grep_scan(search_root: Path, sentinels: list[str]) -> list[str]:
    """`grep -rlF -e <sentinel>... <search_root>` — literal-substring file-list scan.

    Shelled out (not re-walked in Python) so consumer-discovery ordering and
    symlink/binary-file handling stay byte-identical to the retired bash
    scripts' `grep -rlF` — this is a cold, non-hot-path CLI, not the
    constraint-7 hot-path case the migration targets. Multiple sentinels
    (in_fence_consumers snippets scan for both dialects) OR together.
    """
    cmd = ["grep", "-rlF"]
    for sentinel in sentinels:
        cmd += ["-e", sentinel]
    cmd.append(str(search_root))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Review: code-reviewer — grep-absent/grep-failed-to-run must not be
        # conflated with "grep ran and found zero matches" (both used to
        # return [] here, so a machine without grep on PATH silently and
        # permanently reported "no consumers detected" instead of surfacing
        # that verification never actually executed). Fail loud instead.
        raise RuntimeError(
            f"snippet-sync: could not invoke grep to scan {search_root} for consumers "
            f"({type(exc).__name__}: {exc}). Verification did not run — this is not "
            f"the same as 'zero consumers found'."
        ) from exc
    if not result.stdout:
        return []
    # grep always emits forward-slash paths (even the MSYS/git-bash grep this
    # shells out to on Windows) — these are filesystem paths callers reopen via
    # Path/os.path APIs and compare against native os.sep-built strings, not
    # wire ids, so normalize to the host's native separator here.
    return [os.path.normpath(line) for line in result.stdout.split("\n") if line]


# ---------------------------------------------------------------------------
# ORPHAN-SENTINEL DETECTION (2026-07-25 example-doctrine-repo incident, memo
# cross-repo/archive/2026-07-25-example-doctrine-repo-em-orient-assemble-phantom-verbs.md § P1)
#
# `parallel-review-synthesizer.md` carried `reviewer-calibration` sentinels while
# appearing in neither `consumers` nor `contract_blocks`. It was not merely stale:
# the pasted block said "< 5 ... Do not surface inline" forty lines below that
# agent's own hard rule "Never suppress findings" — a shipping agent prompt holding
# two contradictory instructions about suppression.
#
# Four design constraints, all from the incident, all load-bearing:
#
#   1. PREFIX-match, not exact-match. The orphan's BEGIN sentinel lacked the
#      registered `(synced from ...)` clause, so the exact-match grep every other
#      path in this module uses walked straight past it. That is PRECISELY why it
#      survived — an orphan is likelier than a live consumer to carry a drifted
#      sentinel, because nothing has been rewriting it.
#   2. FAIL LOUD; never auto-delete on `--fix`. The orphan's content was authored
#      on purpose even though it was wrong. Name both exits and let a human choose
#      (~/.claude/CLAUDE.md § design-as-offers).
#   3. Flag ADJACENT DEPENDENT PROSE, not just the fenced block. In the sibling
#      finding, a hand-authored paragraph OUTSIDE the sentinels referenced
#      terminology defined only INSIDE them; deleting the block alone would leave
#      that dangling and recreate the defect in a new costume.
#   4. `consumer_source = "scan"` snippets are EXEMPT BY CONSTRUCTION —
#      sentinel-present IS enrolment there, so an orphan cannot exist by definition.
#
# NEGATIVE SPEC: this scan never mutates. `--fix` deliberately has no orphan
# branch that writes; if you are adding one, re-read constraint 2 first.
# ---------------------------------------------------------------------------

#: Terminology-introducing forms in these docs: a backticked code-span or a
#: **bolded** term. Deliberately NOT "any capitalized word" or a similarity
#: score — constraint 3 needs a mechanical, explainable rule a human can audit
#: from the report line, not a heuristic that argues with the reader.
_CODE_SPAN_RE = re.compile(r"`([^`\n]{3,})`")
_BOLD_TERM_RE = re.compile(r"\*\*([^*\n]{3,})\*\*")


def _orphan_begin_re(name: str) -> "re.Pattern[str]":
    """Prefix-matching BEGIN-sentinel recognizer for `name` (constraint 1), in
    BOTH dialects — the HTML comment form and the shell-comment form.

    Matches `<!-- BEGIN <name> -->`, `<!-- BEGIN <name> (synced from ...) -->`,
    `# BEGIN <name>`, and any other trailing clause — but NOT
    `<!-- BEGIN <name>-other ... -->`, because a bare `startswith` on the name
    would swallow every sibling snippet whose name extends this one. The
    boundary after the name must be whitespace or the comment terminator.
    """
    return re.compile(
        r"^(?:<!--\s*|#\s*)BEGIN\s+" + re.escape(name) + r"(?:\s|-->|$)"
    )


#: Generic (name-agnostic) BEGIN-sentinel recognizer, both dialects. Used to
#: build the shared per-tree index in ONE walk — see `_orphan_index`.
_ANY_BEGIN_RE = re.compile(r"^(?:<!--\s*|#\s*)BEGIN\s+(\S+)")

#: Directories never worth walking for sentinels. `.git` alone is the bulk of a
#: tree's file count and can hold sentinel-shaped bytes in packed objects.
_ORPHAN_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".mypy_cache"})

#: Files above this size are not hand-authored prose carrying a pasted snippet;
#: skipping them bounds the walk against a stray large artifact in-tree.
_ORPHAN_MAX_BYTES = 2 * 1024 * 1024

#: Per-`search_root` cache of the walk (Finding 5 — `run()` is called once per
#: registry snippet, and every call used to re-walk and re-read the whole tree).
#: Keyed by the resolved root; a process is short-lived and single-tree in every
#: real invocation shape, so staleness within one process is not a live concern.
_ORPHAN_INDEX_CACHE: dict[str, dict[str, list[tuple[str, int, str]]]] = {}


def _orphan_index(search_root: Path) -> dict[str, list[tuple[str, int, str]]]:
    """Map snippet-name -> [(file, 1-based line, sentinel line text), ...] for
    every BEGIN sentinel of any name, anywhere under `search_root`.

    ONE walk serves every snippet (Finding 5). Reading each file once and
    bucketing by the name captured from the sentinel itself is what makes the
    file-type widening below affordable: the previous shape re-walked and
    re-read the tree per snippet, so scanning more than `*.md` would have
    multiplied an already-repeated cost by the whole tree.

    File-type scope is now "every decodable text file under the root, minus
    `_ORPHAN_SKIP_DIRS`", not `*.md` (Finding 1). The markdown-only scope was
    an assumption about consumer CONVENTION, not a property the scanner could
    enforce: `registry.py::resolve_consumers` puts no extension constraint on a
    declared consumer path, and the shell-comment dialect exists precisely to
    live in non-markdown files. A `.sh`/`.py` file someone mis-pastes a
    sentinel into directly is exactly the failure orphan-detection exists to
    catch, and it was invisible. Undecodable (binary) files are skipped by the
    `UnicodeDecodeError` guard rather than by an extension allowlist, so the
    scope needs no maintenance as new file types appear.
    """
    key = os.path.normpath(str(search_root))
    cached = _ORPHAN_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    index: dict[str, list[tuple[str, int, str]]] = {}
    for path in sorted(search_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _ORPHAN_SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > _ORPHAN_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "BEGIN" not in text:  # cheap reject before the per-line regex walk
            continue
        norm = os.path.normpath(str(path))
        for i, line in enumerate(text.split("\n")):
            stripped = line.strip()
            m = _ANY_BEGIN_RE.match(stripped)
            if m is None:
                continue
            index.setdefault(m.group(1), []).append((norm, i + 1, stripped))

    _ORPHAN_INDEX_CACHE[key] = index
    return index


def _terms_defined_in(block_text: str) -> set[str]:
    """Backticked/bolded terms introduced inside `block_text`."""
    terms: set[str] = set()
    for pattern in (_CODE_SPAN_RE, _BOLD_TERM_RE):
        for m in pattern.finditer(block_text):
            term = m.group(1).strip()
            if term:
                terms.add(term)
    return terms


def _dependent_prose_lines(
    lines: list[str], block_start: int, block_end: int, terms: set[str]
) -> list[tuple[int, str]]:
    """Lines OUTSIDE [block_start, block_end] that reference a term introduced
    INSIDE it (constraint 3).

    NEGATIVE SPEC — do not re-add a "but only if the document doesn't define the
    term itself" filter. That was this function's first implementation and it
    silently defeated constraint 3 on the very incident that motivated it: prose
    REFERENCES are backticked exactly like definitions (``When applying the
    `severity floor`, ...``), so the reference cancelled itself out and the scan
    reported zero dependent lines on a file that had one. There is no textual
    signal distinguishing a markdown definition from a markdown reference, so
    the filter cannot be made to work — it can only appear to.

    Erring toward false positives is the correct trade here and is bounded: this
    only ever runs on a file ALREADY confirmed to carry an orphan, the output is
    advisory context a human reads before choosing exit (A) or (B), and a
    spurious line costs a glance. A missed line costs the dangling-prose defect
    the constraint exists to prevent.

    That trade does NOT extend to sloppy lexical matching, which is a separate
    axis: matching is word-boundary-anchored, so a 3-character term like `add`
    or `cat` does not fire on `address`/`category`. Tightening what "references"
    means lexically costs no recall against the definition-vs-reference problem
    above — the two are independent, and conflating them is how the noise
    argument gets used to excuse a match rule nobody would defend on its own.
    """
    if not terms:
        return []
    outside_idx = [i for i in range(len(lines)) if i < block_start or i > block_end]
    matchers = [
        # A term containing whitespace/punctuation is matched literally — \b is
        # meaningless against a phrase whose edges may not be word characters.
        re.compile(r"\b" + re.escape(term) + r"\b") if term.isidentifier() or term.isalnum()
        else re.compile(re.escape(term))
        for term in terms
    ]
    hits: list[tuple[int, str]] = []
    for i in outside_idx:
        line = lines[i]
        if any(m.search(line) for m in matchers):
            hits.append((i + 1, line.strip()))
    return hits


@dataclass
class OrphanFinding:
    """One file carrying a BEGIN sentinel for a snippet it is not enrolled in."""

    path: str
    line_no: int
    sentinel_line: str
    drifted: bool  # BEGIN line is not byte-equal to the registered sentinel
    dependent_prose: list[tuple[int, str]] = field(default_factory=list)


def _scan_orphans(
    search_root: Path,
    name: str,
    begin_sentinel: str,
    end_sentinel: str,
    registered: set[str],
) -> list[OrphanFinding]:
    """Occurrences under `search_root` of a BEGIN sentinel for `name` in a file
    absent from `registered` (the resolved consumer set plus the canonical
    snippet and the registry itself).

    Reads its candidates from the shared `_orphan_index` — one walk per tree,
    not one per snippet — then re-reads only the files that actually hold a
    finding, to compute block extent and dependent prose.

    NEGATIVE SPEC — do not reintroduce a `break` after the first hit in a file.
    That was the original shape ("the first orphan is the report anchor") and it
    silently truncated: a file accumulating several stale pasted copies reported
    one, with no signal the others existed, so a human who resolved the named
    block and re-ran got a fresh finding they had no way to anticipate. Silent
    caps are forbidden (~/.claude/CLAUDE.md § No silent caps); every occurrence
    is reported.

    CALLER CONTRACT: `consumer_source = "scan"` snippets must never reach here
    (constraint 4 — sentinel-present IS enrolment there, so an orphan cannot
    exist by construction). That gate lives in `run()`; this function has no
    `consumer_source` awareness of its own and would happily report against a
    scan-driven snippet if called directly.
    """
    index = _orphan_index(search_root)
    begin_re = _orphan_begin_re(name)
    by_file: dict[str, list[tuple[int, str]]] = {}
    for norm, line_no, sentinel_line in index.get(name, []):
        if norm in registered:
            continue
        # The index keys on the name token alone; re-test the full line so the
        # boundary rule (constraint 1) governs, not just the captured token.
        if not begin_re.match(sentinel_line):
            continue
        by_file.setdefault(norm, []).append((line_no, sentinel_line))

    findings: list[OrphanFinding] = []
    for norm in sorted(by_file):
        try:
            lines = Path(norm).read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):  # pragma: no cover — indexed, so readable
            continue
        for line_no, sentinel_line in by_file[norm]:
            i = line_no - 1
            end_idx = next(
                (j for j in range(i + 1, len(lines)) if lines[j].strip() == end_sentinel),
                len(lines) - 1,
            )
            block_text = "\n".join(lines[i : end_idx + 1])
            findings.append(
                OrphanFinding(
                    path=norm,
                    line_no=line_no,
                    sentinel_line=sentinel_line,
                    drifted=sentinel_line != begin_sentinel,
                    dependent_prose=_dependent_prose_lines(
                        lines, i, end_idx, _terms_defined_in(block_text)
                    ),
                )
            )
    return findings


def _orphan_report(
    findings: list[OrphanFinding], name: str, *, is_fix: bool, delivery: str
) -> list[str]:
    """Human-facing orphan report — names BOTH exits and picks neither
    (constraint 2 / design-as-offers). Never emitted as a silent warning: the
    caller sets a non-zero exit alongside it.

    `delivery` changes exit (A)'s CONTENT, not just its wording. On a `paste`
    row, "add it to `consumers`" is the fix. On an `inject` row that same advice
    is actively WRONG: `consumers` is a logical/documentation set there and
    nothing pastes from it, so following it would leave the block orphaned AND
    silence the check that said so. Offering the wrong repair confidently is
    worse than offering none, which is why this branches rather than emitting
    one generic line.
    """
    is_inject = delivery == "inject"
    n_files = len({f.path for f in findings})
    out = [
        f"ORPHAN       {len(findings)} block(s) across {n_files} file(s) carry a '{name}' "
        f"BEGIN sentinel but are not registered consumers of it:",
    ]
    if is_inject:
        out.append(
            f"             '{name}' is delivery=\"inject\" — assembled into the dispatched"
        )
        out.append(
            "             child prompt via contract_blocks: and pasted by NOTHING, so any"
        )
        out.append(
            "             pasted sentinel is an orphan by construction (declared"
        )
        out.append(
            "             conditional_consumer paths excepted, and already excluded above)."
        )
    for f in findings:
        drift = "  [DRIFTED sentinel — exact-match scans miss this]" if f.drifted else ""
        out.append(f"  {f.path}:{f.line_no}{drift}")
        out.append(f"    {f.sentinel_line}")
        for line_no, text in f.dependent_prose:
            snippet = text if len(text) <= 100 else text[:100] + "..."
            out.append(f"    ^ dependent prose OUTSIDE the block at line {line_no}: {snippet}")
    out.append("")
    out.append("This is NOT auto-repairable — the two exits are genuinely different and")
    out.append("only a human can pick. Nothing has been modified.")
    out.append("")
    if is_inject:
        out.append("  (A) The agent genuinely needs this block → wire it as an INJECTION,")
        out.append(f"      not a paste: add the subagent_type to contract_blocks: for '{name}'")
        out.append("      in subagent-sandbox-policy.yaml (example-doctrine-repo-owned), then delete the pasted")
        out.append(f"      copy. Adding it to [snippet.{name}].consumers is NOT the fix — that")
        out.append("      list is a documentation set on an inject row and nothing pastes from")
        out.append("      it, so the block would stay orphaned with the warning silenced.")
        out.append("      If this file is a genuine paste target on purpose, it needs a")
        out.append("      conditional_consumer entry instead.")
    else:
        out.append(f"  (A) The block belongs there → add the file to [snippet.{name}].consumers")
        out.append("      in snippets/registry.toml, then re-run --fix to bring it into sync.")
    out.append("  (B) The block does NOT belong there → delete it BY HAND, and read any")
    out.append("      'dependent prose' lines above first: they reference terminology")
    out.append("      defined only inside the block and will dangle once it is gone.")
    if is_fix:
        out.append("")
        out.append("--fix deliberately does not delete orphaned blocks. The 2026-07-25")
        out.append("incident's orphan was authored on purpose and was wrong — auto-deletion")
        out.append("would have destroyed the evidence of both facts.")
    return out


def _eligible_glob_report(
    gaps: list[str], name: str, pattern: str, *, delivery: Optional[str]
) -> list[str]:
    """Human-facing report for `eligible_glob` members declared nowhere
    (schema_version 4).

    An `eligible_glob` names the row's FULL candidate universe, so a member in
    neither `consumers` nor `excluded_consumer` is an UNDECLARED absence — the
    zero-information state the field pair exists to remove. Without the glob, a
    newly-added file in the same directory is invisible to both lists alike and
    nothing can ever notice it; with it, this is exactly the defect the check is
    for.

    Not auto-repairable, for the same reason `_orphan_report` isn't: the two
    outcomes ("it should carry the block" / "it deliberately should not") are
    genuinely different and only a human knows which. Nothing is mutated, on
    --fix included.

    `delivery` changes exit (A)'s CONTENT, not its wording (DR-240 § Decision 4,
    the same rule `_orphan_report` and the zero-consumer message follow). On an
    inject row, "add it to `consumers`" is wrong advice — that list is a
    documentation set nothing pastes from, so enrolment there is a
    `contract_blocks:` edit.
    """
    out = [
        f"UNDECLARED   {len(gaps)} file(s) match [snippet.{name}].eligible_glob "
        f"'{pattern}' but appear in NEITHER 'consumers' NOR 'excluded_consumer':",
    ]
    out.extend(f"  {rel}" for rel in gaps)
    out.append("")
    out.append("An eligible_glob declares this row's FULL candidate universe, so a member")
    out.append("named in neither list is an UNDECLARED absence — nobody has decided about")
    out.append("it either way. Nothing has been modified; declare each file:")
    out.append("")
    if delivery == "inject":
        out.append(f"  (A) It should carry '{name}' → wire it as an INJECTION: add its")
        out.append("      subagent_type to contract_blocks: in subagent-sandbox-policy.yaml")
        out.append(f"      (example-doctrine-repo-owned) and mirror it in [snippet.{name}].consumers, which on an")
        out.append("      inject row is the logical/documentation set, not a paste-target list.")
    else:
        out.append(f"  (A) It should carry '{name}' → add it to [snippet.{name}].consumers in")
        out.append("      snippets/registry.toml, then re-run --fix to bring it into sync.")
    out.append(f"  (B) It deliberately should not → add an [[snippet.{name}.excluded_consumer]]")
    out.append("      entry with a non-empty `reason` saying why. An exclusion with no reason")
    out.append("      is rejected: it relocates the zero-information absence rather than")
    out.append("      removing it.")
    return out


def _find_standalone_offset(text: str, sentinel: str, *, fence_aware: bool) -> Optional[int]:
    """Char offset of the start of the line holding the first standalone
    occurrence of `sentinel` (mirrors `_is_standalone_sentinel_line`'s
    fence-skip logic exactly, so the same notion of "standalone" governs
    both consumer-discovery and which occurrence extraction/replacement
    touches). Returns None if no such occurrence exists.
    """
    in_fence = False
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if fence_aware and stripped.startswith("```"):
            in_fence = not in_fence
        elif not (fence_aware and in_fence) and stripped == sentinel:
            return offset
        offset += len(line) + 1
    return None


def _fence_aware_slice(
    text: str, begin: str, *, fence_aware: bool
) -> Optional[tuple[str, str]]:
    """Split `text` into (prefix, rest) where `rest` starts at the line of
    the first STANDALONE (fence-skipped, if fence_aware) BEGIN occurrence.

    Review: code-reviewer — `sentinel_blocks._find_markers` (which both
    `_extract_block` and `_replace_block` delegate to) has zero fence
    awareness: it takes the first LITERAL occurrence of `begin` anywhere in
    the file. For fence_aware=True snippets (meta-ask-preamble), a fenced
    documentation-example occurrence positioned before the real block would
    get extracted/rewritten instead of the genuine one. Slicing off
    everything before the standalone occurrence before handing the
    remainder to the marker-position-based extractor/replacer keeps those
    functions unaware of fences while still landing on the right block.
    fence_aware=False snippets get prefix="" (a no-op slice) — byte-identical
    to the pre-fix behavior.
    """
    if not fence_aware:
        return "", text
    offset = _find_standalone_offset(text, begin, fence_aware=True)
    if offset is None:
        return None
    return text[:offset], text[offset:]


def _rewrite_block(
    path: Path, begin: str, end: str, body: str, *, fence_aware: bool = False
) -> bool:
    """Replace the sentinel block content in-place. Byte-parity port of the
    Python heredoc inlined in all 7 original bash scripts (fence-aware
    slicing added on top — see `_fence_aware_slice`).

    Returns True if the block was actually rewritten, False if markers were
    absent (or vanished since the caller's own gate ran) — the caller MUST
    NOT report "FIXED" on a False return.
    """
    content = path.read_text(encoding="utf-8")
    sliced = _fence_aware_slice(content, begin, fence_aware=fence_aware)
    if sliced is None:
        return False
    prefix, rest = sliced
    updated_rest = _replace_block(rest, begin, end, body)
    if updated_rest is None:
        return False  # markers absent — caller already gated on MISSING_END/has_begin
    path.write_text(prefix + updated_rest, encoding="utf-8")
    return True


_END_SENTINEL_PATTERN_TMPL = r"^\s*<!--\s*END\s+(?!{name}\b)"
_EXAMPLES_HEADING = re.compile(r"^\s*##\s+(Examples?|example)", re.IGNORECASE)
_DONE_PREAMBLE = re.compile(r"<!--\s*BEGIN.*done.*preamble", re.IGNORECASE)


def _insert_block(path: Path, name: str, begin: str, end: str, body: str) -> None:
    """DR-6 placement: insert immediately after the first OTHER sentinel's END
    line found in the file; else before an Examples heading or a
    done-preamble BEGIN sentinel; else EOF. Byte-parity port of
    verify-quota-self-detect-sync.sh::insert_block
    (example-doctrine-repo 432e3285, 2026-07-22)."""
    block_body = body if body.endswith("\n") else body + "\n"
    block = begin + "\n" + block_body + end + "\n"

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    end_pattern = re.compile(_END_SENTINEL_PATTERN_TMPL.format(name=re.escape(name)))

    insert_after = -1
    for i, line in enumerate(lines):
        if end_pattern.match(line):
            insert_after = i
            break

    if insert_after >= 0:
        out = lines[: insert_after + 1] + [block] + lines[insert_after + 1 :]
    else:
        insert_before = len(lines)
        for i, line in enumerate(lines):
            if _EXAMPLES_HEADING.match(line) or _DONE_PREAMBLE.match(line):
                insert_before = i
                break
        out = lines[:insert_before] + [block] + lines[insert_before:]

    path.write_text("".join(out), encoding="utf-8")


def _claim_if_session(path: str, *, cs_lib: Optional[Path] = None) -> None:
    """Best-effort touch-tracking via the native `cs_self_claim` port.

    Calls `coordinator_core.session.claims.self_claim` in-process (2026-07-21
    de-bash cutover — see git log; was previously a `bash -c 'source ...;
    cs_self_claim'` subprocess shim to the example-doctrine-repo-side coordinator-session.sh).
    `cs_lib` is accepted-but-unused for call-site compatibility — the native
    port needs no lib file to source. Best-effort: `self_claim` itself is
    fail-open (always returns True; internal errors are swallowed), matching
    the original scripts' silent-no-op-on-failure contract exactly.
    """
    del cs_lib  # unused: native port needs no lib file to source
    try:
        _self_claim(path)
    except ValueError as exc:
        # self_claim only raises on an empty path (its own contract otherwise
        # always returns True) — debug-log so a caller passing "" is
        # discoverable without breaking the best-effort no-op contract.
        logger.debug("snippet-sync: self_claim(%r) skipped: %s", path, exc)


def run(
    name: str,
    mode: str,
    *,
    plugin_root: Path,
    content_root: Optional[Path] = None,
    machine_local_bin: Optional[str] = None,
    cs_lib: Optional[Path] = None,
    registry_data: Optional[dict] = None,
) -> SyncOutcome:
    """Verify/--fix/--list a single snippet's sentinel-sync state across its consumers.

    `--check` is a synonym for `verify` (quota-self-detect-preamble's original
    alias, standardized across all 7 per the recipe's item-1 ruling).
    """
    if mode not in _VALID_MODES:
        return SyncOutcome(exit_code=2, stderr_lines=[f"ERROR: unknown argument '{mode}'"])
    effective_mode = "verify" if mode == "--check" else mode

    registry_path = plugin_root / "snippets" / "registry.toml"
    try:
        data = registry_data if registry_data is not None else _registry.load_registry(registry_path)
        meta = _registry.get_snippet_meta(data, name)
    except _registry.RegistryError as exc:
        return SyncOutcome(exit_code=exc.exit_code, stderr_lines=[str(exc)])

    begin_sentinel, end_sentinel = _sentinel_pair(name)
    snippet_file = plugin_root / "snippets" / f"{name}.md"
    if not snippet_file.is_file():
        return SyncOutcome(
            exit_code=1, stderr_lines=[f"ERROR: canonical snippet not found at {snippet_file}"]
        )

    consumer_source = meta["consumer_source"]
    fence_aware = meta["fence_aware"]
    allow_insert = meta["allow_insert"]
    header_style = meta["header_style"]
    in_fence_consumers = meta["in_fence_consumers"]
    delivery = meta["delivery"] if consumer_source == "registry" else None
    # Every tree-WALKING surface below (scan-driven consumer discovery, orphan
    # detection) must read the same root the consumer set resolves against, or
    # a COORDINATOR_CONTENT_ROOT redirect splits the two and the scans report
    # against a tree outside the one under test. Single ladder, one definition.
    scan_root = _registry.effective_content_root(plugin_root, content_root)
    shell_begin, shell_end = (
        _shell_sentinel_pair(begin_sentinel, end_sentinel)
        if in_fence_consumers
        else ("", "")
    )

    # ------------------------------------------------------------------
    # Resolve raw candidates.
    # ------------------------------------------------------------------
    resolve_notes: list[str] = []
    if consumer_source == "registry":
        # NEGATIVE SPEC (Review: code-reviewer, Finding 1, 80d115d9 follow-up):
        # an inject row's paste-target surface is its `conditional_consumer`
        # entries and NOTHING ELSE (DR-240 § Decision 3). Plain `consumers` on
        # an inject row is a logical/documentation set nothing pastes from —
        # 80d115d9 gated the ORPHAN-detection `registered` set on `delivery`
        # but left `raw_candidates` (the active sentinel-sync/--fix surface)
        # unconditional, so a file that was both a declared plain consumer AND
        # carried an exact-match sentinel got double-classified: OK/FIXED here
        # *and* ORPHAN below, in the same run, with --fix actively re-syncing
        # content into a file the orphan report told the reader to delete. Do
        # not re-split this gate from the `registered`-set construction below —
        # the whole defect was those two places disagreeing.
        if delivery == "inject":
            raw_candidates = _registry.resolve_conditional_consumers(
                data,
                name,
                plugin_root,
                machine_local_bin=machine_local_bin,
                notes=resolve_notes,
            )
        else:
            raw_candidates = _registry.resolve_consumers(
                data,
                name,
                plugin_root,
                content_root=content_root,
                machine_local_bin=machine_local_bin,
                notes=resolve_notes,
            )
    else:  # "scan"
        search_root = (
            scan_root if meta["search_scope"] == "plugin-root" else scan_root.parent
        )
        scan_sentinels = [begin_sentinel] + ([shell_begin] if in_fence_consumers else [])
        try:
            raw_candidates = _grep_scan(search_root, scan_sentinels)
        except RuntimeError as exc:
            # Review: code-reviewer — grep-tool-absence must surface as a
            # verification failure (exit 2), never as a silent "0 consumers"
            # result indistinguishable from a genuine empty scan.
            return SyncOutcome(exit_code=2, stderr_lines=[f"ERROR: {exc}"])
        if in_fence_consumers:
            # The canonical snippet file is NEVER a consumer — its own fence
            # quotes the shell-comment dialect verbatim (a quoted mention that
            # the in-fence rule would otherwise mistake for a live block).
            canonical = os.path.normpath(str(snippet_file))
            raw_candidates = [f for f in raw_candidates if os.path.normpath(f) != canonical]

    # ------------------------------------------------------------------
    # --list mode: canonical universe (Q15) for registry-driven snippets;
    # scan-discovered set (no independent universe exists) for scan-driven.
    # Short-circuits before the exists+sentinel gate below.
    # ------------------------------------------------------------------
    if effective_mode == "--list":
        if consumer_source == "registry":
            return SyncOutcome(exit_code=0, lines=list(raw_candidates), stderr_lines=resolve_notes)
        gated = []
        for f in raw_candidates:
            if not Path(f).is_file():
                continue
            text = Path(f).read_text(encoding="utf-8")
            if _is_standalone_sentinel_line(text, begin_sentinel, fence_aware=fence_aware) or (
                in_fence_consumers and _has_in_fence_shell_block(text, shell_begin)
            ):
                gated.append(f)
        return SyncOutcome(exit_code=0, lines=gated, stderr_lines=resolve_notes)

    # ------------------------------------------------------------------
    # Build the active consumer set for verify/--fix.
    # ------------------------------------------------------------------
    stderr_lines: list[str] = list(resolve_notes)
    active: list[str] = []
    shell_files: set[str] = set()  # in_fence_consumers=True only
    missing_no_sentinel: list[str] = []  # allow_insert=True only

    for raw in raw_candidates:
        p = Path(raw)
        if not p.is_file():
            stderr_lines.append(f"SKIPPED (not found): {raw}")
            continue
        text = p.read_text(encoding="utf-8")
        has_sentinel = _is_standalone_sentinel_line(text, begin_sentinel, fence_aware=fence_aware)
        has_shell = in_fence_consumers and _has_in_fence_shell_block(text, shell_begin)
        if has_shell:
            shell_files.add(raw)
        if has_sentinel or has_shell:
            active.append(raw)
        elif allow_insert:
            missing_no_sentinel.append(raw)
        # else: exists but no sentinel, allow_insert=False → not a consumer, silent exclusion
        # (matches the 6 non-quota originals exactly).

    # ------------------------------------------------------------------
    # Orphan-sentinel scan (see the ORPHAN-SENTINEL DETECTION block above).
    #
    # This runs BEFORE the zero-consumers early return below, and that ordering
    # is the entire fix, not an incidental placement: the 2026-07-25 incident's
    # snippet (`reviewer-calibration`) has `consumers = []`, so `active` is empty
    # and the early return fires. Scanning after it would have reported "no
    # consumers found — nothing to verify" on the exact snippet that had an
    # orphan, forever.
    # ------------------------------------------------------------------
    orphan_lines: list[str] = []
    orphan_found = False
    if consumer_source == "registry":  # constraint 4 — "scan" is exempt by construction
        # `delivery` (registry schema_version 3, example-doctrine-repo 7ff1cb75e) decides WHAT counts
        # as registered here, and the two cases are genuinely different:
        #
        #   paste  — the plain `consumers` list IS the paste-target set, so a
        #            sentinel in one of those files is expected, not an orphan.
        #   inject — the block reaches its consumers through `contract_blocks:`
        #            assembly at dispatch time and is pasted by NOTHING, so the
        #            plain `consumers` list is a logical/documentation set and a
        #            pasted sentinel in one of those files IS an orphan. Four
        #            registry rows carry a non-empty `consumers` under inject
        #            (disk-first-protocol, run-report-citizenship,
        #            sidecar-emission-contract, sidecar-frontmatter-contract), so
        #            this is a live distinction, not a theoretical one.
        #
        # CARVE-OUT — `conditional_consumer` entries stay paste targets on an
        # inject row and are always registered. Four inject rows are injected for
        # the coordinator personas while carrying a genuinely pasted example-game-repo
        # live-install conditional; treating "inject" as "nothing is ever pasted"
        # hard-errors on those legitimate pastes. The exemption is declared data
        # already parsed, not per-file bookkeeping.
        # `raw_candidates` is already delivery-correct (see the gate above,
        # ~:972-1001) — on an inject row it IS the conditional-consumer set,
        # so `registered` is built from it uniformly for both delivery values.
        # Do not re-split this into a second delivery branch here.
        registered = {os.path.normpath(str(p)) for p in map(Path, raw_candidates)}
        registered.add(os.path.normpath(str(snippet_file)))
        registered.add(os.path.normpath(str(registry_path)))
        orphans = _scan_orphans(
            scan_root, name, begin_sentinel, end_sentinel, registered
        )
        if orphans:
            orphan_found = True
            orphan_lines = _orphan_report(
                orphans, name, is_fix=effective_mode == "--fix", delivery=delivery
            )

    # ------------------------------------------------------------------
    # eligible_glob completeness (registry schema_version 4, example-doctrine-repo 355255cc3).
    #
    # Placed beside the orphan scan and BEFORE the zero-consumers early return
    # for the same reason that scan is: a row can declare an eligible_glob while
    # resolving an empty ACTIVE consumer set (every glob member undeclared is the
    # extreme case of exactly the defect this checks for), and returning early
    # would report "nothing to verify" on the row that needed the check most.
    #
    # NEGATIVE SPEC: like the orphan scan, this never mutates — on --fix
    # included. Which of the two declarations a gap wants is a human call.
    # ------------------------------------------------------------------
    glob_gap_lines: list[str] = []
    glob_gap_found = False
    if consumer_source == "registry" and meta["eligible_glob"]:
        gaps = _registry.eligible_glob_gaps(
            data, name, plugin_root, content_root=content_root
        )
        if gaps:
            glob_gap_found = True
            glob_gap_lines = _eligible_glob_report(
                gaps, name, meta["eligible_glob"], delivery=delivery
            )

    if not active and not missing_no_sentinel:
        # The zero-consumer message carries REPAIR ADVICE, so it branches on
        # delivery for the same reason `_orphan_report`'s exit (A) does
        # (DR-240 § Decision 4). "run --fix to insert sentinel blocks" is
        # correct on a paste row and actively harmful on an inject row: there
        # is nothing to insert, and pasting is the thing an inject row exists
        # to not do. A reader following it would paste blocks whose next
        # verify run reports them as orphans.
        #
        # Missed on the first pass — Decision 4 was applied to the orphan
        # report and not to this message, which is the same defect class one
        # function away. When adding any new advice-bearing line here, check
        # whether it is delivery-specific BEFORE writing it.
        if consumer_source != "registry":
            msg = "no consumers detected — nothing to verify"
        elif delivery == "inject":
            msg = (
                "no paste targets — nothing to verify. This snippet is "
                'delivery="inject": it reaches its consumers via contract_blocks: '
                "assembly at dispatch time, so it has no paste targets beyond any "
                "declared conditional_consumer paths (none resolvable here). Do NOT "
                "run --fix expecting it to insert blocks — on an inject row a pasted "
                "block is an orphan, not a repair."
            )
        else:
            msg = (
                "no consumers found — nothing to verify (run --fix on the consumer "
                "files first to insert sentinel blocks)"
            )
        return SyncOutcome(
            exit_code=1 if (orphan_found or glob_gap_found) else 0,
            lines=[msg] + orphan_lines + glob_gap_lines,
            stderr_lines=stderr_lines,
        )

    # ------------------------------------------------------------------
    # Extract + normalize the canonical snippet body.
    # ------------------------------------------------------------------
    snippet_text = snippet_file.read_text(encoding="utf-8")
    try:
        snippet_body = _extract_snippet_body(
            snippet_text, header_style, begin_sentinel, end_sentinel, snippet_name=name
        )
        _assert_no_residual_comment_fragments(snippet_body, name)
    except ValueError as exc:
        return SyncOutcome(exit_code=2, stderr_lines=[f"ERROR: {exc}"])
    snippet_norm = normalize_snippet(snippet_body)

    if not snippet_norm:
        return SyncOutcome(
            exit_code=2,
            stderr_lines=[
                f"ERROR: canonical snippet body is empty — {snippet_file} is missing its "
                f"BEGIN/END sentinel wrappers.",
                f"       Expected '{begin_sentinel}' ... '{end_sentinel}' around the body. "
                f"Refusing to verify/fix (would wipe consumer blocks).",
            ],
        )

    # In-fence blocks sync against the interior of the canonical body's FIRST
    # ``` fence, not the whole body (see module docstring § in-fence dialect).
    fence_body = _extract_fence_body(snippet_body) if in_fence_consumers else None
    fence_norm = normalize_snippet(fence_body) if fence_body is not None else ""
    if shell_files and not fence_norm:
        return SyncOutcome(
            exit_code=2,
            stderr_lines=[
                f"ERROR: in-fence consumer blocks found but the canonical snippet body in "
                f"{snippet_file} contains no non-empty ``` fenced code block to sync them "
                f"against. Refusing to verify/fix (would wipe consumer blocks).",
            ],
        )

    exit_code = 0
    out_lines: list[str] = []

    # allow_insert=True: MISSING / INSERTED for exists-but-no-sentinel consumers.
    for raw in missing_no_sentinel:
        if effective_mode == "--fix":
            _insert_block(Path(raw), name, begin_sentinel, end_sentinel, snippet_body)
            out_lines.append(f"INSERTED     {raw}")
            _claim_if_session(raw, cs_lib=cs_lib)
        else:
            out_lines.append(f"MISSING      {raw}")
            exit_code = 1

    is_fix = effective_mode == "--fix"
    for raw in active:
        p = Path(raw)
        text = p.read_text(encoding="utf-8")

        missing_end = False
        mismatched = False
        fixed = False
        error_vanished = False

        # In-fence shell-comment dialect first (its rewrite is a single whole-
        # file write; the HTML path below then re-reads nothing — it operates
        # on the updated `text` and its own on-disk rewrite via _rewrite_block).
        if raw in shell_files and fence_body is not None:
            updated, counts = _sync_in_fence_blocks(
                text, shell_begin, shell_end, fence_norm, fence_body, fix=is_fix
            )
            if is_fix and updated != text:
                p.write_text(updated, encoding="utf-8")
                _claim_if_session(raw, cs_lib=cs_lib)
                text = updated
            missing_end = counts["missing_end"] > 0
            mismatched = counts["mismatched"] > 0
            fixed = counts["fixed"] > 0

        if _is_standalone_sentinel_line(text, begin_sentinel, fence_aware=fence_aware):
            # Review: code-reviewer — the MISSING_END pre-check must be
            # position-aware (end marker found AFTER the real begin marker), not
            # a positionless substring test: a stray END-sentinel-shaped line
            # elsewhere in the file (e.g. left over from a prior bad edit) used
            # to pass this check while the genuine BEGIN had no real closing END,
            # then silently produced a false "FIXED" report with zero bytes
            # written. Folding the pre-check into the same fence-aware,
            # position-aware extractor used for the real extraction fixes both
            # the false-FIXED bug and the wrong-occurrence extraction bug in one
            # gate — this now uses the exact same marker positions the eventual
            # --fix rewrite will use.
            sliced = _fence_aware_slice(text, begin_sentinel, fence_aware=fence_aware)
            result = _extract_block(sliced[1], begin_sentinel, end_sentinel) if sliced else None
            if sliced is None or result is None:
                missing_end = True
            elif normalize_snippet(result.block) == snippet_norm:
                pass
            elif is_fix:
                if _rewrite_block(p, begin_sentinel, end_sentinel, snippet_body, fence_aware=fence_aware):
                    fixed = True
                    _claim_if_session(raw, cs_lib=cs_lib)
                else:
                    # Review: code-reviewer — the rewrite must not be
                    # reported as FIXED if _rewrite_block silently found the
                    # markers absent/vanished (fail loud, not a false FIXED).
                    error_vanished = True
            else:
                mismatched = True

        # One report line per file, worst status wins across both dialects —
        # for the pre-in_fence_consumers universe (HTML dialect only) exactly
        # one flag can be set, so the emitted lines are byte-identical to the
        # prior single-dialect report.
        if error_vanished:
            out_lines.append(f"ERROR        {raw}: markers vanished before fix could apply")
            exit_code = 1
        elif missing_end:
            out_lines.append(f"MISSING_END  {raw}")
            exit_code = 1
        elif mismatched:
            out_lines.append(f"MISMATCH     {raw}")
            exit_code = 1
        elif fixed:
            out_lines.append(f"FIXED        {raw}")
        else:
            out_lines.append(f"OK           {raw}")

    if orphan_found:
        out_lines.extend(orphan_lines)
        exit_code = 1

    if glob_gap_found:
        out_lines.extend(glob_gap_lines)
        exit_code = 1

    return SyncOutcome(exit_code=exit_code, lines=out_lines, stderr_lines=stderr_lines)

"""
coordinator_core.pickup_brief — the rebuilt `pickup-assemble brief` engine
(DR-415 kill-and-rebuild arm), written from the requirement rather than
lifted from `coordinator_core.pickup_assemble`.

Spec backlinks:
    docs/decisions/DR-415-the-pickup-brief-computes-only-what-a-co.md
    docs/research/spike-verdicts/2026-09-12-pickup-assemble-brief-under-500ms.md
    docs/plans/2026-09-11-three-ceremony-briefs-rebuilt-from-their-requirements.md,
        chunk C10 (§ The pickup oracle, § Where the rebuilt pickup brief lives)

Contract this module satisfies (the pickup oracle, DR-415's kept set,
quoted verbatim in the plan): a rebuilt pickup brief returns EXACTLY these
keys and no others —

    artifact.{path,classification,resolution,frontmatter}
    artifact.chain.ancestor_count
    gates.{claim,claim_grant,liveness_signal,coast,execution_stamp_match,
           shipped_state,sender_reachability}
    gates.addressee (memo only)
    directives, judgment_points, narration, next_move, sizing_disposition
    preflight.{completeness_items,completeness_batches,tree_quiescence}

Every field DR-415 deleted (`preflight.closure_signals`,
`preflight.deliverable_evidence`, `preflight.premise_checks`,
`preflight.staleness`, `preflight.prereq_reverify`,
`governing_plan_resolution`, `artifact.chain.walk/paths/root`,
`preflight.stealth_skip_flags`, `gates.branch`, `gates.gate_notes`,
`gates.aging_verdict`) is ABSENT from this module's output, not emitted as
null. No flag brings one back (negative-spec, DR-415).

Negative-spec (DR-344 §6): this module was written without opening
`pickup_assemble.brief` / `brief_multi`, or any function in DR-415's eight
deleted groups, as a reference. It re-derives the surviving rules
(artifact resolution, the R4 claim-grant rule, execution-stamp match) from
the plan's § Requirements and § Design text and from this module's own
shared, non-monolith dependencies — never from reading the deleted code.

Import closure (must NOT include `coordinator_core.pickup_assemble` — the
whole point of this module's existence, and the row's own hard
constraint). Modules this file imports, and why:
    coordinator_core.dag                         -- artifact.chain.ancestor_count,
        via walk_forward(..., include_history_tier=False) (C9's kept fixes:
        the SHA-ref short-circuit and the raw id-index scan already live
        there and are exercised for free by this call).
    coordinator_core.git.repo_root                -- repo-root resolution
        (spawn-free walk; see that module's own docstring).
    coordinator_core.git.git_state                 -- `read_index`/
        `head_blobs`, the spawn-free staged-vs-HEAD identity reads
        `compute_tree_quiescence`'s fast path uses for a small, scoped
        `scope:` expansion (§ tree_quiescence below).
    coordinator_core.git.git_index                 -- `scoped_status`, the
        spawn-free worktree-vs-index stat fast path, same fast-path use.
    coordinator_core.git.content_hash              -- `content_matches_
        index_sha`, the normalize-then-hash settle for a stat-mismatch
        candidate (never a raw-bytes hash; see that module's own
        docstring for the CRLF hazard this respects).
    coordinator_core.claim_state                  -- `resolve_claim_state`/
        `handoff_claim_dir`, the ledger-first accessor used for `d2`'s
        stamp-evidence check (C11 row 35: ledger-first so a branch-switch-
        revert desync still reads correctly); a LEAF module, no
        coordinator_core.ops import.
    coordinator_core.lifecycle                    -- `git_common_dir`
        (`lru_cache`d, no extra spawn), resolving the claim-dir root for the
        same `d2` stamp-evidence check.
    coordinator_core.session.claims                -- claim-dir path
        convention, claim_stage, brief_lease_expired, claim_artifact (the
        brief-stage claim take for a single-artifact invocation).
    coordinator_core.session.liveness              -- claim_holder_live,
        claim_held_by_me, session_verdict (the R4 claim-grant rule).
    coordinator_core.session.work_state            -- _parse_fm_dict, the
        shared frontmatter-dict reader.
    coordinator_core.contract.apply_base           -- current_session_env
        (the contextvar-only self session-id accessor the R4 rule needs).
    coordinator_core.contract.decision_object.judgment -- build_judgment_point
        (the shared judgment-point constructor; not this module's own copy).
    coordinator_core.frontmatter.primitives        -- split_frontmatter,
        read_fm_field_unquoted, canonical_body_sha, git_blob_sha1 (the
        PURE-PYTHON git-blob-hash recipe — no `git hash-object` spawn).
    coordinator_core.sizing_disposition            -- compute_sizing_disposition
        (sizing_disposition, verbatim reuse — this module owns the rule).
    coordinator_core.shipped_in_tokens             -- the shipped_in value
        grammar (gates.shipped_state), a leaf module.
    coordinator_core.wire_paths                    -- rel_id (repo-relative
        display-path rendering).
    coordinator_core.artifact_basename             -- md_fallback_candidates
        (the not-found error's tried-basenames list).
    coordinator_core.win_portability               -- no_console_creationflags
        (the one git spawn on this path, § tree_quiescence, must not pop a
        console window on Windows).

`cli_dispatch`, `cli_rejection`, `apply_halt`, `json_payload_flag`
(`coordinator_core.ceremony_common`): this module CONSUMES
`coordinator_core.ceremony_common.json_payload_flag`'s existing
`detect_conflicting_payload_channels` / `resolve_json_payload_flag` for its
own `--decisions`/`--decisions-file` argument parsing in `main()` — the
default per the row body, and correct here because both monoliths already
import this module at top level (measured free, 46.9ms p50 against a
62.5ms bare interpreter, this box, 2026-09-12) and it is not the git-facts
sharing R7/P0 C5 ruled out. `cli_dispatch`, `cli_rejection` and
`apply_halt` are NOT consumed: this module's `main()` is a single-verb
dispatcher (`brief` handled here; `apply`/`drop`/`stamp-check` delegated
through one lazy import into the still-live monolith, per the row body) —
there is no multi-op CLI table here for `cli_dispatch` to route, no
rejection-shaped negative response for `cli_rejection` to render, and no
halt-on-partial-apply state for `apply_halt` to guard. Re-deriving trivial
argv branching locally rather than forcing a three-module import for a
shape this module does not have is the same "no new abstraction for its
own sake" discipline DR-415/P0 C5 apply elsewhere on this plan.

Documented reductions from the monolith's behaviour (this module is a
REWRITE from the requirement, not a byte-identical port — see the row body,
"Write from the requirement, not from the old code"):
    - `resolve_artifact` implements the literal-path, prose-sanitize and
      live/archive basename-fallback tiers. The elision-marker form
      (`…/<basename>`) resolves through the basename fallback rather than a
      dedicated tier, matching the monolith's output. NOT reproduced: the
      suffix-match tier (a prefix-omitted slug resolved by unique
      basename-suffix match, 2026-07-28) and the git-revision-SHA tier
      (2026-08-14). A caller passing either gets a not-found business
      failure (exit 1) where the monolith would have resolved it — a real
      behavioural gap, named here rather than silently absorbed.

      The revision-SHA tier is the one deliberately left out on cost
      grounds rather than scope: it walks git history to map a SHA to the
      artifact it touched, which is exactly the "corpus walk for an answer"
      DR-344 targets. Restoring it needs a measurement, not a reflex.
    - `gates.execution_stamp_match` computes `computed_sha` via
      `frontmatter.primitives.canonical_body_sha` (a pure-Python git-blob-hash,
      zero spawns) and reports `verdict in {"match", "mismatch"}` (or `None`
      when the artifact carries no stamp to check). `stamp_commit` and
      `delta_class` (`stale-bookkeeping`/`stale-substantive`) ARE
      reproduced: the classification is governance-load-bearing — it is
      what routes substantive drift to `surface-to-PM` instead of offering
      a re-stamp — so dropping it changed a verdict, not just a narration.
      Its git spawns sit off the zero-spawn hot path, reached only once a
      stamp or pointer is already present.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime as _dt, timezone as _tz
from pathlib import Path
from typing import Any, Optional

from coordinator_core import dag
from coordinator_core import lifecycle as lifecycle_mod
from coordinator_core.artifact_basename import md_fallback_candidates
from coordinator_core.claim_state import handoff_claim_dir, resolve_claim_state
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops.parse_completeness_item import (
    _Malformed as _CompletenessMalformed,
    parse_completeness_item as _parse_completeness_item,
)
from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)
from coordinator_core.contract.apply_base import (
    OutOfRepoPath,
    assert_in_repo_root,
    current_session_env,
)
from coordinator_core.contract.decision_object.judgment import (
    build_judgment_point as _shared_build_judgment_point,
    build_untrusted_gate_judgment_point as _shared_build_untrusted_gate_judgment_point,
)
from coordinator_core.frontmatter.primitives import (
    canonical_body_sha,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.git import git_index as _git_index
from coordinator_core.git import git_state as _git_state
from coordinator_core.git import repo_root as _repo_root_mod
from coordinator_core.git.content_hash import content_matches_index_sha
from coordinator_core.session import claims as _claims
from coordinator_core.session import core as _session_core
from coordinator_core.session import liveness as _liveness
from coordinator_core.session_baton.store import merge_baton, read_baton
from coordinator_core.session.work_state import _parse_fm_dict
from coordinator_core.shipped_in_tokens import (
    _NO_COMMIT_TOKEN_RE as _SHIPPED_NO_COMMIT_RE,
    _SHA_HEX_RE as _SHIPPED_SHA_RE,
)
from coordinator_core.sizing_disposition import compute_sizing_disposition
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id

# ---------------------------------------------------------------------------
# Exit-code contract (locally scoped — coordinator/bin/pickup-assemble.py's
# own header, reproduced here verbatim so `main()` stays self-contained).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

# Same dir sets as the monolith (contract § artifact) — a live baton may sit
# un-actioned in a LIVE_DIRS entry, or have been swept to its paired
# ARCHIVE_DIRS entry. Kept identical because callers on both sides of the
# cutover (C11) must see the same resolution.
LIVE_DIRS = ("cross-repo/inbox", "state/handoffs", "docs/plans")
ARCHIVE_DIRS = ("cross-repo/archive", "archive/handoffs", "archive/completed")

CLAIM_CLASS_HANDOFF = "handoff"
CLAIM_CLASS_MEMO = "memo"


class _ArtifactUnreadable(Exception):
    pass


class _TransportFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# repo root
# ---------------------------------------------------------------------------

def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Spawn-free repo-root resolution (`coordinator_core.git.repo_root`,
    walk-only — no `git rev-parse` fallback needed for this brief's shape)."""
    top = _repo_root_mod.show_toplevel(str(start) if start is not None else None)
    return Path(top) if top else None


# ---------------------------------------------------------------------------
# artifact resolution + classification
# ---------------------------------------------------------------------------

def _spinoff_classified_kinds() -> frozenset:
    """Ported verbatim (HEAD's own `_SPINOFF_CLASSIFIED_KINDS`) — sourced
    from the canonical `_PRE_RENAME_ALIASES` table via
    `kind_values_for_canonical`, not a hand-spelled literal set."""
    from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
    return frozenset(
        {"spinoff"}
        | set(kind_values_for_canonical("roadmap-baton"))
        | set(kind_values_for_canonical("goal-seed"))
    )


_SPINOFF_CLASSIFIED_KINDS = _spinoff_classified_kinds()


def classify(fm_text: str, path: Path) -> str:
    """`artifact.classification` — HEAD parity port of the monolith's own
    `classify(path, fm_text, repo_root)` (directory residency + frontmatter
    shape, unquoted reads — never the parsed-dict guess this module used
    before): `state/handoffs/` + a recognized `status` -> handoff/spinoff;
    `cross-repo/inbox/` OR (memo-shaped + status open/terminal) -> memo;
    else -> `ambiguous` (never a guess — `resolve_artifact`'s archive-dir
    upgrade, Defect 2, promotes an archive-resident `ambiguous` hit to a
    terminal `archived` record)."""
    posix = path.as_posix().replace("\\", "/")
    in_handoffs_dir = "state/handoffs" in posix
    in_inbox_dir = "cross-repo/inbox" in posix

    kind = read_fm_field_unquoted(fm_text, "kind")
    status = read_fm_field_unquoted(fm_text, "status")

    if in_handoffs_dir and status in {"active", "consumed", "open", "claimed"}:
        return "spinoff" if kind in _SPINOFF_CLASSIFIED_KINDS else "handoff"

    if in_inbox_dir or (_has_memo_shape(fm_text) and (status == "open" or status in _MEMO_TERMINAL_STATUS)):
        return "memo"

    return "ambiguous"


#: Matched-pair wrapper punctuation a caller-pasted path is commonly found
#: wrapped in when it is rendered inline in prose (a memo sentence, a chat
#: message) — see `_sanitize_artifact_path_str`.
_PATH_WRAPPERS = {"(": ")", "[": "]", "<": ">", '"': '"', "'": "'", "`": "`"}

#: Sentence-final punctuation a caller-pasted path commonly picks up when a
#: human ends a sentence with it (`...fix.md.`, `...fix.md,` etc).
_TRAILING_SENTENCE_PUNCT = ".,;:!?"

#: A hard line wrap the rendering surface inserted into a pasted path, plus
#: the continuation indent on either side of it. Deliberately NOT `\s` on
#: either side: a match must be anchored on a real newline, so a path
#: containing an ordinary interior space is never touched.
_LINE_WRAP_RE = re.compile("[ \t]*\r?\n[ \t]*")


def _last_path_segment(path_str: str) -> str:
    r"""Returns the final `/`- or `\`-delimited segment of `path_str` (both
    separators, since Windows paths pasted here may use either) — used by
    `_sanitize_artifact_path_str` to detect a bare `.`/`..` component before
    stripping trailing punctuation would corrupt it."""
    return path_str.replace("\\", "/").rsplit("/", 1)[-1] if path_str else path_str


def _sanitize_artifact_path_str(raw: str) -> str:
    """Fallback-candidate generator: strips prose wrappers and trailing
    sentence punctuation a human commonly pastes around an inline artifact
    path (2026-07-27 incident — `/coordinator:pickup <path>.` with a
    sentence-final period reported "not found" for a file that plainly
    existed, because the literal string carried the trailing `.`).

    Strips, to a fixed point (so a wrapped-and-punctuated path like
    "(`foo.md`)." resolves fully in one call): surrounding whitespace; an
    interior HARD LINE WRAP rejoined with NO separator; a matched wrapper
    pair, ONLY when BOTH ends match; a single trailing `.`/`,`/`;`/`:`/
    `!`/`?`.

    Line-wrap tolerance (2026-08-10, PM ask — the Windows case): a long
    absolute path pasted into a prompt or slash-command argument is
    routinely hard-wrapped mid-token by the rendering surface. A newline is
    not a legal character in a Windows filename and is vanishingly rare in
    a POSIX one, so its presence is evidence of the wrap itself, never of
    the path.

    Negative-spec — the rejoin uses NO separator, so a wrap that fell on a
    genuine SPACE inside a path is NOT recovered: nothing in the wrapped
    string distinguishes a consumed space from a mid-token break, and
    coordinator artifact basenames are kebab-case by construction.

    Negative-spec — this is a FALLBACK CANDIDATE, never a normalizer: the
    caller must attempt resolution with the RAW string first and only fall
    back to this function's output on a miss, so a legitimately-period-
    ending or dot-component path is never mutated out from under a caller
    whose raw input already resolves.

    Guards (do not weaken without re-reading the incident writeup): never
    strips trailing punctuation off a bare `.` or `..` PATH COMPONENT;
    never strips a lone trailing `:` off a bare Windows drive letter.
    """
    s = raw
    while True:
        stripped = s.strip()
        if stripped != s:
            s = stripped
            continue
        unwrapped = _LINE_WRAP_RE.sub("", s)
        if unwrapped != s:
            s = unwrapped
            continue
        if len(s) >= 2 and s[0] in _PATH_WRAPPERS and s[-1] == _PATH_WRAPPERS[s[0]]:
            s = s[1:-1]
            continue
        if s and s[-1] in _TRAILING_SENTENCE_PUNCT:
            if _last_path_segment(s) in (".", ".."):
                break
            if s[-1] == ":" and re.match(r"^[A-Za-z]:$", s):
                break
            s = s[:-1]
            continue
        break
    return s


#: Separator characters a suffix match must land on, so a bare `.endswith()`
#: cannot match mid-word (slug `ate-recommendation` matching
#: `...forwarder-gate-recommendation.md` — a silent wrong-artifact pick).
_SUFFIX_BOUNDARY_CHARS = ("-", "_")

#: Length floor below which the suffix tier does not fire at all — a short
#: slug would sweep the whole tree and match arbitrarily.
_MIN_SUFFIX_SLUG_LEN = 8


def _basename_has_slug_suffix(candidate_name: str, slug: str) -> bool:
    """True when `candidate_name` ends with `slug` once a trailing `.md` is
    stripped from BOTH sides (2026-07-28 suffix-match tier), AND the match
    starts at a genuine filename-COMPONENT boundary — either index 0 of the
    stripped stem, or immediately preceded by a `_SUFFIX_BOUNDARY_CHARS`
    separator.

    The boundary check exists because a bare `.endswith()` also matches
    mid-word — a silent wrong-artifact pick with no ambiguity signal, worse
    than a clean not-found for a resolver whose result gets claimed and
    mutated (2026-07-28 review finding, PM ruling: fail-loud beats fuzzy).

    Suffix-only, deliberately: filenames in this contract are always
    `<date>-<sender>-<slug>`, so a caller who omitted the prefix is missing
    the HEAD of the name, not the tail. Case-sensitive by design (contract
    ask): do not lowercase either side.
    """
    stripped_name = candidate_name[: -len(".md")] if candidate_name.endswith(".md") else candidate_name
    stripped_slug = slug[: -len(".md")] if slug.endswith(".md") else slug
    if not stripped_name.endswith(stripped_slug):
        return False
    boundary_index = len(stripped_name) - len(stripped_slug)
    if boundary_index == 0:
        return True
    return stripped_name[boundary_index - 1] in _SUFFIX_BOUNDARY_CHARS


def _search_dirs_for_slug_suffix(repo_root: Path, slug: str, rel_dirs) -> list[Path]:
    """The ONE place that walks a set of repo-relative dirs looking for a
    file whose basename ENDS WITH `slug` (see `_basename_has_slug_suffix`).

    Callers are responsible for the `_MIN_SUFFIX_SLUG_LEN` floor — this
    function applies the predicate to whatever `slug` it is given and does
    not itself refuse a short one, so a caller skipping the length check
    would sweep the whole tree.
    """
    hits: list[Path] = []
    for rel_dir in rel_dirs:
        base = repo_root / rel_dir
        if not base.is_dir():
            continue
        for candidate in base.rglob("*"):
            if candidate.is_file() and _basename_has_slug_suffix(candidate.name, slug):
                hits.append(candidate)
    return hits


# ---------------------------------------------------------------------------
# Elision-tolerant path resolution (2026-07-24 incident) — a PM/EM baton
# handoff is routinely copy-pasted out of a terminal transcript, and the
# terminal elides long paths (a UUID mid-filename gets replaced with a
# single-glyph U+2026 or an ASCII `...` run). `resolve_artifact` treats
# such a basename as a glob pattern instead of failing closed on a path
# that was never going to exist literally.
# ---------------------------------------------------------------------------

#: Single-glyph ellipsis a terminal may substitute for an elided path run.
_ELLIPSIS_CHAR = "…"


def _is_elided_basename(basename: str) -> bool:
    """True when `basename` carries either elision marker (U+2026 or the
    ASCII `...` three-dot run) — the trigger for glob-based resolution."""
    return _ELLIPSIS_CHAR in basename or "..." in basename


def _elision_glob_pattern(basename: str) -> str:
    """Split `basename` on its elision marker into a `<prefix>*<suffix>`
    glob pattern. Prefers the ASCII `...` split when both markers happen to
    be present; callers only invoke this after `_is_elided_basename`
    confirms at least one is."""
    marker = "..." if "..." in basename else _ELLIPSIS_CHAR
    prefix, _, suffix = basename.partition(marker)
    return f"{prefix}*{suffix}"


def _is_safe_elision_path(artifact_path: str) -> bool:
    """False for an absolute path or a path carrying a literal `..`
    traversal component — elision resolution never globs on those inputs
    (contract § security: search stays inside the repo). A caller that
    fails this check simply gets no elision resolution, which falls through
    to the pre-existing not-found error path unchanged."""
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        return False
    return not any(part == ".." for part in candidate.parts)


def _elision_search_roots(artifact_path: str, repo_root: Path) -> list[tuple[Path, bool]]:
    """`(root, recursive)` pairs to glob, in search order: the directory
    named in the passed path first (non-recursive — that is the exact
    directory the caller named), then each `LIVE_DIRS` entry (recursive),
    then each `ARCHIVE_DIRS` entry (recursive — those are `YYYY-MM`-sharded).
    Live roots are searched before archive roots, matching
    `resolve_artifact`'s "search live first" ordering."""
    passed_dir = repo_root / Path(artifact_path).parent
    roots: list[tuple[Path, bool]] = [(passed_dir, False)]
    roots.extend((repo_root / rel_dir, True) for rel_dir in LIVE_DIRS)
    roots.extend((repo_root / rel_dir, True) for rel_dir in ARCHIVE_DIRS)
    return roots


def _resolve_elided_artifact(artifact_path: str, repo_root: Path) -> list[Path]:
    """Glob-resolve an elided `artifact_path`'s basename against the search
    roots. Returns every distinct match found, in root-search order,
    deduplicated by resolved absolute path. Returns `[]` unconditionally for
    an unsafe path (§ `_is_safe_elision_path`) rather than raising — the
    caller falls through to the ordinary not-found flow."""
    if not _is_safe_elision_path(artifact_path):
        return []
    pattern = _elision_glob_pattern(Path(artifact_path).name)
    hits: list[Path] = []
    seen: set[Path] = set()
    for root_dir, recursive in _elision_search_roots(artifact_path, repo_root):
        if not root_dir.is_dir():
            continue
        globber = root_dir.rglob(pattern) if recursive else root_dir.glob(pattern)
        for candidate in sorted(globber):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            hits.append(candidate)
    return hits


class _ArtifactElisionInconclusive(Exception):
    """Raised when an elided `artifact_path` glob-resolves to 2+ distinct
    candidates — a genuine ask, never a "most recent wins" guess (contract
    § "surface to PM, do not guess"). Carries every candidate (repo-relative
    where possible) so `brief()` can build the judgment point verbatim."""

    def __init__(self, artifact_path: str, candidates: list[str]):
        super().__init__(artifact_path)
        self.artifact_path = artifact_path
        self.candidates = candidates


def _literal_hit(p: Path) -> bool:
    """`is_file()`, but rejects a hit that only "exists" because Win32
    silently strips trailing dots/spaces off the final path component
    (`Path("h1.md.").is_file()` is True on Windows, resolving to `h1.md` —
    POSIX has no such normalization). Without this guard, a raw literal
    carrying caller-pasted trailing punctuation spuriously "hits" on
    Windows only, so the RAW-then-sanitized fallback below silently skips
    the sanitize step and the punctuation leaks into the display path —
    never reproducible on POSIX, so it must be caught here."""
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    name = p.name
    if not name or name[-1] not in ". ":
        return True
    try:
        return any(entry.name == name for entry in p.parent.iterdir())
    except OSError:
        return False


def _is_relative(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def _fallback_search(repo_root: Path, dirs: tuple[str, ...], basename: str) -> list[Path]:
    hits: list[Path] = []
    for d in dirs:
        base_dir = repo_root / d
        if not base_dir.is_dir():
            continue
        for candidate in base_dir.rglob(basename):
            if candidate.is_file():
                hits.append(candidate)
    return hits


def _read_fm_dict(text: str) -> dict[str, Any]:
    split = split_frontmatter(text)
    if split is None:
        return {}
    try:
        return _parse_fm_dict(split.fm_text)
    except Exception:
        return {}


#: `resolution.archived_class`'s discriminator and terminal-field key sets —
#: ported verbatim from the monolith's own `_has_memo_shape`/
#: `_TERMINAL_HANDOFF_FIELDS`/`_TERMINAL_MEMO_FIELDS`/`_extract_terminal_
#: fields` (kept-set: `artifact.resolution` is in DR-415's kept field list).
_TERMINAL_HANDOFF_FIELDS = ("status", "deployment_state", "shipped_in")
_TERMINAL_MEMO_FIELDS = (
    "status", "decision", "decision_note", "actioned_note", "realized_by",
    "picked_up_by", "kind", "from", "created",
)


def _has_memo_shape(fm_text: str) -> bool:
    return (
        read_fm_field_unquoted(fm_text, "from") is not None
        and read_fm_field_unquoted(fm_text, "to") is not None
    )


def _extract_terminal_fields(fm_text: str, field_names: tuple[str, ...]) -> dict[str, Any]:
    return {
        name: read_fm_field_unquoted(fm_text, name)
        for name in field_names
        if read_fm_field_unquoted(fm_text, name) is not None
    }


def _is_under_archive_dir(path: Path, repo_root: Path) -> bool:
    """Ported verbatim (HEAD's own `_is_under_archive_dir`) — true when
    `path` resolves inside one of `ARCHIVE_DIRS`."""
    if not _is_relative(path, repo_root):
        return False
    rel = rel_id(path, repo_root)
    return any(rel == d or rel.startswith(d + "/") for d in ARCHIVE_DIRS)


def _resolve_found_file(found_path: Path, repo_root: Path) -> dict[str, Any]:
    try:
        text = found_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise _ArtifactUnreadable(f"{found_path}: unreadable")
    split = split_frontmatter(text)
    fm_text = split.fm_text if split is not None else ""
    display_path = rel_id(found_path, repo_root) if _is_relative(found_path, repo_root) else str(found_path)
    classification = classify(fm_text, found_path)
    # Defect 2 (HEAD parity): a well-formed handoff/memo passed at its
    # NATIVE archive path exists literally, so it never reaches the
    # archive-fallback search below — `classify()`'s directory checks then
    # correctly find neither live-dir residency and fall through to
    # `ambiguous`. Resolve it to the same terminal `archived` shape the
    # fallback search produces for a swept baton.
    if classification == "ambiguous" and _is_under_archive_dir(found_path, repo_root):
        return _build_archived_resolution(display_path, found_path, repo_root)
    fm = _read_fm_dict(text)
    return {
        "path": display_path,
        "classification": classification,
        "frontmatter": fm,
        # A live-found artifact's `resolution` is `None` (HEAD parity) — the
        # dict shape below is reserved for a multi-hit/archived resolution.
        "resolution": None,
    }


def _build_archived_resolution(display_path: str, archive_hit: Path, repo_root: Path) -> dict[str, Any]:
    try:
        text = archive_hit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    split = split_frontmatter(text)
    fm_text = split.fm_text if split is not None else ""
    is_memo = _has_memo_shape(fm_text)
    terminal_fields = _extract_terminal_fields(
        fm_text, _TERMINAL_MEMO_FIELDS if is_memo else _TERMINAL_HANDOFF_FIELDS
    )
    return {
        "path": display_path,
        "classification": "archived",
        "frontmatter": {},
        "resolution": {
            "status": "archived",
            "archive_path": display_path,
            "terminal_fields": terminal_fields,
            "archived_class": "memo" if is_memo else "handoff",
        },
    }


def _tag_elision(result: dict[str, Any], elision_resolution) -> dict[str, Any]:
    """Attaches `elision_resolution` to a resolved-artifact dict when the
    elision tier is what got the caller there, so `_emit` can narrate the
    guess instead of silently swallowing it."""
    if elision_resolution is None:
        return result
    return {**result, "elision_resolution": elision_resolution}


def resolve_artifact(artifact_path: str, repo_root: Path) -> dict[str, Any]:
    """`artifact.{path,classification,resolution,frontmatter}` — the one
    resolver `pickup_brief` owns (§ Design, "one rule, one home"); C11
    re-points `apply`'s own recompute at this function.

    See the module docstring's documented reductions: literal-path,
    prose-sanitize and live/archive basename-fallback tiers only. The
    suffix-match and git-revision-SHA tiers are NOT reproduced.

    Containment: an absolute `artifact_path` resolving outside `repo_root`,
    or a relative one carrying a literal `..` traversal component, raises
    `OutOfRepoPath` — ported from `pickup_assemble._repo_relative_artifact_
    path`'s P1 regression fix (2026-08-30). Not a caller-convenience tier
    (the four documented reductions above); a bound on what this function
    will ever read, kept even though the ladder above it was rewritten.
    """
    # Collapse the absolute form to the same repo-relative string the rest
    # of the ladder is written against, instead of letting it survive as a
    # second, parallel addressing scheme: every tier below joins back onto
    # `repo_root`, and `_is_safe_elision_path` refuses an absolute outright,
    # so an un-normalized absolute silently bypassed all of them.
    candidate = Path(artifact_path)
    if candidate.is_absolute():
        resolved_abs = assert_in_repo_root(candidate, repo_root)
        artifact_path = resolved_abs.relative_to(repo_root.resolve()).as_posix()
    elif any(part == ".." for part in candidate.parts):
        raise OutOfRepoPath(f"{artifact_path} carries a '..' traversal component")

    #: The caller's path exactly as passed (post absolute-normalization) —
    #: the not-found error names it even after a later tier has replaced
    #: `artifact_path` with a transformed form.
    _raw_artifact_path = artifact_path

    elision_resolution: Optional[dict[str, str]] = None
    if _is_elided_basename(Path(artifact_path).name):
        candidates = _resolve_elided_artifact(artifact_path, repo_root)
        if len(candidates) > 1:
            raise _ArtifactElisionInconclusive(
                artifact_path,
                sorted(
                    rel_id(c, repo_root) if _is_relative(c, repo_root) else str(c)
                    for c in candidates
                ),
            )
        if len(candidates) == 1:
            resolved = candidates[0]
            resolved_display = (
                rel_id(resolved, repo_root) if _is_relative(resolved, repo_root) else str(resolved)
            )
            elision_resolution = {"passed": artifact_path, "resolved": resolved_display}
            artifact_path = resolved_display
        # else: zero matches — fall through with artifact_path unchanged.

    live_path = repo_root / artifact_path
    if _literal_hit(live_path):
        return _tag_elision(_resolve_found_file(live_path, repo_root), elision_resolution)

    #: Set the moment a sanitized (wrapper-/punctuation-stripped) form of the
    #: passed path is what actually resolved, so a caller sees the correction
    #: instead of it being silently swallowed.
    sanitize_resolution: Optional[dict[str, str]] = None

    # RAW path missed literally — retry with the sanitized form as a FALLBACK
    # candidate, never a mutation of the working input (raw always tried
    # first, above).
    sanitized_path = _sanitize_artifact_path_str(artifact_path)
    if sanitized_path != artifact_path:
        sanitized_candidate = Path(sanitized_path)
        if sanitized_candidate.is_absolute():
            assert_in_repo_root(sanitized_candidate, repo_root)
        elif any(part == ".." for part in sanitized_candidate.parts):
            raise OutOfRepoPath(f"{sanitized_path} carries a '..' traversal component")
        sanitized_live = repo_root / sanitized_path
        if _literal_hit(sanitized_live):
            sanitize_resolution = {"passed": artifact_path, "resolved": sanitized_path}
            return _tag_elision({
                **_resolve_found_file(sanitized_live, repo_root),
                "sanitize_resolution": sanitize_resolution,
            }, elision_resolution)
        artifact_path = sanitized_path

    basename = Path(artifact_path).name
    # A tier that ran but is only recorded on success hides that it ran at
    # all from the not-found error (2026-07-28 review, Finding 1): the raw
    # basename's candidates stay named even once the sanitized form took
    # over the search.
    raw_basename = Path(_raw_artifact_path).name
    tried = list(md_fallback_candidates(raw_basename))
    for cand in md_fallback_candidates(basename):
        if cand not in tried:
            tried.append(cand)
    live_hits = []
    archive_hits = []
    seen: set[Path] = set()
    for candidate_basename in tried:
        for hit in _fallback_search(repo_root, LIVE_DIRS, candidate_basename):
            if hit not in seen:
                seen.add(hit)
                live_hits.append(hit)
        for hit in _fallback_search(repo_root, ARCHIVE_DIRS, candidate_basename):
            if hit not in seen:
                seen.add(hit)
                archive_hits.append(hit)
    total = len(live_hits) + len(archive_hits)

    #: Set the moment a bare, prefix-omitted SUFFIX of the passed slug is
    #: what actually resolved (2026-07-28 tier, PM ruling) — narrated the
    #: same way as `sanitize_resolution` so a caller can see the engine
    #: guessed and what it landed on, never silently.
    suffix_resolution: Optional[dict[str, str]] = None

    # Suffix tier: a caller citing a slug with the `<date>-<sender>-` prefix
    # omitted. Spawn-free, and only reached once every exact tier above has
    # found nothing, so a real path/basename never gets here.
    if total == 0:
        stripped_slug = basename[: -len(".md")] if basename.endswith(".md") else basename
        if len(stripped_slug) >= _MIN_SUFFIX_SLUG_LEN:
            tried = list(tried) + [f"*{stripped_slug} (suffix match)"]
            suffix_live = _search_dirs_for_slug_suffix(repo_root, basename, LIVE_DIRS)
            suffix_archive = _search_dirs_for_slug_suffix(repo_root, basename, ARCHIVE_DIRS)
            if suffix_live or suffix_archive:
                live_hits, archive_hits = suffix_live, suffix_archive
                total = len(live_hits) + len(archive_hits)
                if total == 1:
                    # A multi-hit suffix match falls into the ambiguous
                    # branch below unnarrated — that branch already surfaces
                    # every candidate path, so there is no single "resolved"
                    # value to name.
                    only_hit = (suffix_live or suffix_archive)[0]
                    suffix_resolution = {
                        "passed": artifact_path,
                        "resolved": (
                            rel_id(only_hit, repo_root)
                            if _is_relative(only_hit, repo_root)
                            else str(only_hit)
                        ),
                    }

    def _tag(result: dict[str, Any]) -> dict[str, Any]:
        if suffix_resolution is not None:
            result = {**result, "suffix_resolution": suffix_resolution}
        return _tag_elision(result, elision_resolution)

    if total == 0:
        raise _ArtifactUnreadable(
            f"{_raw_artifact_path}: not found at the passed path and not in any of "
            f"{', '.join(LIVE_DIRS + ARCHIVE_DIRS)} (basenames tried: {', '.join(repr(b) for b in tried)})"
        )

    if total > 1:
        live_paths = sorted(rel_id(h, repo_root) for h in live_hits)
        archive_paths = sorted(rel_id(h, repo_root) for h in archive_hits)
        status = "archived" if archive_hits and not live_hits else "multi_hit"
        return _tag({
            "path": artifact_path,
            "classification": "ambiguous",
            "frontmatter": {},
            "resolution": {
                "status": status,
                "live_paths": live_paths,
                "archive_paths": archive_paths,
                "terminal_fields": None,
            },
        })

    if live_hits:
        return _tag(_resolve_found_file(live_hits[0], repo_root))

    resolved_archive_path = rel_id(archive_hits[0], repo_root)
    return _tag(_build_archived_resolution(resolved_archive_path, archive_hits[0], repo_root))


# ---------------------------------------------------------------------------
# artifact.chain.ancestor_count
# ---------------------------------------------------------------------------

def chain_ancestor_count(abs_artifact_path: Path, repo_root: Path, classification: str) -> Optional[int]:
    """`artifact.chain.ancestor_count` (deletion 7: no git-history tier —
    `include_history_tier=False`; `walk` itself is not emitted, no
    consumer reads it, DR-415 accepts an age-pruned predecessor reading
    `missing-link` there)."""
    if classification not in ("handoff", "spinoff", "archived"):
        return None
    walk = dag.walk_forward(
        str(abs_artifact_path),
        edge_kinds=set(getattr(dag, "CONTINUATION_EDGE_KINDS", {"predecessor"})),
        handoff_dir=str(repo_root / "state" / "handoffs"),
        repo_root=str(repo_root),
        include_history_tier=False,
    )
    ordered = walk["orderedPaths"]
    return max(len(ordered) - 1, 0)


# ---------------------------------------------------------------------------
# gates.claim / gates.claim_grant  (R4)
# ---------------------------------------------------------------------------

def gates_claim(abs_artifact_path: Path, repo_root: Path, class_: str, basename: str) -> dict[str, Any]:
    """`gates.claim` — ported from HEAD's `compute_claim_gate`: a read-only
    dual-read of the CLAIM LEDGER DIRECTORY ONLY
    (`.git/coordinator-sessions/<class>-claims/<basename>/`), never the
    frontmatter mirror. `holder` is the ledger's recorded `session_id` iff
    that holder is currently live (`_claim_holder_live`/`_claim_holder_live_
    or_elsewhere`'s cross-repo arm) — `None` both when no ledger claim
    exists and when the ledger claim's holder is not live.

    Deliberately NOT `coordinator_core.claim_state.resolve_claim_state`
    (this module's earlier implementation): that shared accessor degrades a
    dead-ledger-holder to a FRONTMATTER-MIRROR fallback
    (`claimed_by`/`consumed_by`), which is a different, broader question
    than what `gates.claim` answers at HEAD — a live artifact carrying only
    a stale `status: claimed`/`claimed_by` mirror with no live ledger entry
    must read `holder: None` here, matching HEAD, not the mirrored sid."""
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    if not claims_dir.is_dir():
        return {"fetch_state": "not_performed", "holder": None}
    holder_sid = None
    sid_file = claims_dir / "session_id"
    if sid_file.is_file():
        try:
            holder_sid = sid_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            holder_sid = None
    holder_live = _claim_holder_live(claims_dir, str(repo_root), holder_sid)
    return {
        "fetch_state": "not_performed",
        "holder": holder_sid if holder_live else None,
    }


def _explicitly_scoped_session_id() -> str:
    scoped = current_session_env()
    for value in scoped.values():
        value = (value or "").strip()
        if value:
            return value
    return ""


def _claim_holder_live(claims_dir: Path, cwd: str, holder_sid: Optional[str]) -> bool:
    try:
        if _liveness.claim_holder_live(str(claims_dir), cwd):
            return True
    except (OSError, ValueError):
        pass
    if not holder_sid:
        return False
    try:
        verdict = _liveness.session_verdict(holder_sid, cwd=cwd)
    except Exception:
        verdict = None
    return bool(verdict is not None and verdict[0] and verdict[1] == "harness-registry-elsewhere")


def _lineage_related_sessions(repo_root: Path, fm: dict[str, Any]) -> frozenset:
    """Narrow re-derivation of the monolith's handover exception (AC3e): a
    holder session is lineage-related when it is this artifact's own
    author, or the author of a directly-named predecessor. Simplified —
    reads `fm["authoring_session"]`/`fm["session_id"]` (the field name
    every other producer in this codebase writes — `baton_assemble`,
    `backlog_grind_assemble`, `archive_stamp` — not the singular
    `author_session` spelling) and, per predecessor path, the same field
    on that predecessor's own frontmatter — resolved through
    `resolve_artifact` itself, so an archived predecessor (moved to
    `archive/handoffs/YYYY-MM/...`) still contributes its
    `authoring_session` rather than reading as a bare-path miss — rather
    than the monolith's transitive walk over every claim in the tree."""
    related: set = set()
    for key in ("authoring_session", "session_id"):
        v = fm.get(key)
        if isinstance(v, str) and v.strip():
            related.add(v.strip())

    predecessor = fm.get("predecessor")
    if isinstance(predecessor, str) and predecessor.strip() and predecessor.strip() != "none":
        try:
            predecessor_artifact = resolve_artifact(predecessor.strip(), repo_root)
        except (_ArtifactUnreadable, OutOfRepoPath):
            predecessor_artifact = None
        if predecessor_artifact is not None:
            # `resolve_artifact`'s "archived" branch deliberately returns
            # `frontmatter: {}` (a fixed-field `terminal_fields` extract,
            # not the full block — see `_build_archived_resolution`), so
            # an archived predecessor's `authoring_session` never lands in
            # `frontmatter` itself. Re-read the resolved path's own full
            # frontmatter directly rather than widening the archived tier's
            # deliberately-narrow extract for every OTHER caller's sake.
            predecessor_path = repo_root / predecessor_artifact["path"]
            try:
                predecessor_text = predecessor_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                predecessor_text = ""
            predecessor_fm = _read_fm_dict(predecessor_text)
            for key in ("authoring_session", "session_id"):
                v = predecessor_fm.get(key)
                if isinstance(v, str) and v.strip():
                    related.add(v.strip())

    return frozenset(related)


def compute_claim_grant(
    repo_root: Path, class_: str, basename: str, artifact_path: str,
    cwd: Optional[str] = None, fm: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.claim_grant` — R4 (ratified): 1) no claimant -> granted;
    2) claimant is self -> granted, held_by_self; 2b) different session,
    expired brief-stage lease -> granted-with-warning; 3) different,
    live -> denied, UNLESS lineage-related (handover) -> granted;
    4) different, not live or unresolvable -> granted-with-warning.
    Age is never read (R4's PM amendment: "age is not a factor, only
    resolveable aliveness")."""
    cwd_str = cwd if cwd is not None else str(repo_root)
    drop_invocation = f"pickup-assemble drop {artifact_path}"
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename

    def _no_claimant() -> dict[str, Any]:
        return {
            "verdict": "granted", "reason": "no competing claim", "holder": None,
            "holder_live": False, "held_by_self": False, "claim_age_minutes": None,
            "claim_stage": None, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    if not claims_dir.is_dir():
        return _no_claimant()

    holder_sid = None
    sid_file = claims_dir / "session_id"
    if sid_file.is_file():
        try:
            holder_sid = sid_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            holder_sid = None
    if not holder_sid:
        return _no_claimant()

    fm = fm or {}
    stage = _claims.claim_stage(claims_dir)

    try:
        self_holder = _liveness.claim_held_by_me(
            str(claims_dir), my_sid=_explicitly_scoped_session_id(), cwd=cwd_str
        )
    except (OSError, ValueError):
        self_holder = False

    if self_holder:
        return {
            "verdict": "granted", "reason": "you already hold this", "holder": holder_sid,
            "holder_live": True, "held_by_self": True, "claim_age_minutes": None,
            "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    if stage == _claims.CLAIM_STAGE_BRIEF and _claims.brief_lease_expired(claims_dir):
        return {
            "verdict": "granted-with-warning",
            "reason": (
                f"{holder_sid} reserved this at brief and never applied it — the "
                f"{_claims.BRIEF_CLAIM_LEASE_MINUTES}-minute brief-stage lease has elapsed, "
                "so the reservation is takeable"
            ),
            "holder": holder_sid, "holder_live": None, "held_by_self": False,
            "claim_age_minutes": None, "claim_stage": stage,
            "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    holder_live = _claim_holder_live(claims_dir, cwd_str, holder_sid)

    if holder_live:
        related = _lineage_related_sessions(repo_root, fm)
        if holder_sid in related:
            return {
                "verdict": "granted",
                "reason": (
                    f"held by {holder_sid} — that session authored this artifact or holds/"
                    "consumed one of its predecessors; a clean handover, not contention (AC3e)"
                ),
                "holder": holder_sid, "holder_live": True, "held_by_self": False,
                "claim_age_minutes": None, "claim_stage": stage,
                "drop_invocation": drop_invocation, "unclean_prior_holder": False,
            }
        return {
            "verdict": "denied", "reason": f"held by {holder_sid} — live", "holder": holder_sid,
            "holder_live": True, "held_by_self": False, "claim_age_minutes": None,
            "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": False,
        }

    return {
        "verdict": "granted-with-warning", "reason": f"held by {holder_sid}; that session is not live",
        "holder": holder_sid, "holder_live": False, "held_by_self": False, "claim_age_minutes": None,
        "claim_stage": stage, "drop_invocation": drop_invocation, "unclean_prior_holder": True,
    }


#: Claim-staleness settling window — a SEPARATE named constant from
#: `coordinator_core.session.liveness`'s own 30-minute recency window,
#: even though the two share a magnitude today. They answer different
#: questions: liveness asks "is this session alive?"; this constant asks
#: "has a dead holder been gone long enough that taking over is safe?".
#: Env override: COORDINATOR_CLAIM_STALE_AFTER_MINUTES.
CLAIM_STALE_AFTER_MINUTES = int(
    os.environ.get("COORDINATOR_CLAIM_STALE_AFTER_MINUTES", "30")
)


def _adopt_into_baton(
    repo_root: Path, artifact_path: str, fm: Optional[dict[str, Any]] = None
) -> None:
    """Ported verbatim (HEAD's own `_adopt_into_baton`) — record the
    artifact this pickup just claimed into THIS session's baton record's
    `adopted_artifacts[]` (dedup-extends, never replaces). Also names the
    baton from the artifact being adopted when the baton doesn't already
    carry a title or an intent. Fail-open throughout: any failure to
    resolve this session's id, or to read/write the baton store, is
    swallowed here — an advisory fan-in edge must never block a pickup."""
    try:
        sid = _session_core.resolve_session_id(str(repo_root))
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        return
    if not sid:
        return

    kwargs: dict[str, Any] = {
        "adopted_artifacts": [artifact_path],
        "closed_into": artifact_path,
    }
    if fm:
        try:
            _existing_baton = read_baton(sid, cwd=str(repo_root))
            already_named = bool(_existing_baton.get("title")) or bool(
                _existing_baton.get("intent")
            )
        except Exception:  # noqa: BLE001 — naming is best-effort, never load-bearing
            already_named = True
        if not already_named:
            title = fm.get("title")
            if title:
                kwargs["title"] = title
            intent = fm.get("session_goal")
            if intent:
                kwargs["intent"] = intent
            else:
                summary = fm.get("summary")
                if summary:
                    kwargs["intent"] = f"(from summary) {summary}"

    try:
        kwargs["closed_at"] = _session_core.now_iso()
        merge_baton(sid, cwd=str(repo_root), **kwargs)
    except Exception:  # noqa: BLE001 — advisory write must never raise into brief()
        pass


def route_baton_adoption(
    root: Path, artifact_path: str, fm: Optional[dict[str, Any]]
) -> None:
    """Ported verbatim (HEAD's own `route_baton_adoption`) — the `/pickup`
    adoption seam, called from both `claim_at_brief` call sites in
    `brief()` (handoff branch and memo branch). Unconditional plain
    adoption via `_adopt_into_baton`; the multi-baton fan-in inference this
    function used to route into was removed at HEAD (structurally unsound
    — see HEAD's own docstring) and stays gone here too. A genuine
    multi-baton fan-in has its own explicit, operator-invoked route
    (`/mise-en-place`), out of this module's scope."""
    _adopt_into_baton(root, artifact_path, fm)


def acquire_brief_claim(
    repo_root: Path, class_: str, basename: str, cwd: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Takes the brief-stage claim for a single-artifact invocation
    (`session.claims.claim_artifact`, stage=brief) — closes the
    2026-08-10 duplicate-memo read-verify-draft window.

    Returns a record of what the acquisition DISPLACED (a reclaim), or
    `None` when it displaced nothing (a fresh lock, a re-brief of a lock
    this session already holds, or a failed acquisition):

        {"holder": <the sid we took it from>,
         "basis": "expired-brief-lease" | "dead-holder" | "holder-absent"
                  | "holder-liveness-unknown",
         "claim_age_minutes": <age of the claim we displaced>,
         "liveness_basis": <the session.liveness basis behind "basis", or None>,
         "liveness_live": <the session_verdict boolean behind "basis", or None
                  when no verdict was available (see "holder-absent")>}

    `basis` is `"dead-holder"` only when `session_verdict` both ran and
    confirmed the prior holder's process gone (`stable-pid`, live=False);
    no verdict at all (no local dir, no registry record) is
    `"holder-absent"` — no process check ran, so it is never mislabeled a
    confirmed death; every other resolved-but-not-dead verdict
    (recency-only inference, the fail-open "unknown" arm, or a
    structurally-disagreeing `stable-pid`/live=True read) is
    `"holder-liveness-unknown"`. An expired brief-stage lease always wins
    (row 1), regardless of what liveness would otherwise have said.

    Best-effort: a failure to claim degrades to `None` (the returned
    `claim_grant` already carries the authoritative verdict; this is
    advisory reservation only)."""
    cwd_str = cwd or str(repo_root)
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename

    prior_holder: Optional[str] = None
    prior_age: Optional[int] = None
    prior_lease_expired = False
    prior_liveness_basis: Optional[str] = None
    prior_liveness_live: Optional[bool] = None
    if claims_dir.is_dir():
        try:
            prior_holder = (claims_dir / "session_id").read_text(encoding="utf-8").strip() or None
        except OSError:
            prior_holder = None
        prior_age = _claims.claim_age_minutes(claims_dir)
        prior_lease_expired = _claims.brief_lease_expired(claims_dir)
        if prior_holder:
            try:
                verdict = _liveness.session_verdict(prior_holder, cwd=cwd_str)
            except Exception:
                verdict = None
            if verdict is not None:
                prior_liveness_live, prior_liveness_basis = verdict[0], verdict[1]

    try:
        if _claims.touch_brief_claim(class_, basename, cwd=cwd_str):
            return None
        took_it = _claims.claim_artifact(class_, basename, cwd=cwd_str, stage=_claims.CLAIM_STAGE_BRIEF)
    except (OSError, ValueError):
        return None

    if not took_it or prior_holder is None:
        return None
    try:
        now_holder = (claims_dir / "session_id").read_text(encoding="utf-8").strip()
    except OSError:
        now_holder = ""
    if now_holder == prior_holder:
        return None

    if prior_lease_expired:
        basis = "expired-brief-lease"
    elif prior_liveness_basis == "stable-pid" and prior_liveness_live is False:
        basis = "dead-holder"
    elif prior_liveness_basis is None:
        basis = "holder-absent"
    else:
        basis = "holder-liveness-unknown"

    return {
        "holder": prior_holder,
        "basis": basis,
        "claim_age_minutes": prior_age,
        "liveness_basis": prior_liveness_basis,
        "liveness_live": prior_liveness_live,
    }


# ---------------------------------------------------------------------------
# preflight.tree_quiescence / gates.coast
# ---------------------------------------------------------------------------

_NO_CONSOLE = no_console_creationflags()


#: Cap on the candidate-path count `_expand_scope_entries` will settle
#: in-process before giving up and falling back to the spawn. Measured
#: 2026-09-12, this box, against this repo's own `coordinator_core/`
#: subtree (~3,900 tracked files): a raw `git status --porcelain --
#: coordinator_core/ ...` (5-entry pathspec incl. `coordinator_core/`)
#: is ~48ms wall including process creation, while this module's own
#: in-process reads over the SAME subtree cost more: `git_index.
#: scoped_status` alone ~58ms, plus `git_state.head_blobs`'s cold
#: tree-spine walk ~83ms, before any untracked-file directory walk is
#: even counted. Past this cap, git's C-level status walk is already
#: cheaper than re-deriving the same answer file-by-file in Python — the
#: cap keeps the fast path a genuine win rather than a slower
#: reimplementation, never a correctness boundary (the spawn fallback
#: answers identically either way).
_SCOPE_CANDIDATE_CAP = 512


class _ScopeExpansionUnsupported(Exception):
    """Internal signal: give up on the in-process fast path for this call
    and fall back to the original scoped `git status --porcelain` spawn,
    unchanged. Never surfaced to `compute_tree_quiescence`'s caller."""


def _walk_dir_into(root: Path, dir_path: Path, out: set[str]) -> None:
    for dirpath, dirnames, filenames in os.walk(dir_path):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            out.add((Path(dirpath) / fn).relative_to(root).as_posix())
        if len(out) > _SCOPE_CANDIDATE_CAP:
            return


def _expand_scope_entries(root: Path, scope_entries: list[str]) -> set[str]:
    """Expand `scope:` entries (literal files, directories, or globs) into
    concrete repo-relative candidate paths by walking the WORKTREE, never
    the index — this is what lets a new, still-untracked file under a
    scoped directory surface as a candidate at all, the same way a `git
    status --porcelain -- <dir>` pathspec would find it. Raises
    `_ScopeExpansionUnsupported` once the running candidate count passes
    `_SCOPE_CANDIDATE_CAP` (see that constant's docstring for the
    measurement backing the threshold) — the caller falls back to the
    spawn for the WHOLE call in that case, never a partial in-process
    answer."""
    candidates: set[str] = set()
    for entry in scope_entries:
        norm = str(entry).replace("\\", "/").strip()
        if not norm:
            continue
        abs_path = root / norm
        try:
            is_file = abs_path.is_file()
        except OSError:
            is_file = False
        if is_file:
            candidates.add(norm)
        else:
            try:
                is_dir = abs_path.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                _walk_dir_into(root, abs_path, candidates)
            elif any(ch in norm for ch in "*?["):
                for match in root.glob(norm):
                    if match.is_file():
                        candidates.add(match.relative_to(root).as_posix())
                    elif match.is_dir():
                        _walk_dir_into(root, match, candidates)
            else:
                # Not present on disk at all -- may be a deleted tracked
                # path (or a scope entry naming something that never
                # existed). Passed through literally, same as the spawn's
                # own pathspec would treat it.
                candidates.add(norm)
        if len(candidates) > _SCOPE_CANDIDATE_CAP:
            raise _ScopeExpansionUnsupported(
                f"scope entry {entry!r} expands past the "
                f"{_SCOPE_CANDIDATE_CAP}-path fast-path cap"
            )
    return candidates


def _spawn_status_subset(root: Path, paths: list[str]) -> Optional[list[str]]:
    """The escape-hatch spawn (mirrors `divergence.py::_spawn_diverging_
    subset`'s pattern): the same scoped `git status --porcelain` this
    module always issued, restricted to just the undetermined subset a
    reader declined or could not settle in-process. Returns `None` on any
    spawn failure so the caller can fall all the way back."""
    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + paths,
            cwd=str(root), capture_output=True, text=True, timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line[3:].strip() for line in proc.stdout.splitlines() if len(line) > 3]


def _settle_candidates_in_process(root: Path, candidates: set[str]) -> Optional[list[str]]:
    """Sorted dirty subset of `candidates`, or `None` when any reader here
    raises/declines and the whole call must fall back to the spawn.

    Combines the same two axes `git status --porcelain` itself reports:
    staged-vs-HEAD (`X`, via `git_state.read_index`/`head_blobs`, both
    already spawn-free) and worktree-vs-index (`Y`, via `git_index.
    scoped_status`'s stat fast path, settled for a stat-mismatch
    `"candidate"` by `content_hash.content_matches_index_sha`'s
    normalize-then-hash — never a raw-bytes hash, respecting the CRLF
    hazard that module's own docstring documents). A candidate `scoped_
    status` reports `"untracked"`, or one `content_matches_index_sha`
    DECLINES on (`None` — outside its verified precondition set, e.g. a
    non-`autocrlf=true` repo or a `filter=` attribute), is routed to the
    escape-hatch spawn (`_spawn_status_subset`) rather than guessed at:
    guessing "clean" there risks the one bar this row may never cross —
    "a quiet tree must still read quiet" is not worth a saved spawn."""
    if not candidates:
        return []
    ordered = sorted(candidates)
    try:
        index_snapshot = _git_state.read_index(root)
    except _git_state.IndexParseError:
        return None
    try:
        head = _git_state.head_blobs(root, [p for p in ordered if p in index_snapshot])
    except Exception:
        return None
    try:
        stat_verdicts = _git_index.scoped_status(root, ordered)
    except _git_index.IndexParseError:
        return None

    dirty: set[str] = set()
    undetermined: list[str] = []
    for p in ordered:
        entry = index_snapshot.get(p)
        head_entry = head.get(p)
        staged_dirty = entry is not None and (entry.mode, entry.sha) != head_entry

        verdict = stat_verdicts.get(p)
        if verdict == "untracked":
            undetermined.append(p)
            continue

        worktree_dirty = False
        if verdict == "deleted":
            worktree_dirty = True
        elif verdict == "candidate":
            match = content_matches_index_sha(root, p, entry.sha) if entry is not None else None
            if match is None:
                undetermined.append(p)
                continue
            worktree_dirty = not match
        # verdict == "clean" -> worktree_dirty stays False.

        if staged_dirty or worktree_dirty:
            dirty.add(p)

    if undetermined:
        spawned = _spawn_status_subset(root, sorted(undetermined))
        if spawned is None:
            return None
        dirty.update(spawned)

    return sorted(dirty)


#: A `scope:` entry naming a SIBLING repo — `<repo-id>: <path>`. The
#: `(?!//)` guard keeps a URL (`https://...`) from parsing as a repo-id.
_SCOPE_SIBLING_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+):(?!//)\s*(.+)$")


def _partition_scope_entries(
    scope_entries: list[str],
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Split `scope:` entries into this repo's own paths, per-sibling-repo
    paths, and entries that parse as neither.

    A `scope:` entry that is prose rather than a bare `<repo-id>: <path>`
    pair is detect-then-fail-loud into the unparseable bucket — never
    silently counted as dirty (the defect this replaces) and never silently
    dropped. A sibling entry must never reach this repo's own pathspec:
    `git status --porcelain -- "<repo-id>: <path>"` answers a question
    nobody asked.
    """
    local_paths: list[str] = []
    sibling_paths: dict[str, list[str]] = {}
    unparseable: list[str] = []
    for entry in scope_entries:
        match = _SCOPE_SIBLING_PREFIX_RE.match(entry)
        if match is None:
            local_paths.append(entry)
            continue
        repo_id, rest = match.group(1), match.group(2).strip()
        if not rest or " " in rest:
            unparseable.append(entry)
            continue
        sibling_paths.setdefault(repo_id, []).append(rest)
    return local_paths, sibling_paths, unparseable


def _scoped_porcelain_dirty(root: Path, paths: list[str]) -> list[str]:
    """`git status --porcelain` restricted to `paths`, as a list of
    repo-relative dirty paths. Short-circuits on an empty pathspec before
    ever invoking `git` — an unscoped call must never widen to the whole
    worktree (see `compute_tree_quiescence`'s own docstring on the
    `.git/index` stat-cache side effect that would cause)."""
    if not paths:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + paths,
            cwd=str(root), capture_output=True, text=True, timeout=10,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [line[3:].strip() for line in proc.stdout.splitlines() if len(line) > 3]


def compute_tree_quiescence(root: Path, scope_entries: list[str]) -> dict[str, Any]:
    """`preflight.tree_quiescence` — the ONE git fact on this brief's path
    (§ Design, "Git: one fact only"). For a bounded `scope:` (directories/
    globs/files expanding to at most `_SCOPE_CANDIDATE_CAP` worktree
    paths), settles ZERO-spawn in the common case via `_expand_scope_
    entries` + `_settle_candidates_in_process` — the SAME `git status
    --porcelain` answer, read off `git_state`/`git_index`/`content_hash`'s
    already-verified-correct in-process machinery instead of a process.

    An UNSCOPED call (no `scope:` declared at all — `scope_entries` empty)
    reports `quiet` with NO git spawn whatsoever (mirrors the monolith's
    own `_porcelain_dirty_paths`, which short-circuits on an empty
    pathspec list before ever invoking `git`) — never the whole-worktree
    `git status --porcelain -- .` a naive fallback would run. That spawn,
    besides being unbounded on this brief's 500ms bar, updates `.git/
    index`'s on-disk stat-cache mtime as a side effect even though it
    writes no tracked content, which is exactly what `test_read_only_
    invariant.py`'s byte-for-byte `.git/` snapshot equality catches: a
    `brief()` with no `scope:` at all must leave `.git/` untouched, not
    merely uncommitted-content-unchanged.

    A scope past the cap, or any in-process reader declining/raising,
    falls back to the ORIGINAL scoped `git status --porcelain` spawn
    unchanged — this is an optimisation with an escape hatch, never a
    narrowing of what this function can answer (see `_SCOPE_CANDIDATE_
    CAP`'s docstring for why a very wide scope is cheaper left to git's
    own C-level walk)."""
    if not scope_entries:
        return {"verdict": "quiet", "repos": [{"repo": ".", "dirty": [], "unparseable_scope_entries": []}]}

    local_paths, sibling_paths, unparseable = _partition_scope_entries(list(scope_entries))

    local_dirty: Optional[list[str]] = None
    if local_paths:
        try:
            candidates = _expand_scope_entries(root, local_paths)
        except _ScopeExpansionUnsupported:
            candidates = None
        if candidates is not None:
            local_dirty = _settle_candidates_in_process(root, candidates)
    if local_dirty is None:
        local_dirty = _scoped_porcelain_dirty(root, local_paths)

    dirty_found = bool(local_dirty)
    repos: list[dict[str, Any]] = [
        {"repo": ".", "dirty": local_dirty, "unparseable_scope_entries": unparseable}
    ]

    # Sibling repos are settled in-process only — never a `git status` spawn
    # per sibling, which is a fan-out over the scope's repo count. A sibling
    # the in-process reader cannot settle (a scope past `_SCOPE_CANDIDATE_CAP`,
    # or an index it declines to read) is reported as unsettled rather than
    # silently counted quiet: fail loud, as for an unresolvable repo-id.
    for repo_id, rel_paths in sibling_paths.items():
        resolved = registry_get(f"repos.{repo_id.replace('-', '_')}")
        if not resolved:
            repos[0]["unparseable_scope_entries"].extend(f"{repo_id}: {p}" for p in rel_paths)
            continue
        sibling_root = Path(resolved)
        try:
            sibling_candidates = _expand_scope_entries(sibling_root, rel_paths)
        except _ScopeExpansionUnsupported:
            sibling_candidates = None
        sibling_dirty = (
            _settle_candidates_in_process(sibling_root, sibling_candidates)
            if sibling_candidates is not None else None
        )
        if sibling_dirty is None:
            repos[0]["unparseable_scope_entries"].extend(
                f"{repo_id}: {p} (not settled in-process)" for p in rel_paths
            )
            continue
        if sibling_dirty:
            dirty_found = True
        repos.append({"repo": str(sibling_root), "dirty": sibling_dirty, "unparseable_scope_entries": []})

    return {"verdict": "dirty" if dirty_found else "quiet", "repos": repos}


def _resolve_lineage_artifact_path(repo_root: Path, relative_path: str) -> Optional[Path]:
    """Archive-aware resolution of a lineage pointer (e.g. `continued_into`)
    — routed through `dag.resolve_target`, the same path/basename/archive
    tiers every sibling consumer already uses, rather than a bare
    `repo_root / relative_path` join, which only ever hits a still-live
    file. Returns `None` when the reference is absent, unresolvable, or
    resolves only to the `'git-history'` sentinel."""
    resolved = dag.resolve_target(
        relative_path, str(repo_root / "state" / "handoffs"), str(repo_root),
        include_history_tier=False,
    )
    if not resolved or resolved == "git-history":
        return None
    return Path(resolved)


def compute_supersession_gate(root: Path, artifact_path: str, fm: dict[str, Any]) -> Optional[dict[str, Any]]:
    """`gates.supersession` — residency in `state/handoffs/` is not
    pickupability. A `deployment_state: continued` predecessor is
    correctly RETAINED on disk while its successor is still `in_flight`
    (archival's own reverse-membership rule), but that retention is not a
    license to brief it as an ordinary live pickup target. Returns `None`
    (gate inert) unless `fm["deployment_state"] == "continued"`."""
    if fm.get("deployment_state") != "continued":
        return None

    continued_into = fm.get("continued_into") or None
    successor_resolved = _resolve_lineage_artifact_path(root, continued_into) if continued_into else None
    dangling = successor_resolved is None
    successor_path = (
        str(successor_resolved.relative_to(root)).replace("\\", "/")
        if successor_resolved is not None else None
    )

    if dangling:
        if continued_into is None:
            question = (
                f"{artifact_path} is marked deployment_state: continued but has no "
                "continued_into pointer at all — the field is missing or blank. Pick up "
                "this predecessor anyway, or treat the missing pointer as broken and "
                "investigate?"
            )
            evidence = "continued_into: absent/blank — deployment_state: continued with no successor recorded"
            narration = (
                f"{artifact_path} is marked deployment_state: continued but continued_into "
                "is missing or blank — this predecessor was superseded but no successor "
                "pointer was ever recorded."
            )
            next_move = (
                "Populate the missing continued_into pointer by hand before picking up this "
                "predecessor — do not treat residency in state/handoffs/ as license to act on it."
            )
        else:
            question = (
                f"{artifact_path} is stamped continued_into: {continued_into!r}, but that "
                "successor does not resolve on disk (or in any archive dir) — the pointer "
                "is dangling. Pick up this predecessor anyway, or treat the pointer as "
                "broken and investigate?"
            )
            evidence = f"continued_into: {continued_into!r} — dangling, does not resolve"
            narration = (
                f"{artifact_path} is marked deployment_state: continued with a dangling "
                f"continued_into pointer ({continued_into!r} does not resolve on disk or "
                "in any archive dir) — this predecessor was superseded but its successor "
                "cannot be found."
            )
            next_move = (
                "Resolve the dangling continued_into pointer by hand before picking up this "
                "predecessor — do not treat residency in state/handoffs/ as license to act on it."
            )
    else:
        question = (
            f"{artifact_path} is stamped continued_into: {successor_path} — this baton was "
            "superseded. Pick up the successor instead, or deliberately proceed on this "
            "predecessor?"
        )
        evidence = f"continued_into: {continued_into!r} -> resolves to {successor_path}"
        narration = (
            f"{artifact_path} is deployment_state: continued, superseded by {successor_path} "
            "— residency in state/handoffs/ is not pickupability."
        )
        next_move = f"Pick up {successor_path} instead of this superseded predecessor."

    jp = _shared_build_judgment_point(
        None,
        id="j-supersession",
        question=question,
        evidence=evidence,
        dispositions=[
            {
                "value": "redirect-to-successor", "resolves": [],
                "guidance": (
                    "Re-run brief against the successor path instead of this superseded "
                    "predecessor." if not dangling else
                    "No successor resolves — this disposition is unavailable until the "
                    "dangling pointer is repaired."
                ),
            },
            {
                "value": "proceed-anyway", "resolves": [],
                "guidance": (
                    "Deliberately read/act on this predecessor despite the supersession — "
                    "the caller has seen the pointer and means it."
                ),
            },
        ],
        reason="insufficient-evidence",
    )
    return {
        "gate": {
            "continued_into": continued_into,
            "successor_resolves": not dangling,
            "successor_path": successor_path,
            "verdict": "blocked",
        },
        "judgment_point": jp,
        "narration": narration,
        "next_move": next_move,
    }


def compute_coast(
    judgment_points: list[dict[str, Any]],
    claim_grant: Optional[dict[str, Any]] = None,
    tree_quiescence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`gates.coast` — reports what the EM is holding; never gates the
    claim itself (`gates.claim_grant`'s job)."""
    blocking_jps = [jp for jp in judgment_points if jp.get("id")]
    blocked_by = [jp["id"] for jp in blocking_jps]
    notes: list[str] = []
    verdict = "blocked" if blocked_by else "clear"

    grant = claim_grant or {}
    if grant.get("verdict") == "denied":
        verdict = "blocked"
    elif grant.get("verdict") == "granted-with-warning":
        notes.append("claim granted with warning: " + str(grant.get("reason", "stale claim")))

    if tree_quiescence and tree_quiescence.get("verdict") == "dirty":
        notes.append("tree not quiet: uncommitted changes in scoped paths")

    result: dict[str, Any] = {"verdict": verdict, "notes": notes, "blocked_by": blocked_by}

    # AC8 self-sufficiency: a caller reading ONLY gates.coast must learn WHY
    # it is blocked and what unblocks it, without cross-referencing
    # judgment_points[] — reason/remedy are derived here from the actual
    # blocking judgment point(s)' own bodies. Multiple simultaneous blocking
    # judgment points are all enumerated, never just the first.
    if blocking_jps:
        reason_parts: list[str] = []
        remedy_parts: list[str] = []
        for jp in blocking_jps:
            jp_id = jp.get("id", "?")
            question = jp.get("question")
            if question:
                reason_parts.append(f"{jp_id}: {question}")
            unblocking_values = [
                d.get("value") for d in jp.get("dispositions", []) if d.get("resolves") and d.get("value")
            ]
            if unblocking_values:
                remedy_parts.append(f"{jp_id}: resolve via {' or '.join(unblocking_values)}")
        if reason_parts:
            result["reason"] = "; ".join(reason_parts)
        if remedy_parts:
            result["remedy"] = "; ".join(remedy_parts)

    return result


# ---------------------------------------------------------------------------
# gates.liveness_signal
# ---------------------------------------------------------------------------

def compute_liveness_signal(
    repo_root: Path, fm: dict[str, Any], self_session_id: Optional[str] = None
) -> bool:
    """`gates.liveness_signal` — a bare `bool` (HEAD parity: the monolith's
    own `compute_liveness_signal` returns `bool`, not a dict). Reduced port
    of HEAD's frontmatter claim-stamp state machine (see that function's own
    docstring): a class-appropriate durable stamp
    (`claimed_by`/`consumed_by`/`picked_up_by`) naming a session that is
    neither this session nor lineage-related, and IS live
    (`session.liveness.session_live`), fires `True`. No stamp, a
    self/lineage-related stamp, or a stamp whose session cannot be confirmed
    live, fires `False`.

    Documented reduction: does not reproduce HEAD's ledger-first stamp
    resolution (`_resolve_ledger_first_holder`) or the cross-repo
    `harness-registry-elsewhere` arm (`session_verdict`) — both fold into
    this module's separate `gates.claim_grant`/`unclean_prior_holder`
    mechanism instead, which this function does not consult (the two are
    independent producers in HEAD too)."""
    related = set(_lineage_related_sessions(repo_root, fm))
    self_sid = self_session_id if self_session_id is not None else _explicitly_scoped_session_id()
    if self_sid:
        related.add(str(self_sid))

    stamped_sid: Optional[str] = None
    for key in ("claimed_by", "consumed_by", "picked_up_by"):
        v = fm.get(key)
        if v:
            stamped_sid = str(v)
            break

    if not stamped_sid or stamped_sid in related:
        return False
    try:
        return bool(_liveness.session_live(stamped_sid, str(repo_root)))
    except (OSError, ValueError):
        return False


def build_liveness_judgment_point(
    liveness_signal_fired: bool, evidence_pointer: str, resolves: list[str]
) -> Optional[dict[str, Any]]:
    """Ported verbatim (HEAD's own `build_liveness_judgment_point`) — the
    single JUDGMENT entry every mechanical liveness-signal computation
    feeds. `id` is always `j1`; `resolves` (the directive ids a `proceed`
    disposition unblocks) is threaded straight into the `proceed`
    disposition's own `resolves` list — never collapsed to a bare bool.
    No longer `revalidate_at_dispatch: true` (chunk C7 Part A4 — HEAD
    parity): `compute_liveness_signal` reads a durable committed
    frontmatter stamp, stable across the brief-to-apply gap, so a recorded
    `proceed` disposition here is honored rather than discarded and
    recomputed at dispatch."""
    if not liveness_signal_fired:
        return None
    return build_judgment_point(
        "j1",
        "Any peer live on this handoff/plan? Stand down?",
        evidence_pointer,
        [
            {"value": "proceed", "resolves": resolves},
            {"value": "stand-down-and-surface", "resolves": []},
        ],
        None,
        reason="insufficient-evidence",
    )


# ---------------------------------------------------------------------------
# gates.execution_stamp_match
# ---------------------------------------------------------------------------

_RATIFICATION_LINE_RE = re.compile(
    r"^[+-]\s*execution_authorized_(?:by|at|sha|note)\s*:", re.IGNORECASE
)
_STATUS_LINE_RE = re.compile(r"^[+-]\*\*Status:?\*\*")


def _extract_plan_to_execute_pointer(body_text: str) -> Optional[str]:
    m = re.search(r"^##\s*Plan to Execute\s*$\n+(?:.*?\[.*?\]\(([^)]+)\)|.*?`([^`]+\.md)`)",
                   body_text, re.MULTILINE)
    if not m:
        return None
    return m.group(1) or m.group(2)


_PLAN_DIRS_CACHE: tuple[str, ...] = ()


def _plan_dirs() -> tuple[str, ...]:
    """Ported verbatim from HEAD (`pickup_assemble._plan_dirs`): the
    trailing-slash directory prefixes a plan document lives under."""
    global _PLAN_DIRS_CACHE
    if not _PLAN_DIRS_CACHE:
        from coordinator_core.workstream_complete.directives_lessons_plan import (
            _GOVERNING_PLAN_GLOB_DIRS,
        )

        _PLAN_DIRS_CACHE = tuple(f"{d}/" for d in _GOVERNING_PLAN_GLOB_DIRS)
    return _PLAN_DIRS_CACHE


def _artifact_is_a_plan(artifact_path: str) -> bool:
    """Ported verbatim from HEAD (`pickup_assemble._artifact_is_a_plan`):
    True iff `artifact_path` names a plan document by its location. Guards
    `compute_execution_stamp_match`'s own-body fallback — a handoff
    mirroring its plan's `execution_authorized_sha` (without a `## Plan to
    Execute` pointer or `governing_plan:`) satisfies the no-pointer
    condition without being the plan itself, and must fall through to "no
    pointer, nothing to verify" (`None`) rather than hashing the handoff's
    own body against the plan's mirrored stamp."""
    normalized = artifact_path.replace(chr(92), "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if any(part == ".." for part in normalized.split("/")):
        return False
    return normalized.startswith(_plan_dirs())


def _read_file_at_revision(repo_root: Path, revision: str, path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "show", f"{revision}:{path}"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _find_stamp_commit(repo_root: Path, path: str, stamped_sha: str) -> Optional[str]:
    """Ported verbatim from HEAD (`pickup_assemble._find_stamp_commit`): the
    commit `git log -S<stamped_sha>` names as having last changed the
    occurrence count of the stamped literal in `path`. Real `git` spawn,
    off the zero-spawn hot path — only reached once an
    `execution_authorized_sha`/pointer is already present."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--follow", f"-S{stamped_sha}", "--format=%H", "--", path],
            capture_output=True,
            text=True,
            timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _is_bookkeeping_diff_line(line: str) -> bool:
    if _RATIFICATION_LINE_RE.match(line):
        return True
    if _STATUS_LINE_RE.match(line):
        return True
    if not line[1:].strip():
        return True
    return False


def _classify_stamp_delta(repo_root: Path, stamp_commit: str, path: str) -> str:
    """Ported verbatim from HEAD (`pickup_assemble._classify_stamp_delta`):
    every changed content line in `stamp_commit..HEAD -- path` must be a
    ratification-line, a `**Status:**` line, or blank to count as
    `bookkeeping`; anything else defaults to `substantive`."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "diff", f"{stamp_commit}..HEAD", "--", path],
            capture_output=True, text=True, timeout=30,
            **_NO_CONSOLE,
        )
    except (OSError, subprocess.SubprocessError):
        return "substantive"
    if result.returncode != 0:
        return "substantive"
    saw_change = False
    for line in result.stdout.splitlines():
        if line.startswith(("+++", "---", "diff --git", "index ", "@@")):
            continue
        if not line or line[0] not in "+-":
            continue
        saw_change = True
        if not _is_bookkeeping_diff_line(line):
            return "substantive"
    return "bookkeeping" if saw_change else "substantive"


def compute_execution_stamp_match(
    repo_root: Path, fm: dict[str, Any], artifact_path: str
) -> Optional[tuple[dict[str, Any], str]]:
    """`gates.execution_stamp_match`. Ported from HEAD's
    `compute_execution_stamp_match`: `computed_sha` via
    `frontmatter.primitives.canonical_body_sha` (pure Python, zero spawns);
    `stamp_commit`/`delta_class` (`stale-bookkeeping`/`stale-substantive`)
    classification restored via `_find_stamp_commit`/`_classify_stamp_delta`
    (real `git` spawns, off the zero-spawn hot path — only reached once a
    stamp/pointer is already present). Returns `(gate, target_path)` on a
    hit, `None` when the artifact carries no stamp to check."""
    live_path = repo_root / artifact_path
    if not live_path.is_file():
        return None
    try:
        artifact_text = live_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    target_rel_path = artifact_path
    target_text = artifact_text
    split = split_frontmatter(artifact_text)
    body_text = split.body_with_leading_newline if split is not None else artifact_text
    pointer = _extract_plan_to_execute_pointer(body_text) or fm.get("governing_plan")

    if pointer:
        plan_abs = repo_root / pointer
        if not plan_abs.is_file():
            return None
        try:
            plan_text = plan_abs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        plan_split = split_frontmatter(plan_text)
        if plan_split is None:
            return None
        stamped_sha = read_fm_field_unquoted(plan_split.fm_text, "execution_authorized_sha")
        if not stamped_sha:
            return None
        target_rel_path = pointer
        target_text = plan_text
    elif _artifact_is_a_plan(artifact_path):
        stamped_sha = fm.get("execution_authorized_sha")
        if not stamped_sha:
            return None
    else:
        # No pointer of either shape, and the artifact is not itself a plan
        # (e.g. a handoff mirroring its plan's execution_authorized_sha on
        # its own frontmatter for human readability, with no `## Plan to
        # Execute`/`governing_plan:` pointer). Nothing to verify — HEAD
        # deliberately does NOT fall back to hashing the artifact's own
        # body against the mirrored value (see `_artifact_is_a_plan`'s
        # docstring): that comparison is always against the wrong document.
        return None

    computed_sha = canonical_body_sha(target_text)
    if computed_sha is None:
        return None

    stamp_commit = _find_stamp_commit(repo_root, target_rel_path, stamped_sha)

    if computed_sha == stamped_sha:
        return (
            {
                "verdict": "match",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": stamp_commit,
                "delta_class": None,
                "next_move": "Execution authorization stamp matches the current plan body — proceed.",
            },
            target_rel_path,
        )

    if stamp_commit is None:
        return (
            {
                "verdict": "unstampable",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": None,
                "delta_class": None,
                "next_move": (
                    f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} "
                    "— no commit in this file's history introduced the recorded value by the "
                    "canonical recipe."
                ),
            },
            target_rel_path,
        )

    stamp_body_text = _read_file_at_revision(repo_root, stamp_commit, target_rel_path)
    stamp_computed_sha = (
        canonical_body_sha(stamp_body_text) if stamp_body_text is not None else None
    )
    body_invariant_since_stamp = (
        stamp_computed_sha is not None and stamp_computed_sha == computed_sha
    )
    reproduces_at_stamp_commit = stamp_computed_sha == stamped_sha

    if body_invariant_since_stamp and not reproduces_at_stamp_commit:
        return (
            {
                "verdict": "unstampable",
                "stamped_sha": stamped_sha,
                "computed_sha": computed_sha,
                "stamp_commit": stamp_commit,
                "delta_class": None,
                "next_move": (
                    f"Re-stamp execution_authorized_sha on {target_rel_path} to {computed_sha} "
                    "— the recorded value never reproduces the canonical recipe even at its own "
                    "stamp commit, and the plan body is unchanged since then: a mis-computed "
                    "stamp, not a body edit."
                ),
            },
            target_rel_path,
        )

    delta_class = _classify_stamp_delta(repo_root, stamp_commit, target_rel_path)
    if delta_class == "bookkeeping":
        verdict = "stale-bookkeeping"
        next_move = (
            f"Authorization stands on {target_rel_path} — proceed WITHOUT re-stamping. "
            "Every line changed since the stamp commit is a ratification field, a "
            "`**Status:**` line, or blank; re-stamping would only re-sync the hash to a "
            "body whose sole change was the hash, and would move the comparison base "
            "forward so a later substantive edit is measured against less history."
        )
    else:
        verdict = "stale-substantive"
        next_move = (
            "Surface to the PM before proceeding — the plan changed target, scope, or "
            "acceptance criteria since it was stamped; re-authorization is a PM call."
        )
    return (
        {
            "verdict": verdict,
            "stamped_sha": stamped_sha,
            "computed_sha": computed_sha,
            "stamp_commit": stamp_commit,
            "delta_class": delta_class,
            "next_move": next_move,
        },
        target_rel_path,
    )


def build_execution_stamp_judgment_point(gate: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The tier-3 `judgment_points[]` entry for `stale-substantive` — ported
    verbatim from HEAD's `build_execution_stamp_judgment_point`. `unstampable`
    promotes to `build_execution_stamp_directive` instead (tier-1, at the
    call site); `match`/`stale-bookkeeping`/`None` contribute nothing."""
    if gate.get("verdict") != "stale-substantive":
        return None
    return _shared_build_judgment_point(
        None,
        id="jstamp",
        question=(
            "The plan changed since its execution_authorized_sha stamp — bookkeeping drift, "
            "or a substantive change requiring re-authorization?"
        ),
        dispositions=[
            {"value": "re-authorized-proceed", "resolves": []},
            {"value": "surface-to-PM", "resolves": []},
        ],
        evidence="gates.execution_stamp_match",
        reason="insufficient-evidence",
        revalidate_at_dispatch=False,
    )


# ---------------------------------------------------------------------------
# gates.shipped_state
# ---------------------------------------------------------------------------

def compute_shipped_state(fm: dict[str, Any]) -> Optional[dict[str, Any]]:
    """`gates.shipped_state` (HEAD parity) — fires ONLY when a live handoff's
    `deployment_state` is `"shipped"` (telling a peer not to redo finished
    work); `None` otherwise, including for a memo (HEAD never computes this
    for memo classification). Not the `shipped_in` value-grammar reading the
    module docstring originally described — that was this module's own,
    unkept design; HEAD's kept `gates.shipped_state` is this narrower fact."""
    if fm.get("deployment_state") != "shipped":
        return None
    return {"deployment_state": "shipped", "shipped_in": fm.get("shipped_in")}


_BLOCKER_HOLDER_FIELDS = ("claimed_by", "consumed_by", "picked_up_by")


def _blocker_holder(record: dict[str, Any]) -> Optional[str]:
    """Ported verbatim (HEAD's own `_blocker_holder`)."""
    for key in _BLOCKER_HOLDER_FIELDS:
        value = record.get(key)
        if value:
            return str(value)
    return None


def compute_gate_blocker_evidence(
    repo_root: Path, blocked_by: Optional[list[Any]]
) -> list[dict[str, Any]]:
    """Ported (HEAD's own `compute_gate_blocker_evidence`) — a PM-ruled kept
    behaviour, not one of DR-415's eight deletions (plan 2026-08-30-the-
    gate-brief-reads-a-list-where-the-record-wrote-one, chunk C2/C3; see
    test_gate_check_shipped_blocker_evidence.py's own docstring, which
    names the deliberate retirement of the earlier "never a corpus walk"
    bound). One bounded index build
    (`reconcile.handoff_corpus._build_blocker_index`) over the live+
    archived corpus, then a dict-hit lookup per id, resolved to a
    continuation-chain head via `reconcile.gate_eval.collapse_to_chain_
    heads` — never a per-id corpus walk."""
    if not blocked_by:
        return []

    if isinstance(blocked_by, str):
        raw = blocked_by.strip()
        if raw.startswith("[") or raw.endswith("]"):
            return [
                {
                    "id": raw,
                    "status": "unresolvable",
                    "resolved": False,
                    "deployment_state": None,
                    "holder": None,
                    "holder_address": None,
                    "holder_live": None,
                }
            ]
        blocked_by = [raw]

    from coordinator_core.reconcile.gate_eval import collapse_to_chain_heads
    from coordinator_core.reconcile.handoff_corpus import _build_blocker_index

    index, scan_errors = _build_blocker_index(repo_root)
    scan_incomplete = bool(scan_errors)

    results: list[dict[str, Any]] = []
    for raw_id in blocked_by:
        blocker_id = str(raw_id)
        entry: dict[str, Any] = {
            "id": blocker_id,
            "status": "unresolvable",
            "resolved": False,
            "deployment_state": None,
            "holder": None,
            "holder_address": None,
            "holder_live": None,
        }

        paths = index.get(blocker_id)
        if not paths:
            if scan_incomplete:
                entry["status"] = "scan_incomplete"
            results.append(entry)
            continue

        records: list[dict[str, Any]] = []
        unreadable = False
        for path in paths:
            meta = dag._read_meta(str(path))
            if not meta:
                try:
                    path.read_bytes()
                except OSError:
                    unreadable = True
                continue
            meta = dict(meta)
            meta["_path"] = str(path)
            records.append(meta)

        if not records:
            entry["status"] = (
                "scan_incomplete" if (scan_incomplete or unreadable) else "unresolvable"
            )
            results.append(entry)
            continue

        heads = collapse_to_chain_heads(records)
        if len(heads) > 1:
            entry["status"] = "ambiguous"
            results.append(entry)
            continue

        head = heads[0]
        entry["status"] = "resolved"
        entry["resolved"] = True
        entry["deployment_state"] = head.get("deployment_state")
        holder = _blocker_holder(head)
        entry["holder"] = holder
        if holder:
            try:
                from coordinator_core.session import reachability

                address = reachability.resolve_advisory_address(holder)
            except Exception:
                address = ""
            entry["holder_address"] = address or ""
            entry["holder_live"] = bool(address)
        results.append(entry)

    return results


def compute_gate_check_recommendation(blockers: list[dict[str, Any]]) -> dict[str, str]:
    """Ported verbatim (HEAD's own `compute_gate_check_recommendation`)."""
    if not blockers:
        return {
            "disposition": "unresolved",
            "rationale": "gate_check.blockers is empty — no blocked_by id to resolve.",
        }
    resolved = [b for b in blockers if b.get("status") == "resolved"]
    if not resolved:
        ids = ", ".join(str(b.get("stub_id", "<unknown>")) for b in blockers)
        return {
            "disposition": "unresolved",
            "rationale": f"None of blocked_by ids ({ids}) resolved to a record — cannot judge cleared/not-cleared.",
        }
    try:
        from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
    except ImportError:
        HANDOFF_TERMINAL_DEPLOYMENT = frozenset()
    non_terminal = [
        b for b in blockers
        if not (b.get("status") == "resolved" and b.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT)
    ]
    if not non_terminal:
        named = ", ".join(f"{b.get('stub_id')} ({b.get('deployment_state')})" for b in blockers)
        return {"disposition": "cleared", "rationale": f"Every blocked_by id resolved terminal: {named}."}
    unchecked = [b for b in non_terminal if b.get("status") == "scan_incomplete"]
    confirmed_open = [b for b in non_terminal if b.get("status") != "scan_incomplete"]
    parts: list[str] = []
    for blocker in confirmed_open:
        state = blocker.get("deployment_state") or blocker.get("status")
        holder_bit = ""
        if blocker.get("holder"):
            holder_bit = f", held by {blocker['holder']}"
            if blocker.get("holder_address"):
                holder_bit += f" at {blocker['holder_address']}"
        parts.append(f"{blocker.get('stub_id', '<unknown>')} ({state}{holder_bit})")
    clauses: list[str] = []
    if parts:
        clauses.append("Still open: " + "; ".join(parts) + ".")
    if unchecked:
        unchecked_ids = ", ".join(str(b.get("stub_id", "<unknown>")) for b in unchecked)
        clauses.append(f"Could not be checked (scan_incomplete, treated as not-cleared): {unchecked_ids}.")
    return {"disposition": "not-cleared", "rationale": " ".join(clauses)}


_JGATE_CLEARED_GUIDANCE = (
    "Answer from gates.gate_check.blocked_by / .blocking_notes / "
    ".gate_evidence, not gate_dependency prose. blocked_by is retired "
    "separately by reconcile-open, not by clearing."
)
_JGATE_NOT_CLEARED_GUIDANCE = (
    "Leave deployment_state:awaiting_gate — claim-handoff will not fire. "
    "Re-run pickup-assemble once the blocker resolves."
)


def build_gate_check_judgment_point(
    evidence_pointer: str, resolves: list[str], recommendation: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """Ported verbatim (HEAD's own `build_gate_check_judgment_point`)."""
    dispositions = [
        {"value": "cleared", "resolves": resolves, "guidance": _JGATE_CLEARED_GUIDANCE},
        {"value": "not-cleared", "resolves": [], "guidance": _JGATE_NOT_CLEARED_GUIDANCE},
    ]
    if recommendation is not None:
        return build_judgment_point(
            "jgate", "Has this awaiting_gate handoff's gate actually cleared?",
            evidence_pointer, dispositions, recommendation,
        )
    return build_judgment_point(
        "jgate", "Has this awaiting_gate handoff's gate actually cleared?",
        evidence_pointer, dispositions, None, reason="insufficient-evidence",
    )


def build_gate_recheck_directive(artifact_path: str) -> dict[str, Any]:
    """Ported verbatim (HEAD's own `build_gate_recheck_directive`)."""
    return {
        "id": "d-gate-recheck",
        "cli": "archive-stamp-cli",
        "args": ["gate-recheck-handoff", artifact_path, date.today().isoformat()],
        "depends_on": "jgate",
        "already_satisfied": False,
    }


def build_shipped_state_judgment_point(evidence_pointer: str, resolves: list[str]) -> dict[str, Any]:
    """The `deployment_state: shipped` JUDGMENT entry (2026-07-25 defect fix —
    a handoff already stamped shipped previously briefed as freely
    dispatchable, `gates.coast.verdict == "clear"`, `judgment_points: []`,
    telling a peer session to redo finished work). A shipped baton is
    presumptively done, but re-opening one is legitimate (a fix regressed,
    or the stamp landed prematurely) — so this never hard-blocks; it
    surfaces a judgment point the EM resolves, mirroring
    `build_gate_check_judgment_point`'s shape for the sibling
    `awaiting_gate` deployment_state. `compute_coast` blocks on ANY
    `judgment_points[]` entry carrying an `id` (this one always does), so
    `gates.coast.verdict` is never `"clear"` while this is unresolved —
    closing the exact silent-coast-is-clear gap the defect named.

    Tier: `insufficient-evidence` — `deployment_state`/`shipped_in` are
    engine-read frontmatter fields (not a quote of untrusted body text), but
    whether the shipped stamp still reflects reality — has the fix since
    regressed, was the stamp premature — is exactly the read this module
    never mechanizes."""
    return build_judgment_point(
        "jshipped",
        "This handoff is already stamped deployment_state: shipped — reopen "
        "it, or stand down and leave it closed?",
        evidence_pointer,
        [
            {
                "value": "reopen-and-proceed",
                "resolves": resolves,
                "guidance": (
                    "Treat the shipped stamp as stale or premature — the work needs "
                    "further action after all (a fix regressed, or the ship landed "
                    "before the baton was genuinely done). Check `shipped_in` (when "
                    "present) against what actually landed on disk/git before "
                    "proceeding, and record why this baton is being reopened."
                ),
            },
            {
                "value": "confirm-shipped-stand-down",
                "resolves": [],
                "guidance": (
                    "Confirm the shipped stamp is accurate — this baton is "
                    "genuinely done. Stand down; do not claim it. The "
                    "handoff sitting in state/handoffs/ awaiting an archival sweep "
                    "is expected (ship-handoff retains it in place for later "
                    "archival, per `handoff_archive_transition`'s stamp_shipped "
                    "mode) — not a sign it needs picking up."
                ),
            },
        ],
        None,
        reason="insufficient-evidence",
    )


# ---------------------------------------------------------------------------
# gates.sender_reachability / gates.addressee
# ---------------------------------------------------------------------------

_SENT_BY_UNRESOLVED = "unresolved"


def compute_sender_reachability(sent_by: Optional[str]) -> dict[str, Any]:
    """`gates.sender_reachability` — HEAD parity: `{}` when `sent_by` is
    falsy, else `{"outcome", "message", "address", "resolved_at"}` via
    `coordinator_core.session.reachability.resolve_address` (a leaf
    session-registry read, NOT the monolith itself). Advisory only — any
    resolution failure degrades to `not_reachable`, never raises."""
    if not sent_by:
        return {}
    resolved_at = _dt.now(_tz.utc).isoformat()
    if sent_by == _SENT_BY_UNRESOLVED:
        return {
            "outcome": "sender_unresolved",
            "message": "This memo's sender identity was never resolved at send time — no reachability to compute.",
            "address": "",
            "resolved_at": resolved_at,
        }
    try:
        from coordinator_core.session import reachability
        result = reachability.resolve_address(sent_by)
    except Exception:
        result = None

    outcome = result.outcome if result is not None else "not_reachable"
    address = ""
    if outcome == "own_session":
        message = "This memo was sent by this same session — a self-receipt, not a reply target."
    elif outcome == "reachable":
        address = result.address or ""
        if address and address != "<this session>":
            message = f"Sender is reachable — reply via SendMessage to {address}."
        else:
            outcome = "not_reachable"
            address = ""
            message = "Sender's session has ended — action this the normal way."
    elif outcome == "ambiguous":
        message = "Sender's session id matches more than one live session — reachability is ambiguous, not confirmed."
    else:
        outcome = "not_reachable"
        message = "Sender's session has ended — action this the normal way."

    return {"outcome": outcome, "message": message, "address": address, "resolved_at": resolved_at}


def compute_addressee_gate(repo_root: Path, to_value: Optional[str]) -> dict[str, Any]:
    """`gates.addressee` (memo only) — HEAD parity via the same in-process
    `memo.check_addressee` compute core HEAD consumes
    (`coordinator_core.ops.fleet.memo_check_addressee`, a leaf module — never
    the monolith). Returns `{"exit_code": None, "checked": False}` when
    `to_value` is falsy or on a registry-read failure; otherwise
    `{"exit_code": <int>, "checked": True, "message": <str>}`."""
    if not to_value:
        return {"exit_code": None, "checked": False}
    if (repo_root / ".git").is_dir():
        self_root = repo_root
    else:
        try:
            from coordinator_core import lifecycle
            from coordinator_core.ops.fleet._common import main_worktree_root
            self_root = main_worktree_root(lifecycle.git_common_dir(repo_root))
        except RuntimeError:
            return {"exit_code": None, "checked": False}
    # The two exception classes are bound BEFORE the call whose `except`
    # clause names them. Importing them inside that same `try` makes a failing
    # import raise `NameError` out of the handler instead of returning the
    # fallback below -- invisible to the parity oracle, which only exercises
    # the path where the import succeeds. HEAD binds them at module level; the
    # rebuild keeps them local for import cost, so the binding is split out
    # rather than hoisted.
    try:
        from coordinator_core.ops.fleet.memo_check_addressee import (
            compute_check_addressee_candidate,
            format_addressee_message,
        )
        from coordinator_core.ops.fleet._memo_resolver import (
            resolve_self_em_id,
            RegistryReadError,
            AmbiguousReceiverError,
        )
    except ImportError:
        return {"exit_code": None, "checked": False}
    try:
        candidate = compute_check_addressee_candidate(self_root, to_value)
    except (RegistryReadError, AmbiguousReceiverError):
        return {"exit_code": None, "checked": False}
    self_em = resolve_self_em_id(self_root)
    message, exit_code = format_addressee_message(self_em, self_root, to_value, candidate)
    return {"exit_code": exit_code, "checked": True, "message": message}


# ---------------------------------------------------------------------------
# preflight.completeness_items / completeness_batches
# ---------------------------------------------------------------------------

def build_untrusted_gate_judgment_point(
    id: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    *,
    round_trip: str = "terminal",
    revalidate_at_dispatch: bool = False,
) -> dict[str, Any]:
    """The judgment-point constructor for evidence sourced from
    branch-writable content this engine did not itself compute (a
    memo/handoff body quoted verbatim into `evidence`) — the Director of Engineering's
    discriminator: "can the thing being recommended about influence the
    recommendation?" Here the answer is yes, so recommending is structurally
    forbidden rather than merely discouraged, mirroring
    `build_completeness_checklist`'s existing `resolves: []`
    structural-unreachability shape for its probe-confirmation gate.

    Carries NO `recommendation` parameter — a caller cannot pass one even by
    mistake, a type-level guarantee rather than a runtime assertion on one
    gate. Always emits `recommendation: None`, `reason:
    "recommendation-forbidden"`.

    Composes the shared seam's own `build_untrusted_gate_judgment_point`
    (C6, see `build_judgment_point` above) for the dict construction --
    this module's positional signature and default `revalidate_at_dispatch`
    are preserved by this wrapper, not by a second implementation.
    """
    return _shared_build_untrusted_gate_judgment_point(
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason="recommendation-forbidden",
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )


def build_completeness_checklist(fm: dict[str, Any], artifact_path: str) -> dict[str, Any]:
    """Function 6 — parses `completeness_checklist:` items (in-process, via
    `coordinator_core.ops.parse_completeness_item`, AC16 — never re-derives
    the grammar here), hoists `restart-gated` items ahead of `live` items
    (Step 5.5b fixed ordering rule), and returns one `coordinator-tasks-mirror
    init` EM-run directive per item.

    MIRROR IS PRIMARY, HARNESS TASK IS BEST-EFFORT. Each directive carries an
    additive `harness_task_create` payload for a consumer that mirrors the item
    into the agent harness's own task surface. That payload is advisory: this
    repo has no consumer for it, it commands no directive of its own, and an EM
    whose harness lacks `TaskCreate` simply does not act on it. The disk-backed
    `coordinator-tasks-mirror` CLI is the durable half and the only half that
    executes — confirmed inert-but-harmless in a live session on 2026-08-17,
    when `TaskCreate` was absent from an EM tool surface entirely
    (`cross-repo/inbox/2026-08-17-example-cockpit-repo-em-harness-task-create-payload-inert.md`).
    NEGATIVE SPEC: do not invert these. Promoting the harness task to primary
    and demoting the mirror to fallback trades a durable on-disk record for a
    harness capability that has already vanished once.

    SECURITY-LOAD-BEARING (contract § "Probe-confirmation is JUDGMENT, not a
    gates boolean"): a `[probe: ...]` command is UNTRUSTED input with full
    agent-Bash blast radius. This function NEVER returns a directive that
    runs a probe — the only artifact a probe-carrying item produces is a
    `judgment_points` entry ("Run untrusted completeness probe `<cmd>`?")
    whose `dispositions` resolve NOTHING (`resolves: []` on both choices) —
    there is no downstream directive for either disposition to unblock,
    by construction. An autonomous no-human consumer MUST leave that
    judgment point unresolved and the probe unrun; this function's shape
    makes "auto-run the probe" structurally unreachable rather than merely
    discouraged.
    """
    raw_items = fm.get("completeness_checklist")
    if not raw_items:
        return {"items": [], "directives": [], "judgment_points": [], "batches": []}
    if isinstance(raw_items, str):
        raw_items = [raw_items]

    parsed: list[dict[str, Any]] = []
    for raw in raw_items:
        try:
            item_class, assertion, probe = _parse_completeness_item(raw)
        except _CompletenessMalformed as exc:
            parsed.append({"raw": raw, "malformed": True, "error": str(exc)})
            continue
        parsed.append({
            "raw": raw,
            "malformed": False,
            "class": item_class,
            "assertion": assertion,
            "probe": probe or None,
        })

    # Step 5.5b — restart-gated items hoisted ahead of live items (fixed
    # ordering rule; stable sort preserves within-class declaration order).
    ordered = sorted(
        (item for item in parsed if not item["malformed"]),
        key=lambda item: 0 if item["class"] == "restart-gated" else 1,
    )

    basename = Path(artifact_path).name
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []

    for idx, item in enumerate(ordered):
        task_id = f"ct{idx + 1}"
        directives.append({
            "id": f"d-{task_id}-mirror",
            "cli": "coordinator-tasks-mirror",
            "args": ["init", basename, item["assertion"]],
            "depends_on": None,
            "already_satisfied": False,
            "harness_task_create": {"content": item["assertion"], "class": item["class"]},
        })
        if item["probe"]:
            # SECURITY-LOAD-BEARING (see module docstring/negative-spec):
            # `item["probe"]` is untrusted, branch-writable content quoted
            # verbatim into `evidence` — recommending here would nudge an
            # operator toward running attacker-influenceable input.
            # `build_untrusted_gate_judgment_point` makes `recommendation`
            # structurally unreachable rather than merely discouraged.
            judgment_points.append(build_untrusted_gate_judgment_point(
                f"j-{task_id}-probe",
                f"Run untrusted completeness probe `{item['probe']}`?",
                item["probe"],
                [
                    {"value": "confirm-and-run", "resolves": []},
                    {"value": "skip-and-validate-manually", "resolves": []},
                ],
            ))

    # Step 5.5b — `preflight.completeness_batches`: the restart-gated-hoisted
    # ordering as its own evidence field (contract § computed-skills.md
    # "Restart-gated hoist/partition (fixed ordering rule)"), independent of
    # `directives[]`'s parallel ordering above.
    # Review: code-reviewer — Finding 5: `probe` is duplicated here rather
    # than replaced with an index back into `items[]` intentionally — batches
    # is self-contained ordering evidence, requiring no cross-referencing by
    # consumers.
    batches = [
        {"class": item["class"], "assertion": item["assertion"], "probe": item["probe"]}
        for item in ordered
    ]

    return {"items": parsed, "directives": directives, "judgment_points": judgment_points, "batches": batches}


# ---------------------------------------------------------------------------
# directives / judgment_points / narration / next_move assembly
#
# The functions and constants in this section are a BEHAVIOUR-IDENTICAL PORT
# (not a rewrite) from `coordinator_core.pickup_assemble` — DR-415 kept
# `directives[]`/`judgment_points[]` construction unchanged (it is not among
# the decision's eight deleted field groups), and C10's rebuild of this
# module never carried the port over. Ported 2026-09-12, chunk C10b.
# ---------------------------------------------------------------------------

#: `fold-into-plan` guidance, shared verbatim between the `proposal` and
#: `fyi` kind tables below (ported from the monolith's own dedup constant —
#: was a byte-identical duplicate before that fix).
_FOLD_INTO_PLAN_GUIDANCE = (
    "The memo changes a LIVE plan's premise. Edit that plan and commit "
    "— including, and especially, when another session owns or is "
    "executing it: the commit is how that session finds out. A reply "
    "reaches the sender's inbox and our inbound memo gets archived; the "
    "executing EM reads neither, they read their chunk. Declining the "
    "edit as someone else's surface is what destroys the finding. "
    "ANNOTATE, never rewrite: mark stale text superseded-in-part with "
    "the date and reason, and land it in the chunk body that EM reads "
    "next rather than only a preamble. Do NOT re-scope, re-sequence, or "
    "execute their chunks — change the premise record, leave the work. "
    "STAGING DISCIPLINE, because this disposition invites writes into "
    "files other sessions hold open: `git commit <pathspec>` commits the "
    "WORKING TREE version of that path, so committing a plan a peer has "
    "dirty sweeps their in-flight edit under your subject. When the file "
    "already carries a peer's hunks, stage only your own — `git diff "
    "<path>`, drop the hunks that are not yours, `git apply --cached`, "
    "then commit the staged version. Never `git stash`. This disposition "
    "maps to `--decision accepted`, so it requires `realized_by` "
    "(the SHA of the fold commit); `cs_action_memo` fails loud without it. "
    "`decision_note` is not enforced but is worth adding for the record."
)

#: Per-memo-kind disposition tables (`j-kind` judgment point). Ported
#: verbatim from the monolith's own `_KIND_DISPOSITIONS`.
_KIND_DISPOSITIONS: dict[str, list[dict[str, Any]]] = {
    "ask": [
        {
            "value": "accept-mechanical-direct",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Accept and action now, no plan needed — three shapes: route-to-baton "
                "(the ask falls inside an active handoff's scope; fold it into that "
                "handoff's body and commit, rather than triaging it fresh), "
                "direct-dispatch (small/bounded enough to hand an executor immediately), "
                "or do-now-before-gate (act before a pending gate closes). "
                "route-to-baton requires a LIVE target, and the target is a handoff — a "
                "plan or handoff already in a terminal state (`status: implemented` / "
                "`shipped` / `superseded`, or `deployment_state: shipped`) is a "
                "historical record, and folding a forward-binding constraint into one "
                "buries it: nobody reads a delivered plan's Anti-scope before building "
                "the thing it constrains. Recording correspondence against a delivered "
                "plan is fine; writing an instruction there is not. When the only "
                "on-topic artifact is terminal, the fold belongs in live substrate "
                "instead — the roadmap's amendment file, the downstream stub handoff "
                "that will actually build the thing, or a decision record — and say in "
                "the commit why the delivered plan was not the home. Before "
                "accepting: verify the memo's premise against current disk/git state "
                "(a sender's absence-claim is scoped to the sender's own visibility — "
                "treat a contradicting local hit as a real contradiction, not as the "
                "sender simply not having looked; a receiver-repo dedup check is a "
                "same-topic judgment call, not a keyword match) and check no other "
                "session already holds a live claim on the artifact this memo concerns "
                "(an apparently-orphaned lock still needs a liveness check before any "
                "takeover). **A route-to-baton fold into a target that HAS a live "
                "holder is not complete when the commit lands.** The same claim check "
                "that says whether you may write also says whom to tell: a live holder "
                "is mid-work against the body you just changed, will not re-read it on "
                "your account, and can execute the very thing your constraint binds "
                "before ever seeing it — a write nobody is told about is a race you "
                "chose to run. Message them: `gates.competing_claim`'s candidates carry "
                "`send_message_address` for exactly this, resolved fresh in this brief "
                "(never persisted or reused past this instant — see that field's own "
                "negative-spec), so it costs one call and no lookup. Say what you wrote, "
                "where, and what it binds; keep it to that. If the holder is NOT live, "
                "or has no resolvable address, say so in the `decision_note` — \"no "
                "message was owed\" is a finding the next reader needs, and is not the "
                "same as having skipped it. If the memo's `scoped_to` looks too narrow or too broad for "
                "the actual change, challenge it rather than accepting it as given. "
                "Capture any commitment this creates for a sibling repo/session before "
                "moving on, and record the item's distillation fate (ephemeral / "
                "commitment / ratification) so a later `/distill` knows whether to prune "
                "it. If the accepted item is a cross-repo roadmap-stub MOVE, audit the "
                "source side for a residual after the move lands. This disposition maps "
                "to `--decision accepted`, which requires `realized_by` (a pointer to "
                "what realized the ask — typically the commit SHA that landed the fix) "
                "alongside `decision_note`; `cs_action_memo` fails loud without it."
            ),
        },
        {
            "value": "accept-escalate-to-sizing",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Accept, but the ask is novel work in this repo and bigger than a direct "
                "action — route it into `coordinator:sizing` rather than executing inline "
                "or gut-reading \"big enough for a plan\". The sizing lobby picks the room "
                "(dispatch / spec-dispatch / shape / plan / roadmap / pm-decision) for you. "
                "Same premise-verification and live-claim-holder checks as "
                "accept-mechanical-direct apply before accepting. If the memo's "
                "resolution forward-points at an existing plan rather than asking for a "
                "new one, reconcile that plan's on-disk state first: read the plan's "
                "current status, confirm it is still live (not already executed, "
                "abandoned, or superseded) before treating it as the resolution target, "
                "and surface what you find rather than assuming the pointer is still "
                "accurate. This disposition maps to `--decision partial` (the memo is "
                "only partially actioned in-line; the sizing object it escalates to "
                "carries the rest), which requires `realized_by` (a pointer to what "
                "realized this partial step — the sizing object's path, "
                "`state/sizings/<id>.yaml`; a sizing that terminates at `route: "
                "pm-decision` with `xl_exit: null` is a legitimate open state, which is "
                "exactly why `partial` remains right rather than becoming wrong) "
                "alongside `decision_note`; `cs_action_memo` fails loud without it."
            ),
        },
        {
            "value": "decline",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Decline — no action taken on the ask itself. Record why in "
                "`decision_note` (NOT `actioned_note` — a decision-mapped disposition "
                "takes its reasoning via `decision_note`; supplying `actioned_note` here "
                "instead raises a fail-loud from `_build_action_memo_args`) so the sender "
                "(and any later reader) sees the reasoning, not just the verdict. A "
                "wrong-addressee memo (this session is not who the memo names) is a "
                "stop-and-offer, not a silent decline — surface the mismatch rather than "
                "claiming/stamping/actioning a memo addressed to someone else."
            ),
        },
        {
            "value": "surface-to-PM",
            "resolves": [],
            "guidance": (
                "Surface to the PM rather than deciding unilaterally — the right call "
                "when the ask is product direction, a scope change, an external-facing "
                "action, or a genuine no-correct-answer tradeoff. Present the memo and "
                "the fork, not a pre-baked recommendation."
            ),
        },
    ],
    "consult": [
        {
            "value": "reply-short",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Answer in place, briefly — the reply goes directly into `actioned_note`. "
                "Appropriate when the question has a short, self-contained answer that "
                "doesn't need its own section. Actioning this disposition requires "
                "`actioned_note` (the reply itself): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since replying in place is not "
                "an accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "reply, however brief, rather than leaving `actioned_note` empty."
            ),
        },
        {
            "value": "reply-long",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Answer in place, at length — write the full reply under a `## EM "
                "Response` heading in the artifact body, and point `actioned_note` at "
                "that heading rather than duplicating the text. Appropriate when the "
                "question needs reasoning, options, or evidence laid out, not just a "
                "verdict. Actioning this disposition requires `actioned_note` (pointing "
                "at the `## EM Response` heading): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since replying in place is not "
                "an accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "pointer, however brief, rather than leaving `actioned_note` empty."
            ),
        },
    ],
    "proposal": [
        {
            "value": "adopt",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Adopt the proposal as sent — action it directly. Same premise- and "
                "live-claim-holder verification as an `ask` accept applies before "
                "adopting. This disposition maps to `--decision accepted`, which "
                "requires `realized_by` (a pointer to what realized the proposal — "
                "typically the commit SHA that landed it) alongside `decision_note`; "
                "`cs_action_memo` fails loud without it."
            ),
        },
        {
            "value": "decline",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Decline the proposal — no action taken. Record why in `decision_note` "
                "(NOT `actioned_note` — a decision-mapped disposition takes its reasoning "
                "via `decision_note`; supplying `actioned_note` here instead raises a "
                "fail-loud from `_build_action_memo_args`)."
            ),
        },
        {
            "value": "negotiate",
            "resolves": ["d-action-memo"],
            "guidance": (
                "Neither adopt nor decline outright — counter-propose a modified shape "
                "and record the counter in `actioned_note` (or reply body) for the "
                "sender to react to. Actioning this disposition requires `actioned_note` "
                "(the counter, or a pointer to it): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since negotiating is not an "
                "accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "counter, however brief, rather than leaving `actioned_note` empty."
            ),
        },
        {
            "value": "fold-into-plan",
            "resolves": ["d-action-memo"],
            "guidance": _FOLD_INTO_PLAN_GUIDANCE,
        },
    ],
    "fyi": [
        {
            "value": "ack-nil",
            "resolves": ["d-action-memo"],
            "guidance": (
                "No impact on this repo's work — acknowledge and close, no further "
                "action. Actioning this disposition requires `actioned_note` (recording "
                "the nil-impact rationale): `d-action-memo` resolves via the "
                "`--actioned-note` path (no `--decision`, since nil-impact is not an "
                "accepted/partial/declined outcome), and `cs_action_memo` fails loud if "
                "neither `--decision` nor `--actioned-note` is supplied — so state the "
                "rationale, however brief, rather than leaving `actioned_note` empty."
            ),
        },
        {
            "value": "re-plan",
            "resolves": [],
            "guidance": (
                "The FYI invalidates an existing plan's premise deeply enough that the "
                "surface needs re-planning rather than an annotation. Re-planning and "
                "folding into the plan are responses to different MAGNITUDES, not a rule "
                "against the smaller one — if the plan survives with its premise "
                "corrected, `fold-into-plan` is the response, and choosing this one "
                "instead leaves the executing session running on a premise the sender "
                "already told us was dead."
            ),
        },
        {
            "value": "fold-into-plan",
            "resolves": ["d-action-memo"],
            "guidance": _FOLD_INTO_PLAN_GUIDANCE,
        },
        {
            "value": "surgical-fix",
            "resolves": ["d-action-memo"],
            "guidance": (
                "The FYI needs a small, contained fix here — action it directly rather "
                "than a full re-plan. Same premise/live-claim verification as an `ask` "
                "accept applies. This disposition maps to `--decision accepted`, so it "
                "requires BOTH `realized_by` (the SHA of the commit that lands the fix — "
                "`cs_action_memo` fails loud without it) and `decision_note` for the "
                "reasoning; `actioned_note` is rejected on this branch, it belongs to "
                "nil-impact dispositions only. Land the fix first, then action the memo "
                "with its SHA."
            ),
        },
        {
            "value": "surface-to-PM",
            "resolves": [],
            "guidance": (
                "The FYI's impact is a product-direction call — surface it to the PM "
                "rather than deciding it here."
            ),
        },
        {
            "value": "investigate-further",
            "resolves": [],
            "guidance": (
                "The FYI's impact is ambiguous from the memo alone — investigate before "
                "committing to nil/re-plan/surgical-fix/surface-to-PM."
            ),
        },
    ],
}

#: (kind_resolved, disposition_value) -> `cs_action_memo`'s `--decision` mode
#: (accepted/partial/declined). Ported verbatim from the monolith's own
#: `_MEMO_ACTION_DECISION_MAP`.
_MEMO_ACTION_DECISION_MAP: dict[tuple[str, str], str] = {
    ("ask", "accept-mechanical-direct"): "accepted",
    ("ask", "accept-escalate-to-sizing"): "partial",
    ("ask", "decline"): "declined",
    ("proposal", "adopt"): "accepted",
    ("proposal", "decline"): "declined",
    ("fyi", "surgical-fix"): "accepted",
    ("fyi", "fold-into-plan"): "accepted",
    ("proposal", "fold-into-plan"): "accepted",
}

#: decision value -> required `--decisions` content keys. Ported verbatim.
_DECISION_REQUIRED_CONTENT_KEYS: dict[str, tuple[str, ...]] = {
    "accepted": ("realized_by",),
    "partial": ("realized_by",),
    "declined": (),
}

#: The two `reason` values a null `recommendation` may carry. Ported
#: verbatim from the monolith's own `_NULL_RECOMMENDATION_REASONS`.
_NULL_RECOMMENDATION_REASONS = frozenset({"insufficient-evidence", "recommendation-forbidden"})

#: `kind` -> the `j-kind` judgment-point question text. Ported verbatim.
_KIND_QUESTIONS: dict[str, str] = {
    "ask": "ask: Accept mechanical-direct / Accept escalate-to-sizing / Decline / Surface-to-PM?",
    "consult": "consult: Reply short (goes in actioned_note) / Reply long (## EM Response heading, actioned_note points at it)?",
    "proposal": "proposal: Adopt / Decline / Negotiate?",
    "fyi": "fyi impact: nil / plan-invalidated / surgical-fix / product-decision / ambiguous?",
}

#: The terminal `status` values an archived memo may already carry —
#: gates `_archived_open_memo_kind_dispatch`'s call site. Ported verbatim
#: from the monolith's own `_MEMO_TERMINAL_STATUS`.
_MEMO_TERMINAL_STATUS = frozenset({"actioned", "superseded"})


def build_judgment_point(
    id: str,
    question: str,
    evidence: str,
    dispositions: list[dict[str, Any]],
    recommendation: Optional[dict[str, str]],
    *,
    round_trip: str = "terminal",
    revalidate_at_dispatch: bool = False,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """The monolith's own `judgment_points[]` entry constructor — a thin
    positional-first wrapper over the shared seam
    (`contract.decision_object.judgment.build_judgment_point`, imported here
    as `_shared_build_judgment_point`), ported verbatim (DR-415 kept set).
    Distinct from `_shared_build_judgment_point` itself: this module's other,
    independently-written judgment-point builders (`build_liveness_judgment_
    point`, `build_execution_stamp_judgment_point`, `compute_supersession_
    gate`) call the shared seam directly and are unaffected by this wrapper.
    """
    if recommendation is None:
        if reason not in _NULL_RECOMMENDATION_REASONS:
            raise ValueError(
                "build_judgment_point: a null recommendation requires reason "
                f"'insufficient-evidence' or 'recommendation-forbidden', got {reason!r}"
            )
    elif reason is not None:
        raise ValueError("build_judgment_point: reason only accompanies a null recommendation")
    return _shared_build_judgment_point(
        recommendation,
        id=id,
        question=question,
        dispositions=dispositions,
        evidence=evidence,
        reason=reason,
        revalidate_at_dispatch=revalidate_at_dispatch,
        round_trip=round_trip,
    )


# ---------------------------------------------------------------------------
# compute_reply_closure — ported verbatim (DR-415 kept set) from HEAD's
# `coordinator_core.pickup_assemble.compute_reply_closure` /
# `_render_reply_closure` / helpers. A terminal (`status: actioned`/
# `superseded`) ask/consult memo is not closed until a reply reached the
# sender's own tree — `status: actioned` only marks OUR side done. See
# HEAD's own 2026-07-25 defect trail (same-day-only matching, then the
# unlinked-short-stem false positive) preserved verbatim in the comments
# below; this port changes none of that history, only where it lives.
# ---------------------------------------------------------------------------

#: `compute_reply_closure` verdicts where the terminal "nothing further to
#: do" narration stands unchanged — `open`/`unknown` must never join this
#: set (that is the exact polarity inversion the 2026-07-25 defect was).
_REPLY_CLOSURE_TERMINAL_VERDICTS = frozenset({"not_required", "evidenced"})


def _parse_memo_date(value: Optional[str]) -> Optional[date]:
    """Best-effort `created:`-field parse (`YYYY-MM-DD`, extra trailing text
    ignored) — `None` on anything that doesn't parse, so a malformed date
    degrades to "can't compare" rather than a raised exception."""
    if not value:
        return None
    try:
        return _dt.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


_LEADING_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

#: Narration/evidence-string display cap for unconfirmed (date-matched but
#: unlinked) candidates — a busy fleet day can produce dozens; the reason
#: string names the true total and truncates the cited list, never the
#: other way around.
_UNCONFIRMED_CITE_CAP = 5


def _inbound_link_stems(memo_path: str, from_id: str) -> tuple[str, str, str]:
    """The three basename-shaped strings a candidate reply must carry (via
    `in_reply_to`) or cite (in its own text) to count as LINKED rather than
    merely same-day. Returns `(basename, basename_no_ext, tail_stem)` —
    `tail_stem` strips the leading `YYYY-MM-DD-` date AND the `<from_id>-`
    sender segment, matching a reply that cites an elided filename."""
    basename = Path(memo_path).name
    basename_no_ext = basename[:-3] if basename.endswith(".md") else basename
    tail_stem = _LEADING_DATE_RE.sub("", basename_no_ext, count=1)
    sender_prefix = f"{from_id}-"
    if tail_stem.startswith(sender_prefix):
        tail_stem = tail_stem[len(sender_prefix):]
    return basename, basename_no_ext, tail_stem


#: 2026-07-25 third-pass defect floor: any stem shorter than this is skipped
#: as a needle entirely (never treated as a match) — an unfloored short
#: `tail_stem` (e.g. a 1-char topic slug) degrades to a near-empty needle
#: that matches almost any prose. `basename`/`basename_no_ext` always clear
#: this for free (the `YYYY-MM-DD-` prefix alone is 10 chars).
_MIN_LINK_STEM_LENGTH = 10


def _candidate_is_linked(candidate_text: str, candidate_fm_text: str, link_stems: tuple[str, str, str]) -> bool:
    """True when a same-sender, same-window candidate is actually LINKED to
    the inbound memo — `in_reply_to` naming it (by basename or
    basename-minus-`.md`), or the candidate's own text citing its basename
    or its date-and-sender-stripped tail stem. Case-insensitive throughout,
    exact-substring, never fuzzy/token-overlap scoring."""
    basename, basename_no_ext, tail_stem = link_stems
    in_reply_to = read_fm_field_unquoted(candidate_fm_text, "in_reply_to")
    if in_reply_to is not None:
        normalized = in_reply_to.strip().lower()
        if normalized in (basename.lower(), basename_no_ext.lower()):
            return True
    lowered = candidate_text.lower()
    return any(
        needle and len(needle) >= _MIN_LINK_STEM_LENGTH and needle.lower() in lowered
        for needle in (basename, basename_no_ext, tail_stem)
    )


def _format_unconfirmed_reason(unconfirmed: list[str], self_em_id: str, from_id: str, since: date, basename: str) -> str:
    total = len(unconfirmed)
    cited = unconfirmed[:_UNCONFIRMED_CITE_CAP]
    tail_note = f" (+{total - len(cited)} more)" if total > len(cited) else ""
    return (
        f"{total} memo(s) from '{self_em_id}' to '{from_id}' dated on/after {since.isoformat()}, "
        f"none citing '{basename}' (one of these may in fact BE the reply, sent without "
        f"--in-reply-to): {'; '.join(cited)}{tail_note}"
    )


def compute_reply_closure(frontmatter: dict[str, Any], memo_path: str, repo_root: Path) -> dict[str, Any]:
    """Reply-closure predicate for a terminal (`status: actioned`/
    `superseded`) inbound memo — ported verbatim (DR-415 kept set) from
    HEAD's `pickup_assemble.compute_reply_closure`. Returns
    `{"verdict": ..., "reason": Optional[str], "candidates": [...],
    "unconfirmed_candidates": [...]}` — four verdicts: `not_required`
    (`kind: fyi`), `evidenced` (a linked reply found in the sender's own
    tree), `open` (reply required, none found or none linked), `unknown`
    (reply required but the check could not run — sender repo unresolvable,
    no cross-repo/ tree, or the inbound memo's own `from`/`created` missing
    or unparseable). `unknown` is deliberately NOT folded into `open` or
    `not_required`: this is a *suppression* check (it decides whether
    "nothing further to do" gets printed), so fail-open-to-noise is the
    correct polarity."""
    kind = frontmatter.get("kind")
    if kind == "fyi":
        return {"verdict": "not_required", "reason": None, "candidates": [], "unconfirmed_candidates": []}

    from_id = frontmatter.get("from")
    created_raw = frontmatter.get("created")
    inbound_created = _parse_memo_date(created_raw)
    if not from_id or not created_raw:
        return {
            "verdict": "unknown",
            "reason": f"'{memo_path}' frontmatter is missing 'from' and/or 'created' — cannot search for a reply.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }
    if inbound_created is None:
        return {
            "verdict": "unknown",
            "reason": f"'{memo_path}' has an unparseable 'created' value ({created_raw!r}) — cannot date-filter replies.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    from coordinator_core.memo_corpus import receiver_inbox_root
    from coordinator_core.ops.fleet._memo_resolver import (
        AmbiguousReceiverError,
        RegistryReadError,
        resolve_receiver_inbox,
        resolve_self_em_id,
    )

    try:
        _inbox_dir, sender_root, _all_repos = resolve_receiver_inbox(from_id)
    except (RegistryReadError, AmbiguousReceiverError) as exc:
        return {
            "verdict": "unknown",
            "reason": f"machine-local registry lookup for '{from_id}' failed: {exc}",
            "candidates": [],
            "unconfirmed_candidates": [],
        }
    if sender_root is None or not sender_root.is_dir():
        return {
            "verdict": "unknown",
            "reason": f"sender repo for '{from_id}' is not registered (or not present) on this machine.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    # Same per-receiver probe `resolve_receiver_inbox` roots its own
    # resolution on — a hardcoded `sender_root / "cross-repo"` here would
    # keep searching a migrated sender's LEGACY tree after that sender
    # moved to `state/cross-repo/`, inheriting claude-klabauter's own layout rather
    # than the sender's actual one.
    corpus_root_str, _ = receiver_inbox_root(str(sender_root))
    cross_repo_dir = Path(corpus_root_str)
    if not cross_repo_dir.is_dir():
        return {
            "verdict": "unknown",
            "reason": f"'{sender_root}' has no cross-repo/ tree — cannot search for a reply.",
            "candidates": [],
            "unconfirmed_candidates": [],
        }

    # This repo's own EM id, via THE ONE self-identity resolver
    # (`_memo_resolver.resolve_self_em_id`). `compute_addressee_gate`'s
    # `self:` derivation uses the same resolver — do not paste a second
    # copy of this derivation.
    self_em_id = resolve_self_em_id(repo_root)
    link_stems = _inbound_link_stems(memo_path, from_id)
    inbound_basename = link_stems[0]

    linked: list[str] = []
    unconfirmed: list[str] = []
    for search_dir in (cross_repo_dir / "inbox", cross_repo_dir / "archive"):
        if not search_dir.is_dir():
            continue
        for candidate_path in sorted(search_dir.rglob("*.md")):
            try:
                text = candidate_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            split = split_frontmatter(text)
            if split is None:
                continue
            # Cheap frontmatter-only filters (sender, then date) BEFORE the
            # full-text citation scan below — the citation scan only ever
            # runs for a candidate that already passed both.
            candidate_from = read_fm_field_unquoted(split.fm_text, "from")
            if candidate_from != self_em_id:
                continue
            candidate_created = _parse_memo_date(read_fm_field_unquoted(split.fm_text, "created"))
            if candidate_created is None or candidate_created < inbound_created:
                continue
            rel = rel_id(candidate_path, sender_root)
            if _candidate_is_linked(text, split.fm_text, link_stems):
                linked.append(rel)
            else:
                unconfirmed.append(rel)

    if linked:
        return {
            "verdict": "evidenced",
            "reason": None,
            "candidates": linked,
            "unconfirmed_candidates": [],
            "sender_root": str(sender_root),
        }
    if unconfirmed:
        return {
            "verdict": "open",
            "reason": _format_unconfirmed_reason(unconfirmed, self_em_id, from_id, inbound_created, inbound_basename),
            "candidates": [],
            "unconfirmed_candidates": unconfirmed,
        }
    return {
        "verdict": "open",
        "reason": (
            f"no reply from '{self_em_id}' dated on/after {inbound_created.isoformat()} "
            f"found under '{sender_root}'/cross-repo/{{inbox,archive}}."
        ),
        "candidates": [],
        "unconfirmed_candidates": [],
    }


def _render_reply_closure(
    closure: dict[str, Any],
    memo_path: str,
    base_narration: str,
    base_next_move: str,
    status: Optional[str] = None,
) -> tuple[list[dict[str, Any]], str, str]:
    """Folds a `compute_reply_closure` verdict onto a terminal memo's
    narration/next_move/judgment_points triple — ported verbatim (DR-415
    kept set) from HEAD's `pickup_assemble._render_reply_closure`. THE
    single rendering site both terminal-memo emit branches in `brief()`
    (archived-fallback and actioned-in-place) call — do NOT add a second
    copy of this rendering logic at either call site.

    `not_required`/`evidenced` return the base narration/next_move
    unchanged (byte-identical for `not_required`; `evidenced` appends a
    citation of the candidate reply path(s)) and an empty judgment-points
    list. `open`/`unknown` replace `next_move` with an actionable one and
    append exactly one judgment point."""
    verdict = closure["verdict"]
    if verdict in _REPLY_CLOSURE_TERMINAL_VERDICTS:
        if verdict == "evidenced":
            # Candidates are repo-relative to the SENDER's tree, not this
            # one — an unqualified citation sends the reader looking in the
            # receiver's own cross-repo/ and finding nothing.
            sender_root = closure.get("sender_root")
            cited = "; ".join(
                f"{sender_root}/{c}" if sender_root else c for c in closure["candidates"]
            )
            return [], f"{base_narration} Reply evidenced at: {cited}.", base_next_move
        return [], base_narration, base_next_move

    status_display = status if status else "unknown"
    inbound_basename = Path(memo_path).name
    cli_hint = (
        'the cross-repo-memo CLI ("${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}'
        '/bin/cross-repo-memo") — never hand-write a memo file into the receiver\'s tree, which '
        "silently bypasses the summary cap and frontmatter shape every engine path enforces."
    )
    in_reply_to_note = (
        f"pass --in-reply-to {inbound_basename} — the linkage scan cannot confirm the reply "
        "otherwise and this same judgment point will re-fire on the next pickup."
    )
    # `required_content_keys` stamped EXPLICITLY empty rather than left
    # absent: neither disposition maps to a `--decision` at all, so neither
    # needs a content key, but an absent field and an empty one read
    # identically to an operator scanning the decision object.
    dispositions = [
        {"value": "send-reply", "resolves": [], "required_content_keys": []},
        {"value": "already-replied-elsewhere", "resolves": [], "required_content_keys": []},
    ]
    if verdict == "open":
        jp = build_judgment_point(
            "j-reply-closure",
            f"'{memo_path}' is status: {status_display} but no reply to the sender was found in their working tree — send one?",
            closure["reason"],
            dispositions,
            {
                "disposition": "send-reply",
                "rationale": (
                    "an ask/consult memo is not closed until the sender has a reply in their own "
                    f"tree — status: {status_display} only marks OUR side of the exchange done."
                ),
            },
        )
        narration = (
            f"{base_narration} status: {status_display} is necessary but NOT sufficient for an "
            f"ask/consult memo — {closure['reason']}"
        )
        next_move = (
            f"Send the reply via {cli_hint} — {in_reply_to_note}"
        )
        return [jp], narration, next_move

    # verdict == "unknown" — reply required but the check itself could not
    # run; render distinctly from "open" (a confirmed-missing reply) so the
    # EM reads why closure is uncertain rather than mistaking it for the
    # confirmed-open case.
    jp = build_judgment_point(
        "j-reply-closure",
        f"Could not confirm whether '{memo_path}' was actually replied to — reply-closure check did not run to completion.",
        closure["reason"],
        dispositions,
        None,
        reason="insufficient-evidence",
    )
    narration = (
        f"{base_narration} status: {status_display} is necessary but NOT sufficient for an ask/consult "
        f"memo, and the reply-closure check could not confirm a reply reached the sender: "
        f"{closure['reason']}"
    )
    next_move = (
        f"Confirm by hand whether the sender already has a reply, or send one via {cli_hint} — "
        f"{in_reply_to_note}"
    )
    return [jp], narration, next_move


def build_handoff_directives(
    artifact_path: str,
    claim_holder: Optional[str],
    basename: str,
    self_claimed_in_frontmatter: bool = False,
) -> list[dict[str, Any]]:
    """`directives[]` for a handoff/spinoff pickup — `d1` (session claim) +
    `d2` (archive-stamp claim-handoff). Ported verbatim."""
    directives: list[dict[str, Any]] = [
        {
            "id": "d1",
            "cli": "session-claim-cli",
            "args": ["claim-artifact", "handoff", basename],
            "depends_on": None,
            "already_satisfied": claim_holder is not None,
        },
        {
            "id": "d2",
            "cli": "archive-stamp-cli",
            "args": ["claim-handoff", artifact_path],
            "depends_on": None,
            "already_satisfied": self_claimed_in_frontmatter,
        },
    ]
    return directives


def _build_action_memo_args(artifact_path: str, kind_resolved: str, decisions: dict[str, Any]) -> list[str]:
    """Resolves `decisions["j-kind"]` into `cs_action_memo`'s CLI-flag
    surface. Ported verbatim."""
    jkind = decisions.get("j-kind") if isinstance(decisions, dict) else None
    jkind = jkind if isinstance(jkind, dict) else {}
    disposition = jkind.get("disposition")
    args = ["action-memo", artifact_path]
    decision_value = (
        _MEMO_ACTION_DECISION_MAP.get((kind_resolved, disposition))
        if isinstance(disposition, str)
        else None
    )
    if decision_value is not None:
        if jkind.get("actioned_note"):
            raise ValueError(
                f"_build_action_memo_args: decisions['j-kind'] carries both a "
                f"decision-mapped disposition {disposition!r} (kind={kind_resolved!r}) "
                f"and 'actioned_note' — 'actioned_note' is for nil-impact dispositions "
                f"only (fyi/ack-nil-shaped, no --decision). Supply the reasoning via "
                f"'decision_note' instead for this disposition."
            )
        args += ["--decision", decision_value]
        realized_by = jkind.get("realized_by")
        if realized_by:
            args += ["--realized-by", realized_by]
        decision_note = jkind.get("decision_note")
        if decision_note:
            args += ["--decision-note", decision_note]
    elif disposition is not None:
        actioned_note = jkind.get("actioned_note")
        if actioned_note:
            args += ["--actioned-note", actioned_note]
    distill_fate = jkind.get("distill_fate")
    if distill_fate:
        args += ["--distill-fate", distill_fate]
    in_repo_capture = jkind.get("in_repo_capture")
    if in_repo_capture:
        args += ["--in-repo-capture", in_repo_capture]
    return args


def _claim_already_self_held(repo_root: Path, class_: str, basename: str) -> bool:
    """Ported verbatim (HEAD's own `_claim_already_self_held`) — True iff
    THIS session is the recorded holder of the `<class>-claims/<basename>`
    lock dir, so a directive builder can mark its own `claim-artifact`
    directive `already_satisfied` for idempotent same-session re-entry
    (memo's `claim_artifact` REJECTS a same-session reclaim by design)."""
    claims_dir = repo_root / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    if not claims_dir.is_dir():
        return False
    try:
        return _liveness.claim_held_by_me(
            str(claims_dir), my_sid=_explicitly_scoped_session_id(), cwd=str(repo_root)
        )
    except (OSError, ValueError):
        return False


def build_memo_directives(
    artifact_path: str, kind_resolved: str = "ask", decisions: Optional[dict[str, Any]] = None
) -> list[dict[str, Any]]:
    """`directives[]` for a memo pickup — `d1` (session claim), `claim-memo-
    stamp`, `d-action-memo` (disposition-gated terminal write). Ported
    verbatim."""
    decisions = decisions if isinstance(decisions, dict) else {}
    return [
        {
            "id": "d1",
            "cli": "session-claim-cli",
            "args": ["claim-artifact", "memo", Path(artifact_path).name],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "claim-memo-stamp",
            "cli": "archive-stamp-cli",
            "args": ["claim-memo-stamp", artifact_path],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d-action-memo",
            "cli": "archive-stamp-cli",
            "args": _build_action_memo_args(artifact_path, kind_resolved, decisions),
            "depends_on": "j-kind",
            "already_satisfied": False,
        },
    ]


def _required_content_keys(kind: str, disposition: str) -> tuple[str, ...]:
    """Ported verbatim."""
    decision_value = _MEMO_ACTION_DECISION_MAP.get((kind, disposition))
    if decision_value is None:
        return ()
    return _DECISION_REQUIRED_CONTENT_KEYS.get(decision_value, ())


def _dispositions_with_required_keys(kind: str) -> list[dict[str, Any]]:
    """Ported verbatim."""
    return [
        {**entry, "required_content_keys": list(_required_content_keys(kind, entry["value"]))}
        for entry in _KIND_DISPOSITIONS[kind]
    ]


def resolve_memo_kind(fm: dict[str, Any]) -> tuple[str, bool]:
    """M3 kind-enum resolution — absent -> `ask` default; present-
    unrecognized -> `ask` + warn; pinned-enum match -> itself. Ported
    verbatim. Returns `(kind_resolved, unrecognized)`."""
    kind = fm.get("kind")
    if not kind:
        return "ask", False
    if kind not in _KIND_DISPOSITIONS:
        return "ask", True
    return kind, False


def _archived_open_memo_kind_dispatch(
    artifact_path: str, terminal_fields: dict[str, Any], decisions: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Archived-memo-still-open kind-dispatch assembly — an archived MEMO
    whose terminal `status` is NOT already a terminal disposition
    (`_MEMO_TERMINAL_STATUS`) was swept into the archive without ever
    having a disposition stamped. Reuses the same `resolve_memo_kind` /
    `build_memo_directives` / `build_kind_dispatch_judgment_point` triple
    the live memo branch uses. Ported verbatim. Callers gate the
    `terminal_fields.get("status") not in _MEMO_TERMINAL_STATUS` check
    themselves before invoking this — it is unconditional once called."""
    kind_resolved, kind_unrecognized = resolve_memo_kind(terminal_fields)
    directives = build_memo_directives(artifact_path, kind_resolved, decisions)
    jp = build_kind_dispatch_judgment_point(kind_resolved, terminal_fields.get("kind"), kind_unrecognized)
    return directives, [jp]


def build_kind_dispatch_judgment_point(kind_resolved: str, kind_raw: Optional[str], unrecognized: bool) -> dict[str, Any]:
    """M3 kind-dispatch JUDGMENT entry — frames `kind_resolved` as an
    overridable offer, never a verdict. Ported verbatim."""
    entry = build_judgment_point(
        "j-kind",
        _KIND_QUESTIONS[kind_resolved],
        "artifact.kind_resolved",
        _dispositions_with_required_keys(kind_resolved),
        None,
        reason="insufficient-evidence",
    )
    if unrecognized:
        entry["warning"] = f"kind {kind_raw!r} unrecognized — defaulted to 'ask'"
    return entry


def build_execution_stamp_directive(execution_stamp_match: dict[str, Any], target_path: str) -> dict[str, Any]:
    """The tier-1 re-stamp `directives[]` entry — unconditional: the engine
    has already established the recorded value never reproduced the
    canonical recipe, so re-stamping repairs a broken record. Ported
    verbatim."""
    return {
        "id": "d-stamp",
        "cli": "archive-stamp-cli",
        "args": ["restamp-execution-sha", target_path, execution_stamp_match["computed_sha"]],
        "depends_on": None,
        "already_satisfied": False,
    }


def _handoff_stamp_evidence(root: Path, abs_artifact_path: Path) -> bool:
    """Ledger-first evidence that `d2` (`archive-stamp-cli claim-handoff`)
    already landed — a mirror-sourced holder, or a ledger-sourced holder
    whose claim dir carries the durable `stamped` marker (`apply` stage
    alone is reachable on a refused stamp too; see C11 row 35 /
    cross-repo/inbox/2026-08-13-doe-claude-em-pickup-already-satisfied-
    masks-a-refused-write.md). Ledger-first so a branch-switch-revert
    desync (the mirror reverted, the ledger claim did not) still reads
    correctly (C11 row 35)."""
    claim_state = resolve_claim_state(abs_artifact_path, repo_root=root)
    if claim_state.mirror_holder is not None:
        return True
    if claim_state.ledger_holder is not None:
        try:
            common_dir = lifecycle_mod.git_common_dir(root)
        except Exception:
            return False
        claim_dir = handoff_claim_dir(common_dir, abs_artifact_path)
        return _claims.claim_stamped(claim_dir)
    return False


def _build_directives(
    classification: str,
    display_path: str,
    basename: str,
    claim: dict[str, Any],
    claim_grant: dict[str, Any],
    fm: dict[str, Any],
    decisions: dict[str, Any],
    kind_resolved: Optional[str],
    root: Optional[Path] = None,
    abs_artifact_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """`directives[]` assembly, dispatched on `classification` — the wiring
    C10b restores (DR-415 kept this construction; only the wiring lived in
    `pickup_assemble.brief`'s own body, not a separate function there)."""
    if classification in ("handoff", "spinoff"):
        stamp_evidence = False
        if root is not None and abs_artifact_path is not None:
            stamp_evidence = _handoff_stamp_evidence(root, abs_artifact_path)
        self_claimed_in_frontmatter = bool(claim_grant.get("held_by_self")) and stamp_evidence
        return build_handoff_directives(display_path, claim.get("holder"), basename, self_claimed_in_frontmatter)
    if classification == "memo":
        return build_memo_directives(display_path, kind_resolved or "ask", decisions)
    return []


def _build_judgment_points(
    classification: str,
    liveness_fired: bool,
    stamp_gate: Optional[dict[str, Any]],
    kind_resolved: Optional[str],
    kind_raw: Optional[Any],
    kind_unrecognized: bool,
    liveness_resolves: list[str],
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    jp = build_liveness_judgment_point(liveness_fired, "gates.liveness_signal", liveness_resolves)
    if jp is not None:
        points.append(jp)
    if stamp_gate is not None:
        jp2 = build_execution_stamp_judgment_point(stamp_gate)
        if jp2 is not None:
            points.append(jp2)
    if classification == "memo" and kind_resolved is not None:
        points.append(build_kind_dispatch_judgment_point(kind_resolved, kind_raw, kind_unrecognized))
    return points


#: HEAD parity — `_CLASSIFICATION_NOUN`/`_CLASSIFICATION_NEXT_MOVE_PREFIX`/
#: `_ready_summary`, ported verbatim (DR-415 kept `narration`/`next_move`).
_CLASSIFICATION_NOUN: dict[str, str] = {"memo": "memo", "handoff": "handoff", "spinoff": "spinoff"}

_CLASSIFICATION_NEXT_MOVE_PREFIX: dict[str, str] = {
    "memo": "This is a memo — decide its disposition from the options below. ",
    "handoff": "Grab it and run with it — reconcile the pending list against reality. ",
    "spinoff": (
        "This is a spinoff — treat the handoff body as the ground-truth spec; do not "
        "hand-search for pre-existing in-progress work on it — its own declared "
        "successor chain, if any, surfaces mechanically as gates.successor above. "
    ),
}


def _ready_summary(
    classification: str, directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]
) -> tuple[str, str]:
    """Ported verbatim (HEAD's own `_ready_summary`) — shared
    `(narration, next_move)` for a successful compute."""
    blocked_by = [jp["id"] for jp in judgment_points if jp.get("id")]
    held = f"You hold this {_CLASSIFICATION_NOUN.get(classification, 'artifact')}."
    if blocked_by:
        narration = f"{held} {len(directives)} directive(s) ready, {len(blocked_by)} judgment point(s) open."
        next_move = "Resolve the open judgment point(s) before dispatching the ready directives."
    else:
        narration = f"{held} {len(directives)} directive(s) ready to run."
        next_move = "Coast is clear — dispatch the directives."
    prefix = _CLASSIFICATION_NEXT_MOVE_PREFIX.get(classification, "")
    return narration, prefix + next_move


_EXECUTION_PHASE = "execution"


def execution_phase_prefix(fm: dict[str, Any]) -> str:
    """Ported verbatim (HEAD's own `execution_phase_prefix`) — handoff-only."""
    if fm.get("handoff_phase") != _EXECUTION_PHASE:
        return ""
    return (
        "This baton declares handoff_phase: execution — its next move is "
        "/execute-plan, not shaping. Verify the execution-authorization stamp "
        "before proceeding. "
    )


def reply_obligation_at_open(fm: dict[str, Any]) -> Optional[str]:
    """Ported verbatim (HEAD's own `reply_obligation_at_open`) — memo-only.
    `fyi` is the only excused kind; absent/unrecognized still owes."""
    if fm.get("kind") == "fyi":
        return None
    return (
        "A reply to the sender is owed on this memo and is part of actioning "
        "it, not a follow-up: action it and reply in the same pass. "
    )


# ---------------------------------------------------------------------------
# brief() orchestration
# ---------------------------------------------------------------------------

class BriefResult:
    __slots__ = ("decision_object", "exit_code")

    def __init__(self, decision_object: dict[str, Any], exit_code: int) -> None:
        self.decision_object = decision_object
        self.exit_code = exit_code


def _emit(decision_object: dict[str, Any], exit_code: int) -> BriefResult:
    """The single validation chokepoint every decision-object construction
    site routes through — ported verbatim from `pickup_assemble._emit`
    (AC14/AC15/AC5b's enforcement backstop): fails loud (`ValueError`) on
    a missing/empty `narration`, a missing/empty `next_move` whenever
    `gates.coast.verdict` is not `"clear"` (a bare error payload with no
    `gates` object reads as verdict `None`, held to the same bar), and any
    `judgment_points[]` entry missing a `recommendation` key regardless of
    how it was constructed. Does NOT fabricate a default for either field
    — a caller that omits one is a bug at the call site, not this
    chokepoint's to paper over."""
    sanitize_resolution = (decision_object.get("artifact") or {}).get("sanitize_resolution")
    if sanitize_resolution:
        # `passed` is rendered with its line breaks escaped: the sanitize tier
        # also repairs a hard line wrap, and a raw newline here would split
        # this one-line note across the narration.
        _passed = sanitize_resolution["passed"].replace("\r", "\\r").replace("\n", "\\n")
        decision_object = {
            **decision_object,
            "narration": (
                f"Passed path '{_passed}' only resolved after "
                f"trimming surrounding/trailing prose punctuation and rejoining "
                f"any hard line wrap -> "
                f"'{sanitize_resolution['resolved']}'. "
            ) + (decision_object.get("narration") or ""),
        }

    elision_resolution = (decision_object.get("artifact") or {}).get("elision_resolution")
    if elision_resolution:
        decision_object = {
            **decision_object,
            "narration": (
                f"Resolved elided baton path '{elision_resolution['passed']}' -> "
                f"'{elision_resolution['resolved']}'. "
            ) + (decision_object.get("narration") or ""),
        }

    suffix_resolution = (decision_object.get("artifact") or {}).get("suffix_resolution")
    if suffix_resolution:
        decision_object = {
            **decision_object,
            "narration": (
                f"Passed slug '{suffix_resolution['passed']}' had no exact match — resolved "
                f"via unique basename-suffix match -> '{suffix_resolution['resolved']}'. "
            ) + (decision_object.get("narration") or ""),
        }

    narration = decision_object.get("narration")
    if not narration:
        raise ValueError("_emit: decision object missing non-empty 'narration'")

    coast_verdict = (((decision_object.get("gates") or {}).get("coast")) or {}).get("verdict")
    if coast_verdict != "clear":
        next_move = decision_object.get("next_move")
        if not next_move:
            raise ValueError(
                f"_emit: decision object with gates.coast.verdict={coast_verdict!r} "
                "missing non-empty 'next_move'"
            )

    for jp in decision_object.get("judgment_points") or []:
        if "recommendation" not in jp:
            raise ValueError(
                f"_emit: judgment_points entry {jp.get('id', '<no id>')!r} missing "
                "required 'recommendation' key"
            )

    return BriefResult(decision_object, exit_code)


def brief(artifact_path: str, decisions: Optional[dict[str, Any]] = None, claim_at_brief: bool = False, repo_root: Optional[Path] = None) -> BriefResult:
    """Computes the pickup decision object for one artifact — exactly the
    kept-set keys (§ pickup oracle), nothing more. `claim_at_brief=True`
    (single-artifact invocation) takes the brief-stage claim; an
    ` AND `-joined survey never does (`brief_multi` passes False)."""
    root = repo_root if repo_root is not None else resolve_repo_root()
    if root is None:
        raise _TransportFailure("no enclosing git worktree")

    try:
        artifact = resolve_artifact(artifact_path, root)
    except _ArtifactUnreadable as exc:
        return _emit({
            "artifact": {
                "path": artifact_path, "classification": None,
                "frontmatter": {}, "resolution": None,
            },
            "error": str(exc),
            "narration": f"Could not resolve {artifact_path!r}: {exc}",
            "next_move": (
                "Confirm the path or basename, then retry — checked "
                f"{', '.join(LIVE_DIRS + ARCHIVE_DIRS)}."
            ),
        }, EXIT_BUSINESS_FAIL)
    except OutOfRepoPath as exc:
        return _emit({
            "artifact": {
                "path": artifact_path, "classification": "ambiguous",
                "frontmatter": {}, "resolution": None,
            },
            "error": str(exc),
            "narration": f"Could not resolve {artifact_path!r}: {exc}",
            "next_move": "Confirm the path or basename, then retry.",
        }, EXIT_BUSINESS_FAIL)
    except _ArtifactElisionInconclusive as exc:
        candidates_str = ", ".join(exc.candidates)
        jp = build_judgment_point(
            "j-elision",
            f"Which baton does the elided path '{exc.artifact_path}' resolve to?",
            f"candidates: {candidates_str}",
            [{"value": c, "resolves": []} for c in exc.candidates],
            None,
            reason="insufficient-evidence",
        )
        return _emit({
            "artifact": {
                "path": exc.artifact_path,
                "classification": "ambiguous",
                "frontmatter": {},
                "resolution": {"status": "elision_inconclusive", "candidates": exc.candidates},
            },
            "preflight": {"tree_quiescence": compute_tree_quiescence(root, [])},
            "gates": {"claim": {}, "addressee": {}, "coast": compute_coast([])},
            "directives": [],
            "judgment_points": [jp],
            "narration": (
                f"'{exc.artifact_path}' is an elided path that matches {len(exc.candidates)} "
                f"candidates — cannot resolve without operator input: {candidates_str}."
            ),
            "next_move": "Pick the intended baton from the candidates and re-run brief with its literal path.",
        }, EXIT_BUSINESS_FAIL)

    classification = artifact["classification"]
    fm = artifact.get("frontmatter") or {}
    display_path = artifact["path"]
    abs_path = root / display_path

    if classification == "ambiguous":
        tree_quiescence = compute_tree_quiescence(root, fm.get("scope", []) or [])
        return _emit({
            "artifact": artifact,
            "gates": {"claim": {}, "addressee": {}, "coast": compute_coast([])},
            "directives": [],
            "judgment_points": [],
            "narration": f"Could not classify {display_path} against the handoff/spinoff/memo shape.",
            "next_move": "Read the artifact directly and confirm its kind by hand before proceeding.",
            "preflight": {"tree_quiescence": tree_quiescence},
        }, EXIT_BUSINESS_FAIL)

    if classification == "archived":
        resolution = artifact.get("resolution") or {}
        archive_path = resolution.get("archive_path", "an archive directory")
        terminal_fields = resolution.get("terminal_fields") or {}
        base_narration = f"{display_path} is archived at {archive_path} — a terminal record."
        base_next_move = "Nothing further to do — this artifact already closed."
        archived_class = resolution.get("archived_class")
        directives: list[dict[str, Any]] = []
        judgment_points: list[dict[str, Any]] = []
        narration, next_move = base_narration, base_next_move
        # `"from" in terminal_fields` doubles as the memo-vs-handoff
        # discriminator here (mirrors HEAD): an archived handoff's
        # terminal_fields never carries that key, so the reply-closure
        # check (memo-only) only runs when this archived artifact is
        # actually a memo.
        if "from" in terminal_fields:
            closure = compute_reply_closure(terminal_fields, display_path, root)
            judgment_points, narration, next_move = _render_reply_closure(
                closure,
                display_path,
                base_narration,
                base_next_move,
                status=terminal_fields.get("status"),
            )
        # Archived-memo-still-open kind-dispatch (2026-07-27 doe-claude-em
        # memo defect fix) — an archived memo whose terminal `status` is
        # NOT already a terminal disposition was swept into the archive
        # without ever having a disposition stamped on it; fire the same
        # `j-kind` judgment point and memo directives the live in-place
        # memo branch fires, keyed off `archived_class` (never the
        # `"from" in terminal_fields` heuristic above it, which only
        # gates the reply-closure check).
        if (
            archived_class == "memo"
            and terminal_fields.get("status") not in _MEMO_TERMINAL_STATUS
        ):
            kind_resolved, kind_unrecognized = resolve_memo_kind(terminal_fields)
            directives = build_memo_directives(display_path, kind_resolved, decisions or {})
            judgment_points = judgment_points + [
                build_kind_dispatch_judgment_point(
                    kind_resolved, terminal_fields.get("kind"), kind_unrecognized
                )
            ]
        tree_quiescence = compute_tree_quiescence(root, [])
        archived_artifact = artifact
        if archived_class != "memo":
            ancestor_count = chain_ancestor_count(abs_path, root, classification)
            if ancestor_count is not None:
                archived_artifact = {**artifact, "chain": {"ancestor_count": ancestor_count}}
        return _emit({
            "artifact": archived_artifact,
            "gates": {"claim": {}, "addressee": {}, "coast": compute_coast(judgment_points, tree_quiescence=tree_quiescence)},
            "directives": directives,
            "judgment_points": judgment_points,
            "narration": narration,
            "next_move": next_move,
            "preflight": {"tree_quiescence": tree_quiescence},
        }, EXIT_OK)

    if classification == "memo" and fm.get("status") in _MEMO_TERMINAL_STATUS:
        # M0 short-circuit (HEAD parity) — an already-`actioned`/`superseded`
        # memo is a read-only terminal artifact: surface the terminal
        # fields, emit no claim directive, never re-run M3 kind-dispatch.
        # `compute_reply_closure`/`_render_reply_closure` (a sender-repo
        # inbox/archive walk) gate whether "nothing further to do" actually
        # holds — an ask/consult memo isn't closed until the sender has a
        # reply in their own tree.
        memo_status = fm.get("status")
        tree_quiescence = compute_tree_quiescence(root, [str(s) for s in fm.get("scope") or []])
        base_narration = f"{display_path} is an {memo_status} memo — a terminal record."
        base_next_move = "Nothing further to do — this memo already closed."
        closure = compute_reply_closure(fm, display_path, root)
        judgment_points, narration, next_move = _render_reply_closure(
            closure, display_path, base_narration, base_next_move, status=memo_status
        )
        return _emit({
            "artifact": {**artifact, "terminal_state": {
                "status": memo_status, "decision": fm.get("decision"),
                "decision_note": fm.get("decision_note"), "actioned_note": fm.get("actioned_note"),
                "realized_by": fm.get("realized_by"), "superseded_by": fm.get("superseded_by"),
            }},
            "gates": {
                "addressee": {},
                "liveness_signal": False,
                "sender_reachability": compute_sender_reachability(fm.get("sent_by")),
                "coast": compute_coast(judgment_points, tree_quiescence=tree_quiescence),
            },
            "directives": [],
            "judgment_points": judgment_points,
            "narration": narration,
            "next_move": next_move,
            "preflight": {"tree_quiescence": tree_quiescence},
        }, EXIT_OK)

    if classification in ("handoff", "spinoff"):
        supersession = compute_supersession_gate(root, display_path, fm)
        if supersession is not None:
            jp = supersession["judgment_point"]
            supersession_judgment_points = [jp]
            tree_quiescence = compute_tree_quiescence(root, [])
            return _emit({
                "artifact": artifact,
                "gates": {
                    "supersession": supersession["gate"],
                    "coast": compute_coast(supersession_judgment_points, tree_quiescence=tree_quiescence),
                },
                "directives": [],
                "judgment_points": supersession_judgment_points,
                "narration": supersession["narration"],
                "next_move": supersession["next_move"],
                "preflight": {"tree_quiescence": tree_quiescence},
            }, EXIT_BUSINESS_FAIL)

    ancestor_count = chain_ancestor_count(abs_path, root, classification)
    artifact_out = dict(artifact)
    if ancestor_count is not None:
        artifact_out["chain"] = {"ancestor_count": ancestor_count}

    class_token = CLAIM_CLASS_MEMO if classification == "memo" else CLAIM_CLASS_HANDOFF
    basename = Path(display_path).name

    reclaimed: Optional[dict[str, Any]] = None
    if claim_at_brief and classification != "archived":
        reclaimed = acquire_brief_claim(root, class_token, basename, cwd=str(root))
        route_baton_adoption(root, display_path, fm)

    claim = gates_claim(abs_path, root, class_token, basename)
    claim_grant = compute_claim_grant(root, class_token, basename, display_path, cwd=str(root), fm=fm)
    liveness_fired = compute_liveness_signal(root, fm)

    scope = fm.get("scope") if isinstance(fm.get("scope"), list) else []
    tree_quiescence = compute_tree_quiescence(root, [str(s) for s in scope])

    if claim["holder"] is not None and claim_grant.get("verdict") != "granted":
        # Live-claim-holder stand-down (HEAD parity, both the handoff/
        # spinoff branch and the memo/handoff parity fix, cross-repo/inbox/
        # 2026-08-17-doe-claude-em-memo-claim-fires-after-the-em-can-
        # already-act.md): a DENIED grant against a live foreign holder
        # halts the brief before the artifact body is worth reading — no
        # directives, a liveness judgment point as the only offer. Row 2 of
        # `compute_claim_grant` (`held_by_self`) resolves `granted` for a
        # same-session re-brief, so this branch is never reached for that
        # case.
        live_claim_jp = build_liveness_judgment_point(liveness_fired, "gates.liveness_signal", [])
        live_claim_judgment_points = [jp for jp in (live_claim_jp,) if jp]
        if live_claim_judgment_points:
            claim_next_move = "Resolve the open judgment point(s) below before proceeding."
        else:
            claim_next_move = (
                f"{claim['holder']} holds the claim but no live signal fired for it — "
                "confirm by hand whether that session is still active before proceeding."
            )
        stand_down_gates: dict[str, Any] = {
            "claim": claim,
            "claim_grant": claim_grant,
            "liveness_signal": liveness_fired,
            "coast": compute_coast(
                live_claim_judgment_points, claim_grant=claim_grant, tree_quiescence=tree_quiescence
            ),
        }
        if classification == "memo":
            stand_down_gates["addressee"] = compute_addressee_gate(root, fm.get("to"))
            stand_down_gates["sender_reachability"] = compute_sender_reachability(fm.get("sent_by"))
        return _emit({
            "artifact": artifact,
            "gates": stand_down_gates,
            "directives": [],
            "judgment_points": live_claim_judgment_points,
            "narration": f"{display_path} is already claimed by {claim['holder']}.",
            "next_move": claim_next_move,
            "preflight": {"tree_quiescence": tree_quiescence},
        }, EXIT_BUSINESS_FAIL)

    stamp_gate_hit = compute_execution_stamp_match(root, fm, display_path)
    stamp_gate = stamp_gate_hit[0] if stamp_gate_hit else None
    stamp_gate_target = stamp_gate_hit[1] if stamp_gate_hit else None
    sender_reachability = compute_sender_reachability(fm.get("sent_by")) if classification == "memo" else None

    kind_resolved: Optional[str] = None
    kind_unrecognized = False
    if classification == "memo":
        kind_resolved, kind_unrecognized = resolve_memo_kind(fm)
        artifact_out["kind_resolved"] = kind_resolved

    directives = _build_directives(
        classification, display_path, basename, claim, claim_grant, fm, decisions or {}, kind_resolved,
        root=root, abs_artifact_path=abs_path,
    )
    if classification == "memo" and directives and _claim_already_self_held(root, class_token, basename):
        # Idempotent same-session re-entry (HEAD parity) — d1 (claim-
        # artifact) already landed for THIS session on a prior partial
        # apply; skip re-dispatching its handler rather than hard-failing
        # on the designed same-session-reclaim rejection.
        directives[0]["already_satisfied"] = True

    liveness_resolves = ["d2"] if classification in ("handoff", "spinoff") else [d["id"] for d in directives]
    judgment_points = _build_judgment_points(
        classification, liveness_fired, stamp_gate, kind_resolved, fm.get("kind"), kind_unrecognized,
        liveness_resolves,
    )

    shipped_state = compute_shipped_state(fm) if classification in ("handoff", "spinoff") else None
    if shipped_state is not None:
        judgment_points.append(build_shipped_state_judgment_point("gates.shipped_state", ["d2"]))

    # AC18 — the pre-tagged tier split (HEAD parity): `unstampable` promotes
    # to an unconditional re-stamp directive; `stale-substantive` already
    # contributed a `judgment_points[]` entry above via
    # `_build_judgment_points`/`build_execution_stamp_judgment_point`.
    # `match`/`stale-bookkeeping`/`None` contribute neither.
    if stamp_gate is not None and stamp_gate_target is not None:
        if stamp_gate["verdict"] == "unstampable":
            directives.append(build_execution_stamp_directive(stamp_gate, stamp_gate_target))

    gate_check: Optional[dict[str, Any]] = None
    if classification in ("handoff", "spinoff") and fm.get("deployment_state") == "awaiting_gate":
        # `awaiting_gate` branch (HEAD parity, Defect 3 + Piece A/B). The
        # corpus-wide `blocked_by` resolution (`compute_gate_blocker_
        # evidence`) is NOT one of DR-415's eight deletions and is itself a
        # PM-ruled kept behaviour (plan 2026-08-30-the-gate-brief-reads-a-
        # list-where-the-record-wrote-one, chunk C2/C3 — see
        # test_gate_check_shipped_blocker_evidence.py's own docstring), so
        # it is ported here rather than reduced.
        typed_meta = dag._read_meta(str(abs_path))
        blocked_by = typed_meta.get("blocked_by")
        gate_check = {
            "gate_dependency": fm.get("gate_dependency"),
            "blocked_by": blocked_by,
            "blocking_notes": fm.get("blocking_notes"),
            "gate_evidence": typed_meta.get("gate_evidence"),
        }
        gate_check["blockers"] = [
            {
                "stub_id": entry["id"],
                "status": entry["status"],
                "resolved": entry["resolved"],
                "deployment_state": entry["deployment_state"],
                "holder": entry["holder"],
                "holder_address": entry["holder_address"],
                "holder_live": entry["holder_live"],
            }
            for entry in compute_gate_blocker_evidence(root, blocked_by)
        ]
        gate_recommendation = compute_gate_check_recommendation(gate_check["blockers"])
        gate_jp = build_gate_check_judgment_point("gates.gate_check", ["d2", "d-gate-recheck"], recommendation=gate_recommendation)
        judgment_points.append(gate_jp)
        directives.append(build_gate_recheck_directive(display_path))

    if classification in ("handoff", "spinoff") and len(directives) >= 2:
        # `depends_on` for `d2` (claim-handoff) — AND-semantics across
        # every blocking judgment point that independently claims to gate
        # it (HEAD parity): `jgate` (+ `d-gate-recheck`, awaiting_gate),
        # `jshipped` (shipped), and `j1` (a firing liveness signal),
        # unconditional on any of the three — not only when `gate_check`
        # fires.
        blocking_ids: list[Any] = []
        if gate_check is not None:
            blocking_ids.append("jgate")
            blocking_ids.append("d-gate-recheck")
        if shipped_state is not None:
            blocking_ids.append("jshipped")
        if liveness_fired:
            blocking_ids.append("j1")
        if not blocking_ids:
            directives[1]["depends_on"] = None
        elif len(blocking_ids) == 1:
            directives[1]["depends_on"] = blocking_ids[0]
        else:
            directives[1]["depends_on"] = blocking_ids
    elif classification == "memo" and liveness_fired and directives:
        # Same gating rule, `d1`/`claim-memo-stamp` side — a memo carries
        # no `jgate`/`jshipped` (handoff-only gates), so `j1` is the only
        # blocking id a memo's claim directives can ever carry. Both the
        # session-claim (`d1`) and the archive-stamp claim-memo-stamp
        # directive share it (HEAD parity) — neither should land ahead of
        # a firing liveness signal.
        directives[0]["depends_on"] = "j1"
        if len(directives) >= 2:
            directives[1]["depends_on"] = "j1"
        if len(directives) >= 3:
            # `d-action-memo`'s own `depends_on` is unconditionally
            # `j-kind` (widened here to `["j-kind", "j1"]`, AND-semantics,
            # once liveness fires) — without this, a firing liveness
            # signal blocks only the claim-side directives while the
            # terminal memo write still lands unguarded by the contended-
            # claim judgment point ahead of it.
            existing = directives[2]["depends_on"]
            if existing in (None, "j1"):
                directives[2]["depends_on"] = "j1"
            elif isinstance(existing, list):
                if "j1" not in existing:
                    directives[2]["depends_on"] = [*existing, "j1"]
            else:
                directives[2]["depends_on"] = [existing, "j1"]

    is_handoff_like = classification in ("handoff", "spinoff")
    if is_handoff_like:
        completeness = build_completeness_checklist(fm, display_path)
        # Completeness-checklist directives (one `coordinator-tasks-mirror
        # init` per item, restart-gated-hoisted) and the probe-confirmation
        # judgment points are independent of the claim chain — appended, not
        # gated behind j1/jgate. They are appended BEFORE `compute_coast`
        # reads `judgment_points`: an unresolved untrusted-probe gate must
        # hold the coast, or an autonomous consumer reads "clear" past it.
        directives = directives + completeness["directives"]
        judgment_points = judgment_points + completeness["judgment_points"]
    else:
        completeness = {"items": None, "batches": None}

    coast = compute_coast(judgment_points, claim_grant, tree_quiescence)
    sizing = compute_sizing_disposition(root, fm) if is_handoff_like else None

    narration, next_move = _ready_summary(classification, directives, judgment_points)
    if classification in ("handoff", "spinoff"):
        from coordinator_core.sizing_disposition import unsized_next_move_prefix
        next_move = execution_phase_prefix(fm) + unsized_next_move_prefix(sizing) + next_move
        if claim_grant.get("held_by_self"):
            narration = f"Already held by you — resuming, not contending. {narration}"
            if not judgment_points:
                next_move = "Already held by you — resume. " + next_move
    reply_obligation: Optional[str] = None
    if classification == "memo":
        reply_prefix = reply_obligation_at_open(fm)
        if reply_prefix:
            next_move = reply_prefix + next_move
            reply_obligation = "reply-owed-on-action"

    gates_obj: dict[str, Any] = {
        "claim": claim,
        "claim_grant": claim_grant,
        "liveness_signal": liveness_fired,
        "coast": coast,
    }
    if stamp_gate is not None:
        gates_obj["execution_stamp_match"] = stamp_gate
    if shipped_state is not None:
        gates_obj["shipped_state"] = shipped_state
    if gate_check is not None:
        gates_obj["gate_check"] = gate_check
    if classification == "memo":
        gates_obj["sender_reachability"] = sender_reachability
        gates_obj["addressee"] = compute_addressee_gate(root, fm.get("to"))

    decision_object: dict[str, Any] = {
        "artifact": artifact_out,
        "gates": gates_obj,
        "directives": directives,
        "judgment_points": judgment_points,
        "narration": narration,
        "next_move": next_move,
        "sizing_disposition": sizing,
        "preflight": {
            "completeness_items": completeness["items"],
            "completeness_batches": completeness["batches"],
            "tree_quiescence": tree_quiescence,
        },
    }
    if classification == "memo":
        decision_object["preflight"]["reply_obligation"] = reply_obligation

    if reclaimed is not None:
        basis = reclaimed["basis"]
        if basis == "expired-brief-lease":
            basis_phrase = f"its {_claims.BRIEF_CLAIM_LEASE_MINUTES}-minute brief-stage lease elapsed"
        elif basis == "dead-holder":
            basis_phrase = "that session's process was confirmed gone"
        elif basis == "holder-absent":
            basis_phrase = (
                "no session directory or harness-registry record could be found for that "
                "session — liveness could not be checked at all"
            )
        else:
            basis_phrase = (
                "that session had not refreshed its claim inside the liveness window — "
                "liveness NOT confirmed dead, only inferred from inactivity"
            )
        age = reclaimed.get("claim_age_minutes")
        age_phrase = f" (claim was {age}m old)" if age is not None else ""
        note = (
            f"RECLAIMED from session {reclaimed['holder']} — {basis_phrase}{age_phrase}. "
            "That session may still believe it holds this; reconcile before acting externally on it. "
        )
        decision_object["narration"] = note + decision_object.get("narration", "")
        decision_object["gates"]["claim_reclaim"] = reclaimed

    addressee_gate = gates_obj.get("addressee") or {}
    addressee_business_fail = (
        addressee_gate.get("checked")
        and addressee_gate.get("exit_code") not in (0, None)
        and not os.environ.get("COORDINATOR_OVERRIDE_MEMO_ADDRESSEE")
    )
    if addressee_business_fail:
        # `cross_seat_override` names the bypass only where the gate actually
        # fires — an addressee gate that passed has no override to offer.
        decision_object["gates"]["addressee"] = {
            **addressee_gate,
            "cross_seat_override": "COORDINATOR_OVERRIDE_MEMO_ADDRESSEE",
        }
        decision_object["directives"] = []
        decision_object["judgment_points"] = []
        decision_object["narration"] = (
            f"{artifact['path']} names an addressee this session is not — "
            f"{addressee_gate.get('message', 'addressee mismatch')}."
        )
        decision_object["next_move"] = (
            "Confirm the addressee, or set COORDINATOR_OVERRIDE_MEMO_ADDRESSEE "
            "if you and the PM judge otherwise."
        )
    exit_code = (
        EXIT_BUSINESS_FAIL
        if claim_grant.get("verdict") == "denied" or addressee_business_fail
        else EXIT_OK
    )
    return _emit(decision_object, exit_code)


def _strip_aside(artifact_arg: str) -> str:
    """Strips a documented trailing ` -- <prose>` EM-facing aside off the
    whole multi-artifact argument before any path-splitting runs, so the
    aside is never glued onto the last artifact's path (2026-08-11 defect:
    an unstripped aside on an ` AND `-joined argument corrupted the final
    path's resolution instead of surfacing as prose).

    Matches ONCE, against the raw argument as a whole — the aside is
    documented as trailing the entire multi-artifact expression, not any
    one bullet line, so this runs before bullet-line splitting reassembles
    a newline-separated grab into the ` AND `-joined form below.
    """
    match = _ASIDE_RE.search(artifact_arg)
    if match is None:
        return artifact_arg
    return artifact_arg[: match.start()]


def _reassemble_bullet_lines(artifact_arg: str) -> str:
    """Recombines a pasted markdown bullet list of artifact paths — one per
    line, each optionally prefixed with `- `/`* ` and leading indentation —
    into the ` AND `-joined form `split_artifact_args` already knows how to
    split, so a newline-separated grab and an ` AND `-joined grab dispose
    through the identical downstream path.

    CONTRACT REVERSAL (`_baton_unification_routing_enabled` is now True).
    The rule this function was written under — "N independent dispositions,
    one decision object per artifact" — described the pre-unification
    world. One decision object per artifact still holds, and this function
    is unchanged by the flip: the splitting is what makes the paths
    separable at all. What no longer holds is the INDEPENDENCE of the
    dispositions downstream. A multi-artifact grab whose members are
    inheritable batons now converges: each is claimed, and the held set
    unifies into ONE successor carrying them as fan-in legs, rather than N
    batons standing separately. Do not restore the old sentence from a
    reading of this function alone — the reversal lives one layer down, in
    the routing, not in the parsing.

    Only fires when the input contains a REAL newline AND at least one
    resulting line is bullet-prefixed. An unmarked multi-line paste (no
    bullets at all) is left byte-identical — that shape is the existing
    hard-line-wrap-inside-ONE-path signal `_sanitize_artifact_path_str`
    already tolerates as a single mid-token wrap, and reinterpreting every
    newline here as an artifact boundary would break that fallback and
    silently fragment a single long path instead.
    """
    if "\n" not in artifact_arg and "\r" not in artifact_arg:
        return artifact_arg
    raw_lines = re.split(r"\r\n|\r|\n", artifact_arg)
    if not any(_BULLET_PREFIX_RE.match(line) for line in raw_lines):
        return artifact_arg
    paths = [
        stripped
        for line in raw_lines
        if (stripped := _BULLET_PREFIX_RE.sub("", line, count=1).strip())
    ]
    if not paths:
        return artifact_arg
    return " AND ".join(paths)


def _expand_braces(artifact_arg: str) -> list[str]:
    """Expand ONE `PREFIX{a,b,c}SUFFIX` brace group into N literal paths.

    Alternatives are comma-split at the group's own nesting depth (a nested
    `{...}` inside an alternative doesn't fragment the split) and each
    alternative is whitespace/newline-stripped, so a shell-style line-wrapped
    brace list (`{a,\n  b}`) resolves the same as `{a,b}`. A string with no
    `{`, or an unbalanced `{` with no matching `}` at the same depth, passes
    through UNCHANGED as `[artifact_arg]` — this is the no-behavior-change
    guarantee for every pre-existing single-path caller.

    A second (or further) brace group in the same string is expanded by
    recursing on each already-substituted alternative; each recursive call
    operates on a strictly shorter string than its parent (the matched group
    is replaced by one alternative), so recursion is depth-bounded by the
    number of brace groups in the input and cannot loop.
    """
    start = artifact_arg.find("{")
    if start == -1:
        return [artifact_arg]
    depth = 0
    end = -1
    for i in range(start, len(artifact_arg)):
        ch = artifact_arg[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return [artifact_arg]

    prefix = artifact_arg[:start]
    suffix = artifact_arg[end + 1 :]
    inner = artifact_arg[start + 1 : end]

    alternatives: list[str] = []
    nested_depth = 0
    current: list[str] = []
    for ch in inner:
        if ch == "{":
            nested_depth += 1
            current.append(ch)
        elif ch == "}":
            nested_depth -= 1
            current.append(ch)
        elif ch == "," and nested_depth == 0:
            alternatives.append("".join(current))
            current = []
        else:
            current.append(ch)
    alternatives.append("".join(current))

    if len(alternatives) < 2:
        return [artifact_arg]

    expanded: list[str] = []
    for alt in alternatives:
        combined = prefix + alt.strip() + suffix
        expanded.extend(_expand_braces(combined))
    return expanded


#: Standalone multi-artifact join token. `/pickup` is explicitly multi-artifact
#: ("one decision object per artifact") and callers commonly join paths with the
#: bare word ` AND `. Split is whitespace-bounded and case-sensitive on purpose:
#: the `\s+AND\s+` boundary never fires inside a path segment (`/BRAND/`,
#: `COMMAND.md`, a lowercase `and`), so single-path behavior is untouched.
_ARTIFACT_JOIN_RE = re.compile(r"\s+AND\s+")


#: A markdown list-item bullet at the start of a line — optional leading
#: horizontal whitespace (a nested/indented sub-bullet renders the same as a
#: top-level one when a caller pastes a bullet list; both mean "one more
#: artifact"), then a literal `-`/`*` marker, then required whitespace before
#: the path itself.
_BULLET_PREFIX_RE = re.compile(r"^[ \t]*[-*][ \t]+")


#: Trailing EM-facing aside marker (contract § Multi-Artifact Grab:
#: `pickup a AND b -- <free text>`). Recognized once, at the FIRST standalone
#: ` -- ` (whitespace-bounded on both sides, so a hyphenated path segment is
#: never mistaken for it) — everything from there to the end of the argument
#: is the aside, not an artifact-path candidate.
_ASIDE_RE = re.compile(r"\s+--\s+")


def split_artifact_args(artifact_arg: str) -> list[str]:
    """Split a `brief` argument on the standalone ` AND ` token into N paths,
    then brace-expand (`PREFIX{a,b,c}SUFFIX`) each resulting path.

    A single path with no standalone ` AND ` and no `{...}` group returns
    `[path]` unchanged. Empty/whitespace-only segments are dropped; a
    fully-empty result degrades to `[artifact_arg]` so the caller still gets
    a resolvable-or-failing single entry rather than an empty batch.

    A trailing ` -- <prose>` EM-facing aside is stripped first
    (`_strip_aside`) so it is never swallowed as (part of) a path. A
    newline-separated bullet list is then reassembled into the ` AND `-joined
    form (`_reassemble_bullet_lines`) before the split below runs, so
    bullets, `AND`, and brace groups all compose freely.
    """
    working = _reassemble_bullet_lines(_strip_aside(artifact_arg))
    parts = [p.strip() for p in _ARTIFACT_JOIN_RE.split(working)]
    paths = [p for p in parts if p]
    if not paths:
        paths = [artifact_arg]
    expanded: list[str] = []
    for path in paths:
        expanded.extend(_expand_braces(path))
    return expanded


def brief_multi(
    artifact_arg: str, decisions: Optional[dict[str, Any]] = None, repo_root: Optional[Path] = None
) -> list[BriefResult]:
    paths = split_artifact_args(artifact_arg)
    claim_at_brief = len(paths) == 1
    return [brief(p, decisions, claim_at_brief=claim_at_brief, repo_root=repo_root) for p in paths]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _usage(prog: str, stream=None) -> int:
    stream = sys.stderr if stream is None else stream
    print(f"usage: {prog} brief <artifact-path> [--decisions <json> | --decisions-file <path>]", file=stream)
    print(f"       {prog} apply <artifact-path> [--session-id <id>] [--decisions <json> | --decisions-file <path>]", file=stream)
    print(f"       {prog} drop <artifact-path> [--session-id <id>]", file=stream)
    print(f"       {prog} stamp-check <plan-path>", file=stream)
    return EXIT_USAGE


#: `--decisions[jp_id]` optional content keys `cs_action_memo` accepts
#: alongside `disposition`/`value` — ported verbatim from
#: `pickup_assemble.DISPOSITION_CONTENT_KEYS` (same closed set,
#: `validate_decisions_shape` below is this module's own copy of that
#: validator, not an import — this module's import closure must not
#: include `coordinator_core.pickup_assemble`).
DISPOSITION_CONTENT_KEYS = (
    "decision_note",
    "realized_by",
    "actioned_note",
    "distill_fate",
    "in_repo_capture",
)


def validate_decisions_shape(decisions: Any) -> Optional[str]:
    """Validates a parsed `--decisions` JSON value against the required
    judgment-point-id -> `{"disposition": <str>, ...}` map shape, failing
    loud on a wrong-shaped payload rather than silently leaving the
    judgment point unresolved. `disposition` is required on every entry,
    or its exact equivalent `value` (normalized to `disposition` in
    place). Returns `None` when `decisions` is a valid map (including the
    empty map), else a one-line actionable error string naming the first
    offending id and the expected shape."""
    if not isinstance(decisions, dict):
        return (
            f"--decisions must be a JSON object mapping judgment-point id to "
            f'{{"disposition": <value>}}, got {type(decisions).__name__}'
        )
    allowed_keys = {"disposition", "value", *DISPOSITION_CONTENT_KEYS}
    expected_form = (
        '{"disposition": "<value>"'
        + "".join(f', "{key}": "<value>"' for key in DISPOSITION_CONTENT_KEYS)
        + " (all but disposition optional)}"
    )
    for jp_id, value in decisions.items():
        if not isinstance(value, dict):
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {expected_form}}}'
            )
        has_disposition = "disposition" in value
        has_value = "value" in value
        if not has_disposition and not has_value:
            return (
                f"--decisions[{jp_id!r}] must be shaped "
                f'{{"disposition": <value>}}, got {value!r} — expected form: '
                f'{{"{jp_id}": {expected_form}}}'
            )
        if has_disposition and has_value and value["disposition"] != value["value"]:
            return (
                f"--decisions[{jp_id!r}] carries both 'disposition' "
                f"({value['disposition']!r}) and 'value' ({value['value']!r}) "
                f"and they disagree — supply only one"
            )
        unknown_keys = set(value) - allowed_keys
        if unknown_keys:
            return (
                f"--decisions[{jp_id!r}] has unrecognized key(s) "
                f"{sorted(unknown_keys)!r} — accepted keys are "
                f"{sorted(allowed_keys)!r}"
            )
        if not has_disposition:
            value["disposition"] = value.pop("value")
        elif has_value:
            del value["value"]
        if value.get("distill_fate") == "ratification" and not value.get("in_repo_capture"):
            return (
                f"--decisions[{jp_id!r}] sets distill_fate=\"ratification\" but "
                f"omits \"in_repo_capture\" — supply the in-repo capture path "
                f'(e.g. "docs/decisions/..." or "docs/plans/...") the '
                f"ratification was captured to; cs_action_memo hard-requires it "
                f"for this distill_fate"
            )
    return None


def main(argv: list[str]) -> int:
    """Handles `brief` itself; delegates `apply`/`drop`/`stamp-check` to
    `coordinator_core.pickup_assemble.main` through a lazy import (C11's
    shim change is one line; those three verbs are unchanged by this row)."""
    if not argv:
        return _usage("pickup-assemble")
    if argv[0] in ("--help", "-h"):
        _usage("pickup-assemble", stream=sys.stdout)
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd in ("apply", "drop", "stamp-check"):
        from coordinator_core.pickup_assemble import main as _monolith_main
        return _monolith_main(argv)

    if subcmd != "brief":
        print(f"pickup-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return _usage("pickup-assemble")

    if not rest:
        return _usage("pickup-assemble")

    artifact_path = rest[0]
    tail = rest[1:]
    decisions: dict[str, Any] = {}
    conflict = detect_conflicting_payload_channels(tail)
    if conflict is not None:
        print(f"pickup-assemble: {conflict}", file=sys.stderr)
        return EXIT_USAGE
    i = 0
    while i < len(tail):
        tok = tail[i]
        if (payload := resolve_json_payload_flag(tail, i)).consumed:
            if payload.error is not None:
                print(f"pickup-assemble: {payload.error}", file=sys.stderr)
                return EXIT_USAGE
            decisions = payload.value
            i += payload.consumed
        else:
            print(f"pickup-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return EXIT_USAGE

    shape_error = validate_decisions_shape(decisions)
    if shape_error is not None:
        print(f"pickup-assemble: {shape_error}", file=sys.stderr)
        return EXIT_USAGE

    try:
        results = brief_multi(artifact_path, decisions)
    except _TransportFailure as exc:
        failure = _emit({
            "error": str(exc), "transport_failure": True,
            "narration": f"Could not compute a brief: {exc}.",
            "next_move": "Confirm the command is run from inside a git worktree, then retry.",
        }, EXIT_TRANSPORT_FAIL)
        print(json.dumps(failure.decision_object))
        return failure.exit_code
    except Exception as exc:  # noqa: BLE001 - structural backstop
        failure = _emit({
            "error": str(exc), "transport_failure": True,
            "narration": f"brief() raised an unexpected exception: {exc}.",
            "next_move": "Re-run; if this repeats, report the traceback.",
        }, EXIT_TRANSPORT_FAIL)
        print(json.dumps(failure.decision_object))
        return failure.exit_code

    if len(results) == 1:
        payload: Any = results[0].decision_object
        exit_code = results[0].exit_code
    else:
        payload = [r.decision_object for r in results]
        exit_code = max(r.exit_code for r in results)

    print(json.dumps(payload, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

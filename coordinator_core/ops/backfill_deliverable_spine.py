"""
coordinator_core.ops.backfill_deliverable_spine — deliverable_id backfill for the
fleet artifact spine (handoff / plan / spinoff / roadmap / completion corpus).

Purpose: Group the board-relevant artifact corpus by workstream, emit a
human-reviewable GROUPING REPORT, then (with ``write=True``) stamp
``deliverable_id`` onto mutable artifacts. Archived/immutable files are
reported (derive-at-emit) but NEVER written. Already-threaded artifacts
(carrying ``deliverable_id``) are never re-minted.

Corpus: handoff / plan / spinoff / roadmap / completion / archived-spec / sizing entities.
    In scope:  state/handoffs/*.md   (active + spinoffs)
               archive/handoffs/**   (archived handoffs — immutable)
               docs/plans/*.md       (non-sidecar plans)
               archive/completed/**  (completion entries — immutable)
               archive/specs/**      (archived specs, moved there by
                                       ``fleet.archive_plans`` — MUTABLE, unlike
                                       archive/handoffs and archive/completed; a
                                       mint-where-absent leg, never a migration
                                       of an existing id)
               state/roadmap/**/OVERVIEW.md  (roadmaps)
               docs/ROADMAP.md
               state/sizings/*.yaml  (whole-document YAML, no frontmatter fence — see
                                       ``read_yaml_document``)
    Out of scope: lessons, backlogs, memos, decisions, wiki, tasks/, templates/

    A fourth, orthogonal leg mints ``plan_id`` (``pln-<slug>-<6hex>``, per-file
    identity, never shared across a group the way ``deliverable_id`` is) onto
    ``docs/plans/*.md`` and ``archive/specs/**`` records lacking one. See
    ``extract_plan_id`` / ``_mint_plan_id`` / ``run_plan_id_leg``.

Grouping key:
    Plans / handoffs / spinoffs: ``workstream:`` frontmatter field.
    Completions: ``chain:`` field (semantic equivalent in that artifact type).
    Roadmaps: ``workstream:`` field (if present).
    Sizings: the citing plan's ``deliverable_id``, resolved via the reverse
        edge a plan authors at mint time (``sizing_object:`` pointing AT the
        sizing). ``build_plan_sizing_index`` walks the plan corpus once,
        independently of this module's own grouping loop, mapping each
        cited sizing path to its citing plan's ``deliverable_id``;
        ``get_workstream_key`` looks a sizing up in that index. A sizing with
        no citing plan (or whose citing plan has no ``deliverable_id`` of its
        own yet) is reported UNKEYED — a distinct, named outcome from the
        UNKNOWN group other kinds fall into when their workstream field is
        absent, per AC9. UNKEYED does not invent a synthetic grouping key.
    Null / absent key -> artifact placed in UNKNOWN group (UNKEYED for sizings).

Ambiguity (fail-loud, exit 2 in dry-run; blocked in write mode):
    A group is ambiguous when it contains 2+ distinct plan slugs with NO
    shared predecessor link — indicating two unrelated deliverables collided
    on the same workstream string, or that a workstream string drifted.

Idempotency:
    Artifacts already carrying ``deliverable_id:`` are reported as
    "already-threaded" and skipped (carry rule D1 — never re-mint).

Port of: backfill-deliverable-spine.sh (DoE ca30f76c, 2026-07-17).
BIG_PORT wave, direct-import trampoline variant #1 — no
``register_op``, no IPC; the DoE-side polyglot trampoline imports and calls
``main()`` in-process, exactly like coordinator_core.hooks.auto_push /
coordinator_core.ops.handoff_gate_aging / coordinator_core.ops.backfill_initiative_fk.

Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § C5, AC7
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § BIG_PORT Wave C

Public API:
    main(argv, default_coordinator_root=None) -> int
        CLI entry point. ``argv`` is the trampoline's own ``sys.argv[1:]``.
        ``default_coordinator_root`` is used when ``--root`` is not passed on
        the command line — the ``coordinator/bin/backfill-deliverable-spine.py``
        trampoline resolves DoE-claude's own ``coordinator/`` directory (via
        the shared ``doe_root()`` registry helper, NOT its own ``__file__``
        location — this executable now lives in claude-klabauter while the
        corpus it walks, DoE-claude's ``coordinator/{state,docs,archive}``,
        stayed in DoE-claude) and passes it here. If BOTH ``--root`` and
        ``default_coordinator_root`` are absent (e.g. ``doe_root()`` could
        not resolve on this machine either), this module fails loud
        (exit 1) rather than falling back to its own ``coordinator_core/ops/``
        directory — that directory has none of the corpus subdirs and would
        silently enumerate an empty corpus instead of erroring.

Exit-code contract (byte-parity with the bash oracle):
    0 — clean (no ambiguities; write applied or dry-run report emitted)
    1 — fatal error (bad arguments, corpus root unreadable)
    2 — ambiguous groups detected (human review required before --write)
    (transport/import failure — CLAUDE_KLABAUTER_ROOT resolution or `coordinator_core`
    import failing before this module is even reached — is NOT a code this
    function can return; it is handled by the DoE trampoline itself, which
    uses a DEDICATED exit code 3 for that case per the porter addendum §3b
    fail-loud-gate-script rule, since this tool is a mutating fail-loud CLI,
    not a best-effort/never-block one, and codes 0/1/2 are all live business
    outcomes here. See the trampoline's own Usage comment block.)
    4 — a --write pass refused one or more writes (a named skip fired inside
    `_stamp_file`/`_stamp_yaml_document` — LockTimeout, MutateAbort,
    FileNotFoundError, or UnicodeDecodeError) — codes 0-3 above keep their
    prior meanings; 4 is additive, never overloading an existing code, and is
    distinct from 3 (which is the DoE trampoline's own transport-failure
    code, never returned by this function).

Departure from the oracle (additive robustness, not a behavior change to any
tested path):
    - The oracle's C3a dependency (`bin/mint-deliverable-id.sh` must exist and
      be executable, hard-checked at startup with a "C3a must land before C5"
      remediation message) is now a structural Python import-time guarantee:
      `mint-deliverable-id.sh` was itself ported to
      `coordinator_core.ops.mint_deliverable_id` in an earlier wave, and this
      module calls that module's pure `mint()` function DIRECTLY (in-process,
      same package) rather than shelling out to the bash trampoline the
      oracle invoked via `bash "$MINT_HELPER" --slug ...`. This is strictly
      cheaper (no subprocess hop, no CLAUDE_KLABAUTER_ROOT re-resolution) and produces
      byte-identical mint output, since `mint_deliverable_id.mint()` is
      itself a faithful port of the same bash logic `mint-deliverable-id.sh`
      used to shell out to. If `coordinator_core.ops.mint_deliverable_id`
      cannot be imported, that surfaces as an ordinary Python ImportError at
      trampoline-import time (transport-failure class), not a business exit
      code from this module.
    - Corpus enumeration is explicitly sorted (alphabetical, per directory)
      before files are appended to the working corpus list. The bash oracle's
      `find ... -print0` enumeration order is filesystem/inode-dependent and
      was never a specified behavior to reproduce (verified empirically:
      re-running the oracle against the same fixture returns files in
      creation order on some filesystems and reverse order on others) — only
      the SET of grouped artifacts and the ambiguity/immutability verdicts
      are the correctness contract. Sorting makes output deterministic across
      runs and platforms without changing any of those verdicts.
    - Atomic frontmatter rewrites additionally `os.chmod` the temp file to
      the original file's mode bits before `os.replace` (porter addendum §5,
      permission preservation) — the bash oracle's `awk ... > $TMPFILE; mv
      $TMPFILE $gf` did NOT preserve the original file's permission bits
      either (the temp file got default/umask permissions from shell
      redirection); this port closes that latent gap defensively even though
      the corpus is markdown-only (never executable) today.

Sizing corpus extension (C5, this module's second parser class):
    `state/sizings/*.yaml` records are whole-document YAML — no `---`
    frontmatter fence at all — so `extract_fm_field`'s fence-scanning parser
    structurally cannot read them (it would scan for a SECOND `---` that
    never appears and return "" for every field, silently). `read_yaml_document`
    is a second, dedicated parser class for this shape: `yaml.safe_load` over
    the whole file, returning `{}` (not raising) on any read/parse failure —
    matching `extract_fm_field`'s own "absent/unreadable/empty all return
    empty" contract so a malformed sizing record degrades to UNKEYED rather
    than crashing the whole corpus walk. `classify_artifact` gains a
    `"sizing"` arm, a class distinct from every existing one (AC8).

    C5b (join-key leg, unblocked once C0 vendored `deliverable_id` onto
    `sizing-object.schema.json` at 1.8.0): `extract_deliverable_id` dispatches
    by artifact class — a sizing's `deliverable_id` is read via
    `read_yaml_document` (there is no `---` fence to scan), every other class
    still via `extract_fm_field`. Minting a NEW id onto a sizing reuses
    `group_corpus`/`_find_group_id`'s existing carry-or-mint machinery
    unmodified; the only new surface is the write mechanism itself,
    `_stamp_yaml_document`, because `_stamp_file`'s fence-anchored injection
    point does not exist on a fenceless whole-document YAML file — it inserts
    `deliverable_id:` as the second line via plain text-line surgery (never a
    `yaml.safe_load` + re-dump round-trip, which would silently drop this
    corpus's load-bearing inline comments). `_stamp_deliverable_id` dispatches
    between the two stamping mechanisms by artifact class.

    The question C5b's own task body left open — whether the sizing group
    key should key off `deliverable_id` once readable, rather than the
    (never-populated) `workstream:` document key C5 originally wired up — is
    answered here: `get_workstream_key`'s sizing arm now resolves through the
    citing plan's `sizing_object:` back-pointer (a 1:1 reverse edge authored
    at plan-mint time) to that plan's `deliverable_id`, via
    `build_plan_sizing_index`. See "Grouping key" above and the "load-bearing
    design note" in
    docs/plans/2026-08-10-sizing-grouping-key-is-the-plan-back-pointer.md for
    why the index must be built by its own pass over the plan corpus rather
    than reused from this module's grouping loop (`group_corpus` short-
    circuits an already-`deliverable_id`-threaded plan out of `group_files`
    before any grouping key is computed, and most plans are already
    threaded).

    Backfilling the live `state/sizings/` corpus (an actual `--write` run
    over it) WAS out of scope for an earlier plan (DoE asked for the
    capability, not the run, at that time) — a later plan
    (pln-spec-backlinks-cite-a-stable-d-451b3e § C2)
    explicitly re-scopes it back in: 148 of 266 sizings measured lacking a
    `deliverable_id` are backfilled by an ordinary `--write` run the same as
    any other in-scope corpus member, via the existing sizing-only-group
    `AC6` write-pass branch below.

Write scoping (`--only-kind`):
    `--write` is corpus-wide by construction: the named-group write set on
    this repo is 56 sizings PLUS 8 handoffs and 1 plan, so a run intended to
    close the sizing retro also stamps 9 unrelated artifacts — the
    `2026-08-01-deliverable-id-fork-remediation` anti-scope's "may stamp
    unrelated artifacts" hazard, with no flag to discharge it.
    `--only-kind <class>` (repeatable) restricts which artifact classes the
    WRITE PASS may touch. It deliberately does NOT filter enumeration,
    grouping, or the report: the ambiguity contract ("Cannot --write while
    ambiguous groups exist") is computed over the whole corpus, and narrowing
    the corpus would let a scoped run write into a group a full run would
    have refused. Out-of-scope files are reported `[skip-out-of-scope]`.
    A group with no in-scope mutable file is skipped BEFORE `_find_group_id`
    runs, so a scoped run never mints an id it will not stamp.

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - `extract_fm_field` reads only the FIRST frontmatter fence block (between
      the first pair of `---` delimiters) and returns the REMAINDER OF THE
      LINE after `field:` (not just the first whitespace token) — this
      deliberately differs from `coordinator_core.ops._fm_util
      .extract_frontmatter_scalar`, which returns only the first
      whitespace-delimited token. The two helpers are NOT interchangeable;
      this module does not reuse `_fm_util` because the oracle's awk pattern
      (`sub("^" field ":[[:space:]]*", "", val)` over the WHOLE line, not
      split on whitespace) has different multi-word-value semantics.
    - `is_sidecar_plan` enumerates an enumerated, non-exhaustive suffix list
      (prior-art-check / coverage-check / plan-coverage-check / code-review /
      code-review-slice-* / codereview-item* / the Staff Engineer-review / the Director of Engineering-review /
      docs-check / doc-link-check / review) — a new sidecar-type convention
      invented after this port lands will silently NOT be excluded, exactly
      as it would silently not have been excluded by the bash oracle either.
    - The write-mode per-group "does any file already carry an id" idempotency
      re-check (mirroring the oracle's defensive re-scan inside the write
      loop) is preserved even though it is provably unreachable within a
      single invocation (group membership already excludes already-threaded
      files at scan time) — kept for byte-parity with the oracle's control
      flow and because it is genuinely load-bearing across two SEPARATE
      invocations of the same corpus (a prior partial `--write` run that
      stamped some-but-not-all files in a group).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, IO, List, Optional, Tuple

from pathlib import Path

from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.ops.mint_deliverable_id import mint as _mint
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.wire_paths import rel_id

# Generator-provenance declaration (generator_provenance.py). _stamp_file()
# stamps deliverable_id onto whichever mutable artifacts in the corpus
# (state/handoffs/*.md, docs/plans/*.md, archive/specs/**, state/roadmap/**/
# OVERVIEW.md -- see the module docstring's "Corpus" section) currently lack
# the field -- a data-dependent set, declared MUTATES rather than a fixed
# GENERATES artifact.
MUTATES = [
    "state/handoffs/*.md",
    "docs/plans/*.md",
    "archive/specs/**/*.md",
    "state/roadmap/**/OVERVIEW.md",
]

_PROG = "backfill-deliverable-spine"
_UNKNOWN_GROUP_KEY = "__UNKNOWN__"
_UNKEYED_SIZING_GROUP_KEY = "__UNKEYED_SIZING__"

# ---------------------------------------------------------------------------
# unit 1 — frontmatter extraction + path-classification predicates
# (mirrors oracle L110-235: extract_fm_field / is_immutable_path / is_sidecar_plan)
# ---------------------------------------------------------------------------

_FM_FENCE_RE = re.compile(r"^---[ \t]*$")
_YAML_NULLS = {"null", "~"}

_SIDECAR_SUFFIXES = (
    ".prior-art-check.md",
    ".coverage-check.md",
    ".plan-coverage-check.md",
    ".code-review.md",
    ".the Staff Engineer-review.md",
    ".the Director of Engineering-review.md",
    ".docs-check.md",
    ".doc-link-check.md",
    ".review.md",
)


def extract_fm_field(path: str, field_name: str) -> str:
    """Scalar value for `field_name` from the first YAML frontmatter fence block.

    Faithful port of the oracle's `extract_fm_field` awk one-liner. Reads ONLY
    the first `---`-delimited block; takes the REST OF THE LINE after
    `field_name:` (not just the first token); strips one layer of matching
    surrounding quotes; treats bare YAML `null`/`~` as empty. Returns "" when
    absent, unreadable, or empty.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        print(f"skip: extract_fm_field: with open(path, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""

    prefix = field_name + ":"
    fence_count = 0
    for line in text.splitlines():
        if _FM_FENCE_RE.match(line):
            fence_count += 1
            if fence_count == 2:
                break
            continue
        if fence_count != 1:
            continue
        if not line.startswith(prefix):
            continue
        val = line[len(prefix):]
        # awk `sub("^field:[[:space:]]*", ...)` — strip only LEADING whitespace,
        # not trailing (the oracle does not trim trailing whitespace either).
        val = val.lstrip(" \t")
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1]
        elif len(val) >= 2 and val[0] == "'" and val[-1] == "'":
            val = val[1:-1]
        if val in _YAML_NULLS:
            val = ""
        return val
    return ""


def read_yaml_document(path: str) -> Dict[str, object]:
    """Second parser class: whole-document YAML, for `state/sizings/*.yaml`.

    A sizing record carries NO `---` frontmatter fence at all — the whole
    file IS the document. `extract_fm_field`'s fence-scanning parser cannot
    read these (it waits for a second `---` that never comes) and this reader
    exists precisely to close that gap, not to replace `extract_fm_field` for
    the markdown-with-frontmatter corpus.

    Returns `{}` — never raises — on a missing file, an unreadable file, a
    YAML parse error, or a document that does not parse to a mapping (e.g. a
    bare scalar or list at the top level). Mirrors `extract_fm_field`'s own
    "absent/unreadable/empty -> empty" contract so a malformed sizing record
    degrades to an UNKEYED outcome rather than crashing the corpus walk.
    """
    try:
        import yaml
    except ImportError:
        print(f"skip: read_yaml_document: import yaml failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
    except OSError:
        print(f"skip: read_yaml_document: with open(path, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    except yaml.YAMLError:
        print(f"skip: read_yaml_document: yaml.safe_load(fh) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def extract_deliverable_id(path: str, artifact_class: str) -> str:
    """`deliverable_id` for `path`, dispatched by `artifact_class` (C5b).

    `extract_fm_field` reads only the first `---`-fenced block; a sizing
    record has no fence at all, so a sizing must be read via
    `read_yaml_document` instead — reusing `extract_fm_field` on a sizing
    path would silently return "" for every sizing regardless of whether
    `deliverable_id` is actually set (fence_count never reaches 1). Mirrors
    the same "absent/unreadable/empty -> empty" contract both readers share.
    """
    if artifact_class == "sizing":
        doc = read_yaml_document(path)
        val = doc.get("deliverable_id")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return ""
    return extract_fm_field(path, "deliverable_id")


def extract_plan_id(path: str, artifact_class: str) -> str:
    """`plan_id` for `path`, dispatched by `artifact_class` — mirrors
    `extract_deliverable_id`'s shape/dispatch exactly (see that function's
    docstring for why a sizing must route through `read_yaml_document`
    rather than the fence-scanning `extract_fm_field`). The `plan_id` leg
    itself only ever walks `docs/plans/`/`archive/specs/` (see
    `run_plan_id_leg`), but this helper stays fully dispatched for parity
    with its sibling and to avoid a silent "" on a future caller that does
    pass a sizing path.
    """
    if artifact_class == "sizing":
        doc = read_yaml_document(path)
        val = doc.get("plan_id")
        if isinstance(val, str) and val.strip():
            return val.strip()
        return ""
    return extract_fm_field(path, "plan_id")


def is_immutable_path(path: str) -> bool:
    """True when `path` lives in an immutable archive path (never written)."""
    norm = path.replace(os.sep, "/")
    return "/archive/handoffs/" in norm or "/archive/completed/" in norm


def is_sidecar_plan(path: str) -> bool:
    """True when the plan file is a sidecar (not a primary plan artifact).

    Two sidecar conventions:
      Old: foo.md.sidecar-type.md   (double-md; caught by the `.md.` + `.md` check)
      New: foo.sidecar-type.md      (single-md suffix; explicit suffix list)
    """
    base = os.path.basename(path)
    # Old double-md convention: literal ".md." substring followed eventually by ".md".
    if ".md." in base and base.endswith(".md"):
        return True
    for suffix in _SIDECAR_SUFFIXES:
        if base.endswith(suffix):
            return True
    # code-review-slice-*.md / codereview-item*.md / plan-coverage-check.*.md —
    # variable-infix patterns, checked explicitly.
    if base.endswith(".md"):
        stem = base[: -len(".md")]
        if ".code-review-slice-" in stem:
            return True
        if ".codereview-item" in stem:
            return True
        if ".plan-coverage-check." in stem:
            return True
    return False


# ---------------------------------------------------------------------------
# unit 2 — classify_artifact / get_workstream_key
# (mirrors oracle L196-235)
# ---------------------------------------------------------------------------


def classify_artifact(path: str) -> str:
    """Artifact class: plan | handoff | handoff-archived | completion | archived-spec | roadmap | sizing | unknown."""
    norm = path.replace(os.sep, "/")
    if "/docs/plans/" in norm and norm.endswith(".md"):
        return "plan"
    if "/state/handoffs/" in norm and norm.endswith(".md"):
        return "handoff"
    if "/archive/handoffs/" in norm:
        return "handoff-archived"
    if "/archive/completed/" in norm:
        return "completion"
    if "/archive/specs/" in norm and norm.endswith(".md"):
        return "archived-spec"
    if norm.endswith("/docs/ROADMAP.md"):
        return "roadmap"
    if "/state/roadmap/" in norm and norm.endswith("/OVERVIEW.md"):
        return "roadmap"
    if "/state/sizings/" in norm and norm.endswith(".yaml"):
        return "sizing"
    return "unknown"


def build_plan_sizing_index(
    coordinator_root: str,
) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Reverse edge (AC1): sizing_object path -> citing plan's deliverable_id.

    Built by its OWN pass over `docs/plans/*.md` (non-sidecar), independent
    of `group_corpus`'s grouping loop — that loop short-circuits an artifact
    already carrying `deliverable_id` into `already_threaded` BEFORE any
    grouping key is computed, and the majority of local plans are already
    threaded. A plan that cites a sizing via `sizing_object:` is therefore
    usually NOT in `group_files`, but its `deliverable_id` is exactly the
    value a citing sizing needs to inherit — so this index must see
    already-threaded plans too, which the grouping loop deliberately does
    not. Only a plan carrying BOTH `sizing_object:` and `deliverable_id:` is
    indexed; a plan that cites a sizing but is not itself threaded yet
    contributes no edge (there is nothing yet for the sizing to inherit).

    Deliberately built off `sizing_object:` (the plan -> sizing forward
    pointer, read here as the reverse-edge SOURCE), never off a sizing's own
    `plan:` field — that forward edge is sparsely populated on the corpus
    this plan targets; the citing plan's `sizing_object:` is the ask.

    Returns `(index, ambiguous_sizing_refs)`. This module already has a
    fail-loud ambiguity contract ("Ambiguity (fail-loud, exit 2 in dry-run;
    blocked in write mode)", see this module's docstring, and `main()`'s
    `ERROR: Cannot --write while ambiguous groups exist.` guard) — a
    colliding `sizing_object:` (two distinct plans, each with its own
    `deliverable_id`, both citing the same sizing) must participate in that
    same contract rather than being silently resolved by iteration order.
    A collision is therefore NEVER written into `index` under either
    candidate id — `index.get(key)` misses for it exactly like an
    uncited sizing, so it falls through `get_workstream_key`'s existing
    miss path to `_UNKEYED_SIZING_GROUP_KEY` (AC3: never invent, never
    silently misassign) — and it is recorded in `ambiguous_sizing_refs`
    (key -> sorted colliding citing-plan slugs) so the caller can surface
    it as a human-review-required ambiguity rather than dropping it.
    """
    entries: Dict[str, List[Tuple[str, str]]] = {}
    plans_dir = os.path.join(coordinator_root, "docs", "plans")
    for f in _list_md_files(plans_dir, recursive=False):
        if is_sidecar_plan(f):
            continue
        sizing_ref = extract_fm_field(f, "sizing_object")
        if not sizing_ref:
            continue
        deliverable_id = extract_fm_field(f, "deliverable_id")
        if not deliverable_id:
            continue
        key = sizing_ref.strip().replace(os.sep, "/")
        entries.setdefault(key, []).append((_derive_plan_slug(f), deliverable_id))

    index: Dict[str, str] = {}
    ambiguous_sizing_refs: Dict[str, List[str]] = {}
    for key, plan_entries in entries.items():
        distinct_ids = {pid for _slug, pid in plan_entries}
        if len(distinct_ids) > 1:
            ambiguous_sizing_refs[key] = sorted(slug for slug, _pid in plan_entries)
            continue
        index[key] = plan_entries[0][1]
    return index, ambiguous_sizing_refs


def get_workstream_key(
    path: str,
    artifact_class: str,
    coordinator_root: Optional[str] = None,
    plan_sizing_index: Optional[Dict[str, str]] = None,
) -> str:
    """Group key: `chain:` for completions, `workstream:` frontmatter field
    for plan/handoff/spinoff/roadmap, and — for a sizing (AC2) — the citing
    plan's `deliverable_id`, resolved via `plan_sizing_index` (see
    `build_plan_sizing_index`). A sizing with no citing plan in the index, or
    a call site that has not built one (`coordinator_root`/`plan_sizing_index`
    absent), resolves to `_UNKEYED_SIZING_GROUP_KEY` (AC3) — never a
    synthetic key.
    """
    if artifact_class == "completion":
        return extract_fm_field(path, "chain")
    if artifact_class == "sizing":
        if coordinator_root and plan_sizing_index:
            rel_key = _rel(path, coordinator_root)
            deliverable_id = plan_sizing_index.get(rel_key)
            if deliverable_id:
                return deliverable_id
        return _UNKEYED_SIZING_GROUP_KEY
    return extract_fm_field(path, "workstream")


# ---------------------------------------------------------------------------
# unit 3 — corpus enumeration + grouping + ambiguity detection
# (mirrors oracle L237-399)
# ---------------------------------------------------------------------------


def _list_md_files(directory: str, recursive: bool) -> List[str]:
    if not os.path.isdir(directory):
        return []
    out: List[str] = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(directory):
            for name in filenames:
                if name.endswith(".md"):
                    out.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if name.endswith(".md") and os.path.isfile(full):
                out.append(full)
    out.sort()
    return out


def _list_yaml_files(directory: str, recursive: bool) -> List[str]:
    if not os.path.isdir(directory):
        return []
    out: List[str] = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(directory):
            for name in filenames:
                if name.endswith(".yaml"):
                    out.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if name.endswith(".yaml") and os.path.isfile(full):
                out.append(full)
    out.sort()
    return out


def _find_named(directory: str, basename: str) -> List[str]:
    if not os.path.isdir(directory):
        return []
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(directory):
        if basename in filenames:
            out.append(os.path.join(dirpath, basename))
    out.sort()
    return out


def enumerate_corpus(coordinator_root: str) -> List[str]:
    """Full board-relevant corpus, deterministically (sorted) ordered per section."""
    corpus: List[str] = []

    corpus.extend(_list_md_files(os.path.join(coordinator_root, "state", "handoffs"), recursive=False))
    corpus.extend(_list_md_files(os.path.join(coordinator_root, "archive", "handoffs"), recursive=True))

    plans_dir = os.path.join(coordinator_root, "docs", "plans")
    for f in _list_md_files(plans_dir, recursive=False):
        if not is_sidecar_plan(f):
            corpus.append(f)

    corpus.extend(_list_md_files(os.path.join(coordinator_root, "archive", "completed"), recursive=True))
    # archive/specs/** is `fleet.archive_plans` relocating whole plan
    # families — including their sidecar review artifacts
    # (.prior-art-check.md / .plan-coverage-check.md / etc). Those sidecars
    # are excluded here exactly as `is_sidecar_plan` already excludes them
    # from `docs/plans/*.md` above — a sidecar is not itself a plan-shaped
    # artifact warranting its own deliverable_id/plan_id.
    for f in _list_md_files(os.path.join(coordinator_root, "archive", "specs"), recursive=True):
        if not is_sidecar_plan(f):
            corpus.append(f)
    corpus.extend(_find_named(os.path.join(coordinator_root, "state", "roadmap"), "OVERVIEW.md"))

    roadmap_doc = os.path.join(coordinator_root, "docs", "ROADMAP.md")
    if os.path.isfile(roadmap_doc):
        corpus.append(roadmap_doc)

    corpus.extend(_list_yaml_files(os.path.join(coordinator_root, "state", "sizings"), recursive=False))

    return corpus


@dataclass
class _GroupingResult:
    total_scanned: int = 0
    already_threaded: List[Tuple[str, str]] = field(default_factory=list)  # (path, id)
    group_files: Dict[str, List[str]] = field(default_factory=dict)  # ws_key -> [paths]
    group_plan_slugs: Dict[str, List[str]] = field(default_factory=dict)  # ws_key -> [slugs]


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _derive_plan_slug(path: str) -> str:
    slug = extract_fm_field(path, "slug")
    if slug:
        return slug
    base = os.path.basename(path)
    if base.endswith(".md"):
        base = base[: -len(".md")]
    return _DATE_PREFIX_RE.sub("", base)


def group_corpus(
    corpus: List[str],
    coordinator_root: Optional[str] = None,
    plan_sizing_index: Optional[Dict[str, str]] = None,
) -> _GroupingResult:
    """Group `corpus` by workstream key. `coordinator_root`/`plan_sizing_index`
    are optional so existing single-arg callers keep working — a sizing
    resolved without them always lands UNKEYED (AC3), never a wrong key.
    """
    result = _GroupingResult(total_scanned=len(corpus))

    for f in corpus:
        artifact_class = classify_artifact(f)

        existing_id = extract_deliverable_id(f, artifact_class)
        if existing_id:
            result.already_threaded.append((f, existing_id))
            continue

        ws_key = get_workstream_key(
            f, artifact_class, coordinator_root, plan_sizing_index
        ) or _UNKNOWN_GROUP_KEY
        result.group_files.setdefault(ws_key, []).append(f)

        # Review: coordinator:code-reviewer — was `artifact_class == "plan"`
        # only, so an `archived-spec` file (grouped by the same `workstream:`
        # key as a `plan`) never contributed a slug here, leaving two
        # distinct archived specs (or an archived spec + a plan with a
        # different real slug) sharing a `workstream:` key silently
        # collapsed onto one `deliverable_id` with no ambiguity warning.
        # `_PLAN_ID_CLASSES` (unit 4b) is already the class-agnostic
        # "plan-shaped" predicate this needs — reused here so a future third
        # plan-shaped class cannot reintroduce the same blindness by editing
        # only one of the two sites.
        if artifact_class in _PLAN_ID_CLASSES:
            plan_slug = _derive_plan_slug(f)
            slugs = result.group_plan_slugs.setdefault(ws_key, [])
            if plan_slug not in slugs:
                slugs.append(plan_slug)

    return result


def detect_ambiguous_groups(result: _GroupingResult) -> List[str]:
    """Workstream keys with 2+ distinct plan slugs (excluding the UNKNOWN group)."""
    ambiguous = []
    for ws_key, slugs in result.group_plan_slugs.items():
        if ws_key == _UNKNOWN_GROUP_KEY:
            continue
        if len(slugs) > 1:
            ambiguous.append(ws_key)
    return ambiguous


@dataclass
class _Metrics:
    known_group_count: int = 0
    unknown_count: int = 0
    unkeyed_sizing_count: int = 0
    mutable_to_thread: int = 0
    mutable_unknown: int = 0
    immutable_derive: int = 0


def compute_metrics(result: _GroupingResult) -> _Metrics:
    metrics = _Metrics()
    for ws_key, files in result.group_files.items():
        is_unknown = ws_key == _UNKNOWN_GROUP_KEY
        is_unkeyed_sizing = ws_key == _UNKEYED_SIZING_GROUP_KEY
        if is_unknown:
            metrics.unknown_count = len(files)
        elif is_unkeyed_sizing:
            # AC9: named and queryable, distinct from both the UNKNOWN group
            # (other kinds' missing-workstream case) and `ambiguous`.
            metrics.unkeyed_sizing_count = len(files)
        else:
            metrics.known_group_count += 1
        for gf in files:
            if is_immutable_path(gf):
                metrics.immutable_derive += 1
            elif is_unknown or is_unkeyed_sizing:
                metrics.mutable_unknown += 1
            else:
                metrics.mutable_to_thread += 1
    return metrics


# ---------------------------------------------------------------------------
# unit 4 — proposed-id preview, write-pass stamping, report emission
# (mirrors oracle L400-690)
# ---------------------------------------------------------------------------


def _rel(path: str, coordinator_root: str) -> str:
    """Repo-relative wire form of `path` — ALWAYS forward-slash (rel_id).

    Bug fixed 2026-07-21: this previously stripped `coordinator_root` via a
    raw `str.startswith`/slice against a `/`-suffixed prefix, which never
    matched on Windows (`coordinator_root` and `path` both carry `os.sep` ==
    `\\` there), silently falling through to the ABSOLUTE path in every
    report line. `rel_id` normalizes correctly on every platform and is the
    single sanctioned repo-relative-id constructor (see wire_paths.py).
    """
    try:
        return rel_id(Path(path), Path(coordinator_root))
    except ValueError:
        return path


def _derive_sizing_slug(path: str) -> str:
    """Own-slug for a sizing (C2-leg3): a sizing has no `workstream:`/plan
    group to derive an id FROM when it is UNKEYED (no citing plan), so an
    unkeyed sizing mints a STANDALONE `deliverable_id` off its own slug
    instead — mirrors `_derive_plan_slug`'s own "slug field, else filename
    minus date prefix" fallback, since no sizing record in this corpus
    carries a `slug:` field today (checked empirically), only the
    date-prefixed filename.
    """
    doc = read_yaml_document(path)
    slug = doc.get("slug")
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    base = os.path.basename(path)
    if base.endswith(".yaml"):
        base = base[: -len(".yaml")]
    return _DATE_PREFIX_RE.sub("", base)


def _propose_unkeyed_sizing_id(path: str) -> str:
    """Dry-run-only preview string for a standalone unkeyed-sizing mint —
    parallels `_propose_id`'s hex-mint preview but keyed off the sizing's
    OWN slug (`_derive_sizing_slug`), never a workstream string."""
    slug = _derive_sizing_slug(path)
    ws_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", slug)[:40]
    return f"dlv-{ws_slug}-??????  [standalone, minted from own slug at --write time]"


def _propose_id(ws_key: str, gfiles: List[str]) -> str:
    """Dry-run-only preview string — never a real mint (no `mint()` call).

    A sizing-only group's `ws_key` already IS the citing plan's
    `deliverable_id` (AC2) — not a workstream string to slugify — so it is
    surfaced as-is rather than run through the hex-mint preview path below.
    """
    if gfiles and all(classify_artifact(gf) == "sizing" for gf in gfiles):
        return f"{ws_key}  [from citing plan's deliverable_id]"
    for gf in gfiles:
        gf_class = classify_artifact(gf)
        if gf_class in ("handoff", "handoff-archived"):
            if canonical_kind(extract_fm_field(gf, "kind")) == "roadmap-baton":
                stub = extract_fm_field(gf, "stub_id")
                if stub:
                    return f"dlv-{stub}  [from stub_id]"
    ws_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", ws_key)[:40]
    return f"dlv-{ws_slug}-??????  [hex minted at --write time]"


def _find_group_id(gfiles: List[str], ws_key: str, out: IO[str], err: IO[str]) -> str:
    """Mint (or carry) ONE deliverable_id for a group. Mirrors oracle L580-616."""
    for gf in gfiles:
        existing_id = extract_deliverable_id(gf, classify_artifact(gf))
        if existing_id:
            print(f"  [carry] Using existing id from corpus: {existing_id}", file=err)
            return existing_id

    stub_id_found = ""
    for gf in gfiles:
        if canonical_kind(extract_fm_field(gf, "kind")) == "roadmap-baton":
            stub = extract_fm_field(gf, "stub_id")
            if stub:
                stub_id_found = stub
                break

    if stub_id_found:
        group_id, _label = _mint(stub_id=stub_id_found)
    else:
        ws_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", ws_key)[:40]
        group_id, _label = _mint(slug=ws_slug)

    print(f"  [mint] {ws_key} -> {group_id}", file=err)
    return group_id


def _stamp_file(path: str, deliverable_id: str, field_name: str = "deliverable_id") -> bool:
    """Atomically inject `<field_name>: <value>` after the opening `---` fence.

    Faithful port of the oracle's awk injection: insert once, immediately
    after the first `---` line; drop any pre-existing `<field_name>:` line
    inside the first frontmatter block (idempotency guard — should not exist
    given the caller's already-threaded exclusion, but kept for parity).

    `field_name` defaults to `"deliverable_id"` (the oracle's only field);
    the `plan_id` leg (C2) reuses this same injection point with
    `field_name="plan_id"` rather than duplicating the fence-scan logic.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    prefix = f"{field_name}:"
    out_lines: List[str] = []
    fence_count = 0
    injected = False
    for line in lines:
        stripped = line.rstrip("\n").rstrip("\r")
        if _FM_FENCE_RE.match(stripped):
            fence_count += 1
            out_lines.append(line)
            if fence_count == 1 and not injected:
                out_lines.append(f"{field_name}: {deliverable_id}\n")
                injected = True
            continue
        if fence_count == 1 and stripped.startswith(prefix):
            continue
        out_lines.append(line)

    if not injected:
        # A plan-shaped document with NO `---` frontmatter fence at all
        # (e.g. a hand-authored narrative doc) has no anchor for the
        # oracle's fence-scan injection point — the loop above copies the
        # file through unchanged and `injected` never flips, which used to
        # make this function report success (unconditional `return True`
        # below) while silently writing back the SAME bytes, never actually
        # threading the field. A minimal fence is created at the top of the
        # file instead, so the mint this caller already committed to is
        # actually persisted rather than silently dropped.
        out_lines = ["---\n", f"{field_name}: {deliverable_id}\n", "---\n"] + out_lines
        injected = True

    orig_mode = None
    try:
        orig_mode = os.stat(path).st_mode
    except OSError:
        print(f"skip: _stamp_file: orig_mode = os.stat(path).st_mode failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    tmp_path = f"{path}.backfill.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(out_lines)
    if orig_mode is not None:
        try:
            os.chmod(tmp_path, orig_mode)
        except OSError:
            print(f"skip: _stamp_file: os.chmod(tmp_path, orig_mode) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    os.replace(tmp_path, path)

    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(path)
    return True


_DELIVERABLE_ID_PATTERN = re.compile(
    r"^dlv-(?!placeholder-replace-with)[0-9a-zA-Z][0-9a-zA-Z.-]*$"
)


def _validate_stamped_deliverable_id(parsed: dict) -> list:
    """Narrow, op-local validation gate — checks ONLY the shape of the
    single field this stamp writes (`deliverable_id`), never the whole
    sizing document against the full `sizing-object.schema.json`.

    Why this exists (same defect class as `handoff_backfill_claim_stamp`'s
    `_validate_backfilled_fields`, commit 0bb630418): full-document
    validation via `validate_frontmatter(parsed, <sizing schema path>)`
    gates a single-field stamp on the WHOLE post-mutation document already
    satisfying the current schema — including `required: [..., "premise"]`,
    a field added well after this schema's 1.0.0 baseline. A legacy sizing
    record that predates `premise` (or any other later-added required
    field) fails full-document validation on fields this stamp never
    touches, refusing exactly the pre-backfill record the stamp exists to
    thread `deliverable_id` onto. Verified against a constructed legacy
    record (missing `premise`) before this fix landed: full-document
    validation refused it with `{'field': 'premise', 'error': 'required
    field missing'}, even though `deliverable_id` itself was well-formed.

    Deliberately does NOT call `coordinator_core.frontmatter.schema_validate`
    — `schema_validate.py` is a hard external dependency (DoE imports
    `validate_frontmatter_obj` by path) and its leniency is contract;
    narrowing that shared path would be a global behaviour change under
    one caller's remit. This function is local to THIS op and checks
    nothing beyond the one field THIS stamp actually writes.

    Returns a (possibly empty) list of `{field, error, hint}` dicts, the
    same shape `validate_frontmatter` returns, so the existing
    `format_validation_errors`/`MutateAbort` call site needs no change.
    Empty -> valid.
    """
    errors: list = []
    deliverable_id = parsed.get("deliverable_id")
    if deliverable_id is not None and (
        not isinstance(deliverable_id, str) or not _DELIVERABLE_ID_PATTERN.match(deliverable_id)
    ):
        errors.append(
            {
                "field": "deliverable_id",
                "error": f"expected null or a string matching ^dlv-...$, got {deliverable_id!r}",
                "hint": 'deliverable_id must be null or start with "dlv-" (not "dlv-placeholder-replace-with")',
            }
        )
    return errors


def _stamp_yaml_document(path: str, deliverable_id: str, repo_root: str) -> bool:
    """Inject `deliverable_id: <id>` into a whole-document sizing YAML (C5b).

    A sizing record carries NO `---` frontmatter fence (see
    `read_yaml_document`'s docstring), so `_stamp_file`'s fence-anchored
    injection point does not exist on this shape. Inserted after any leading
    comment/blank lines via plain text-line surgery — never a full
    `yaml.safe_load` + re-dump round-trip, which would silently drop every
    comment in the file (this corpus's `estimate.tshirt`/`route`/`detents`
    inline comments are load-bearing documentation, not decoration). Drops
    any pre-existing `deliverable_id:` top-level line first (idempotency
    guard, same rationale as `_stamp_file`'s fence-block guard — should be
    unreachable given the caller's already-threaded exclusion, kept for
    parity/defense).

    Review: staff-eng — Finding 5(a-c) and the sibling insertion-point
    finding, all closed together:

    (a) Reads and writes now route through `locked_write.locked_rmw` rather
        than two independent `open()` calls — this closes hazard (c), the
        missing cross-process lock, on the same primitive
        `deliverable_cascade._advance_one_sizing` and
        `coordinator-doc-new::_write_sizing_reverse_edge` both take on the
        SAME corpus. `locked_rmw` itself reads via `Path.read_text(encoding=
        "utf-8")` (Python's universal-newline text mode) and writes the
        mutated text's own literal bytes with no `os.linesep` re-encoding —
        so a CRLF-authored sizing is homogenised to LF on any stamp (rather
        than the pre-existing hazard's OS-dependent CRLF<->LF flip-flop).
        Full CRLF ROUND-TRIP fidelity is bounded by `locked_rmw`'s own
        read/write primitive, which is outside this module's write surface;
        this stamp no longer introduces a WORSE, platform-dependent
        corruption on top of that shared bound.
    (b) `errors="replace"` on the read leg is gone — a decode failure now
        propagates as a genuine exception, which the caller wraps into a
        named skip (see `_stamp_deliverable_id`) instead of silently
        persisting U+FFFD replacement characters into a write-back path.
    (c) locked_rmw, above.
    (d) The stamp no longer assumes line index 1 is safe: it now inserts
        after every leading blank/`#`-comment line instead, and the
        post-mutation document is `yaml.safe_load`'d and schema-validated
        before the write is allowed to land — a record that would corrupt
        (block-scalar continuation caught mid-scope, or a shape the vendored
        `sizing-object.schema.json` rejects) aborts the stamp via
        `MutateAbort` rather than silently persisting a broken document.
    """
    import yaml  # noqa: PLC0415 — function-local, mirrors this module's other deferred imports
    from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw  # noqa: PLC0415

    def _mutate(old_text: str) -> str:
        lines = old_text.splitlines(keepends=True)
        out_lines: List[str] = [
            line for line in lines if not line.startswith("deliverable_id:")
        ]
        insert_at = 0
        for line in out_lines:
            stripped = line.strip()
            if stripped == "" or stripped.startswith("#"):
                insert_at += 1
                continue
            break
        # Always LF: `old_text` arrives via `locked_rmw`'s `Path.read_text(
        # encoding="utf-8")` read leg, which is Python universal-newline text
        # mode — any `\r\n` in the on-disk file is already normalized to `\n`
        # before this function ever sees it, so a CRLF-preserving branch here
        # is structurally unreachable. Full CRLF round-trip fidelity would
        # have to be fixed at `locked_write.py`'s read primitive, well
        # outside this module's write surface (see this function's own
        # docstring, point (a)).
        # Review: coordinator:code-reviewer — collapsed the dead
        # CRLF-detection conditional that used to read as if it preserved
        # CRLF while being unable to.
        eol = "\n"
        out_lines.insert(insert_at, f"deliverable_id: {deliverable_id}{eol}")
        new_text = "".join(out_lines)

        try:
            parsed = yaml.safe_load(new_text)
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(
                f"stamp: post-mutation YAML parse failed for {path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise MutateAbort(
                f"stamp: post-mutation document at {path} did not parse to a YAML mapping"
            )
        errors = _validate_stamped_deliverable_id(parsed)
        if errors:
            details = "; ".join(f"{e['field']}: {e['error']} ({e['hint']})" for e in errors)
            raise MutateAbort(
                f"stamp: post-mutation validation failed for {path}: {details}"
            )
        return new_text

    try:
        locked_rmw(Path(path), _mutate, repo_root=Path(repo_root))
    except FileNotFoundError:
        print(f"skip: _stamp_yaml_document: file not found: {path}", file=sys.stderr)
        return False
    except LockTimeout as exc:
        print(
            f"skip: _stamp_yaml_document: timed out waiting for file lock on {path}: {exc}",
            file=sys.stderr,
        )
        return False
    except MutateAbort as exc:
        print(
            f"skip: _stamp_yaml_document: {exc.args[0] if exc.args else 'mutation aborted'}",
            file=sys.stderr,
        )
        return False
    except UnicodeDecodeError as exc:
        print(f"skip: _stamp_yaml_document: undecodable file {path}: {exc}", file=sys.stderr)
        return False

    # DR-276: declared AFTER the write lands, never before — same rule as
    # `_stamp_file`'s own call site.
    declare_write(path)
    return True


def _stamp_deliverable_id(path: str, artifact_class: str, deliverable_id: str, repo_root: str) -> bool:
    """Dispatch to the correct stamping mechanism for `artifact_class` (C5b).

    Returns True when the write actually landed (and `declare_write` fired),
    False when a named skip fired instead (`LockTimeout`/`MutateAbort`/
    `FileNotFoundError`/`UnicodeDecodeError`) — the caller must not report a
    refused write as a stamp (see module docstring's write-failure-reporting
    fix).
    """
    if artifact_class == "sizing":
        return _stamp_yaml_document(path, deliverable_id, repo_root)
    return _stamp_file(path, deliverable_id)


# ---------------------------------------------------------------------------
# unit 4b — plan_id leg (C2, fourth leg): per-file identity, no grouping.
# `plan_id` is minted per-plan off that plan's OWN slug ("pln-<slug>-<6hex>",
# schema: plan.schema.json § plan_id), never shared across a workstream group
# the way `deliverable_id` is — so this leg needs none of unit 3's
# grouping/ambiguity machinery.
# ---------------------------------------------------------------------------


def _mint_plan_id(slug: str) -> str:
    """Mint a `pln-<slug>-<6hex>` plan_id.

    Deliberately NOT `mint_deliverable_id.mint()` — that function's slug path
    hardcodes the `dlv-` prefix (see its own module docstring); a `plan_id`
    is a distinct namespace (`pln-`) per `plan.schema.json`'s `plan_id`
    field. Same hash recipe as `mint()`'s slug path (slug + wall-clock
    seconds + pid + a random component -> sha1 -> first 6 hex chars) for
    uniqueness parity — not cryptographic, matching that function's own
    documented caveat.
    """
    import hashlib  # noqa: PLC0415 — function-local, mirrors this module's other deferred imports
    import random  # noqa: PLC0415
    import time  # noqa: PLC0415

    hash_input = f"{slug}|{int(time.time())}|{os.getpid()}|{random.randint(0, 32767)}"
    six_hex = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()[:6]
    return f"pln-{slug}-{six_hex}"


_PLAN_ID_CLASSES = ("plan", "archived-spec")

# `is_sidecar_plan`'s suffix list is a documented, deliberately
# non-exhaustive port of the bash oracle (see this module's docstring
# negative-spec) — a newer sidecar-naming convention invented after that
# port landed (e.g. `<reviewer-name>-review.md`, such as
# `.sonnet-review.md`) is silently NOT excluded by it, and that function is
# never touched to "catch up" (byte-parity is load-bearing for the other
# legs' corpus enumeration and grouping behaviour). The plan_id leg has no
# workstream/grouping fallback to accidentally shield such a sidecar the
# way the deliverable_id legs' "no workstream -> skip" path does, so it
# needs its own narrow, leg-local generalization of that naming family.
_REVIEW_SIDECAR_RE = re.compile(r"\.[A-Za-z0-9]+-review\.md$")


@dataclass
class _PlanIdLegResult:
    population_lacking: int = 0
    stamped: int = 0
    write_failed: int = 0


def run_plan_id_leg(
    corpus: List[str],
    coordinator_root: str,
    mode: str,
    out: IO[str],
    err: IO[str],
    only_kind: Optional[List[str]] = None,
) -> _PlanIdLegResult:
    """Mint-where-absent `plan_id` onto `docs/plans/`/`archive/specs/` records.

    Never-re-mint (carry rule D1), same as every other leg: a file already
    carrying a real `plan_id` is skipped before anything else runs. No
    grouping/ambiguity concept applies here — `plan_id` is per-file identity,
    not shared across a workstream group.

    `only_kind` is accepted ONLY to print the asymmetry loudly (Review:
    coordinator:code-reviewer) — this leg does NOT filter on it. A caller
    reasoning from `--only-kind`'s name alone could otherwise be surprised
    that a scoped run still mints/writes `plan_id` onto every unthreaded
    `docs/plans/*.md`/`archive/specs/**` record. See the module's `--only-kind`
    help text for the same disclosure.
    """
    result = _PlanIdLegResult()
    print("", file=out)
    print("-" * 60, file=out)
    print("PLAN_ID LEG (C2, fourth leg)", file=out)
    if only_kind:
        print(
            "  NOTE: this leg is unconditional and NOT scoped by --only-kind "
            f"({', '.join(only_kind)}) — it will still mint/write plan_id onto "
            "every unthreaded docs/plans/*.md and archive/specs/** record.",
            file=out,
        )
    print("-" * 60, file=out)

    for f in corpus:
        artifact_class = classify_artifact(f)
        if artifact_class not in _PLAN_ID_CLASSES:
            continue

        if is_sidecar_plan(f):
            continue

        if _REVIEW_SIDECAR_RE.search(os.path.basename(f)):
            continue

        if artifact_class == "plan" and not _DATE_PREFIX_RE.match(os.path.basename(f)):
            # Undated `docs/plans/` document (INDEX.md, README.md,
            # config-slash-*.md, ...) — not a real plan record, never mint.
            continue

        existing = extract_plan_id(f, artifact_class)
        if existing:
            continue

        result.population_lacking += 1
        rel = _rel(f, coordinator_root)

        if mode != "write":
            print(f"  [would-mint] {rel}  (lacks plan_id)", file=out)
            continue

        slug = _derive_plan_slug(f)
        plan_id = _mint_plan_id(slug)
        landed = _stamp_file(f, plan_id, field_name="plan_id")
        if landed:
            print(f"  [stamp] {rel}  -> {plan_id}", file=out)
            result.stamped += 1
        else:
            print(f"  [skip-write-failed] {rel}  (write refused — see stderr)", file=out)
            result.write_failed += 1

    print("", file=out)
    print("PLAN_ID LEG SUMMARY:", file=out)
    print(f"  Lacking plan_id:    {result.population_lacking}", file=out)
    if mode == "write":
        print(f"  Stamped:            {result.stamped}", file=out)
        if result.write_failed:
            print(f"  Write failures:     {result.write_failed}", file=out)
    return result


def _emit_report(
    coordinator_root: str,
    mode: str,
    result: _GroupingResult,
    ambiguous_keys: List[str],
    metrics: _Metrics,
    out: IO[str],
    ambiguous_sizing_refs: Optional[Dict[str, List[str]]] = None,
) -> None:
    ambiguous_sizing_refs = ambiguous_sizing_refs or {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("=" * 60, file=out)
    print("GROUPING REPORT — backfill-deliverable-spine", file=out)
    print(f"Generated: {now}", file=out)
    print(f"Root: {coordinator_root}", file=out)
    print(f"Mode: {mode}", file=out)
    print("=" * 60, file=out)
    print("", file=out)
    print("SUMMARY", file=out)
    print("-------", file=out)
    print(f"  Total artifacts scanned:      {result.total_scanned}", file=out)
    print(f"  Already-threaded (skip):      {len(result.already_threaded)}", file=out)
    print(f"  Known workstream groups:      {metrics.known_group_count}", file=out)
    print(f"  Unknown (no workstream):      {metrics.unknown_count}", file=out)
    print(f"  Unkeyed sizings (no plan):    {metrics.unkeyed_sizing_count}", file=out)
    print(f"  Ambiguous groups:             {len(ambiguous_keys)}", file=out)
    if ambiguous_sizing_refs:
        print(f"  Ambiguous sizing citations:   {len(ambiguous_sizing_refs)}", file=out)
    print(f"  Mutable to stamp (named):     {metrics.mutable_to_thread}", file=out)
    print(f"  Mutable (unknown, skip):      {metrics.mutable_unknown}", file=out)
    print(f"  Immutable (derive-at-emit):   {metrics.immutable_derive}", file=out)
    print("", file=out)

    if result.already_threaded:
        print("ALREADY-THREADED (carry — no action):", file=out)
        for epath, eid in result.already_threaded:
            print(f"  [{eid}]  {_rel(epath, coordinator_root)}", file=out)
        print("", file=out)

    named_keys = sorted(
        k for k in result.group_files
        if k not in (_UNKNOWN_GROUP_KEY, _UNKEYED_SIZING_GROUP_KEY)
    )
    if named_keys:
        print("-" * 60, file=out)
        print("NAMED GROUPS", file=out)
        print("-" * 60, file=out)

        for ws_key in named_keys:
            gfiles = result.group_files[ws_key]
            is_ambig = ws_key in ambiguous_keys
            proposed_id = _propose_id(ws_key, gfiles)

            print("", file=out)
            if is_ambig:
                print(f"GROUP [AMBIGUOUS]: {ws_key}", file=out)
                print(f"  Proposed id: {proposed_id}", file=out)
                print("  AMBIGUITY: 2+ distinct plan slugs in this workstream group.", file=out)
                print("             Two unrelated deliverables may share this workstream string.", file=out)
                slugs = " ".join(result.group_plan_slugs.get(ws_key, [])) or "<none>"
                print(f"             Plan slugs: {slugs}", file=out)
                print("  Artifacts:", file=out)
            else:
                print(f"GROUP [OK]: {ws_key}", file=out)
                print(f"  Proposed id: {proposed_id}", file=out)
                print("  Artifacts:", file=out)

            for gf in gfiles:
                gf_class = classify_artifact(gf)
                gf_rel = _rel(gf, coordinator_root)
                if is_immutable_path(gf):
                    print(f"    [{gf_class}] {gf_rel}  (immutable — derive-at-emit, no write)", file=out)
                else:
                    print(f"    [{gf_class}] {gf_rel}  (mutable — will stamp deliverable_id)", file=out)
        print("", file=out)

    if _UNKNOWN_GROUP_KEY in result.group_files:
        print("-" * 60, file=out)
        print("UNKNOWN GROUP (no workstream / chain field)", file=out)
        print("  These artifacts have no deterministic group key.", file=out)
        print("  They will carry null deliverable_id until the field is added.", file=out)
        print("  Artifacts:", file=out)
        for uf in result.group_files[_UNKNOWN_GROUP_KEY]:
            uf_class = classify_artifact(uf)
            uf_rel = _rel(uf, coordinator_root)
            if is_immutable_path(uf):
                print(f"    [{uf_class}] {uf_rel}  (immutable — derive-at-emit)", file=out)
            else:
                print(f"    [{uf_class}] {uf_rel}  (mutable — no workstream, skip)", file=out)
        print("", file=out)

    if _UNKEYED_SIZING_GROUP_KEY in result.group_files:
        print("-" * 60, file=out)
        print("UNKEYED SIZINGS (no citing plan)", file=out)
        print("  A sizing with no plan citing it via `sizing_object:` — or", file=out)
        print("  whose citing plan has no `deliverable_id` of its own yet —", file=out)
        print("  has nothing to inherit and therefore no shared group key.", file=out)
        print("  Distinct from UNKNOWN and from `ambiguous` — no synthetic", file=out)
        print("  GROUP key is invented for it. Each such sizing instead mints", file=out)
        print("  its OWN standalone deliverable_id at --write time, derived", file=out)
        print("  from the sizing's own slug (C2-leg3).", file=out)
        print("  Artifacts:", file=out)
        for sf in result.group_files[_UNKEYED_SIZING_GROUP_KEY]:
            sf_class = classify_artifact(sf)
            sf_rel = _rel(sf, coordinator_root)
            proposed = _propose_unkeyed_sizing_id(sf)
            print(f"    [{sf_class}] {sf_rel}  (unkeyed — would mint: {proposed})", file=out)
        print("", file=out)

    if ambiguous_keys:
        print("=" * 60, file=out)
        print("AMBIGUOUS GROUPS — HUMAN REVIEW REQUIRED", file=out)
        print("=" * 60, file=out)
        print("  The following workstream keys are ambiguous and cannot be", file=out)
        print("  assigned a deliverable_id without human disambiguation.", file=out)
        print("  Resolve before running --write:", file=out)
        print("", file=out)
        for ak in ambiguous_keys:
            slugs = " ".join(result.group_plan_slugs.get(ak, [])) or "<none>"
            print(f"  - '{ak}' (plan slugs: {slugs})", file=out)
        print("", file=out)

    if ambiguous_sizing_refs:
        print("=" * 60, file=out)
        print("AMBIGUOUS SIZING CITATIONS — HUMAN REVIEW REQUIRED", file=out)
        print("=" * 60, file=out)
        print("  The following sizings are cited via `sizing_object:` by 2+", file=out)
        print("  plans carrying DIFFERENT deliverable_ids. Neither plan's id", file=out)
        print("  is assigned — the sizing is UNKEYED until this is resolved.", file=out)
        print("", file=out)
        for sk in sorted(ambiguous_sizing_refs):
            slugs = " ".join(ambiguous_sizing_refs[sk]) or "<none>"
            print(f"  - '{sk}' (citing plan slugs: {slugs})", file=out)
        print("", file=out)

    print("=" * 60, file=out)
    total_ambiguous = len(ambiguous_keys) + len(ambiguous_sizing_refs)
    if total_ambiguous:
        print(f"STATUS: REVIEW REQUIRED ({total_ambiguous} ambiguous group(s))", file=out)
        print("  Fix ambiguities before running --write.", file=out)
    else:
        print("STATUS: CLEAN — no ambiguities detected", file=out)
        if mode == "dry-run":
            print("  Run with --write to stamp deliverable_ids onto mutable artifacts.", file=out)
    print("=" * 60, file=out)


_HELP_TEXT = """\
backfill-deliverable-spine — Backfill deliverable_id onto fleet artifact corpus.

Purpose: Group the board-relevant artifact corpus by workstream, emit a human-
         reviewable GROUPING REPORT, then (with --write) stamp deliverable_id
         onto mutable artifacts. Archived/immutable files are reported (derive-
         at-emit) but NEVER written. Already-threaded artifacts (carrying
         deliverable_id) are never re-minted.

Corpus: handoff / plan / spinoff / roadmap / completion / archived-spec / sizing entities.
  In scope:  state/handoffs/*.md   (active + spinoffs)
             archive/handoffs/**   (archived handoffs — immutable)
             docs/plans/*.md       (non-sidecar plans)
             archive/completed/**  (completion entries — immutable)
             archive/specs/**      (archived specs — MUTABLE, mint-where-absent)
             state/roadmap/**/OVERVIEW.md  (roadmaps)
             docs/ROADMAP.md
             state/sizings/*.yaml  (whole-document YAML, no frontmatter fence)
  Out of scope: lessons, backlogs, memos, decisions, wiki, tasks/, templates/

  A fourth leg additionally mints plan_id (pln-<slug>-<6hex>, per-file) onto
  docs/plans/*.md and archive/specs/** records lacking one, reported as a
  separate PLAN_ID LEG count — never summed with the deliverable_id counts.

Usage:
  backfill-deliverable-spine [--dry-run] [--root <coordinator-root>]
  backfill-deliverable-spine --write [--root <coordinator-root>]
  backfill-deliverable-spine --write [--only-kind <class>]...  [--root <coordinator-root>]
                              --only-kind restricts the WRITE PASS to the named
                              artifact class(es) (repeatable) — does not filter
                              enumeration, grouping, or the report. Accepted:
                              plan, handoff, handoff-archived, completion,
                              archived-spec, roadmap, sizing. Does not scope
                              the plan_id leg, which is unconditional.

Exit codes:
  0 — clean (no ambiguities; write applied or dry-run report emitted)
  1 — fatal error (bad arguments, corpus root unreadable)
  2 — ambiguous groups detected (human review required before --write)
  4 — a --write pass refused one or more writes (see Write failures in the
      WRITE COMPLETE block); codes 0-3 keep their prior meanings

Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § C5, AC7\
"""


def main(
    argv: List[str],
    default_coordinator_root: Optional[str] = None,
    out: IO[str] = sys.stdout,
    err: IO[str] = sys.stderr,
) -> int:
    """CLI entry point. See module docstring for the exit-code contract."""
    mode = "dry-run"
    root_override: Optional[str] = None
    only_kind: List[str] = []
    _VALID_KINDS = ("plan", "handoff", "handoff-archived", "completion", "archived-spec", "roadmap", "sizing")

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dry-run":
            mode = "dry-run"
            i += 1
        elif arg == "--write":
            mode = "write"
            i += 1
        elif arg == "--root":
            if i + 1 >= len(argv):
                print("ERROR: --root requires a value", file=err)
                return 1
            root_override = argv[i + 1]
            i += 2
        elif arg == "--only-kind":
            if i + 1 >= len(argv):
                print("ERROR: --only-kind requires a value", file=err)
                return 1
            kind = argv[i + 1]
            if kind not in _VALID_KINDS:
                print(
                    f"ERROR: --only-kind: unknown class {kind!r} — accepted: "
                    + ", ".join(_VALID_KINDS),
                    file=err,
                )
                return 1
            if kind not in only_kind:
                only_kind.append(kind)
            i += 2
        elif arg in ("-h", "--help"):
            print(_HELP_TEXT, file=out)
            return 0
        else:
            print(f"ERROR: Unknown argument: {arg}", file=err)
            print("Usage: backfill-deliverable-spine [--dry-run|--write] [--root <path>]", file=err)
            return 1

    if root_override:
        coordinator_root = root_override
    elif default_coordinator_root:
        coordinator_root = default_coordinator_root
    else:
        # No --root, and the caller (the coordinator/bin/backfill-deliverable-spine.py
        # trampoline) could not resolve a default either (e.g. DOE_ROOT /
        # repos.doe_claude unresolvable on this machine). Do NOT fall back to
        # this module's OWN directory (coordinator_core/ops/) — that is a
        # nonsense corpus root (no state/handoffs, docs/plans, etc. live there)
        # and would silently enumerate an empty corpus instead of failing loud.
        # This is a fatal error, matching this tool's documented exit-1
        # "corpus root unreadable" contract.
        print(
            "ERROR: no corpus root available — pass --root <path> explicitly, "
            "or resolve DOE_ROOT / repos.doe_claude in the machine-local "
            "registry so the trampoline can derive a default.",
            file=err,
        )
        return 1

    coordinator_root = os.path.abspath(coordinator_root)
    if not os.path.isdir(coordinator_root):
        print(f"ERROR: COORDINATOR_ROOT not found: {coordinator_root}", file=err)
        return 1

    corpus = enumerate_corpus(coordinator_root)
    plan_sizing_index, ambiguous_sizing_refs = build_plan_sizing_index(coordinator_root)
    result = group_corpus(corpus, coordinator_root, plan_sizing_index)
    ambiguous_keys = sorted(detect_ambiguous_groups(result))
    metrics = compute_metrics(result)

    mode_label = mode
    if only_kind:
        mode_label = f"{mode} (scope: {', '.join(only_kind)})"

    _emit_report(coordinator_root, mode_label, result, ambiguous_keys, metrics, out, ambiguous_sizing_refs)

    total_ambiguous = len(ambiguous_keys) + len(ambiguous_sizing_refs)
    write_failed = 0

    # Review: coordinator:code-reviewer — the deliverable_id leg's ambiguity
    # gate used to be an early `return 2` here, which also blocked
    # `run_plan_id_leg` below (unreachable code after a `return`) even though
    # the plan_id leg is per-file and has no grouping/ambiguity concept of
    # its own (see its docstring). One unrelated ambiguous deliverable_id
    # group therefore silently withheld plan_id stamping corpus-wide. The
    # deliverable_id write pass is now skipped in place (`elif`) rather than
    # returning, so the two legs' write passes are genuinely independent; the
    # `return 2` semantics for the deliverable_id leg are preserved below,
    # after both legs have run.
    if mode == "write" and total_ambiguous:
        print("", file=err)
        print("ERROR: Cannot --write while ambiguous groups exist.", file=err)
        print(f"       Resolve the {total_ambiguous} ambiguous group(s) above first.", file=err)
    elif mode == "write":
        print("", file=out)
        print("-" * 60, file=out)
        print("WRITE PASS — stamping deliverable_ids", file=out)
        print("-" * 60, file=out)

        stamped = 0
        stamped_unkeyed_sizing = 0
        skipped_immutable = 0
        skipped_unknown = 0
        skipped_out_of_scope = 0
        write_failed = 0

        for ws_key in sorted(
            k for k in result.group_files
            if k not in (_UNKNOWN_GROUP_KEY, _UNKEYED_SIZING_GROUP_KEY)
        ):
            gfiles = result.group_files[ws_key]

            # Review: coordinator:code-reviewer — a wholly-out-of-scope group
            # (every file either immutable or not in --only-kind) used to
            # `continue` here, skipping the per-file loop below entirely —
            # so its files got NO report line at all, not even
            # [skip-immutable]/[skip-out-of-scope], and the printed
            # Stamped + Skipped(*) total silently undercounted the full
            # corpus for a scoped run. Fixed by letting the group fall
            # through into the per-file loop unconditionally: every file
            # still gets its normal report line and counter increment, but
            # `group_id` derivation (and therefore minting) is skipped for a
            # pre-skipped group — safe because `pre_skipped` is exactly the
            # predicate for "no file in this group can reach the stamp
            # branch below", so `group_id` is never dereferenced.
            pre_skipped = only_kind and not any(
                not is_immutable_path(gf) and classify_artifact(gf) in only_kind
                for gf in gfiles
            )

            if pre_skipped:
                group_id = None
            elif gfiles and all(classify_artifact(gf) == "sizing" for gf in gfiles):
                # AC6: `ws_key` here already IS the citing plan's
                # deliverable_id (get_workstream_key's sizing arm) — never
                # re-derive it via _find_group_id's mint-or-carry search,
                # which is written to derive an id FROM group-file content
                # and would mint a BRAND NEW id off `ws_key` as if it were a
                # workstream slug.
                group_id = ws_key
            else:
                # Negative spec: a group mixing a sizing with a non-sizing
                # artifact (both sharing `ws_key`) falls through to
                # `_find_group_id` unconditionally — the `all(...)` bypass
                # above only fires when EVERY file in the group is a
                # sizing. This is only safe because `get_workstream_key`'s
                # sizing arm returns a real `deliverable_id` string
                # (`dlv-...`-shaped) while every other class's `ws_key`
                # comes from a hand-authored `workstream:`/`chain:` field —
                # the two key spaces would have to collide (a `workstream:`
                # literally equal to some plan's `deliverable_id`) for a
                # mixed group to exist at all. That collision is not
                # currently guarded against; if it is ever observed, this
                # bypass must be widened to key on `ws_key`'s SHAPE (e.g. a
                # `dlv-` prefix check), not just group-file homogeneity.
                group_id = _find_group_id(gfiles, ws_key, out, err)

            for gf in gfiles:
                if is_immutable_path(gf):
                    print(f"  [skip-immutable] {_rel(gf, coordinator_root)}  (derive-at-emit)", file=out)
                    skipped_immutable += 1
                    continue

                gf_class = classify_artifact(gf)
                existing_id = extract_deliverable_id(gf, gf_class)
                if existing_id:
                    print(
                        f"  [skip-threaded] {_rel(gf, coordinator_root)}  (already has id: {existing_id})",
                        file=out,
                    )
                    continue

                if only_kind and gf_class not in only_kind:
                    print(
                        f"  [skip-out-of-scope] {_rel(gf, coordinator_root)}  (class: {gf_class}, not in --only-kind scope)",
                        file=out,
                    )
                    skipped_out_of_scope += 1
                    continue

                if group_id is None:
                    # Structurally unreachable: `pre_skipped` is true only when
                    # NO file in the group is both mutable and in `only_kind`,
                    # and the three `continue`s above admit only such a file
                    # here. Asserted rather than assumed because the invariant
                    # spans two loops and is not visible to a type checker — a
                    # future reorder of the per-file skips would otherwise
                    # stamp a literal `None` onto a live record silently, which
                    # is the failure class this whole module exists to avoid.
                    raise RuntimeError(
                        f"invariant violated: no group_id derived for in-scope file {gf} "
                        f"(group {ws_key!r}, class {gf_class}) — refusing to stamp"
                    )
                landed = _stamp_deliverable_id(gf, gf_class, group_id, coordinator_root)
                if landed:
                    print(f"  [stamp] {_rel(gf, coordinator_root)}  -> {group_id}", file=out)
                    stamped += 1
                else:
                    print(
                        f"  [skip-write-failed] {_rel(gf, coordinator_root)}  (write refused — see stderr)",
                        file=out,
                    )
                    write_failed += 1

        stamped_standalone_plan = 0
        if _UNKNOWN_GROUP_KEY in result.group_files:
            # C2-leg4: an ungrouped `plan`/`archived-spec` (no `workstream:`
            # field, so no shared GROUP to derive an id FROM) is not left
            # un-mintable forever — mirrors the C2-leg3 unkeyed-sizing
            # standalone-mint shape exactly: `mint(slug=...)`'s existing slug
            # arm, keyed off the record's OWN slug (`_derive_plan_slug`),
            # never a synthetic workstream key. Every OTHER class in the
            # UNKNOWN group (handoff, roadmap, completion) keeps the prior
            # skip-unknown behaviour unchanged — only these two classes have
            # a per-file identity concept that makes a standalone mint
            # meaningful (see `run_plan_id_leg`'s own `plan_id`, minted the
            # same per-file way).
            for uf in result.group_files[_UNKNOWN_GROUP_KEY]:
                if is_immutable_path(uf):
                    continue

                uf_class = classify_artifact(uf)
                # Same leg-local generalizations `run_plan_id_leg` already
                # applies to this exact class pair — a review sidecar
                # (`.<reviewer>-review.md`, not covered by `is_sidecar_plan`'s
                # own negative-spec-frozen suffix list) or an undated
                # `docs/plans/` document (INDEX.md, README.md,
                # config-slash-*.md — never itself a plan record) must never
                # receive a standalone mint either; both fall through to the
                # ordinary skip-unknown path below, same as any other
                # never-groupable artifact.
                is_review_sidecar = _REVIEW_SIDECAR_RE.search(os.path.basename(uf)) is not None
                is_undated_plan_doc = (
                    uf_class == "plan" and not _DATE_PREFIX_RE.match(os.path.basename(uf))
                )
                if uf_class not in ("plan", "archived-spec") or is_review_sidecar or is_undated_plan_doc:
                    print(
                        f"  [skip-unknown] {_rel(uf, coordinator_root)}  (no workstream field — cannot assign id)",
                        file=out,
                    )
                    skipped_unknown += 1
                    continue

                existing_id = extract_deliverable_id(uf, uf_class)
                if existing_id:
                    print(
                        f"  [skip-threaded] {_rel(uf, coordinator_root)}  (already has id: {existing_id})",
                        file=out,
                    )
                    continue

                if only_kind and uf_class not in only_kind:
                    print(
                        f"  [skip-out-of-scope] {_rel(uf, coordinator_root)}  (class: {uf_class}, not in --only-kind scope)",
                        file=out,
                    )
                    skipped_out_of_scope += 1
                    continue

                slug = _derive_plan_slug(uf)
                standalone_id, _label = _mint(slug=slug)
                print(f"  [mint-standalone] {_rel(uf, coordinator_root)} -> {standalone_id}", file=out)
                landed = _stamp_deliverable_id(uf, uf_class, standalone_id, coordinator_root)
                if landed:
                    print(f"  [stamp] {_rel(uf, coordinator_root)}  -> {standalone_id}", file=out)
                    stamped_standalone_plan += 1
                else:
                    print(
                        f"  [skip-write-failed] {_rel(uf, coordinator_root)}  (write refused — see stderr)",
                        file=out,
                    )
                    write_failed += 1

        if _UNKEYED_SIZING_GROUP_KEY in result.group_files:
            # C2-leg3: an unkeyed sizing (no citing plan, so no shared
            # workstream GROUP to derive an id FROM) is not left un-mintable
            # — it mints its OWN standalone deliverable_id off its own slug
            # via `mint(slug=...)`'s existing slug arm (never a second
            # minting function). Each file mints independently — there is no
            # group here, unlike the named-groups loop above.
            for sf in result.group_files[_UNKEYED_SIZING_GROUP_KEY]:
                sf_class = classify_artifact(sf)  # always "sizing"

                existing_id = extract_deliverable_id(sf, sf_class)
                if existing_id:
                    print(
                        f"  [skip-threaded] {_rel(sf, coordinator_root)}  (already has id: {existing_id})",
                        file=out,
                    )
                    continue

                if only_kind and sf_class not in only_kind:
                    print(
                        f"  [skip-out-of-scope] {_rel(sf, coordinator_root)}  (class: {sf_class}, not in --only-kind scope)",
                        file=out,
                    )
                    skipped_out_of_scope += 1
                    continue

                slug = _derive_sizing_slug(sf)
                sizing_id, _label = _mint(slug=slug)
                print(f"  [mint-unkeyed] {_rel(sf, coordinator_root)} -> {sizing_id}", file=out)
                landed = _stamp_deliverable_id(sf, sf_class, sizing_id, coordinator_root)
                if landed:
                    print(f"  [stamp] {_rel(sf, coordinator_root)}  -> {sizing_id}", file=out)
                    stamped_unkeyed_sizing += 1
                else:
                    print(
                        f"  [skip-write-failed] {_rel(sf, coordinator_root)}  (write refused — see stderr)",
                        file=out,
                    )
                    write_failed += 1

        print("", file=out)
        print("WRITE COMPLETE:", file=out)
        print(f"  Stamped:                  {stamped + stamped_unkeyed_sizing + stamped_standalone_plan}", file=out)
        print(f"  Stamped (named groups):   {stamped}", file=out)
        print(f"  Stamped (unkeyed sizings): {stamped_unkeyed_sizing}", file=out)
        print(f"  Stamped (standalone plan/spec): {stamped_standalone_plan}", file=out)
        print(f"  Skipped (immutable):      {skipped_immutable}", file=out)
        print(f"  Skipped (unknown):        {skipped_unknown}", file=out)
        if only_kind:
            print(f"  Skipped (out-of-scope):   {skipped_out_of_scope}", file=out)
        if write_failed:
            print(f"  Write failures:           {write_failed}", file=out)

    # plan_id leg (C2, fourth leg) — per-file identity, no grouping/ambiguity
    # concept, reported as its OWN separate count (never summed with the
    # deliverable_id populations above — a sizing/plan/archived-spec are
    # different lifecycles per AC2).
    plan_id_leg = run_plan_id_leg(corpus, coordinator_root, mode, out, err, only_kind)

    if write_failed or plan_id_leg.write_failed:
        return 4

    if total_ambiguous:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
coordinator_core.ops.backfill_deliverable_spine — deliverable_id backfill for the
fleet artifact spine (handoff / plan / spinoff / roadmap / completion corpus).

Purpose: Group the board-relevant artifact corpus by workstream, emit a
human-reviewable GROUPING REPORT, then (with ``write=True``) stamp
``deliverable_id`` onto mutable artifacts. Archived/immutable files are
reported (derive-at-emit) but NEVER written. Already-threaded artifacts
(carrying ``deliverable_id``) are never re-minted.

Corpus: handoff / plan / spinoff / roadmap / completion entities ONLY.
    In scope:  state/handoffs/*.md   (active + spinoffs)
               archive/handoffs/**   (archived handoffs — immutable)
               docs/plans/*.md       (non-sidecar plans)
               archive/completed/**  (completion entries — immutable)
               state/roadmap/**/OVERVIEW.md  (roadmaps)
               docs/ROADMAP.md
    Out of scope: lessons, backlogs, memos, decisions, wiki, tasks/, templates/

Grouping key:
    Plans / handoffs / spinoffs: ``workstream:`` frontmatter field.
    Completions: ``chain:`` field (semantic equivalent in that artifact type).
    Roadmaps: ``workstream:`` field (if present).
    Null / absent key -> artifact placed in UNKNOWN group.

Ambiguity (fail-loud, exit 2 in dry-run; blocked in write mode):
    A group is ambiguous when it contains 2+ distinct plan slugs with NO
    shared predecessor link — indicating two unrelated deliverables collided
    on the same workstream string, or that a workstream string drifted.

Idempotency:
    Artifacts already carrying ``deliverable_id:`` are reported as
    "already-threaded" and skipped (carry rule D1 — never re-mint).

Port of: backfill-deliverable-spine.sh (example-doctrine-repo ca30f76c, 2026-07-17).
BIG_PORT wave, direct-import trampoline variant #1 — no
``register_op``, no IPC; the example-doctrine-repo-side polyglot trampoline imports and calls
``main()`` in-process, exactly like coordinator_core.hooks.auto_push /
coordinator_core.ops.handoff_gate_aging / coordinator_core.ops.backfill_initiative_fk.

Spec backlink: docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § C5, AC7
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § BIG_PORT Wave C

Public API:
    main(argv, default_coordinator_root=None) -> int
        CLI entry point. ``argv`` is the trampoline's own ``sys.argv[1:]``.
        ``default_coordinator_root`` is used when ``--root`` is not passed on
        the command line — the ``coordinator/bin/backfill-deliverable-spine``
        trampoline resolves example-doctrine-repo's own ``coordinator/`` directory (via
        the shared ``doe_root()`` registry helper, NOT its own ``__file__``
        location — this executable now lives in claude-klabauter while the
        corpus it walks, example-doctrine-repo's ``coordinator/{state,docs,archive}``,
        stayed in example-doctrine-repo) and passes it here. If BOTH ``--root`` and
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
    function can return; it is handled by the example-doctrine-repo trampoline itself, which
    uses a DEDICATED exit code 3 for that case per the porter addendum §3b
    fail-loud-gate-script rule, since this tool is a mutating fail-loud CLI,
    not a best-effort/never-block one, and codes 0/1/2 are all live business
    outcomes here. See the trampoline's own Usage comment block.)

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

_PROG = "backfill-deliverable-spine"
_UNKNOWN_GROUP_KEY = "__UNKNOWN__"

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
    """Artifact class: plan | handoff | handoff-archived | completion | roadmap | unknown."""
    norm = path.replace(os.sep, "/")
    if "/docs/plans/" in norm and norm.endswith(".md"):
        return "plan"
    if "/state/handoffs/" in norm and norm.endswith(".md"):
        return "handoff"
    if "/archive/handoffs/" in norm:
        return "handoff-archived"
    if "/archive/completed/" in norm:
        return "completion"
    if norm.endswith("/docs/ROADMAP.md"):
        return "roadmap"
    if "/state/roadmap/" in norm and norm.endswith("/OVERVIEW.md"):
        return "roadmap"
    return "unknown"


def get_workstream_key(path: str, artifact_class: str) -> str:
    """Group key: `chain:` for completions, `workstream:` for everything else."""
    if artifact_class == "completion":
        return extract_fm_field(path, "chain")
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
    corpus.extend(_find_named(os.path.join(coordinator_root, "state", "roadmap"), "OVERVIEW.md"))

    roadmap_doc = os.path.join(coordinator_root, "docs", "ROADMAP.md")
    if os.path.isfile(roadmap_doc):
        corpus.append(roadmap_doc)

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


def group_corpus(corpus: List[str]) -> _GroupingResult:
    result = _GroupingResult(total_scanned=len(corpus))

    for f in corpus:
        artifact_class = classify_artifact(f)

        existing_id = extract_fm_field(f, "deliverable_id")
        if existing_id:
            result.already_threaded.append((f, existing_id))
            continue

        ws_key = get_workstream_key(f, artifact_class) or _UNKNOWN_GROUP_KEY
        result.group_files.setdefault(ws_key, []).append(f)

        if artifact_class == "plan":
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
    mutable_to_thread: int = 0
    mutable_unknown: int = 0
    immutable_derive: int = 0


def compute_metrics(result: _GroupingResult) -> _Metrics:
    metrics = _Metrics()
    for ws_key, files in result.group_files.items():
        is_unknown = ws_key == _UNKNOWN_GROUP_KEY
        if is_unknown:
            metrics.unknown_count = len(files)
        else:
            metrics.known_group_count += 1
        for gf in files:
            if is_immutable_path(gf):
                metrics.immutable_derive += 1
            elif is_unknown:
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


def _propose_id(ws_key: str, gfiles: List[str]) -> str:
    """Dry-run-only preview string — never a real mint (no `mint()` call)."""
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
        existing_id = extract_fm_field(gf, "deliverable_id")
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


def _stamp_file(path: str, deliverable_id: str) -> None:
    """Atomically inject `deliverable_id: <id>` after the opening `---` fence.

    Faithful port of the oracle's awk injection: insert once, immediately
    after the first `---` line; drop any pre-existing `deliverable_id:` line
    inside the first frontmatter block (idempotency guard — should not exist
    given the caller's already-threaded exclusion, but kept for parity).
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    out_lines: List[str] = []
    fence_count = 0
    injected = False
    for line in lines:
        stripped = line.rstrip("\n").rstrip("\r")
        if _FM_FENCE_RE.match(stripped):
            fence_count += 1
            out_lines.append(line)
            if fence_count == 1 and not injected:
                out_lines.append(f"deliverable_id: {deliverable_id}\n")
                injected = True
            continue
        if fence_count == 1 and stripped.startswith("deliverable_id:"):
            continue
        out_lines.append(line)

    orig_mode = None
    try:
        orig_mode = os.stat(path).st_mode
    except OSError:
        print(f"skip: _stamp_file: orig_mode = os.stat(path).st_mode failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    tmp_path = f"{path}.backfill.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as fh:
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


def _emit_report(
    coordinator_root: str,
    mode: str,
    result: _GroupingResult,
    ambiguous_keys: List[str],
    metrics: _Metrics,
    out: IO[str],
) -> None:
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
    print(f"  Ambiguous groups:             {len(ambiguous_keys)}", file=out)
    print(f"  Mutable to stamp (named):     {metrics.mutable_to_thread}", file=out)
    print(f"  Mutable (unknown, skip):      {metrics.mutable_unknown}", file=out)
    print(f"  Immutable (derive-at-emit):   {metrics.immutable_derive}", file=out)
    print("", file=out)

    if result.already_threaded:
        print("ALREADY-THREADED (carry — no action):", file=out)
        for epath, eid in result.already_threaded:
            print(f"  [{eid}]  {_rel(epath, coordinator_root)}", file=out)
        print("", file=out)

    named_keys = sorted(k for k in result.group_files if k != _UNKNOWN_GROUP_KEY)
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

    print("=" * 60, file=out)
    if ambiguous_keys:
        print(f"STATUS: REVIEW REQUIRED ({len(ambiguous_keys)} ambiguous group(s))", file=out)
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

Corpus: handoff / plan / spinoff / roadmap / completion entities ONLY.
  In scope:  state/handoffs/*.md   (active + spinoffs)
             archive/handoffs/**   (archived handoffs — immutable)
             docs/plans/*.md       (non-sidecar plans)
             archive/completed/**  (completion entries — immutable)
             state/roadmap/**/OVERVIEW.md  (roadmaps)
             docs/ROADMAP.md
  Out of scope: lessons, backlogs, memos, decisions, wiki, tasks/, templates/

Usage:
  backfill-deliverable-spine [--dry-run] [--root <coordinator-root>]
  backfill-deliverable-spine --write [--root <coordinator-root>]

Exit codes:
  0 — clean (no ambiguities; write applied or dry-run report emitted)
  1 — fatal error (bad arguments, corpus root unreadable)
  2 — ambiguous groups detected (human review required before --write)

Spec backlink: docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § C5, AC7\
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
        # No --root, and the caller (the coordinator/bin/backfill-deliverable-spine
        # trampoline) could not resolve a default either (e.g. DOE_ROOT /
        # repos.example_doctrine_repo unresolvable on this machine). Do NOT fall back to
        # this module's OWN directory (coordinator_core/ops/) — that is a
        # nonsense corpus root (no state/handoffs, docs/plans, etc. live there)
        # and would silently enumerate an empty corpus instead of failing loud.
        # This is a fatal error, matching this tool's documented exit-1
        # "corpus root unreadable" contract.
        print(
            "ERROR: no corpus root available — pass --root <path> explicitly, "
            "or resolve DOE_ROOT / repos.example_doctrine_repo in the machine-local "
            "registry so the trampoline can derive a default.",
            file=err,
        )
        return 1

    coordinator_root = os.path.abspath(coordinator_root)
    if not os.path.isdir(coordinator_root):
        print(f"ERROR: COORDINATOR_ROOT not found: {coordinator_root}", file=err)
        return 1

    corpus = enumerate_corpus(coordinator_root)
    result = group_corpus(corpus)
    ambiguous_keys = sorted(detect_ambiguous_groups(result))
    metrics = compute_metrics(result)

    _emit_report(coordinator_root, mode, result, ambiguous_keys, metrics, out)

    if mode == "write":
        if ambiguous_keys:
            print("", file=err)
            print("ERROR: Cannot --write while ambiguous groups exist.", file=err)
            print(f"       Resolve the {len(ambiguous_keys)} ambiguous group(s) above first.", file=err)
            return 2

        print("", file=out)
        print("-" * 60, file=out)
        print("WRITE PASS — stamping deliverable_ids", file=out)
        print("-" * 60, file=out)

        stamped = 0
        skipped_immutable = 0
        skipped_unknown = 0

        for ws_key in sorted(k for k in result.group_files if k != _UNKNOWN_GROUP_KEY):
            gfiles = result.group_files[ws_key]
            group_id = _find_group_id(gfiles, ws_key, out, err)

            for gf in gfiles:
                if is_immutable_path(gf):
                    print(f"  [skip-immutable] {_rel(gf, coordinator_root)}  (derive-at-emit)", file=out)
                    skipped_immutable += 1
                    continue

                existing_id = extract_fm_field(gf, "deliverable_id")
                if existing_id:
                    print(
                        f"  [skip-threaded] {_rel(gf, coordinator_root)}  (already has id: {existing_id})",
                        file=out,
                    )
                    continue

                _stamp_file(gf, group_id)
                print(f"  [stamp] {_rel(gf, coordinator_root)}  -> {group_id}", file=out)
                stamped += 1

        if _UNKNOWN_GROUP_KEY in result.group_files:
            for uf in result.group_files[_UNKNOWN_GROUP_KEY]:
                if not is_immutable_path(uf):
                    print(
                        f"  [skip-unknown] {_rel(uf, coordinator_root)}  (no workstream field — cannot assign id)",
                        file=out,
                    )
                    skipped_unknown += 1

        print("", file=out)
        print("WRITE COMPLETE:", file=out)
        print(f"  Stamped:            {stamped}", file=out)
        print(f"  Skipped (immutable): {skipped_immutable}", file=out)
        print(f"  Skipped (unknown):   {skipped_unknown}", file=out)

    if ambiguous_keys:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

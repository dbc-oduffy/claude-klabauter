"""
coordinator_core.ops.plan_capture_persist — persist a captured harness
plan-mode body (Claude Code's built-in ``/plan``, surfaced via the
``ExitPlanMode`` tool) as a compliant coordinator plan artifact.

Purpose: the sibling doctrine repo's
``coordinator/hooks/scripts/plan-persistence-check.py``
(a ``PostToolUse(ExitPlanMode)`` hook) captures the approved plan body from
``tool_response.plan`` and writes it verbatim to
``docs/plans/<YYYY-MM-DD>-<slug>.md`` — raw markdown, **no frontmatter at
all**. That file is schema-invisible: it carries no ``plan_id``,
``deliverable_id``, or ``sizing_object``, so
``assert_plan_sizing_citation.py`` (frontmatter-only reader) and every other
frontmatter-driven gate cannot see it. This op is the compliant replacement
for that raw write: given the plan body text, it scaffolds a real artifact
via ``coordinator/bin/coordinator-doc-new.py --type plan`` (frontmatter,
``plan_id``, ``deliverable_id``, the ``sizing_object`` reverse FK are all
engine-written — this module never hand-authors a frontmatter key), carries
the body into the canonical section skeleton, and synthesizes a
``yaml plan-tasks`` block from any ``## Chunks`` section the body contains.

This module does NOT wire itself into the hook — that edit belongs to the
hook's own (sibling doctrine) repo, a different scope (see the invocation
contract memo this module ships alongside,
``cross-repo/outbox/<date>-doe-claude-em-plan-persistence-hook-should-call-
plan-capture-persist.md``). Until that wiring lands, the hook's existing raw
write is unchanged and this op is reachable only by direct invocation
(JSON-RPC ``plan.persist_capture``, in-repo) or via its CLI trampoline
(``coordinator/bin/plan-capture-persist.py``, cross-repo).

Slug/date derivation (``derive_hook_slug``/``local_day``) is a byte-parity
port of the hook's own ``_derive_slug``/``_local_day`` — same H1-first-60-
char algorithm, same local-timezone calendar day — so this op's own
collision pre-check (``predict_existing_capture_path``) recognizes exactly
the file the hook itself would have written for the SAME plan body, on a
retry or a not-yet-wired install. ``coordinator-doc-new``'s OWN default
output path uses a DIFFERENT slug algorithm (40-char truncation via
``_slug_from_title``, ported here as ``predict_doc_new_plan_path`` for the
collision-against-an-ALREADY-COMPLIANT-plan pre-check) — the two are
deliberately not unified; each predicts the exact filename its own producer
would use, and this op checks both before writing.

Fail-open is a CALLER responsibility, not this op's: this is an ordinary
fail-loud CLI/op (a genuine error returns a non-zero exit / an
``{"exit_code": 1, ...}`` reply, never swallowed). The invocation contract
memo instructs the calling hook to wrap this op in a short, bounded timeout
and treat ANY failure (non-zero exit, timeout, malformed stdout) as "fall
back to today's raw write" — worst case is the pre-existing status quo, a
captured-but-noncompliant plan, never plan LOSS. See that memo's own
"Fail-open contract" section for the exact wrapper shape.

Op name: ``plan.persist_capture`` (MUTATING — writes ``docs/plans/*.md`` and
possibly ``docs/README.md``... no: see Negative-spec. Classification mirrors
``handoff.author_fork``'s DR-208 additive-create bucket, not any existing
carve-out DR; this op is new enough that a dedicated DR entry is a follow-up,
named in the invocation-contract memo, not decided silently here).

Negative-spec:
    - Does NOT write ``docs/README.md`` — returns the corrected sibling row
      as a string in its result payload; a peer/human applies it. (Same
      split the earlier port prototype used, and for the same reason: the
      README's own malformed-row cleanup is explicitly a different scope.)
    - Does NOT delete or touch anything outside ``docs/plans/`` in the
      caller's worktree.
    - Does NOT hand-author plan frontmatter — every frontmatter field is
      written by ``coordinator-doc-new --type plan``, invoked as a
      subprocess.
    - Does NOT require a sizing object — a captured vanilla plan very
      plausibly never went through ``coordinator:sizing``; when none is
      supplied, this op scaffolds with ``--no-sizing-object`` (the sanctioned
      explicit-absence declaration), never a bare guess.
    - Does NOT overwrite a DIFFERENT existing plan at the same predicted
      filename — returns ``status: "collision"``, writes nothing.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

_PROG = "plan-capture-persist"

# Generator-provenance declaration (C2, generator_provenance.py's AST reader).
# persist_captured_plan() writes a new docs/plans/<date>-<slug>.md whose name
# is derived from the captured plan's own title each call (and may delete a
# differently-slugged duplicate in the same dir) -- a data-dependent target
# within docs/plans/, not a fixed artifact.
MUTATES = ["docs/plans/*.md"]

_TOP_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_CHUNK_HEADING_RE = re.compile(
    r"^###[ \t]+(?P<id>\S+)[ \t]*(?:[—:-][ \t]*(?P<title>.+?))?[ \t]*$",
    re.MULTILINE,
)
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+/[^`\s]+|[^`\s]+\.[A-Za-z0-9]+)`")
_DUPLICATE_TITLE_MIN_RATIO = 0.82

_CHANGE_KIND_KEYWORDS: List[Tuple[str, str]] = [
    ("wiki", "wiki-append"),
    ("doctrine", "doctrine-edit"),
    ("hook", "hook-edit"),
    ("skill", "skill-edit"),
    ("agent-prompt", "agent-prompt-edit"),
    ("config", "config-edit"),
    ("verification", "verification"),
    ("test", "test-edit"),
    ("doc", "doc-edit"),
]
_DEFAULT_CHANGE_KIND = "code-edit"
_SURFACE_UNKNOWN_PLACEHOLDER = "TBD — verify surface (ported from a captured plan)"


class PersistError(Exception):
    """Fail-loud condition; carries the message to print/return on failure."""


# ---------------------------------------------------------------------------
# Slug / date derivation — byte-parity ports of the two producers this op's
# collision pre-check must recognize (see module docstring).
# ---------------------------------------------------------------------------


def local_day() -> str:
    """Local calendar day, YYYY-MM-DD — matches the hook's ``_local_day``
    (bash oracle: ``date -I``, local timezone, NOT UTC)."""
    return date.today().isoformat()


def derive_hook_slug(plan_content: str) -> str:
    """Port of the hook's own ``_derive_slug`` (60-char truncation from the
    first ``# `` H1 line; a UTC HHMMSS fallback when no H1 is present) — see
    the sibling doctrine repo's
    ``coordinator/hooks/scripts/plan-persistence-check.py``.
    Byte-parity is load-bearing: this is what predicts the exact filename
    the hook itself would already have written for the SAME plan body.
    """
    h1_line = None
    for line in plan_content.splitlines():
        if line.startswith("# "):
            h1_line = line
            break
    if h1_line is not None:
        h1_text = h1_line[2:]
        h1_lower = h1_text.lower()
        h1_slug = re.sub(r"[^a-z0-9]+", "-", h1_lower)
        slug = h1_slug.strip("-")
        slug = slug[:60]
        slug = slug.rstrip("-")
        return slug
    return "plan-" + datetime.now(timezone.utc).strftime("%H%M%S")


def predict_hook_capture_path(repo_root: Path, plan_content: str) -> Path:
    """Predict the exact ``docs/plans/<date>-<slug>.md`` path the hook's OWN
    raw write would already have used for ``plan_content`` (or will use, if
    this op fails and the caller falls back to it) — the collision surface
    against the hook's own frontmatter-less raw dump."""
    slug = derive_hook_slug(plan_content)
    return repo_root / "docs" / "plans" / f"{local_day()}-{slug}.md"


def predict_doc_new_plan_path(repo_root: Path, title: str) -> Path:
    """Port of ``coordinator-doc-new``'s own ``_slug_from_title`` (40-char
    truncation, re-stripped after truncation) + its ``plan`` default output
    path (``docs/plans/<date>-<slug>.md``) — the collision surface against
    an ALREADY-COMPLIANT plan this op (or a prior manual scaffold) already
    produced for the same title. Deliberately a SEPARATE algorithm from
    ``derive_hook_slug`` (40 chars, not 60) — each predicts its own
    producer's actual output, not a unified guess.
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = slug[:40]
    slug = slug.strip("-")
    return repo_root / "docs" / "plans" / f"{local_day()}-{slug}.md"


# ---------------------------------------------------------------------------
# Plan-body parsing (unchanged pure-text logic — no filesystem dependency)
# ---------------------------------------------------------------------------


def extract_h1_title(text: str) -> Optional[str]:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("##"):
            return stripped[2:].strip()
    return None


def split_top_sections(text: str) -> List[Tuple[str, str]]:
    """Split ``text`` into ``[(heading_text, section_body), ...]`` at every
    top-level (``## ``) heading, mirroring markdown structure a captured
    plan body typically has. Text before the first ``## `` heading (the H1
    title line, any lead-in) is discarded — it carries nothing this op
    needs (the title is already captured structurally via ``extract_h1_
    title``)."""
    matches = list(_TOP_HEADING_RE.finditer(text))
    sections: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((heading, text[start:end].strip("\n")))
    return sections


def _demote_headings(body: str) -> str:
    def _demote_line(line: str) -> str:
        if re.match(r"^(#{1,5})([ \t].*)?$", line):
            return "#" + line
        return line

    return "\n".join(_demote_line(line) for line in body.splitlines())


def build_problem_body(sections: List[Tuple[str, str]]) -> str:
    """Concatenate every non-``Chunks`` top-level section (heading demoted
    one level) into one markdown blob for the scaffold's ``## Problem``
    section — lossless carry-forward; classifying prose into Acceptance
    Criteria / Anti-scope / Out of scope is left to EM review rather than
    invented here."""
    parts = []
    for heading, body in sections:
        if heading.strip().lower() == "chunks":
            continue
        parts.append(f"### {heading}\n\n{_demote_headings(body)}".rstrip())
    return "\n\n".join(parts).strip()


def _infer_change_kind(title: str, body: str) -> str:
    haystack = f"{title}\n{body}".lower()
    for keyword, kind in _CHANGE_KIND_KEYWORDS:
        if keyword in haystack:
            return kind
    return _DEFAULT_CHANGE_KIND


def _infer_surface(title: str, body: str) -> str:
    for haystack in (title, body):
        match = _BACKTICK_PATH_RE.search(haystack)
        if match:
            return match.group(1)
    return _SURFACE_UNKNOWN_PLACEHOLDER


def parse_chunk_rows(chunks_section_body: str) -> List[dict]:
    """Parse a ``## Chunks`` section body into plan-tasks rows — see
    ``_infer_change_kind``/``_infer_surface`` for the heuristic (and
    honestly weak) ``change_kind``/``surface`` guesses; every row is
    schema-validated (``validate_rows``) before it is written."""
    matches = list(_CHUNK_HEADING_RE.finditer(chunks_section_body))
    rows: List[dict] = []
    for i, m in enumerate(matches):
        chunk_id = m.group("id").rstrip(":—-")
        title = (m.group("title") or "").strip() or f"PLACEHOLDER — untitled chunk {chunk_id}"
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(chunks_section_body)
        body = chunks_section_body[start:end].strip("\n")
        row = {
            "id": chunk_id,
            "title": title,
            "change_kind": _infer_change_kind(title, body),
            "surface": _infer_surface(title, body),
            "queue_scope": "project",
            "disposition": "open",
        }
        if body.strip():
            row["body"] = body.strip() + "\n"
        rows.append(row)
    return rows


def validate_rows(rows: List[dict]) -> None:
    """Validate every synthesized row against the vendored plan-tasks
    schema — the same validator ``plan_tasks_mutate._validate_row`` uses."""
    from coordinator_core.frontmatter.schema_validate import (
        _PLAN_TASKS_SCHEMA_DICT,
        _apply_cross_field_rules,
        _validate_json_schema_node,
        format_validation_errors,
    )

    for row in rows:
        errors = _validate_json_schema_node(row, _PLAN_TASKS_SCHEMA_DICT, _PLAN_TASKS_SCHEMA_DICT)
        errors.extend(_apply_cross_field_rules(row, "plan-tasks"))
        if errors:
            raise PersistError(
                f"{_PROG}: synthesized task row {row.get('id')!r} failed schema "
                f"validation: {format_validation_errors(errors)}"
            )


def dump_rows(rows: List[dict]) -> str:
    return yaml.safe_dump(
        rows, sort_keys=False, default_flow_style=False, allow_unicode=True, width=4096,
    )


_PROBLEM_PLACEHOLDER_RE = re.compile(
    r"(## Problem\n\n)<!-- State the problem this plan solves\. -->\n"
)
_TASKS_FENCE_RE = re.compile(r"(```yaml plan-tasks\n).*?(\n```)", re.DOTALL)


def splice_problem_body(scaffold_text: str, problem_body: str) -> str:
    if not _PROBLEM_PLACEHOLDER_RE.search(scaffold_text):
        raise PersistError(
            f"{_PROG}: scaffold's '## Problem' placeholder not found in the "
            "expected shape — coordinator-doc-new's plan skeleton may have "
            "drifted; refusing to guess where the carried body belongs"
        )
    replacement = (
        f"## Problem\n\n{problem_body}\n" if problem_body else "## Problem\n\n<!-- carried body was empty -->\n"
    )
    return _PROBLEM_PLACEHOLDER_RE.sub(lambda _m: replacement, scaffold_text, count=1)


def splice_tasks(scaffold_text: str, rows: List[dict]) -> str:
    if not rows:
        return scaffold_text
    if not _TASKS_FENCE_RE.search(scaffold_text):
        raise PersistError(
            f"{_PROG}: scaffold's 'yaml plan-tasks' fence not found in the "
            "expected shape — coordinator-doc-new's plan skeleton may have "
            "drifted; refusing to guess where the task spine belongs"
        )
    body_yaml = dump_rows(rows).rstrip("\n")
    return _TASKS_FENCE_RE.sub(lambda m: m.group(1) + body_yaml + m.group(2), scaffold_text, count=1)


# ---------------------------------------------------------------------------
# Duplicate detection (fuzzy fallback — the exact-path predictors above are
# the primary signal; this covers drift, e.g. a differently-slugged prior
# raw dump for the same title).
# ---------------------------------------------------------------------------


def _title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_frontmatterless_duplicate(plans_dir: Path, title: str, exclude: Optional[Path] = None) -> Optional[Path]:
    if not plans_dir.is_dir():
        return None
    candidates: List[Path] = []
    for fpath in sorted(plans_dir.glob("*.md")):
        if exclude is not None and fpath.resolve() == exclude.resolve():
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if split_frontmatter(text) is not None:
            continue
        candidate_title = extract_h1_title(text)
        if not candidate_title:
            continue
        if _title_similarity(candidate_title, title) >= _DUPLICATE_TITLE_MIN_RATIO:
            candidates.append(fpath)
    if not candidates:
        return None
    if len(candidates) > 1:
        rendered = ", ".join(str(c) for c in candidates)
        raise PersistError(
            f"{_PROG}: {len(candidates)} frontmatter-less docs/plans/*.md files "
            f"match title {title!r} — refusing to guess which is the capture "
            f"duplicate: {rendered}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# coordinator-doc-new invocation
# ---------------------------------------------------------------------------


_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_DOC_NEW_PATH = _ENGINE_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def invoke_coordinator_doc_new(
    repo_root: Path,
    *,
    title: str,
    sizing_object_relpath: Optional[str],
    branch: Optional[str],
) -> Path:
    """Shell out to ``coordinator/bin/coordinator-doc-new.py --type plan``.
    Always lets that tool pick its OWN default ``--out`` (never passed here)
    so this op's collision pre-check (``predict_doc_new_plan_path``, the
    same algorithm) and the tool's actual write target can never disagree.

    ``repo_root`` here is the CALLER's plans repo (the write target — passed
    through as ``cwd=`` below and used to relativize the returned path, both
    unrelated to where the scaffolder script itself lives). The scaffolder
    is resolved against the ENGINE checkout root instead
    (``Path(__file__).resolve().parents[2]``, mirroring
    ``plan_tasks_mutate.py``'s ``_HARVEST_CLI_PATH`` self-location
    convention for the same "shell out to a sibling coordinator/bin/ engine
    CLI" problem — chosen over ``coordinator_core.claude_klabauter_root`` because this
    module already lives inside the engine checkout, so self-location is a
    free ``Path(__file__)`` derivation with no subprocess, versus
    ``claude_klabauter_root``'s Rung 2 fallback which can shell out to the
    ``machine-local`` CLI). Resolving the scaffolder against ``repo_root``
    instead was the bug this fix addresses: for any caller repo that is NOT
    the engine checkout (i.e. every real consumer repo), no
    ``coordinator/bin/coordinator-doc-new.py`` exists there and the op always
    raised ``PersistError``.
    """
    doc_new = _DOC_NEW_PATH
    if not doc_new.is_file():
        raise PersistError(f"{_PROG}: coordinator-doc-new not found at {doc_new} (engine root: {_ENGINE_ROOT})")

    argv = [sys.executable, str(doc_new), "--type", "plan", "--title", title]
    if sizing_object_relpath:
        argv += ["--sizing-object", sizing_object_relpath]
    else:
        argv += ["--no-sizing-object"]
    if branch:
        argv += ["--branch", branch]

    proc = subprocess.run(
        argv, cwd=str(repo_root), capture_output=True, text=True, timeout=60,
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        raise PersistError(
            f"{_PROG}: coordinator-doc-new failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not stdout_lines:
        raise PersistError(f"{_PROG}: coordinator-doc-new produced no output path")
    printed = stdout_lines[-1].strip()
    candidate = Path(printed)
    return candidate if candidate.is_absolute() else (repo_root / candidate)


def readme_row(plan_path: Path) -> str:
    basename = plan_path.name
    return f"- [`{basename}`](plans/{basename})"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def persist_captured_plan(
    plan_content: str,
    *,
    repo_root: Path,
    sizing_object: Optional[str] = None,
    title_override: Optional[str] = None,
    branch: Optional[str] = None,
) -> dict:
    """Core orchestration — shared by the JSON-RPC handler and the CLI.

    Returns a structured result dict (never raises for an EXPECTED failure
    mode — those come back as ``status: "error"``/``"collision"`` with a
    ``reason``; only a genuine programming-error-shaped exception escapes):
        {
          "status": "ok" | "idempotent" | "collision" | "error",
          "path": str | None,          # repo-relative, POSIX-normalized
          "readme_row": str | None,
          "reason": str | None,
        }
    """
    plan_content = (plan_content or "").rstrip("\n")
    if not plan_content:
        return {"status": "error", "path": None, "readme_row": None, "reason": "empty plan body"}

    title = (title_override or extract_h1_title(plan_content) or "").strip()
    if not title:
        return {"status": "error", "path": None, "readme_row": None, "reason": "no title (no '# ' H1 line) and none supplied"}

    # --- Collision pre-check 1: an already-compliant plan at the path
    # coordinator-doc-new would itself produce for this title. ---
    predicted_compliant = predict_doc_new_plan_path(repo_root, title)
    if predicted_compliant.is_file():
        try:
            existing_text = predicted_compliant.read_text(encoding="utf-8")
        except OSError as exc:
            return {"status": "error", "path": None, "readme_row": None, "reason": f"cannot read {predicted_compliant}: {exc}"}
        if split_frontmatter(existing_text) is not None:
            rel = predicted_compliant.relative_to(repo_root).as_posix()
            return {"status": "idempotent", "path": rel, "readme_row": readme_row(predicted_compliant), "reason": "already persisted (compliant plan exists at the predicted path)"}

    # --- Collision pre-check 2: the hook's OWN raw-dump path for this body. ---
    hook_path = predict_hook_capture_path(repo_root, plan_content)
    reconcile_raw_dump: Optional[Path] = None
    if hook_path.is_file():
        try:
            existing_text = hook_path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"status": "error", "path": None, "readme_row": None, "reason": f"cannot read {hook_path}: {exc}"}
        if split_frontmatter(existing_text) is None:
            if existing_text.rstrip("\n") == plan_content:
                reconcile_raw_dump = hook_path  # same capture — safe to supersede
            else:
                rel = hook_path.relative_to(repo_root).as_posix()
                return {"status": "collision", "path": None, "readme_row": None, "reason": f"a DIFFERENT plan already exists at {rel} — not overwritten"}

    # --- Body / task-spine parsing (pure text — no writes yet). ---
    sections = split_top_sections(plan_content)
    chunks_body = ""
    for heading, body in sections:
        if heading.strip().lower() == "chunks":
            chunks_body = body
            break
    rows = parse_chunk_rows(chunks_body) if chunks_body else []
    try:
        validate_rows(rows)
    except PersistError as exc:
        return {"status": "error", "path": None, "readme_row": None, "reason": str(exc)}
    problem_body = build_problem_body(sections)

    # --- Scaffold + splice. ---
    try:
        scaffold_path = invoke_coordinator_doc_new(
            repo_root, title=title, sizing_object_relpath=sizing_object, branch=branch,
        )
        scaffold_text = scaffold_path.read_text(encoding="utf-8")
        scaffold_text = splice_problem_body(scaffold_text, problem_body)
        scaffold_text = splice_tasks(scaffold_text, rows)
        scaffold_path.write_text(scaffold_text, encoding="utf-8", newline="\n")
    except PersistError as exc:
        return {"status": "error", "path": None, "readme_row": None, "reason": str(exc)}

    # --- Reconcile the hook's raw dump, if this write superseded it. ---
    if reconcile_raw_dump is not None and reconcile_raw_dump.resolve() != scaffold_path.resolve():
        reconcile_raw_dump.unlink(missing_ok=True)

    # --- Fuzzy fallback duplicate scan (drift case — different slug, same title). ---
    plans_dir = repo_root / "docs" / "plans"
    try:
        dup_path = find_frontmatterless_duplicate(plans_dir, title, exclude=scaffold_path)
    except PersistError as exc:
        # The scaffold already landed successfully — an ambiguous fuzzy-scan
        # result is reported, not treated as a failure of the write itself.
        rel = scaffold_path.relative_to(repo_root).as_posix()
        return {"status": "ok", "path": rel, "readme_row": readme_row(scaffold_path), "reason": f"scaffolded, but duplicate scan is ambiguous: {exc}"}
    if dup_path is not None:
        dup_path.unlink(missing_ok=True)

    rel = scaffold_path.relative_to(repo_root).as_posix()
    return {"status": "ok", "path": rel, "readme_row": readme_row(scaffold_path), "reason": None}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("plan.persist_capture")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``plan.persist_capture`` handler.

    MUTATING — writes at most one file under ``docs/plans/`` in the caller's
    worktree (plus removing a superseded raw-dump duplicate, if any — still
    scoped to ``docs/plans/``). No git commit from this handler.

    Params:
        plan_content   (str, required) — the captured plan body.
        sizing_object  (str | null) — repo-relative ``state/sizings/*.yaml``
                       path. Omitted/null scaffolds with ``--no-sizing-object``.
        title          (str | null) — override; else derived from the body's
                       first ``# `` H1 line.
        branch         (str | null) — forwarded to ``coordinator-doc-new``.

    Returns the ``persist_captured_plan`` result dict verbatim (see that
    function's docstring for the shape) on success, or ``{"exit_code": 1,
    "error": str}`` when ``repo_root`` is unresolved or ``plan_content`` is
    missing entirely (a param-shape error, not an expected persistence
    outcome — those come back as ``status`` values, per the negative-spec).
    """
    if repo_root is None:
        return {"exit_code": 1, "error": f"{_PROG}: no repo_root resolved"}
    plan_content = params.get("plan_content")
    if not isinstance(plan_content, str):
        return {"exit_code": 1, "error": f"{_PROG}: missing required param: plan_content"}

    from coordinator_core.ops.fleet._common import main_worktree_root

    worktree_root = main_worktree_root(repo_root)
    return persist_captured_plan(
        plan_content,
        repo_root=worktree_root,
        sizing_object=params.get("sizing_object") or None,
        title_override=params.get("title") or None,
        branch=params.get("branch") or None,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROG)
    parser.add_argument("--repo-root", dest="repo_root", required=True, metavar="PATH")
    parser.add_argument("--plan-file", dest="plan_file", default=None, metavar="PATH",
                         help="Read plan body from this file instead of stdin.")
    parser.add_argument("--sizing-object", dest="sizing_object", default=None, metavar="PATH")
    parser.add_argument("--title", default=None, metavar="TEXT")
    parser.add_argument("--branch", default=None, metavar="NAME")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint — reads the plan body from ``--plan-file`` or stdin,
    prints ONE JSON line (the ``persist_captured_plan`` result dict) to
    stdout, and returns 0 for every EXPECTED outcome (``ok``/``idempotent``/
    ``collision``) or 1 for ``error``. This is the entrypoint the
    ``coordinator/bin/plan-capture-persist.py`` trampoline calls via
    ``coordinator_core.cli_entry.run_op_main`` — see that module's own
    docstring for why the trampoline pattern exists (DR-276 write-claim
    recording), and the invocation-contract memo for how an external
    (cross-repo) caller is expected to invoke this trampoline and interpret
    its output.
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.plan_file:
        try:
            plan_content = Path(args.plan_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(json.dumps({"status": "error", "path": None, "readme_row": None, "reason": f"cannot read --plan-file: {exc}"}))
            return 1
    else:
        plan_content = sys.stdin.read()

    repo_root = Path(args.repo_root)
    result = persist_captured_plan(
        plan_content,
        repo_root=repo_root,
        sizing_object=args.sizing_object,
        title_override=args.title,
        branch=args.branch,
    )
    print(json.dumps(result))
    return 0 if result["status"] in ("ok", "idempotent", "collision") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

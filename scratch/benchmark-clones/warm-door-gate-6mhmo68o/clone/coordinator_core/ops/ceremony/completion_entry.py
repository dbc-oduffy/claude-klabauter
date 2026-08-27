"""
coordinator_core.ops.ceremony.completion_entry -- native completion-entry scaffold + fill.

Purpose: port op 0 (scaffold the workstream completion entry, OLD
``wsc_commit.py:_tail_scaffold_completion_entry`` -- a
``coordinator-doc-new --type completion`` subprocess with a stdout PATH-RETURN
contract) and op D2 (fill residues: prose / chain_terminal / authored_by, OLD
``wsc_commit.py:_fill_completion_entry_residues``) as in-process pure-Python
helpers -- no bash/node spawn (AC1/AC2).

Scaffold-creation decision (plan § C7): a thin native equivalent of
``coordinator-doc-new``'s ``_scaffold_completion`` emitter (DoE-claude
``coordinator/bin/coordinator-doc-new.py:1509``) -- ``completion_ops.py`` covers
only ``completion.reconcile_commits`` (fold commits into an EXISTING entry)
and ``plan.append_session``, neither of which covers entry CREATION, so
reuse was not available for op 0.

Both writes route through ``locked_write.locked_rmw`` (the OLD code wrote
BOTH ops unlocked via bare ``Path.read_text``/``Path.write_text`` -- fixed
here per plan § C7). The residue fill additionally routes frontmatter
mutation through ``frontmatter/primitives.py`` (``split_frontmatter`` /
``read_fm_field`` / ``replace_fm_field`` / ``rebuild``) instead of the OLD
code's hand-rolled substring anchors on the raw whole-file text -- narrowing
every structural mutation to the parsed frontmatter block rather than the
raw file text also removes the (unlikely but real) hazard of a body line
that happens to start with ``chain_terminal:`` being matched by accident.

The ``authored_by`` fill is a special case: the scaffold emits it as a
COMMENTED-OUT placeholder line (``# authored_by: PLACEHOLDER  # ...``), not a
live YAML key -- ``primitives.py``'s field helpers only operate on live
``key:`` lines, so this fill still does a narrow regex substitution, but
scoped to the parsed frontmatter TEXT (``split.fm_text``) rather than the
raw whole-file text the OLD code matched against.

Negative-spec:
    - Does NOT shell out to ``coordinator-doc-new`` or any other script --
      pure ``pathlib`` + ``frontmatter/primitives.py`` (AC1/AC2).
    - Does NOT compute ``commits:`` or ``loe:`` -- the scaffolder emits
      schema-valid placeholders (``commits: []``, ``loe: {...: null}``);
      those are filled by ``completion.reconcile_commits`` and the
      session-LOE aggregation op respectively, both out of scope here
      (mirrors the OLD code's own explicit D2 out-of-scope note).
    - Does NOT overwrite an already-scaffolded entry at the deterministic
      ``archive/completed/<YYYY-MM>/<YYYY-MM-DD>-<slug>.md`` path -- a
      pre-existing file at that path is a clean skip (idempotent re-invoke),
      never a clobber.
    - Does NOT weaken the block-scalar guard ``replace_fm_field`` already
      carries -- if ``chain_terminal:`` were ever authored as a block
      scalar (never happens from this module's own scaffold, but a
      defensive invariant), the fill aborts loudly via ``MutateAbort``
      rather than truncating it.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C7
Behavior reference (read for behavior, not structure, per the plan's Anti-scope):
    DoE:coordinator/bin/coordinator-doc-new.py:1509 (_scaffold_completion)
    claude-klabauter coordinator_core/ops/ceremony/wsc_commit.py:944 (_tail_scaffold_completion_entry)
    claude-klabauter coordinator_core/ops/ceremony/wsc_commit.py:1372 (_fill_completion_entry_residues)
"""

from __future__ import annotations
import sys

import re
from datetime import date
from pathlib import Path
from typing import Any, Dict

from coordinator_core.frontmatter.primitives import (
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw

TailResult = Dict[str, Any]

OP_COMPLETION_SCAFFOLD = "completion_entry:scaffold"
OP_COMPLETION_RESIDUE_FILL = "completion_entry:residue_fill"

# Mirrors coordinator-doc-new's --nature default (DoE-claude coordinator/bin/coordinator-doc-new.py:2438).
_COMPLETION_NATURE_DEFAULT = "infra"

# Body-paragraph sentinel comment the scaffold's own body emits verbatim -- matched as a
# single multi-line block so the fill is robust to the exact wrapped-comment wording.
_COMPLETION_BODY_SENTINEL_START = "<!-- ONE paragraph"

# Narrow-anchor value check for the chain_terminal fill: read_fm_field returns the
# trimmed rest-of-line (value + any trailing inline comment); this compares only the
# FIRST whitespace-delimited token so a reword of the trailing comment never breaks
# the check (mirrors the OLD code's field-name-prefix anchor discipline).
_CHAIN_TERMINAL_FALSE_TOKEN = "false"

# The scaffold emits authored_by as a COMMENTED-OUT placeholder line (not a live YAML
# key) -- primitives.py's key-line helpers do not apply, so this fill matches the full
# commented line directly, scoped to the parsed frontmatter text only.
_AUTHORED_BY_PLACEHOLDER_RE = re.compile(r"^# authored_by: PLACEHOLDER.*$", re.MULTILINE)


def _today() -> str:
    """Return today's date as YYYY-MM-DD (ISO-8601 date)."""
    return date.today().isoformat()


def _slug_from_title(title: str) -> str:
    """Sanitize a title into a filesystem-safe slug (<=40 chars).

    Port of coordinator-doc-new's ``_slug_from_title`` (DoE-claude
    ``coordinator/bin/coordinator-doc-new.py:473``).
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:40]


def _yaml_quote(value: str) -> str:
    """Double-quote a string for YAML frontmatter, escaping backslashes/quotes/newlines.

    Port of ``memo_compose._yaml_quote`` (DoE-claude ``coordinator/bin/lib/memo_compose.py:44``).
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _completion_entry_rel_path(title: str) -> Path:
    """Deterministic output path: archive/completed/<YYYY-MM>/<YYYY-MM-DD>-<slug>.md.

    Mirrors coordinator-doc-new's ``_default_output_path`` completion branch
    (DoE-claude ``coordinator/bin/coordinator-doc-new.py:2229``).
    """
    today = _today()
    slug = _slug_from_title(title)
    month = today[:7]
    return Path("archive") / "completed" / month / f"{today}-{slug}.md"


def _scaffold_text(title: str, nature: str, chain: str | None) -> str:
    """Generate validator-clean completion-entry frontmatter + canonical body placeholder.

    Byte-shape port of coordinator-doc-new's ``_scaffold_completion``
    (DoE-claude ``coordinator/bin/coordinator-doc-new.py:1509``), minus the
    optional ``completion_id`` field (not part of this chunk's scope --
    see the plan § C7 body).
    """
    today = _today()
    lines = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"created: {today}",
        f"nature: {nature}  # one of: roadmap|bugfix|tech-debt|infra",
        "nature_inferred: false  # true when auto-inferred by workstream-complete Step 3 Sonnet dispatch; false when explicitly set (--nature arg or COMPLETION_NATURE env var)",
    ]
    if chain:
        lines.append(f"chain: {_yaml_quote(chain)}")
    else:
        lines.append(
            "# chain: null  # fill with chain slug; omit this line entirely for standalone (non-chain) entries"
        )
    lines.extend(
        [
            "commits: []  # fill via completion.reconcile_commits (Step 2.6.8)",
            "status: pending-release",
            "chain_terminal: false  # set to true on /pickup → /workstream-complete (chain-terminal entry)",
            "# authored_by: PLACEHOLDER  # fill with $em_sid at runtime (type: string; forensic tracing only)",
            "loe:",
            "  agent_dispatches: null",
            "  opus_dispatches: null",
            "  em_tokens: null",
            "  tshirt: null",
            "---",
            "",
            "<!-- ONE paragraph (≤8 sentences): what shipped + why it matters.",
            "     Banned ## sections: '## Reviewer chain', '## Deviations from plan',",
            "     '## Acceptance criteria', '## Universal lessons captured'.",
            "     The completion entry is the queryable index, not the synthesis archive. -->",
            "",
        ]
    )
    return "\n".join(lines)


def scaffold_completion_entry(
    worktree_root: Path,
    title: str,
    *,
    nature: str = _COMPLETION_NATURE_DEFAULT,
    chain: str | None = None,
) -> TailResult:
    """Op 0: scaffold the workstream completion entry (native, no subprocess).

    PATH-RETURN CONTRACT (parity with the OLD subprocess seam): the on-disk
    path is returned as ``completion_entry_path`` (repo-root-relative,
    forward-slash) so a caller (C9) can include it in a staged-path union
    and downstream ops can gate on it. ``completion_entry_path`` is non-empty
    on both the success path AND the already-exists skip path (both leave a
    valid on-disk entry the caller can act on).

    Idempotent: a pre-existing file at the deterministic path is a clean
    skip, never a clobber -- including the race where a concurrent caller
    wins between this function's own existence pre-check and its
    lock-protected write (caught via ``MutateAbort`` inside the RMW).

    Returns {acted, skipped, failed, completion_entry_path: str}.
    """
    rel_path = _completion_entry_rel_path(title)
    abs_path = worktree_root / rel_path

    if abs_path.exists():
        return {
            "acted": [],
            "skipped": [f"{OP_COMPLETION_SCAFFOLD}:already-exists:{rel_path}"],
            "failed": [],
            "completion_entry_path": str(rel_path),
        }

    content = _scaffold_text(title, nature, chain)

    def _mutate(old_text: str) -> str:
        if old_text:
            # A concurrent creator won the race between the exists() pre-check
            # above and this lock-protected write -- never clobber their content.
            raise MutateAbort("completion entry created concurrently")
        return content

    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        locked_rmw(abs_path, _mutate, repo_root=worktree_root, missing_ok=True)
    except MutateAbort:
        print(f"skip: scaffold_completion_entry: abs_path.parent.mkdir(parents=True, exist_ok=True) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {
            "acted": [],
            "skipped": [f"{OP_COMPLETION_SCAFFOLD}:already-exists:{rel_path}"],
            "failed": [],
            "completion_entry_path": str(rel_path),
        }
    except (LockTimeout, OSError) as exc:
        print(f"skip: scaffold_completion_entry: abs_path.parent.mkdir(parents=True, exist_ok=True) failed: {exc}", file=sys.stderr)
        return {
            "acted": [],
            "skipped": [],
            "failed": [f"{OP_COMPLETION_SCAFFOLD}: {type(exc).__name__} -- {str(exc)[:160]}"],
            "completion_entry_path": "",
        }

    return {
        "acted": [f"{OP_COMPLETION_SCAFFOLD}:{rel_path}"],
        "skipped": [],
        "failed": [],
        "completion_entry_path": str(rel_path),
    }


def fill_completion_entry_residues(
    worktree_root: Path,
    completion_entry_path: str,
    prose: str,
    *,
    chain_terminal: bool,
    sid: str,
) -> Dict[str, bool]:
    """Op D2: fill the scaffolded completion entry with op-owned residues.

    Replaces the body's ``<!-- ONE paragraph ... -->`` sentinel with *prose*
    (when non-empty) and fills the frontmatter fields the op already has
    concrete values for at write time: ``chain_terminal`` (from STEP_2_7
    evidence, live upstream in C5/C9) and ``authored_by`` (the ceremony's
    sid, uncommenting the placeholder scaffold line).

    Out of scope (mirrors the OLD code's own explicit D2 scope note):
    ``commits`` and ``loe`` -- the scaffolder already emits these as
    schema-valid non-sentinel values; they are filled by
    ``completion.reconcile_commits`` and the session-LOE aggregation op
    respectively, neither of which this function invokes.

    Idempotent: a second call against an already-filled entry finds no
    sentinel, no ``chain_terminal: false`` token, and no commented
    placeholder line -- every field stays False and the write is skipped
    (``locked_rmw``'s byte-identical no-op short-circuit).

    Returns a dict with three bool fields -- {"prose": bool,
    "chain_terminal": bool, "authored_by": bool} -- one per fill, so the
    caller sees exactly which op-owned residues landed rather than a single
    aggregated flag that hides a partial-fill event. An empty
    ``completion_entry_path``, an unreadable/unwritable file, or a file with
    no parseable frontmatter block degrades to all-False (best-effort -- the
    caller does not abort on a fill failure; op 0's own acted/failed[]
    already reflects the scaffold's own success).
    """
    empty_result: Dict[str, bool] = {"prose": False, "chain_terminal": False, "authored_by": False}
    if not completion_entry_path:
        return dict(empty_result)

    entry_abs = (
        worktree_root / completion_entry_path
        if not Path(completion_entry_path).is_absolute()
        else Path(completion_entry_path)
    )

    filled = dict(empty_result)

    def _mutate(text: str) -> str:
        split = split_frontmatter(text)
        if split is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {completion_entry_path}")

        fm = split.fm_text
        body = split.body_with_leading_newline

        if prose:
            start = body.find(_COMPLETION_BODY_SENTINEL_START)
            if start != -1:
                end = body.find("-->", start)
                if end != -1:
                    end += len("-->")
                    body = body[:start] + prose + body[end:]
                    filled["prose"] = True

        if chain_terminal:
            current = read_fm_field(fm, "chain_terminal")
            if current is not None and current.split()[0] == _CHAIN_TERMINAL_FALSE_TOKEN:
                fm = replace_fm_field(fm, "chain_terminal", "true")
                filled["chain_terminal"] = True

        if sid:
            m = _AUTHORED_BY_PLACEHOLDER_RE.search(fm)
            if m:
                new_line = f"authored_by: {sid}  # forensic tracing only"
                fm = fm[: m.start()] + new_line + fm[m.end() :]
                filled["authored_by"] = True

        if not any(filled.values()):
            # No-op -- return text unchanged so locked_rmw skips the write entirely.
            return text

        new_split = split._replace(body_with_leading_newline=body)
        return rebuild(new_split, fm)

    try:
        locked_rmw(entry_abs, _mutate, repo_root=worktree_root)
    except (LockTimeout, MutateAbort, OSError):
        return dict(empty_result)

    return filled

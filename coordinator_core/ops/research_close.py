"""
coordinator_core.ops.research_close — JSON-RPC "research.close" op.

Purpose: closes a research run by tier. Scouts first concatenates the run's ``digest-*.md``
files, then a ``## Grounding`` section holding ``grounding.md`` when it has text, into
``<scratch_dir>/digest.md`` and archives that as its only output (``probe-*.md`` and
``grounding.md`` are never archived separately). Every tier
copies the run's declared final outputs into ``docs/research/<YYYY-MM-DD>-<topic_slug>/``
and makes exactly one ``commit_paths`` call over the copied paths plus the trail record
``docs/research/trail/<run_id>.yaml`` (built by ``_research_trail``).

Params: ``scratch_dir`` (absolute), ``tier`` (scouts|corpus|deep), ``run_id``,
``topic_slug`` (both safe single path segments), ``value_class`` (optional, in
``rc.VALUE_CLASSES``, default ``tier``), ``sizing`` (optional repo-relative path named in the
record; it must exist on disk, else the close is refused before any copy or commit, and it
lands in the same commit as the trail record; the close removes its ``research`` block first
(``_research_sizing.drop_research_block``) so the sizing reads as a build sizing, and the reply
carries ``research_block_dropped``; a refused commit restores the pre-drop bytes), ``outputs`` (optional list of
scratch-relative file paths; default is every regular file directly in scratch_dir and
directly in each ``segment_dirs`` subdirectory), ``segment_dirs`` (optional safe single
path segments: a multi-pipeline run writes every segment after the first under
``<scratch_dir>/<pipeline>/``, archived at the same relative path), ``local_root``
(optional, absolute: a local-only run archives to ``<local_root>/<YYYY-MM-DD>-<topic_slug>/``
outside every git checkout and commits only its trail record, which carries no paths and no
content; the reply's ``claims_out`` is the ``claims-emit --out`` stem there), ``require_claims`` (optional list of non-empty scratch-relative paths, each of
which must hold a non-empty JSON array; otherwise the close is refused with
``deliverable: false`` before any copy or commit), ``halted_segments`` (optional list of safe
path segments naming pipelines whose leg halted: an entry that is also in ``segment_dirs``
has its ``require_claims`` entries dropped and may have no dir; an entry outside
``segment_dirs`` is only echoed; the reply carries the list, success and refusal alike), ``require_grounding`` (optional bool,
scouts only, default false: true refuses the close with ``deliverable: false`` before any write
when ``grounding.md`` is absent or whitespace-only). A scouts ``grounding.md`` that has text is
always enforced, with or without the param: an ungrounded or contradicted claim, an invented
contradiction ruling, a digest with no section, or no graded claim refuses the close
(``_research_grounding.grounding_problems``). When such a refusal finds a ``digest-*.md`` newer
than ``grounding.md``, the error says so and the reply carries ``next_action`` naming
``research.reground``. With a ``grounding.carry.json`` the enforced report is the merged one
(carried sections for digests unchanged since ``grounding.basis.json``), and with a basis the
newer-digest check compares content, not mtimes; an unreadable carry or basis file refuses the close.
Only a named segment is excused: a root-level ``require_claims`` entry has no segment prefix
and is never excused (the research route puts ``nlm-preflight`` ahead of notebooklm, so the
root never holds notebooklm's claims).
``require_outputs`` (optional list of non-empty scratch-relative paths, each a non-empty regular
file; otherwise the close is refused with ``deliverable: false`` before any copy or commit).
Unlike ``require_claims``, a halted segment never excuses an entry: the file is the run's
deliverable.

Before any copy, every output is scanned for secret-shaped values (a run can read a
third-party tree that commits credentials); a hit refuses the close naming file, line and
pattern, never the value. After the commit, untracked files under ``docs/research/`` written
since the run began and outside the destination are reported as ``strays``: a member that
wrote its output elsewhere left it uncommitted and unarchived.

Idempotency: a re-run whose destination dir already holds byte-equal copies of every
output and the identical trail record returns the sha of the commit that landed them (found
by walking HEAD for the close subject); when no such commit is found, missing files are
written and the commit is made. A destination file or trail record holding different bytes
is refused, never overwritten, except a trail record the new one extends (same keys, old paths a
subset of the new, count not smaller): a re-close after moving strays into scratch adds them
and commits the extended record; a re-close on a later day names a new destination dir, so
its record differs and is refused.

``group_runs`` (optional list of safe run ids, must include ``run_id``; per-group research fan-out):
with a ``sizing``, the research block is dropped after this run's trail is written and only when
every listed sibling's trail file is on disk; otherwise the sizing is left untouched and the reply
carries ``research_block: "kept (<n> of <m> groups closed)"``. A sibling counts closed by its trail
file, not its landed commit. Absent ``group_runs`` the drop precedes the trail write.

Negative-spec:
  - Never calls ``ceremony.scoped_git_commit`` or ``run_commit_pipeline``; one
    ``commit_paths`` call or none.
  - Zero spawns on the clean path; one (``hash_worktree_blobs_via_spawn``) only on a
    CRLF-pinned path.
  - A missing output or a missing/escaping ``sizing`` is a structured refusal before any copy
    or commit; the sizing, minus its research block, rides the same single ``commit_paths`` call.
  - A refused close leaves the sizing byte-identical.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
from functools import partial
from pathlib import Path
from typing import Optional

from coordinator_core.git import commit_walk
from coordinator_core.git.commit import (
    CommitRefused,
    FilterUnsupported,
    NothingToCommit,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_index import IndexParseError, parse_index_stat
from coordinator_core.git.git_state import head_sha
from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
from coordinator_core.ipc import register_op
from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops import _research_grounding
from coordinator_core.ops import _research_trail
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops._research_sizing import ResearchSizingRefused, drop_research_block
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.repo_relative_text import relativize_text

_RESEARCH_DIR = Path("docs") / "research"
_WALK_LIMIT = 200

#: Secret-shaped values, by provider format or by an assignment to a secret-named key whose
#: value mixes letters and digits. Names only: a doc that records a key's NAME passes.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}")),
    (
        "secret-named assignment",
        re.compile(
            r"(?i)\b[\w.-]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
            r"[\w.-]*[\"']?\s*[:=]\s*[\"']?(?=[A-Za-z0-9/+_-]*\d)(?=[A-Za-z0-9/+_-]*[A-Za-z])"
            r"[A-Za-z0-9/+_-]{16,}"
        ),
    ),
)


def _err(error: str, **extra) -> dict:
    out = {"exit_code": 1, "committed": False, "error": error}
    out.update(extra)
    return out


def _blocking(what: str) -> str:
    return f"- **ungrounded** {what}"


def _json_file(path: Path, kind: type) -> tuple[Optional[object], Optional[str]]:
    """``(None, None)`` when absent; ``(value, None)`` when it parses to ``kind``; else ``(None, blocking line)``. Twin of research_reground._load_json."""
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, _blocking(f"{path.name} unreadable: {exc.__class__.__name__}")
    if not isinstance(value, kind):
        return None, _blocking(f"{path.name} is not a JSON {kind.__name__}")
    return value, None


def _digests_since_basis(scratch: Path, basis: dict) -> tuple[Optional[dict], Optional[str]]:
    """``changed_digests`` of the current ``digest-*.md`` against ``basis``; an unreadable digest is a blocking line."""
    try:
        current = {
            p.name: p.read_text(encoding="utf-8")
            for p in sorted(scratch.glob("digest-*.md")) if p.is_file()
        }
    except (OSError, UnicodeDecodeError) as exc:
        return None, _blocking(f"a digest is unreadable: {exc.__class__.__name__}")
    return _research_grounding.changed_digests(basis, current), None


def _grounding_text(scratch: Path) -> str:
    """The report the close enforces, or "" when absent or whitespace-only.

    With a ``grounding.carry.json`` it is the merged report (``effective_report``): carried
    sections for digests unchanged since ``grounding.basis.json``, fresh sections for the rest. An
    unreadable report, carry file or basis file yields a blocking ungrounded line so it refuses the
    close instead of passing unchecked."""
    path = scratch / _research_grounding.GROUNDING_REPORT
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        return _blocking(f"{_research_grounding.GROUNDING_REPORT} unreadable: {exc.__class__.__name__}")
    basis, bad = _json_file(scratch / _research_grounding.BASIS_FILE, dict)
    if bad:
        return bad
    carry, bad = _json_file(scratch / _research_grounding.CARRY_FILE, dict)
    if bad:
        return bad
    if carry is not None:
        changed: dict = {}
        if basis is not None:
            changed, bad = _digests_since_basis(scratch, basis)
            if bad:
                return bad
        text = _research_grounding.effective_report(text, carry, set(changed))
    return text if text.strip() else ""


def _grounding_predates_a_digest(scratch: Path) -> bool:
    """True when the grading is stale against a ``digest-*.md``.

    With a ``grounding.basis.json`` it is true iff some digest's content differs from the basis; a
    touched-but-unchanged digest is current. Without one it is today's mtime comparison. An
    unreadable or unstatable file is False."""
    basis, _ = _json_file(scratch / _research_grounding.BASIS_FILE, dict)
    if basis is not None:
        changed, bad = _digests_since_basis(scratch, basis)
        return not bad and bool(changed)
    try:
        grounded = (scratch / _research_grounding.GROUNDING_REPORT).stat().st_mtime
        digests = [p.stat().st_mtime for p in scratch.glob("digest-*.md") if p.is_file()]
    except OSError:
        return False
    return _research_grounding.grounding_predates_digest(grounded, digests)


def _build_digest(scratch: Path) -> Optional[str]:
    """Concatenate ``digest-*.md`` (then a ``## Grounding`` section when ``grounding.md`` holds
    text) into ``scratch/digest.md``; an error string when no digest exists."""
    digests = sorted(p for p in scratch.glob("digest-*.md") if p.is_file())
    if not digests:
        return f"no digest-*.md under {str(scratch)!r}"
    body = "\n\n".join(p.read_text(encoding="utf-8").rstrip("\n") for p in digests)
    grounding = _grounding_text(scratch)
    if grounding:
        body += "\n\n## Grounding\n\n" + grounding.rstrip("\n")
    body += "\n"
    (scratch / "digest.md").write_text(body, encoding="utf-8", newline="\n")
    return None


def _resolve_outputs(
    scratch: Path, outputs: Optional[list], segment_dirs: list[str],
    halted: frozenset = frozenset(),
) -> tuple[list[Path], Optional[str]]:
    if outputs is None:
        found = sorted(p for p in scratch.iterdir() if p.is_file())
        for name in segment_dirs:
            seg = scratch / name
            if not seg.is_dir():
                if name in halted:
                    continue
                return [], f"segment output dir missing: {name!r}"
            found.extend(sorted(p for p in seg.iterdir() if p.is_file()))
        if not found:
            return [], f"no output files directly under {str(scratch)!r}"
        return found, None
    resolved: list[Path] = []
    for rel in outputs:
        if not isinstance(rel, str) or not rel:
            return [], f"outputs entry is not a non-empty string: {rel!r}"
        p = contained_path(scratch / rel, [scratch])
        if p is None:
            return [], f"output escapes scratch_dir: {rel!r}"
        if not p.is_file():
            return [], f"missing scratch output: {rel!r}"
        resolved.append(p)
    if not resolved:
        return [], "outputs is empty"
    return resolved, None


def _secret_hits(sources: list[Path], scratch: Path) -> list[str]:
    hits: list[str] = []
    for src in sources:
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append(f"{src.relative_to(scratch).as_posix()}:{lineno}: {name}")
                    break
    return hits


def _archived_bytes(src: Path, roots: list[tuple[str, str]]) -> bytes:
    """Bytes to archive for `src`: UTF-8 text gets machine-absolute roots rewritten, anything else
    (and every local-only run, which has no roots) is copied byte-for-byte."""
    data = src.read_bytes()
    if not roots:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return relativize_text(text, roots).encode("utf-8")


def _extends(old_text: str, new_text: str) -> bool:
    """True when `new_text` is `old_text` plus outputs: every key but ``output_count`` and
    ``paths`` is equal, the old paths are a subset of the new, and the count did not shrink."""
    def split(text: str) -> tuple[list[str], list[str], Optional[int]]:
        head: list[str] = []
        paths: list[str] = []
        count: Optional[int] = None
        for line in text.splitlines():
            if line.startswith("  - "):
                paths.append(line[4:])
            elif line.startswith("output_count: "):
                try:
                    count = int(line[len("output_count: "):])
                except ValueError:
                    return [], [], None
            elif not line.startswith("paths:"):
                head.append(line)
        return head, paths, count

    old_head, old_paths, old_count = split(old_text)
    new_head, new_paths, new_count = split(new_text)
    if old_count is None or new_count is None or not old_head or old_head != new_head:
        return False
    return set(old_paths) <= set(new_paths) and old_count <= new_count


def _strays(worktree: Path, dest_dir: Path, since: float) -> list[str]:
    """Untracked files under docs/research/, outside `dest_dir`, modified at or after `since`."""
    root = worktree / _RESEARCH_DIR
    if not root.is_dir():
        return []
    try:
        tracked = set(parse_index_stat(worktree))
    except (IndexParseError, OSError):
        return []
    found: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        here = Path(dirpath)
        if here == dest_dir or dest_dir in here.parents:
            continue
        for name in files:
            path = here / name
            rel = path.relative_to(worktree).as_posix()
            try:
                if rel not in tracked and path.stat().st_mtime >= since:
                    found.append(rel)
            except OSError:
                continue
    return sorted(found)


def _landed_sha(worktree: Path, subject: str) -> Optional[str]:
    common = resolve_git_common_dir(worktree)
    head = head_sha(worktree)
    if head is None:
        return None
    for i, (sha, commit) in enumerate(commit_walk.walk(common, head)):
        if i >= _WALK_LIMIT:
            break
        if (commit.get("message") or "").split("\n", 1)[0].strip() == subject:
            return sha
    return None


def _close_committed(
    worktree: Path,
    scratch: Path,
    run_id: str,
    topic_slug: str,
    outputs: Optional[list],
    segment_dirs: list[str],
    meta: dict,
    local_root_raw: Optional[str] = None,
    halted_segments: Optional[list] = None,
) -> dict:
    """Archive the outputs and commit the trail record: the one commit site of this op.

    A repo run commits the copies and the record in one ``commit_paths`` call; a local-only
    run archives under its ``local_root`` and commits only the record. ``halted_segments``
    is echoed in the reply when given.
    """
    result = _close_archived(
        worktree, scratch, run_id, topic_slug, outputs, segment_dirs, meta, local_root_raw,
        frozenset(halted_segments or ()),
    )
    if halted_segments is not None:
        result["halted_segments"] = halted_segments
    return result


def _close_archived(
    worktree: Path,
    scratch: Path,
    run_id: str,
    topic_slug: str,
    outputs: Optional[list],
    segment_dirs: list[str],
    meta: dict,
    local_root_raw: Optional[str],
    halted: frozenset,
) -> dict:
    local = bool(local_root_raw)
    if local:
        from coordinator_core.ops import _research_local

        try:
            archive_root = _research_local.outside_checkouts(Path(local_root_raw), "local_root")
        except _research_local.LocalRootError as exc:
            return _err(str(exc))
    else:
        archive_root = worktree / _RESEARCH_DIR
    sources, problem = _resolve_outputs(scratch, outputs, segment_dirs, halted)
    if problem:
        return _err(problem)
    hits = _secret_hits(sources, scratch)
    if hits:
        return _err(
            "secret-shaped values in the run's outputs; redact them in scratch (record key names "
            "only) and re-run research.close",
            secret_hits=hits,
        )
    since = min(p.stat().st_mtime for p in sources)

    dest_dir = archive_root / f"{datetime.date.today().isoformat()}-{topic_slug}"
    if contained_path(dest_dir, [archive_root]) is None:
        return _err(f"destination escapes {str(archive_root)!r}: {str(dest_dir)!r}")

    pairs = [(src, dest_dir / src.relative_to(scratch)) for src in sources]
    roots = [] if local else [
        (str(scratch), dest_dir.relative_to(worktree).as_posix()),
        (str(worktree), ""),
    ]
    payload = {dst: _archived_bytes(src, roots) for src, dst in pairs}
    rel_paths = [] if local else [dst.relative_to(worktree).as_posix() for _, dst in pairs]
    try:
        trail_rel = _research_trail.trail_rel(run_id)
        trail_text = _research_trail.render(
            _research_trail.record(
                run_id=run_id,
                sizing=meta["sizing"],
                value_class=meta["value_class"],
                tier=meta["tier"],
                destination="local-only" if local else "repo",
                paths=rel_paths,
                output_count=len(sources),
            )
        )
    except ValueError as exc:
        return _err(f"trail record refused: {exc}")
    trail_path = worktree / trail_rel
    trail_bytes = trail_text.encode("utf-8")
    extend = False
    if trail_path.exists() and (not trail_path.is_file() or trail_path.read_bytes() != trail_bytes):
        extend = trail_path.is_file() and _extends(
            trail_path.read_text(encoding="utf-8", errors="replace"), trail_text
        )
        if not extend:
            return _err(
                f"trail record exists with different content, refusing to overwrite: {trail_rel}",
                dest=trail_rel,
            )
    for src, dst in pairs:
        if dst.exists() and (not dst.is_file() or dst.read_bytes() != payload[dst]):
            return _err(
                f"destination exists with different content, refusing to overwrite: {str(dst)!r}",
                dest=str(dest_dir),
            )
    subject = f"research({topic_slug}): close {run_id}"

    pre_drop: Optional[bytes] = None
    group_runs = meta.get("group_runs")
    block_note: Optional[str] = None
    if meta.get("sizing_rel") and not group_runs:
        try:
            pre_drop = drop_research_block(worktree, meta["sizing_rel"])
        except ResearchSizingRefused as exc:
            return _err(f"sizing research block not dropped: {exc}")

    # Trap: a retry after the extended trail was written but the commit refused finds equal bytes
    # and resolves the earlier close commit (same subject), leaving the extension uncommitted.
    # A drop that just changed the sizing is uncommitted work, so the run is not landed.
    landed = (
        pre_drop is None
        and trail_path.is_file() and not extend and all(dst.is_file() for _, dst in pairs)
    )
    sha = _landed_sha(worktree, subject) if landed else None
    if sha is None:
        try:
            for src, dst in pairs:
                if not dst.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(payload[dst])
            trail_path.parent.mkdir(parents=True, exist_ok=True)
            if extend or not trail_path.is_file():
                trail_path.write_bytes(trail_bytes)
        except OSError as exc:
            if pre_drop is not None:
                (worktree / meta["sizing_rel"]).write_bytes(pre_drop)
            return _err(f"cannot write the close outputs: {exc}")
        if meta.get("sizing_rel") and group_runs:
            siblings = [g for g in group_runs if g != run_id]
            closed = 1 + sum(1 for g in siblings if (worktree / _research_trail.trail_rel(g)).is_file())
            if closed == len(group_runs):
                try:
                    pre_drop = drop_research_block(worktree, meta["sizing_rel"])
                except ResearchSizingRefused as exc:
                    return _err(f"sizing research block not dropped: {exc}")
            else:
                block_note = f"kept ({closed} of {len(group_runs)} groups closed)"
        commit_rel = [*rel_paths, trail_rel]
        if meta.get("sizing_rel") and meta["sizing_rel"] not in commit_rel:
            commit_rel.append(meta["sizing_rel"])
        message = apply_missing_trailers(subject + "\n", worktree, commit_rel)
        try:
            outcome = commit_paths(
                worktree,
                commit_rel,
                message,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree),
            )
            sha = outcome.sha
        except IndexStaleAfterCommit as exc:
            sha = getattr(getattr(exc, "outcome", None), "sha", None)
        except NothingToCommit:
            sha = head_sha(worktree)
        except (CommitRefused, FilterUnsupported, IndexWriteError) as exc:
            if pre_drop is not None:
                (worktree / meta["sizing_rel"]).write_bytes(pre_drop)
            return _err(f"commit refused: {exc}", paths=commit_rel)
    dropped = pre_drop is not None
    note = {"research_block": block_note} if block_note is not None else {}
    if local:
        return {
            **note,
            "exit_code": 0,
            "committed": True,
            "research_block_dropped": dropped,
            "sha": sha or "",
            "local_only": True,
            "destination": dest_dir.as_posix(),
            "paths": [dst.as_posix() for _, dst in pairs],
            "trail": trail_rel,
            "claims_out": (dest_dir / topic_slug).as_posix(),
            "run_id": run_id,
            "deliverable": True,
        }
    return _with_strays(
        {
            **note,
            "exit_code": 0, "committed": True, "deliverable": True, "sha": sha or "",
            "paths": rel_paths, "trail": trail_rel, "research_block_dropped": dropped,
        },
        worktree, dest_dir, since,
    )


def _with_strays(result: dict, worktree: Path, dest_dir: Path, since: float) -> dict:
    strays = _strays(worktree, dest_dir, since)
    if strays:
        result["strays"] = strays
        result["warning"] = (
            f"{len(strays)} untracked docs/research file(s) written during this run sit outside "
            f"{dest_dir.relative_to(worktree).as_posix()}; move each into the run's scratch dir "
            "and re-close, or delete it"
        )
    return result


def _missing_claims(scratch: Path, require_claims) -> Optional[dict]:
    """A refusal reply when a ``require_claims`` path is not a non-empty JSON array, else None."""
    if require_claims is None:
        return None
    if not isinstance(require_claims, list) or not all(
        isinstance(r, str) and r for r in require_claims
    ):
        return _err("require_claims must be a list of non-empty scratch-relative paths")
    for rel in require_claims:
        p = contained_path(scratch / rel, [scratch])
        if p is None:
            return _err(f"require_claims entry escapes scratch_dir: {rel!r}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, list) or not data:
            return _err(f"run is not deliverable: {rel} holds no claims", deliverable=False)
    return None


def _missing_outputs(scratch: Path, require_outputs) -> Optional[dict]:
    """A refusal reply when a ``require_outputs`` path is not a non-empty regular file, else None."""
    if require_outputs is None:
        return None
    if not isinstance(require_outputs, list) or not all(
        isinstance(r, str) and r for r in require_outputs
    ):
        return _err("require_outputs must be a list of non-empty scratch-relative paths")
    for rel in require_outputs:
        p = contained_path(scratch / rel, [scratch])
        if p is None:
            return _err(f"require_outputs entry escapes scratch_dir: {rel!r}")
        try:
            landed = p.is_file() and p.stat().st_size > 0
        except OSError:
            landed = False
        if not landed:
            return _err(f"run is not deliverable: {rel} is missing or empty", deliverable=False)
    return None


def _close_sync(worktree: Path, params: dict) -> dict:
    tier = str(params.get("tier") or "")
    run_id = str(params.get("run_id") or "")
    topic_slug = str(params.get("topic_slug") or "")
    scratch_raw = str(params.get("scratch_dir") or "")
    outputs = params.get("outputs")
    value_class = str(params.get("value_class") or tier)
    sizing = params.get("sizing")

    if tier not in rc.TIERS:
        return _err(f"tier must be one of {list(rc.TIERS)}: {tier!r}")
    if value_class not in rc.VALUE_CLASSES:
        return _err(f"value_class must be one of {list(rc.VALUE_CLASSES)}: {value_class!r}")
    if sizing is not None and not isinstance(sizing, str):
        return _err(f"sizing must be a repo-relative path string: {sizing!r}")
    if not scratch_raw:
        return _err("missing required param: scratch_dir")
    scratch = Path(scratch_raw)
    if not scratch.is_dir():
        return _err(f"scratch_dir is not a directory: {scratch_raw!r}")
    for name, value in (("run_id", run_id), ("topic_slug", topic_slug)):
        if not value or not safe_id(value):
            return _err(f"{name} is not a safe path segment: {value!r}")
    if outputs is not None and not isinstance(outputs, list):
        return _err("outputs must be a list of scratch-relative paths")
    segment_dirs = params.get("segment_dirs") or []
    if not isinstance(segment_dirs, list) or not all(
        isinstance(d, str) and safe_id(d) for d in segment_dirs
    ):
        return _err(f"segment_dirs must be a list of safe path segments: {segment_dirs!r}")
    halted_segments = params.get("halted_segments")
    if halted_segments is not None and (
        not isinstance(halted_segments, list)
        or not all(isinstance(h, str) and safe_id(h) for h in halted_segments)
    ):
        return _err(f"halted_segments must be a list of safe path segments: {halted_segments!r}")
    require_claims = params.get("require_claims")
    if halted_segments and isinstance(require_claims, list):
        excused = set(halted_segments) & set(segment_dirs)
        require_claims = [
            r for r in require_claims
            if not (isinstance(r, str) and r.replace("\\", "/").split("/", 1)[0] in excused
                    and "/" in r.replace("\\", "/"))
        ]
    require_grounding = params.get("require_grounding", False)
    if not isinstance(require_grounding, bool):
        return _err(f"require_grounding must be a bool: {require_grounding!r}")
    if require_grounding and tier != "scouts":
        return _err(f"require_grounding applies to tier scouts only: {tier!r}")
    problem = _missing_claims(scratch, require_claims) or _missing_outputs(
        scratch, params.get("require_outputs")
    )
    if problem:
        if halted_segments is not None:
            problem["halted_segments"] = halted_segments
        return problem
    if tier == "scouts":
        report = _research_grounding.GROUNDING_REPORT
        grounding = _grounding_text(scratch)
        problem = None
        if require_grounding and not grounding:
            problem = _err(f"run is not deliverable: {report} is absent or empty", deliverable=False)
        elif grounding:
            found = _research_grounding.grounding_problems(
                grounding, [p.name for p in sorted(scratch.glob("digest-*.md")) if p.is_file()]
            )
            if found:
                message = f"run is not deliverable: {report}: " + "; ".join(found)
                extra: dict = {}
                if _grounding_predates_a_digest(scratch):
                    message += f"; {report} predates an edited digest; run {_research_grounding.REGROUND_OP}"
                    extra["next_action"] = {
                        "op": _research_grounding.REGROUND_OP,
                        "params": {"run_id": run_id, "scratch_dir": Path(os.path.abspath(scratch)).as_posix()},
                    }
                problem = _err(
                    message,
                    deliverable=False,
                    grounding=_research_grounding.grounding_counts(grounding),
                    **extra,
                )
        if problem:
            if halted_segments is not None:
                problem["halted_segments"] = halted_segments
            return problem
        problem = _build_digest(scratch)
        if problem:
            return _err(problem)
        outputs, segment_dirs = ["digest.md"], []
    group_runs = params.get("group_runs")
    if group_runs is not None:
        if (
            not isinstance(group_runs, list) or not group_runs
            or not all(isinstance(g, str) and g and safe_id(g) for g in group_runs)
        ):
            return _err(f"group_runs must be a non-empty list of safe run ids: {group_runs!r}")
        if run_id not in group_runs:
            return _err(f"group_runs must include this run_id {run_id!r}: {group_runs!r}")
    sizing_rel = None
    if sizing is not None:
        sized = contained_path(worktree / sizing, [worktree])
        if sized is None:
            return _err(f"trail record refused: sizing escapes the repo: {sizing!r}")
        if not sized.is_file():
            return _err(f"sizing is not on disk: {sizing!r}")
        sizing_rel = os.path.normpath(sizing).replace("\\", "/")
    meta = {
        "tier": tier, "value_class": value_class, "sizing": sizing, "sizing_rel": sizing_rel,
        "group_runs": group_runs,
    }
    local_root_raw = params.get("local_root")
    return _close_committed(
        worktree, scratch, run_id, topic_slug, outputs, segment_dirs, meta,
        str(local_root_raw) if local_root_raw else None, halted_segments,
    )


@register_op("research.close")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.close" handler.

    Params: see the module docstring; ``run_id`` and ``topic_slug`` are required for every tier.

    Returns (exit_code 0): ``{committed: true, deliverable: true, sha, paths, trail}``;
    local-only adds ``local_only``, ``destination``, ``claims_out``. Returns (exit_code 1):
    ``{committed: false, error}``; a ``require_claims``, ``require_outputs`` or ``require_grounding``
    refusal adds ``deliverable: false``; a grounding-verdict refusal also adds ``grounding`` (the
    five verdict counts); when a ``digest-*.md`` is newer than ``grounding.md`` that refusal also
    adds ``next_action: {op: "research.reground", params: {run_id, scratch_dir}}`` (absolute
    ``scratch_dir``).
    ``halted_segments`` is echoed in every reply when the param was given.
    """
    if repo_root is None:
        return _err("repo_root (engine common_dir) is required for research.close")
    worktree = main_worktree_root(repo_root)
    return await asyncio.to_thread(_close_sync, worktree, params)

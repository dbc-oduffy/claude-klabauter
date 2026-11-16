"""
coordinator_core.ops.fleet.memo_sweep_ref_only — memo.sweep_ref_only.

A memo is ref-only when a receiver holds its `refs/coordinator/inbox/` anchor but no
file of that name in either corpus root's inbox or archive. This op counts ref-only memos
across the registry's fleet repos (dry run, the default) and, on `dry_run: false`,
materialises them hook-free through `_receiver_materialize.materialize_memo`: OPEN memos
into the inbox byte-for-byte, TERMINAL memos (`status`/`lifecycle` in
`archive_actioned_memos._TERMINAL_MEMO_STATUSES`) straight into the archive with a
`filed by memo.sweep_ref_only <date>` note line.

Negative-spec:
  - Never overwrites: a filename already in either corpus root is not ref-only.
  - Never retires or moves an anchor (`memo.heal_inbox` does that).
  - No remote transport and no push; a registry repo absent on this box is `not_local`.
  - Dry run is in-process ref and blob reads and writes nothing.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import feature_refusal
from coordinator_core.ops.fleet._common import build_act_result, build_setup_error_result
from coordinator_core.ops.fleet._memo_anchor import anchor_names, present_filenames, resolve_anchor
from coordinator_core.ops.fleet._memo_resolver import RegistryReadError, read_registry_repos
from coordinator_core.ops.fleet._outbox_enum import build_dry_run_result
from coordinator_core.ops.fleet._receiver_materialize import (
    DEST_ARCHIVE,
    DEST_INBOX,
    STATUS_ALREADY_PRESENT,
    STATUS_MATERIALIZED,
    materialize_memo,
)
from coordinator_core.ops.fleet.archive_actioned_memos import _TERMINAL_MEMO_STATUSES

_MODE = "sweep_ref_only"
_KNOWN_PARAM_KEYS = frozenset({"dry_run", "repos", "limit"})

#: Files materialised per apply call across all repos. Each file is one hook-free commit plus
#: one `git update-index` spawn, so a call stays inside the 500ms brightline only while small.
DEFAULT_APPLY_LIMIT = 5

AGE_BUCKETS = ("<7d", "7-30d", "30-90d", ">90d", "unknown")

MUTATES = [
    "state/cross-repo/inbox/*.md",
    "cross-repo/inbox/*.md",
    "state/cross-repo/archive/*.md",
    "cross-repo/archive/*.md",
]

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


class _RefOnly(NamedTuple):
    filename: str
    terminal: bool
    bucket: str
    content: Optional[str]
    unreadable: Optional[str]


class _RepoScan(NamedTuple):
    refs: int
    inbox_files: int
    present: Dict[str, tuple]
    ref_only: List[_RefOnly]


def _age_bucket(filename: str, fm: dict, today: datetime.date) -> str:
    candidates = []
    match = _DATE_PREFIX_RE.match(filename)
    if match:
        candidates.append(match.group(1))
    created = fm.get("created")
    if created is not None:
        candidates.append(str(created)[:10])
    for text in candidates:
        try:
            days = (today - datetime.date.fromisoformat(text)).days
        except ValueError:
            continue
        if days < 0:
            continue
        if days < 7:
            return "<7d"
        if days < 30:
            return "7-30d"
        if days < 90:
            return "30-90d"
        return ">90d"
    return "unknown"


def _status_of(fm: dict) -> str:
    raw = fm.get("status") if fm.get("status") is not None else fm.get("lifecycle")
    return str(raw).strip().lower() if raw is not None else ""


def _count_files(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.rglob("*") if p.is_file())


def _scan_repo(path: Path, today: datetime.date, *, keep_content: bool) -> _RepoScan:
    from coordinator_core import memo_corpus

    common_dir = resolve_git_common_dir(path)
    anchors = anchor_names(common_dir)
    present = present_filenames(path)
    inbox_files = _count_files(Path(memo_corpus.memo_corpus_root(str(path))) / "inbox")

    by_name: Dict[str, List[str]] = {}
    for fname, _csha, bsha in sorted(anchors):
        if fname not in present:
            by_name.setdefault(fname, []).append(bsha)

    ref_only: List[_RefOnly] = []
    for fname in sorted(by_name):
        data = next((d for d in (resolve_anchor(common_dir, b) for b in by_name[fname]) if d is not None), None)
        if data is None:
            ref_only.append(_RefOnly(fname, False, "unknown", None, "anchor blob is unresolvable"))
            continue
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            ref_only.append(_RefOnly(fname, False, "unknown", None, f"anchor blob is not valid UTF-8: {exc}"))
            continue
        fm = parse_frontmatter(content).get("frontmatter") or {}
        ref_only.append(_RefOnly(
            fname, _status_of(fm) in _TERMINAL_MEMO_STATUSES, _age_bucket(fname, fm, today),
            content if keep_content else None, None,
        ))
    return _RepoScan(len(anchors), inbox_files, present, ref_only)


def _summary(scan: _RepoScan) -> dict:
    age: Dict[str, Dict[str, int]] = {
        "open": {b: 0 for b in AGE_BUCKETS}, "terminal": {b: 0 for b in AGE_BUCKETS},
    }
    counts = {"open": 0, "terminal": 0, "unreadable": 0}
    for memo in scan.ref_only:
        if memo.unreadable is not None:
            counts["unreadable"] += 1
            continue
        kind = "terminal" if memo.terminal else "open"
        counts[kind] += 1
        age[kind][memo.bucket] += 1
    return {"refs": scan.refs, "inbox_files": scan.inbox_files, "ref_only": counts, "age": age}


def _validate_params(params: dict):
    dry_run = params.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, True, "memo.sweep_ref_only: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )
    unknown = set(params) - _KNOWN_PARAM_KEYS
    if unknown:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.sweep_ref_only: unrecognized param(s) {sorted(unknown)} — known params: {sorted(_KNOWN_PARAM_KEYS)}.",
        )
    repos = params.get("repos")
    if repos is not None and not (isinstance(repos, list) and all(isinstance(r, str) and r for r in repos)):
        return build_setup_error_result(
            _MODE, dry_run, "memo.sweep_ref_only: repos must be a list of registry repo names.",
        )
    limit = params.get("limit", DEFAULT_APPLY_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return build_setup_error_result(_MODE, dry_run, "memo.sweep_ref_only: limit must be an int >= 1.")
    return dry_run, repos, limit


def _fleet(selected: Optional[List[str]]):
    """`(local, not_local)` from the registry: `local` maps repo name -> (path, aliases) with
    repos sharing one path collapsed onto the first name; `not_local` lists names whose path is
    not a git checkout on this box. Raises `RegistryReadError`; an unknown name in `selected`
    comes back as a `ValueError`."""
    registry = read_registry_repos()
    names = {key[len("repos."):]: path for key, path in registry.items()}
    if selected is not None:
        wanted = [s[len("repos."):] if s.startswith("repos.") else s for s in selected]
        missing = [w for w in wanted if w not in names]
        if missing:
            raise ValueError(f"not in the registry: {sorted(missing)}")
        names = {n: names[n] for n in wanted}
    local: Dict[str, tuple] = {}
    not_local: List[str] = []
    seen: Dict[str, str] = {}
    for name in sorted(names):
        path = Path(names[name])
        if not (path / ".git").exists():
            not_local.append(name)
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            local[seen[key]][1].append(name)
            continue
        seen[key] = name
        local[name] = (path, [])
    return local, not_local


def _commit_message(filename: str) -> str:
    return f"cross-repo memo materialized: {filename}\n\nMaterialized by memo.sweep_ref_only.\n"


def _filed_content(content: str, today: datetime.date) -> str:
    return content.rstrip("\n") + f"\n\nfiled by memo.sweep_ref_only {today.isoformat()}\n"


@register_op("memo.sweep_ref_only")
def _memo_sweep_ref_only(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.sweep_ref_only' MUTATING op handler (dry run by default).

    Params:
        dry_run (bool, optional, default true): count only, writing nothing; false materialises.
        repos   (list[str], optional): registry repo names; default every registry repo.
        limit   (int, optional, default 5): files materialised per apply call, across all repos.

    Dry run returns `repos: {name: {refs, inbox_files, ref_only: {open, terminal, unreadable},
    age: {open: {bucket: n}, terminal: {bucket: n}}}}` and `not_local: [names]`. Apply returns
    the same per-repo counts as they stood before the call, plus `materialized` (count this call)
    and `remaining` (ref-only memos still unfiled after it, failures included); the caller
    repeats while `materialized` is above zero.
    A repo absent on this box is `not_local` and skipped. Nothing is overwritten.
    """
    validated = _validate_params(params)
    if isinstance(validated, dict):
        return validated
    dry_run, selected, limit = validated

    refusal = feature_refusal("cross_repo_memos")
    if refusal:
        return build_setup_error_result(_MODE, dry_run, refusal)
    try:
        local, not_local = _fleet(selected)
    except RegistryReadError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.sweep_ref_only: registry could not be read: {exc.reason}")
    except ValueError as exc:
        return build_setup_error_result(_MODE, dry_run, f"memo.sweep_ref_only: {exc}")

    today = _today()
    scans = {name: _scan_repo(path, today, keep_content=not dry_run) for name, (path, _a) in local.items()}
    summaries = {name: {**_summary(scan), "aliases": local[name][1]} for name, scan in scans.items()}

    if dry_run:
        result = build_dry_run_result(_MODE, [])
        result["repos"] = summaries
        result["not_local"] = not_local
        return result

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    budget = limit
    for name in sorted(scans):
        scan = scans[name]
        path = local[name][0]
        present = set(scan.present)
        for memo in scan.ref_only:
            if budget <= 0:
                break
            budget -= 1
            row_id = f"{name}:{memo.filename}"
            if memo.unreadable is not None:
                failed.append({"id": row_id, "reason": f"{memo.unreadable} — cannot materialise {memo.filename!r}"})
                continue
            outcome = materialize_memo(
                path, memo.filename,
                _filed_content(memo.content, today) if memo.terminal else memo.content,
                dest=DEST_ARCHIVE if memo.terminal else DEST_INBOX,
                commit_message=_commit_message(memo.filename), known_present=present,
            )
            if outcome.status == STATUS_MATERIALIZED:
                present.add(memo.filename)
                acted.append({
                    "id": row_id, "action": "filed" if memo.terminal else "materialized",
                    "path": outcome.rel_path, "commit_sha": outcome.commit_sha,
                })
            elif outcome.status == STATUS_ALREADY_PRESENT:
                skipped.append({"id": row_id, "reason": STATUS_ALREADY_PRESENT})
            else:
                failed.append({"id": row_id, "reason": f"{outcome.status}: {outcome.reason}"})

    total = sum(len(s.ref_only) for s in scans.values())
    result = build_act_result(_MODE, acted, skipped, failed)
    result["repos"] = summaries
    result["not_local"] = not_local
    result["materialized"] = len(acted)
    result["remaining"] = total - len(acted) - len(skipped)
    return result

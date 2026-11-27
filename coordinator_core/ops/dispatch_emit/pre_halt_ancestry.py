"""Which ledger rows a verdict credits with a commit at or before the halt: a row of one of the
requested statuses (default met only; a post_commit row is never an already_landed close) whose
met_by names a sha token that resolves to `base` or an ancestor of it. At most two git spawns,
both through `run_git`, none when no row cites a token. land_run._unchanged_since_halt and
terminal_commit._unchanged_since_halt_failures each repeat the landing-sha rule (one distinct
cited sha, else base); change both together."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.ops.dispatch_emit.pre_halt_credit import sha_tokens

MAX_CITED: int = 8
#: Marks a synthesised resume ledger whose stage staged no row: the gate declares paths from the run's leg footprint.
EMPTY_STAGE_RESUME_FIELD: str = "empty_stage"
CITED_DETAIL_KEY: str = "cited"


def _row_tokens(
    items: Sequence[object], ids: Iterable[str], statuses: Sequence[str]
) -> dict[str, list[str]]:
    wanted = {str(i) for i in ids}
    out: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in statuses:
            continue
        rid = str(item.get("id"))
        if rid not in wanted or rid in out:
            continue
        met_by = item.get("met_by")
        text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by)
        toks = sha_tokens(text)
        if toks:
            out[rid] = toks
    return out


def cited_at_or_before(
    root: Path,
    base: str,
    items: Sequence[object],
    ids: Iterable[str],
    statuses: Sequence[str] = ("met",),
) -> dict[str, str]:
    """{row id: full 40-hex sha} for each id in `ids` whose verdict item (status in `statuses`,
    met by default) has a met_by naming, by pre_halt_credit.sha_tokens, a commit that is `base`
    or an ancestor of `base`. Never raises; {} on any git failure. Ancestry is the only check: the
    cited commit is not tested for touching the row's declared paths, so a verdict citing any
    unrelated ancestor credits the row."""
    try:
        rows = _row_tokens(items, ids, statuses)
        distinct: list[str] = []
        for toks in rows.values():
            for t in toks:
                if t not in distinct:
                    distinct.append(t)
        considered = distinct[:MAX_CITED]
        if not considered or not base:
            return {}
        cwd = str(root)
        # An unresolvable or ambiguous short token may fail the whole call; the failure returns {}.
        res = run_git(
            ["rev-list", "--no-walk=unsorted", "--ignore-missing",
             *(f"{t}^{{commit}}" for t in considered)],
            cwd=cwd,
        )
        if res.returncode != 0:
            return {}
        resolved = [s for s in res.stdout.split() if len(s) == 40]
        if not resolved:
            return {}
        outside = run_git(["rev-list", *resolved, f"^{base}"], cwd=cwd)
        if outside.returncode != 0:
            return {}
        not_reachable = set(outside.stdout.split())
        credited: dict[str, str] = {}
        for rid, toks in rows.items():
            for t in toks:
                if t not in considered:
                    continue
                sha = next((s for s in resolved if s.startswith(t) and s not in not_reachable), None)
                if sha:
                    credited[rid] = sha
                    break
        return credited
    except Exception:
        return {}


def landed_met_credit(
    root: Path, items: Sequence[object], ids: Iterable[str], detail: Optional[dict] = None
) -> frozenset[str]:
    """The subset of `ids` whose met verdict item has a met_by naming a commit reachable from HEAD,
    whether it landed before or after the halt base, and the cited commit changed a file or is a
    merge (an empty commit is no landing evidence). Nothing is credited when `detail` already
    holds CITED_DETAIL_KEY (the at-or-before-base landing stands). Never raises; one extra git
    spawn over the cited shas, none when no row cites a token; a git failure credits no row."""
    if isinstance(detail, dict) and CITED_DETAIL_KEY in detail:
        return frozenset()
    try:
        cited = cited_at_or_before(root, "HEAD", items, ids)
        if not cited:
            return frozenset()
        res = run_git(
            ["log", "--no-walk=unsorted", "--format=%x00%H %P", "--name-only", *sorted(set(cited.values()))],
            cwd=str(root),
        )
        if res.returncode != 0:
            return frozenset()
        changed: set[str] = set()
        for block in res.stdout.split("\0")[1:]:
            head, _, names = block.partition("\n")
            parts = head.split()
            if parts and (len(parts) > 2 or names.strip()):
                changed.add(parts[0])
        return frozenset(rid for rid, sha in cited.items() if sha in changed)
    except Exception:
        return frozenset()


PATH_CREDIT_DETAIL_KEY: str = "path_credit"
_PATH_END = r"(?![\w/-]|\.\w)"


def _named_declared(text: str, declared: Sequence[str]) -> list[str]:
    """The declared paths `text` names, an `@sha` or `:line` suffix after the path ignored."""
    norm = re.sub(r"(?<![\w.])\./", "", text.replace("\\", "/"))
    return [
        p for p in declared
        if re.search(r"(?<![\w/.-])" + re.escape(p) + _PATH_END, norm)
    ]


def met_by_paths_at_head_credit(
    root: Path,
    items: Sequence[object],
    ids: Iterable[str],
    declared_paths: Sequence[str],
    detail: Optional[dict] = None,
) -> frozenset[str]:
    """The subset of `ids` whose met verdict item has a met_by naming at least one declared product
    path, every named path being a file that is tracked and byte-identical between HEAD and the
    worktree, with a commit at HEAD that last changed them (the landing). Two git spawns over the
    union of every row's named paths (status, then log), none when no row names a path. When every
    id is credited, `detail` receives landed_sha, landed_paths, CITED_DETAIL_KEY and
    PATH_CREDIT_DETAIL_KEY, so `halt_landing.since_halt_close` reads the rows as landed; a partial
    credit leaves `detail` alone (a row without evidence must not ride another's close). Never
    raises; a git failure credits nothing."""
    try:
        wanted = [str(i) for i in ids]
        declared = [p for p in dict.fromkeys(declared_paths) if isinstance(p, str) and p]
        if not wanted or not declared:
            return frozenset()
        named: dict[str, list[str]] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("status") != "met":
                continue
            rid = str(item.get("id"))
            if rid not in wanted or rid in named:
                continue
            met_by = item.get("met_by")
            text = " ".join(str(m) for m in met_by) if isinstance(met_by, (list, tuple)) else str(met_by or "")
            hit = _named_declared(text, declared)
            if hit:
                named[rid] = hit
        union = sorted({p for hits in named.values() for p in hits})
        if not union:
            return frozenset()
        cwd = str(root)
        status = run_git(
            ["--no-optional-locks", "--literal-pathspecs", "status", "--porcelain", "-z",
             "--untracked-files=all", "--ignored=matching", "--", *union],
            cwd=cwd,
        )
        if status.returncode != 0:
            return frozenset()
        dirty = {
            entry[3:].replace("\\", "/") for entry in status.stdout.split("\0") if len(entry) > 3
        }
        clean = {
            p for p in union
            if p not in dirty and ((Path(root) / p).is_file() or (Path(root) / p).is_symlink())
        }
        credited_rows = {rid: hits for rid, hits in named.items() if all(p in clean for p in hits)}
        paths = sorted({p for hits in credited_rows.values() for p in hits})
        if not paths:
            return frozenset()
        log = run_git(
            ["--literal-pathspecs", "log", "-1", "--format=%H", "HEAD", "--", *paths], cwd=cwd,
        )
        sha = log.stdout.strip()
        if log.returncode != 0 or len(sha) != 40:
            return frozenset()
        if isinstance(detail, dict) and set(credited_rows) == set(wanted):
            detail.setdefault("landed_sha", sha)
            detail.setdefault("landed_paths", paths)
            cited = detail.setdefault(CITED_DETAIL_KEY, {})
            for rid in credited_rows:
                cited.setdefault(rid, sha)
            detail[PATH_CREDIT_DETAIL_KEY] = {rid: sha for rid in credited_rows}
        return frozenset(credited_rows)
    except Exception:
        return frozenset()


def refire_futile_fields(failures: Sequence[object]) -> dict:
    """`{"refire_futile": True}` when every ledger failure is the unchanged-since-halt refusal (a refire
    reproduces it), else `{}`; spread into the refused reply."""
    if failures and all(isinstance(f, dict) and f.get("reason") == UNCHANGED_SINCE_HALT_REASON for f in failures):
        return {"refire_futile": True}
    return {}


def verified_post_commit_credit(
    root: Path, base: str, items: Sequence[object], credited: Iterable[str]
) -> frozenset[str]:
    """The subset of `credited` whose verdict item is post_commit and whose met_by resolves to
    `base` or an ancestor of it. No git spawn when no credited id is post_commit. Never raises;
    a git failure withholds verification from every row (fail closed); an unresolvable token
    only fails its own row."""
    try:
        verified = cited_at_or_before(root, base, items, credited, statuses=("post_commit",))
        return frozenset(verified)
    except Exception:
        return frozenset()

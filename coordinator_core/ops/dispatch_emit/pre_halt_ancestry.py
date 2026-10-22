"""Which ledger rows a verdict credits with a commit at or before the halt: a row of one of the
requested statuses (default met only; a post_commit row is never an already_landed close) whose
met_by names a sha token that resolves to `base` or an ancestor of it. At most two git spawns,
both through `run_git`, none when no row cites a token. land_run._unchanged_since_halt and
terminal_commit._unchanged_since_halt_failures each repeat the landing-sha rule (one distinct
cited sha, else base); change both together."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.pre_halt_credit import sha_tokens

MAX_CITED: int = 8
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

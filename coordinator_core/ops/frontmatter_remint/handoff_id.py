"""handoff_id resolver for `frontmatter.remint`: the wire-derived id, never a fabricated one.

The value is `_derive_handoff_id(repo, path)[0]` with `repo` the emitting repo's own
slug, because the hash input is `f"{repo}:{basename}"` and any other label mints an id
the cockpit wire never publishes. The mint site is guarded here, not left to the schema.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from coordinator_core.dag import (
    build_handoff_id_index,
    read_handoff_meta,
    scan_repo_handoff_corpus,
)
from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.ops.emit.sections.handoffs import _derive_handoff_id
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved
from coordinator_core.win_portability import no_console_creationflags

_MINTED_ID_RE = re.compile(r"^hnd-(?!placeholder-replace-with)[a-z0-9-]+-[0-9a-f]{6}$")

_SCAN_TIMEOUT_S = 2

#: Where a handoff id is cited: state ledgers and the handoff archive, not the whole repo (the
#: full-repo scan measured over the 500ms bar). Machine-written run artifacts are excluded.
_SCAN_SCOPE = (
    "state",
    "archive/handoffs",
    ":(exclude)state/warp",
    ":(exclude)state/plan-blitz",
    ":(exclude)state/sizings",
    ":(exclude)state/queue-grind",
    ":(exclude)state/run-evidence",
    ":(exclude)state/review-trail",
    ":(exclude)state/mise-workflows",
)


#: Per-call citer index set by `primed` (old id -> repo-relative citing files). Trap: one scan per
#: call, never per path -- a per-path spawn made a 14-path call ~2-3s of process time.
_PRIMED: contextvars.ContextVar[dict[str, list[str]] | None] = contextvars.ContextVar("remint_citers", default=None)


def scan_citers(old_ids: Iterable[str], repo_root: Path) -> dict[str, list[str]]:
    """Repo-relative files under `_SCAN_SCOPE` (tracked or untracked, not ignored) citing each id.

    One `git grep --no-index` spawn for every id; `--no-index` keeps it valid where `repo_root` is
    not a repository. A spawn failure, timeout or git error (exit >1) raises rather than reporting
    no citers.
    """
    ids = sorted({i for i in old_ids if i})
    found: dict[str, set[str]] = {i: set() for i in ids}
    if not ids:
        return {}
    patterns = [arg for i in ids for arg in ("-e", i)]
    proc = subprocess.run(
        [
            "git", "-c", "grep.threads=1", "grep", "--no-index", "--exclude-standard",
            "-I", "-F", "-o", "-z", *patterns, "--", *_SCAN_SCOPE,
        ],
        cwd=repo_root,
        capture_output=True,
        timeout=_SCAN_TIMEOUT_S,
        **no_console_creationflags(),
    )
    if proc.returncode > 1:
        raise RuntimeError(f"citer scan failed: git grep exit {proc.returncode}")
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        rel, sep, match = line.partition("\0")
        if sep and match in found:
            found[match].add(rel.replace("\\", "/"))
    return {i: sorted(v) for i, v in found.items()}


@contextlib.contextmanager
def primed(old_ids: Iterable[str], repo_root: Path) -> Iterator[None]:
    """Run one citer scan for every id up front; `resolve` reads it instead of spawning."""
    token = _PRIMED.set(scan_citers(old_ids, repo_root))
    try:
        yield
    finally:
        _PRIMED.reset(token)


def _citers(old_id: str, repo_root: Path, self_abs: str) -> list[str]:
    """Files citing `old_id`, self excluded: from the primed index, else one scan of its own."""
    index = _PRIMED.get()
    hits = index[old_id] if index is not None and old_id in index else scan_citers([old_id], repo_root).get(old_id, [])
    root = os.path.abspath(repo_root)
    return [h for h in hits if os.path.abspath(os.path.join(root, h)) != self_abs]


def resolve(path: Path, fm: Mapping[str, Any], repo_root: Path) -> Resolved | Refused:
    """Derive the wire handoff_id for `path`; refuse if underivable, referenced or colliding."""
    repo = resolve_repo_name(repo_root)
    new_id = _derive_handoff_id(repo, str(path))[0]
    if not _MINTED_ID_RE.match(new_id):
        return Refused("handoff-id-underivable", f"derived {new_id!r} fails the minted-id shape")

    self_abs = os.path.abspath(path)
    paths = scan_repo_handoff_corpus(str(repo_root))
    metas = {os.path.abspath(p): read_handoff_meta(p) for p in paths}
    index = build_handoff_id_index(paths, metas)

    holder = index.get(new_id)
    if holder is not None and holder != self_abs:
        return Refused("handoff-id-collision", f"{new_id} is already held by {holder}")

    old = fm.get("handoff_id")
    old_id = str(old).strip() if old is not None else ""
    if old_id and old_id != new_id:
        try:
            refs = _citers(old_id, repo_root, self_abs)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return Refused("handoff-id-referenced", f"{old_id} citer scan failed: {exc}")
        if refs:
            return Refused("handoff-id-referenced", f"{old_id} is cited by {', '.join(refs)}")
    return Resolved({"handoff_id": new_id})

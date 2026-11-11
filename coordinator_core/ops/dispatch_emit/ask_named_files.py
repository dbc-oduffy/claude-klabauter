"""Resolve the files an ask names through test node ids and bare test-module stems."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from coordinator_core.git.run import run_git
from coordinator_core.git.tracked_case import tracked_spelling
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import named_paths

_EDGES = "`'\"()[]<>{}"
_PUNCT = ".,;:"
_STEM = re.compile(r"^test_\w+$")


def _tokens(texts: Iterable[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        for raw in str(text).split():
            tok = raw.strip(_EDGES + _PUNCT)
            if tok:
                out.append(tok)
    return out


def _stem_hits(stems: list[str], repo_root: Path) -> dict[str, list[str]]:
    specs = [f":(glob)**/{s}.py" for s in stems]
    res = run_git(["ls-files", "-z", "--", *specs], cwd=str(repo_root))
    hits: dict[str, list[str]] = {s: [] for s in stems}
    if res.returncode != 0:
        return hits
    for rel in filter(None, res.stdout.split("\0")):
        stem = rel.rsplit("/", 1)[-1][: -len(".py")]
        if stem in hits:
            hits[stem].append(rel)
    return hits


def named_test_files(texts: Iterable[str], repo_root: Path) -> list[str]:
    """Repo-relative POSIX paths the `texts` name by `path::test` node id or bare `test_*` module stem.

    A node id contributes its file part when that file exists. A bare stem contributes its tracked
    path only when exactly one tracked `<stem>.py` exists; an ambiguous or untracked stem names
    nothing. Stems resolve in one `git ls-files` spawn. Node-id files come first, then stems, each in
    order of appearance; de-duplicated.
    """
    repo_root = Path(repo_root)
    toks = _tokens(texts)
    file_parts = [t.split("::", 1)[0] for t in toks if "::" in t]
    stems = list(dict.fromkeys(t for t in toks if _STEM.match(t)))
    by_file = named_paths(
        file_parts, repo_root, spelling=lambda paths: tracked_spelling(repo_root, paths)
    ) if file_parts else []
    hits = _stem_hits(stems, repo_root) if stems else {}
    out = list(by_file)
    for stem in stems:
        if len(hits.get(stem, ())) == 1 and hits[stem][0] not in out:
            out.append(hits[stem][0])
    return out

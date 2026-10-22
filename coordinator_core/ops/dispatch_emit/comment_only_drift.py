"""Comment-only blob-drift proof for `dispatch.terminal_commit`'s refusal path.

A drifted path is tolerated only when the bytes the review read (the write-once pre-review
snapshot beside the prep sidecar) and the bytes now on disk are the same Python AST, docstrings
included. A non-`.py` path is tolerated only when the repo-declared verifier
(`comment_only_verifier`) exits 0 over every non-`.py` candidate at once. Anything unprovable is
real drift. Pure byte reads and `ast`; zero spawns on the .py path; at most one on the non-.py
path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Collection, Mapping, Optional, Sequence

from coordinator_core.comment_strip.lang_python import proof_equivalent
from coordinator_core.git.content_hash import eol_folded_sha256

_SIDECAR_SUFFIX = ".review-prep.md"


def verify_comment_only(rel_path: str, before: bytes, after: bytes) -> bool:
    """True iff `rel_path` is a `.py` file, both sides decode as UTF-8 and parse, and their ASTs
    are equal with docstrings kept (comments and formatting only). Any other suffix, a decode
    or parse failure -> False."""
    if not rel_path.lower().endswith(".py"):
        return False
    try:
        old = before.replace(b"\r\n", b"\n").decode("utf-8")
        new = after.replace(b"\r\n", b"\n").decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Trap: `set()` keeps every docstring in the compared AST; `None` would strip them from
    # `before` only and let a docstring rewrite read as comment-only.
    return proof_equivalent(old, new, set())


def reviewed_preimages(
    worktree_root: Path, prep_sidecar_rel: Optional[str], expected: Mapping[str, str]
) -> dict[str, bytes]:
    """{path: bytes} for each path in `expected` (path -> eol-folded sha256) whose pre-review
    snapshot blob exists beside the sidecar AND eol_folded_sha256(blob) == expected[path].
    A path with no blob, or a blob that hashes otherwise, is absent. Zero spawns."""
    if not isinstance(prep_sidecar_rel, str) or not prep_sidecar_rel:
        return {}
    root = Path(worktree_root)
    sidecar = root / prep_sidecar_rel
    try:
        if root.resolve() not in sidecar.resolve().parents:
            return {}
    except (OSError, ValueError):
        return {}
    name = sidecar.name
    if not name.endswith(_SIDECAR_SUFFIX):
        return {}
    snap = sidecar.parent / f"{name[: -len(_SIDECAR_SUFFIX)]}.pre-review"
    try:
        files = json.loads((snap / "manifest.json").read_text(encoding="utf-8")).get("files")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(files, dict):
        return {}
    out: dict[str, bytes] = {}
    for path, want in expected.items():
        raw = files.get(path)
        if not isinstance(raw, str) or not raw or "/" in raw or "\\" in raw or raw.startswith("."):
            continue
        try:
            blob = (snap / raw).read_bytes()
        except OSError:
            continue
        if eol_folded_sha256(blob) == want:
            out[path] = blob
    return out


def tolerated_drift(
    worktree_root: Path,
    prep_sidecar_rel: Optional[str],
    drift: Sequence[Mapping],
    *,
    untrusted_paths: Collection[str] = (),
) -> tuple[list[dict], list[str]]:
    """(tolerated, real) over a commit_v2 `blob_drift` list. tolerated: [{path, expected,
    actual}] for content-sha256 entries whose preimage resolves, whose current worktree bytes
    hash to `actual`, and for which verify_comment_only(preimage, current) holds (`.py`) or the
    declared verifier accepted the whole non-`.py` batch. Non-`.py` is all-or-nothing: one
    undeclared suffix or a False verify_pairs makes every non-`.py` candidate real. real: every
    other drifted path (a `blob`-basis entry, a null actual, no preimage, or a failed proof).
    `untrusted_paths` (the landing set) is forwarded so a verifier the landing writes is refused."""
    candidates = {
        d["path"]: d["expected"]
        for d in drift
        if d.get("basis") == "content-sha256"
        and d.get("actual")
        and isinstance(d.get("path"), str)
        and isinstance(d.get("expected"), str)
    }
    preimages = reviewed_preimages(worktree_root, prep_sidecar_rel, candidates) if candidates else {}
    tolerated: list[dict] = []
    real: list[str] = []
    root = Path(worktree_root)
    verdict: dict[str, bool] = {}
    other: list[tuple[str, bytes, bytes]] = []
    for d in drift:
        path = d.get("path")
        pre = preimages.get(path) if isinstance(path, str) else None
        ok = False
        if pre is not None:
            try:
                current = (root / path).read_bytes()
            except OSError:
                current = None
            if current is not None and eol_folded_sha256(current) == d["actual"]:
                if path.lower().endswith(".py"):
                    ok = verify_comment_only(path, pre, current)
                else:
                    other.append((path, pre, current))
        verdict[path] = ok
    if other:
        from coordinator_core.ops.dispatch_emit import comment_only_verifier as cov

        decl = cov.declared_verifier(root)
        if decl is not None and all(cov.covers(decl, p) for p, _, _ in other):
            if cov.verify_pairs(root, decl, other, untrusted_paths):
                for p, _, _ in other:
                    verdict[p] = True
    for d in drift:
        path = d.get("path")
        if verdict.get(path):
            tolerated.append({"path": path, "expected": d["expected"], "actual": d["actual"]})
        else:
            real.append(path)
    return tolerated, real

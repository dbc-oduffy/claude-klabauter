"""
coordinator_core.ops.detect_primary_languages — JSON-RPC "detect.primary_languages"
operation.

Purpose: pure-Python, cross-platform port of the fence-inventory
`detect-primary-languages-by-extension` fingerprint
(`pipelines/deep-research/repo-research-internals.md:11`) — rank a caller-supplied
`target_root`'s file extensions by count to pick primary language(s) for
repomap generation in the deep-research pipeline.

Fence oracle (POSIX coreutils pipeline, never executed here — ported by
INTENT, not transliterated, per constraint (2) / DEC-2): a `find` walk with
`-o` (or) clauses fanning out over candidate extensions, piped through
`sed`/`sort`/`uniq -c` to tally and rank, with `ls ... 2>/dev/null`-style
stderr-swallow idioms guarding missing paths.

Port notes (why this is not a transliteration):
    - The `find -o` clause list is replaced by a single `os.walk` pass that
      tallies EVERY file extension found under `target_root` via
      `collections.Counter`, not a fixed candidate set — a strict
      generalization of the fence's enumerated extensions, so a language the
      fence's `find` clause never anticipated still surfaces.
    - `sort | uniq -c` (count + rank) becomes `Counter.most_common()`; ties
      are broken by extension name (ascending) for a deterministic,
      platform-independent order — the fence's `sort` output order was itself
      an accident of coreutils' locale-collation, not a meaningful contract.
    - The `ls ... 2>/dev/null` stderr-swallow idiom (silently skip
      unreadable/missing entries) becomes an explicit `try/except OSError`
      around each `os.walk` directory read — same observable behavior (skip
      and continue), zero subprocess, zero shell stderr redirection.
    - Vendor/build/VCS directories are pruned from the walk (see
      `_SKIP_DIR_NAMES`, shared convention with `cartography_stack.py`) so a
      single `.venv` or `node_modules` tree cannot dominate the extension
      tally on a large repo — the fence's `find` had no such pruning and
      would have been skewed the same way on a real repo; this closes that
      gap rather than reproducing it.

Wire params:
    target_root (str, required) — containment root to fingerprint. Guarded
                                   via coordinator_core.cartography._guard.path_guard
                                   (post-resolve() containment, symlink-safe).
    top_n       (int, optional) — cap on how many extensions populate
                                   `primary` (default 3). Does not truncate
                                   `counts`, which always reports every
                                   extension found.

Reply fields:
    counts  (list[{"extension": str, "count": int}]) — every distinct file
            extension found under target_root with its file count, ranked
            descending by count then ascending by extension name.
    primary (list[str]) — the top `top_n` extensions by count (same order as
            `counts`), the fence's "primary language(s)" signal. Empty when
            no files were found.

Idempotency (AC7): read-only extension tally — no file is opened for write,
no state is mutated, no subprocess is spawned. A second invocation with
identical inputs returns a byte-identical result; the on-disk tree is the
only input and this handler never mutates it. Manifest idempotency hazard:
none (op-classification.tsv row `detect-primary-languages-by-extension`).

Negative-spec:
    - Does NOT shell out to `find`/`sed`/`sort`/`uniq`/`ls` — pure `os.walk`
      + `pathlib` + `collections.Counter`.
    - Does NOT cap or sample the directory walk — every file under
      `target_root` (minus `_SKIP_DIR_NAMES`) is tallied; only the reported
      `primary` list is capped, at `top_n`.
    - Does NOT special-case a fixed extension allowlist — every extension
      found is counted, a strict superset of the fence's enumerated clauses.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
§ Wave 2 "detect" cluster; manifest row in
state/audits/2026-07-22-command-payload-inventory/op-classification.tsv.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography._skip_dirs import SKIP_DIR_NAMES as _SKIP_DIR_NAMES
from coordinator_core.ipc import register_op

_DEFAULT_TOP_N = 3


def _tally_extensions(root: Path) -> Counter:
    """Return a Counter of file extension -> count found anywhere under `root`.

    Skips `_SKIP_DIR_NAMES` directories entirely (not merely their
    contents), mirroring `os.walk`'s in-place `dirnames` pruning idiom.
    Extensionless files (no dot in the name) are skipped — they carry no
    language signal.
    """
    counts: Counter = Counter()
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _err: None):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for filename in filenames:
            suffix = Path(filename).suffix
            if suffix:
                counts[suffix] += 1
    return counts


def detect_primary_languages(target_root: str | Path, top_n: int = _DEFAULT_TOP_N) -> dict:
    """Fingerprint `target_root`'s dominant file extensions.

    Pure function over an already-resolved, contained root — no param
    validation, no registry side effect (that lives in the registered
    handler below). Exposed at module scope for direct unit testing.
    """
    root = Path(target_root)
    tally = _tally_extensions(root)
    ranked = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    counts = [{"extension": ext, "count": count} for ext, count in ranked]
    primary = [ext for ext, _count in ranked[:top_n]]
    return {"counts": counts, "primary": primary}


@register_op("detect.primary_languages")
async def _detect_primary_languages(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "detect.primary_languages" handler.

    Required params: target_root. Optional params: top_n (default 3).

    Returns: {counts, primary} (see module docstring "Reply fields").

    Raises ValueError if target_root is missing or top_n is not a positive
    int; propagates coordinator_core.cartography._guard.PathEscapeError if
    target_root cannot be resolved or escapes itself.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("detect.primary_languages requires param: target_root")

    top_n = params.get("top_n", _DEFAULT_TOP_N)
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n < 1:
        raise ValueError("detect.primary_languages param top_n must be a positive int")

    guarded_root = path_guard(target_root, ".")
    return detect_primary_languages(guarded_root, top_n=top_n)

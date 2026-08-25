"""
coordinator_core.ops.eol.audit_producers — "eol.audit_producers" op.

Purpose: makima's own text-mode-write ratchet
(`coordinator_core/tests/test_text_mode_writes_pin_newline.py`) only ever scans
this repo. This op makes the identical verdict available against any caller-
supplied `target_root` — the accrual fix a sibling repo actually needs, since a
one-time `eol.repair` pass without it just refills (see the plan's § Problem).

AST predicate lifted verbatim from
`coordinator_core/tests/test_text_mode_writes_pin_newline.py`
(`_is_test_file`, `_literal_mode`, `_writes_text`, `_pins_newline`,
`_offenders_in`, `_production_sources`, `_SKIP_DIRS`, `_OPEN_NAMES`,
`_OPEN_ATTRS`, `_TEXT_WRITE_ATTRS`) — duplicated, not imported: that module is
test code (out of this op's scope per this plan's C4, and per the source
module's own "TEST CODE IS OUT OF SCOPE" negative-spec), and importing a
`test_*` module from production code is not a shape this repo uses elsewhere.
Pointing this op at `target_root=<this repo>` must reproduce
`test_no_production_text_write_omits_newline`'s OFFENDER SET (AC6) — same
test-file exclusion, same literal-mode/newline-pin logic — so any future edit
to the shared predicate logic must be mirrored here (or lifted into a shared
helper both import; not done here because the source module is explicitly
test-only and this op's scope per C4 is this file alone).
`test_audit_producers_matches_source_ratchet.py` enforces this as a live
comparison over this repo's own corpus, not a promise.

`_SKIP_DIRS` is the ONE deliberate, documented exception to the exact-mirror
claim above (F14, staff-eng review 2026-08-20): this op's set is a superset
of the ratchet's, adding `site-packages`/`build`/`dist` — directories the
ratchet's single fixed target (this repo) never needs to skip but an
arbitrary caller-supplied `target_root` can carry. See `_SKIP_DIRS`'s own
comment for the full rationale.

AST, never behavioural, for the same reason the source ratchet gives: Python
only translates `"\n"` to `"\r\n"` in text mode on Windows, so a behavioural
check (write a real file, inspect its bytes) passes vacuously on macOS/Linux —
exactly the hosts where the defect is invisible. Source-shape parsing is red
everywhere, host-independent.

Target resolution: `target_root` wire param, path-guarded via
`coordinator_core.cartography._guard.path_guard` — the injected `repo_root`
argument is ignored entirely (per this plan's anti-scope: "Do not resolve the
target root from the environment" / AC7). Reference shape:
`coordinator_core/ops/cartography_tree.py :: _cartography_tree`.

COMPUTE_ONLY / scope "none" classification is made at registration time (this
plan's C5), not here — this module makes no write of any kind: it only walks
`target_root`'s filesystem with `pathlib.Path.rglob` / `Path.read_text` and
parses source with `ast.parse`. No subprocess, no `open(..., "w")`.

Negative-spec:
  - Does NOT modify any producer file — read-only reporting (this plan's C8
    negative-spec: "eol.audit_producers pins its default mode as read-only
    reporting").
  - Does NOT walk test code — `_is_test_file` exclusion mirrors the source
    ratchet exactly (test writes go to `tmp_path`, no EOL policy governs them).
  - Does NOT spawn a subprocess of any kind (pure `pathlib`/`ast`).
  - Does NOT assert on a non-literal `mode=` (dynamic mode is not ours to
    accuse — mirrors the source predicate).

Spec backlink: docs/plans/2026-08-20-every-repo-detects-its-own-eol-drift.md § C4, AC6, AC7
Spec backlink: coordinator_core/tests/test_text_mode_writes_pin_newline.py (predicate source)
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.cartography._guard import path_guard
from coordinator_core.ipc import register_op

#: Directories that never hold production writes we own — mirrors the source
#: ratchet's `_SKIP_DIRS` (see module docstring on why this must stay in
#: lockstep rather than import the test module), WIDENED by three entries
#: the ratchet does not carry (F14, staff-eng review 2026-08-20):
#: `site-packages`, `build`, `dist`. The ratchet only ever walks THIS repo
#: (hardcoded `REPO`), where none of the three hold code this repo owns; this
#: op runs against any caller-supplied `target_root`, including a repo that
#: hosts the fleet's venv (this repo's own CLAUDE.md: "This repo hosts the
#: fleet's venv") or ships a build/dist output directory — both reachable in
#: practice, unlike the ratchet's single fixed target. Documented, DELIBERATE
#: divergence from an exact mirror — see
#: `test_audit_producers_matches_source_ratchet.py`'s own accounting for it.
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", "_vendor", "archive",
    "site-packages", "build", "dist",
})

_OPEN_NAMES = frozenset({"open"})
#: ``os.fdopen``, ``io.open``, ``Path.open`` all take the same mode/newline pair.
_OPEN_ATTRS = frozenset({"fdopen", "open"})
_TEXT_WRITE_ATTRS = frozenset({"write_text"})


def _is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    base = parts[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
        or "tests" in parts
    )


def _literal_mode(call: ast.Call) -> Optional[str]:
    """The call's mode as a literal, or None when absent/non-literal."""
    for kw in call.keywords:
        if kw.arg == "mode":
            v = kw.value
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
        if kw.arg is None:  # **kwargs -- mode may arrive dynamically
            return None
    if len(call.args) >= 2:
        v = call.args[1]
        return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
    return None


def _writes_text(call: ast.Call, attr: str) -> bool:
    if attr in _TEXT_WRITE_ATTRS:
        return True
    mode = _literal_mode(call)
    if mode is None:
        return False  # default "r", or dynamic -- not ours to assert on
    if "b" in mode:
        return False
    return any(c in mode for c in "wax+")


def _pins_newline(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "newline":
            return True
        if kw.arg is None:  # **kwargs could carry it; do not accuse
            return True
    return False


def _offenders_in(src: str, rel: str) -> List[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _OPEN_NAMES:
            attr = "open"
        elif isinstance(func, ast.Attribute) and (
            func.attr in _OPEN_ATTRS or func.attr in _TEXT_WRITE_ATTRS
        ):
            attr = func.attr
        else:
            continue
        if not _writes_text(node, attr):
            continue
        if _pins_newline(node):
            continue
        out.append(f"{rel}:{node.lineno}  {attr}(...)")
    return out


def _production_sources(root: Path) -> List[Tuple[str, str]]:
    found = []
    for path in root.rglob("*.py"):
        if _SKIP_DIRS.intersection(path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        if _is_test_file(rel):
            continue
        try:
            found.append((rel, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return found


def audit_producers(root: Path) -> dict:
    """Pure, synchronous core: walk `root` for production `.py` sources and
    report every text-mode write call that omits `newline=`.

    Returns {target_root, offenders: [str, ...], offender_count, scanned}.
    """
    sources = _production_sources(root)
    offenders: List[str] = []
    for rel, src in sources:
        offenders.extend(_offenders_in(src, rel))
    return {
        "offenders": sorted(offenders),
        "offender_count": len(offenders),
        "scanned": len(sources),
    }


@register_op("eol.audit_producers")
def _eol_audit_producers(params: dict, repo_root: Optional[Path] = None) -> dict:
    """"eol.audit_producers" handler — makima's text-mode-write ratchet,
    portable to any target_root. See module docstring for the full contract.

    Wire params:
        target_root (str, required) — root of the tree to walk. Must resolve
                                       inside a git worktree.

    Returns:
        target_root     (str)   — resolved target_root, echoed.
        offenders        (list) — sorted "<rel>:<lineno>  <call>(...)" strings.
        offender_count   (int)  — len(offenders).
        scanned          (int)  — number of production .py sources scanned.

    `repo_root` (injected by the engine) is ignored entirely — the target is
    resolved solely from `target_root`, per this plan's anti-scope ("Do not
    resolve the target root from the environment") and AC7.

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root cannot
    be resolved inside a git worktree.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("eol.audit_producers requires param: target_root")

    guarded_root = path_guard(target_root, ".")
    result = audit_producers(guarded_root)
    result["target_root"] = str(guarded_root)
    return result

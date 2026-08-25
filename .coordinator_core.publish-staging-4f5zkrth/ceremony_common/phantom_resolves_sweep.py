"""coordinator_core.ceremony_common.phantom_resolves_sweep — the shared
mechanical core for the phantom-resolves-id guard, generalized fleet-wide
(2026-07-27) from `workstream_complete.test_workstream_complete`'s own
`test_no_judgment_point_resolves_a_phantom_directive_id` (the sole prior
instance of this class of guard).

Background: a judgment point's `dispositions[].resolves` can name a
directive id nothing in the SAME pass ever emits, in which case choosing
that disposition silently resolves nothing at apply time. The
`jp-coverage-verdict`/`d-tail` bug (fixed 2026-07-27) was one instance of
this class in `workstream_complete`; nothing before this module swept any
OTHER assembler package for the same defect shape — this is the
generalization that closes that gap.

This module owns only the MECHANICAL satisfiability check (never a
per-package sweep — each package that emits judgment points is too
architecturally distinct for one universal sweep to construct valid input
for; see `test_phantom_resolves_id_sweep.py`'s per-package provider
registry for the part of this guard that IS package-specific).

Spec backlink: cross-repo/inbox/2026-07-27-… "Generalize seam guards
fleet-wide" dispatch (DoE-claude, 2026-07-27); prior-instance spec backlink
`docs/plans/2026-07-26-workstream-complete-computed-frontage.md`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, NamedTuple


class PhantomSweepResult(NamedTuple):
    """The three id sets one package's representative sweep collects —
    same triple `workstream_complete`'s own
    `_sweep_directive_ids_and_resolves_ids` returned, generalized to a
    named, importable shape any package's provider can build."""

    directive_ids: frozenset[str]
    resolves_ids: frozenset[str]
    judgment_point_ids: frozenset[str]


def resolves_id_is_satisfiable(
    resolves_id: str,
    emitted_directive_ids: Iterable[str],
    *,
    dynamic_suffix_bases: frozenset[str] = frozenset(),
) -> bool:
    """A `resolves` id is satisfiable when it names a directive id this
    sweep actually emitted, OR when it is a documented dynamic-suffix base
    (a resolves id some producer only ever emits with a caller-computed
    suffix — a per-lesson index, a memo basename, a review-slice id — so an
    exact match would false-fail a real, correctly-wired directive)."""
    emitted = frozenset(emitted_directive_ids)
    if resolves_id in emitted:
        return True
    if resolves_id in dynamic_suffix_bases:
        return any(d.startswith(f"{resolves_id}-") or d.startswith(f"{resolves_id}:") for d in emitted)
    return False


def assert_no_phantom_resolves_ids(
    result: PhantomSweepResult,
    *,
    package_name: str,
    dynamic_suffix_bases: frozenset[str] = frozenset(),
    no_directive_backing_ids: frozenset[str] = frozenset(),
) -> None:
    """Asserts every id in `result.resolves_ids` is satisfiable — exactly,
    via a documented dynamic-suffix family, or an explicitly named
    no-directive-backing exception (a resolves id naming a step with no
    `directives[]` entry at all, by design — e.g. `workstream_complete`'s
    `d-render-final-summary`, a pure string-formatting fan-in) — never a
    bare string nothing in `directives[]` will ever match."""
    for resolves_id in sorted(result.resolves_ids):
        if resolves_id in no_directive_backing_ids:
            continue
        assert resolves_id_is_satisfiable(
            resolves_id, result.directive_ids, dynamic_suffix_bases=dynamic_suffix_bases
        ), (
            f"{package_name}: a judgment point disposition resolves {resolves_id!r}, which names "
            "no directive this sweep ever emits (directly, via a documented dynamic-suffix family, "
            "or a named no-directive-backing exception) -- picking that disposition would silently "
            "resolve nothing"
        )


# ---------------------------------------------------------------------------
# Package discovery -- the same `brief(`-scan technique
# DoE-claude's `test_assembler_wiring_parity.py` uses against a resolved
# sibling checkout, run here directly against THIS repo's own
# `coordinator_core/` tree (no cross-repo resolution needed -- we ARE the
# makima side).
# ---------------------------------------------------------------------------

_COORDINATOR_CORE_ROOT = Path(__file__).resolve().parents[1]
_BRIEF_DEF_RE = re.compile(r"^def brief\(", re.MULTILINE)
_CONSUMES_MANIFEST_DEF_RE = re.compile(r"^CONSUMES_MANIFEST\s*[:=]", re.MULTILINE)


def discover_brief_defining_packages(coordinator_core: Path = _COORDINATOR_CORE_ROOT) -> dict[str, Path]:
    """Map package name -> the file under it that defines `brief(`, for
    every immediate subpackage of `coordinator_core/`. First hit wins per
    package (a package defining `brief(` in two files is a separate defect
    this discovery isn't scoped to catch) -- mirrors DoE-claude's
    `test_assembler_wiring_parity._brief_defining_packages` exactly, since
    both discover the same underlying fact from the same source tree."""
    found: dict[str, Path] = {}
    for path in sorted(coordinator_core.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _BRIEF_DEF_RE.search(text):
            continue
        rel = path.relative_to(coordinator_core)
        package = rel.parts[0]
        found.setdefault(package, path)
    return found


def discover_consumes_manifest_modules(
    coordinator_core: Path = _COORDINATOR_CORE_ROOT,
) -> dict[str, Path]:
    """Map package name -> the file under it that defines a module-level
    `CONSUMES_MANIFEST` constant. This is the auto-discovery basis for the
    argv prog-slot contract guard's manifest union (`test_argv_prog_slot_
    contract.py`) -- a newly-added ceremony package that defines its own
    `CONSUMES_MANIFEST` is picked up automatically, with no hand-edited
    import list to fall out of sync."""
    found: dict[str, Path] = {}
    for path in sorted(coordinator_core.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _CONSUMES_MANIFEST_DEF_RE.search(text):
            continue
        rel = path.relative_to(coordinator_core)
        package = rel.parts[0]
        found.setdefault(package, path)
    return found


SweepProvider = Callable[[], PhantomSweepResult]

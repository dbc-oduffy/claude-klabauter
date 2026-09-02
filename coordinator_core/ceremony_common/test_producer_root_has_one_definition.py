"""
coordinator_core.ceremony_common.test_producer_root_has_one_definition —
the artifact that keeps the producer-root seam from forking a third time.

Purpose: the engine's `coordinator/bin` directory is computed in exactly one
place FOR THE CEREMONY CLI-DISPATCH PATH — `resolve_cli_script_root`. Every
module that dispatches a consumes-manifest producer resolves through that
call rather than recomputing `parents[2] / "coordinator" / "bin"` inline.

Why this file exists rather than a note asking authors to remember: the same
defect was fixed twice, incompatibly, on two branches (2026-09-01 and
2026-09-02), because the seam had been deleted and each caller left holding
its own copy of the expression. One of those fixes argued the seam could not
exist at all. A rule with no artifact behind it is how that happened; this is
the artifact.

NEGATIVE SPEC — this guard is deliberately NOT repo-wide. Modules outside the
dispatch path legitimately compute an engine-relative directory for their own
reasons: `install/door_install.py` names the generator's bin dir,
`plugin_health/sentinel.py` and `ops/` resolve individual scripts or a `lib/`
subpath. Those are different questions with different answers, and widening
this guard to cover them would force a shared helper onto call sites that do
not share a requirement. The claim under test is about the ceremony CLI
dispatch path only.
"""
from __future__ import annotations

import re
from pathlib import Path

#: The packages whose producer resolution is the seam's business. A module
#: added here is claiming its scripts ship in the engine's `coordinator/bin`.
_DISPATCH_PACKAGES = (
    "ceremony_common",
    "merge_assemble",
    "workday_complete",
    "workstream_complete",
    "workweek_complete",
)

#: The expression the seam exists to own. Matched on source text rather than
#: AST because the failure mode being prevented is a COPY — a reader pasting
#: the line into a new module — and a copy is textual.
_INLINE_JOIN = re.compile(
    r'parents\[2\]\s*/\s*"coordinator"\s*/\s*"bin"'
)

_PKG_ROOT = Path(__file__).resolve().parents[1]

#: `resolve_cli_script_root`'s own body is the one legal site: it IS the
#: definition every other module resolves through.
_DEFINITION_SITE = _PKG_ROOT / "ceremony_common" / "cli_dispatch.py"


def _dispatch_path_modules() -> list[Path]:
    out: list[Path] = []
    for pkg in _DISPATCH_PACKAGES:
        for f in sorted((_PKG_ROOT / pkg).rglob("*.py")):
            if f.name.startswith("test_") or "tests" in f.parts:
                continue
            out.append(f)
    return out


def test_the_dispatch_path_computes_the_producer_root_in_exactly_one_place():
    """A second definition is the fork. The two rival 2026-09 fixes both
    started from a caller that owned its own copy of this expression."""
    offenders: list[str] = []
    for f in _dispatch_path_modules():
        if f == _DEFINITION_SITE:
            continue
        text = f.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue  # prose citing the expression is not a second copy
            if _INLINE_JOIN.search(line):
                offenders.append(f"{f.relative_to(_PKG_ROOT)}:{i}: {line.strip()}")

    assert not offenders, (
        "the producer root is computed outside `resolve_cli_script_root`:\n  "
        + "\n  ".join(offenders)
        + "\n\nResolve through `ceremony_common.cli_dispatch.resolve_cli_script_root()` "
        "instead. See that function's NEGATIVE SPEC for what a second copy cost "
        "the last two times."
    )


def test_the_definition_site_still_holds_the_definition():
    """Guards the guard: if `resolve_cli_script_root` is deleted again, the
    test above passes vacuously — nothing computes the root inline because
    nothing computes it at all — and reads as green while the seam is gone."""
    from coordinator_core.ceremony_common.cli_dispatch import resolve_cli_script_root

    assert _INLINE_JOIN.search(_DEFINITION_SITE.read_text(encoding="utf-8"))
    assert resolve_cli_script_root() == _PKG_ROOT.parent / "coordinator" / "bin"

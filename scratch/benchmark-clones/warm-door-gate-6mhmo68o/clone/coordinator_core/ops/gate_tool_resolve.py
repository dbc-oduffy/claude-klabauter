"""
coordinator_core.ops.gate_tool_resolve -- shared PATH-resolution seam for the
five DoD tools `gate.validate_invocable`'s dimension modules shell out to
(C1b, docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1b).

WHY THIS IS NOT A `pyproject.toml` DEPENDENCY
    `docs/decisions/2026-07-21-coordinator-core-dependency-and-environment-
    boundary.md` D1 ("required, plainly declared, no extras") and D2
    (provisioned onto the machine by `scripts/setup.py`) govern IMPORTED
    runtime dependencies of `coordinator_core` -- pydantic, psutil. D1's own
    rationale is explicit that it is about import-time behaviour: "no
    conditional-import fallback anywhere in coordinator_core". mypy, ruff,
    interrogate, diff-cover, and pytest-cov are never imported by anything in
    this package -- they are external executables an ADVISORY gate shells
    out to, once, per dimension, when asked. D1/D2 do not reach them:
    declaring five dev tools as hard requirements for every machine that
    runs the installer would tax every install for a check most machines
    never invoke.

PRECEDENT
    `coordinator/bin/static-check` `resolve_pyright()` (2026-08-11,
    `docs/reference/shell-out-carve-outs.md` class (f)) is the shape this
    module generalizes from one tool to five: PATH-resolved only
    (`shutil.which`, never a hardcoded path), no manifest entry, degrade to
    a truthful `UNAVAILABLE` verdict naming the tool and how to get it,
    never a silent pass and never a crash. This module is the ONE place
    that resolution logic lives -- the five dimension modules (C2 types,
    C3 docstrings, C4 tests) each call `resolve_tool()` once for their own
    tool rather than hand-rolling a sixth `shutil.which` variant.

WHAT THIS MODULE DOES NOT DO
    It does not invoke any tool -- `resolve_tool()` only answers "is X on
    PATH, and if not, why should a caller not treat that as a crash." The
    dimension module that owns a tool decides how to invoke it (argv shape,
    timeout, output parsing) and is the shell-out-carve-out site of record
    for that invocation, per `docs/reference/shell-out-carve-outs.md`'s
    per-site enumeration -- this module supplies only the executable path.

DEGRADE PATH -- keeps `_overall_verdict`'s no-vacuous-PASS property true
    A dimension whose tool does not resolve returns
    `Verdict.UNAVAILABLE` with a `detail` naming the tool and a one-line
    "how to get it" (never a bare "not found"). `gate_validate_invocable
    ._overall_verdict` already treats UNAVAILABLE as non-PASS unless another
    dimension actually measured something (see that module's docstring) --
    this module's job is only to make the per-dimension UNAVAILABLE
    detection itself PATH-resolved rather than manifest-declared, not to
    touch that aggregation rule.

WINDOWS
    `shutil.which` resolves PATHEXT-suffixed executables (`.exe`, `.cmd`,
    `.bat`) transparently on Windows when given a bare name -- unlike
    `coordinator/bin/static-check`'s pyright case (an npm global shim that
    needed an explicit `.cmd` fallback candidate), none of these five tools
    need a second candidate name; `shutil.which("mypy")` alone resolves the
    Windows Scripts-dir shim. Kept as one candidate per tool rather than
    speculatively adding `.cmd`/`.exe` variants that `shutil.which` already
    covers.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C1b
"""

from __future__ import annotations

import dataclasses
import shutil
from typing import Optional

# One tool name per DoD dimension leg that shells out. Kept as a plain
# mapping (not an enum) so a caller can iterate `KNOWN_TOOLS` for a
# provisioning-status report without importing a dimension module.
KNOWN_TOOLS: dict[str, str] = {
    "mypy": "pip install mypy",
    "ruff": "pip install ruff",
    "interrogate": "pip install interrogate",
    "diff-cover": "pip install diff-cover",
    "pytest-cov": "pip install pytest-cov",
}
"""tool name -> a one-line, copy-pasteable install hint for that tool's
`UNAVAILABLE` detail string. `pytest-cov` is a pytest PLUGIN, not a
standalone executable -- see `resolve_tool()`'s docstring for how its
resolution differs from the other four."""


@dataclasses.dataclass(frozen=True)
class ToolResolution:
    """Result of resolving one DoD tool.

    `available` is the single field a dimension module needs to branch on;
    `path` and `reason` are for the `UNAVAILABLE` detail string / a
    provisioning-status report, never for the dimension module to
    re-derive resolution logic against.
    """

    tool: str
    available: bool
    path: Optional[str]
    reason: str


def resolve_tool(tool: str) -> ToolResolution:
    """Resolve one DoD tool by PATH lookup only -- never a hardcoded path.

    `tool` must be a key of `KNOWN_TOOLS`; an unknown name raises
    `ValueError` rather than silently resolving nothing (a typo'd tool name
    reading as "legitimately absent" would be a worse failure mode than a
    loud error at the call site that introduced the typo).

    `pytest-cov` is a pytest plugin, not a standalone CLI -- `shutil.which`
    would never find a `pytest-cov` executable because none exists. It
    resolves via `importlib.util.find_spec("pytest_cov")` instead (the
    installed-distribution name `pytest-cov` maps to the importable module
    `pytest_cov`, per that package's own packaging). `path` is left `None`
    for this tool even when available, since there is no executable path to
    report -- a caller must not assume `available=True` implies `path` is
    populated.
    """
    if tool not in KNOWN_TOOLS:
        raise ValueError(
            f"gate_tool_resolve.resolve_tool: unknown tool {tool!r}; "
            f"must be one of {sorted(KNOWN_TOOLS)}"
        )

    install_hint = KNOWN_TOOLS[tool]

    if tool == "pytest-cov":
        import importlib.util

        spec = importlib.util.find_spec("pytest_cov")
        if spec is None:
            return ToolResolution(
                tool=tool,
                available=False,
                path=None,
                reason=(
                    f"pytest-cov not importable (pytest_cov module not found); "
                    f"install with: {install_hint}"
                ),
            )
        return ToolResolution(tool=tool, available=True, path=None, reason="resolved")

    found = shutil.which(tool)
    if found is None:
        return ToolResolution(
            tool=tool,
            available=False,
            path=None,
            reason=f"{tool} not found on PATH; install with: {install_hint}",
        )
    return ToolResolution(tool=tool, available=True, path=found, reason="resolved")

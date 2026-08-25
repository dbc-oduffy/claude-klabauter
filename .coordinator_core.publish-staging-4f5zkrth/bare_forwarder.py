"""Bare-name forwarder: resolve and exec the CLAUDE_HOME-installed CLI of the
same name. Used by plugin-bin shims so a bare command resolves on POSIX tool
shells where ~/.claude/bin is not on PATH.
Spec backlink: docs/plans/2026-06-18-machine-local-bare-invocation-macos.md
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (T1b chunk B2 — bare forwarders: claude-home, coordinator-settings-home)
"""
from __future__ import annotations

import os
import sys

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.win_portability import is_executable


def forward(name: str, argv: list[str] | None = None) -> None:
    """Exec the settings-home-first-resolved ``<name>`` binary with argv, or
    exit 127 with a remediation message if neither rung is executable.

    Resolution order (Gate 1, settings-home-first): try
    ``<settings-home>/bin/<name>`` first; fall back to the legacy
    ``.claude/bin/<name>`` rung under CLAUDE_HOME (or under the home directory
    when CLAUDE_HOME is unset) when the settings-home
    candidate isn't executable. Mirrors the settings-home-first *ordering*
    used by ``coordinator_core.pyresolve._machine_local_impl`` — the
    predicate differs (``os.access(X_OK)`` here vs ``os.path.exists`` there)
    because this rung's result is ``execv``'d directly and must be
    executable, while pyresolve's is invoked via ``subprocess.run([sys.executable,
    ...])`` and only needs to exist.
    Review: code-reviewer — Finding 1, 2026-07-24-codereview-sliceowns-zero-makima
    sidecar (docstring overstated "precedence" match; tightened to "ordering"
    plus the predicate-divergence rationale).

    `argv` defaults to ``sys.argv[1:]`` when omitted (matching pre-widen
    behavior for direct `python -m` invocation); callers that already have an
    explicit argv (e.g. an entrypoint's own `main(argv)` parameter) should
    pass it through so the forwarded args and the declared argv can't diverge.
    Review: code-reviewer — Finding 1, 2026-07-22-codereview-slice... sidecar
    (machine_local_forwarder.main(argv) silently ignored its own argv param,
    forwarding sys.argv instead; widened here so the two stay in sync by
    construction rather than by test-author discipline).

    NEGATIVE-SPEC: the ``os.execv`` call below is a deliberate isolation
    boundary and must NEVER be collapsed to an in-process import/call, on
    ANY platform. On POSIX, ``os.execv`` is true process REPLACEMENT — the
    current process image is overwritten by the resolved binary in place,
    so there is no parent process image left for control to return to.
    On Windows, CPython implements ``os.execv`` over ``CreateProcess``: a
    new child process is created and the calling process then exits, so a
    parent briefly exists rather than being replaced in-place — this is a
    spawn-then-exit, not an in-place replacement, on that platform. The
    isolation-boundary rationale this call exists for holds on both
    platforms regardless of that mechanism difference: the resolved binary
    always runs as a genuinely separate process, never folded into this
    one's own import graph. Reason recorded in
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md
    (that audit does not itself repeat the POSIX-only "process replacement"
    framing corrected here).
    """
    legacy = os.path.join(str(home_dir()), ".claude", "bin", name)
    settings_home_candidate = os.path.join(str(settings_home()), "bin", name)
    if is_executable(settings_home_candidate):
        real = settings_home_candidate
    else:
        real = legacy
    if not is_executable(real):
        print(
            f"{name}: resolver not installed at {real} — "
            "run /coordinator:setup (Phase 3).",
            file=sys.stderr,
        )
        sys.exit(127)
    if argv is None:
        argv = sys.argv[1:]
    os.execv(real, [real, *argv])

# -*- coding: utf-8 -*-
"""The guard over `prose_transport_probe.py` (C2).

Spec backlink: docs/plans/2026-08-31-prose-flags-travel-as-files-through-the.md § C2

RULING (C12, 2026-08-31). The probe (`prose_transport_probe.py`, landed
verbatim in its recorded form during plan review) measured 13 uncovered
(entrypoint, flag) pairs at `baseline_ref a7eb44bf99` -- every declared
prose-bearing flag on a `.cmd`-forwarded `coordinator/bin` entrypoint that
lacks an unconditional, flag-scoped newline refusal (Rule 1). C2-C10 burned
that worklist to zero; this module asserts the probe reports ZERO such
pairs on the fast tier, unconditionally, with no `designed_red` marker. A
future prose-bearing flag added to a `.cmd`-forwarded entrypoint without a
newline refusal (or a `--<flag>-file` remedy routed through
`refuse_newline_argv`) turns this RED on the fast tier -- that is the
guard doing its job, not a regression to wave back in with a new marker.

NEGATIVE SPEC
    - No exemption list, allowlist, or per-pair skip anywhere in this
      module. An allowlist of "known uncovered" rows is the exact shape
      this baton has convicted three times (a set member whose file is
      gone, a parity exemption outliving its scope call, a stale wsc-tail
      allowlist row) -- an entry outlives its reason and the guard
      certifies a defect instead of catching one. The ONLY sanctioned
      not-yet-green state was the `designed_red` marker itself; C12 removed
      it once the probe reported zero, and no successor marker replaces it.
    - Does not re-author or re-derive `prose_transport_probe.py`'s scoring
      predicates (`declared`, `flag_refused`, `flag_legged`) -- invokes the
      probe as the script it is written to be (its own docstring names
      `python coordinator/bin/tests/prose_transport_probe.py [repo_root]`
      as the run form; the module executes `scan()` at import time rather
      than behind `if __name__ == "__main__"`, so a plain `import` re-runs
      it against the wrong cwd -- a subprocess is the only faithful
      invocation). The recorded baseline stays meaningful against the same
      instrument that produced it either way.
    - Does not assert on `UNLEGGED` (Rule 2) lines -- the probe's own
      docstring states Rule 2 is informational only, not yet reduced to a
      checkable predicate. Gating on it here would gate on an unnamed
      foreclosure.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

REPO_ROOT = Path(__file__).resolve().parents[3]
PROBE_PATH = Path(__file__).resolve().parent / "prose_transport_probe.py"


@pytest.mark.spawns_process
def test_no_uncovered_prose_flag_transport_pairs():
    """Rule 1 gate: the probe must report zero UNREFUSED (entrypoint, flag)
    pairs among declared prose-bearing flags on `.cmd`-forwarded
    `coordinator/bin` entrypoints.

    Was RED on landing (13 uncovered pairs at baseline_ref a7eb44bf99). C12
    confirmed the worklist reached zero and removed the `designed_red`
    marker; this assertion itself never changed shape and now binds on the
    fast tier.
    """
    result = subprocess.run(
        [sys.executable, str(PROBE_PATH), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    unrefused_lines = [
        line for line in result.stdout.splitlines() if line.startswith("UNREFUSED ")
    ]
    assert unrefused_lines == [], (
        "%d prose-bearing flag(s) on a .cmd-forwarded coordinator/bin "
        "entrypoint lack an unconditional, flag-scoped newline refusal "
        "(Rule 1):\n%s\n\nfull probe output:\n%s"
        % (len(unrefused_lines), "\n".join(unrefused_lines), result.stdout)
    )

"""coordinator_core.hooks.guard_hook_generation_self_probe — SessionStart
(startup|clear|compact) op: wires the hook-generation kill-switch self-probe
into boot.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-hook-generation-self-probe.py`
— a DR-047 PLUMBING stub over
`coordinator_core.ops.session.guard_hook_generation_self_probe.run_self_probe`,
which already lives in THIS repo. DoE's own version resolved a sibling claude-klabauter
checkout, placed it on `sys.path`, and called the function across that
boundary; that whole cross-repo resolution step is dropped here — this op is
a same-repo sibling import, matching `preuse_write_dispatch.py`'s own W4-C7
precedent.

`run_self_probe`'s own module docstring documents it as cheap-by-construction
(a handful of stat/read calls, no subprocess) specifically because it fires
every boot; this handler still bounds the call with a `ThreadPoolExecutor`
soft timeout rather than trusting that contract unconditionally — same
belt-and-suspenders posture as the source script, ported unchanged (including
the `shutdown(wait=False)` + `os._exit(0)` escape on timeout: Python cannot
hard-kill a thread, and both `with ThreadPoolExecutor(...)` and a plain
`return`/`sys.exit` after a bare `shutdown()` still join the abandoned worker
at interpreter shutdown, wedging this SessionStart boot).

Op contract: `params` is the flat SessionStart payload dict; none of its
fields are read (mirrors the source script, which drains stdin without
parsing it). Returns `context_only("SessionStart", text)` when
`run_self_probe` returns non-empty text, `no_advisory()` otherwise or on any
failure (config dir unresolvable, the probe raising, a malformed return type,
or a timeout — every path is fail-open, matching the source script's own
unconditional exit-0 contract).

Negative-spec:
    Does NOT resolve a sibling engine checkout, place anything on `sys.path`,
    or write a `housekeeping-failures.log` fallback record for an unresolved
    engine root — there is no cross-repo resolution step left to fail; the
    only failure classes left are the ones a same-process call can hit
    (unresolvable home dir, the probe raising, a timeout), each of which
    degrades to `no_advisory()` silently rather than a best-effort log write.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from pathlib import Path

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

_SELF_PROBE_TIMEOUT_SECS = 5


@register_op("hooks.guard_hook_generation_self_probe")
async def _handler(params: dict, repo_root=None) -> dict:
    try:
        from coordinator_core.ops.session.guard_hook_generation_self_probe import (
            run_self_probe,
        )
    except Exception:
        return no_advisory()

    try:
        home = str(Path.home())
    except RuntimeError:
        return no_advisory()
    config_dir_raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
    config_dir = Path(config_dir_raw)

    # See module docstring: `os._exit(0)` is the only portable way to abandon
    # a hung worker without wedging this synchronous SessionStart hook.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(run_self_probe, config_dir)
        try:
            text = future.result(timeout=_SELF_PROBE_TIMEOUT_SECS)
        except _FutureTimeoutError:
            executor.shutdown(wait=False)
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)  # fail-open -- never block SessionStart on a hung probe
        executor.shutdown(wait=False)
    except Exception:
        executor.shutdown(wait=False)
        return no_advisory()

    if not isinstance(text, str) or not text:
        return no_advisory()
    return context_only("SessionStart", text)

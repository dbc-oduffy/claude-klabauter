# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""wsc-close.py — TEMPORARY forwarder to `archive-session-scope.py`.

This file used to BE the archival CLI. It was renamed on 2026-08-30 because
the WSC name was false: nothing in `/workstream-complete` calls it and its
only job is a SessionEnd claim-dir archive. See
`docs/install/relocation-ledger.json` for the recorded relocation.

Why a forwarder rather than a clean rename: the sole caller is DoE-claude's
`coordinator/hooks/scripts/sessionend-archive-session.py::_archive`, which
hardcodes this path and, at the time of the rename, degraded to a silent
no-op when the path was absent. Between the rename landing here and that
hook being repointed, a hard rename would stop every session on the host
archiving its claim directory with nothing erroring — the 24h reap in
`ops/session/reap.py` being the only remaining backstop.

NEGATIVE SPEC — this file is scheduled for deletion, not for maintenance.
Do not add behavior to it, do not import it, and do not cite it as the
archival CLI. Step 3 of the agreed sequence (memo
`cross-repo/inbox/2026-08-30-doe-claude-em-rename-wsc-close-yes-and-our-hook-now-fails-loud.md`)
deletes it, together with `wsc-close.cmd`, once DoE-claude confirms
`_archive` resolves `archive-session-scope.py`.

argv is forwarded verbatim; the exit code is the target's own.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    target = Path(__file__).resolve().parent / "archive-session-scope.py"
    spec = importlib.util.spec_from_file_location("archive_session_scope", target)
    if spec is None or spec.loader is None:
        print(
            f"wsc-close.py: forwarding target not loadable at {target}",
            file=sys.stderr,
        )
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())

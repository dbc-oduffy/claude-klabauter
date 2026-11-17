# normalize-env.py — CLI trampoline over claude-klabauter coordinator_core.ops.normalize_env.
#
# Idempotent environment-normalization for coordinator install Step Zero: the
# sole writer of environment state during coordinator setup. Windows-first
# mutations (git core.longpaths, uv install, Python 3.12 via winget, WindowsApps
# App Execution alias disable); macOS/Linux prints offers only, with the single
# exception of the consent-gated Darwin ~/.bash_profile login-shell reconstruction
# (C2). Full logic (1075-line bash oracle) ported to
# coordinator_core/ops/normalize_env.py — see that module's docstring for the
# probe-delegation design.
#
# Shebang note: the SHEBANG line above is `python3`, not `python` — this is the
# pure-Python-with-`.cmd` shape (a `normalize-env.cmd` sibling exists), not the
# legacy `#!/bin/sh` polyglot shape the "Windows exception" below is scoped to.
# `python3` is the PEP-394-portable token on POSIX (bare `python` often doesn't
# exist on modern Debian/Ubuntu/Fedora) and is safe here because Windows
# invocation is carried by the `.cmd` sibling, which resolves its own
# interpreter and never reads this shebang. A bareword `python3` TYPED BY A
# USER on a clean Windows install can still hit the Store App-Execution-Alias
# stub — that's what the `.cmd` and INSTALL.md's Windows note exist for.
# coordinator-content-repo coordinator/docs/wiki/portability/bash-on-windows-gotchas.md § pure-Python-with-`.cmd` carve-out.
#
# Bootstrap-trap note (RETIRED for this file specifically): the bash oracle had
# to parse under bash 3.2 (stock macOS) because it ran as bash source. This
# trampoline runs as Python instead — the bash-3.2 floor no longer applies to
# it. The former bash-source BRIDGE (`bash -c 'source scripts/lib/prereq_probe.sh'`)
# was retired on the 2026-07-21 de-bash cutover: probe logic now runs in-process
# via the native `coordinator_core.install.prereq_probe` port (see that module's
# docstring). `scripts/lib/prereq_probe.sh` itself was DELETED on 2026-07-22
# (PM override of the prior DR-079 keep-as-vendor-SSOT ruling — see that DR's
# amendment) despite example-retrieval-repo-ue-addon and deep-research vendoring it
# byte-stable; those repos' parity gates are expected to go red until they
# re-vendor against the Python port (memo owed, see cross-repo/archive).
#
# Negative-spec (2026-08-14): this trampoline used to call
# `_forward_prereq_probe_path()`, which set NORMALIZE_ENV_PREREQ_PROBE_PATH to
# that deleted file. The consumer did NOT ignore the var — it gated on the
# path being a real file and returned EXIT_HARD_ERROR, so every documented
# invocation of this script exited 1. Do not reintroduce any forwarding of a
# probe-library path: the probes are in-process Python and need no path.
#
# Usage:
#   normalize-env [--dry-run] [--yes] [--restore <backup-file>]
#
# Exit codes (business — identical to the retired bash oracle, byte-for-byte):
#   0  success / no-op / all mutations applied / nothing to fix
#   1  hard error (backup-file write failure)
#   2  CLI usage error (unknown flag; --restore missing arg; --restore file not found)
#   3  non-interactive consent-gate abort (EOF/no-tty without --yes) — ZERO mutations
#   4  partial failure (one or more mutation steps failed)
# Exit code 5 is RESERVED here for a claude-klabauter transport failure (CLAUDE_KLABAUTER_ROOT
# unresolvable / coordinator_core.ops.normalize_env not importable) — this is
# a Family I (fresh-install) surface: on a cold machine CLAUDE_KLABAUTER_ROOT may not yet
# be resolvable, and 5 lets a caller distinguish "claude-klabauter link is down" from any
# of the five business codes above (porter-brief rule A3b — never collide a
# transport-failure code with a business code, and never silently exit 0 on a
# gate/validator script that a caller relies on for pass/fail).
#
# Spec backlink: docs/plans/2026-06-22-coordinator-env-normalization-step-zero.md (FB-3)
# Port of: coordinator/scripts/normalize-env.sh (retired on cutover; see git log)
# Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md § BIG_PORT Wave C

import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(os.path.dirname(_SCRIPT_DIR), "bin", "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from cc_invoke import require_dispatch_engine_on_path  # noqa: E402

_EXIT_TRANSPORT_FAILURE = 5


def _import_main():
    """Resolve CLAUDE_KLABAUTER_ROOT, put it on sys.path, and import the ported CLI entry.

    Reuses cc_invoke's battle-tested CLAUDE_KLABAUTER_ROOT resolution ladder (env var ->
    settings-home pointer file -> coordinator-claude-klabauter-root.sh) rather than
    re-deriving it — this is a plain in-process import, not an RPC invoke, so
    cc_invoke's subprocess-spawn transport (cc_invoke()/route()) is
    deliberately NOT used here (template-variant #1 — direct-import trampoline,
    per r1-doe-port-template.md § 1).
    """
    claude_klabauter_root = require_dispatch_engine_on_path()
    from coordinator_core.ops.normalize_env import main as _op_main

    return _op_main


def main() -> None:
    try:
        op_main = _import_main()
    except RuntimeError as exc:
        print(f"normalize-env.py: CLAUDE_KLABAUTER_ROOT resolution failed: {exc}", file=sys.stderr)
        print(
            "  This is a known Family I (fresh-install) gap: on a cold machine "
            "COORDINATOR_ENGINE_ROOT may not yet be resolvable. Run coordinator:install first, "
            "or set COORDINATOR_ENGINE_ROOT explicitly.",
            file=sys.stderr,
        )
        sys.exit(_EXIT_TRANSPORT_FAILURE)
    except ImportError as exc:
        print(
            f"normalize-env.py: coordinator_core.ops.normalize_env not importable: {exc}",
            file=sys.stderr,
        )
        sys.exit(_EXIT_TRANSPORT_FAILURE)

    sys.exit(op_main(sys.argv[1:]))


if __name__ == "__main__":
    main()

"""
coordinator_core.ops.gen_doe_root_pointer — Port of: gen-doe-root-pointer.sh
(DoE b5a4192c, 2026-07-20).

Purpose: reads `repos.doe_claude` (env override first, then the `machine-local` registry)
and writes `<settings-home>/machine-local/.doe-root` (one line, the DoE repo root, no
trailing junk) so cold-terminal consumers (the `claude()` shim, inline resolver
fallbacks) can `cat` the pointer with zero tool dependency.

Write target relocated 2026-07-28 from the legacy `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME}/.claude}/.doe-root`.
`~/.claude` is a git working tree synced between machines, and `.doe-root` was TRACKED
in it — so this generator was committing a machine-specific absolute path that then
overwrote the other machine's value (Windows clobbering macOS and back). The settings-home
`machine-local/` plane never syncs, and was already rung 2 of every reader, so the write
seam now agrees with the read seam. See `_pointer_file()` for the no-dual-write rationale.

Spec backlink: DoE-claude:pln-coordinator-maximalist-install-e73afa § C1
Design: docs/plans/2026-07-04-coordinator-maximalist-install-shape.md § Design decisions
        (pointer is a projected cache; registry = source of truth; dry-run lesson cited)

Resolution order for the DoE clone root (unchanged from the bash oracle):
  1. REPO_DOE_CLAUDE env var (operator override, also used by install sandbox tests)
  2. `machine-local get repos.doe_claude`  (registry — primary)
  3. fail-loud with remediation

Recast (thin caller): docs/plans/2026-07-09-resolver-unification-v3split-01.md § C3 —
this module does NOT delegate to coordinator/lib/resolve-coordinator-clone.py's
--clone-root verb (unlike claude-doe). --clone-root's rung 3 is the .doe-root
pointer file THIS module writes — falling through to it here would read back a value
this module is in the middle of refreshing, risking a stale read on the write path.

Idempotency: if the live pointer already contains the correct value, the file is not
rewritten (avoids spurious mtime churn and concurrent-write races in steady state).

Dry-run safety: `--check-only` writes to a temp path, validates the content, then
discards. The live pointer is byte-unchanged after any `--check-only` run.

Fail-loud contract: if repos.doe_claude is unset/empty in both the env override and the
registry, `main()` returns non-zero with a stderr message and a remediation hint.

Negative-spec:
    - Does NOT clone the DoE repo, does NOT edit any registry key, does NOT write any
      file other than the live pointer (and a temp file discarded in --check-only mode).
    - Does NOT reimplement the machine-local registry.toml/registry.local.toml parser —
      shells out to the `machine-local` CLI (PATH-resolved) exactly like the bash oracle's
      Tier 2, so the registry-merge logic has exactly one implementation. The bash oracle
      ALSO tried a script-directory-relative sibling before falling back to PATH (an
      install-bootstrap optimization for when PATH isn't wired up yet); this module has no
      equivalent "co-located bin dir" concept once ported into the claude-klabauter engine tree, so
      it checks PATH only. Observable behavior (the resolved value, or the fail-loud path)
      is identical either way -- the sibling check was a resolution-speed optimization,
      never a distinct semantic branch.
    - The bash >= 4 version guard from the oracle (DR-148 defense-in-depth) has no meaning
      here -- this is a pure-Python module, not a bash script. Omitted intentionally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

from coordinator_core._settings_home import machine_local_dir, native_path_form
from coordinator_core.machine_resolver import registry_get as _registry_get
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

GENERATES = []  # writes only <settings-home>/machine-local/.doe-root, outside any git tree

_PROG = "gen-doe-root-pointer.sh"  # literal program-name prefix, matches the DoE filename


def _resolve_machine_local() -> Optional[str]:
    """Locate the `machine-local` CLI on PATH. Returns None if absent."""
    return shutil.which("machine-local")


def _resolve_doe_root() -> "tuple[Optional[str], int]":
    """Resolve the DoE clone root. Returns (root_or_None, exit_code_on_failure).

    On success returns (root, 0). On failure returns (None, 1) after printing a
    stderr diagnostic + remediation hint, mirroring the bash oracle's two-tier
    resolution (env override, then machine-local registry).
    """
    # Tier 1: explicit env override (install sandbox tests / operator pin).
    # Normalized like tier 2: an override exported by a Git-Bash caller carries
    # the MSYS mount form (`/x/DoE-claude`), and the pointer this writes is read
    # almost exclusively by native-Windows node / py.exe processes, which resolve
    # a leading `/x/` as drive-relative `X:\x\...`. Tier 2 is normalized upstream
    # by `machine-local get`'s own repos.* branch; without this the two tiers
    # would disagree on path form for the same clone. No-op off Windows.
    env_override = os.environ.get("REPO_DOE_CLAUDE", "")
    if env_override:
        return native_path_form(env_override), 0

    # Tier 2: machine-local registry. Tries `registry_get` first, in-process
    # and zero-spawn (see `coordinator_core.machine_resolver.registry_get`),
    # then falls back to the `machine-local get repos.doe_claude` CLI on a
    # miss -- `registry_get` alone doesn't reach the CLI's autodiscovery/
    # `path-exceptions.toml` rungs, and this repo's own `.coordinator-dev-repo`
    # marker proves autodiscovery is live for repos.doe_claude on a real
    # machine (2026-08-16 review finding). The fallback also restores this
    # module's own negative-spec above ("shells out to the `machine-local`
    # CLI... so the registry-merge logic has exactly one implementation"),
    # which the zero-spawn-only conversion had silently broken.
    resolved_raw = _registry_get("repos.doe_claude") or ""
    if resolved_raw:
        return native_path_form(resolved_raw), 0

    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print(
            f"{_PROG}: machine-local not found — cannot read registry",
            file=sys.stderr,
        )
        print(
            "  Remediation: run /coordinator:install (Phase 3) to install machine-local,\n"
            "  or set REPO_DOE_CLAUDE=<path> to bypass the registry lookup.",
            file=sys.stderr,
        )
        return None, 1

    try:
        result = subprocess.run(
            [ml_bin, "get", "repos.doe_claude"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        result = None

    if result is None or result.returncode != 0:
        print(f"{_PROG}: machine-local get repos.doe_claude failed", file=sys.stderr)
        print(
            "  Remediation: machine-local set repos.doe_claude <path>  then /coordinator:install",
            file=sys.stderr,
        )
        return None, 1

    resolved = native_path_form(result.stdout.strip())
    if not resolved:
        print(
            f"{_PROG}: repos.doe_claude is unset in the registry",
            file=sys.stderr,
        )
        print(
            "  Remediation: machine-local set repos.doe_claude <path>  then /coordinator:install",
            file=sys.stderr,
        )
        return None, 1

    return resolved, 0


def _pointer_file() -> str:
    """Compute the target pointer path — `<settings-home>/machine-local/.doe-root`.

    Precedence is `machine_local_dir()`'s: $COORDINATOR_SETTINGS_HOME wins over
    ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings.

    This target is machine-local by construction, which is the whole point: the
    pointer's value is a machine-specific absolute path (`/Users/…` on macOS,
    `C:\\…` on Windows), so it MUST NOT live anywhere that syncs between
    machines. The prior target — `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME}/.claude}/.doe-root`
    — was exactly such a place: `~/.claude` is a git working tree that is
    committed and pushed across machines, and `.doe-root` was a TRACKED file in
    it, so a Windows-written pointer would land on macOS (and back) and resolve
    the DoE clone to a path that does not exist on the reading machine.

    This is a move, not a new rung: `_doe_root`'s resolution order already ranked
    `<settings-home>/machine-local/.doe-root` as rung 2 (durable mirror) above
    `~/.claude/.doe-root` at rung 3, where it is labelled the LEGACY fallback — see
    `coordinator_core.doe_root_pointer`. The generator was simply still writing the
    legacy location; relocating the write makes the write and read seams agree.
    Rung 3 stays readable so a machine installed before this change keeps
    resolving until it re-runs install.

    Deliberately writes the durable target ONLY — no dual-write to the legacy
    `~/.claude/.doe-root`. A dual-write would preserve the cross-machine clobber
    this relocation exists to remove, since the legacy copy is the synced one.
    """
    return str(machine_local_dir() / ".doe-root")


def _seed_plugin_mirror_source_path(doe_root: str) -> None:
    """Dual-seed ``plugin.mirrors.coordinator-claude.source_path`` from the same
    resolved ``doe_root`` value the pointer file was just written from — the two
    must never drift apart (see module docstring's callers / install.md step
    3.5a.1). Absent-only, idempotent; prints its own ``plugin_mirror_source_path:``
    contract row. Silent-skip (with a row) if ``machine-local`` is not on PATH —
    this is a best-effort convenience seed, not a hard requirement of writing the
    pointer itself.
    """
    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print("plugin_mirror_source_path: skipped (machine-local not found)")
        return
    # Read tier: in-process (zero-spawn -- see
    # `coordinator_core.machine_resolver.registry_get`) rather than shelling
    # out to `machine-local get`. The write tier below still shells out --
    # no in-process write substitute exists.
    if _registry_get("plugin.mirrors.coordinator-claude.source_path"):
        print("plugin_mirror_source_path: ready (no-op)")
        return
    try:
        from coordinator_core.win_portability import no_console_creationflags

        subprocess.run(
            [ml_bin, "set", "plugin.mirrors.coordinator-claude.source_path", doe_root],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        print("plugin_mirror_source_path: skipped (machine-local not found)")
        return
    print(f"plugin_mirror_source_path: written ({doe_root})")


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, resolve, validate, write (or --check-only dry-run).

    Emits a stdout ``doe_root_pointer: <status>`` contract row on every exit path
    (Phase 7 install-status-table row) — folds what used to be an install.md-side
    if/echo wrapper directly into this CLI so the doc call collapses to one line
    (docs/plans/2026-07-23-skills-carry-no-code-extirpation.md § M3/D9).

    Unrecognized argv tokens are silently ignored (matches the pass-through-
    tolerant convention already established by the sibling install op
    ``register_coordinator_mirror`` — a caller forwarding a blob of unrelated
    install flags, e.g. ``--non-interactive``/``--reconfigure``, must not fail
    this generator). This is a deliberate loosening from the prior strict
    "unknown argument" fail-loud contract, made specifically to support
    ``${ARGUMENTS}``-blob passthrough from install.md's single-line call sites.
    """
    check_only = False
    graceful_skip_unresolved = False
    for arg in argv:
        if arg == "--check-only":
            check_only = True
        elif arg == "--graceful-skip-unresolved":
            graceful_skip_unresolved = True
        elif arg in ("--help", "-h"):
            prog = os.path.basename(sys.argv[0]) if sys.argv else _PROG
            print(f"Usage: {prog} [--check-only] [--graceful-skip-unresolved]", file=sys.stderr)
            print("  (no flag)                   Write or refresh <settings-home>/machine-local/.doe-root from the registry.", file=sys.stderr)
            print("  --check-only                Validate without mutating the live pointer (dry-run-safe).", file=sys.stderr)
            print("  --graceful-skip-unresolved  Exit 0 with a 'skipped' row (not fail-loud) when", file=sys.stderr)
            print("                              repos.doe_claude cannot be resolved on the live path.", file=sys.stderr)
            return 0
        # else: silently ignored -- see docstring.

    doe_root, rc = _resolve_doe_root()
    if doe_root is None:
        if graceful_skip_unresolved and not check_only:
            print("doe_root_pointer: skipped (repos.doe_claude not resolved — complete step 3.5a first)")
            return 0
        return rc

    # Strip any trailing slash for a canonical single-line value.
    doe_root = doe_root.rstrip("/")

    if not os.path.isdir(doe_root):
        print(f'{_PROG}: resolved root not found at "{doe_root}"', file=sys.stderr)
        print(
            "  Remediation: confirm repos.doe_claude in the registry is a valid directory,\n"
            "  or set REPO_DOE_CLAUDE=<path>  then /coordinator:install",
            file=sys.stderr,
        )
        print("doe_root_pointer: failed (see stderr for gen-doe-root-pointer.py output)")
        return 1

    if not os.path.isdir(os.path.join(doe_root, "coordinator")):
        print(
            f'{_PROG}: coordinator/ subdir absent at "{doe_root}/coordinator"',
            file=sys.stderr,
        )
        print(
            "  Remediation: confirm the resolved repos.doe_claude root has coordinator/ populated (W4.2 cutover required).",
            file=sys.stderr,
        )
        print("doe_root_pointer: failed (see stderr for gen-doe-root-pointer.py output)")
        return 1

    pointer_file = _pointer_file()

    if check_only:
        if os.path.isfile(pointer_file):
            try:
                with open(pointer_file, encoding="utf-8") as fh:
                    existing = fh.read().rstrip("\n")
            except OSError:
                existing = ""
            if existing == doe_root:
                print(f"doe_root_pointer: check: {pointer_file} up to date (no-op)")
                return 0

        # Review: code-reviewer (2026-07-17 Finding 1, ported from gen_claude_doe_launcher.py:247-249)
        # — hardcoded POSIX "/tmp" fallback crashed --check-only on real Windows (no TMPDIR there).
        # tempfile.gettempdir() resolves TMPDIR/TEMP/TMP per-platform.
        fd, tmp_path = tempfile.mkstemp(prefix="gen-doe-root-pointer.", dir=tempfile.gettempdir())
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(doe_root + "\n")

            with open(tmp_path, encoding="utf-8") as fh:
                written = fh.read().rstrip("\n")

            if written != doe_root:
                print(
                    f'{_PROG}: --check-only validation failed (wrote "{written}", '
                    f'expected "{doe_root}")',
                    file=sys.stderr,
                )
                print("doe_root_pointer: failed in check-only validation (see stderr)")
                return 1

            print(
                f'{_PROG}: --check-only FAILED — {pointer_file} is stale or absent, '
                f'would write "{doe_root}" (not written)',
                file=sys.stderr,
            )
            print(f"doe_root_pointer: check failed: {pointer_file} is stale or absent (would write)")
            return 1
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                print(f"skip: main: os.remove(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass

    # Live write path: idempotent -- skip if the pointer already holds the correct value.
    if os.path.isfile(pointer_file):
        try:
            with open(pointer_file, encoding="utf-8") as fh:
                existing = fh.read().rstrip("\n")
        except OSError:
            existing = ""
        if existing == doe_root:
            # Already correct -- no-op (no mtime churn, no race).
            print("doe_root_pointer: ready (no-op)")
            _seed_plugin_mirror_source_path(doe_root)
            return 0

    pointer_dir = os.path.dirname(pointer_file)
    if pointer_dir and not os.path.isdir(pointer_dir):
        os.makedirs(pointer_dir, exist_ok=True)

    # Atomic write: write to a sibling temp file then rename into place.
    fd, tmp_live = tempfile.mkstemp(prefix=".doe-root.tmp.", dir=pointer_dir or ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(doe_root + "\n")
        os.replace(tmp_live, pointer_file)
        # DR-276: declared AFTER the write lands, at the FINAL destination,
        # never the discarded tmp_live.
        declare_write(pointer_file)
    except OSError:
        try:
            os.remove(tmp_live)
        except OSError:
            print(f"skip: main: os.remove(tmp_live) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        print("doe_root_pointer: failed (see stderr for gen-doe-root-pointer.py output)")
        raise

    print(f'{_PROG}: wrote "{doe_root}" to {pointer_file}', file=sys.stderr)
    print(f"doe_root_pointer: written ({pointer_file})")
    _seed_plugin_mirror_source_path(doe_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

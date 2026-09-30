"""
coordinator_core.ops.gen_content_root_pointer — Port of: gen-content-root-pointer.sh
(DoE b5a4192c, 2026-07-20).

Purpose: reads `repos.content_root` (env override first, then the `machine-local` registry)
and writes `<settings-home>/machine-local/.coordinator-content-root` (one line, the DoE repo root, no
trailing junk) so cold-terminal consumers (the `claude()` shim, inline resolver
fallbacks) can `cat` the pointer with zero tool dependency.

Write target relocated 2026-07-28 from the legacy `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME}/.claude}/.coordinator-content-root`.
`~/.claude` is a git working tree synced between machines, and `.coordinator-content-root` was TRACKED
in it — so this generator was committing a machine-specific absolute path that then
overwrote the other machine's value (Windows clobbering macOS and back). The settings-home
`machine-local/` plane never syncs, and was already rung 2 of every reader, so the write
seam now agrees with the read seam. See `_pointer_file()` for the no-dual-write rationale.

Spec backlink: coordinator-content-repo:pln-coordinator-maximalist-install-e73afa § C1
Design: docs/plans/2026-07-04-coordinator-maximalist-install-shape.md § Design decisions
        (pointer is a projected cache; registry = source of truth; dry-run lesson cited)

Resolution order for the DoE clone root (unchanged from the bash oracle):
  1. REPO_CONTENT_ROOT env var (operator override, also used by install sandbox tests)
  2. `machine-local get repos.content_root`  (registry — primary)
  3. fail-loud with remediation

Recast (thin caller): docs/plans/2026-07-09-resolver-unification-v3split-01.md § C3 —
this module does NOT delegate to coordinator/lib/resolve-coordinator-clone.py's
--clone-root verb (unlike claude-author). --clone-root's rung 3 is the .coordinator-content-root
pointer file THIS module writes — falling through to it here would read back a value
this module is in the middle of refreshing, risking a stale read on the write path.

Idempotency: if the live pointer already contains the correct value, the file is not
rewritten (avoids spurious mtime churn and concurrent-write races in steady state).

Dry-run safety: `--check-only` writes to a temp path, validates the content, then
discards. The live pointer is byte-unchanged after any `--check-only` run.

Fail-loud contract: if repos.content_root is unset/empty in both the env override and the
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

from coordinator_core._settings_home import (
    machine_local_dir,
    native_path_form,
    resolve_machine_local_cli,
)
from coordinator_core.data_root import content_root_for
from coordinator_core.machine_resolver import registry_get as _registry_get
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

GENERATES = []

_PROG = "gen-content-root-pointer.py"

_MUTATION_DISABLE_ENV = "COORDINATOR_DISABLE_MACHINE_MUTATION"

_LIVE_WRITE_ALLOW_ENV = "COORDINATOR_ALLOW_LIVE_CONTENT_ROOT_WRITE"


def _resolve_machine_local() -> Optional[str]:
    return resolve_machine_local_cli()


def _resolve_content_root() -> "tuple[Optional[str], int]":
    env_override = os.environ.get("REPO_CONTENT_ROOT", "")
    if env_override:
        return native_path_form(env_override), 0

    resolved_raw = _registry_get("repos.content_root") or ""
    if resolved_raw:
        return native_path_form(resolved_raw), 0

    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print(
            f"{_PROG}: machine-local not found — cannot read registry",
            file=sys.stderr,
        )
        print(
            "  Remediation: python3 <claude-klabauter>/scripts/setup.py  (installs machine-local),\n"
            "  or set REPO_CONTENT_ROOT=<path> to bypass the registry lookup.",
            file=sys.stderr,
        )
        return None, 1

    try:
        result = subprocess.run(
            [ml_bin, "get", "repos.content_root"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        result = None

    if result is None or result.returncode != 0:
        print(f"{_PROG}: machine-local get repos.content_root failed", file=sys.stderr)
        print(
            "  Remediation: machine-local set repos.content_root <path>\n"
            "  Then: python3 <claude-klabauter>/scripts/setup.py",
            file=sys.stderr,
        )
        return None, 1

    resolved = native_path_form(result.stdout.strip())
    if not resolved:
        print(
            f"{_PROG}: repos.content_root is unset in the registry",
            file=sys.stderr,
        )
        print(
            "  Remediation: machine-local set repos.content_root <path>\n"
            "  Then: python3 <claude-klabauter>/scripts/setup.py",
            file=sys.stderr,
        )
        return None, 1

    return resolved, 0


def _pointer_file() -> str:
    """Compute the target pointer path — `<settings-home>/machine-local/.coordinator-content-root`.

    Precedence is `machine_local_dir()`'s: $COORDINATOR_SETTINGS_HOME wins over
    ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings.

    This target is machine-local by construction, which is the whole point: the
    pointer's value is a machine-specific absolute path (`/Users/…` on macOS,
    `C:\\…` on Windows), so it MUST NOT live anywhere that syncs between
    machines. The prior target — `${CLAUDE_CONFIG_DIR:-${CLAUDE_HOME:-$HOME}/.claude}/.coordinator-content-root`
    — was exactly such a place: `~/.claude` is a git working tree that is
    committed and pushed across machines, and `.coordinator-content-root` was a TRACKED file in
    it, so a Windows-written pointer would land on macOS (and back) and resolve
    the DoE clone to a path that does not exist on the reading machine.

    This is a move, not a new rung: `_content_root`'s resolution order already ranked
    `<settings-home>/machine-local/.coordinator-content-root` as rung 2 (durable mirror) above
    `~/.claude/.coordinator-content-root` at rung 3, where it is labelled the LEGACY fallback — see
    `coordinator_core.content_root_pointer`. The generator was simply still writing the
    legacy location; relocating the write makes the write and read seams agree.
    Rung 3 stays readable so a machine installed before this change keeps
    resolving until it re-runs install.

    Deliberately writes the durable target ONLY — no dual-write to the legacy
    `~/.claude/.coordinator-content-root`. A dual-write would preserve the cross-machine clobber
    this relocation exists to remove, since the legacy copy is the synced one.
    """
    return str(machine_local_dir() / ".coordinator-content-root")


_LEGACY_POINTER_NAME = ".coordinator-content-root"  # private-name-ok: compat twin, readers still resolve it


def _pointer_files() -> List[str]:
    """The pointer plus its legacy-named twin, same value, deduplicated."""
    primary = _pointer_file()
    twin = str(machine_local_dir() / _LEGACY_POINTER_NAME)
    return list(dict.fromkeys([primary, twin]))


def _pointer_current(files: List[str], value: str) -> bool:
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read().rstrip("\n") != value:
                    return False
        except OSError:
            return False
    return True


def _seed_plugin_mirror_source_path(content_root: str) -> None:
    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print("plugin_mirror_source_path: skipped (machine-local not found)")
        return
    if _registry_get("plugin.mirrors.coordinator-claude.source_path"):
        print("plugin_mirror_source_path: ready (no-op)")
        return
    try:
        from coordinator_core.win_portability import no_console_creationflags

        subprocess.run(
            [ml_bin, "set", "plugin.mirrors.coordinator-claude.source_path", content_root],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        print("plugin_mirror_source_path: skipped (machine-local not found)")
        return
    print(f"plugin_mirror_source_path: written ({content_root})")


def _refuse_live_pointer_write(pointer_file: str) -> Optional[str]:
    """Return a reason to refuse writing ``pointer_file``, or None to proceed.

    Resolution order, and it matters:

    0. ``COORDINATOR_ALLOW_LIVE_CONTENT_ROOT_WRITE=1`` — the named opt-in, first.

    1. UNDER PYTEST with a temp-rooted target — allowed. This is the sandbox-
       redirected shape and it must keep working: the suite-root quarantine sets
       the kill switch below for every test, so honouring that switch here
       unconditionally refuses ~18 legitimate tmp-scoped tests of this writer.
       The exemption is scoped to pytest DELIBERATELY (see the divergence note).

    2. ``COORDINATOR_DISABLE_MACHINE_MUTATION=1`` — the operator-facing switch.
       Now reached regardless of the target path for any non-pytest caller, which
       is what ``install.substrate``'s module docstring promises of this switch
       ("regardless of the path involved").

    3. UNDER PYTEST with a target outside the temp dir — refused. No test has
       business writing the operator's real pointer. This does not depend on the
       quarantine fixture having run, which is the point: that fixture returns
       EARLY for ``@pytest.mark.real_home`` — before it sets the switch — so a
       marked test gets the real home AND no switch, though the marker is
       documented as being for read-only oracles.

    DIVERGENCE FROM ``substrate._refuse_machine_mutation``, stated because an
    earlier version of this docstring claimed to mirror it and did not: substrate
    checks the kill switch FIRST, unconditionally, and only then considers the
    temp path. Here the temp-rooted PYTEST case is exempted ahead of the switch,
    because substrate's guarded call sites are mutations the sandbox CANNOT
    redirect (a Windows registry write, an AppX delete) whereas this one it CAN —
    ``_pointer_file()`` follows ``COORDINATOR_SETTINGS_HOME``/``HOME``, so a
    sandboxed test genuinely writes inside its own tmpdir. Scoping the exemption
    to pytest keeps the switch unconditional for every real caller, which is the
    half of substrate's contract that actually protects an operator.

    Escape hatch for a test that genuinely must write the live pointer:
    ``COORDINATOR_ALLOW_LIVE_CONTENT_ROOT_WRITE=1``. Deliberately its own key rather
    than reusing the ``real_home`` marker — writing the operator's live pointer
    should have to be asked for by name, at the call site, not inherited from a
    marker that means "read the real home".

    Bug: state/bug-backlog/2026-08-26-a-test-writes-the-live-claude-machine-lo-
    6cdf6bc87771.yaml — a test put a pytest tmpdir path into the operator's real
    pointer, which under concurrency hands a peer session a pointer into a
    deleted tmpdir.
    """
    if os.environ.get(_LIVE_WRITE_ALLOW_ENV) == "1":
        return None

    under_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    tmp_root = os.path.realpath(tempfile.gettempdir())
    target = os.path.realpath(os.path.dirname(pointer_file) or ".")
    temp_rooted = target == tmp_root or target.startswith(tmp_root + os.sep)

    if under_pytest and temp_rooted:
        return None

    if os.environ.get(_MUTATION_DISABLE_ENV) == "1":
        return f"{_MUTATION_DISABLE_ENV}=1"

    if under_pytest:
        return (
            "running under pytest and the target is outside the OS temp dir "
            f"({target!r}) -- set {_LIVE_WRITE_ALLOW_ENV}=1 if this write is "
            "genuinely intended"
        )
    return None


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, resolve, validate, write (or --check-only dry-run).

    Emits a stdout ``content_root_pointer: <status>`` contract row on every exit path
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

    REFUSES the live write when ``COORDINATOR_DISABLE_MACHINE_MUTATION=1``. That
    switch is set suite-wide by ``coordinator_core/conftest.py::
    _quarantine_real_home`` and is the operator-facing kill switch
    ``install.substrate._refuse_machine_mutation`` already honours; this writer
    did not, which is why a test could land a pytest tmpdir path in the operator's
    real ``<settings-home>/machine-local/.coordinator-content-root`` (state/bug-backlog/
    2026-08-26-a-test-writes-the-live-claude-machine-lo-6cdf6bc87771.yaml).

    The switch is checked rather than a tmp-path heuristic BECAUSE the target is
    not caller-supplied: ``_pointer_file()`` derives it from ``machine_local_dir()``,
    so a caller that has lost ``HOME``/``CLAUDE_HOME`` resolves it through
    ``Path.home()``'s passwd fallback and silently addresses the real machine.
    A path heuristic cannot see that; the switch does not need to.

    Refusing exits 0, not non-zero: an install that legitimately runs under the
    switch must not be failed by it, and every caller branches on the status row.
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
            print("  (no flag)                   Write or refresh <settings-home>/machine-local/.coordinator-content-root (and its .coordinator-content-root twin) from the registry.", file=sys.stderr)
            print("  --check-only                Validate without mutating the live pointer (dry-run-safe).", file=sys.stderr)
            print("  --graceful-skip-unresolved  Exit 0 with a 'skipped' row (not fail-loud) when", file=sys.stderr)
            print("                              repos.content_root cannot be resolved on the live path.", file=sys.stderr)
            return 0

    content_root, rc = _resolve_content_root()
    if content_root is None:
        if graceful_skip_unresolved and not check_only:
            print(
                "content_root_pointer: skipped (repos.content_root unset — "
                "machine-local set repos.content_root <path>; then python3 "
                "<claude-klabauter>/scripts/setup.py)"
            )
            return 0
        return rc

    content_root = content_root.rstrip("/")

    if not os.path.isdir(content_root):
        print(f'{_PROG}: resolved root not found at "{content_root}"', file=sys.stderr)
        print(
            "  Remediation: confirm repos.content_root in the registry is a valid directory,\n"
            "  or set REPO_CONTENT_ROOT=<path>, then: python3 <claude-klabauter>/scripts/setup.py",
            file=sys.stderr,
        )
        print("content_root_pointer: failed (see stderr for gen-content-root-pointer.py output)")
        return 1

    if content_root_for(content_root) is None:
        print(
            f'{_PROG}: no coordinator-claude content found under "{content_root}" '
            f'(neither "{content_root}/coordinator" nor "{content_root}" itself carries the '
            "plugin marker)",
            file=sys.stderr,
        )
        print(
            "  Remediation: confirm the resolved repos.content_root root is a coordinator-claude "
            "clone — either a dev clone (content nested under coordinator/) or a flat "
            "OSS/marketplace clone (content at the repo root), then re-run:\n"
            "  python3 <claude-klabauter>/scripts/setup.py",
            file=sys.stderr,
        )
        print("content_root_pointer: failed (see stderr for gen-content-root-pointer.py output)")
        return 1

    pointer_file = _pointer_file()
    pointer_files = _pointer_files()

    if check_only:
        if _pointer_current(pointer_files, content_root):
            print(f"content_root_pointer: check: {pointer_file} up to date (no-op)")
            return 0

        fd, tmp_path = tempfile.mkstemp(prefix="gen-content-root-pointer.", dir=tempfile.gettempdir())
        try:
            with os.fdopen(fd, "w", newline="\n") as fh:
                fh.write(content_root + "\n")

            with open(tmp_path, encoding="utf-8") as fh:
                written = fh.read().rstrip("\n")

            if written != content_root:
                print(
                    f'{_PROG}: --check-only validation failed (wrote "{written}", '
                    f'expected "{content_root}")',
                    file=sys.stderr,
                )
                print("content_root_pointer: failed in check-only validation (see stderr)")
                return 1

            print(
                f'{_PROG}: --check-only FAILED — {pointer_file} is stale or absent, '
                f'would write "{content_root}" (not written)',
                file=sys.stderr,
            )
            print(f"content_root_pointer: check failed: {pointer_file} is stale or absent (would write)")
            return 1
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                print(f"skip: main: os.remove(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass

    if _pointer_current(pointer_files, content_root):
        print("content_root_pointer: ready (no-op)")
        _seed_plugin_mirror_source_path(content_root)
        return 0

    refusal = _refuse_live_pointer_write(pointer_file)
    if refusal:
        print(f"{_PROG}: refusing to write {pointer_file}: {refusal}", file=sys.stderr)
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            # A REAL install reaching this is nearly always an accident — an
            # ambient COORDINATOR_DISABLE_MACHINE_MUTATION left exported by a
            # prior debug session or a wrapping harness. `run_required_py` treats
            # rc 0 as success, so a one-line stderr note inside a "required"
            # phase is exactly the thing an operator scrolls past. Say it loudly
            # on STDOUT, where the Phase-7 status table is read.
            print(
                f"{_PROG}: WARNING — the .coordinator-content-root pointer was NOT written. "
                f"Unset {_MUTATION_DISABLE_ENV} and re-run if that was not "
                "deliberate."
            )
        print("content_root_pointer: refused (machine mutation disabled)")
        return 0

    pointer_dir = os.path.dirname(pointer_file)
    if pointer_dir and not os.path.isdir(pointer_dir):
        os.makedirs(pointer_dir, exist_ok=True)

    for target in pointer_files:
        fd, tmp_live = tempfile.mkstemp(prefix=".pointer.tmp.", dir=pointer_dir or ".")
        try:
            with os.fdopen(fd, "w", newline="\n") as fh:
                fh.write(content_root + "\n")
            os.replace(tmp_live, target)
            declare_write(target)
        except OSError:
            try:
                os.remove(tmp_live)
            except OSError:
                print(f"skip: main: os.remove(tmp_live) failed: {sys.exc_info()[1]}", file=sys.stderr)
            print("content_root_pointer: failed (see stderr for gen-content-root-pointer.py output)")
            raise

    print(f'{_PROG}: wrote "{content_root}" to {pointer_file}', file=sys.stderr)
    print(f"content_root_pointer: written ({pointer_file})")
    _seed_plugin_mirror_source_path(content_root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

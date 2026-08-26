"""
coordinator_core.ops.install_lfs_pre_push_hook — the LFS pre-push gate's
tracked source of truth, plus its installer.

Purpose: writes (or upgrades) `.git/hooks/pre-push` with the coordinator LFS
gate — a POSIX-sh shim that answers "does this repo track any LFS file?" by
FILE READ, skipping the stock git-lfs shim entirely when the answer is no.
Measured on this box 2026-08-25: the stock shim costs ~267ms / ~20 process
spawns on EVERY push of a repository tracking ZERO LFS files. The gate takes a
no-op push event from 290.6ms / 25 procs to 93.8ms / 9 procs.

WHY THIS MODULE EXISTS AT ALL. The gate itself landed in C1 and worked, but it
lived only in `.git/hooks/`, which is untracked per-clone state that no commit
carries and no installer wrote. That made the win this box's, not the fleet's:
a fresh clone paid the full 267ms until somebody hand-installed the file. AC7
asks for exactly the missing half — "the disposition survives re-clone" — and
this module is that half. The hook body below is the tracked source of truth;
`.git/hooks/pre-push` is a rendering of it.

Spec backlink: chunk C8 of
    `docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md`
    (discharges AC7's second clause). Decision record: DR-223's `pre-push` row
    (`docs/decisions/DR-223-git-hook-minimization-enumerated-local-hooks.md`).
    PM ruling 2026-08-26: the durability gap is fixed here, not routed to
    `state/improvement-queue/2026-08-25-a-hook-removal-does-not-propagate-git-
    ho-d8b135178364.yaml` — that row keeps the GENERAL hook-propagation
    problem; this module closes the LFS pre-push instance.

Shell-out carve-out: generating a `#!/bin/sh` git-hook body is carve-out (b)
in `docs/reference/shell-out-carve-outs.md`, whose Sites list is
ENUMERATION-CONSTITUTIVE — this module names its hook file (`pre-push`)
explicitly, as that carve-out requires, and is registered in that list. It is
NOT a JSON-RPC op and deliberately carries no `_registry_map.py` entry: the
two sibling hook installers it is modelled on
(`install_meta_repo_precommit_hook.py`, `install_publish_repo_precommit_hook.py`)
are `main(argv)` entry points invoked by the install chain, and a third
convention would be invention where reuse was available. C8's own text said
"register it"; the sibling shape is what shipped, because the chunk's
reuse-do-not-invent instruction outranks its guess at the mechanism.

Foreign-hook handling — NOT symmetric with the pre-commit siblings, and this
asymmetry is the correctness core:

    (a) no `pre-push` at all            -> write ours
    (b) the STOCK git-lfs shim, or an
        older version of OUR gate       -> overwrite (this is the upgrade
                                           path; the stock shim is precisely
                                           the thing being replaced)
    (c) anything else                   -> DO NOT touch it; print the one-line
                                           offer and return cleanly

Case (b) is why this file cannot copy the siblings' "never overwrite a foreign
hook" rule verbatim. Their foreign hook is somebody's work; ours is the
vendor default that git-lfs re-creates on `git lfs install`, and refusing to
replace it would make this installer a no-op on every clone that has ever run
git-lfs — which is all of them.

Detection is by CONTENT MARKER, never by mtime, length, or hash of a
particular version: an older rendering of our own gate must still be
recognised as ours so it can be upgraded in place.

Negative-spec:
    - NEVER blocks its caller. Every failure path returns a non-zero code that
      `scripts/setup.py` treats as ADVISORY. A setup run that aborts over a
      push-path optimisation is worse than the gap it closes.
    - Does NOT run `git lfs` anywhere, in the installer or the hook. The
      predicate is a file read of `.gitattributes` / `$GIT_COMMON_DIR/info/
      attributes`. Measured 2026-08-25: `git lfs track` is 280.5ms, MORE than
      the 267.2ms shim it would gate, and `git lfs ls-files` is 214.1ms / 16
      spawns. Gating a 267ms cost behind a 280ms question is a net regression.
      Any future edit that "simplifies" the predicate into a git-lfs call
      re-introduces the entire defect.
    - Does NOT delete a hook. Case (c) leaves the tree exactly as found.
    - Does NOT chmod on Windows as a decision input — the exec bit is written
      for git's POSIX shebang dispatch and never read back. See
      `check_posix_exec_assumptions.py`'s `_REASON_CHMOD_HOOK_POSIX_EXEC`,
      whose allowlist names this file.
    - The `coordinator-lfs-gate:` decision-line tokens in the hook body are a
      CROSS-REPO CONTRACT (AC7c; a consumer tests the exact strings). They are
      not cosmetic and must not be reworded.

What this does NOT close, stated here so the AC7 tick cannot repeat the
2026-08-25 failure of ticking a two-clause criterion on one clause: an
EXISTING clone that never re-runs `scripts/setup.py` stays on the stock shim.
Durable-by-install is what AC7 asks for and what this delivers.
Durable-without-any-install is not achievable for a `.git/hooks/` file at all
— DR-223 records why — and claiming it here would be a false tick.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

GENERATES = []  # writes only the local .git/hooks/pre-push, which is never tracked

_PROG = "install-lfs-pre-push-hook"

# The marker identifying ANY version of our own gate, and the marker
# identifying the stock git-lfs shim we replace. Both are content probes --
# see the module docstring on why detection may not key on anything else.
_GATE_MARKER = "coordinator-lfs-gate"
_STOCK_LFS_MARKER = "git lfs pre-push"

# Cross-repo contract (AC7c). Re-exported so tests and consumers assert
# against one definition instead of re-typing the strings.
DECISION_LINE_SKIPPED = "coordinator-lfs-gate: not-tracked skipped"
DECISION_LINE_DELEGATING = "coordinator-lfs-gate: tracked delegating"

HOOK_FILENAME = "pre-push"

# THE TRACKED SOURCE OF TRUTH. `.git/hooks/pre-push` is a rendering of this
# constant; the guard asserts the two agree. LF endings are deliberate and
# load-bearing: git execs hooks through sh on every platform, Windows
# included, and CRLF in a `#!/bin/sh` script breaks the shebang line.
_HOOK_TEMPLATE = """#!/bin/sh
# coordinator-lfs-gate (installed by coordinator_core/ops/install_lfs_pre_push_hook.py)
#
# Local disposition for docs/decisions/DR-223-git-hook-minimization-enumerated-local-hooks.md's
# `pre-push` row: the stock git-lfs pre-push shim (`git lfs pre-push "$@"`) costs ~267ms /
# ~20 process spawns on EVERY push of a repository tracking ZERO LFS files -- pure overhead,
# not work on this repo's objects.
#
# THE PREDICATE MUST NOT INVOKE git-lfs. Measured on this box 2026-08-25:
#   git lfs track   280.5ms   <-- MORE than the 267.2ms shim it would gate
#   git --version    12.0ms   (this box's single-spawn floor)
# Any `git lfs ...` predicate pays git-lfs' own startup fan-out, which IS the cost being
# removed; gating a 267ms shim behind a 280ms question is a net regression. The plan's C1b
# records the same finding for `git lfs ls-files` (214.1ms / 16 spawns). So the predicate is
# a FILE READ of the attribute files that would have to declare `filter=lfs` for anything
# here to be LFS-tracked -- zero git-lfs processes, and zero spawns beyond this shell.
#
# Fail-safe direction: when the answer is uncertain, DELEGATE. A false "tracked" costs one
# slow push; a false "not-tracked" pushes refs whose LFS objects never upload, which strands
# them for every peer. Correctness first -- no ref reaches the remote without its objects.
#
# DO NOT EDIT THIS FILE IN PLACE. It is a rendering of `_HOOK_TEMPLATE` in
# coordinator_core/ops/install_lfs_pre_push_hook.py, which is the tracked source of truth and
# what re-clone durability depends on (AC7, chunk C8). An in-place edit here is invisible to
# every other clone and is reverted by the next `scripts/setup.py` run.
#
# Guarded by coordinator_core/tests/test_no_lfs_hook_on_push_path.py and
# coordinator_core/tests/test_lfs_pre_push_hook_is_installable.py.

_git_dir="$(git rev-parse --git-common-dir 2>/dev/null)" || _git_dir=".git"
_declares_lfs=""

for _attrs in ".gitattributes" "$_git_dir/info/attributes"; do
  if [ -f "$_attrs" ]; then
    if grep -q "filter=lfs" "$_attrs" 2>/dev/null; then
      _declares_lfs="yes"
      break
    fi
  fi
done

if [ -z "$_declares_lfs" ]; then
  echo "coordinator-lfs-gate: not-tracked skipped" >&2
  exit 0
fi

echo "coordinator-lfs-gate: tracked delegating" >&2
command -v git-lfs >/dev/null 2>&1 || { printf >&2 "
%s

" "This repository is configured for Git LFS but 'git-lfs' was not found on your path. If you no longer wish to use Git LFS, remove this hook by deleting the 'pre-push' file in the hooks directory (set by 'core.hookspath'; usually '.git/hooks')."; exit 2; }
exec git lfs pre-push "$@"
"""


def hook_body() -> str:
    """The hook text this installer writes. One accessor so tests, the
    installer, and any future consumer read the same bytes rather than
    re-deriving them."""
    return _HOOK_TEMPLATE


def classify_existing(current: str | None) -> str:
    """Which of the three foreign-hook cases the on-disk hook falls into.

    Returns one of `"absent"`, `"ours"`, `"stock-lfs"`, `"foreign"`. Split out
    from `install()` so the asymmetry documented in the module docstring is
    testable directly rather than only through a filesystem side effect.
    """
    if current is None:
        return "absent"
    if _GATE_MARKER in current:
        return "ours"
    if _STOCK_LFS_MARKER in current:
        return "stock-lfs"
    return "foreign"


def install(hooks_dir: Path) -> tuple[int, str]:
    """Install or upgrade the gate at `hooks_dir / "pre-push"`.

    Returns `(exit_code, message)`. Exit code is 0 for every outcome the
    caller should treat as success — including the deliberate no-write of case
    (c) — and non-zero only when the write itself failed. Never raises to the
    caller: the install chain must complete regardless.
    """
    target = hooks_dir / HOOK_FILENAME
    desired = hook_body()

    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else None
    except OSError as exc:
        return 1, f"{_PROG}: could not read {target} ({exc}) — hook left as found."

    case = classify_existing(current)

    if case == "foreign":
        return 0, (
            f"{_PROG}: {target} exists and is not the coordinator gate or the stock "
            f"git-lfs shim — left untouched.\n"
            f"  To install the gate over it: python -m coordinator_core.ops."
            f"install_lfs_pre_push_hook --force {hooks_dir}"
        )

    if case == "ours" and current == desired:
        return 0, f"{_PROG}: {target} already current — no write."

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the LF endings the shebang line depends on; Python
        # would otherwise translate them to CRLF on Windows.
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(desired)
        if os.name != "nt":
            # Write-only: git needs this bit for its own shebang dispatch on
            # POSIX. Never read back as a decision input, and meaningless on
            # Windows. See check_posix_exec_assumptions._REASON_CHMOD_HOOK_POSIX_EXEC.
            os.chmod(target, 0o755)
    except OSError as exc:
        return 1, f"{_PROG}: failed writing {target} ({exc})."

    verb = {"absent": "installed", "ours": "upgraded", "stock-lfs": "replaced stock git-lfs shim at"}[case]
    return 0, f"{_PROG}: {verb} {target}."


def main(argv: list[str] | None = None) -> int:
    """CLI entry point, mirroring the sibling hook installers' `main(argv)`
    shape. Argument is the repo root; `--force` installs over case (c)."""
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    if force:
        args.remove("--force")

    root = Path(args[0]).resolve() if args else Path.cwd()
    hooks_dir = root / ".git" / "hooks" if (root / ".git").is_dir() else root

    if force:
        target = hooks_dir / HOOK_FILENAME
        try:
            if target.is_file():
                target.unlink()
        except OSError as exc:
            print(f"{_PROG}: --force could not remove {target} ({exc}).", file=sys.stderr)
            return 1

    code, message = install(hooks_dir)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":  # pragma: no cover - CLI trampoline
    raise SystemExit(main())

"""coordinator_core/tests/test_no_lfs_hook_on_push_path.py --

Guard for DR-223's `pre-push` row (docs/decisions/DR-223-git-hook-
minimization-enumerated-local-hooks.md): LFS should only be involved on the
push path if the repo in question actually uses LFS.

Spec backlink: chunk C1 of
`docs/plans/2026-08-25-push-re-homes-onto-the-cadence-surfaces.md`
(dispatch brief `state/dispatch-briefs/2026-08-25-push-re-homes-onto-
the-cadence-surfaces/C1.md`). Discharges AC7.

Problem measured (spike, cited in the dispatch brief, re-verifiable):
this repo tracks ZERO LFS files (`git lfs ls-files` / `git lfs track` both
empty) yet `.git/hooks/pre-push` was, prior to this chunk, the stock
git-lfs shim (`git lfs pre-push "$@"` unconditionally) -- 267.2ms / 20.0
process spawns on EVERY push, whether it carries zero commits or five.
That cost is git-lfs' own startup fan-out, not work on this repo's
objects, and it fires on the hot push path regardless of what is being
pushed.

PM RULING, 2026-08-25: "LFS should only be involved if the repo in
question uses LFS." The fix is CONDITIONAL, keyed on the repo's own
LFS-tracking state (`git lfs track` output empty means untracked), never
a hardcoded repo name and never `--no-verify` (which would skip every
hook, including load-bearing ones).

Disposition landed by this chunk (see this repo's own `.git/hooks/pre-push`
for the actual shim, and DR-223's `pre-push` row for the decision record):
the local hook now checks `git lfs track` before delegating, emitting one
deterministic `coordinator-lfs-gate:` decision line to stderr either way
(`not-tracked skipped` when the repo tracks nothing, `tracked delegating`
when it does) so the hook's evaluation is a positive, observable event
rather than indistinguishable from never having fired.

RE-CLONE DURABILITY -- gap CLOSED 2026-08-26 by chunk C8, which is what
this paragraph used to defer to. `.git/hooks/` is still untracked per-clone
state (nothing changes that; DR-223 records why no `.git/hooks/` file can be
re-clone-proof on its own), but the hook body now has a TRACKED source of
truth at `coordinator_core/ops/install_lfs_pre_push_hook.py::_HOOK_TEMPLATE`
and `scripts/setup.py` installs it, so a fresh clone gets the gate from the
install chain rather than from somebody remembering. That installer is guarded
by `test_lfs_pre_push_hook_is_installable.py`; the division of labour is that
THIS file proves the gate WORKS on whatever clone runs it, and that one proves
the gate ARRIVES on a clone that never had it. Still open, and deliberately
not claimed as closed: an EXISTING clone that never re-runs setup stays on the
stock shim. The GENERAL "a hook removal does not propagate" problem remains at
`state/improvement-queue/2026-08-25-a-hook-removal-does-not-propagate-git-ho-
d8b135178364.yaml` -- C8 closed the LFS pre-push instance, not the class.

Negative-spec:
    - Skips (does not fail) when this box has no `.git/hooks/pre-push`
      installed at all -- an absent hook is the same zero-cost outcome
      this chunk exists to produce, and this test proves nothing about a
      hook that was never installed on this particular clone.
    - Does NOT assert anything about a repo that DOES track LFS files --
      the predicate this file gates on is "zero LFS-tracked files", and a
      repo that legitimately adopts LFS is expected to pay the delegating
      cost; this guard must not misfire in that case (see
      `test_resolvable_hooks_path_invariant_holds`, which holds regardless
      of LFS-tracking state).
    - Does not itself install, remove, or modify the hook -- read-only /
      execute-and-observe probe, mirroring
      `coordinator/bin/tests/test_installed_hook_gate_scripts_resolve.py`'s
      shape (cited by the dispatch brief) and
      `coordinator_core/tests/test_hot_path_hook_import_budget.py`'s
      subprocess-probe convention.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Spawns real external processes (git, sh); runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The stable decision-line token this chunk's disposition emits on stderr,
# and its two verdict strings. Durable home for this contract, per DR-223's
# `pre-push` row and its "AC7c'S TOKEN NEEDS A DURABLE HOME" note -- a
# cross-repo consumer (claude-klabauter-59) tests against these exact
# strings; changing them is a cross-repo-visible break.
_DECISION_TOKEN = "coordinator-lfs-gate:"
_VERDICT_NOT_TRACKED = "not-tracked skipped"
_VERDICT_TRACKED = "tracked delegating"


def _no_console_creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_common_dir(target: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(target),
            capture_output=True,
            text=True,
            check=False,
            creationflags=_no_console_creationflags(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = target / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _lfs_tracking_is_empty() -> bool:
    """The LFS-tracking predicate this chunk's disposition keys on: `git lfs
    track` reports nothing tracked. Mirrors the dispatch brief's own
    re-verifiable evidence (`git lfs ls-files` / `git lfs track` both empty
    on this repo at authorship). Treated as "not tracked" (safe default) if
    `git-lfs` itself is not installed on this box -- an untracked repo with
    no git-lfs binary present is unambiguously not using LFS.
    """
    try:
        result = subprocess.run(
            ["git", "lfs", "track"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            creationflags=_no_console_creationflags(),
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("Listing tracked")
    ]
    return not lines


@pytest.fixture(scope="module")
def _pre_push_hook_path() -> Path:
    common_dir = _git_common_dir(_REPO_ROOT)
    if common_dir is None:
        pytest.skip("not resolvable as a git repo from this checkout")
    return common_dir / "hooks" / "pre-push"


def test_pre_push_hook_carries_the_conditional_lfs_gate(_pre_push_hook_path: Path) -> None:
    """When this box has zero LFS-tracked files, the installed `pre-push`
    hook (if any) must be THIS chunk's conditional gate, not the stock
    unconditional git-lfs shim (`git lfs pre-push "$@"` with no predicate).
    An absent hook is also a legitimate pass -- it is the same zero-cost
    outcome by a different route (module docstring's negative-spec)."""
    if not _pre_push_hook_path.is_file():
        pytest.skip("no .git/hooks/pre-push installed on this box -- already zero-cost")
    if not _lfs_tracking_is_empty():
        pytest.skip("this repo tracks LFS files -- the untracked-fast-path predicate does not apply")

    text = _pre_push_hook_path.read_text(encoding="utf-8")
    assert _DECISION_TOKEN in text, (
        f"{_pre_push_hook_path} is installed on a repo tracking zero LFS files but does not "
        f"carry the {_DECISION_TOKEN!r} decision token -- this is the stock, unconditional "
        "git-lfs pre-push shim (or an unrelated hook), which pays git-lfs' ~267ms/~20-process "
        "startup cost on every push regardless of whether this repo uses LFS. See "
        "docs/decisions/DR-223-git-hook-minimization-enumerated-local-hooks.md's `pre-push` "
        "row for the conditional disposition this repo expects here."
    )


def test_pre_push_hook_declares_both_decision_verdicts_statically(_pre_push_hook_path: Path) -> None:
    """Static content check (no `sh` execution -- new shell-out spawn sites
    are a closed, PM-ratified list per `docs/reference/shell-out-carve-
    outs.md`, and this repo's push path is not on it): the installed hook's
    SOURCE TEXT must declare both `coordinator-lfs-gate:` verdict strings
    on the two branches of the LFS-tracking predicate, per the module
    docstring's "two signals" -- a decision LINE, not silence, so a gate
    that never fires is distinguishable from one that correctly did
    nothing. This proves the hook is WIRED to emit the line; it does not
    execute the hook to observe the line firing at push time (a detached
    push child's stderr is lost regardless -- see AC7c's own resolvable-
    hooks-path invariant below, which IS runtime-checkable without a
    push)."""
    if not _pre_push_hook_path.is_file():
        pytest.skip("no .git/hooks/pre-push installed on this box -- already zero-cost")
    if not _lfs_tracking_is_empty():
        pytest.skip("this repo tracks LFS files -- the untracked-fast-path predicate does not apply")

    text = _pre_push_hook_path.read_text(encoding="utf-8")
    for verdict in (_VERDICT_NOT_TRACKED, _VERDICT_TRACKED):
        expected = f"{_DECISION_TOKEN} {verdict}"
        assert expected in text, (
            f"{_pre_push_hook_path} does not declare {expected!r} anywhere in its source -- "
            "the installed gate must emit BOTH verdicts (one per branch of the LFS-tracking "
            "predicate), not just the one this repo currently exercises."
        )


def test_gate_predicate_never_invokes_git_lfs(_pre_push_hook_path: Path) -> None:
    """The predicate that decides whether to delegate must not itself be a
    git-lfs invocation. Measured on this box 2026-08-25:

        git lfs track   280.5ms   <-- MORE than the 267.2ms shim it gates
        git lfs ls-files 214.1ms / 16 spawns (plan C1b's own finding)
        git --version    12.0ms   (single-spawn floor)

    git-lfs' startup fan-out IS the cost this chunk exists to remove, so
    asking `git lfs` anything to decide whether to run `git lfs` is a net
    regression -- it was the first implementation of this gate and it made
    every push slower than the stock shim while passing every other test in
    this module. The predicate must be a file read of the attribute files
    instead. This assertion is the thing that keeps it that way.
    """
    if not _pre_push_hook_path.is_file():
        pytest.skip("no .git/hooks/pre-push installed on this box -- already zero-cost")

    text = _pre_push_hook_path.read_text(encoding="utf-8")
    predicate_region, _, delegation_region = text.partition(
        f"{_DECISION_TOKEN} {_VERDICT_TRACKED}"
    )
    assert delegation_region, (
        f"{_pre_push_hook_path} does not carry the {_VERDICT_TRACKED!r} verdict, so the "
        "predicate region cannot be isolated -- see the both-verdicts test above."
    )
    offenders = [
        line.strip()
        for line in predicate_region.splitlines()
        if "git lfs " in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"{_pre_push_hook_path}'s predicate invokes git-lfs before deciding whether to "
        f"delegate: {offenders!r}. git-lfs' startup cost (~214-280ms measured) is what this "
        "gate exists to avoid, so a git-lfs predicate makes every push slower than the stock "
        "shim it replaced. Use a file read of .gitattributes / $GIT_COMMON_DIR/info/attributes "
        "for `filter=lfs` instead -- zero git-lfs processes."
    )


def test_resolvable_hooks_path_invariant_holds() -> None:
    """AC7c: regardless of which re-clone-durable disposition is chosen
    (DR-223's `pre-push` row), `core.hooksPath` must still resolve to a
    directory that actually contains a `pre-push` file, checkable WITHOUT
    running a push -- a detached push child's stderr is otherwise lost.
    `core.hooksPath` redirection (e.g. an engine-scoped commit-hook
    bypass) is explicitly ruled out as this chunk's own mechanism
    precisely because it would fail this invariant by suppressing the
    whole hook directory; this test is what would catch that regression
    if a future change reintroduced it."""
    common_dir = _git_common_dir(_REPO_ROOT)
    if common_dir is None:
        pytest.skip("not resolvable as a git repo from this checkout")

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            creationflags=_no_console_creationflags(),
        )
    except OSError:
        pytest.skip("git not resolvable on this box")
    if result.returncode != 0:
        pytest.skip("git rev-parse --git-path hooks failed on this box")

    raw = result.stdout.strip()
    hooks_dir = Path(raw)
    if not hooks_dir.is_absolute():
        hooks_dir = _REPO_ROOT / hooks_dir
    hooks_dir = hooks_dir.resolve()

    assert hooks_dir.is_dir(), (
        f"the resolved hooks directory {hooks_dir} does not exist -- "
        "core.hooksPath (or the default .git/hooks) must resolve to a real directory"
    )
    if not _pre_push_installed_anywhere(hooks_dir):
        pytest.skip(
            "no pre-push hook installed on this box -- the invariant this test guards "
            "(a chosen disposition must not hide the hooks directory itself) has nothing "
            "to check without an installed hook; absence is the zero-cost outcome, not a "
            "hidden-directory failure"
        )
    assert (hooks_dir / "pre-push").is_file(), (
        f"{hooks_dir} is the resolved hooks directory but does not contain `pre-push`, even "
        "though one is installed elsewhere on this box -- a `core.hooksPath` redirect (or "
        "similar) is hiding the installed hook from what git itself will actually resolve at "
        "push time. This is the exact silent-disabling failure AC7c exists to catch."
    )


def _pre_push_installed_anywhere(resolved_hooks_dir: Path) -> bool:
    """True if a `pre-push` file exists either at the resolved hooks
    directory or at the plain `.git/hooks` default -- used only to decide
    whether the invariant assertion above has anything to check, never as
    the invariant's own pass/fail condition."""
    if (resolved_hooks_dir / "pre-push").is_file():
        return True
    default_dir = _REPO_ROOT / ".git" / "hooks"
    return (default_dir / "pre-push").is_file()


if __name__ == "__main__":
    sys.exit(
        pytest.main([__file__, "-v"])
    )

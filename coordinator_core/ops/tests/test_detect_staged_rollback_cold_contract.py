"""Pins today's COLD baseline for the commit-path mass-deletion gate, before any
warm-transport move touches it.

Spec: `state/dispatch-briefs/2026-08-25-the-commit-hooks-reach-a-warm-engine/C1.md`,
`docs/reference/commit-hook-warm-reach-contract.md` (this file's companion document
-- read that first for the full Set A / Set B enumeration this file exists to gate).

THIS IS NOT A UNIT TEST OF THE DETECTION MATH. `coordinator_core.ops.detect_staged_
rollback` already has its own coverage for `find_mass_deletion` /
`_mass_deletion_should_fire`. What is missing, and what AC1 of the C1 brief asks for,
is proof that the WIRED gate -- installed hook body, real `git commit`, real `HEAD`,
in a scratch clone that is NOT this repo -- still blocks a real mass-deletion attempt
today, cold, before any door/warm-engine transport lands. A green unit suite proves
the imports work; only a real blocked `git commit` proves the gate still guards.

Construction follows `state/audits/2026-08-25-commit-hook-chain-cost-spike.py`'s own
two named traps:
  1. The hook body is installed via `_hook_body(_GATE_REGISTRY)` (through
     `install_claude_klabauter_precommit_hook.main`), NEVER by copying this repo's own
     `.git/hooks/` verbatim -- a copied body pins whatever defect happened to be on
     disk (see that plan's D8) rather than the contract this file is meant to gate.
  2. The gate script this scratch clone's hook invokes is a thin trampoline that
     imports the REAL `coordinator_core.ops.detect_staged_rollback.main` from this
     actual checkout (via `sys.path` insertion of this repo's root) -- not a stub
     that fakes an exit code. The detection math under test is the real math.

Negative-spec:
    - Does not touch `coordinator_core/warm/door/` at all -- this file pins the COLD
      path only, the thing the warm transport must not regress, not the warm path
      itself.
    - Does not assert anything about `emit_indeterminate` / Set B envelopes -- those
      are door-side C-source facts, verified by direct source reading in the
      companion doc, not something a Python-level git-hook test can observe.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops import install_claude_klabauter_precommit_hook as _mod
from coordinator_core.ops.install_claude_klabauter_precommit_hook import _GATE_REGISTRY, main
from coordinator_core.testing.sh_interpreter import require_sh_interpreter

# Behavioral: spawns real git/sh/python processes -- cadence-tier, not the per-commit
# fast path. Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_THIS_REPO_ROOT = str(Path(__file__).resolve().parents[3])

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    run_env["GIT_CONFIG_GLOBAL"] = os.devnull
    run_env["GIT_CONFIG_SYSTEM"] = os.devnull
    if env:
        run_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=run_env,
        creationflags=_NOWIN,
    )


def _make_scratch_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh, real `git init` repo -- NOT this repo -- that the installer is
    monkeypatched to treat as claude-klabauter itself. Mirrors `test_install_claude_klabauter_
    precommit_hook.py`'s own `_make_claude_klabauter_repo` helper (same monkeypatch shape),
    kept local rather than imported so this file's `writes:` scope stays exactly the
    one path the dispatch brief names."""
    repo = tmp_path / "scratch-clone"
    repo.mkdir()
    result = _git(repo, "init", "-q", "-b", "main")
    assert result.returncode == 0, result.stderr
    for cfg in (
        ("user.email", "probe@example.invalid"),
        ("user.name", "probe"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", *cfg)
    monkeypatch.setattr(_mod, "_self_repo_root", lambda: str(repo))
    monkeypatch.setattr(_mod, "_bin_dir", lambda: repo / "coordinator" / "bin")
    return repo


def _write_real_gate_trampoline(repo: Path) -> None:
    """Writes the ONE gate script this scratch clone's installed hook body points
    at (`coordinator/bin/detect-staged-rollback.py`, repo-root-relative --
    `_gate_block`'s own docstring). Delegates straight to the REAL op module in
    THIS actual checkout via a `sys.path` insert of `_THIS_REPO_ROOT` -- no stub,
    no faked exit code. This is deliberately narrower than the production
    trampoline (`coordinator/bin/detect-staged-rollback.py`'s own engine-root
    resolution via `cc_invoke`): that resolution is exactly what the warm-reach
    plan is about to change, and this file's job is pinning what the GATE does,
    not how a future chunk locates the engine."""
    fake_bin = repo / "coordinator" / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    script = fake_bin / "detect-staged-rollback.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            import sys
            sys.path.insert(0, {_THIS_REPO_ROOT!r})
            from coordinator_core.ops.detect_staged_rollback import main as _op_main
            sys.exit(_op_main(sys.argv[1:]))
            """
        ),
        encoding="utf-8",
    )
    os.chmod(script, 0o755)


def _install_real_hook_body(repo: Path) -> None:
    """`main()` (the installer's own CLI entry) writes `_hook_body(_GATE_REGISTRY)`
    to `.git/hooks/pre-commit` -- the sanctioned seeding path this file's dispatch
    brief names (never a verbatim `.git/hooks/` copy)."""
    rc = main([str(repo)])
    assert rc == 0
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    from coordinator_core.ops.install_claude_klabauter_precommit_hook import _hook_body

    assert hook.read_text(encoding="utf-8") == _hook_body(_GATE_REGISTRY), (
        "installed hook body diverged from _hook_body(_GATE_REGISTRY) -- "
        "seeding must never fall back to copying a stale .git/hooks/ body"
    )


def test_real_mass_deletion_against_real_head_blocks_the_commit(tmp_path, monkeypatch):
    """AC1: a planted mass deletion, staged against a REAL HEAD in a scratch clone
    whose hook body was installed (not copied), still blocks a real `git commit`
    today -- cold, no warm transport involved. Crosses the RATIO leg
    (`MASS_DELETION_RATIO_THRESHOLD` = 0.90), not the absolute floor (5127 -- too
    many files for a per-test fixture): 10 tracked files, 9 staged deletions,
    ratio 0.9 >= threshold."""
    require_sh_interpreter()  # fail loud here, not deep inside a subprocess mismatch

    repo = _make_scratch_clone(tmp_path, monkeypatch)
    _write_real_gate_trampoline(repo)
    _install_real_hook_body(repo)

    tracked = [repo / f"file{i}.txt" for i in range(10)]
    for f in tracked:
        f.write_text("seed\n", encoding="utf-8")
    # Staged explicitly by name, never `-A`: the gate trampoline written by
    # `_write_real_gate_trampoline` above already sits untracked under
    # `coordinator/bin/` in this same working tree, and an unscoped `-A` would
    # sweep it into the seed commit too, inflating the tracked-total
    # denominator (11, not 10) and pulling the deletion ratio below the 0.90
    # threshold this test means to cross exactly.
    add = _git(repo, "add", "--", *[f.name for f in tracked])
    assert add.returncode == 0, add.stderr
    seed_commit = _git(repo, "commit", "-q", "-m", "seed: 10 tracked files")
    assert seed_commit.returncode == 0, seed_commit.stderr

    head_before = _git(repo, "rev-parse", "HEAD")
    assert head_before.returncode == 0

    for f in tracked[:9]:
        f.unlink()
    stage = _git(repo, "add", "-A")
    assert stage.returncode == 0, stage.stderr

    result = _git(repo, "commit", "-q", "-m", "attempted mass deletion")

    assert result.returncode != 0, (
        "a staged deletion of 9/10 tracked files (ratio 0.9) must be BLOCKED by "
        f"the cold gate; commit unexpectedly succeeded. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "BLOCKED" in result.stderr

    head_after = _git(repo, "rev-parse", "HEAD")
    assert head_after.returncode == 0
    assert head_after.stdout == head_before.stdout, (
        "HEAD moved despite the BLOCKED commit -- the gate did not actually stop it"
    )


def test_override_env_lets_the_same_mass_deletion_through(tmp_path, monkeypatch):
    """Companion negative case: the same planted deletion, same scratch clone
    shape, but with the documented override armed -- proves the block above is the
    gate firing on purpose, not an unrelated transport failure that would ALSO
    block an override-armed commit."""
    require_sh_interpreter()

    repo = _make_scratch_clone(tmp_path, monkeypatch)
    _write_real_gate_trampoline(repo)
    _install_real_hook_body(repo)

    tracked = [repo / f"file{i}.txt" for i in range(10)]
    for f in tracked:
        f.write_text("seed\n", encoding="utf-8")
    # See the sibling test's comment: staged explicitly, never `-A`, so the
    # untracked gate trampoline under `coordinator/bin/` does not inflate the
    # tracked-total denominator.
    assert _git(repo, "add", "--", *[f.name for f in tracked]).returncode == 0
    assert _git(repo, "commit", "-q", "-m", "seed: 10 tracked files").returncode == 0

    for f in tracked[:9]:
        f.unlink()
    assert _git(repo, "add", "-A").returncode == 0

    result = _git(
        repo,
        "commit",
        "-q",
        "-m",
        "deliberate prune, override armed",
        env={"COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION": "1"},
    )
    assert result.returncode == 0, (
        f"override-armed commit unexpectedly blocked. stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_scratch_clone_is_not_this_repo(tmp_path, monkeypatch):
    """Guards the construction itself: the scratch clone must be a distinct
    filesystem location from this actual checkout, or the whole point of proving
    the gate against a REAL, independent HEAD is void."""
    repo = _make_scratch_clone(tmp_path, monkeypatch)
    assert str(repo.resolve()) != _THIS_REPO_ROOT
    assert not str(repo.resolve()).startswith(_THIS_REPO_ROOT + os.sep)

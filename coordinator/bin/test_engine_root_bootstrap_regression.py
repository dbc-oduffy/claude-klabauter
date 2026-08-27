"""test_engine_root_bootstrap_regression.py — regression coverage for the
"CLI dies with ModuleNotFoundError: coordinator_core" defect class.

Purpose: a `coordinator/bin/` CLI that imports `coordinator_core` (directly,
or transitively via a helper it calls) without first putting the engine root
on `sys.path` dies on the claude-klabauter mirror, where `coordinator_core`
is not pip-installed and the interpreter's `sys.path[0]` is `bin/`, not the
checkout root. This has bitten five separate files one instance at a time
(record-platform-outcome, queue-triage, coordinator-lesson-add,
plan-tasks-resolve and this sweep's other fixes, 2026-08-12) — the
parametrized cases below invoke each fixed CLI as a real subprocess with
`PYTHONPATH` unset, so `sys.path[0]` is genuinely the file's own `bin/`
directory rather than a path this test's own harness happened to add. A
bare in-process import would NOT reproduce the bug: the harness's own
sys.path already carries the engine root by the time an import statement
runs.

DISCOVERED-SET GATE (AC5): `test_no_undiscovered_bootstrap_gaps` below is the
part of this file that stops the class from recurring invisibly.
`bootstrap_discovery.discover_bootstrap_candidates` (`coordinator/bin/lib/
bootstrap_discovery.py`) walks the whole `coordinator/bin/` tree with `ast`
(not grep — see that module's docstring for why a top-level `import
coordinator_core` grep under-reports) and returns every `.py` file that
imports `coordinator_core` anywhere with no recognizable engine-root
bootstrap backing it up. That set is asserted EMPTY here. A newly added CLI
that imports `coordinator_core` (directly, transitively, or from inside a
function body) without bootstrapping the engine root enters this set and
fails the assertion — it does not need a hand-typed entry anywhere in this
file to be caught, which is the property the old hand-maintained
`_FIXED_CLIS`-only list did not have.

The discovered-set gate's first run against this tree (2026-08-12, before the
fixes in this same change) found 10 files the 2026-08-12 sweep's hand-typed
list missed: advance-tracker-status.py, age-sweep-lessons.py,
append-goal-event.py, archive-paper-trail.py,
assert-no-terminal-plans-in-live.py, coordinator-ceremony-hook.py,
migrate-lessons-md-to-yaml.py, reap-integrated-review-findings.py,
regen-cockpit-schema.py, wsc-session-disposition.py — all genuinely broken
(each verified individually against its own source before being fixed, per
the discovery module's own "do not assume a file without the idiom is
broken" discipline), all fixed alongside this file in the same change.

A second pass (same day) closed a predicate gap in the discovery module
itself: `_should_skip_file` was `.py`-only, so ~73 extensionless polyglot
CLI entrypoints under `coordinator/bin/` (`plan-tasks-resolve`,
`coordinator-lesson-add`, `queue-triage`, etc. — the two files that
ORIGINATED this workstream) were invisible to the gate regardless of their
own bootstrap state. Widening the predicate to admit any extensionless
file (discriminated from `.py` on content via `ast.parse`, not a filename
extension list — see `bootstrap_discovery.py`'s docstring) surfaced exactly
one additional genuinely-broken file: `coordinator-initiative` (its
`_resolve_initiatives_dir` function-scoped `coordinator_core.win_portability`
import had no engine-root bootstrap backing it), fixed alongside this
predicate change. Every other extensionless file already carried a working
hand-rolled bootstrap or never imports `coordinator_core` at all.

The parametrized `_FIXED_CLIS` cases below are kept as hand-verified pins for
the specific historical vacuity-trap shapes (module-scope vs function-scoped
import — see the next paragraph), not as the discovery mechanism; the
discovered-set gate above is what makes a hand-typed list unnecessary for
catching a NEW instance.

Two of the fixed files (`plan-tasks-resolve`, `percolate-full-payload-
proof.py`) keep their specific fixed `coordinator_core` import
function-scoped rather than relocated to module scope — a plain `--help`
invocation exits inside `argparse.parse_args()` before that function is ever
called, so `--help` alone would pass identically with the fix reverted. Both
get an argv/probe shaped to actually reach the deferred import (see each
entry's comment below); this was independently verified by re-running the
same invocation against the pre-fix blob (`git show
8775138da5c6^:<path>`) and confirming it dies with `ModuleNotFoundError`
there while passing here.

Negative-spec: does not attempt to enumerate EVERY CLI under `coordinator/
bin/` by hand — `test_no_undiscovered_bootstrap_gaps` is what closes that
gap; the parametrized list below only pins the historically-tricky vacuity
cases plus a couple of the newly-fixed files whose reaching probe is worth
keeping explicit.

Spec backlink: state/handoffs/2026-08-12-engine-root-bootstrap-shared-seam.md
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile

import pytest
from coordinator_core.win_portability import no_console_creationflags

# Every test here invokes each fixed CLI as a REAL subprocess with
# PYTHONPATH unset -- an in-process import cannot reproduce the
# "sys.path[0] is bin/, not the checkout root" defect this file guards
# against (see module docstring). age-sweep-lessons's probe additionally
# spawns real `git init`/`git add` on a throwaway temp dir to exercise its
# --apply write path. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_BIN_DIR, "lib")

import lib  # noqa: F401 — bootstraps coordinator/bin/lib onto sys.path
from bootstrap_discovery import (  # noqa: E402
    FAMILY_SELF_LOCATION,
    _bare_entrypoint_names,
    discover_bootstrap_candidates,
    scan_resolver_call_sites,
)

# Each entry: (relative CLI path, argv passed after the CLI path).
# --help reaches the fixed import for most of these: the affected import sits
# at module scope (or is reached before argparse would ever refuse), so a
# ModuleNotFoundError fires before --help's own output would. `plan-tasks-
# resolve` gets argv that walks past `parse_args()` into the function
# actually carrying the fixed import — a read-only refusal path, no plan/repo
# mutation.
#
# `assert-no-terminal-plans-in-live.py` is deliberately NOT in this list: its
# fixed import lives in `_coordinator_state_root()`, reached from `main()`
# only when `_query_terminal_paths()` (a live records.query call) returns a
# non-empty list -- `--help` never refuses here (this file hand-rolls argv
# parsing with no argparse, so `--help` is a silent no-op token) and the case
# would pass vacuously whenever the live checkout happens to have zero
# terminal-status plans. See
# test_assert_no_terminal_plans_in_live_coordinator_state_root_imports_coordinator_core
# below for its dedicated, deterministically-reaching probe (review finding,
# 2026-08-12).
_FIXED_CLIS = [
    (
        "plan-tasks-resolve",
        [
            "--id",
            "C1",
            "--plan",
            "/nonexistent/plan.md",
            "--moved-to",
            "/nonexistent/target.md",
            "--disposition-detail",
            "regression probe",
        ],
    ),  # reaches _resolve_repo_root's and _read_source_row's deferred
    # coordinator_core imports, then refuses cleanly (exit 2) when the
    # nonexistent plan path can't be read -- no plan/repo mutation attempted.
    ("merge-gate-and-pr.py", ["--help"]),
    ("merge-recovery-and-tag-cut.py", ["--help"]),
    ("merge-release-notes-derive.py", ["--help"]),
    ("probe-memory-headroom.py", ["--help"]),
    ("with-suite-mutex", []),  # refuses on usage before --help is consulted
    ("parallel-review-orthogonality-guard.py", ["--help"]),
    ("percolate-full-payload-proof.py", ["--help"]),
    ("queue-triage.py", ["--help"]),
    ("record-platform-outcome.py", ["--help"]),
    # 2026-08-12 discovered-set fixes (module-scope or reached-before-argparse
    # imports -- --help is real evidence for each):
    ("advance-tracker-status.py", ["--help"]),
    ("append-goal-event.py", ["--help"]),
    ("archive-paper-trail.py", ["--help"]),
    ("coordinator-ceremony-hook.py", ["--help"]),
    ("reap-integrated-review-findings.py", ["--help"]),
    ("regen-cockpit-schema.py", ["--help"]),
    ("wsc-session-disposition.py", ["--help"]),
]


@pytest.mark.parametrize("rel_path, argv", _FIXED_CLIS, ids=[c[0] for c in _FIXED_CLIS])
def test_cli_does_not_die_on_coordinator_core_import(rel_path: str, argv: list[str]) -> None:
    script = os.path.join(_BIN_DIR, rel_path)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, script, *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        **no_console_creationflags(),
    )
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, (
        f"{rel_path} died importing coordinator_core with sys.path[0]=bin/ "
        f"and no PYTHONPATH set:\n{combined}"
    )
    assert "No module named 'coordinator_core'" not in combined


def test_percolate_full_payload_proof_git_init_scratch_dest_imports_coordinator_core() -> None:
    """`percolate-full-payload-proof.py`'s fixed import lives inside
    `_git_init_scratch_dest`, called only from `main()` after
    `parse_args()` -- `--help` never reaches it (see module docstring).
    This calls the function directly, from a subprocess whose cwd is
    OUTSIDE the checkout (so a leaked '' == cwd sys.path entry can't
    smuggle `coordinator_core` in the way it would from the repo root) and
    with `PYTHONPATH` unset, matching the mirror shape the other entries
    exercise via `--help`. The call is read-only against the repo: it only
    `git init`s a throwaway temp directory, per the function's own
    docstring.
    """
    script = os.path.join(_BIN_DIR, "percolate-full-payload-proof.py")
    probe = (
        "import importlib.util, sys, tempfile\n"
        "from pathlib import Path\n"
        f"spec = importlib.util.spec_from_file_location('percolate_probe', {script!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "with tempfile.TemporaryDirectory() as tmpdir:\n"
        "    mod._git_init_scratch_dest(Path(tmpdir))\n"
        "print('PROBE_OK')\n"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as outside_repo_cwd:
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=outside_repo_cwd,
            timeout=30,
            **no_console_creationflags(),
        )
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, (
        "percolate-full-payload-proof.py's _git_init_scratch_dest died "
        f"importing coordinator_core with cwd outside the repo:\n{combined}"
    )
    assert "No module named 'coordinator_core'" not in combined
    assert "PROBE_OK" in proc.stdout


def test_age_sweep_lessons_apply_path_imports_coordinator_core() -> None:
    """`age-sweep-lessons.py`'s `no_console_creationflags` import lives on the
    `--apply` branch, reached only once an aged-universal entry actually needs
    archiving -- `--help` never gets there. Builds a scratch `state/lessons/`
    with one aged `[universal]` entry in a throwaway `git init` tree (so the
    real `git mv` this triggers never touches this repo), runs `--apply` with
    `PYTHONPATH` unset, and asserts both that the import succeeds AND that the
    archival itself completed (proving the probe actually reached --apply's
    write path, not just its dry-run branch).
    """
    script = os.path.join(_BIN_DIR, "age-sweep-lessons.py")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory() as scratch:
        lessons_dir = os.path.join(scratch, "state", "lessons")
        os.makedirs(lessons_dir, exist_ok=True)
        with open(os.path.join(lessons_dir, "aged-entry.yaml"), "w", encoding="utf-8") as fh:
            fh.write("scope: universal\ncreated: 2020-01-01\ntext: probe entry\n")
        subprocess.run(
            ["git", "init", "-q", scratch],
            check=True,
            env=env,
            **no_console_creationflags(),
        )
        subprocess.run(
            ["git", "-C", scratch, "add", "state/lessons/aged-entry.yaml"],
            check=True,
            env=env,
            **no_console_creationflags(),
        )
        proc = subprocess.run(
            [
                sys.executable,
                script,
                lessons_dir,
                "--before",
                "2025-01-01",
                "--apply",
            ],
            capture_output=True,
            text=True,
            cwd=scratch,
            env=env,
            timeout=30,
            **no_console_creationflags(),
        )
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, (
        "age-sweep-lessons.py died importing coordinator_core on its --apply "
        f"path with PYTHONPATH unset:\n{combined}"
    )
    assert "No module named 'coordinator_core'" not in combined
    assert "[APPLIED]" in proc.stdout, f"probe did not reach the archival write path:\n{combined}"


def test_assert_no_terminal_plans_in_live_coordinator_state_root_imports_coordinator_core() -> None:
    """`assert-no-terminal-plans-in-live.py`'s fixed import lives in
    `_coordinator_state_root()`, called from `main()` only when
    `_query_terminal_paths()` returns a non-empty list -- relying on the live
    checkout's ambient `docs/plans/` corpus to force that path would pass
    vacuously the day that corpus happens to be empty (review finding,
    2026-08-12). This probe forces the reaching path deterministically
    instead: loads the module via `importlib`, monkeypatches
    `_query_terminal_paths` to return one synthetic path with no help from
    `records.query`, and runs `main()` against a scratch `--root` with an
    empty `docs/plans/` (so the synthetic path is unheld and "movable",
    proving `_coordinator_state_root()` was actually called, not skipped).
    Runs with `cwd` outside the checkout and `PYTHONPATH` unset, matching the
    other function-scoped probes in this file.
    """
    script = os.path.join(_BIN_DIR, "assert-no-terminal-plans-in-live.py")
    with tempfile.TemporaryDirectory() as scratch_root:
        os.makedirs(os.path.join(scratch_root, "docs", "plans"), exist_ok=True)
        probe = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('assert_probe', {script!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "mod._query_terminal_paths = lambda: ['synthetic-terminal-plan.md']\n"
            f"sys.exit(mod.main(['--root', {scratch_root!r}]))\n"
        )
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as outside_repo_cwd:
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                env=env,
                cwd=outside_repo_cwd,
                timeout=30,
                **no_console_creationflags(),
            )
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, (
        "assert-no-terminal-plans-in-live.py's _coordinator_state_root died "
        f"importing coordinator_core with PYTHONPATH unset:\n{combined}"
    )
    assert "No module named 'coordinator_core'" not in combined
    assert "MOVABLE terminal plan still in docs/plans/: synthetic-terminal-plan.md" in combined, (
        f"probe did not reach _coordinator_state_root's deferred import path:\n{combined}"
    )


def test_coordinator_initiative_create_imports_coordinator_core() -> None:
    """`coordinator-initiative` is extensionless (no `.py` suffix) -- it was
    invisible to the pre-fix `_should_skip_file` predicate (`.py`-only), one
    of the discovery-predicate gaps this change closes. Its fixed import
    lives in `_resolve_initiatives_dir`, called only from `create`/`attach`
    -- `--help` never reaches it (see module docstring's vacuity-trap note).

    `CLAUDE_KLABAUTER_ROOT` is pointed at a nonexistent path so `coordinator-state-
    root.py --central` fails cleanly (exit 1, "failed to resolve central
    state root") once the `coordinator_core.win_portability` import itself
    has already succeeded -- no `state/initiatives/` write is attempted on
    this shared tree.
    """
    script = os.path.join(_BIN_DIR, "coordinator-initiative.py")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["COORDINATOR_ENGINE_ROOT"] = "/nonexistent-regression-probe-root"
    proc = subprocess.run(
        [
            sys.executable,
            script,
            "create",
            "--id",
            "regression-probe-xyz",
            "--label",
            "probe",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        **no_console_creationflags(),
    )
    combined = proc.stdout + proc.stderr
    assert "ModuleNotFoundError" not in combined, (
        "coordinator-initiative died importing coordinator_core with "
        f"sys.path[0]=bin/ and no PYTHONPATH set:\n{combined}"
    )
    assert "No module named 'coordinator_core'" not in combined
    assert "failed to resolve central state root" in combined, (
        f"probe did not reach _resolve_initiatives_dir's subprocess call:\n{combined}"
    )


def test_ensure_engine_on_path_mirror_shaped_checkout() -> None:
    """AC3: proves `cc_invoke.ensure_engine_on_path` from a genuine
    published-mirror SHAPE, not merely "PYTHONPATH unset" (which the other
    tests in this file already exercise, and which is NOT the same claim --
    a PYTHONPATH-unset run from inside this checkout still resolves the
    engine root by walking up from `__file__` to the real `coordinator_core/`
    + `pyproject.toml` markers a few directories up).

    Builds a temp tree carrying ONLY a `coordinator/bin/lib/cc_invoke.py`
    payload (a verbatim copy of the real file -- it has no module-scope
    import of anything else in this tree, so it is genuinely self-contained)
    with NO `coordinator_core` package anywhere in or above that tree, and no
    `pyproject.toml` marker either -- `_walk_up_to_checkout`'s two-marker
    probe (see cc_invoke.py) cannot find a checkout no matter how far up it
    walks. `COORDINATOR_SETTINGS_HOME` is pointed at an empty temp dir so
    the registry/pointer-file rungs can't accidentally resolve THIS
    machine's real engine-repo checkout and mask the mirror shape being tested.

    Two sub-cases, both run as real subprocesses (not reasoned about):
      1. No `CLAUDE_KLABAUTER_ROOT` set: every rung misses (self-location finds no
         checkout, the pointer file doesn't exist, the registry is pointed
         at an empty dir). `ensure_engine_on_path` must return None and NOT
         raise -- its documented best-effort contract -- and a subsequent
         bare `import coordinator_core` in the same process must still fail
         with ModuleNotFoundError (proving the mirror shape genuinely has no
         coordinator_core reachable any other way).
      2. `CLAUDE_KLABAUTER_ROOT` set to this real checkout's root: rung 1 hits
         immediately, `ensure_engine_on_path` returns that root, and a
         subsequent `import coordinator_core` in the same process SUCCEEDS
         -- proving the seam actually threads sys.path correctly once an
         operator has configured CLAUDE_KLABAUTER_ROOT, which is the shape every
         mirror install instructs the operator to do.
    """
    real_cc_invoke = os.path.join(_LIB_DIR, "cc_invoke.py")
    real_mlir = os.path.join(_LIB_DIR, "machine_local_impl_resolve.py")
    # cc_invoke.py imports this at module TOP (not lazily, unlike the sibling
    # above) since the resolver-ladder split, so a mirror without it dies on a
    # ModuleNotFoundError before any rung runs -- the mirror payload's own
    # dependency gap, never the thing this probe is asking about.
    real_engine_bootstrap = os.path.join(_LIB_DIR, "engine_bootstrap.py")
    real_claude_klabauter_root = os.path.dirname(os.path.dirname(_BIN_DIR))  # .../coordinator/bin -> .../<repo>

    with tempfile.TemporaryDirectory() as mirror_root, tempfile.TemporaryDirectory() as empty_settings_home:
        mirror_lib = os.path.join(mirror_root, "coordinator", "bin", "lib")
        os.makedirs(mirror_lib, exist_ok=True)
        # cc_invoke.py's own `_resolve_claude_klabauter_root` registry rung lazily
        # imports this co-located sibling module (see cc_invoke.py's
        # `_machine_local_impl_resolver`) -- a real mirror payload ships it
        # alongside cc_invoke.py, so the mirror shape being tested here must
        # too, or this probe would trip a real seam's own dependency gap
        # rather than testing the thing AC3 asks about.
        for src in (real_cc_invoke, real_mlir, real_engine_bootstrap):
            with open(src, encoding="utf-8") as fh:
                source = fh.read()
            with open(os.path.join(mirror_lib, os.path.basename(src)), "w", encoding="utf-8") as fh:
                fh.write(source)
        probe_script = os.path.join(mirror_root, "coordinator", "bin", "probe.py")
        with open(probe_script, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys\n"
                "_LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')\n"
                "sys.path.insert(0, _LIB_DIR)\n"
                "import cc_invoke\n"
                "root = cc_invoke.ensure_engine_on_path(__file__)\n"
                "print('RESOLVED_ROOT=' + repr(root))\n"
                "try:\n"
                "    import coordinator_core  # noqa: F401\n"
                "    print('IMPORT_OK')\n"
                "except ModuleNotFoundError:\n"
                "    print('IMPORT_FAILED')\n"
            )

        base_env = dict(os.environ)
        base_env.pop("PYTHONPATH", None)
        base_env["COORDINATOR_SETTINGS_HOME"] = empty_settings_home

        # Sub-case 1: no CLAUDE_KLABAUTER_ROOT -- best-effort None, no coordinator_core anywhere.
        env_no_root = dict(base_env)
        env_no_root.pop("CLAUDE_KLABAUTER_ROOT", None)
        proc = subprocess.run(
            [sys.executable, probe_script],
            capture_output=True,
            text=True,
            env=env_no_root,
            cwd=mirror_root,
            timeout=30,
            **no_console_creationflags(),
        )
        assert "Traceback" not in proc.stderr, (
            f"ensure_engine_on_path raised instead of degrading gracefully:\n{proc.stdout}{proc.stderr}"
        )
        assert "RESOLVED_ROOT=None" in proc.stdout, (
            f"mirror-shaped checkout with no CLAUDE_KLABAUTER_ROOT should resolve nothing:\n{proc.stdout}{proc.stderr}"
        )
        assert "IMPORT_FAILED" in proc.stdout, (
            f"mirror shape should have no coordinator_core reachable:\n{proc.stdout}{proc.stderr}"
        )

        # Sub-case 2: CLAUDE_KLABAUTER_ROOT set to the real checkout -- seam threads it onto sys.path.
        env_with_root = dict(base_env)
        env_with_root["COORDINATOR_ENGINE_ROOT"] = real_claude_klabauter_root
        proc2 = subprocess.run(
            [sys.executable, probe_script],
            capture_output=True,
            text=True,
            env=env_with_root,
            cwd=mirror_root,
            timeout=30,
            **no_console_creationflags(),
        )
        assert "Traceback" not in proc2.stderr, f"unexpected failure:\n{proc2.stdout}{proc2.stderr}"
        assert f"RESOLVED_ROOT={real_claude_klabauter_root!r}" in proc2.stdout, (
            f"CLAUDE_KLABAUTER_ROOT-configured mirror checkout should resolve that root:\n{proc2.stdout}{proc2.stderr}"
        )
        assert "IMPORT_OK" in proc2.stdout, (
            f"CLAUDE_KLABAUTER_ROOT-configured mirror checkout should import coordinator_core:\n{proc2.stdout}{proc2.stderr}"
        )


def test_ensure_hooks_fleet_and_gate_validate_invocable_mirror_shaped_checkout() -> None:
    """AC (cross-repo/inbox/2026-08-17-doe-claude-em-mirror-entrypoints-missing-
    coordinator_core.md): `coordinator-ensure-hooks-fleet` and
    `gate-validate-invocable` both used to resolve their engine root with a
    scheme correct ONLY in this tree's layout (`<root>/coordinator/bin` — a
    fixed dirname-hop count for the former, no bootstrap at all for the
    latter). The published OSS mirror puts `bin/` at the repo root instead,
    so both entrypoints died there with `ModuleNotFoundError: No module
    named 'coordinator_core'` — published breakage, twice.

    Mirrors `test_ensure_engine_on_path_mirror_shaped_checkout`'s shape: a
    temp tree with `bin/` at its OWN root (not `coordinator/bin`), carrying
    verbatim copies of both fixed entrypoints plus their `lib/` dependencies
    (`cc_invoke.py` for the shared `_resolve_claude_klabauter_root` resolver,
    `git_hook_install.py` for the fleet entrypoint's own import), with
    `CLAUDE_KLABAUTER_ROOT` pointed at this real checkout — the shape every mirror
    install instructs the operator to configure. Both entrypoints must
    report a successful bootstrap with NO `ModuleNotFoundError`, proving the
    fix is layout-independent rather than merely "still works in our tree".
    """
    real_lib_dir = _LIB_DIR
    real_bin_dir = _BIN_DIR
    real_claude_klabauter_root = os.path.dirname(os.path.dirname(real_bin_dir))  # .../coordinator/bin -> .../<repo>

    with tempfile.TemporaryDirectory() as mirror_root:
        mirror_bin = os.path.join(mirror_root, "bin")
        mirror_lib = os.path.join(mirror_bin, "lib")
        os.makedirs(mirror_lib, exist_ok=True)

        for name in ("cc_invoke.py", "engine_bootstrap.py", "git_hook_install.py"):
            with open(os.path.join(real_lib_dir, name), encoding="utf-8") as fh:
                source = fh.read()
            with open(os.path.join(mirror_lib, name), "w", encoding="utf-8") as fh:
                fh.write(source)

        for name in ("coordinator-ensure-hooks-fleet", "gate-validate-invocable"):
            with open(os.path.join(real_bin_dir, name), encoding="utf-8") as fh:
                source = fh.read()
            with open(os.path.join(mirror_bin, name), "w", encoding="utf-8") as fh:
                fh.write(source)

        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["COORDINATOR_ENGINE_ROOT"] = real_claude_klabauter_root

        # coordinator-ensure-hooks-fleet: probe the module-level bootstrap
        # directly (does NOT call main()/ensure_hooks_fleet(), which would
        # mutate every registered repo's git hooks — out of scope for this
        # import-boundary regression proof).
        fleet_script = os.path.join(mirror_bin, "coordinator-ensure-hooks-fleet")
        fleet_probe = (
            "import importlib.util, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"loader = SourceFileLoader('fleet_probe', {fleet_script!r})\n"
            "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "loader.exec_module(mod)\n"
            "assert mod._BOOTSTRAP_ERROR is None, mod._BOOTSTRAP_ERROR\n"
            "assert callable(mod.ensure_hooks_fleet)\n"
            "print('FLEET_BOOTSTRAP_OK')\n"
        )
        fleet_proc = subprocess.run(
            [sys.executable, "-c", fleet_probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=mirror_root,
            timeout=30,
            **no_console_creationflags(),
        )
        fleet_combined = fleet_proc.stdout + fleet_proc.stderr
        assert "ModuleNotFoundError" not in fleet_combined, (
            "coordinator-ensure-hooks-fleet died importing coordinator_core "
            f"in a mirror-shaped (bin/ at repo root) checkout:\n{fleet_combined}"
        )
        assert "No module named 'coordinator_core'" not in fleet_combined
        assert "FLEET_BOOTSTRAP_OK" in fleet_proc.stdout, (
            f"coordinator-ensure-hooks-fleet's bootstrap did not complete cleanly:\n{fleet_combined}"
        )

        # gate-validate-invocable: run the real CLI end to end (read-only —
        # gate.validate_invocable is advisory-only per its own module
        # docstring, never mutates the repo) against a nonexistent path, so
        # the deferred coordinator_core import is genuinely reached.
        gate_script = os.path.join(mirror_bin, "gate-validate-invocable")
        gate_proc = subprocess.run(
            [sys.executable, gate_script, "nonexistent-regression-probe.py"],
            capture_output=True,
            text=True,
            env=env,
            cwd=mirror_root,
            timeout=30,
            **no_console_creationflags(),
        )
        gate_combined = gate_proc.stdout + gate_proc.stderr
        assert "ModuleNotFoundError" not in gate_combined, (
            "gate-validate-invocable died importing coordinator_core in a "
            f"mirror-shaped (bin/ at repo root) checkout:\n{gate_combined}"
        )
        assert "No module named 'coordinator_core'" not in gate_combined
        assert '"overall"' in gate_proc.stdout, (
            f"gate-validate-invocable did not reach the op and print a verdict:\n{gate_combined}"
        )

        # gate-validate-invocable --help: this is the exact invocation the
        # published mirror's hermetic entrypoint gate makes to check "does
        # this start cleanly" -- the bug the PM reported (cross-repo/inbox/
        # 2026-08-17-doe-claude-em-mirror-entrypoints-missing-coordinator-
        # core.md follow-up) was that this path fell through to the op and
        # printed the dimensions JSON instead of usage text, so the
        # sub-legs above (which never pass --help) stayed green over a
        # broken behavior. Must be exit 0, usage text on stdout, and NO
        # dimensions JSON (no '"overall"' key).
        gate_help_proc = subprocess.run(
            [sys.executable, gate_script, "--help"],
            capture_output=True,
            text=True,
            env=env,
            cwd=mirror_root,
            timeout=30,
            **no_console_creationflags(),
        )
        assert gate_help_proc.returncode == 0, (
            "gate-validate-invocable --help must exit 0 in a mirror-shaped "
            f"checkout, got {gate_help_proc.returncode}:\n"
            f"{gate_help_proc.stdout}{gate_help_proc.stderr}"
        )
        assert '"overall"' not in gate_help_proc.stdout, (
            "gate-validate-invocable --help ran the op and printed the "
            f"dimensions JSON instead of usage text:\n{gate_help_proc.stdout}"
        )
        assert "usage:" in gate_help_proc.stdout, (
            f"gate-validate-invocable --help did not print usage text:\n{gate_help_proc.stdout}"
        )

        # Same-tree sanity leg: both entrypoints must still start cleanly in
        # OUR OWN layout (<root>/coordinator/bin), unchanged by this fix.
        env_own_tree = dict(os.environ)
        env_own_tree.pop("PYTHONPATH", None)
        own_fleet_script = os.path.join(real_bin_dir, "coordinator-ensure-hooks-fleet")
        own_fleet_probe = (
            "import importlib.util, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"loader = SourceFileLoader('fleet_probe_own', {own_fleet_script!r})\n"
            "spec = importlib.util.spec_from_loader(loader.name, loader)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "loader.exec_module(mod)\n"
            "assert mod._BOOTSTRAP_ERROR is None, mod._BOOTSTRAP_ERROR\n"
            "assert callable(mod.ensure_hooks_fleet)\n"
            "print('FLEET_BOOTSTRAP_OK')\n"
        )
        own_fleet_proc = subprocess.run(
            [sys.executable, "-c", own_fleet_probe],
            capture_output=True,
            text=True,
            env=env_own_tree,
            cwd=real_bin_dir,
            timeout=30,
            **no_console_creationflags(),
        )
        own_fleet_combined = own_fleet_proc.stdout + own_fleet_proc.stderr
        assert "ModuleNotFoundError" not in own_fleet_combined, (
            f"coordinator-ensure-hooks-fleet regressed in our own tree layout:\n{own_fleet_combined}"
        )

        own_gate_proc = subprocess.run(
            [sys.executable, os.path.join(real_bin_dir, "gate-validate-invocable"), "nonexistent-regression-probe.py"],
            capture_output=True,
            text=True,
            env=env_own_tree,
            cwd=real_bin_dir,
            timeout=30,
            **no_console_creationflags(),
        )
        own_gate_combined = own_gate_proc.stdout + own_gate_proc.stderr
        assert "ModuleNotFoundError" not in own_gate_combined, (
            f"gate-validate-invocable regressed in our own tree layout:\n{own_gate_combined}"
        )
        assert '"overall"' in own_gate_proc.stdout, (
            f"gate-validate-invocable did not reach the op in our own tree layout:\n{own_gate_combined}"
        )

        own_gate_help_proc = subprocess.run(
            [sys.executable, os.path.join(real_bin_dir, "gate-validate-invocable"), "--help"],
            capture_output=True,
            text=True,
            env=env_own_tree,
            cwd=real_bin_dir,
            timeout=30,
            **no_console_creationflags(),
        )
        assert own_gate_help_proc.returncode == 0, (
            "gate-validate-invocable --help must exit 0 in our own tree "
            f"layout, got {own_gate_help_proc.returncode}:\n"
            f"{own_gate_help_proc.stdout}{own_gate_help_proc.stderr}"
        )
        assert '"overall"' not in own_gate_help_proc.stdout, (
            "gate-validate-invocable --help ran the op in our own tree "
            f"layout instead of printing usage text:\n{own_gate_help_proc.stdout}"
        )
        assert "usage:" in own_gate_help_proc.stdout, (
            f"gate-validate-invocable --help did not print usage text in our own tree layout:\n{own_gate_help_proc.stdout}"
        )


def _assert_bare_entrypoint_classifier_available() -> None:
    """Shared guard for every test in this file that trusts a bare-entrypoint
    scan (`discover_bootstrap_candidates` or `scan_resolver_call_sites`):
    both silently narrow to `.py`-only files when `_bare_entrypoint_names()`
    returns `None` (`coordinator_core` not importable), dropping ~11
    extensionless snapshot files from the scan with no failure signal of its
    own. In this test's process `coordinator_core` is importable by
    construction (this module imports `win_portability` at module scope), so
    a `None` here means the classifier broke, not that the engine is
    missing -- pin that loud, in every caller, rather than trusting a scan
    that may have quietly narrowed.
    """
    assert _bare_entrypoint_names() is not None, (
        "the bare-entrypoint classifier is unavailable, so discovery silently "
        "narrowed to .py-only files and this gate no longer covers the ~73 "
        "extensionless CLIs it is asserted to cover"
    )


def test_no_undiscovered_bootstrap_gaps() -> None:
    """AC5/AC6: the discovered set of `coordinator/bin/` files that import
    `coordinator_core` with no recognizable engine-root bootstrap must be
    EMPTY. A newly added CLI missing the bootstrap enters this set and fails
    this assertion -- see module docstring for the 10 files this gate found
    on its first run against this tree, before they were fixed alongside it.
    """
    # The extensionless half of the scan is admitted by the settled
    # `test_bin_launcher_parity` classifier, and `_bare_entrypoint_names`
    # returns None (falling back to `.py`-only) when `coordinator_core` is not
    # importable. That fallback is correct for the module's own callers but
    # would silently re-open the exact gap this gate closed, so pin it loud
    # HERE: in this test's process coordinator_core is importable by
    # construction (this module imports win_portability at module scope), so a
    # None means the classifier broke, not that the engine is missing.
    _assert_bare_entrypoint_classifier_available()
    candidates = discover_bootstrap_candidates(_BIN_DIR)
    if candidates:
        offenders = "\n".join(
            f"  - {c.rel_path} "
            f"({'module-scope' if c.module_scope_import else 'function-scoped'} import)"
            for c in candidates
        )
        pytest.fail(
            "coordinator/bin/ CLI(s) import coordinator_core with no recognizable "
            f"engine-root bootstrap:\n{offenders}\n\n"
            "Fix: ensure_engine_on_path / require_engine_on_path (env-first) or "
            "require_colocated_engine_on_path (self-location-first, coordinator/bin/"
            "X.py only) at module scope before the coordinator_core import -- "
            "ensure_ swallows, require_ raises."
        )


# AC4: checked-in snapshot of which resolver FAMILY each file under
# coordinator/bin/ uses, captured from the 2026-08-12 working tree (see plan
# `2026-08-12-hand-rolled-engine-root-bootstraps-become-seam-calls.md`, C3).
#
# Keyed on rel_path -> set of families, deliberately NOT on (lineno, call_name):
# the C4/C5/C6 migration rewrites `resolve_engine_root(...)` call sites into
# `require_engine_on_path(...)` one-liners, which changes both the call name and
# every line number below it in the file. A snapshot keyed on those would go red
# on every migration commit for no semantic reason, and would force each of the
# concurrent per-batch migration executors to edit this one shared file. The
# FAMILY is the invariant AC4 actually pins: a site may change call name, but an
# env-first file must stay env-first.
#
# `lib/cc_invoke.py` is the sole multi-family entry -- it defines both variants.
# ROWS REMOVED 2026-08-25 -- each named a file that no longer calls a resolver,
# which this map's own drift message says to drop in the same commit as the
# removal. Three of them (`scoped-git-commit`, `sweep-actioned-memos.py`,
# `sweep-shipped-handoffs.py`) no longer EXIST on disk at all; the other three
# (`coordinator-prepare-commit-msg`, `schema-drift-gate.py`,
# `workday-complete-assemble.py`) became thin forwarders whose bootstrap now
# happens inside `lib/entry_point_shim.py` on their behalf, so the resolver
# call is no longer in the file this map keys on.
_RESOLVER_FAMILY_BY_FILE = {
    # ROWS ADDED 2026-08-25 -- eleven files called a resolver with no row here.
    # The map is a two-way snapshot (drift + vanished + undiscovered), so a
    # missing row is as much a stale-snapshot failure as an orphaned one.
    "advance-tracker-status.py": frozenset({"env_first"}),
    "age-sweep-lessons.py": frozenset({"env_first"}),
    "app-session.py": frozenset({"env_first"}),
    "append-goal-event.py": frozenset({"env_first"}),
    "append-plan-session.py": frozenset({"env_first"}),
    "archive-paper-trail.py": frozenset({"env_first"}),
    "assert-no-terminal-plans-in-live.py": frozenset({"env_first"}),
    "autonomous-verb.py": frozenset({"self_location"}),
    "cartography.py": frozenset({"self_location"}),
    "check-auto-reconcile.py": frozenset({"env_first"}),
    "check-mcp-versions.py": frozenset({"self_location"}),
    "check-no-illegal-paths.py": frozenset({"env_first"}),
    "coordinator-ceremony-hook.py": frozenset({"env_first"}),
    "coordinator-doc-new.py": frozenset({"env_first"}),
    "coordinator-initiative.py": frozenset({"env_first"}),
    "coordinator-lesson-add.py": frozenset({"env_first"}),
    "coordinator-prepare-commit-msg.py": frozenset({"self_location"}),
    "coordinator-safe-commit.py": frozenset({"env_first"}),
    "coordinator-tasks-mirror.py": frozenset({"env_first"}),
    "coordinator-validate-local-config.py": frozenset({"self_location"}),
    "coordinator-write-review-trail.py": frozenset({"env_first"}),
    "cross-repo-memo.py": frozenset({"env_first"}),
    "debash-scorecard.py": frozenset({"env_first"}),
    "distill-delete-guard.py": frozenset({"self_location"}),
    "distill-harvest-debt.py": frozenset({"self_location"}),
    "distill-log-append.py": frozenset({"self_location"}),
    "distill-log-normalize.py": frozenset({"self_location"}),
    "distill-ripe-filter.py": frozenset({"self_location"}),
    "distill-sidecar-sweep.py": frozenset({"self_location"}),
    "doctor.py": frozenset({"self_location"}),
    "emit-goal-from-artifact.py": frozenset({"env_first"}),
    "engine-gap-lint.py": frozenset({"env_first"}),
    "fan-out-dispatch.py": frozenset({"env_first"}),
    "fix-concrete-path-citations.py": frozenset({"self_location"}),
    "fleet-env-cutover.py": frozenset({"self_location"}),
    "handoff-archive-transition.py": frozenset({"env_first"}),
    "handoff-backfill-claim-stamp.py": frozenset({"env_first"}),
    "handoff-discharge-criteria.py": frozenset({"env_first"}),
    "handoff-has-live-children.py": frozenset({"env_first"}),
    "handoff-reconcile-close-terminal.py": frozenset({"env_first"}),
    "handoff-stamp-phase.py": frozenset({"env_first"}),
    "lessons-outbox-drain.py": frozenset({"self_location"}),
    "lib/cc_invoke.py": frozenset({"env_first", "self_location"}),
    "lib/coordinator_registry.py": frozenset({"env_first"}),
    "lib/entry_point_shim.py": frozenset({"self_location"}),
    "lib/repo_identity.py": frozenset({"env_first"}),
    "lib/workday_ceremony_lib.py": frozenset({"env_first"}),
    "merge-gate-and-pr.py": frozenset({"env_first"}),
    "merge-recovery-and-tag-cut.py": frozenset({"env_first"}),
    "merge-release-notes-derive.py": frozenset({"env_first"}),
    "parallel-review-orthogonality-guard.py": frozenset({"env_first"}),
    "percolate-full-payload-proof.py": frozenset({"env_first"}),
    "percolate-preflight-scratch-publish.py": frozenset({"env_first"}),
    "plan-task-brief.py": frozenset({"env_first"}),
    "plan-tasks-resolve": frozenset({"env_first"}),
    "plan-tasks-stamp": frozenset({"env_first"}),
    "probe-memory-headroom.py": frozenset({"env_first"}),
    "probe-prereq.py": frozenset({"self_location"}),
    "publish.py": frozenset({"env_first"}),
    "queue-triage.py": frozenset({"env_first"}),
    "reap-integrated-review-findings.py": frozenset({"env_first"}),
    "reaper-resting-batons.py": frozenset({"env_first"}),
    "record-platform-outcome.py": frozenset({"env_first"}),
    "regen-cockpit-schema.py": frozenset({"env_first"}),
    "seed-marketplace-enabledplugins.py": frozenset({"self_location"}),
    "spawn-census": frozenset({"self_location"}),
    "standup.py": frozenset({"env_first"}),
    "sweep-terminal-handoffs.py": frozenset({"env_first"}),
    "tests/test_cc_invoke_py.py": frozenset({"env_first", "self_location"}),
    "tests/test_checked_repo_resolver.py": frozenset({"env_first"}),
    "tests/test_checked_repo_resolver_c4.py": frozenset({"env_first"}),
    "validate-fast-and-packageability.py": frozenset({"self_location"}),
    "whats-next.py": frozenset({"env_first"}),
    "with-suite-mutex": frozenset({"env_first"}),
    "workday-complete-args-and-validate.py": frozenset({"env_first"}),
    "workday-complete-backfill-inject-anchor.py": frozenset({"env_first"}),
    "workday-complete-close.py": frozenset({"self_location"}),
    "workday-complete-step1-validate.py": frozenset({"self_location"}),
    "workday-start-advisory-counters.py": frozenset({"self_location"}),
    "workday-start-day-branch-resolve.py": frozenset({"self_location"}),
    "workweek-complete-advisories.py": frozenset({"self_location"}),
    "workweek-complete-brief.py": frozenset({"self_location"}),
    "workweek-complete-doc-staleness.py": frozenset({"self_location"}),
    "workweek-complete-doc-verify.py": frozenset({"self_location"}),
    "wsc-close.py": frozenset({"self_location"}),
    "wsc-session-disposition.py": frozenset({"env_first"}),
}


def test_resolver_family_map_no_family_drift() -> None:
    """AC4 (part 1): no file under `coordinator/bin/` has moved between the
    env-first and self-location-first resolver families relative to the
    checked-in `_RESOLVER_FAMILY_BY_FILE` snapshot above.

    This is the assertion that would have caught the naive migration: the two
    ladders disagree on rung order deliberately, so mapping a site onto the
    wrong variant flips which checkout it resolves. Changing a call NAME within
    a family (the C4/C5/C6 one-liner migration) is expected and does not move
    this map; changing a file's FAMILY fails loud.
    """
    _assert_bare_entrypoint_classifier_available()
    current: dict[str, set[str]] = {}
    for site in scan_resolver_call_sites(_BIN_DIR):
        current.setdefault(site.rel_path, set()).add(site.family)

    drifted = {
        rel: (sorted(_RESOLVER_FAMILY_BY_FILE[rel]), sorted(fams))
        for rel, fams in current.items()
        if rel in _RESOLVER_FAMILY_BY_FILE and set(fams) != set(_RESOLVER_FAMILY_BY_FILE[rel])
    }
    assert not drifted, (
        "resolver family changed for file(s) under coordinator/bin/ -- "
        f"{ {k: f'expected {v[0]}, found {v[1]}' for k, v in drifted.items()} }. "
        "env-first and self-location-first resolve to different checkouts; a "
        "migration must preserve a site's family."
    )

    vanished = sorted(set(_RESOLVER_FAMILY_BY_FILE) - set(current))
    assert not vanished, (
        f"file(s) no longer call any resolver: {vanished}. The guard above "
        "already confirmed the bare-entrypoint classifier is available, so "
        "this is NOT the narrowed-scan failure mode -- if a bootstrap was "
        "genuinely, deliberately removed, drop its row from "
        "_RESOLVER_FAMILY_BY_FILE in the same commit; do not touch the "
        "snapshot for any other reason."
    )

    new = sorted(set(current) - set(_RESOLVER_FAMILY_BY_FILE))
    assert not new, (
        f"file(s) call a resolver but are not in the checked-in snapshot: {new}. "
        "Add a row for each to _RESOLVER_FAMILY_BY_FILE in the same commit."
    )


def _assert_shim_never_self_locates() -> None:
    """The teeth behind `entry_point_shim.py`'s depth exemption above.

    The shim is allowed to call a self-location-first resolver from
    `coordinator/bin/lib/` ONLY because it hands over a top-level
    `BIN_DIR / "<entrypoint>.py"` path -- the depth `parents[2]` is correct
    for. Passing its own `__file__` would probe one directory too deep and
    silently fall through to the registry ladder, which is exactly the trap
    the exemption must not become a hole for.
    """
    shim = os.path.join(str(_BIN_DIR), "lib", "entry_point_shim.py")
    if not os.path.isfile(shim):
        return
    with open(shim, encoding="utf-8") as fh:
        source = fh.read()
    # AST, not a regex over the text: the shim's own docstring QUOTES the
    # `require_colocated_engine_on_path(__file__)` form it deliberately does
    # NOT use, and a text scan reads that prose as a call site.
    governed = {"require_colocated_engine_on_path", "resolve_colocated_claude_klabauter_root"}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in governed:
            continue
        for arg in node.args:
            offending = any(
                isinstance(sub, ast.Name) and sub.id == "__file__"
                for sub in ast.walk(arg)
            )
            assert not offending, (
                f"entry_point_shim.py:{node.lineno} passes its own __file__ to "
                f"{name}(): the fixed-depth parents[2] rung misresolves from "
                f"lib/. Pass the top-level BIN_DIR / '<entrypoint>.py' path it "
                f"stands in for."
            )


def test_no_self_location_family_call_below_top_level() -> None:
    """AC4 (part 2): the depth trap is unrepresentable. `resolve_colocated_claude_klabauter_root`
    / `require_colocated_engine_on_path` resolve via a fixed-depth `parents[2]` rung
    that is correct only at `coordinator/bin/X.py` -- one level deeper (`coordinator/
    bin/lib/`, `coordinator/bin/tests/`) it silently falls through to the registry
    ladder instead, risking cross-checkout misresolution. No file below
    `coordinator/bin/`'s top level may call either name.

    Two exemptions, both because the rule is about a file BOOTSTRAPPING ITSELF
    through a rung that misresolves at its own depth -- not about the name
    appearing in a file:

    - `coordinator/bin/lib/cc_invoke.py` DEFINES both names;
      `require_colocated_engine_on_path`'s own body calling
      `resolve_colocated_claude_klabauter_root` is the implementation, not a caller that
      forgot the depth rule.
    - anything under `coordinator/bin/tests/` calls these names as the
      subject-under-test (asserting the variant's own ladder behaviour), not to
      put its own engine root on `sys.path`. Family drift in a test file is
      still caught -- by `test_resolver_family_map_no_family_drift` above, which
      covers `tests/` unexempted -- so this exemption narrows the depth check
      without opening a hole.
    - `coordinator/bin/lib/entry_point_shim.py` bootstraps on behalf of the
      TOP-LEVEL entry points it stands in for, and passes each one's own
      `BIN_DIR / "<name>.py"` path rather than its own `__file__` -- so
      `parents[2]` is evaluated against a `coordinator/bin/X.py` path and
      resolves correctly, which is the whole point of the rule rather than an
      exception to it. `_assert_shim_never_self_locates` below is what keeps
      this exemption honest: the moment the shim passes its OWN `__file__`, it
      IS bootstrapping itself at the wrong depth and fails loud.
    """
    _assert_bare_entrypoint_classifier_available()
    _assert_shim_never_self_locates()
    offenders = [
        site
        for site in scan_resolver_call_sites(_BIN_DIR)
        if site.family == FAMILY_SELF_LOCATION
        and "/" in site.rel_path
        and site.rel_path != "lib/cc_invoke.py"
        and site.rel_path != "lib/entry_point_shim.py"
        and not site.rel_path.startswith("tests/")
    ]
    assert not offenders, (
        "self-location-first resolver called below coordinator/bin/'s top level "
        f"(the fixed-depth parents[2] rung misresolves there): {offenders}"
    )

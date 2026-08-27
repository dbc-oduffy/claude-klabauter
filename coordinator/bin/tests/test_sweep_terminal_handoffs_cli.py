"""test_sweep_terminal_handoffs_cli.py — the two CLI-level criteria of
`docs/plans/2026-08-26-the-sweep-stops-paying-for-a-room-it-nev.md` that
shipped with no covering assertion at all.

AC-3 (`asyncio` absent from the CLI's import graph) and AC-8 (the DR-277
disposition is unchanged — the sweep warns and PROCEEDS on a repo-identity
MISMATCH) both read `open` on a plan at `status: landed`. Neither had a test;
`sweep-terminal-handoffs.py` had no test file whatsoever. AC-11 was the same
shape — implementation real, behaviour correct, entirely unguarded — so the
absence of a red is not evidence of a discharge.

AC-3 is FAILED AS WRITTEN, and the failure is a defect in the criterion
rather than in the work; see `TestAC3AsyncioImportGraph` for which half is
discharged and which half the plan forbade itself from reaching.

Spec backlink: `pln-the-sweep-stops-paying-for-a-r-292f23` § AC-3, AC-8.
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns real `git` processes (the fixture repo) and a fresh interpreter for
# the import-graph probe; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "sweep-terminal-handoffs.py"
_PROJECT_ROOT = _BIN_DIR.parent.parent

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_module():
    """Import sweep-terminal-handoffs.py as a fresh module object each call."""
    spec = importlib.util.spec_from_file_location(
        "sweep_terminal_handoffs_under_test", str(_SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(
            ["git", *argv], cwd=str(root), check=True,
            capture_output=True, text=True, env=_GIT_ENV, timeout=15,
            **no_console_creationflags(),
        )
    (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root), check=True,
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"], cwd=str(root), check=True,
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        **no_console_creationflags(),
    )
    return root


class TestAC3AsyncioImportGraph:
    """AC-3 as written: "after `main()` returns, `asyncio` not in sys.modules
    for the standalone path". That is FALSE on disk, and the criterion is the
    defect rather than the work — the same shape AC-1 failed in.

    `sweep-terminal-handoffs.py :: main` drives `_common.archive_and_commit`,
    which is still a coroutine and which this plan's own § Out of scope
    excludes outright as claude-klabauter-15's. So AC-3 named a property
    discharged only by de-async-ing a function the plan forbade itself from
    touching, and the CLI necessarily keeps one `asyncio.run(...)` boundary.

    What C2 actually delivered, and what these two tests pin, is the half
    that IS reachable: the op module carries no module-scope `import asyncio`
    (the 31.3ms cost, dragging ssl/socket, on the classification-only path),
    and importing the CLI does not drag `asyncio` either. The third test is a
    tripwire on the residual: it goes red the day `archive_and_commit`
    becomes sync, at which point AC-3 becomes dischargeable as written.
    """

    def test_op_module_has_no_module_scope_asyncio_import(self):
        text = (
            _PROJECT_ROOT
            / "coordinator_core" / "ops" / "fleet" / "archive_terminal_handoffs.py"
        ).read_text(encoding="utf-8")
        module_scope = [
            line for line in text.splitlines()
            if line.startswith("import asyncio") or line.startswith("from asyncio")
        ]
        assert not module_scope, (
            "C2 deleted the op module's module-scope `import asyncio`; every "
            f"remaining import must be function-local. Found: {module_scope!r}"
        )

    def test_importing_the_cli_does_not_drag_asyncio(self):
        probe = (
            "import sys, importlib.util as u; "
            f"spec = u.spec_from_file_location('m', r'{_SCRIPT}'); "
            "m = u.module_from_spec(spec); spec.loader.exec_module(m); "
            "print('asyncio_loaded=' + str('asyncio' in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=60,
            **no_console_creationflags(),
        )
        assert result.returncode == 0, result.stderr
        assert "asyncio_loaded=False" in result.stdout, (
            "importing the sweep CLI must not pay the asyncio import; got "
            f"{result.stdout!r}"
        )

    def test_main_still_loads_asyncio_because_archive_and_commit_is_a_coroutine(self):
        """TRIPWIRE, not a discharge. Pins AC-3's residual to the one named
        reason it survives. When `_common.archive_and_commit` goes sync and
        the `asyncio.run(...)` boundary at `main`'s body disappears, this
        test goes red — that is the signal to flip AC-3 from FAILED-as-written
        to discharged, not a reason to delete the assertion.
        """
        text = _SCRIPT.read_text(encoding="utf-8")
        assert "asyncio.run(" in text, (
            "if the CLI no longer runs an asyncio boundary, AC-3 is now "
            "dischargeable as written — replace this tripwire with the "
            "criterion's own `'asyncio' not in sys.modules` assertion"
        )
        common = (
            _PROJECT_ROOT / "coordinator_core" / "ops" / "fleet" / "_common.py"
        ).read_text(encoding="utf-8")
        assert "async def archive_and_commit" in common, (
            "`archive_and_commit` is the reason the CLI keeps an asyncio "
            "boundary at all; if it is now sync, AC-3's residual is gone"
        )


class TestAC8DR277DispositionUnchanged:
    """AC-8: the sweep still WARNS and PROCEEDS on a repo-identity MISMATCH;
    a test pins that it does NOT exit non-zero.

    Named as an AC because the cheapest wrong way to "fix" a discarded
    verdict is to start enforcing it, which DR-277 forbids absent a
    carve-out. The disposition lived in `main()` with nothing holding it
    there.
    """

    def _run_main_with_verdict(self, tmp_path, verdict):
        mod = _load_module()
        repo = _init_repo(tmp_path / "repo")

        def _fake_resolver(explicit_root=None):
            return str(repo), verdict

        mod.resolve_checked_repo_root = _fake_resolver
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main([])
        return code, out.getvalue(), err.getvalue()

    def test_mismatch_warns_to_stderr_and_still_exits_zero(self, tmp_path):
        code, out, err = self._run_main_with_verdict(
            tmp_path,
            {
                "verdict": "MISMATCH",
                "message": "repo-identity: MISMATCH sentinel for AC-8",
                "session_root": "/elsewhere",
                "resolved_root": None,
                "sid": "sid-ac8",
            },
        )
        assert "MISMATCH sentinel for AC-8" in err, (
            f"a MISMATCH must warn to stderr (DR-277 advisory); got {err!r}"
        )
        assert code == 0, (
            "DR-277: the sweep WARNS and PROCEEDS on MISMATCH — it must never "
            f"exit non-zero on the verdict alone; got exit {code}"
        )
        # POSITIVE LIMB: exit 0 alone is also what an early return would give
        # if the warn branch were reordered ahead of the sweep. The tail's own
        # census line proves `main()` PROCEEDED through the sweep, not merely
        # that it declined to fail.
        assert "terminal handoffs archived" in out, (
            "proceeding means reaching the sweep tail, not just exiting 0; got "
            f"stdout {out!r}"
        )

    def test_match_proceeds_silently_with_no_repo_identity_warning(self, tmp_path):
        code, _out, err = self._run_main_with_verdict(
            tmp_path,
            {
                "verdict": "MATCH",
                "message": "repo-identity: MATCH sentinel for AC-8",
                "session_root": None,
                "resolved_root": None,
                "sid": "sid-ac8",
            },
        )
        assert "MATCH sentinel for AC-8" not in err, (
            f"MATCH proceeds silently — nothing goes to stderr; got {err!r}"
        )
        assert code == 0


_TERMINAL_HANDOFF = """---
title: A terminal handoff the census must name
status: consumed
deployment_state: abandoned
---

body
"""


class TestDryRunCensus:
    """`--dry-run` is the operator's census surface over the same `plan_sweep`
    classification the acting path runs.

    Its reason for existing is that the only other census route — the op's
    `dry_run:true` preview — returns `candidates: []` under `exit_code:1`
    from `fleet/_common.py :: _setup_error`, so a caller reading the
    candidate list instead of the exit code cannot tell a REFUSED invocation
    from an empty corpus. That mis-read is what the 2026-08-27 zero-vs-25
    disagreement was.

    The load-bearing property is not that the census prints — it is that it
    prints WITHOUT acting: no move, no commit, and no `archive_sweeps`
    liveness stamp (a census must not hold the cadence gate open on behalf of
    a sweep that never ran).
    """

    def _run_dry_run(self, tmp_path, *, seed_terminal: bool = True):
        mod = _load_module()
        repo = _init_repo(tmp_path / "repo")
        if seed_terminal:
            (repo / "state" / "handoffs" / "2026-01-01_census-fixture.md").write_text(
                _TERMINAL_HANDOFF, encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "-A"], cwd=str(repo), check=True,
                capture_output=True, text=True, env=_GIT_ENV, timeout=15,
                **no_console_creationflags(),
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "seed handoff"], cwd=str(repo), check=True,
                capture_output=True, text=True, env=_GIT_ENV, timeout=15,
                **no_console_creationflags(),
            )

        mod.resolve_checked_repo_root = lambda explicit_root=None: (
            str(repo),
            {"verdict": "MATCH", "message": "", "session_root": None,
             "resolved_root": None, "sid": "sid-census"},
        )
        stamped = []
        mod._stamp_archive_sweeps_liveness = lambda root: stamped.append(root)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mod.main(["--dry-run"])
        return repo, code, out.getvalue(), err.getvalue(), stamped

    def test_census_names_what_it_would_move_and_moves_nothing(self, tmp_path):
        repo, code, out, err, _stamped = self._run_dry_run(tmp_path)
        src = repo / "state" / "handoffs" / "2026-01-01_census-fixture.md"
        assert code == 0, f"census exits 0; got {code}, stderr={err!r}"
        assert "2026-01-01_census-fixture.md" in out, (
            "the census must NAME the record it would move — an unnamed count "
            f"is the very ambiguity this flag exists to remove; got {out!r}"
        )
        assert src.exists(), "a census must not move the record it names"
        assert not (repo / "archive" / "handoffs").exists(), (
            "a census must not create the archive destination"
        )
        head = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%s"], cwd=str(repo),
            capture_output=True, text=True, env=_GIT_ENV, timeout=15,
            **no_console_creationflags(),
        ).stdout.strip()
        assert head == "seed handoff", (
            f"a census must not commit; HEAD subject is {head!r}"
        )

    def test_census_does_not_stamp_the_archive_sweeps_liveness_key(self, tmp_path):
        _repo, code, _out, _err, stamped = self._run_dry_run(tmp_path)
        assert code == 0
        assert stamped == [], (
            "a census is not a sweep — stamping `archive_sweeps` would let a "
            "run of censuses hold the cadence gate open while nothing was "
            f"ever archived; got {stamped!r}"
        )

    def test_empty_corpus_census_says_so_rather_than_printing_nothing(self, tmp_path):
        _repo, code, out, _err, _stamped = self._run_dry_run(
            tmp_path, seed_terminal=False
        )
        assert code == 0
        assert "no terminal handoffs would be archived" in out, (
            "an empty census must say it is empty — silence is the failure "
            f"mode this flag exists to fix; got {out!r}"
        )

"""Regression test for `sync_mirror`'s `renamed_dir_names` orphan-sweep exemption.

Covers the forward-compatible hook added to `coordinator/lib/percolate/publish_sync.py`
for the engine-side directory-rename primitive (§ state/audits/2026-08-05-first-full-
payload-identity-findings.md Group E, and
coordinator_core/percolate/rewrite_basename.py's `rename_directories`). Not yet wired
to any real publish target -- see this module's own `sync_mirror` docstring for why:
closing the hazard end to end also needs a `coordinator/bin/publish.py` call-site change
(reading the engine's rename ledger and passing it into this parameter) and an
`coordinator_core/percolate/engine.py` sequencing change (reap-before-rename), both out
of scope for this dispatch.

`TestProcessTargetLedgerExemption` below covers the OTHER half, now landed:
`coordinator/bin/publish.py`'s `process_target` call-site that reads the row's
rename-generation ledger and forwards it here as `renamed_dir_names` -- and, per
docs/plans/2026-08-11-dry-run-cries-wolf-on-the-rename-exemption.md, does so
identically under `--dry-run` and a real run.

Loaded by file path (`importlib.util.spec_from_file_location`), matching the convention
`coordinator/tests/test_percolate_driver_sync.py` already uses for this same module --
`coordinator/` and `coordinator/lib/` carry no `__init__.py`, so a dotted import is not
available.

Negative-spec: no persona names, no codenames, no consumer-home path literals. All
fixture content is synthetic.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

# TestProcessTargetLedgerExemption's dry_run=False leg needs a real
# committed git work tree: `process_target`'s non-dry-run path materializes
# every contributing root from its committed ref before syncing, a
# production behaviour no mock stands in for. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# `publish_sync.py` does `from .ignore import ...` -- a package-relative import that
# only resolves when the module is loaded AS PART OF its `percolate` package, not via a
# bare `spec_from_file_location` (which `coordinator/tests/test_percolate_driver_sync.py`
# avoids by going through `publish._import_publish_sync` instead). `coordinator/` and
# `coordinator/lib/` carry no `__init__.py` (no dotted import available from repo root),
# but `coordinator/lib/percolate/` DOES have one, so putting `coordinator/lib` on
# `sys.path` and importing `percolate.publish_sync` as an ordinary package member
# resolves the relative import correctly.
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402

# Real exception class production's `publish.py::process_target` matches on
# (`except (KeyError, rewrite_basename_module.DirectoryRenamePairShapeError)`)
# when unioning a static `basename_rename` section into the ledger-derived
# exemption set -- attached below to `_FakeRewriteBasenameModule` so that
# except-tuple evaluation resolves even though the fake never raises it
# itself (it only stands in for the ledger-load half of the module's
# contract, per its own docstring).
from coordinator_core.percolate.rewrite_basename import (  # noqa: E402
    DirectoryRenamePairShapeError as _RealDirectoryRenamePairShapeError,
)

# `coordinator/tests/_repo_paths.py` centralizes the two-rung `setup/` directory
# resolution `process_target` needs for `publish_sync_module` -- reused here rather
# than hand-rolled again (that file's own docstring names five prior hand-rolled
# copies as the drift class it exists to prevent).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_COORDINATOR_TESTS = _REPO_ROOT / "coordinator" / "tests"
if str(_COORDINATOR_TESTS) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_TESTS))

from _repo_paths import real_setup_dir as _real_setup_dir  # noqa: E402

_PUBLISH_PY_PATH = _REPO_ROOT / "coordinator" / "bin" / "publish.py"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_renamed_dir_exemption_under_test", _PUBLISH_PY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_git_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)


def _make_ignore():
    return publish_sync.load_ignore(None)


class TestRenamedDirNamesExemption:
    def test_default_none_preserves_existing_orphan_removal(self, tmp_path, monkeypatch):
        """Omitting the parameter entirely must behave exactly as before its
        introduction -- an unrelated leftover directory is still removed as an
        orphan. `COORDINATOR_OVERRIDE_ORPHAN_SWEEP=1` is required here for a reason
        independent of this parameter: the pre-existing 2026-07-26 top-level-presence
        preflight FATAL-aborts on ANY orphan by default -- this is exactly the
        production hazard the `sync_mirror` docstring's Group-E note names (a real
        directory-rename row would trip this same preflight without BOTH this
        exemption AND that env override, or a further guard change, wired end to
        end)."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "stray").mkdir()
        (dst / "stray" / "leftover.txt").write_text("x", encoding="utf-8")

        publish_sync.sync_mirror(src, dst, _make_ignore(), dry_run=False)

        assert not (dst / "stray").exists()

    def test_renamed_dir_name_is_exempt_from_orphan_removal(self, tmp_path):
        """A destination directory the caller names via `renamed_dir_names` -- e.g.
        one the engine's own directory-rename primitive produced last pass -- must
        survive the orphan sweep even though no source directory shares its name."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "renamed-target").mkdir()
        (dst / "renamed-target" / "kept.txt").write_text("payload", encoding="utf-8")

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=False,
            renamed_dir_names=frozenset({"renamed-target"}),
        )

        assert (dst / "renamed-target").is_dir()
        assert (dst / "renamed-target" / "kept.txt").read_text(encoding="utf-8") == "payload"

    def test_exemption_does_not_shield_an_unrelated_directory(self, tmp_path, monkeypatch):
        """The exemption is name-scoped -- a DIFFERENT stray directory not named in
        `renamed_dir_names` is still reaped as an ordinary orphan. Same preflight
        override reason as the first test above."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", "1")
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "renamed-target").mkdir()
        (dst / "genuinely-stray").mkdir()

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=False,
            renamed_dir_names=frozenset({"renamed-target"}),
        )

        assert (dst / "renamed-target").is_dir()
        assert not (dst / "genuinely-stray").exists()

    def test_dry_run_never_deletes_an_unexempted_orphan_either(self, tmp_path):
        """Sanity check that the exemption plumbing did not disturb the pre-existing
        dry-run-never-deletes contract."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (dst / "stray").mkdir()

        publish_sync.sync_mirror(
            src, dst, _make_ignore(), dry_run=True,
            renamed_dir_names=frozenset(),
        )

        assert (dst / "stray").is_dir()


class _FakeRewriteBasenameModule:
    """Stands in for `coordinator_core.percolate.rewrite_basename`: exercises
    `process_target`'s ledger-load call-site contract (`rename_ledger_path` then
    `read_directory_rename_ledger`) without touching the real engine module or a
    real ledger file on disk."""

    # Real production exception type (not a duplicated stand-in) -- see the
    # module-level import above for why this is required and why the fake
    # never needs to raise it itself.
    DirectoryRenamePairShapeError = _RealDirectoryRenamePairShapeError

    def __init__(self, names=(), *, raise_on_read=None):
        self._names = list(names)
        self._raise_on_read = raise_on_read

    def rename_ledger_path(self, target_name, *, state_home=None):
        return Path(f"/fake-ledger/{target_name}.json")

    def read_directory_rename_ledger(self, ledger):
        if self._raise_on_read is not None:
            raise self._raise_on_read
        return list(self._names)


class _EngineSentinel:
    """Just needs to be non-`None` -- under `dry_run=True`, `process_target`
    never dispatches any engine phase against it (§ its own dry-run skip
    print), so this stands in wherever a real `FakeClaudeKlabauterPercolate` would be
    unnecessary machinery."""


class _FakeClaudeKlabauterPercolateForRealRun:
    """Minimal engine double for the `dry_run=False` leg of AC3 -- same shape
    as `test_publish_guard_before_mutate.py`'s `FakeClaudeKlabauterPercolate`, pared to
    only what a clean mirror-mode publish with no guard hooks touches."""

    def __init__(self):
        self.calls = []

    def load_store(self, path):
        return {"schema_version": 1, "base": {}, "targets": {}}

    def resolve_target(self, store, name):
        return {"hooks": [], "file_surface": {}, "guards": [], "inject": []}

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        self.calls.append({"target": target, "phase": phase})
        return {
            "phase": phase,
            "guard_results": [],
            "rename_manifest": None,
            "restored_native": [],
            "visited_files": [],
        }

    def run_inject_for_section(self, target_root, section, *, stdin=None, visited_sink=None):
        if stdin is not None:
            stdin.read()

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return {"ran": False, "skipped": True, "exit_code": None, "findings": ""}


def _mirror_target(tmp_path, name, *, dst_dirs=()):
    """A mirror-mode row: SOURCE is a real committed git work tree (required
    for the `dry_run=False` leg -- `process_target`'s non-dry-run path
    materializes every contributing root from its committed ref before
    syncing), DESTINATION is a plain directory pre-populated with `dst_dirs`
    top-level subdirectories (the candidates the orphan sweep evaluates)."""
    publish = _load_publish_module()
    src = tmp_path / f"{name}-src"
    _init_git_repo(src)
    (src / "source-file.md").write_text("source content")
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)

    dst = tmp_path / f"{name}-dst"
    _init_git_repo(dst)
    for dir_name in dst_dirs:
        (dst / dir_name).mkdir()

    return publish, publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


class TestProcessTargetLedgerExemption:
    """`process_target`'s `renamed_dir_names` ledger-load gate
    (docs/plans/2026-08-11-dry-run-cries-wolf-on-the-rename-exemption.md C1):
    preview and real run must compute the SAME exemption set from the row's
    rename-generation ledger, and a defensive load must degrade to an empty
    exemption set rather than raise."""

    def test_ac1_dry_run_ledger_exemption_suppresses_would_abort(self, tmp_path, monkeypatch, capsys):
        publish, target = _mirror_target(tmp_path, "ac1", dst_dirs=("renamed-target",))
        monkeypatch.setattr(
            publish,
            "_import_percolate_rewrite_basename_module",
            lambda: _FakeRewriteBasenameModule(names=["renamed-target"]),
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_EngineSentinel(), store={"targets": {}})
        totals = publish.RunTotals()
        setup_dir = _real_setup_dir()
        publish_sync_module = publish._import_publish_sync(setup_dir)
        out = io.StringIO()

        publish.process_target(
            target, setup_dir, totals,
            identity_file_exists=False, identity=None, dry_run=True,
            engine_ctx=engine_ctx, publish_sync_module=publish_sync_module, out=out,
        )

        # The top-level-presence WOULD ABORT warning prints straight to
        # `sys.stderr` (`sync_mirror`'s own hardcoded `file=sys.stderr`), not
        # the `out` sink `process_target` was handed -- capsys, not `out`.
        stderr_text = capsys.readouterr().err
        assert "WOULD ABORT" not in stderr_text
        assert "renamed-target" not in stderr_text

    def test_ac2_control_unexempted_dir_still_warns(self, tmp_path, monkeypatch, capsys):
        publish, target = _mirror_target(tmp_path, "ac2", dst_dirs=("genuinely-stray",))
        monkeypatch.setattr(
            publish,
            "_import_percolate_rewrite_basename_module",
            lambda: _FakeRewriteBasenameModule(names=["some-other-directory"]),
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_EngineSentinel(), store={"targets": {}})
        totals = publish.RunTotals()
        setup_dir = _real_setup_dir()
        publish_sync_module = publish._import_publish_sync(setup_dir)
        out = io.StringIO()

        publish.process_target(
            target, setup_dir, totals,
            identity_file_exists=False, identity=None, dry_run=True,
            engine_ctx=engine_ctx, publish_sync_module=publish_sync_module, out=out,
        )

        stderr_text = capsys.readouterr().err
        assert "WOULD ABORT" in stderr_text
        assert "genuinely-stray" in stderr_text

    def test_ac3_exemption_set_equal_between_dry_run_and_real_run(self, tmp_path, monkeypatch):
        captured = []
        publish, dry_target = _mirror_target(tmp_path, "ac3dry", dst_dirs=("renamed-target",))
        _, real_target = _mirror_target(tmp_path, "ac3real", dst_dirs=("renamed-target",))
        monkeypatch.setattr(
            publish,
            "_import_percolate_rewrite_basename_module",
            lambda: _FakeRewriteBasenameModule(names=["renamed-target"]),
        )
        real_dispatch_mirror_like = publish.dispatch_mirror_like

        def _spy_dispatch_mirror_like(*args, **kwargs):
            captured.append(kwargs.get("renamed_dir_names"))
            return real_dispatch_mirror_like(*args, **kwargs)

        monkeypatch.setattr(publish, "dispatch_mirror_like", _spy_dispatch_mirror_like)

        setup_dir = _real_setup_dir()
        publish_sync_module = publish._import_publish_sync(setup_dir)

        # dry-run leg
        dry_engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_EngineSentinel(), store={"targets": {}})
        publish.process_target(
            dry_target, setup_dir, publish.RunTotals(),
            identity_file_exists=False, identity=None, dry_run=True,
            engine_ctx=dry_engine_ctx, publish_sync_module=publish_sync_module, out=io.StringIO(),
        )

        # real-run leg
        real_engine_ctx = publish.PercolateEngineContext(
            engine_claude_klabauter=_FakeClaudeKlabauterPercolateForRealRun(), store={"targets": {}}
        )
        publish.process_target(
            real_target, setup_dir, publish.RunTotals(),
            identity_file_exists=False, identity=None, dry_run=False,
            engine_ctx=real_engine_ctx, percolate_store_path=tmp_path / "store.yaml",
            publish_sync_module=publish_sync_module, out=io.StringIO(),
        )

        assert len(captured) == 2
        assert captured[0] == captured[1] == frozenset({"renamed-target"})
        # AC1's counterpart on the real-run leg: the exempted directory
        # actually survived the real orphan sweep, not just the preview text.
        assert (real_target.dest_dir / "renamed-target").is_dir()

    def test_ac4_import_failure_degrades_to_empty_exemption_set(self, tmp_path, monkeypatch, capsys):
        publish, target = _mirror_target(tmp_path, "ac4", dst_dirs=("renamed-target",))

        def _raise_import():
            raise ImportError("no rewrite_basename module for this test")

        monkeypatch.setattr(publish, "_import_percolate_rewrite_basename_module", _raise_import)
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_EngineSentinel(), store={"targets": {}})
        totals = publish.RunTotals()
        setup_dir = _real_setup_dir()
        publish_sync_module = publish._import_publish_sync(setup_dir)
        out = io.StringIO()

        publish.process_target(
            target, setup_dir, totals,
            identity_file_exists=False, identity=None, dry_run=True,
            engine_ctx=engine_ctx, publish_sync_module=publish_sync_module, out=out,
        )

        # No raise (the assertion is that we got here at all) and the
        # unresolved exemption degrades to empty -- the directory is still
        # flagged, proving the fallback landed rather than silently applying
        # an exemption that was never actually loaded.
        assert "WOULD ABORT" in capsys.readouterr().err

        # Review: coordinator:code-reviewer -- the dry-run leg above alone does
        # not distinguish "this code path was never reached" (pre-fix, when the
        # ledger load was gated on `and not dry_run` and never ran under
        # `dry_run=True`) from "this code path was reached and degraded"
        # (post-fix). Only the real-run leg tells them apart: pre-fix, a real
        # run's ungated, un-caught import raise propagated and crashed the
        # publish loudly; post-fix, the same raise is caught and degrades to an
        # empty exemption set, so the now-unexempted directory trips the
        # top-level-presence preflight's real-run branch (`FATAL` + `SystemExit(3)`,
        # not the dry-run `WOULD ABORT` warning) -- proving the catch actually
        # fired on the real-run path rather than merely on dry-run's.
        real_publish, real_target = _mirror_target(tmp_path, "ac4real", dst_dirs=("renamed-target",))
        monkeypatch.setattr(
            real_publish, "_import_percolate_rewrite_basename_module", _raise_import
        )
        real_engine_ctx = real_publish.PercolateEngineContext(
            engine_claude_klabauter=_FakeClaudeKlabauterPercolateForRealRun(), store={"targets": {}}
        )
        with pytest.raises(SystemExit) as exc_info:
            real_publish.process_target(
                real_target, setup_dir, real_publish.RunTotals(),
                identity_file_exists=False, identity=None, dry_run=False,
                engine_ctx=real_engine_ctx, percolate_store_path=tmp_path / "ac4-store.yaml",
                publish_sync_module=publish_sync_module, out=io.StringIO(),
            )
        assert exc_info.value.code == 3
        assert "FATAL" in capsys.readouterr().err

    def test_ac5_unreadable_ledger_degrades_to_empty_exemption_set(self, tmp_path, monkeypatch, capsys):
        """`raise_on_read=OSError(...)` exercises the NEW call-site `try/except`
        in `process_target`, not a reachable failure of the real ledger reader:
        `read_directory_rename_ledger` (coordinator_core/percolate/
        rewrite_basename.py) already swallows `(OSError, ValueError)` internally
        and returns `[]`, so it never raises this against the real
        implementation. This fake simulates the call-site catch as
        belt-and-suspenders defense-in-depth against a future change to that
        reader, not a live gap in today's code (§ code review finding, docs/
        plans/2026-08-11-dry-run-cries-wolf-on-the-rename-exemption.md)."""
        publish, target = _mirror_target(tmp_path, "ac5", dst_dirs=("renamed-target",))
        monkeypatch.setattr(
            publish,
            "_import_percolate_rewrite_basename_module",
            lambda: _FakeRewriteBasenameModule(raise_on_read=OSError("unreadable ledger")),
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_EngineSentinel(), store={"targets": {}})
        totals = publish.RunTotals()
        setup_dir = _real_setup_dir()
        publish_sync_module = publish._import_publish_sync(setup_dir)
        out = io.StringIO()

        publish.process_target(
            target, setup_dir, totals,
            identity_file_exists=False, identity=None, dry_run=True,
            engine_ctx=engine_ctx, publish_sync_module=publish_sync_module, out=out,
        )

        assert "WOULD ABORT" in capsys.readouterr().err

        # Review: coordinator:code-reviewer -- real-run leg, same rationale as
        # AC4's addition above: only this leg proves the catch fired on the
        # path that used to run uncaught before this commit.
        real_publish, real_target = _mirror_target(tmp_path, "ac5real", dst_dirs=("renamed-target",))
        monkeypatch.setattr(
            real_publish,
            "_import_percolate_rewrite_basename_module",
            lambda: _FakeRewriteBasenameModule(raise_on_read=OSError("unreadable ledger")),
        )
        real_engine_ctx = real_publish.PercolateEngineContext(
            engine_claude_klabauter=_FakeClaudeKlabauterPercolateForRealRun(), store={"targets": {}}
        )
        with pytest.raises(SystemExit) as exc_info:
            real_publish.process_target(
                real_target, setup_dir, real_publish.RunTotals(),
                identity_file_exists=False, identity=None, dry_run=False,
                engine_ctx=real_engine_ctx, percolate_store_path=tmp_path / "ac5-store.yaml",
                publish_sync_module=publish_sync_module, out=io.StringIO(),
            )
        assert exc_info.value.code == 3
        assert "FATAL" in capsys.readouterr().err

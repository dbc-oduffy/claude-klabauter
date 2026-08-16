"""test_percolate_liveops_preflight.py — unit tests for
coordinator/bin/percolate-liveops-preflight's engine-resolution discriminant
and verdict line (docs/plans/2026-08-15-klabauter-release-channels.md, C11).

Fixture-based via a stub of the CLI's own `_import_modules` seam — mirrors
test_session_liveness_cli.py's `stub_import_module` idiom. Never depends on
this machine's real live peer list, never requires CLAUDE_KLABAUTER_ROOT to resolve,
never spawns `git rev-parse`.

Pins:
  - AC: a live session in a registered `engine.working_repos.*` repo is
    reported "unaffected" and does not count toward the verdict's N.
  - AC: a live session in a repo NOT in `engine.working_repos.*` is
    reported "AFFECTED" and counts toward the verdict's N.
  - AC: two registry aliases naming the SAME on-disk path are censused
    once, not twice (no double-counted verdict).
  - AC: the zero-affected case ("affects 0 of N") reads as a normal,
    unremarkable report line, not a special-cased/broken-looking one —
    this repo's own machine is expected to read this way today.
  - AC: no live sessions anywhere -> "affects 0 of 0", not a crash or a
    div-by-zero.

Loaded by file path (`importlib.machinery.SourceFileLoader`) — same load
idiom as test_session_liveness_cli.py's `_load_cli_module`.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys

from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "percolate_liveops_preflight", str(_BIN_DIR / "percolate-liveops-preflight.py")
    )
    spec = importlib.util.spec_from_loader("percolate_liveops_preflight", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class _PeerRow:
    def __init__(self, session_id: str, address: "str | None"):
        self.session_id = session_id
        self.address = address


class _StubLiveness:
    """Stand-in for coordinator_core.session.liveness — `live_ids_by_cwd` and
    `lines_by_cwd` key on the exact `cwd` the CLI passes."""

    def __init__(self, live_ids_by_cwd: dict, lines_by_cwd: "dict | None" = None):
        self._live_ids_by_cwd = live_ids_by_cwd
        self._lines_by_cwd = lines_by_cwd or {}

    def live_session_ids(self, cwd=None):
        return frozenset(self._live_ids_by_cwd.get(cwd, []))

    def active_sessions(self, cwd=None):
        return self._lines_by_cwd.get(cwd, [f"{sid}  Live (stub)" for sid in self._live_ids_by_cwd.get(cwd, [])])


class _StubPeerRoster:
    def __init__(self, rows_by_cwd: "dict | None" = None):
        self._rows_by_cwd = rows_by_cwd or {}

    def build_roster(self, repo_root, raise_on_failure=False):
        return self._rows_by_cwd.get(repo_root, [])


class _StubMachineResolver:
    """Stand-in for coordinator_core.machine_resolver — `registry_dir()`
    returns an opaque sentinel; `load_flat_registry_file(path)` looks the
    per-file flat dict up by `path.name`, mirroring the real module's
    two-file (tracked, local) merge without touching any real TOML."""

    def __init__(self, tracked: "dict | None" = None, local: "dict | None" = None):
        self._files = {
            "registry.toml": tracked or {},
            "registry.local.toml": local or {},
        }

    def registry_dir(self):
        return Path("/stub-registry-dir")

    def load_flat_registry_file(self, path):
        return dict(self._files.get(Path(path).name, {}))


def _patch_import_modules(monkeypatch, liveness, peer_roster, machine_resolver):
    monkeypatch.setattr(_cli, "_import_modules", lambda: (liveness, peer_roster, machine_resolver))


def _run(monkeypatch, capsys, *, cwd, tracked_registry, live_ids_by_cwd, lines_by_cwd=None, rows_by_cwd=None):
    monkeypatch.chdir(cwd)
    liveness = _StubLiveness(live_ids_by_cwd, lines_by_cwd)
    peer_roster = _StubPeerRoster(rows_by_cwd)
    machine_resolver = _StubMachineResolver(tracked=tracked_registry)
    _patch_import_modules(monkeypatch, liveness, peer_roster, machine_resolver)
    rc = _cli.main(["run"])
    out = capsys.readouterr().out
    return rc, out


def test_working_repo_session_reported_unaffected_and_excluded_from_verdict(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "engine-working-repo"
    repo.mkdir()
    registry = {
        "repos.working_one": str(repo),
        "engine.working_repos.working_one": str(repo),
    }
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-working"]},
    )
    assert rc == 0
    assert "unaffected" in out
    assert "AFFECTED" not in out
    assert "verdict: affects 0 of 1 live sessions." in out


def test_non_working_repo_session_reported_affected_and_counted(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "published-mirror-consumer"
    repo.mkdir()
    registry = {
        "repos.consumer_one": str(repo),
        # No engine.working_repos.* entry for this repo -- it resolves the
        # published mirror by construction.
    }
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-consumer"]},
    )
    assert rc == 0
    assert "AFFECTED" in out
    assert "verdict: affects 1 of 1 live sessions." in out


def test_addressable_session_prints_its_resolved_address(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "consumer-with-address"
    repo.mkdir()
    registry = {"repos.consumer_two": str(repo)}
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-addressable"]},
        rows_by_cwd={str(repo): [_PeerRow("sid-addressable", "consumer-01 [abc123]")]},
    )
    assert rc == 0
    assert "address=consumer-01 [abc123]" in out
    assert "address=not addressable" not in out


def test_unaddressable_session_names_the_gap_not_a_guess(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "consumer-without-address"
    repo.mkdir()
    registry = {"repos.consumer_three": str(repo)}
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-unaddressable"]},
        rows_by_cwd={},
    )
    assert rc == 0
    assert "address=not addressable" in out


def test_two_registry_aliases_of_same_path_censused_once(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "aliased-repo"
    repo.mkdir()
    registry = {
        "repos.alias_a": str(repo),
        "repos.alias_b": str(repo),
        "engine.working_repos.alias_a": str(repo),
    }
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-aliased"]},
    )
    assert rc == 0
    # A double-counted census would report this session twice under two
    # different repo names, and the verdict denominator would read 2.
    assert out.count("sid-aliased") == 1
    assert "verdict: affects 0 of 1 live sessions." in out


def test_module_docstring_is_populated():
    """Review: coordinatorcode-reviewer-eb287fb6 -- `from __future__ import
    annotations` used to precede the module docstring, demoting it to a
    dead expression that never became `__doc__`. Pins that it now does."""
    assert _cli.__doc__ is not None
    assert "REPORT, not a gate" in _cli.__doc__


def test_two_registry_aliases_with_path_shape_variance_censused_once(tmp_path, monkeypatch, capsys):
    """Same on-disk repo aliased via a case variant and a trailing
    separator -- covers the case/separator/trailing-slash variance
    `_normalize_path`'s realpath+normpath+normcase pipeline exists to
    handle, distinct from `test_two_registry_aliases_of_same_path_censused_once`
    above which only exercises byte-identical duplicate paths."""
    repo = tmp_path / "case-variant-repo"
    repo.mkdir()
    canonical = str(repo)
    trailing = canonical + os.sep

    if sys.platform.startswith("win") or sys.platform == "darwin":
        # Case-insensitive filesystem by default -- an uppercased path
        # names the SAME on-disk repo, exercising `normcase`.
        cased = canonical.upper()
    else:
        # POSIX filesystems are case-sensitive by default -- an
        # uppercased path would name a DIFFERENT (nonexistent) directory,
        # not a variant of this one, so casing isn't portably exercisable
        # here. Trailing-separator variance still is, below.
        cased = canonical

    # "alias_case" sorts before "alias_trailing" -- it is the preferred
    # (real, non-"_this_repo") alias name, and its literal path (`cased`)
    # is what the CLI passes as `cwd` to the liveness stub.
    registry = {
        "repos.alias_case": cased,
        "repos.alias_trailing": trailing,
        "engine.working_repos.alias_case": canonical,
    }
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={cased: ["sid-case-variant"]},
    )
    assert rc == 0
    assert out.count("sid-case-variant") == 1
    assert "verdict: affects 0 of 1 live sessions." in out


def test_zero_affected_case_reads_as_a_normal_unremarkable_line(tmp_path, monkeypatch, capsys):
    """AC (negative spec): "affects 0 of N" on an all-engine-working box is
    a correct, ordinary output — not a special-cased or broken-looking one."""
    repo_a = tmp_path / "working-a"
    repo_b = tmp_path / "working-b"
    repo_a.mkdir()
    repo_b.mkdir()
    registry = {
        "repos.a": str(repo_a),
        "repos.b": str(repo_b),
        "engine.working_repos.a": str(repo_a),
        "engine.working_repos.b": str(repo_b),
    }
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo_a): ["sid-a"], str(repo_b): ["sid-b"]},
    )
    assert rc == 0
    assert "verdict: affects 0 of 2 live sessions." in out
    assert "AFFECTED" not in out


def test_no_live_sessions_anywhere_reports_zero_of_zero(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "idle-repo"
    repo.mkdir()
    registry = {"repos.idle_one": str(repo)}
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={},
    )
    assert rc == 0
    assert "verdict: affects 0 of 0 live sessions." in out
    assert "no live sessions found" in out


def test_never_exits_nonzero_on_a_nonzero_verdict(tmp_path, monkeypatch, capsys):
    """Negative spec, hard boundary: this is a report, never a gate — a
    verdict with affected > 0 must still exit 0."""
    repo = tmp_path / "affected-repo"
    repo.mkdir()
    registry = {"repos.affected_one": str(repo)}
    rc, out = _run(
        monkeypatch,
        capsys,
        cwd=tmp_path,
        tracked_registry=registry,
        live_ids_by_cwd={str(repo): ["sid-x", "sid-y"]},
    )
    assert rc == 0
    assert "verdict: affects 2 of 2 live sessions." in out


def test_unknown_subcommand_exits_2(capsys):
    rc = _cli.main(["bogus-subcommand"])
    assert rc == 2


def test_help_flag_exits_0_without_importing_modules(monkeypatch, capsys):
    def _boom():
        raise AssertionError("help must not import coordinator_core modules")

    monkeypatch.setattr(_cli, "_import_modules", _boom)
    rc = _cli.main(["--help"])
    assert rc == 0


def test_claude_klabauter_root_resolution_failure_is_transport_fail(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT not resolvable")

    monkeypatch.setattr(_cli, "_import_modules", _boom)
    rc = _cli.main(["run"])
    assert rc == _cli._TRANSPORT_FAIL

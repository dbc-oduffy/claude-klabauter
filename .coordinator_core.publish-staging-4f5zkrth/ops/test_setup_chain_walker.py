"""
Co-located pytest for coordinator_core.ops.setup_chain_walker.

Independently re-derives parity with the bash oracle rather than
re-transcribing it: each test builds its own fixture manifest/repo-root and
asserts on this module's *own* documented exit-code contract and
NDJSON/table shape, not on captured oracle output.

Port of: setup.sh (DoE 6fb5fb37, 2026-07-22).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import setup_chain_walker as scw
from coordinator_core.testing.doe_root import resolve_doe_root

# Declared, not excused: the override-pair (exit 93) and trampoline-
# transport-failure tests spawn a real `sys.executable` process because the
# property under test is real `os.environ` isolation across a process
# boundary -- deliberately "in-subprocess so os.environ isolation is real,
# not monkeypatched" (see comment above `_make_repo_root`). No mock stands
# in for that. This is the only spawn shape in the file (isolated to those
# two call sites), so it is left as-is rather than hoisted. The spawn
# ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def test_help_exits_0_and_prints_usage(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Usage: python3 scripts/setup.sh" in out
    assert "94=semi-hard-git-auth-unverified" in out


def test_version_exits_0(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--version"])
    assert exc_info.value.code == 0
    assert "coordinator-claude setup version 1.0.0" in capsys.readouterr().out


def test_unknown_arg_exits_1(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--bogus"])
    assert exc_info.value.code == 1
    assert "Unknown argument: --bogus" in capsys.readouterr().err


def test_phase_seed_install_spinoff_is_dag_root_noop(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--phase", "seed-install-spinoff"])
    assert exc_info.value.code == 0
    assert "seeds no leg baton for itself" in capsys.readouterr().out


def test_phase_unknown_exits_1(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--phase", "not-a-real-phase"])
    assert exc_info.value.code == 1
    assert "Unknown --phase value: 'not-a-real-phase'" in capsys.readouterr().err


def test_phase_missing_name_exits_1(capsys):
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--phase"])
    assert exc_info.value.code == 1


def test_phase_flag_forgotten_name_exits_1(capsys):
    # Edge the oracle explicitly guards: --phase followed by another flag.
    with pytest.raises(scw._SetupError) as exc_info:
        scw.parse_args(["--phase", "--check"])
    assert exc_info.value.code == 1
    assert "Did you forget the phase name?" in capsys.readouterr().err


def test_phase_chain_preinstall_sets_flag_and_does_not_exit():
    flags = scw.parse_args(["--phase", "chain-preinstall"])
    assert flags["run_chain_preinstall"] is True


# ---------------------------------------------------------------------------
# _self_resolve_walker_roots — `python3 -m coordinator_core.ops.
# setup_chain_walker` entry point fallback used when neither
# COORDINATOR_SETUP_REPO_ROOT nor COORDINATOR_SETUP_LIB_DIR is set.
# Review: code-reviewer (Finding 5, 2026-08-03) — every existing test sets
# both env vars, leaving this branch entirely uncovered.
# ---------------------------------------------------------------------------

def _add_coordinator_claude_source_evidence(tree: Path) -> None:
    """Positive evidence `_looks_like_coordinator_claude_source` requires —
    `.claude-plugin/plugin.json` PLUS a `commands/` dir."""
    (tree / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tree / ".claude-plugin" / "plugin.json").write_text("{}")
    (tree / "commands").mkdir(parents=True, exist_ok=True)


def test_self_resolve_walker_roots_success_via_flag(tmp_path):
    # Defect B fix: the root comes from the override ladder
    # (--coordinator-root / $COORDINATOR_CLAUDE_ROOT), never a guess
    # derived from this module's own on-disk location, and (Defect fix
    # 2026-08-07) never a registered publish mirror.
    coordinator_tree = tmp_path / "coordinator-claude-checkout"
    coordinator_tree.mkdir()
    _add_coordinator_claude_source_evidence(coordinator_tree)
    resolved = scw._self_resolve_walker_roots(["--coordinator-root", str(coordinator_tree)])
    assert resolved is not None
    repo_root, lib_dir, rung = resolved
    assert repo_root == coordinator_tree
    assert lib_dir == repo_root / "scripts" / "lib"
    assert rung == "--coordinator-root flag"


def test_self_resolve_walker_roots_success_via_env(tmp_path, monkeypatch):
    coordinator_tree = tmp_path / "coordinator-claude-checkout"
    coordinator_tree.mkdir()
    _add_coordinator_claude_source_evidence(coordinator_tree)
    monkeypatch.setenv("COORDINATOR_CLAUDE_ROOT", str(coordinator_tree))
    resolved = scw._self_resolve_walker_roots([])
    assert resolved is not None
    assert resolved[0] == coordinator_tree
    assert resolved[2] == "$COORDINATOR_CLAUDE_ROOT env"


def test_self_resolve_walker_roots_returns_none_when_ladder_unresolved(monkeypatch):
    # No --coordinator-root, no env var: the registry rung was DROPPED
    # (Defect fix 2026-08-07 — repos.coordinator_claude is retired and
    # names the publish mirror), so there is nothing left to monkeypatch;
    # an empty ladder must report None so the caller emits a real
    # diagnostic rather than proceeding on a guessed root (Defect B's root
    # cause).
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    assert scw._self_resolve_walker_roots([]) is None


def test_self_resolve_walker_roots_returns_none_when_override_path_absent(tmp_path):
    # An override that resolves to a syntactically valid but nonexistent
    # path must also report None, not a stale/unverified root.
    ghost = tmp_path / "does-not-exist"
    assert scw._self_resolve_walker_roots(["--coordinator-root", str(ghost)]) is None


def test_self_resolve_walker_roots_returns_none_when_no_source_evidence(tmp_path):
    # Requirement 2: a bare directory (no .claude-plugin/plugin.json, no
    # commands/ or hooks/) is not accepted even though it exists on disk.
    bare = tmp_path / "just-a-directory"
    bare.mkdir()
    assert scw._self_resolve_walker_roots(["--coordinator-root", str(bare)]) is None


def test_resolve_coordinator_root_ladder_rejects_registered_publish_mirror(tmp_path, monkeypatch):
    # Requirement 1: a candidate that resolves to a registered
    # publish.mirrors.*.path entry is rejected outright, even though it may
    # otherwise look like a valid coordinator-claude source checkout (a
    # publish mirror ships the same plugin-manifest shape as a real source
    # clone — see scripts/setup.py's _looks_like_coordinator_claude_source
    # docstring for why plugin.json alone can't tell them apart).
    mirror = tmp_path / "coordinator-claude-mirror"
    mirror.mkdir()
    _add_coordinator_claude_source_evidence(mirror)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: str(Path(target_root).resolve()) == str(mirror.resolve()),
    )
    assert scw._resolve_coordinator_root_ladder(["--coordinator-root", str(mirror)]) is None


def test_resolve_coordinator_root_ladder_noop_when_no_mirrors_registered(tmp_path):
    # OSS case: no publish mirrors registered anywhere -- the mirror check
    # must be a pure no-op, never a new failure mode, when the underlying
    # registry lookup itself raises/is unavailable.
    coordinator_tree = tmp_path / "coordinator-claude-checkout"
    coordinator_tree.mkdir()
    _add_coordinator_claude_source_evidence(coordinator_tree)
    resolved = scw._resolve_coordinator_root_ladder(["--coordinator-root", str(coordinator_tree)])
    assert resolved is not None
    assert resolved[0] == coordinator_tree


# ---------------------------------------------------------------------------
# Rung 3 — engine.working_repos.doe_claude registry key (C1b).
# ---------------------------------------------------------------------------

def test_resolve_coordinator_root_ladder_rung3_resolves_via_registry(tmp_path, monkeypatch):
    # The raw registered value is one directory ABOVE the plugin source
    # (verified live) -- the derivation (C1a's
    # _resolve_plugin_root_for_machine_local) is what makes it resolve to
    # the actual `<value>/coordinator` checkout, which carries the
    # positive-evidence shape.
    doe_root = tmp_path / "doe-claude"
    plugin_root = doe_root / "coordinator"
    plugin_root.mkdir(parents=True)
    _add_coordinator_claude_source_evidence(plugin_root)
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    (plugin_root / "templates" / "bin" / "_machine_local.py").write_text("")

    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(scw, "registry_get", lambda key: str(doe_root) if key == "engine.working_repos.doe_claude" else None)

    resolved = scw._resolve_coordinator_root_ladder([])
    assert resolved is not None
    assert resolved == (plugin_root, "engine.working_repos.doe_claude registry key")


def test_resolve_coordinator_root_ladder_rung3_rejects_publish_mirror(tmp_path, monkeypatch):
    doe_root = tmp_path / "doe-claude"
    plugin_root = doe_root / "coordinator"
    plugin_root.mkdir(parents=True)
    _add_coordinator_claude_source_evidence(plugin_root)
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    (plugin_root / "templates" / "bin" / "_machine_local.py").write_text("")

    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(scw, "registry_get", lambda key: str(doe_root) if key == "engine.working_repos.doe_claude" else None)
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_publish_destination",
        lambda target_root, env=None: str(Path(target_root).resolve()) == str(plugin_root.resolve()),
    )

    assert scw._resolve_coordinator_root_ladder([]) is None


def test_resolve_coordinator_root_ladder_rung3_rejects_missing_positive_evidence(tmp_path, monkeypatch):
    doe_root = tmp_path / "doe-claude"
    doe_root.mkdir()
    # No `.claude-plugin/plugin.json` anywhere under `doe_root` -- the
    # derivation returns None (no templates/bin/_machine_local.py, no
    # plugin.json at either candidate shape), so the rung is a miss.
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(scw, "registry_get", lambda key: str(doe_root) if key == "engine.working_repos.doe_claude" else None)

    assert scw._resolve_coordinator_root_ladder([]) is None


def test_resolve_coordinator_root_ladder_rung3_fail_open_when_key_absent(monkeypatch):
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(scw, "registry_get", lambda key: None)

    assert scw._resolve_coordinator_root_ladder([]) is None


def test_resolve_coordinator_root_ladder_flag_and_env_outrank_registry(tmp_path, monkeypatch):
    doe_root = tmp_path / "doe-claude"
    plugin_root = doe_root / "coordinator"
    plugin_root.mkdir(parents=True)
    _add_coordinator_claude_source_evidence(plugin_root)
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    (plugin_root / "templates" / "bin" / "_machine_local.py").write_text("")
    monkeypatch.setattr(scw, "registry_get", lambda key: str(doe_root) if key == "engine.working_repos.doe_claude" else None)

    flag_tree = tmp_path / "flag-checkout"
    flag_tree.mkdir()
    _add_coordinator_claude_source_evidence(flag_tree)
    resolved = scw._resolve_coordinator_root_ladder(["--coordinator-root", str(flag_tree)])
    assert resolved == (flag_tree, "--coordinator-root flag")

    env_tree = tmp_path / "env-checkout"
    env_tree.mkdir()
    _add_coordinator_claude_source_evidence(env_tree)
    monkeypatch.setenv("COORDINATOR_CLAUDE_ROOT", str(env_tree))
    resolved = scw._resolve_coordinator_root_ladder([])
    assert resolved == (env_tree, "$COORDINATOR_CLAUDE_ROOT env")


def test_main_self_resolves_roots_via_coordinator_root_flag(monkeypatch, capsys, tmp_path):
    # Success path through main(): neither trampoline env var set, but
    # --coordinator-root resolves a real (fixture) checkout, so self-
    # resolution succeeds and --check proceeds past the roots-resolution
    # branch instead of hitting the "cannot resolve" diagnostic.
    monkeypatch.delenv("COORDINATOR_SETUP_REPO_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_SETUP_LIB_DIR", raising=False)
    monkeypatch.setenv("NON_INTERACTIVE", "true")
    repo_root = _make_repo_root(tmp_path, direct_deps=[])
    _add_coordinator_claude_source_evidence(repo_root)
    code = scw.main(["--check", "--coordinator-root", str(repo_root)])
    out = capsys.readouterr().out
    assert "cannot resolve the chain-walker roots" not in out
    assert "root rung:    --coordinator-root flag" in out
    assert code == 0


def test_main_reports_diagnostic_and_exits_1_when_self_resolve_fails(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_SETUP_REPO_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_SETUP_LIB_DIR", raising=False)
    monkeypatch.setattr(scw, "_self_resolve_walker_roots", lambda argv=None, err=None: None)
    code = scw.main(["--check"])
    err = capsys.readouterr().err
    assert code == 1
    assert "cannot resolve the chain-walker roots" in err
    assert "--coordinator-root" in err
    assert "COORDINATOR_CLAUDE_ROOT" in err
    assert "repos.coordinator_claude" in err


# ---------------------------------------------------------------------------
# Override-flag pair integrity (exit 93) — exercised via main() in-subprocess
# so os.environ isolation is real, not monkeypatched.
# ---------------------------------------------------------------------------

def _make_repo_root(tmp_path: Path, direct_deps: list | None = None) -> Path:
    repo_root = tmp_path / "coordinator"
    (repo_root / "scripts" / "lib").mkdir(parents=True)
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"direct_deps": direct_deps or []}
    (manifest_dir / "agent-install-manifest.json").write_text(json.dumps(manifest))
    return repo_root


def _run_main(repo_root: Path, argv: list[str], env_extra: dict | None = None):
    env = dict(os.environ)
    env["COORDINATOR_SETUP_REPO_ROOT"] = str(repo_root)
    env["COORDINATOR_SETUP_LIB_DIR"] = str(repo_root / "scripts" / "lib")
    env.pop("COORDINATOR_RUN_MODE", None)
    env.pop("COORDINATOR_CHAIN_PREINSTALL_CONSENT", None)
    env["NON_INTERACTIVE"] = "true"
    if env_extra:
        env.update(env_extra)
    module_path = str(Path(scw.__file__).parent.parent.parent)
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from coordinator_core.ops.setup_chain_walker import main; "
        "sys.exit(main(sys.argv[1:]))" % module_path
    )
    res = subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=_CREATIONFLAGS,
    )
    return res


def test_override_pair_incomplete_skip_only_exits_93(tmp_path):
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(repo_root, ["--skip-dep-check"])
    assert res.returncode == 93
    assert "requires --accept-missing-deps-risk" in res.stderr


def test_override_pair_incomplete_accept_only_exits_93(tmp_path):
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(repo_root, ["--accept-missing-deps-risk"])
    assert res.returncode == 93


def test_check_mode_is_pair_exempt(tmp_path):
    # --check is read-only; override-pair check does not apply to it at all.
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(repo_root, ["--check", "--skip-dep-check"])
    assert res.returncode == 0


# ---------------------------------------------------------------------------
# Agent-direct short-circuit (exit 92)
# ---------------------------------------------------------------------------

def test_agent_direct_without_override_pair_exits_92(tmp_path):
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(repo_root, ["--i-am-agent"])
    assert res.returncode == 92
    assert "AGENT_MANIFEST_PATH=docs/install/AGENT.md" in res.stderr


def test_agent_direct_check_mode_still_hits_early_agent_gate(tmp_path):
    # The EARLY agent-direct short-circuit in main() fires before the
    # --check dispatch branch regardless of --check being read-only —
    # matches the oracle's ordering (agent-direct gate precedes --check).
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(repo_root, ["--i-am-agent", "--check"])
    assert res.returncode == 92


def test_agent_direct_with_chain_preinstall_consent_token_falls_through(tmp_path):
    repo_root = _make_repo_root(tmp_path)
    res = _run_main(
        repo_root,
        ["--i-am-agent", "--phase", "chain-preinstall"],
        env_extra={"COORDINATOR_CHAIN_PREINSTALL_CONSENT": "some-token"},
    )
    assert res.returncode == 0
    assert "nothing to preinstall" in res.stdout


def test_agent_direct_override_pair_reaches_full_install_body_not_reblocked(tmp_path):
    # Review: code-reviewer (Finding 1, 2026-07-17) regression test — a full
    # (non-check, non-preflight, non-chain-preinstall) install invocation
    # carrying the documented override pair must reach the install body, not
    # get re-blocked at exit 92 by run_mode_prompt's own unconditional
    # i_am_agent check (the bug: run_mode_prompt had no override_pair
    # awareness, so it re-raised exit 92 even after the top-level
    # agent-direct short-circuit had already admitted the invocation).
    repo_root = _make_repo_root(tmp_path, direct_deps=[])
    res = _run_main(
        repo_root,
        ["--i-am-agent", "--skip-dep-check", "--accept-missing-deps-risk"],
    )
    assert res.returncode != 92
    assert "Agent-direct invocation detected" not in res.stderr


# ---------------------------------------------------------------------------
# --check mode: DAG-root (empty direct_deps)
# ---------------------------------------------------------------------------

def test_check_dag_root_zero_deps_exits_0(tmp_path):
    repo_root = _make_repo_root(tmp_path, direct_deps=[])
    res = _run_main(repo_root, ["--check"])
    assert res.returncode == 0
    assert "chain walk complete — coordinator-claude is DAG root" in res.stdout


def test_check_hard_dep_missing_exits_90(tmp_path):
    dep = {
        "id": "some-hard-dep",
        "severity": "hard",
        "sibling_dir_name": "does-not-exist-anywhere",
        "upstream_url": "https://example.invalid/repo.git",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    repo_root = _make_repo_root(tmp_path, direct_deps=[dep])
    res = _run_main(repo_root, ["--check"])
    assert res.returncode == 90
    assert "hard dep [some-hard-dep] is missing" in res.stderr


def test_check_hard_dep_present_but_broken_exits_nonzero(tmp_path):
    # Defect A: a hard dep that IS present but fails its own functional
    # probe must exit non-zero (exit 1 — "hard probe failure"), never fall
    # through to "all deps satisfied" the way a bare status-branch with no
    # severity handling used to.
    sib = tmp_path / "broken-sib"
    sib.mkdir()
    dep = {
        "id": "broken-hard-dep",
        "severity": "hard",
        "sibling_dir_name": "broken-sib",
        "upstream_url": "https://example.invalid/repo.git",
        "functional_probe": {"kind": "file_exists", "path": "README.md"},
    }
    repo_root = _make_repo_root(tmp_path, direct_deps=[dep])
    res = _run_main(repo_root, ["--check"])
    assert res.returncode == 1
    assert "hard dep [broken-hard-dep] is present but its functional probe failed" in res.stderr
    assert "all deps satisfied" not in res.stdout


def test_check_soft_dep_missing_warns_and_continues(tmp_path):
    dep = {
        "id": "some-soft-dep",
        "severity": "soft",
        "sibling_dir_name": "does-not-exist-anywhere",
        "upstream_url": "https://example.invalid/repo.git",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    repo_root = _make_repo_root(tmp_path, direct_deps=[dep])
    res = _run_main(repo_root, ["--check"])
    assert res.returncode == 0
    assert "soft dep [some-soft-dep] is absent" in res.stderr
    assert "soft dep(s) absent or present-but-broken — proceeding" in res.stdout


# ---------------------------------------------------------------------------
# dep_probe — functional probe kinds (native port; independent re-derivation)
# ---------------------------------------------------------------------------

def _repo_with_manifest(sibling_root: Path, dep: dict) -> tuple[Path, Path]:
    repo_root = sibling_root / "coordinator"
    (repo_root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps({"direct_deps": [dep]}))
    return repo_root, manifest_path


def test_dep_probe_sibling_dir_exists_present(tmp_path):
    (tmp_path / "some-sib").mkdir()
    dep = {
        "id": "d1",
        "severity": "hard",
        "sibling_dir_name": "some-sib",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)
    assert scw.dep_probe("d1", manifest_path, repo_root) == "present"


def test_dep_probe_flat_shape_backward_compat(tmp_path):
    """A dep already carrying the pre-normalization flat
    ``functional_probe_kind``/``functional_probe_args`` shape (no nested
    ``functional_probe`` key) must still probe correctly —
    ``_normalize_dep`` passes it through untouched rather than assuming
    every caller has moved to the nested manifest schema."""
    (tmp_path / "some-sib-flat").mkdir()
    dep = {
        "id": "d1-flat",
        "severity": "hard",
        "sibling_dir_name": "some-sib-flat",
        "functional_probe_kind": "sibling_dir_exists",
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)
    assert scw.dep_probe("d1-flat", manifest_path, repo_root) == "present"


def test_dep_probe_sibling_missing(tmp_path):
    dep = {
        "id": "d2",
        "severity": "hard",
        "sibling_dir_name": "totally-absent",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)
    assert scw.dep_probe("d2", manifest_path, repo_root) == "missing"


def test_dep_probe_file_exists_present_but_broken_then_present(tmp_path):
    (tmp_path / "sib3").mkdir()
    dep = {
        "id": "d3",
        "severity": "hard",
        "sibling_dir_name": "sib3",
        "functional_probe": {"kind": "file_exists", "path": "README.md"},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)

    # README.md does not exist under sib3 → present-but-broken.
    assert scw.dep_probe("d3", manifest_path, repo_root) == "present-but-broken"

    (tmp_path / "sib3" / "README.md").write_text("hi")
    assert scw.dep_probe("d3", manifest_path, repo_root) == "present"


def test_dep_probe_file_exists_any(tmp_path):
    (tmp_path / "sib3b").mkdir()
    (tmp_path / "sib3b" / "second.txt").write_text("hi")
    dep_first_present = {
        "id": "d3b-first",
        "severity": "hard",
        "sibling_dir_name": "sib3b",
        "functional_probe": {"kind": "file_exists_any", "paths": ["first.txt", "second.txt"]},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep_first_present)
    assert scw.dep_probe("d3b-first", manifest_path, repo_root) == "present"

    dep_second_only = {
        "id": "d3b-second",
        "severity": "hard",
        "sibling_dir_name": "sib3b",
        "functional_probe": {"kind": "file_exists_any", "paths": ["absent.txt", "second.txt"]},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep_second_only)
    assert scw.dep_probe("d3b-second", manifest_path, repo_root) == "present"

    dep_none_present = {
        "id": "d3b-none",
        "severity": "hard",
        "sibling_dir_name": "sib3b",
        "functional_probe": {"kind": "file_exists_any", "paths": ["absent.txt", "also-absent.txt"]},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep_none_present)
    assert scw.dep_probe("d3b-none", manifest_path, repo_root) == "present-but-broken"


def test_dep_probe_nested_manifest_shape_normalizes(tmp_path, capsys):
    """Regression pin for the 2026-07-22 always-'' bug: the REAL manifest
    schema nests probes under ``functional_probe: {kind, ...}`` (see
    docs/install/agent-install-manifest.json on disk), not a flat
    ``functional_probe_kind`` key. Before read_manifest_deps normalized via
    manifest_reader._dep_to_record, this exact dep shape probed
    functional_probe_kind == "" and silently fell into the unknown-probe-kind
    branch -- present-but-broken with a WARNING, never 'present'. Uses the
    manifest's real multi-path shape (mirrors coordinator-claude's own
    file_exists_any dep entry) rather than a single-path stand-in."""
    (tmp_path / "sib-nested").mkdir()
    (tmp_path / "sib-nested" / "coordinator" / "CLAUDE.md").parent.mkdir(parents=True)
    (tmp_path / "sib-nested" / "coordinator" / "CLAUDE.md").write_text("hi")
    dep = {
        "id": "nested-dep",
        "severity": "soft",
        "sibling_dir_name": "sib-nested",
        "upstream_url": "https://example.invalid/repo.git",
        "functional_probe": {
            "kind": "file_exists_any",
            "paths": [".claude-plugin/plugin.json", "coordinator/CLAUDE.md"],
        },
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)

    status = scw.dep_probe("nested-dep", manifest_path, repo_root)
    assert status == "present"
    assert "unknown probe kind" not in capsys.readouterr().err


def test_dep_probe_makima_seam_resolvable_bypasses_sibling_gate(tmp_path):
    """makima_seam_resolvable must NOT require sibling_dir_name to exist on disk --
    project-makima is registry/env-resolved, not sibling-directory-colocated."""
    dep = {
        "id": "project-makima",
        "severity": "hard",
        "sibling_dir_name": "does-not-exist-anywhere",
        "functional_probe": {"kind": "makima_seam_resolvable"},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)

    # No sibling dir was created at all -- an ordinary sibling_dir_exists/
    # file_exists probe would report "missing" here. makima_seam_resolvable
    # instead checks find_spec("coordinator_core.invoke") directly, which
    # succeeds because this test process is itself running inside
    # coordinator_core (this very package).
    assert scw.dep_probe("project-makima", manifest_path, repo_root) == "present"


def test_dep_probe_unknown_kind_warns_present_but_broken(tmp_path, capsys):
    (tmp_path / "sib4").mkdir()
    dep = {
        "id": "d4",
        "severity": "hard",
        "sibling_dir_name": "sib4",
        "functional_probe": {"kind": "some_future_probe_kind"},
    }
    repo_root, manifest_path = _repo_with_manifest(tmp_path, dep)

    status = scw.dep_probe("d4", manifest_path, repo_root)
    assert status == "present-but-broken"
    assert "unknown probe kind" in capsys.readouterr().err


def test_dep_probe_nested_layout_bare_name_fallback(tmp_path):
    # sibling_dir_name "coordinator-claude" absent, but "coordinator" (the
    # "-claude"-stripped bare name) IS present — nested-layout fallback.
    (tmp_path / "coordinator").mkdir()
    dep = {
        "id": "d5",
        "severity": "soft",
        "sibling_dir_name": "coordinator-claude",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    repo_root = tmp_path / "some-other-root"
    (repo_root / "scripts" / "lib").mkdir(parents=True)
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps({"direct_deps": [dep]}))

    assert scw.dep_probe("d5", manifest_path, repo_root) == "present"


def test_sibling_fallback_rejects_name_match_that_fails_functional_probe(tmp_path):
    # Defect B hardening: a "-claude"-strip name match that EXISTS on disk
    # but fails the dep's own functional probe (e.g. an unrelated directory
    # that coincidentally shares the stripped name) must be rejected, not
    # silently accepted as a resolved sibling_path — the caller (dep_probe)
    # must see "missing", never manufacture "present-but-broken" out of a
    # coincidental name match.
    coincidental = tmp_path / "coordinator"
    coincidental.mkdir()  # exists, but has no README.md — fails file_exists
    resolved = scw._sibling_fallback(
        tmp_path,
        "coordinator-claude",
        "",
        probe_kind="file_exists",
        probe_args={"path": "README.md"},
    )
    assert resolved is None

    (coincidental / "README.md").write_text("hi")
    resolved = scw._sibling_fallback(
        tmp_path,
        "coordinator-claude",
        "",
        probe_kind="file_exists",
        probe_args={"path": "README.md"},
    )
    assert resolved == coincidental


def test_sibling_fallback_rejects_engine_tree_and_repo_root(tmp_path):
    # A candidate that IS the makima engine's own tree (or under it), or IS
    # the walked repo's own repo_root (or under it), must never be accepted
    # — this is the exact coincidental match that manufactured Defect A's
    # "found but broken" contradiction out of "not found here".
    engine_tree = scw._engine_tree_root()
    resolved = scw._sibling_fallback(
        engine_tree.parent,
        "coordinator-claude",
        "",
        probe_kind="sibling_dir_exists",
        probe_args={},
    )
    assert resolved is None

    repo_root = tmp_path / "repo-root"
    sibling_root = tmp_path
    same_name_dir = sibling_root / "coordinator"
    same_name_dir.mkdir()
    resolved = scw._sibling_fallback(
        sibling_root,
        "coordinator-claude",
        "",
        probe_kind="sibling_dir_exists",
        probe_args={},
        repo_root=same_name_dir,
    )
    assert resolved is None


def test_dep_probe_sibling_fallback_missing_when_candidate_fails_probe(tmp_path):
    # End-to-end via dep_probe: a "-claude"-strip candidate exists on disk
    # but fails the dep's declared functional probe -> overall status must
    # be "missing", never "present"/"present-but-broken".
    (tmp_path / "coordinator").mkdir()  # exists, but no README.md
    dep = {
        "id": "d6",
        "severity": "soft",
        "sibling_dir_name": "coordinator-claude",
        "functional_probe": {"kind": "file_exists", "path": "README.md"},
    }
    repo_root = tmp_path / "some-other-root"
    (repo_root / "scripts" / "lib").mkdir(parents=True)
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps({"direct_deps": [dep]}))

    assert scw.dep_probe("d6", manifest_path, repo_root) == "missing"


# ---------------------------------------------------------------------------
# _co_pf_emit_row equivalent — pf_emit_row: table + NDJSON + hard/semihard flags
# ---------------------------------------------------------------------------

def test_pf_emit_row_hard_fail_sets_flag(capsys):
    hard, semihard = scw.pf_emit_row("python", "missing", "hard", "not found")
    assert hard is True
    assert semihard is False
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.err
    row = json.loads(captured.out.strip())
    assert row == {"id": "python", "status": "fail", "severity": "hard", "hint": "not found"}


def test_pf_emit_row_semihard_warn_sets_flag(capsys):
    hard, semihard = scw.pf_emit_row("clone_auth", "warn", "semi-hard", "unauthenticated")
    assert hard is False
    assert semihard is True
    assert "[BLOCK]" in capsys.readouterr().err


def test_pf_emit_row_advisory_fail_never_sets_hard_flag(capsys):
    hard, semihard = scw.pf_emit_row("ue", "fail", "advisory", "not installed")
    assert hard is False
    assert semihard is False
    assert "[WARN]" in capsys.readouterr().err


def test_pf_emit_row_present_maps_ndjson_status_pass(capsys):
    scw.pf_emit_row("dep1", "present", "hard", "")
    row = json.loads(capsys.readouterr().out.strip())
    assert row["status"] == "pass"


# ---------------------------------------------------------------------------
# Fresh-install-shape smoke test (FAMILY-I: MAKIMA_ROOT may be unresolvable).
# Exercises the real DoE-side trampoline end-to-end with MAKIMA_ROOT
# forced-unresolvable, asserting the dedicated transport-failure exit code
# (95) and an actionable remediation message — not a bare traceback.
#
# Not circular: this test probes the transport-failure arm exclusively —
# makima's own coordinator_core code is never imported/reached on this path
# (the trampoline raises and exits at the RuntimeError catch in setup.py's
# main() before op_main() is ever looked up). Isolation is layered across
# every _resolve_makima_root rung, not just the env-var one: env.pop(
# "MAKIMA_ROOT") clears rung 1, COORDINATOR_SETTINGS_HOME redirected to an
# empty temp dir clears the rung-1.5 machine-local pointer file, and HOME
# redirected to a fake temp dir clears rung 2 (cc_invoke._claude_home() falls
# back to os.path.expanduser("~"), i.e. $HOME, when CLAUDE_HOME is unset —
# see DoE-claude coordinator/bin/lib/cc_invoke.py:148-158 — so this also
# starves _machine_local_get's bin/_machine_local.py lookup). Popping
# MAKIMA_ROOT alone is NOT sufficient (_resolve_makima_root falls through to
# the machine-local registry rung); the COORDINATOR_SETTINGS_HOME + HOME
# redirection is load-bearing for the no-circularity guarantee this test
# relies on.
# ---------------------------------------------------------------------------

# Renamed from setup.sh -> setup.py by DoE-claude's 2026-07-22 de-bash
# campaign (pure extension rename, no logic change — see setup.py's own
# header). The bare-.sh oracle path is gone; check for the surviving
# artifact so this test's isolation-mechanism guarantee (see comment above)
# is exercised rather than silently skipped.
_DOE_SETUP_PY = Path(resolve_doe_root() or "/doe-root-unresolved") / "coordinator" / "scripts" / "setup.py"


@pytest.mark.skipif(
    not _DOE_SETUP_PY.is_file(),
    reason="DoE-claude sibling repo (coordinator/scripts/setup.py) not present at this layout",
)
def test_trampoline_transport_failure_has_dedicated_exit_code_and_remediation(tmp_path):
    env = dict(os.environ)
    env.pop("MAKIMA_ROOT", None)
    # Force settings-home to an empty temp dir so the machine-local pointer
    # file rung of _resolve_makima_root also misses, and point HOME there too
    # so no real machine-local state leaks into the probe.
    env["COORDINATOR_SETTINGS_HOME"] = str(tmp_path / "no-settings-home")
    env["HOME"] = str(tmp_path / "fake-home")
    res = subprocess.run(
        [sys.executable, str(_DOE_SETUP_PY), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=_CREATIONFLAGS,
    )
    if res.returncode == 0:
        pytest.skip(
            "MAKIMA_ROOT resolved via a rung this sandbox couldn't suppress "
            "(e.g. sibling dir found) — not a fresh-install shape here"
        )
    assert res.returncode == 95
    assert "MAKIMA_ROOT resolution failed" in res.stderr
    assert "remediation" in res.stderr.lower()


def test_resolve_manifest_path_remediation_has_no_dead_publish_sh_command(tmp_path, capsys):
    # Regression pin: setup/publish.sh was retired repo-wide by DoE-claude's
    # percolate-python-port work (2026-07-21/22). The remediation text must
    # not tell an operator to run a dead command, and must not shell out via
    # bash (naked-Python-only convention — see project CLAUDE.md § Runtime
    # conventions).
    repo_root = tmp_path / "coordinator"
    os.makedirs(repo_root, exist_ok=True)
    result = scw.resolve_manifest_path(repo_root)
    assert result is None
    captured = capsys.readouterr()
    assert "setup/publish.sh" not in captured.err
    assert "bash " not in captured.err
    assert "python3 coordinator/bin/publish.py coordinator-claude-toplevel-install" in captured.err
    assert "python3 coordinator/bin/publish.py coordinator-claude" in captured.err


# ---------------------------------------------------------------------------
# _sibling_search_root — layout-invariant anchor for the sibling-dir default
# ---------------------------------------------------------------------------

def _dep_clone(peers: Path, name: str) -> Path:
    clone = peers / name
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "plugin.json").write_text("{}")
    return clone


_SIBLING_DEP = {
    "id": "coordinator-claude",
    "severity": "hard",
    "sibling_dir_name": "coordinator-claude",
    "upstream_url": "https://example.invalid/coordinator-claude.git",
    "functional_probe": {
        "kind": "file_exists_any",
        "paths": [".claude-plugin/plugin.json", "coordinator/.claude-plugin/plugin.json"],
    },
}


def _layout(peers: Path, *, nested: bool) -> tuple[Path, Path]:
    """Build a checkout under ``peers`` and return (repo_root, manifest_path).

    ``nested`` mirrors a working-repo clone (chain-walk.py hands the walker
    the ``coordinator/`` tree root, one level below the checkout root);
    ``nested=False`` mirrors the flat publish-repo layout, where the
    ``coordinator/`` tree root and the checkout root are the same directory.
    """
    checkout = peers / "checkout"
    (checkout / ".git").mkdir(parents=True)
    manifest_dir = checkout / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps({"direct_deps": [_SIBLING_DEP]}))
    repo_root = checkout / "coordinator" if nested else checkout
    (repo_root / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
    return repo_root, manifest_path


def test_sibling_default_resolves_beside_the_checkout_in_nested_layout(tmp_path):
    """A dev clone hands the walker ``<checkout>/coordinator`` as repo_root.
    The sibling default must land beside the CHECKOUT (``<peers>/
    coordinator-claude``), never inside it (``<checkout>/coordinator-claude``,
    which no clone ever creates and which failed the hard-dep gate on every
    dev machine)."""
    clone = _dep_clone(tmp_path, "coordinator-claude")
    repo_root, manifest_path = _layout(tmp_path, nested=True)

    resolved, via_override = scw._resolve_dep_root(
        "coordinator-claude", "coordinator-claude", manifest_path, repo_root, argv=[]
    )
    assert (resolved, via_override) == (clone, False)
    assert scw.dep_probe("coordinator-claude", manifest_path, repo_root, argv=[]) == "present"


def test_sibling_default_resolves_beside_the_checkout_in_flat_layout(tmp_path):
    """The flat (publish-repo) layout hands the walker the checkout root
    itself. Same anchor, same answer — a fix that merely added one more
    ``.parent`` would resolve this one level too high."""
    clone = _dep_clone(tmp_path, "coordinator-claude")
    repo_root, manifest_path = _layout(tmp_path, nested=False)

    resolved, _ = scw._resolve_dep_root(
        "coordinator-claude", "coordinator-claude", manifest_path, repo_root, argv=[]
    )
    assert resolved == clone
    assert scw.dep_probe("coordinator-claude", manifest_path, repo_root, argv=[]) == "present"


def test_sibling_search_root_falls_back_to_manifest_owner_without_git(tmp_path, monkeypatch):
    """A payload with no ``.git`` (tarball, history-free mirror) resolves the
    default against the checkout that OWNS the manifest — the same place the
    ``.git`` walk would have found."""
    monkeypatch.setattr(
        "coordinator_core.subagent_sandbox.engine.resolve_git_root_cheap", lambda cwd: None
    )
    clone = _dep_clone(tmp_path, "coordinator-claude")
    repo_root, manifest_path = _layout(tmp_path, nested=True)

    assert scw._sibling_search_root(repo_root, manifest_path) == tmp_path
    assert scw.dep_probe("coordinator-claude", manifest_path, repo_root, argv=[]) == "present"
    assert clone.is_dir()


def test_sibling_search_root_never_returns_a_path_inside_the_walked_tree(tmp_path):
    """The defect's signature, pinned directly: whichever layout the walker
    is handed, the anchor must sit OUTSIDE the tree being walked."""
    for nested in (True, False):
        peers = tmp_path / f"peers-{nested}"
        peers.mkdir()
        repo_root, manifest_path = _layout(peers, nested=nested)
        anchor = scw._sibling_search_root(repo_root, manifest_path)
        assert anchor == peers
        # The anchor is an ancestor of the walked tree, never a directory
        # inside it — so no sibling_dir_name can resolve into the checkout.
        repo_root.relative_to(anchor)
        with pytest.raises(ValueError):
            anchor.relative_to(repo_root)

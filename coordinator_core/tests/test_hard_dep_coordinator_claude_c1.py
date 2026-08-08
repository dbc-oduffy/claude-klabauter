"""Regression pin for AC1 — coordinator-claude flipped soft -> hard.

PM ruling 2026-08-03, verbatim: "klabauter-claude-klabauter is useless without
Example-doctrine-repo/coordinator-claude to wire into, it's a 100% hard dep for us." No DR
ever ratified `soft` — it traced only to the prose of
docs/plans/2026-07-04-claude-klabauter-install-and-doctor-system.md, which authored
docs/install/agent-install-manifest.json. This chunk SETTLES that open
question rather than reversing a ratified one.

THREE sites, tested independently here because they do not share an
implementation:
  (a) docs/install/agent-install-manifest.json declares
      direct_deps[coordinator-claude].severity: hard — checked by
      `test_manifest_declares_coordinator_claude_severity_hard` below (a
      thin data-shape assertion; the interesting behavior is (b)/(c)).
  (b) coordinator_core.ops.setup_chain_walker.consent_gate — asserted by
      test on the CODE (90/93), never merely non-zero, per the contract:
      a test accepting any failure would pass on an unrelated crash.
  (c) scripts/setup.py's own dep-check (`check_coordinator_claude_dep`) and
      registration guard (`register_claude_klabauter_root`) — these implement the
      soft path independently of the walker and needed an actual code
      change (not just a severity-field flip); tested here at the function
      level per this repo's existing scripts/test_setup.py convention
      (subprocess/pip-touching paths are out of scope for a unit test —
      these two functions return/exit BEFORE any subprocess call on the
      missing-dep branches exercised here).

ALSO covers the divergent-resolution gap the chunk's own review comment
flags: `setup_chain_walker.dep_probe` previously resolved a dep's sibling
root via `repo_root.parent / sibling_dir_name` ONLY, ignoring the manifest's
own `configurable_locations[].override.{flag,env}` ladder that
`scripts/setup.py::_resolve_coordinator_claude_root` already honours. Case 3
below pins the fix: coordinator-claude resolvable ONLY via
COORDINATOR_CLAUDE_ROOT / --coordinator-root (no sibling clone present) must
exit 0 (i.e. not land in the hard set) from the walker, matching
scripts/setup.py's existing behavior.

Spec backlink: docs/plans/2026-08-03-install-chain-install-md-and-chain-walk-contract.md
  chunk C1 (AC1)

Negative-spec: does not touch example-retrieval-repo's or example-game-repo's severity for
their own claude-klabauter dep declarations — different repos, different owners.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import setup_chain_walker as scw

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MANIFEST_PATH = _REPO_ROOT / "docs" / "install" / "agent-install-manifest.json"
_SETUP_PY_PATH = _REPO_ROOT / "scripts" / "setup.py"


def _load_scripts_setup_module():
    """Load scripts/setup.py as a standalone module, matching the loader
    scripts/test_setup.py already uses (named to avoid colliding with an
    installed `setup` package on sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "_scripts_setup_under_test_c1", _SETUP_PY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def setup_mod():
    return _load_scripts_setup_module()


# ---------------------------------------------------------------------------
# (a) manifest declaration
# ---------------------------------------------------------------------------


def test_manifest_declares_coordinator_claude_severity_hard():
    data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    dep = next(d for d in data["direct_deps"] if d["id"] == "coordinator-claude")
    assert dep["severity"] == "hard"


# ---------------------------------------------------------------------------
# (b) coordinator_core.ops.setup_chain_walker.consent_gate — the walker's
# own enforcement site. Uses a synthetic manifest (not the real one) so the
# fixture controls sibling-dir presence/absence deterministically — never
# moves/creates/deletes a real clone anywhere on this machine.
# ---------------------------------------------------------------------------


def _ladder_manifest(tmp_path: Path) -> tuple[Path, Path]:
    """A synthetic manifest declaring coordinator-claude as a hard dep with
    the SAME configurable_locations shape the real manifest carries — the
    ladder `dep_probe` must consult: --coordinator-root flag ->
    COORDINATOR_CLAUDE_ROOT env -> ../coordinator-claude sibling default."""
    coord_dep = {
        "id": "coordinator-claude",
        "severity": "hard",
        "sibling_dir_name": "coordinator-claude",
        "upstream_url": "https://github.com/dbc-oduffy/coordinator-claude",
        "functional_probe": {
            "kind": "file_exists_any",
            "paths": [".claude-plugin/plugin.json", "coordinator/CLAUDE.md"],
        },
    }
    manifest = {
        "direct_deps": [coord_dep],
        "configurable_locations": [
            {
                "name": "coordinator_root",
                "dependency_id": "coordinator-claude",
                "default": "../coordinator-claude",
                "override": {
                    "flag": "--coordinator-root",
                    "env": "COORDINATOR_CLAUDE_ROOT",
                },
            }
        ],
    }
    repo_root = tmp_path / "claude-klabauter-repo"
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return repo_root, manifest_path


def test_walker_consent_gate_hard_dep_missing_exits_90(tmp_path, monkeypatch):
    """Case 1: coordinator-claude missing (no sibling clone, no override) —
    consent_gate must raise the walker's own _SetupError(90), asserted on
    the CODE, not merely on non-zero."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])
    repo_root, manifest_path = _ladder_manifest(tmp_path)
    with pytest.raises(scw._SetupError) as exc_info:
        scw.consent_gate(False, False, True, manifest_path, repo_root, tmp_path)
    assert exc_info.value.code == 90


def test_walker_consent_gate_hard_dep_broken_exits_90(tmp_path, monkeypatch):
    """coordinator-claude sibling present but functional probe fails
    (present-but-broken) is ALSO a hard-set entry — the walker's own
    severity-driven contract (no code change expected here; confirmed by
    test rather than by reading, per the chunk body)."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])
    repo_root, manifest_path = _ladder_manifest(tmp_path)
    (repo_root.parent / "coordinator-claude").mkdir(parents=True)
    # sibling dir exists but neither functional-probe file is present.
    with pytest.raises(scw._SetupError) as exc_info:
        scw.consent_gate(False, False, True, manifest_path, repo_root, tmp_path)
    assert exc_info.value.code == 90


def test_walker_consent_gate_half_passed_override_exits_93(tmp_path, monkeypatch):
    """Case 2: only one of the override-flag pair supplied -> exit 93,
    regardless of dep status."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])
    repo_root, manifest_path = _ladder_manifest(tmp_path)
    with pytest.raises(scw._SetupError) as exc_info:
        scw.consent_gate(True, False, True, manifest_path, repo_root, tmp_path)
    assert exc_info.value.code == 93
    with pytest.raises(scw._SetupError) as exc_info:
        scw.consent_gate(False, True, True, manifest_path, repo_root, tmp_path)
    assert exc_info.value.code == 93


def test_walker_consent_gate_ladder_env_override_no_sibling_passes(tmp_path, monkeypatch):
    """Case 3: coordinator-claude resolvable ONLY via COORDINATOR_CLAUDE_ROOT
    (no sibling clone present) must NOT land in the hard set — this failed
    before dep_probe's ladder fix (it resolved purely on
    repo_root.parent / sibling_dir_name)."""
    repo_root, manifest_path = _ladder_manifest(tmp_path)
    override_root = tmp_path / "elsewhere-coord-env"
    (override_root / ".claude-plugin").mkdir(parents=True)
    (override_root / ".claude-plugin" / "plugin.json").write_text("{}")
    monkeypatch.setenv("COORDINATOR_CLAUDE_ROOT", str(override_root))
    monkeypatch.setattr(sys, "argv", ["prog"])
    assert not (repo_root.parent / "coordinator-claude").is_dir()

    scw.consent_gate(False, False, True, manifest_path, repo_root, tmp_path)  # must not raise
    assert scw.dep_probe("coordinator-claude", manifest_path, repo_root) == "present"


def test_walker_consent_gate_ladder_flag_override_no_sibling_passes(tmp_path, monkeypatch):
    """Case 3, flag form: --coordinator-root, no sibling clone present."""
    repo_root, manifest_path = _ladder_manifest(tmp_path)
    override_root = tmp_path / "elsewhere-coord-flag"
    (override_root / "coordinator").mkdir(parents=True)
    (override_root / "coordinator" / "CLAUDE.md").write_text("# doc")
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog", "--coordinator-root", str(override_root)])
    assert not (repo_root.parent / "coordinator-claude").is_dir()

    scw.consent_gate(False, False, True, manifest_path, repo_root, tmp_path)  # must not raise
    assert scw.dep_probe("coordinator-claude", manifest_path, repo_root) == "present"


def test_walker_dep_probe_no_configurable_location_falls_back_to_sibling(tmp_path, monkeypatch):
    """Negative-spec: a dep the manifest declares no configurable_locations
    entry for (e.g. an ordinary plugin-to-plugin dep) still resolves via the
    old sibling-dir-only behaviour — the ladder generalization must not
    special-case "coordinator-claude" in walker code, and must not break a
    dep with no override surface of its own."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["prog"])
    dep = {
        "id": "some-other-dep",
        "severity": "hard",
        "sibling_dir_name": "sib-no-ladder",
        "functional_probe": {"kind": "sibling_dir_exists"},
    }
    manifest = {
        "direct_deps": [dep],
        "configurable_locations": [
            {
                "name": "coordinator_root",
                "dependency_id": "coordinator-claude",
                "override": {"flag": "--coordinator-root", "env": "COORDINATOR_CLAUDE_ROOT"},
            }
        ],
    }
    repo_root = tmp_path / "claude-klabauter-repo2"
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    assert scw.dep_probe("some-other-dep", manifest_path, repo_root) == "missing"
    (tmp_path / "sib-no-ladder").mkdir()
    assert scw.dep_probe("some-other-dep", manifest_path, repo_root) == "present"


# ---------------------------------------------------------------------------
# (c) scripts/setup.py's OWN enforcement — check_coordinator_claude_dep
# (dep-check) and register_claude_klabauter_root (registration guard). Function-level,
# per this repo's existing scripts/test_setup.py convention: the
# missing-dep branches exercised here return/exit BEFORE any subprocess
# call, so no pip/network/machine-local touch happens.
# ---------------------------------------------------------------------------


def test_setup_py_check_coordinator_claude_dep_missing_exits_90(setup_mod, tmp_path, monkeypatch):
    """Case 1, scripts/setup.py side: was WARN + advisory + return (soft,
    never fatal) before this chunk. Now fails loud with the SAME contract
    code (90) the walker's consent_gate raises for the identical condition."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    repo_root = tmp_path / "claude-klabauter"
    repo_root.mkdir()
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.check_coordinator_claude_dep(repo_root, args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING
    assert setup_mod.EXIT_HARD_DEP_MISSING == 90


def test_setup_py_check_coordinator_claude_dep_present_passes(setup_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    repo_root = tmp_path / "claude-klabauter"
    repo_root.mkdir()
    coord = tmp_path / "coordinator-claude"
    (coord / ".claude-plugin").mkdir(parents=True)
    (coord / ".claude-plugin" / "plugin.json").write_text("{}")
    # plugin.json ALONE stopped being sufficient at 336aad969 -- a publish mirror
    # ships it too, so the OSS source shape requires commands/ or hooks/ alongside.
    (coord / "commands").mkdir()
    args = setup_mod.Args()
    # Must not raise/exit.
    setup_mod.check_coordinator_claude_dep(repo_root, args)


def test_setup_py_check_coordinator_claude_dep_case3_env_only_passes(setup_mod, tmp_path, monkeypatch):
    """Case 3, scripts/setup.py side: this entrypoint already honoured the
    ladder via _resolve_coordinator_claude_root before this chunk — pinned
    here so the two entrypoints stay symmetric."""
    repo_root = tmp_path / "claude-klabauter"
    repo_root.mkdir()
    override_root = tmp_path / "elsewhere-coord"
    (override_root / ".claude-plugin").mkdir(parents=True)
    (override_root / ".claude-plugin" / "plugin.json").write_text("{}")
    (override_root / "commands").mkdir()  # see 336aad969 note above
    monkeypatch.setenv("COORDINATOR_CLAUDE_ROOT", str(override_root))
    assert not (tmp_path / "coordinator-claude").is_dir()
    args = setup_mod.Args()
    setup_mod.check_coordinator_claude_dep(repo_root, args)  # must not raise


def _claude_klabauter_repo_root(tmp_path: Path, name: str = "claude-klabauter") -> Path:
    """A repo_root that `resolve_repo_identity` resolves to "claude-klabauter"
    -- `5080edc48` ("installer: the same setup.py installs two repos, so it
    must know which one") added that identity gate in front of
    `register_claude_klabauter_root` after these tests were written for a
    single-identity `register_claude_klabauter_root`; without the manifest marker,
    identity is unresolved and the function now exits 95 before reaching the
    behavior under test. Every register_claude_klabauter_root test here exercises the
    claude-klabauter branch, never klabauter's."""
    repo_root = tmp_path / name
    manifest_dir = repo_root / "docs" / "install"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "agent-install-manifest.json").write_text(
        json.dumps({"repo_id": "claude-klabauter"})
    )
    return repo_root


def test_setup_py_register_claude_klabauter_root_missing_coord_no_override_exits_90(setup_mod, tmp_path, monkeypatch):
    """The registration guard's soft-dep "skip with advisory, exit 0" branch
    is now wrong when the override pair was NOT supplied — fail loud (90)."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(
        "coordinator_core.install._shared.resolve_machine_local_cli", lambda plugin_root: None
    )
    repo_root = _claude_klabauter_repo_root(tmp_path)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.register_claude_klabauter_root(repo_root, "git-root auto-discovery", repo_root, args)
    assert exc_info.value.code == setup_mod.EXIT_HARD_DEP_MISSING


def test_setup_py_register_claude_klabauter_root_missing_coord_with_override_degrades(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """Registration DOES still degrade gracefully (advisory, exit 0) when
    the operator supplied the override pair — the risk was already
    accepted; `main()` never even calls `check_coordinator_claude_dep` in
    that branch (see `main`'s `if args.skip_dep_check: ... else: ...`).

    NEITHER key is registered in this branch — repos.claude_klabauter and
    engine.working_repos.claude_klabauter share the one guard (2026-08-05
    example-doctrine-repo-em working-repo-rule proposal, accepted)."""
    monkeypatch.delenv("COORDINATOR_CLAUDE_ROOT", raising=False)
    monkeypatch.setattr(
        "coordinator_core.install._shared.resolve_machine_local_cli", lambda plugin_root: None
    )
    repo_root = _claude_klabauter_repo_root(tmp_path)
    args = setup_mod.Args()
    args.skip_dep_check = True
    args.accept_risk = True
    result = setup_mod.register_claude_klabauter_root(repo_root, "git-root auto-discovery", repo_root, args)
    assert result == repo_root
    out = capsys.readouterr().out
    assert "ADVISORY" in out
    assert "repos.claude_klabauter" in out
    assert "engine.working_repos.claude_klabauter" in out


def test_setup_py_register_claude_klabauter_root_happy_path_registers_both_keys(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """Machine-local present, both `machine-local set` invocations succeed ->
    both repos.claude_klabauter and engine.working_repos.claude_klabauter are
    registered to the same resolved path, and both PASS lines print.

    Spec backlink: cross-repo/inbox/2026-08-05-example-doctrine-repo-em-working-repo-
    rule-19-keys-13-consumers-name-the-set-explicitly.md — our half is the
    install-time write of engine.working_repos.claude_klabauter alongside the
    pre-existing repos.claude_klabauter write."""
    monkeypatch.setattr(
        "coordinator_core.install._shared.resolve_machine_local_cli",
        lambda plugin_root: ["machine-local"],
    )
    calls = []

    def _fake_run(argv, timeout=None, **kwargs):
        calls.append(argv)
        return setup_mod.subprocess.CompletedProcess(argv, returncode=0)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
    repo_root = _claude_klabauter_repo_root(tmp_path)
    args = setup_mod.Args()
    result = setup_mod.register_claude_klabauter_root(repo_root, "git-root auto-discovery", repo_root, args)
    assert result == repo_root

    assert calls == [
        ["machine-local", "set", "repos.claude_klabauter", str(repo_root)],
        ["machine-local", "set", "engine.working_repos.claude_klabauter", str(repo_root)],
    ]
    out = capsys.readouterr().out
    assert f"PASS [registration] repos.claude_klabauter = {repo_root}" in out
    assert f"PASS [registration] engine.working_repos.claude_klabauter = {repo_root}" in out


def test_setup_py_register_claude_klabauter_root_second_key_failure_exits_loud(
    setup_mod, tmp_path, monkeypatch, capsys
):
    """The new key's write sits inside the SAME fail-loud contract as the
    pre-existing repos.claude_klabauter write: repos.claude_klabauter succeeds,
    engine.working_repos.claude_klabauter fails -> exit loud (non-zero), same
    as a repos.claude_klabauter failure would, not a silent partial success."""
    monkeypatch.setattr(
        "coordinator_core.install._shared.resolve_machine_local_cli",
        lambda plugin_root: ["machine-local"],
    )

    def _fake_run(argv, timeout=None, **kwargs):
        returncode = 0 if argv[2] == "repos.claude_klabauter" else 1
        return setup_mod.subprocess.CompletedProcess(argv, returncode=returncode)

    monkeypatch.setattr(setup_mod.subprocess, "run", _fake_run)
    repo_root = _claude_klabauter_repo_root(tmp_path)
    args = setup_mod.Args()
    with pytest.raises(SystemExit) as exc_info:
        setup_mod.register_claude_klabauter_root(repo_root, "git-root auto-discovery", repo_root, args)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "engine.working_repos.claude_klabauter" in err


def test_setup_py_main_half_passed_override_exits_93_both_directions(setup_mod):
    """Case 2, scripts/setup.py side: pre-existing top-of-main() gate, pinned
    here alongside the walker's own so both entrypoints' Case-2 behavior is
    documented in one regression file."""
    assert setup_mod.main(["--skip-dep-check"]) == setup_mod.EXIT_FLAG_PAIR_VIOLATION
    assert setup_mod.main(["--accept-missing-deps-risk"]) == setup_mod.EXIT_FLAG_PAIR_VIOLATION
    assert setup_mod.EXIT_FLAG_PAIR_VIOLATION == 93


class TestUpstreamUrlSiblingFallback:
    """The repoint-is-one-field property C7/C9/C10 ask sibling repos to rely on.

    `dep_probe` resolves on `sibling_dir_name`; `upstream_url` only builds the
    remediation hint. Without the URL-basename fallback, a consumer who
    repoints `upstream_url` alone leaves an external adopter cloning
    `<url-basename>` while the walker hunts the stale `sibling_dir_name` —
    missing, with a hint looping them back to the URL they already cloned.
    """

    @staticmethod
    def _manifest(tmp_path, sibling_dir_name, upstream_url):
        import json

        repo_root = tmp_path / "consumer"
        (repo_root / "docs" / "install").mkdir(parents=True)
        manifest_path = repo_root / "docs" / "install" / "agent-install-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "agent_install_contract_version": 3,
                    "repo_id": "consumer",
                    "direct_deps": [
                        {
                            "id": "the-engine",
                            "severity": "hard",
                            "sibling_dir_name": sibling_dir_name,
                            "upstream_url": upstream_url,
                            "functional_probe_kind": "sibling_dir_exists",
                        }
                    ],
                }
            )
        )
        return repo_root, manifest_path

    def test_declared_sibling_dir_still_wins_when_present(self, tmp_path):
        from coordinator_core.ops.setup_chain_walker import dep_probe

        repo_root, manifest_path = self._manifest(
            tmp_path, "claude-klabauter", "https://github.com/dbc-oduffy/claude-klabauter"
        )
        (tmp_path / "claude-klabauter").mkdir()
        assert dep_probe("the-engine", manifest_path, repo_root) == "present"

    def test_upstream_url_basename_resolves_when_declared_name_absent(self, tmp_path):
        from coordinator_core.ops.setup_chain_walker import dep_probe

        repo_root, manifest_path = self._manifest(
            tmp_path, "claude-klabauter", "https://github.com/dbc-oduffy/claude-klabauter"
        )
        (tmp_path / "claude-klabauter").mkdir()
        assert dep_probe("the-engine", manifest_path, repo_root) == "present"

    def test_dot_git_suffix_is_stripped(self, tmp_path):
        from coordinator_core.ops.setup_chain_walker import dep_probe

        repo_root, manifest_path = self._manifest(
            tmp_path, "claude-klabauter", "https://github.com/dbc-oduffy/claude-klabauter.git"
        )
        (tmp_path / "claude-klabauter").mkdir()
        assert dep_probe("the-engine", manifest_path, repo_root) == "present"

    def test_still_missing_when_neither_name_is_on_disk(self, tmp_path):
        from coordinator_core.ops.setup_chain_walker import dep_probe

        repo_root, manifest_path = self._manifest(
            tmp_path, "claude-klabauter", "https://github.com/dbc-oduffy/claude-klabauter"
        )
        assert dep_probe("the-engine", manifest_path, repo_root) == "missing"

    def test_no_upstream_url_falls_back_to_prior_behaviour(self, tmp_path):
        from coordinator_core.ops.setup_chain_walker import dep_probe

        repo_root, manifest_path = self._manifest(tmp_path, "claude-klabauter", "")
        assert dep_probe("the-engine", manifest_path, repo_root) == "missing"
        (tmp_path / "claude-klabauter").mkdir()
        assert dep_probe("the-engine", manifest_path, repo_root) == "present"

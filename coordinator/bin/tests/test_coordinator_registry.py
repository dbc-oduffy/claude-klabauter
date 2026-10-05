from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import coordinator_registry as reg  # noqa: E402

import pytest  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# ---------------------------------------------------------------------------
# AC-1: KNOWN_TYPES — exact 31-type set
#
# Golden pin derived from schemas/coordinator-registry.manifest.json (source
# of truth). Reconciled 2026-07-25: this pin had drifted silently because the
# hand-rolled fail_test() helper (fixed in 23f65fce) let these assertions run
# to completion without ever raising, so three real upstream manifest edits
# in coordinator-content-repo never got mirrored here:
#   - "flight-recorder" REMOVED — DoE commit 3aa9a79f ("C8(subsume): retire
#     flight-recorder — rm schema, repoint registry+artifact-shape to
#     run-report") deleted the flight-recorder schema and repointed the
#     registry to run-report; the type was subsumed, not merely renamed.
#   - "run-report" ADDED — the same 3aa9a79f repoint.
#   - "tier-u-grant" ADDED — DoE commit 58cdc600 ("Tier-U grant token schema +
#     manifest registration").
#   - "sizing-object" ADDED — DoE commit adf618d5 ("register sizing-object
#     doc-type — manifest row + drift-guard fixture").
#   - "subagent-sidecar" ADDED — manifest registration closing AC-10's unmet
#     half (agent-side decision-object container scaffolder, schemaName null
#     — schema-of-record is schemas/decision-object.schema.json $defs/
#     subagent_sidecar, not a standalone file); retires the coordinator-doc-
#     new / type_enum.py local shims that pre-dated this manifest row.
# Reconciled 2026-08-02 (stale-test cleanup, triage-F): DoE commit 410eae0d1
# ("manifest + skills: --type and kind now agree", deliverable
# dlv-baton-kind-vocabulary-one-axis-per-field-1be219) renamed the docTypes
# entries so the --type flag agrees with the kind value each scaffolds:
#   - "spinoff-roadmap" RENAMED to "roadmap-baton" (legacy spelling remains a
#     permanent CLI-side alias in coordinator-doc-new, not a second manifest
#     row).
#   - "spinoff-goal" RENAMED to "goal-seed" (same alias treatment).
#   - "spinoff-roadmap-creator" RENAMED to "roadmap-seed" (same alias
#     treatment).
# Prior reconciliation history (2026-07-12: spike-result, strategic-self-
# description, workflow; 2026-07-11: spinoff-goal, spinoff-roadmap-creator,
# goal, recovery) retained below for context. Do NOT weaken this to a
# subset/superset check — it is an exact-set pin; add new entries here in the
# same commit that adds a type to the manifest.
# ---------------------------------------------------------------------------
_EXPECTED_KNOWN_TYPES: frozenset[str] = frozenset({
    "handoff",
    "spinoff",
    "roadmap-baton",
    "goal-seed",
    "roadmap-seed",
    "recovery",
    "memo",
    "plan",
    "decision",
    "audit-record",
    "problem-set",
    "completion",
    "completion-receipt",
    "goal",
    "health-status",
    "run-report",
    "research-synthesis",
    "review-findings",
    "review",
    "prior-art-check",
    "plan-coverage-check",
    "docs-check",
    "improvement-queue",
    "bug-backlog",
    "debt-backlog",
    "lesson",
    "spike-result",
    "strategic-self-description",
    "workflow",
    "tier-u-grant",
    "sizing-object",
    "subagent-sidecar",
})


def test_known_types():
    assert reg.KNOWN_TYPES == _EXPECTED_KNOWN_TYPES


def test_known_types_count():
    assert len(reg.KNOWN_TYPES) == 32


def test_sidecar_types():
    assert reg.SIDECAR_TYPES == frozenset(
        {"review", "prior-art-check", "plan-coverage-check", "docs-check"}
    )


def test_queue_types():
    assert reg.QUEUE_TYPES == frozenset(
        {"improvement-queue", "bug-backlog", "debt-backlog"}
    )


def test_repo_aliases():
    assert reg.REPO_ALIASES == {"example_game_workbench_repo": "example-game-repo"}


def test_receiver_em_aliases():
    # Inverse of REPO_ALIASES.
    assert reg.RECEIVER_EM_ALIASES == {"example-game-repo": "example_game_workbench_repo"}


def test_central_receiver_ids():
    ids = reg.CENTRAL_RECEIVER_IDS
    assert "coordinator-content-repo-em" in ids
    assert ids <= frozenset({"coordinator-content-repo-em", "content-root-em", "coordinator-content-repo-em"})
    assert not ids & {"claude-central-em", "central-em", "central"}


# AC-7: CENTRAL_REPO_BASENAMES retired (C1 — basename anchor abandoned; the
# manifest key was removed; the Python constant is gone). No assertion here.
# The validate-frontmatter-schema.js consumer must be updated separately.


def test_sidecar_suffixes():
    # Review F2/F3 — new symbol replacing local _SIDECAR_SUFFIX.
    assert reg.SIDECAR_SUFFIXES == {
        "review": "review",
        "prior-art-check": "prior-art-check",
        "plan-coverage-check": "plan-coverage-check",
        "docs-check": "docs-check",
    }


# ---------------------------------------------------------------------------
# AC-9: repo_key_to_em_id — central anchor and normal cases (C1)
#
# repos.content_root resolves to the manifest-derived canonical central identity
# (identity.centralReceiverIds[0] == "coordinator-content-repo-em"), NOT the retired
# "claude-central-em" literal — see _central_canonical_id() in
# coordinator_registry.py. "claude-central-em" remains a valid receiver alias
# (see CENTRAL_RECEIVER_IDS) but is no longer the canonical return here.
# ---------------------------------------------------------------------------


def test_repo_key_to_em_id_content_root_canonical():
    assert reg.repo_key_to_em_id("repos.content_root") == "coordinator-content-repo-em"


def test_central_canonical_id():
    assert reg._central_canonical_id() == "coordinator-content-repo-em"


def test_repo_key_to_em_id_example_retrieval_repo():
    assert reg.repo_key_to_em_id("repos.project_rag") == "example-retrieval-repo-em"


def test_repo_key_to_em_id_example_game_repo_alias():
    assert reg.repo_key_to_em_id("repos.example_game_workbench_repo") == "example-game-repo-em"


_CONTENT_ROOT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_em_id_for_root_content_root_canonical():
    assert reg.em_id_for_root(
        _CONTENT_ROOT_ROOT, {"repos.content_root": _CONTENT_ROOT_ROOT}
    ) == "coordinator-content-repo-em"


def test_em_id_for_root_none():
    assert reg.em_id_for_root(
        None, {"repos.content_root": _CONTENT_ROOT_ROOT}
    ) == "unknown-sender-em"


def test_em_id_for_root_basename_fallback():
    fake_claude_home = os.path.expanduser("~/.claude")
    assert reg.em_id_for_root(fake_claude_home, {}) == ".claude-em"


def test_em_id_for_root_registered_non_central_loop_step_3():
    fake_example_game_repo_root = "/nonexistent/fake-example-game-repo-for-test"
    assert reg.em_id_for_root(
        fake_example_game_repo_root, {"repos.example_game_workbench_repo": fake_example_game_repo_root}
    ) == "example-game-repo-em"


def test_content_root_em_alias_in_central_receiver_ids():
    assert "coordinator-content-repo-em" in reg.CENTRAL_RECEIVER_IDS


# ---------------------------------------------------------------------------
# C1: codename-free manifest-bootstrap rung ladder — by import, not by reading.
#
# The OSS depersonalize scrub rewrites WIRE IDENTIFIERS (REPO_CONTENT_ROOT,
# repos.content_root) into names no machine has ever set,
# leaving the split-repo layout's manifest bootstrap with zero live rungs and
# an import-time FileNotFoundError. These tests exercise the module in a
# fresh subprocess (import-time behavior can't be observed by re-importing an
# already-imported module) with REPO_CONTENT_ROOT unset, covering both
# the pointer-present and pointer-unreachable cases.
#
# Spec backlink: pln-the-published-engine-resolves-ae0bf7 § C1
# ---------------------------------------------------------------------------
import subprocess  # noqa: E402
import sys as _sys  # noqa: E402
import tempfile  # noqa: E402

_IMPORT_SNIPPET = (
    "import sys; "
    "sys.path.insert(0, {lib_dir!r}); "
    "import coordinator_registry; "
    "print(coordinator_registry._MANIFEST_PATH)"
).format(lib_dir=_LIB_DIR)


def _run_import_subprocess(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_sys.executable, "-c", _IMPORT_SNIPPET],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_bootstrap_import_succeeds_with_pointer_present():
    """Case (a): REPO_CONTENT_ROOT unset, real ambient pointer/registry
    state left intact — import must succeed via a codename-free rung (or the
    co-located rung, if this checkout happens to be co-located)."""
    env = dict(os.environ)
    env.pop("REPO_CONTENT_ROOT", None)
    result = _run_import_subprocess(env)
    assert result.returncode == 0, (
        f"expected import to succeed with pointer/registry state present; "
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), "expected _MANIFEST_PATH to print a non-empty path"


def test_bootstrap_import_falls_back_to_vendored_manifest_with_pointer_unreachable():
    """Case (b): REPO_CONTENT_ROOT unset AND HOME/CLAUDE_HOME/
    COORDINATOR_SETTINGS_HOME redirected to an empty temp dir — no LIVE rung
    can resolve, proving the file rungs are load-bearing (not merely present)
    rather than accidentally passing on ambient state. Must NOT delete or
    edit the real pointer files/registry TOMLs; redirection only.

    UPDATED (standalone-install fix, klabauter must run standalone per PM
    ruling — every live rung is structurally dead on that published mirror):
    this case now succeeds via the vendored fallback rung
    (coordinator/bin/lib/_vendor/coordinator-registry.manifest.json) rather
    than raising. The prior "fails loud with no rung reachable" contract is
    retired — a genuinely unreachable-manifest failure is no longer
    reproducible from this module alone; see
    coordinator/bin/tests/test_coordinator_registry_vendored_fallback_rung.py
    for the fallback rung's own dedicated coverage."""
    with tempfile.TemporaryDirectory() as _empty_home:
        env = dict(os.environ)
        env.pop("REPO_CONTENT_ROOT", None)
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env["HOME"] = _empty_home
        env["CLAUDE_HOME"] = _empty_home
        env["COORDINATOR_SETTINGS_HOME"] = os.path.join(_empty_home, ".coordinator-claude-settings")
        env.pop("MACHINE_LOCAL_IMPL", None)
        result = _run_import_subprocess(env)
        assert result.returncode == 0, (
            "expected import to succeed via the vendored fallback rung when no "
            f"live rung can resolve a manifest; stderr:\n{result.stderr}"
        )
        assert "_vendor" in result.stdout


# ---------------------------------------------------------------------------
# The two tests above assert the ladder's
# PRESENCE (case a passes via whatever ambient rung this dev box happens to
# carry; case b asserts a negative and can't witness a working rung). Four
# prior reviews shipped BLOCKER-1 (a present-but-INERT ladder on a real OSS
# box) through exactly that gap. This test closes it: a payload-shaped
# fixture tree (coordinator/bin/lib/ not flattened + lib/ flattened, per
# setup/publish-targets.portable) imported under a genuinely OSS-shaped
# environment (empty HOME/USERPROFILE/COORDINATOR_SETTINGS_HOME, no
# CLAUDE_PLUGIN_ROOT, no content-root pointer reachable), with the manifest
# reachable ONLY via the new marketplace-cache rung
# (_mp_marketplace_cache_rung(), BLOCKER-1a) under a synthetic CLAUDE_HOME —
# asserting import SUCCEEDS and _MANIFEST_PATH resolves inside that rung's
# fixture, not merely that some path got printed.
# ---------------------------------------------------------------------------
import shutil  # noqa: E402

_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)
_REAL_COORDINATOR_LIB_DIR = os.path.join(_COORDINATOR_DIR, "lib")


def _build_payload_shaped_fixture(root: str) -> tuple[str, str]:
    """Returns (payload_lib_dir, claude_home_dir).

    payload_lib_dir: <root>/engine-payload/coordinator/bin/lib/ — holds a
    real copy of coordinator_registry.py + machine_local_impl_resolve.py,
    exactly where the payload ships them (coordinator/bin -> coordinator/bin,
    NOT flattened, per setup/publish-targets.portable). The pointer rung reads
    through `machine_local_impl_resolve`, which is copied alongside; the
    pointer file itself is deliberately absent (no ambient content-root
    pointer on an OSS box).

    claude_home_dir: <root>/claude-home/ — the `CLAUDE_HOME` env value, a
    `$HOME` substitute (Convention A). Its `.claude/` subdir holds ONLY the
    marketplace-cache manifest, at the exact layout
    resolve_coordinator_clone._newest_cache_dir() /_mp_marketplace_cache_rung()
    probe: `.claude/plugins/cache/coordinator-claude/coordinator/<version>/
    schemas/coordinator-registry.manifest.json`. The engine-payload tree
    ships NO manifest anywhere under it (per the findings' "two mirrors"
    ground truth) — reachability depends entirely on this rung.
    """
    payload_lib_dir = os.path.join(root, "engine-payload", "coordinator", "bin", "lib")
    os.makedirs(payload_lib_dir)
    for _name in ("coordinator_registry.py", "machine_local_impl_resolve.py"):
        shutil.copyfile(os.path.join(_LIB_DIR, _name), os.path.join(payload_lib_dir, _name))

    claude_home_dir = os.path.join(root, "claude-home")
    cache_manifest_dir = os.path.join(
        claude_home_dir, ".claude", "plugins", "cache", "coordinator-claude", "coordinator", "1.2.3", "schemas"
    )
    os.makedirs(cache_manifest_dir)
    with open(
        os.path.join(cache_manifest_dir, "coordinator-registry.manifest.json"), "w", encoding="utf-8"
    ) as _fh:
        _fh.write(
            '{"docTypes": [], "queueTypes": [], '
            '"identity": {"repoAliases": [], "centralReceiverIds": ["coordinator-content-repo-em"]}}'
        )

    return payload_lib_dir, claude_home_dir


def test_bootstrap_import_succeeds_on_payload_shaped_tree_under_oss_environment():
    with tempfile.TemporaryDirectory() as _tmp:
        _payload_lib_dir, _claude_home_dir = _build_payload_shaped_fixture(_tmp)
        _empty_home = os.path.join(_tmp, "empty-home")
        os.makedirs(_empty_home)

        _snippet = (
            "import sys; "
            f"sys.path.insert(0, {_payload_lib_dir!r}); "
            "import coordinator_registry; "
            "print(coordinator_registry._MANIFEST_PATH)"
        )
        env = {
            "HOME": _empty_home,
            "USERPROFILE": _empty_home,
            "CLAUDE_HOME": _claude_home_dir,
            "COORDINATOR_SETTINGS_HOME": os.path.join(_empty_home, ".coordinator-claude-settings"),
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }
        result = subprocess.run(
            [_sys.executable, "-c", _snippet],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, (
            "expected import to succeed via the marketplace-cache rung on a "
            f"payload-shaped tree under an OSS-shaped environment; "
            f"stderr:\n{result.stderr}"
        )
        _manifest_path = result.stdout.strip()
        assert _manifest_path, "expected _MANIFEST_PATH to print a non-empty path"
        assert _manifest_path.startswith(_claude_home_dir), (
            f"expected _MANIFEST_PATH ({_manifest_path!r}) to resolve inside the "
            f"marketplace-cache fixture ({_claude_home_dir!r}), not some other rung"
        )


# ---------------------------------------------------------------------------
# C1D: content_root() gets the same codename-free rung ladder, in-process via
# monkeypatch (not a subprocess — content_root() runs at CALL time, not import
# time, so isolating just its own rungs from the ambient machine's real
# REPO_CONTENT_ROOT/registry state is enough; the module import at the
# top of this file already proved import-time behavior above).
#
# Spec backlink: pln-the-published-engine-resolves-ae0bf7 § C1D
# ---------------------------------------------------------------------------
import tempfile as _tempfile  # noqa: E402

import pytest  # noqa: E402


def _clear_content_root_env(monkeypatch):
    """Strip every env var content_root()'s legacy chain reads, and stub the
    codename rungs' pointer/marketplace-cache/registry helpers to '' / None,
    so a test can install exactly the one rung under test.

    REPO_CONTENT_ROOT now runs FIRST in
    content_root(), ahead of the codename-free rungs, so they must be cleared
    here too (this function already did) for the codename-rung tests below
    to observe their own rung rather than short-circuiting on the reordered
    override.
    _mp_marketplace_cache_rung() is a new real
    filesystem probe (~/.claude/plugins/cache/coordinator-claude/coordinator)
    that could accidentally resolve on a dev box with a real marketplace
    install; stub it like every other rung so isolation holds.
    """
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(reg, "_mp_content_root_pointer_rung", lambda: "")
    monkeypatch.setattr(reg, "_mp_marketplace_cache_rung", lambda: "")
    monkeypatch.setattr(reg, "_mp_flat_layout_probe_rung", lambda: "")
    monkeypatch.setattr(reg, "_registry_machine_local_get", lambda key: None)


def test_content_root_resolves_via_content_root_pointer_rung(monkeypatch):
    with _tempfile.TemporaryDirectory() as _fake_root:
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(reg, "_mp_content_root_pointer_rung", lambda: _fake_root)
        assert reg.content_root() == _fake_root


def test_content_root_resolves_via_marketplace_cache_rung(monkeypatch):
    """The real marketplace-cache install
    location resolves like the flat-layout rung: as-is, gated on
    `<cand>/state` being a directory (BLOCKER-2)."""
    with _tempfile.TemporaryDirectory() as _fake_root:
        os.makedirs(os.path.join(_fake_root, "state"))
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(reg, "_mp_marketplace_cache_rung", lambda: _fake_root)
        assert reg.content_root() == _fake_root


def test_mp_marketplace_cache_rung_excludes_unparseable_version_dirs(monkeypatch):
    with _tempfile.TemporaryDirectory() as _home:
        _cache_parent = os.path.join(_home, "plugins", "cache", "coordinator-claude", "coordinator")
        os.makedirs(os.path.join(_cache_parent, "0371a29ed35d"))
        os.makedirs(os.path.join(_cache_parent, "021d0d725330"))
        os.makedirs(os.path.join(_cache_parent, "1.2.3"))
        monkeypatch.setattr(reg, "_mlir_claude_home", lambda: _home)
        assert reg._mp_marketplace_cache_rung() == os.path.join(_cache_parent, "1.2.3")


def test_content_root_resolves_via_flat_layout_probe_rung(monkeypatch):
    """The flat marketplace-clone layout is the clone root directly
    (resolve_coordinator_clone.py::resolve_clone_root() treats it the same
    way) — used as-is, no conversion. Gated (Review: staff-eng BLOCKER-2) on
    `<cand>/state` being a directory."""
    with _tempfile.TemporaryDirectory() as _fake_root:
        os.makedirs(os.path.join(_fake_root, "state"))
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(reg, "_mp_flat_layout_probe_rung", lambda: _fake_root)
        assert reg.content_root() == _fake_root


def test_content_root_flat_layout_rejected_without_state_dir(monkeypatch):
    with _tempfile.TemporaryDirectory() as _fake_root:
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(reg, "_mp_flat_layout_probe_rung", lambda: _fake_root)
        with pytest.raises(reg._DoeUnresolvable):
            reg.content_root()


def test_content_root_normalizes_claude_plugin_root_content_root_to_repo_root(monkeypatch):
    """Private/dev layout: CLAUDE_PLUGIN_ROOT is a CONTENT root
    (`<repo_root>/coordinator`), one level below the repo root content_root()
    must return — the plugin-root-vs-content-repo-root distinction this chunk exists
    to close. The marker lives beside the repo root, not beside the content
    root, so the normalizer must climb one level. Gated (Review: staff-eng
    BLOCKER-2) on `<repo_root>/state` being a directory."""
    with _tempfile.TemporaryDirectory() as _repo_root:
        os.makedirs(os.path.join(_repo_root, ".claude-plugin"))
        with open(os.path.join(_repo_root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as _f:
            _f.write("{}")
        os.makedirs(os.path.join(_repo_root, "state"))
        _content_root = os.path.join(_repo_root, "coordinator")
        os.makedirs(_content_root)
        _clear_content_root_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _content_root)
        assert reg.content_root() == _repo_root


def test_content_root_uses_claude_plugin_root_directly_in_oss_flat_layout(monkeypatch):
    """OSS flat layout: CLAUDE_PLUGIN_ROOT already IS the repo root
    (manifest ships flat at plugin root, marker directly beside it) — no
    parent-climb, used as-is. Gated (Review: staff-eng BLOCKER-2) on
    `<flat_root>/state` being a directory."""
    with _tempfile.TemporaryDirectory() as _flat_root:
        os.makedirs(os.path.join(_flat_root, ".claude-plugin"))
        with open(os.path.join(_flat_root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as _f:
            _f.write("{}")
        os.makedirs(os.path.join(_flat_root, "state"))
        _clear_content_root_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _flat_root)
        assert reg.content_root() == _flat_root


def test_content_root_rejects_foreign_plugin_root_over_explicit_override(monkeypatch):
    """
    CLAUDE_PLUGIN_ROOT set to a DIFFERENT plugin's root (no
    .claude-plugin/plugin.json under it, since it belongs to a foreign
    plugin's content, not the plugin root itself) must NOT be accepted, and
    an explicit correct REPO_CONTENT_ROOT override must win instead."""
    with _tempfile.TemporaryDirectory() as _foreign_root, _tempfile.TemporaryDirectory() as _correct_root:
        os.makedirs(os.path.join(_correct_root, "state"), exist_ok=True)
        _clear_content_root_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _foreign_root)
        monkeypatch.setenv("REPO_CONTENT_ROOT", _correct_root)
        assert reg.content_root() == _correct_root


def test_content_root_resolves_via_registry_live_path_rung(monkeypatch):
    """machine-local
    plugin.mirrors.coordinator-claude.live_path is now routed through the
    same CLAUDE_PLUGIN_ROOT-shaped normalizer (not trusted as a repo root
    unconverted) and gated on `<cand>/state` being a directory (BLOCKER-2)."""
    with _tempfile.TemporaryDirectory() as _fake_root:
        os.makedirs(os.path.join(_fake_root, ".claude-plugin"))
        with open(os.path.join(_fake_root, ".claude-plugin", "plugin.json"), "w", encoding="utf-8") as _f:
            _f.write("{}")
        os.makedirs(os.path.join(_fake_root, "state"))
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(
            reg,
            "_registry_machine_local_get",
            lambda key: _fake_root if key == "plugin.mirrors.coordinator-claude.live_path" else None,
        )
        assert reg.content_root() == _fake_root


def test_content_root_rejects_live_path_content_root_without_git(monkeypatch):
    with _tempfile.TemporaryDirectory() as _content_root:
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(
            reg,
            "_registry_machine_local_get",
            lambda key: _content_root if key == "plugin.mirrors.coordinator-claude.live_path" else None,
        )
        with pytest.raises(reg._DoeUnresolvable):
            reg.content_root()


def test_content_root_falls_back_to_legacy_env_chain_when_codename_rungs_unreachable(monkeypatch):
    _clear_content_root_env(monkeypatch)
    monkeypatch.setenv("REPO_CONTENT_ROOT", "/fake/coordinator-content-repo")
    assert reg.content_root() == "/fake/coordinator-content-repo"


def test_content_root_env_override_wins_over_live_pointer_when_both_set(monkeypatch):
    """With a
    live content-root pointer AND an explicit REPO_CONTENT_ROOT
    override both present, the explicit override must win (it is an
    operator's stated intent and cannot be present by accident); ambient
    pointer-file state must not outrank it."""
    with _tempfile.TemporaryDirectory() as _pointer_root, _tempfile.TemporaryDirectory() as _override_root:
        _clear_content_root_env(monkeypatch)
        monkeypatch.setattr(reg, "_mp_content_root_pointer_rung", lambda: _pointer_root)
        monkeypatch.setenv("REPO_CONTENT_ROOT", _override_root)
        assert reg.content_root() == _override_root


def test_content_root_raises_unresolvable_when_every_rung_including_codename_rungs_fails(monkeypatch):
    _clear_content_root_env(monkeypatch)
    with pytest.raises(reg._DoeUnresolvable):
        reg.content_root()


def test_plugin_root_candidate_climbs_content_root_to_repo_root(tmp_path):
    repo_root = tmp_path / "doe-repo"
    content_root = repo_root / "coordinator"
    content_root.mkdir(parents=True)
    (repo_root / ".claude-plugin").mkdir()
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")

    result = reg._mp_repo_root_from_plugin_root_candidate(str(content_root))

    assert result == str(repo_root)


def test_plugin_root_candidate_bare_drive_root_falls_through_unchanged(tmp_path, monkeypatch):
    """KNOWN LATENT DIVERGENCE from coordinator_core's copy (B7): this call
    site uses drive_root_guard="normpath", which -- unlike the engine
    copy's drive_root_guard="preserve" -- truncates a bare Windows
    drive-root-syntax candidate's internal working value from "C:\\" to
    "C:" before probing it. That truncation is normally MASKED because the
    unmatched-fallback branch returns the original `candidate` unchanged
    (not the truncated working value) -- so for the common case (no
    marketplace marker at the drive root) both guard shapes return the
    same thing, pinned here. The divergence becomes externally visible only
    if the truncated "C:" value itself matches something the "C:\\" value
    would not have (e.g. a marketplace marker one path-join away from "C:"
    vs "C:\\") -- reported to the EM/PM per the "KNOWN CROSS-COPY
    DIVERGENCE" docstring note, not reproduced as a live failing case here
    (constructing one needs a real marker at a drive root, not creatable
    from a test fixture).
    abs-path-ok: drive-root syntax fixture, not a hardcoded host path."""
    drive_root = "C:" + "\\"

    result = reg._mp_repo_root_from_plugin_root_candidate(drive_root)

    assert result == drive_root


def test_plugin_root_candidate_basename_casefold_case_insensitive_on_any_platform(tmp_path):
    """KNOWN DIVERGENCE from coordinator_core's copy: this call site uses
    basename_compare="casefold", which is case-insensitive on every
    platform (not just Windows, unlike the engine copy's normcase-based
    compare). Pins current behaviour."""
    repo_root = tmp_path / "doe-repo"
    content_root = repo_root / "COORDINATOR"
    content_root.mkdir(parents=True)
    (repo_root / ".claude-plugin").mkdir()
    (repo_root / ".claude-plugin" / "plugin.json").write_text("{}")

    result = reg._mp_repo_root_from_plugin_root_candidate(str(content_root))

    assert result == str(repo_root)


def test_plugin_root_candidate_no_manifest_relpath_fallback(tmp_path):
    repo_root = tmp_path / "doe-repo"
    content_root = repo_root / "coordinator"
    (content_root / "schemas").mkdir(parents=True)
    (content_root / "schemas" / "coordinator-registry.manifest.json").write_text("{}")

    result = reg._mp_repo_root_from_plugin_root_candidate(str(content_root))

    assert result == str(content_root)


def test_plugin_root_candidate_allow_unchanged_fallback_true_returns_candidate(tmp_path):
    candidate = str(tmp_path / "unrecognized")
    os.makedirs(candidate)

    result = reg._mp_repo_root_from_plugin_root_candidate(candidate, allow_unchanged_fallback=True)

    assert result == candidate


def test_plugin_root_candidate_allow_unchanged_fallback_false_returns_empty(tmp_path):
    candidate = str(tmp_path / "unrecognized")
    os.makedirs(candidate)

    result = reg._mp_repo_root_from_plugin_root_candidate(candidate, allow_unchanged_fallback=False)

    assert result == ""

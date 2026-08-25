"""
Tests for coordinator_core.ops.fleet._memo_resolver — the shared registry resolver.

C3 test surface (docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C3, AC3):
  - read_registry_repos returns {} on "nothing configured", but RAISES RegistryReadError
    on a genuinely unreadable/corrupt registry file (fail-loud, not silent {}).
  - No folder-scan fallback exists anywhere in this module (asserted structurally: a
    forced registry-read failure must propagate as RegistryReadError, never degrade to
    a directory scan or any write).
  - resolve_receiver_inbox raises AmbiguousReceiverError when a central receiver id
    fans in to more than one distinct registered repos.* key.
  - resolve_receiver_inbox still returns (None, None, all_repos) on a legitimate
    zero-match (not registered) — preserves memo_send.py's existing call-site contract.

Spec backlink: docs/decisions/DR-210-makima-native-tooling-ownership-strangler.md
    § Amendment 2026-07-21.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id,
    convention_repo_key,
    read_publish_mirrors,
    read_registry_repos,
    receiver_em_to_repo_key,
    resolve_receiver_inbox,
    resolve_self_em_id,
    same_repo_path,
    suggest_nearest_receiver,
)


def _make_claude_home(tmp_path: Path, receiver_repos: dict[str, Path]) -> Path:
    """Minimal machine-local registry fixture (mirrors test_memo_send.py's factory)."""
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _install_doe_manifest(claude_home: Path, doe_root: Path, manifest: dict) -> None:
    """Install a DoE registry manifest at `doe_root`, reachable via the DR-071 ladder.

    Points the ladder's durable rung (`<settings-home>/machine-local/.doe-root`)
    at `doe_root`. The pre-2026-07-28 fixture wrote `<CLAUDE_HOME>/.doe-root` —
    a location no writer has written since `ops.gen_doe_root_pointer` moved the
    pointer under the settings home, and which `coordinator_core.doe_root_pointer`
    (the canonical resolver `_memo_resolver` consumes) has never read.

    Callers whose registry fixture registers `repos.doe_claude` must pass that
    same path as `doe_root`: the registry rung outranks this file rung, exactly
    as on a real machine, where the two agree.
    """
    schemas_dir = doe_root / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    (schemas_dir / "coordinator-registry.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")


class TestReadRegistryReposFailLoud:
    def test_returns_empty_on_nothing_configured(self, tmp_path, monkeypatch):
        """No registry.toml/registry.local.toml at all → {} (legitimate, not an error)."""
        missing_home = tmp_path / "nonexistent-claude-home"
        missing_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(missing_home))

        assert read_registry_repos() == {}

    def test_raises_on_corrupt_registry_file(self, tmp_path, monkeypatch):
        """A PRESENT but unparseable registry.toml raises RegistryReadError — no silent {}."""
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        # Invalid TOML: unterminated string.
        (machine_local / "registry.toml").write_text(
            'schema = 1\n"repos.broken" = "unterminated\n', encoding="utf-8"
        )
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

        with pytest.raises(RegistryReadError):
            read_registry_repos()

    def test_corrupt_registry_does_not_fall_back_to_folder_scan(self, tmp_path, monkeypatch):
        """A corrupt registry raises loud — it must NOT be swallowed into a scan-derived {}.

        Structural guard for footgun #3 (AC3): the only acceptable outcome of a
        registry-read failure is a raised exception, never a filesystem scan and
        never a silently-empty result indistinguishable from "not configured".
        """
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

        with pytest.raises(RegistryReadError) as exc_info:
            read_registry_repos()
        assert "registry.toml" in str(exc_info.value)


class TestRegistryHomeHonorsMachineLocalImpl:
    """MACHINE_LOCAL_IMPL — the test-isolation override ~8 sibling
    coordinator_core/ops/*.py modules already honour via their own
    _machine_local_impl() helper (e.g. queue_append.py, deliverable_rollup.py) —
    must also redirect this module's DIRECT tomllib registry reads.

    Correction memo: cross-repo/inbox/2026-07-21-claude-central-em-correction-
    no-live-detector-for-double-list-plus-machine-local-impl-gap.md — prior to
    this fix, memo_list's mirror/receiver enumeration (routed through
    registry_home()) never consulted MACHINE_LOCAL_IMPL, so a consumer's test
    fixture that redirects via that ONE documented override saw no rows at all.
    """

    def _make_mocked_settings_home(self, tmp_path: Path, registry_toml_body: str) -> Path:
        """Synthetic settings-home mirroring the real install layout:
        <settings-home>/bin/_machine_local.py sits alongside
        <settings-home>/machine-local/ — the same sibling relationship
        _machine_local_impl()'s own default and machine_local_dir()'s own
        default both resolve against. Returns the impl script path.
        """
        settings_home_root = tmp_path / "mocked-settings-home"
        impl_script = settings_home_root / "bin" / "_machine_local.py"
        impl_script.parent.mkdir(parents=True)
        impl_script.write_text("# stub — never executed by this test\n", encoding="utf-8")

        machine_local = settings_home_root / "machine-local"
        machine_local.mkdir()
        (machine_local / "registry.toml").write_text(registry_toml_body, encoding="utf-8")
        return impl_script

    def test_read_registry_repos_honors_machine_local_impl_override(self, tmp_path, monkeypatch):
        # Point CLAUDE_HOME at an unrelated, EMPTY home — if MACHINE_LOCAL_IMPL were
        # ignored, read_registry_repos would silently fall through to this empty
        # home and return {}, masking the override entirely.
        unrelated_home = tmp_path / "unrelated-claude-home"
        unrelated_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(unrelated_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        impl_script = self._make_mocked_settings_home(
            tmp_path, 'schema = 1\n"repos.project_rag" = "/abs/path/to/project-rag"\n'
        )
        monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl_script))

        assert read_registry_repos() == {"repos.project_rag": "/abs/path/to/project-rag"}

    def test_read_publish_mirrors_honors_machine_local_impl_override(self, tmp_path, monkeypatch):
        """Same override, exercised through read_publish_mirrors() — the seam
        memo_list._enumerate_publish_mirrors() consumes.
        """
        unrelated_home = tmp_path / "unrelated-claude-home"
        unrelated_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(unrelated_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        impl_script = self._make_mocked_settings_home(
            tmp_path,
            "schema = 1\n"
            "[publish.mirrors.coordinator_claude]\n"
            'owner = "claude-central-em"\n'
            'path = "/abs/path/to/mirror"\n',
        )
        monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(impl_script))

        mirrors = read_publish_mirrors()

        assert mirrors["coordinator_claude"]["owner"] == "claude-central-em"
        assert mirrors["coordinator_claude"]["path"] == "/abs/path/to/mirror"

    def test_non_bin_override_falls_back_to_settings_home_resolution(self, tmp_path, monkeypatch):
        """An override NOT under a `bin/` directory carries no settings-home
        information, so settings-home resolution stays authoritative.

        Sibling fixtures (DoE's cross-repo-memo-roundtrip.test.py, cross-repo-
        memo.test.py) point MACHINE_LOCAL_IMPL at a bare `<tmpdir>/_mock_machine_
        local.py` to redirect the DoE-side SPAWN target only. Deriving
        `parent.parent` from that climbs one level ABOVE the tmpdir and resolves
        the registry to an unrelated directory — every receiver then reads as
        "not registered in the machine-local registry", which is how the CLI-path
        memo.send refusal presented (in-process dispatches, which set no
        MACHINE_LOCAL_IMPL, passed the identical case).
        """
        claude_home = _make_claude_home(tmp_path, {"project_rag": tmp_path / "rag-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

        bare_impl = tmp_path / "sender-repo" / "_mock_machine_local.py"
        bare_impl.parent.mkdir(parents=True)
        bare_impl.write_text("# stub — never executed by this test\n", encoding="utf-8")
        monkeypatch.setenv("MACHINE_LOCAL_IMPL", str(bare_impl))

        assert read_registry_repos() == {"repos.project_rag": str(tmp_path / "rag-repo")}

    def test_no_override_falls_back_to_claude_home_resolution(self, tmp_path, monkeypatch):
        """Absent MACHINE_LOCAL_IMPL, registry_home() is unaffected (unchanged
        CLAUDE_HOME/COORDINATOR_SETTINGS_HOME precedence, per existing tests above).
        """
        monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
        claude_home = _make_claude_home(tmp_path, {"project_rag": tmp_path / "rag-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert read_registry_repos() == {"repos.project_rag": str(tmp_path / "rag-repo")}


class TestResolveReceiverInboxZeroMatch:
    def test_unregistered_receiver_returns_none_tuple(self, tmp_path, monkeypatch):
        """Zero-match stays a (None, None, all_repos) return — not an exception.

        Preserves memo_send.py's existing call-site contract (it builds its own
        fail-loud setup-error envelope from the None).
        """
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        inbox_dir, receiver_repo_path, all_repos = resolve_receiver_inbox("nobody-em")

        assert inbox_dir is None
        assert receiver_repo_path is None
        assert all_repos == {}

    def test_registered_receiver_resolves(self, tmp_path, monkeypatch):
        receiver_repo = tmp_path / "rag-repo"
        claude_home = _make_claude_home(tmp_path, {"project_rag": receiver_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        inbox_dir, receiver_repo_path, all_repos = resolve_receiver_inbox("project-rag-em")

        assert receiver_repo_path == receiver_repo
        assert inbox_dir == receiver_repo / "cross-repo" / "inbox"
        assert all_repos["repos.project_rag"] == str(receiver_repo)

    def test_registry_read_failure_propagates(self, tmp_path, monkeypatch):
        """A corrupt registry raises RegistryReadError THROUGH resolve_receiver_inbox too."""
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

        with pytest.raises(RegistryReadError):
            resolve_receiver_inbox("project-rag-em")


class TestAmbiguousCentralReceiver:
    def test_two_central_ids_registered_to_different_repos_is_ambiguous(
        self, tmp_path, monkeypatch
    ):
        """Two distinct central ids both registered to DIFFERENT repos → fail loud.

        Manifest declares centralReceiverIds = ['central-em', 'doe-claude-em']; if the
        machine-local registry has BOTH 'repos.central' and 'repos.doe_claude' populated
        (a genuine misconfiguration/disagreement), the pre-C3 implementation picked one
        arbitrarily via unordered set iteration. Post-C3: raise, don't guess.
        """
        claude_home = _make_claude_home(
            tmp_path,
            {
                "central": tmp_path / "central-repo",
                "doe_claude": tmp_path / "doe-claude-repo",
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        # repos.doe_claude is registered, so the DR-071 ladder's registry rung
        # resolves the DoE root — the manifest must live there.
        _install_doe_manifest(
            claude_home,
            tmp_path / "doe-claude-repo",
            {
                "identity": {
                    "centralReceiverIds": ["central-em", "doe-claude-em"],
                    "repoAliases": [],
                }
            },
        )

        with pytest.raises(AmbiguousReceiverError) as exc_info:
            resolve_receiver_inbox("central-em")
        assert "repos.central" in exc_info.value.candidate_keys
        assert "repos.doe_claude" in exc_info.value.candidate_keys

    def test_single_central_id_registered_resolves_cleanly(self, tmp_path, monkeypatch):
        """Only one central id has a registered repo → resolves without ambiguity."""
        claude_home = _make_claude_home(
            tmp_path, {"doe_claude": tmp_path / "doe-claude-repo"}
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        _install_doe_manifest(
            claude_home,
            tmp_path / "doe-claude-repo",
            {
                "identity": {
                    "centralReceiverIds": ["central-em", "doe-claude-em"],
                    "repoAliases": [],
                }
            },
        )

        inbox_dir, receiver_repo_path, all_repos = resolve_receiver_inbox("central-em")
        assert receiver_repo_path == tmp_path / "doe-claude-repo"


class TestConventionAndAliasMapping:
    def test_convention_repo_key_strips_em_suffix(self):
        assert convention_repo_key("project-rag-em") == "repos.project_rag"

    def test_receiver_em_to_repo_key_convention_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "no-such-home"))
        assert receiver_em_to_repo_key("project-makima-em") == "repos.project_makima"


class TestSuggestNearestReceiver:
    """C4 — 'did you mean?' suggestion surface (footgun #2). Suggests, never resolves."""

    def test_makima_em_suggests_project_makima_em(self):
        """The plan's own worked example: 'makima-em' -> suggests 'project-makima-em'."""
        all_repos = {
            "repos.project_makima": "/abs/path/to/project-makima",
            "repos.project_rag": "/abs/path/to/project-rag",
        }

        suggestion = suggest_nearest_receiver("makima-em", all_repos)

        assert suggestion == "project-makima-em"

    def test_no_candidates_returns_none(self):
        """Empty registry -> nothing to suggest, returns None (not an exception)."""
        assert suggest_nearest_receiver("anything-em", {}) is None

    def test_wildly_unrelated_id_returns_none(self):
        """A receiver id with no close match returns None rather than a bad guess."""
        all_repos = {"repos.project_rag": "/abs/path/to/project-rag"}

        suggestion = suggest_nearest_receiver("xyz-completely-unrelated-zzz", all_repos)

        assert suggestion is None

    def test_exact_match_still_only_suggests_does_not_resolve(self):
        """Even an exact-match id returns a plain suggestion string, not a resolved tuple.

        suggest_nearest_receiver is a suggestion surface only — callers must not
        treat its return value as a resolution; it never returns a Path or opens
        any file.
        """
        all_repos = {"repos.project_rag": "/abs/path/to/project-rag"}

        suggestion = suggest_nearest_receiver("project-rag-em", all_repos)

        assert suggestion == "project-rag-em"
        assert isinstance(suggestion, str)

    def test_suggests_via_alias_shortname_when_alias_registered(self, tmp_path, monkeypatch):
        """An aliased receiver whose registry key IS registered is a valid suggestion candidate."""
        claude_home = tmp_path / "claude-home"
        claude_home.mkdir()
        _install_doe_manifest(
            claude_home,
            tmp_path / "doe-claude-repo",
            {
                "identity": {
                    "centralReceiverIds": [],
                    "repoAliases": [
                        {"shortname": "holodeck", "registryKey": "claude_unreal_holodeck"}
                    ],
                }
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        all_repos = {"repos.claude_unreal_holodeck": "/abs/path/to/holodeck"}

        suggestion = suggest_nearest_receiver("holodck-em", all_repos)

        assert suggestion == "holodeck-em"


class TestCanonicalReceiverId:
    """canonical_receiver_id() — the addressee-gate normalization the `to:`
    frontmatter field is stamped from (memo_send.py), not the raw caller string.
    """

    def _make_manifest(
        self,
        tmp_path,
        claude_home,
        *,
        doe_root=None,
        central_ids=None,
        redirect_aliases=None,
        aliases=None,
    ):
        """`doe_root` defaults to a path nothing else claims; a caller whose
        registry fixture registers `repos.doe_claude` must pass that path, since
        the DR-071 ladder's registry rung outranks the pointer file."""
        manifest = {
            "identity": {
                "repoAliases": aliases or [],
                "centralReceiverIds": central_ids or [],
                "redirectAliases": redirect_aliases or [],
            }
        }
        _install_doe_manifest(
            claude_home, doe_root or (tmp_path / "fake-doe-root"), manifest
        )

    def test_central_alias_canonicalizes_to_registered_key(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {"doe_claude": tmp_path / "doe-claude-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(
            tmp_path, claude_home,
            doe_root=tmp_path / "doe-claude-repo",
            central_ids=["claude-central-em", "central-em", "central", "doe-claude-em"],
        )

        for alias in ("claude-central-em", "central-em", "central", "doe-claude-em"):
            assert canonical_receiver_id(alias) == "doe-claude-em", alias

    def test_redirect_alias_canonicalizes_to_same_central_id(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {"doe_claude": tmp_path / "doe-claude-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(
            tmp_path, claude_home,
            doe_root=tmp_path / "doe-claude-repo",
            central_ids=["claude-central-em", "doe-claude-em"],
            redirect_aliases=[".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em"],
        )

        for redirect in (".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em"):
            assert canonical_receiver_id(redirect) == "doe-claude-em", redirect

    def test_non_central_receiver_returned_unchanged(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {"project_rag": tmp_path / "rag-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(tmp_path, claude_home, central_ids=["doe-claude-em"])

        assert canonical_receiver_id("project-rag-em") == "project-rag-em"

    def test_manifest_absent_is_passthrough_noop(self, tmp_path, monkeypatch):
        """No .doe-root sentinel at all → both central_ids/redirect_aliases are
        empty sets, so ANY input passes through stripped/lowercased, never a crash."""
        missing_home = tmp_path / "no-doe-root-home"
        missing_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(missing_home))

        assert canonical_receiver_id("claude-central-em") == "claude-central-em"
        assert canonical_receiver_id("  Project-Rag-EM  ") == "project-rag-em"

    def test_ambiguous_central_ids_raises(self, tmp_path, monkeypatch):
        """Two central ids registered to two different repos → fail loud, same
        as resolve_receiver_inbox's own ambiguity contract."""
        claude_home = _make_claude_home(
            tmp_path,
            {"central": tmp_path / "central-repo", "doe_claude": tmp_path / "doe-claude-repo"},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(
            tmp_path, claude_home,
            doe_root=tmp_path / "doe-claude-repo",
            central_ids=["central-em", "doe-claude-em"],
        )

        with pytest.raises(AmbiguousReceiverError):
            canonical_receiver_id("central-em")

    def test_registry_read_failure_propagates(self, tmp_path, monkeypatch):
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        claude_home = tmp_path / "claude-home"
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(tmp_path, claude_home, central_ids=["doe-claude-em"])

        with pytest.raises(RegistryReadError):
            canonical_receiver_id("doe-claude-em")

    def test_central_alias_with_no_registered_repo_passes_through(self, tmp_path, monkeypatch):
        """Central alias declared in the manifest, but nothing registered anywhere
        → nothing to canonicalize TO, passthrough (resolve_receiver_inbox is the
        authority that turns this into a fail-loud setup error at send-time)."""
        claude_home = _make_claude_home(tmp_path, {"unrelated": tmp_path / "unrelated-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(tmp_path, claude_home, central_ids=["doe-claude-em"])

        assert canonical_receiver_id("doe-claude-em") == "doe-claude-em"


class TestResolveSelfEmId:
    """resolve_self_em_id() — THE ONE self-identity resolver (2026-07-26
    subprocess-elision spinoff, `_memo_resolver.py:815-856`). Both
    `compute_addressee_gate`'s `self:` line and `compute_reply_closure`'s
    sender-id derivation route through this; prior to this, only indirect
    coverage existed via `test_pickup_assemble.py`'s non-aliased fixtures
    (2026-07-26 review finding 3).
    """

    def _make_manifest(self, tmp_path, claude_home, *, aliases=None):
        _install_doe_manifest(
            claude_home,
            tmp_path / "fake-doe-root",
            {
                "identity": {
                    "repoAliases": aliases or [],
                    "centralReceiverIds": [],
                    "redirectAliases": [],
                }
            },
        )

    def test_matches_non_aliased_registered_repo(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "project-makima"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_makima": self_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert resolve_self_em_id(self_repo) == "project-makima-em"

    def test_matches_aliased_registered_repo(self, tmp_path, monkeypatch):
        """2026-07-26 review finding 1: an aliased repo must resolve to the
        SAME id the DoE CLI's `repo_key_to_em_id` produces
        (`claude_unreal_holodeck` -> `holodeck` -> `holodeck-em`), not the
        naive underscore->dash convention (`claude-unreal-holodeck-em`)."""
        self_repo = tmp_path / "claude-unreal-holodeck"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"claude_unreal_holodeck": self_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        self._make_manifest(
            tmp_path, claude_home,
            aliases=[{"shortname": "holodeck", "registryKey": "claude_unreal_holodeck"}],
        )

        assert resolve_self_em_id(self_repo) == "holodeck-em"

    def test_matches_repos_doe_claude(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "DoE-claude"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"doe_claude": self_repo})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert resolve_self_em_id(self_repo) == "doe-claude-em"

    def test_unregistered_repo_falls_back_to_basename_convention(self, tmp_path, monkeypatch):
        self_repo = tmp_path / "some-unregistered-repo"
        self_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": tmp_path / "rag-repo"})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        assert resolve_self_em_id(self_repo) == "some-unregistered-repo-em"

    def test_registry_read_failure_falls_back_to_basename_never_raises(
        self, tmp_path, monkeypatch
    ):
        """Self-identity derivation is best-effort/display-only and must never
        raise, even on a genuinely corrupt registry file."""
        self_repo = tmp_path / "some-repo"
        self_repo.mkdir()
        machine_local = tmp_path / "claude-home" / ".coordinator-claude-settings" / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))

        assert resolve_self_em_id(self_repo) == "some-repo-em"


class TestSameRepoPath:
    """same_repo_path() — THE ONE path-equality helper for receiver/self
    resolution (`_memo_resolver.py:796-812`). Lift-and-shift of the
    pre-existing `_same_path`; direct coverage was previously only
    incidental (2026-07-26 review finding 3)."""

    def test_existing_paths_match_via_samefile(self, tmp_path):
        a = tmp_path / "repo"
        a.mkdir()
        assert same_repo_path(a, Path(str(a))) is True

    def test_existing_paths_differ(self, tmp_path):
        a = tmp_path / "repo-a"
        b = tmp_path / "repo-b"
        a.mkdir()
        b.mkdir()
        assert same_repo_path(a, b) is False

    def test_nonexistent_path_falls_back_to_normcase_realpath(self, tmp_path):
        """Neither path exists (registry entry pointing at a not-yet-cloned
        sibling) → os.path.samefile raises OSError, falls back to
        normcase+realpath string comparison rather than raising."""
        a = tmp_path / "not-yet-cloned"
        assert same_repo_path(a, Path(str(a))) is True
        assert same_repo_path(a, tmp_path / "different-not-cloned") is False

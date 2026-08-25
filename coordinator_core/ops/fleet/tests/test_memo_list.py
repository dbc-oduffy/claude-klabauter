"""
Tests for coordinator_core.ops.fleet.memo_list — memo.list COMPUTE_ONLY UDS op.

C2 test surface (docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C2, AC2):
  - setup-error envelope on bad params (missing/wrong-typed dry_run, dry_run:false,
    non-string to)
  - enumeration mode: every registered repos.* key appears as a dry_run candidate
  - resolution mode: a registered `to` resolves to its destination inbox path
  - resolution mode: an unresolved `to` reports resolved:false with a reason
    (and a C4 "did you mean?" suggestion when a close match is registered)
  - no-write proof: enumeration and resolution both leave the filesystem
    byte-for-byte unchanged (no new files/dirs anywhere under tmp_path)
  - store-less-ness architecture test (DR-210 Open-Q §2; mirrors memo_send.py's
    C6/AC8 TestNoMemoIndex, applied here for C2's memo.list)

Harness: asyncio.run() in sync test functions — no pytest-asyncio dependency.
Pattern: mirrors test_memo_send.py's registry fixture factory.

Spec backlink: pln-memo-tool-rebuild-claude-klabauter-owns--bd5745 § C2
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from coordinator_core.ops.fleet.memo_list import (
    _MODE,
    _memo_list,
    _validate_list_params,
)
from coordinator_core.ops.fleet._memo_compose import (
    _ENGINE_ACTOR_ID,
    _TOPIC_SLUG_RE,
    _memo_filename,
    resolve_sender_id,
)


def _run(result):
    """Run async coroutine synchronously, or pass a plain result through
    unchanged (some handlers this file exercises are now plain `def`)."""
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _make_claude_home(
    tmp_path: Path,
    receiver_repos: dict[str, str],
    mirror_tables: dict[str, dict] | None = None,
) -> Path:
    """Minimal machine-local registry fixture (mirrors test_memo_send.py's factory).

    receiver_repos: {registry_key_suffix: repo_path_str} e.g. {"example_retrieval_repo": "/..."}
    → writes "repos.example_retrieval_repo" = <path> in registry.local.toml.

    mirror_tables: {mirror_key: {"owner": str, "path": str (optional),
    "aliases": list[str] (optional)}} — writes REAL bracket-table TOML syntax
    (`[publish.mirrors.<key>]` + `owner = "..."` etc.) in registry.toml,
    exactly as a hand-authored `registry.toml` and `test_memo_draft.py`'s own
    `_make_claude_home` fixture do (`aliases`, when present, as a genuine TOML
    list — not a newline-joined string). Review Finding 2
    (state/review-trail/findings/2026-07-21-codereview-slicememo-clean-split-op-coverage-coordinator-core-ops-fleet-memo-draft-py.md):
    the PRIOR fixture wrote flat quoted-dotted-key strings
    (`"publish.mirrors.X.owner" = "..."`), a shape only the old, incorrect
    `_read_registry_raw()` flat-string merge could parse — it does not match
    what `tomllib` produces for real `[publish.mirrors.X]` tables (a nested
    dict), so the old fixture never exercised the real-world shape and papered
    over the bug this fixture now reproduces.
    """
    claude_home = tmp_path / "claude-home"
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True)

    baseline_lines = ["schema = 1"]
    for mirror_key, entry in (mirror_tables or {}).items():
        baseline_lines.append(f"\n[publish.mirrors.{mirror_key}]")
        owner = entry.get("owner")
        if owner is not None:
            baseline_lines.append(f'owner = "{owner}"')
        path = entry.get("path")
        if path is not None:
            toml_path = str(path).replace("\\", "\\\\").replace('"', '\\"')
            baseline_lines.append(f'path = "{toml_path}"')
        aliases = entry.get("aliases")
        if aliases:
            alias_list = ", ".join(f'"{a}"' for a in aliases)
            baseline_lines.append(f"aliases = [{alias_list}]")
    (machine_local / "registry.toml").write_text(
        "\n".join(baseline_lines) + "\n", encoding="utf-8"
    )

    lines = []
    for key_suffix, repo_path in receiver_repos.items():
        toml_val = str(repo_path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key_suffix}" = "{toml_val}"')
    (machine_local / "registry.local.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return claude_home


def _write_doe_manifest(
    claude_home: Path, tmp_path: Path, manifest: dict, doe_root: Path | None = None
) -> None:
    """Write a .doe-root sentinel + coordinator-registry.manifest.json fixture.

    Mirrors test_memo_check_addressee.py's `_write_doe_manifest` pattern —
    a hermetic tmp_path-scoped manifest, never the real machine's DoE tree.

    The sentinel lands on the DR-071 ladder's durable rung
    (`<settings-home>/machine-local/.doe-root`), not the pre-2026-07-28
    `<CLAUDE_HOME>/.doe-root` — a location no writer has written since
    `ops.gen_doe_root_pointer` moved the pointer under the settings home.
    A caller whose registry fixture registers `repos.doe_claude` must pass
    that path as `doe_root`; the registry rung outranks the pointer file.
    """
    doe_root = doe_root or (tmp_path / "doe-root")
    schemas_dir = doe_root / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    machine_local = claude_home / ".coordinator-claude-settings" / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / ".doe-root").write_text(str(doe_root), encoding="utf-8")
    (schemas_dir / "coordinator-registry.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _receivers(candidates: list) -> list:
    """Filter an enumeration-mode candidates list down to kind:'receiver' entries."""
    return [c for c in candidates if c.get("kind") == "receiver"]


def _snapshot(tmp_path: Path) -> set:
    """Return the set of all paths (files + dirs) under tmp_path, for a
    before/after no-write comparison."""
    return {str(p) for p in tmp_path.rglob("*")}


# ===========================================================================
# 1. setup-error envelope on bad params
# ===========================================================================

class TestSetupErrorEnvelope:
    def test_dry_run_missing(self):
        result = _validate_list_params({})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_not_bool(self):
        result = _validate_list_params({"dry_run": "yes"})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_dry_run_false_rejected(self):
        """memo.list has no act mode — dry_run:false is a setup error, not a no-op."""
        result = _validate_list_params({"dry_run": False})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_to_wrong_type_rejected(self):
        result = _validate_list_params({"dry_run": True, "to": 12345})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_from_id_wrong_type_rejected(self):
        result = _validate_list_params({"dry_run": True, "from_id": 12345})
        assert isinstance(result, dict)
        assert result["exit_code"] == 1

    def test_from_id_absent_is_fine(self):
        """from_id is entirely optional — its absence validates cleanly."""
        result = _validate_list_params({"dry_run": True})
        assert not isinstance(result, dict)
        dry_run, to, topic, from_id = result
        assert from_id is None

    def test_handler_dry_run_false_returns_setup_error(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        result = _run(_memo_list({"dry_run": False}))
        assert result["exit_code"] == 1
        assert result["dry_run"] is False


# ===========================================================================
# 2. Enumeration mode (no `to`)
# ===========================================================================

class TestEnumerationMode:
    def test_enumerates_all_registered_receivers(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "example-retrieval-repo"
        holo_repo = tmp_path / "example-game-workbench-repo"
        rag_repo.mkdir()
        holo_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"example_retrieval_repo": str(rag_repo), "example_game_workbench_repo": str(holo_repo)},
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        assert result["mode"] == _MODE
        assert result["dry_run"] is True
        assert result["acted"] == []
        assert result["skipped"] == []
        assert result["failed"] == []

        receivers = _receivers(result["candidates"])
        ids = {c["repo_key"] for c in receivers}
        assert ids == {"repos.example_retrieval_repo", "repos.example_game_workbench_repo"}
        for c in receivers:
            assert c["resolved"] is True
            assert c["target_inbox"].endswith(os.path.join("cross-repo", "inbox"))
            # is_central must be machine-readable, not left for the client to infer.
            assert c["is_central"] is False
            assert c["aliases"] == []

    def test_empty_registry_yields_no_receivers_but_registry_status_present(
        self, tmp_path, monkeypatch
    ):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        assert _receivers(result["candidates"]) == []
        # No receivers/mirrors/aliases configured is NOT the same as a
        # registry-read failure — the positive registry_status entry must
        # still be present (soft-status signal, not a fallback trigger).
        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True

    def test_no_registry_configured_yields_no_receivers(self, tmp_path, monkeypatch):
        """Neither registry.toml nor registry.local.toml present → {} (not an error)."""
        missing_home = tmp_path / "nonexistent-claude-home"
        missing_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(missing_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        assert _receivers(result["candidates"]) == []
        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True


# ===========================================================================
# 2b. Enumeration mode — publish_mirrors section (kind: "publish_mirror")
# ===========================================================================

class TestEnumerationPublishMirrors:
    def test_publish_mirrors_land_in_own_section_not_conflated_with_receivers(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"example_retrieval_repo": str(rag_repo)},
            mirror_tables={
                "deep_research_claude": {
                    "owner": "deep-research-em",
                    "path": "/some/mirror/path",
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        candidates = result["candidates"]

        receivers = _receivers(candidates)
        receiver_ids = {c["id"] for c in receivers}
        assert "deep_research_claude" not in receiver_ids
        assert "deep-research-claude-em" not in receiver_ids
        for c in receivers:
            assert c["kind"] == "receiver"

        mirrors = [c for c in candidates if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        mirror = mirrors[0]
        assert mirror["mirror_key"] == "deep_research_claude"
        assert mirror["em_id"] == "deep-research-claude-em"
        assert mirror["owner"] == "deep-research-em"
        assert mirror["path"] == "/some/mirror/path"
        assert mirror["is_receiver"] is False
        assert "deep-research-claude" in mirror["aliases"]
        assert "deep-research-claude-em" in mirror["aliases"]

    def test_no_publish_mirrors_configured_yields_empty_mirror_section(
        self, tmp_path, monkeypatch
    ):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        mirrors = [c for c in result["candidates"] if c["kind"] == "publish_mirror"]
        assert mirrors == []


# ===========================================================================
# 2c. Enumeration mode — canonical_home_alias section + is_central flag
# ===========================================================================

class TestEnumerationAliasesAndCentral:
    def test_canonical_home_alias_section_present_and_empty_when_field_absent(
        self, tmp_path, monkeypatch
    ):
        """identity.redirectAliases is not yet promoted on any machine today
        (per _memo_resolver.read_redirect_aliases()'s own docstring) — absence
        must yield an empty section, not an error."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        aliases = [c for c in result["candidates"] if c["kind"] == "canonical_home_alias"]
        assert aliases == []

    def test_is_central_from_settings_home_sentinel_with_no_legacy_pointer(
        self, tmp_path, monkeypatch
    ):
        """The DoE receiver is flagged is_central when the ONLY doe-root pointer
        on the machine is the durable `<settings-home>/machine-local/.doe-root`.

        This is the exact shape of every machine installed since
        `ops.gen_doe_root_pointer` stopped writing `~/.claude/.doe-root` (that
        path is inside the git-tracked `~/.claude` meta-repo, so per-machine
        clone paths fought over one synced file). The three manifest readers in
        `_memo_resolver` still read only that retired legacy location, so
        `read_central_receiver_ids()` came back empty and `--list-receivers`
        rendered "repos.doe_claude not registered on this machine" on a machine
        where the receiver was registered and delivery to it worked.
        """
        doe_repo = tmp_path / "doe-claude-repo"
        doe_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"doe_claude": str(doe_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # `doe_root=doe_repo` is required, per _write_doe_manifest's own contract:
        # this fixture registers `repos.doe_claude`, and the registry rung outranks
        # the pointer file. Omitting it wrote the manifest under `tmp_path/doe-root`
        # while the winning rung resolved to `doe_repo` — a mismatch that stayed
        # latent only while the reader ignored the ladder entirely and consulted
        # the legacy pointer alone.
        _write_doe_manifest(
            claude_home,
            tmp_path,
            {"identity": {"centralReceiverIds": ["central-em", "doe-claude-em"]}},
            doe_root=doe_repo,
        )
        # The legacy rungs must be genuinely absent — both the retired location
        # the readers used to consult and the ladder's own legacy rung.
        assert not (claude_home / ".doe-root").exists()
        assert not (claude_home / ".claude" / ".doe-root").exists()

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        receivers = _receivers(result["candidates"])
        doe = [c for c in receivers if c["repo_key"] == "repos.doe_claude"]
        assert len(doe) == 1
        assert doe[0]["is_central"] is True

    def test_registry_status_entry_shape(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True
        assert isinstance(statuses[0]["note"], str) and statuses[0]["note"]


# ===========================================================================
# 2d. Enumeration mode — redirect-alias precedence over publish_mirror
#     (defect fix: same id emitted twice with contradictory guidance)
# ===========================================================================

class TestRedirectPrecedenceOverPublishMirror:
    def test_fully_shadowed_mirror_is_omitted_entirely(self, tmp_path, monkeypatch):
        """Every alias of a mirror is also a redirect alias -> mirror entry
        absent, redirect entries present, no id duplicated across kinds."""
        claude_home = _make_claude_home(
            tmp_path,
            {},
            mirror_tables={
                "coordinator_claude": {"owner": "claude-central-em"},
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_doe_manifest(
            claude_home,
            tmp_path,
            {
                "identity": {
                    "redirectAliases": [
                        ".claude-em",
                        "claude-home",
                        "coordinator-claude",
                        "coordinator-claude-em",
                    ],
                }
            },
        )

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        candidates = result["candidates"]

        mirrors = [c for c in candidates if c["kind"] == "publish_mirror"]
        assert mirrors == [], (
            "coordinator_claude's alias surface (coordinator-claude, "
            "coordinator-claude-em) is fully shadowed by redirectAliases — "
            "the mirror entry must be omitted entirely, not emitted empty."
        )

        aliases = [c for c in candidates if c["kind"] == "canonical_home_alias"]
        alias_ids = {c["id"] for c in aliases}
        assert alias_ids == {
            ".claude-em", "claude-home", "coordinator-claude", "coordinator-claude-em",
        }

        # Regression assertion for the invariant itself: no id appears in
        # both a publish_mirror addressable surface and a
        # canonical_home_alias entry.
        mirror_surface: set = set()
        for m in mirrors:
            mirror_surface.add(m["id"])
            if m["em_id"]:
                mirror_surface.add(m["em_id"])
            mirror_surface.update(m["aliases"])
        assert mirror_surface.isdisjoint(alias_ids)

    def test_partial_shadowing_mirror_survives_with_non_colliding_alias_only(
        self, tmp_path, monkeypatch
    ):
        """Mirror with one colliding alias and one non-colliding alias ->
        entry SURVIVES with only the non-colliding alias — the naive
        "drop the whole mirror on any collision" shape would wrongly delete
        this entry's genuine owner-routing info."""
        claude_home = _make_claude_home(
            tmp_path,
            {},
            mirror_tables={
                "deep_research_claude": {
                    "owner": "deep-research-em",
                    "path": "/some/mirror/path",
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Only ONE of deep_research_claude's two mechanically-derived aliases
        # (deep-research-claude, deep-research-claude-em) collides.
        _write_doe_manifest(
            claude_home,
            tmp_path,
            {"identity": {"redirectAliases": ["deep-research-claude-em"]}},
        )

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        mirrors = [c for c in result["candidates"] if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        mirror = mirrors[0]
        assert mirror["mirror_key"] == "deep_research_claude"
        assert mirror["owner"] == "deep-research-em"
        assert mirror["path"] == "/some/mirror/path"
        assert mirror["aliases"] == ["deep-research-claude"]
        assert "deep-research-claude-em" not in mirror["aliases"]
        # em_id itself was the colliding id -> must not be advertised.
        assert mirror["em_id"] is None

        aliases = [c for c in result["candidates"] if c["kind"] == "canonical_home_alias"]
        alias_ids = {c["id"] for c in aliases}
        assert alias_ids == {"deep-research-claude-em"}

        mirror_surface = {mirror["id"], *mirror["aliases"]}
        assert mirror_surface.isdisjoint(alias_ids)

    def test_degraded_no_redirect_field_matches_pre_fix_behavior(
        self, tmp_path, monkeypatch
    ):
        """`redirectAliases` absent from the manifest -> mirror enumeration
        byte-identical to pre-fix behavior (subtraction of set() is a no-op)."""
        claude_home = _make_claude_home(
            tmp_path,
            {},
            mirror_tables={
                "deep_research_claude": {
                    "owner": "deep-research-em",
                    "path": "/some/mirror/path",
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        # Manifest present but without identity.redirectAliases at all.
        _write_doe_manifest(
            claude_home, tmp_path, {"identity": {"repoAliases": []}}
        )

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        mirrors = [c for c in result["candidates"] if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        mirror = mirrors[0]
        assert mirror["mirror_key"] == "deep_research_claude"
        assert mirror["em_id"] == "deep-research-claude-em"
        assert mirror["owner"] == "deep-research-em"
        assert mirror["path"] == "/some/mirror/path"
        assert "deep-research-claude" in mirror["aliases"]
        assert "deep-research-claude-em" in mirror["aliases"]

        aliases = [c for c in result["candidates"] if c["kind"] == "canonical_home_alias"]
        assert aliases == []


# ===========================================================================
# 2e. Enumeration mode — repos.* / publish.mirrors.* PATH collision
#     (defect fix: an id could appear both as a valid `--to` receiver AND
#     as a publish_mirror, with memo.send refusing the receiver form —
#     memo_list must agree with memo_send's send-path authority)
# ===========================================================================

class TestReceiverMirrorPathCollision:
    def test_repo_shadowed_by_mirror_path_excluded_from_receivers(
        self, tmp_path, monkeypatch
    ):
        """A repos.* entry whose path matches a publish.mirrors.*.path is
        excluded from the receiver block and surfaces only as a
        publish_mirror — mirrors memo.send's own refusal of that `to`."""
        shared_repo = tmp_path / "claude-klabauter"
        shared_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"claude_klabauter": str(shared_repo)},
            mirror_tables={
                "claude_klabauter": {
                    "owner": "claude-central-em",
                    "path": str(shared_repo),
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        candidates = result["candidates"]

        receivers = _receivers(candidates)
        assert receivers == [], (
            "repos.claude_klabauter resolves to the same path as "
            "publish.mirrors.claude_klabauter -> memo.send refuses it, so "
            "it must not appear as a receiver."
        )

        mirrors = [c for c in candidates if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        assert mirrors[0]["mirror_key"] == "claude_klabauter"
        assert mirrors[0]["path"] == str(shared_repo)

        # Regression assertion for the invariant: no id's path is ever
        # addressable as both a receiver and a publish_mirror.
        receiver_paths = {c["repo_path"] for c in receivers}
        assert str(shared_repo) not in receiver_paths

    def test_normal_sibling_with_no_mirror_collision_unaffected(
        self, tmp_path, monkeypatch
    ):
        """A repos.* entry with no path collision against any mirror is
        entirely unaffected by the new exclusion check."""
        rag_repo = tmp_path / "example-retrieval-repo"
        mirror_repo = tmp_path / "unrelated-mirror"
        rag_repo.mkdir()
        mirror_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"example_retrieval_repo": str(rag_repo)},
            mirror_tables={
                "deep_research_claude": {
                    "owner": "deep-research-em",
                    "path": str(mirror_repo),
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        receivers = _receivers(result["candidates"])
        assert {c["repo_key"] for c in receivers} == {"repos.example_retrieval_repo"}

        mirrors = [c for c in result["candidates"] if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        assert mirrors[0]["mirror_key"] == "deep_research_claude"

    def test_redirect_alias_subtraction_still_behaves_alongside_path_collision(
        self, tmp_path, monkeypatch
    ):
        """The pre-existing per-id redirect-alias subtraction (drop-if-empty)
        is unaffected by the new path-collision exclusion — both mechanisms
        coexist without interfering with each other."""
        shared_repo = tmp_path / "claude-klabauter"
        shared_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"claude_klabauter": str(shared_repo)},
            mirror_tables={
                "claude_klabauter": {
                    "owner": "claude-central-em",
                    "path": str(shared_repo),
                },
                "deep_research_claude": {
                    "owner": "deep-research-em",
                    "path": str(tmp_path / "unrelated-mirror-2"),
                },
            },
        )
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_doe_manifest(
            claude_home,
            tmp_path,
            {"identity": {"redirectAliases": ["deep-research-claude-em"]}},
        )

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        candidates = result["candidates"]

        # Path-collision exclusion still applies to claude_klabauter.
        receivers = _receivers(candidates)
        assert receivers == []

        # Redirect-alias subtraction still applies to deep_research_claude,
        # unrelated to the path-collision mechanism.
        mirrors = [c for c in candidates if c["kind"] == "publish_mirror"]
        mirror_keys = {m["mirror_key"] for m in mirrors}
        assert "claude_klabauter" in mirror_keys
        assert "deep_research_claude" in mirror_keys
        drc = next(m for m in mirrors if m["mirror_key"] == "deep_research_claude")
        assert drc["aliases"] == ["deep-research-claude"]
        assert drc["em_id"] is None

        aliases = [c for c in candidates if c["kind"] == "canonical_home_alias"]
        assert {a["id"] for a in aliases} == {"deep-research-claude-em"}


# ===========================================================================
# 3. Resolution mode (`to` supplied) — the --dry-run/--check verb
# ===========================================================================

class TestResolutionMode:
    def test_resolved_to_reports_destination_inbox(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))

        assert result["exit_code"] == 0
        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True
        assert candidate["receiver"] == "example-retrieval-repo-em"
        assert candidate["target_inbox"] == str(rag_repo / "cross-repo" / "inbox")
        assert candidate["note"] is None

    def test_unresolved_to_reports_resolved_false_with_reason(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "unregistered-em"}))

        assert result["exit_code"] == 0  # still a clean preview, not a setup error
        candidate = result["candidates"][0]
        assert candidate["resolved"] is False
        assert candidate["target_inbox"] is None
        assert "not registered" in candidate["note"]

    def test_unresolved_to_suggests_nearest_match(self, tmp_path, monkeypatch):
        """C4 parity: claude-klabauter-em -> suggests claude-klabauter-em, same as memo.send."""
        claude_klabauter_repo = tmp_path / "claude-klabauter"
        claude_klabauter_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": str(claude_klabauter_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "claude-klabauter-em"}))

        candidate = result["candidates"][0]
        assert candidate["resolved"] is False
        assert "claude-klabauter-em" in candidate["note"]


# ===========================================================================
# 3b. resolved_filename — DR-026 filename-authority exposure (topic + to)
# ===========================================================================

class TestResolvedFilename:
    def test_resolved_to_plus_topic_yields_resolved_filename_matching_send(
        self, tmp_path, monkeypatch
    ):
        """to + topic, to resolves -> resolved_filename equals memo_send's own
        _memo_filename output for the same sender/topic (locks the two
        together — any drift between the two ops fails this test)."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import datetime

        result = _run(
            _memo_list(
                {"dry_run": True, "to": "example-retrieval-repo-em", "topic": "example-topic"}
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True

        today = datetime.date.today().isoformat()
        expected = _memo_filename(today, _ENGINE_ACTOR_ID, "example-topic")
        assert candidate["resolved_filename"] == expected

    def test_non_claude_klabauter_caller_preview_matches_send_shared_derivation(
        self, tmp_path, monkeypatch
    ):
        """The defect this closes: a non-claude-klabauter caller's from_id must produce
        the SAME filename memo.send would actually write for that caller —
        asserted against the shared `resolve_sender_id` + `_memo_filename`
        derivation (not a hardcoded string that could drift from either)."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import datetime

        result = _run(
            _memo_list({
                "dry_run": True,
                "to": "example-retrieval-repo-em",
                "topic": "smoke",
                "from_id": "claude-central-em",
            })
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True

        today = datetime.date.today().isoformat()
        # Shared-authority assertion: what memo.send would actually write for
        # this exact from_id/topic pair, via the identical two functions
        # memo_list._resolve_candidate calls internally.
        expected = _memo_filename(
            today, resolve_sender_id("claude-central-em"), "smoke"
        )
        assert candidate["resolved_filename"] == expected
        # Sanity: this must NOT be the engine-actor-namespaced filename —
        # that was exactly the defect (preview always showed claude-klabauter-engine
        # regardless of the caller's actual from_id).
        assert "claude-klabauter-engine" not in candidate["resolved_filename"]
        assert "claude-central-em" in candidate["resolved_filename"]

    def test_claude_klabauter_origin_caller_still_previews_correctly(self, tmp_path, monkeypatch):
        """Guard against regressing the currently-accidentally-correct case:
        a caller that supplies NO from_id (claude-klabauter-origin / engine-default
        send) still previews with the engine actor id — identical to
        memo.send's own no-from_id default."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        import datetime

        result = _run(
            _memo_list({"dry_run": True, "to": "example-retrieval-repo-em", "topic": "smoke"})
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        today = datetime.date.today().isoformat()
        expected = _memo_filename(today, resolve_sender_id(None), "smoke")
        assert candidate["resolved_filename"] == expected
        assert _ENGINE_ACTOR_ID in candidate["resolved_filename"]

    def test_unknown_caller_identity_fails_loud_not_engine_fallback(
        self, tmp_path, monkeypatch
    ):
        """Degraded case: a from_id that sanitizes to an empty sender slug
        (all-punctuation/non-ASCII) is a caller identity memo.list cannot
        resolve into a real filename — mirrors memo.send's own posture
        (_memo_filename raises ValueError there too) via a fail-loud
        exit_code:1 setup-error envelope, NEVER a silent fallback to the
        engine actor id (that fallback is exactly the wrong-but-confident
        shape this whole fix exists to remove)."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_list({
                "dry_run": True,
                "to": "example-retrieval-repo-em",
                "topic": "smoke",
                "from_id": "!!!",
            })
        )

        assert result["exit_code"] == 1
        assert "resolved_filename" not in str(result)
        assert "claude-klabauter-engine" not in str(result)

    def test_to_only_resolution_has_no_resolved_filename(self, tmp_path, monkeypatch):
        """to supplied without topic -> unchanged from before this addition:
        no resolved_filename key at all."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))

        candidate = result["candidates"][0]
        assert candidate["resolved"] is True
        assert "resolved_filename" not in candidate

    def test_topic_only_no_to_has_no_effect(self, tmp_path, monkeypatch):
        """topic without to -> enumeration mode unaffected (to gates resolution
        mode entirely; topic alone does nothing)."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "topic": "example-topic"}))

        assert result["exit_code"] == 0
        for candidate in result["candidates"]:
            assert "resolved_filename" not in candidate

    def test_unresolved_to_plus_topic_has_no_resolved_filename(
        self, tmp_path, monkeypatch
    ):
        """to unresolved + topic supplied -> no resolved_filename (resolution
        gate requires BOTH to resolve AND topic to be present)."""
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_list(
                {"dry_run": True, "to": "unregistered-em", "topic": "example-topic"}
            )
        )

        candidate = result["candidates"][0]
        assert candidate["resolved"] is False
        assert "resolved_filename" not in candidate

    def test_invalid_topic_fails_loud_locked_to_memo_send_regex(self, tmp_path, monkeypatch):
        """to resolves + an INVALID topic (slash) -> exit_code:1 setup error,
        no resolved_filename anywhere — and the same input is independently
        confirmed to fail memo.send's own _TOPIC_SLUG_RE (locks the two
        validators together, mirroring the filename-function lock above)."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        bad_topic = "bad/topic with spaces"
        assert not _TOPIC_SLUG_RE.fullmatch(bad_topic), (
            "test fixture bug: bad_topic must actually be invalid per "
            "memo.send's own regex for this lock to mean anything"
        )

        result = _run(
            _memo_list({"dry_run": True, "to": "example-retrieval-repo-em", "topic": bad_topic})
        )

        assert result["exit_code"] == 1
        assert "resolved_filename" not in str(result)

    def test_empty_string_topic_fails_loud_not_coerced_to_absent(self, tmp_path, monkeypatch):
        """An explicitly-passed empty/whitespace-only topic fails loud via the
        same regex, rather than being silently treated as 'no topic'."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_list({"dry_run": True, "to": "example-retrieval-repo-em", "topic": "   "})
        )

        assert result["exit_code"] == 1

    def test_absent_topic_key_still_behaves_as_before(self, tmp_path, monkeypatch):
        """A genuinely absent topic key (not passed at all) is fine — resolution
        proceeds with no resolved_filename, no validation error."""
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True
        assert "resolved_filename" not in candidate


# ===========================================================================
# 4. No-write proof (AC2: provably side-effect-free)
# ===========================================================================

class TestNoWriteProof:
    def test_enumeration_leaves_filesystem_unchanged(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        before = _snapshot(tmp_path)
        _run(_memo_list({"dry_run": True}))
        after = _snapshot(tmp_path)

        assert after == before, (
            "memo.list enumeration must not write/create anything on disk "
            f"(AC2 no-write proof). New paths: {after - before}"
        )

    def test_resolution_leaves_filesystem_unchanged_resolved(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        before = _snapshot(tmp_path)
        _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))
        after = _snapshot(tmp_path)

        assert after == before, (
            "memo.list resolution (resolved case) must not write/create anything "
            f"on disk (AC2 no-write proof). New paths: {after - before}"
        )

    def test_resolution_leaves_filesystem_unchanged_unresolved(self, tmp_path, monkeypatch):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        before = _snapshot(tmp_path)
        _run(_memo_list({"dry_run": True, "to": "unregistered-em"}))
        after = _snapshot(tmp_path)

        assert after == before, (
            "memo.list resolution (unresolved case) must not write/create anything "
            f"on disk. New paths: {after - before}"
        )


# ===========================================================================
# 5. Store-less-ness architecture test (DR-210 Open-Q §2; mirrors memo_send.py
#    C6/AC8 TestNoMemoIndex, applied here for C2)
# ===========================================================================

class TestNoMemoIndex:
    """memo.list must remain a pure read, no retained fleet-wide index.

    Two layers, mirroring test_memo_send.py's TestNoMemoIndex exactly:
      1. Structural: no module-level mutable collection (dict/list/set) in
         coordinator_core.ops.fleet.memo_list.
      2. Runtime: repeated handler calls leave module globals unchanged.

    Spec backlink:
        docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C2
        ("a `test_no_memo_index`-shaped store-less-ness architecture test").
    """

    def test_no_memo_index(self):
        import types
        import coordinator_core.ops.fleet.memo_list as memo_list_mod

        module_globals = {
            name: val
            for name, val in vars(memo_list_mod).items()
            if not name.startswith("__")
        }
        mutable_collections = {
            name: val
            for name, val in module_globals.items()
            if isinstance(val, (dict, list, set))
            and not isinstance(val, types.ModuleType)
        }

        assert mutable_collections == {}, (
            f"memo_list module MUST NOT contain module-level mutable collections "
            f"(dict/list/set) — any such binding would violate the Q-d store-less-ness "
            f"invariant. Found violating names: {sorted(mutable_collections.keys())}"
        )

    def test_handler_calls_do_not_mutate_module_state(self, tmp_path, monkeypatch):
        import coordinator_core.ops.fleet.memo_list as memo_list_mod

        rag_repo = tmp_path / "example-retrieval-repo"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"example_retrieval_repo": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        def _mutable_module_names():
            return frozenset(
                name
                for name, val in vars(memo_list_mod).items()
                if isinstance(val, (dict, list, set)) and not name.startswith("__")
            )

        names_before = _mutable_module_names()

        _run(_memo_list({"dry_run": True}))
        _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))
        _run(_memo_list({"dry_run": True, "to": "unregistered-em"}))

        names_after = _mutable_module_names()

        assert names_after == names_before, (
            f"memo_list module must not accumulate new module-level mutable state "
            f"across handler calls (Q-d store-less-ness invariant). "
            f"Names that appeared after handler calls: {sorted(names_after - names_before)}"
        )

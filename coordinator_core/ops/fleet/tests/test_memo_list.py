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

import pytest

from coordinator_core.ops.fleet.memo_list import (
    _MODE,
    _memo_list,
    _validate_list_params,
)
from coordinator_core.ops.fleet._memo_compose import (
    _TOPIC_SLUG_RE,
    _memo_filename,
    resolve_sender_id,
)


@pytest.fixture(autouse=True)
def _drop_settings_home_override(monkeypatch):
    """Neutralise ``COORDINATOR_SETTINGS_HOME`` for every test in this module.

    ``_make_claude_home`` below writes its registry under
    ``<CLAUDE_HOME>/.coordinator-claude-settings/machine-local`` and points
    CLAUDE_HOME at it, which only isolates the resolver while
    ``_settings_home.settings_home()`` derives the settings home from
    CLAUDE_HOME. That resolver consults ``COORDINATOR_SETTINGS_HOME`` first and
    the suite-root home quarantine (``coordinator_core/conftest.py::
    _quarantine_real_home``) does not clear it, so on a box where an operator
    exports it these tests enumerate the operator's REAL fleet registry rather
    than the one they seeded. Same isolation ``test_memo_resolver.py`` applies.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)


def _run(result):
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _make_claude_home(
    tmp_path: Path,
    receiver_repos: dict[str, str],
    mirror_tables: dict[str, dict] | None = None,
) -> Path:
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
    return [c for c in candidates if c.get("kind") == "receiver"]


def _snapshot(tmp_path: Path) -> set:
    return {str(p) for p in tmp_path.rglob("*")}


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


class TestEnumerationMode:
    def test_enumerates_all_registered_receivers(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        holo_repo = tmp_path / "example-game-workbench-repo"
        rag_repo.mkdir()
        holo_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"project_rag": str(rag_repo), "example_game_workbench_repo": str(holo_repo)},
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
        assert ids == {"repos.project_rag", "repos.example_game_workbench_repo"}
        for c in receivers:
            assert c["resolved"] is True
            assert c["target_inbox"].endswith(os.path.join("cross-repo", "inbox"))
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
        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True

    def test_no_registry_configured_yields_no_receivers(self, tmp_path, monkeypatch):
        missing_home = tmp_path / "nonexistent-claude-home"
        missing_home.mkdir()
        monkeypatch.setenv("CLAUDE_HOME", str(missing_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        assert _receivers(result["candidates"]) == []
        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True


class TestEnumerationPublishMirrors:
    def test_publish_mirrors_land_in_own_section_not_conflated_with_receivers(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"project_rag": str(rag_repo)},
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


class TestEnumerationAliasesAndCentral:
    def test_canonical_home_alias_section_present_and_empty_when_field_absent(
        self, tmp_path, monkeypatch
    ):
        claude_home = _make_claude_home(tmp_path, {})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        aliases = [c for c in result["candidates"] if c["kind"] == "canonical_home_alias"]
        assert aliases == []

    def test_is_central_from_settings_home_sentinel_with_no_legacy_pointer(
        self, tmp_path, monkeypatch
    ):
        doe_repo = tmp_path / "doe-claude-repo"
        doe_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"doe_claude": str(doe_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
        _write_doe_manifest(
            claude_home,
            tmp_path,
            {"identity": {"centralReceiverIds": ["central-em", "doe-claude-em"]}},
            doe_root=doe_repo,
        )
        assert not (claude_home / ".doe-root").exists()
        assert not (claude_home / ".claude" / ".doe-root").exists()

        result = _run(_memo_list({"dry_run": True}))

        assert result["exit_code"] == 0
        receivers = _receivers(result["candidates"])
        doe = [c for c in receivers if c["repo_key"] == "repos.doe_claude"]
        assert len(doe) == 1
        assert doe[0]["is_central"] is True

    def test_registry_status_entry_shape(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True}))

        statuses = [c for c in result["candidates"] if c["kind"] == "registry_status"]
        assert len(statuses) == 1
        assert statuses[0]["ok"] is True
        assert isinstance(statuses[0]["note"], str) and statuses[0]["note"]


class TestRedirectPrecedenceOverPublishMirror:
    def test_fully_shadowed_mirror_is_omitted_entirely(self, tmp_path, monkeypatch):
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
        assert mirror["em_id"] is None

        aliases = [c for c in result["candidates"] if c["kind"] == "canonical_home_alias"]
        alias_ids = {c["id"] for c in aliases}
        assert alias_ids == {"deep-research-claude-em"}

        mirror_surface = {mirror["id"], *mirror["aliases"]}
        assert mirror_surface.isdisjoint(alias_ids)

    def test_degraded_no_redirect_field_matches_pre_fix_behavior(
        self, tmp_path, monkeypatch
    ):
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


class TestReceiverMirrorPathCollision:
    def test_repo_shadowed_by_mirror_path_excluded_from_receivers(
        self, tmp_path, monkeypatch
    ):
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

        receiver_paths = {c["repo_path"] for c in receivers}
        assert str(shared_repo) not in receiver_paths

    def test_normal_sibling_with_no_mirror_collision_unaffected(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "project-rag"
        mirror_repo = tmp_path / "unrelated-mirror"
        rag_repo.mkdir()
        mirror_repo.mkdir()
        claude_home = _make_claude_home(
            tmp_path,
            {"project_rag": str(rag_repo)},
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
        assert {c["repo_key"] for c in receivers} == {"repos.project_rag"}

        mirrors = [c for c in result["candidates"] if c["kind"] == "publish_mirror"]
        assert len(mirrors) == 1
        assert mirrors[0]["mirror_key"] == "deep_research_claude"

    def test_redirect_alias_subtraction_still_behaves_alongside_path_collision(
        self, tmp_path, monkeypatch
    ):
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

        receivers = _receivers(candidates)
        assert receivers == []

        mirrors = [c for c in candidates if c["kind"] == "publish_mirror"]
        mirror_keys = {m["mirror_key"] for m in mirrors}
        assert "claude_klabauter" in mirror_keys
        assert "deep_research_claude" in mirror_keys
        drc = next(m for m in mirrors if m["mirror_key"] == "deep_research_claude")
        assert drc["aliases"] == ["deep-research-claude"]
        assert drc["em_id"] is None

        aliases = [c for c in candidates if c["kind"] == "canonical_home_alias"]
        assert {a["id"] for a in aliases} == {"deep-research-claude-em"}


class TestResolutionMode:
    def test_resolved_to_reports_destination_inbox(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is False
        assert candidate["target_inbox"] is None
        assert "not registered" in candidate["note"]

    def test_unresolved_to_suggests_nearest_match(self, tmp_path, monkeypatch):
        claude_klabauter_repo = tmp_path / "claude-klabauter"
        claude_klabauter_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"claude_klabauter": str(claude_klabauter_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "claude-klabauter-em"}))

        candidate = result["candidates"][0]
        assert candidate["resolved"] is False
        assert "claude-klabauter-em" in candidate["note"]


class TestResolvedFilename:
    def test_resolved_to_plus_topic_yields_resolved_filename_matching_send(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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
        expected = _memo_filename(today, resolve_sender_id(None), "example-topic")
        assert candidate["resolved_filename"] == expected

    def test_non_claude_klabauter_caller_preview_matches_send_shared_derivation(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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
        expected = _memo_filename(
            today, resolve_sender_id("claude-central-em"), "smoke"
        )
        assert candidate["resolved_filename"] == expected
        assert "claude-klabauter-engine" not in candidate["resolved_filename"]
        assert "claude-central-em" in candidate["resolved_filename"]

    def test_claude_klabauter_origin_caller_still_previews_correctly(self, tmp_path, monkeypatch):
        """Guard against regressing the currently-accidentally-correct case:
        a caller that supplies NO from_id (claude-klabauter-origin / engine-default
        send) still previews with the SAME resolved sender id memo.send
        would actually sign with — a RESOLVED RECEIVER identity
        (`resolve_sender_id(None)`), never a fixed `claude-klabauter-engine` literal
        (DoE e267d18336 withdrew the concurrence that made that literal
        sufficient)."""
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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
        assert "claude-klabauter-engine" not in candidate["resolved_filename"]

    def test_root_threaded_preview_matches_caller_worktree_not_ambient_cwd(
        self, tmp_path, monkeypatch
    ):
        """memo.list's
        `repo_root` handler param must be threaded through to the defaulted-
        sender resolution, never left to the engine process's ambient cwd
        under the warm resident engine (DR-315: one process serves several
        callers' repos). Registers two DISTINCT repos — one the caller's
        actual `repo_root`, one what `_resolve_repo_root()`'s ambient-cwd
        probe would return — and asserts the preview resolves against the
        former. Fails on the unfixed code (which called
        `resolve_sender_id(from_id)` with no `root`, falling through to the
        ambient-cwd fallback)."""
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        caller_repo = tmp_path / "caller-repo"
        caller_repo.mkdir()
        ambient_repo = tmp_path / "ambient-repo"
        ambient_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {
            "project_rag": str(rag_repo),
            "caller_repo": str(caller_repo),
            "ambient_repo": str(ambient_repo),
        })
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        from coordinator_core.ops.fleet import _memo_compose as mc
        monkeypatch.setattr(mc, "_resolve_repo_root", lambda: str(ambient_repo))

        import datetime

        result = _run(
            _memo_list(
                {"dry_run": True, "to": "example-retrieval-repo-em", "topic": "root-thread"},
                repo_root=caller_repo,
            )
        )

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True

        today = datetime.date.today().isoformat()
        expected = _memo_filename(
            today, resolve_sender_id(None, root=str(caller_repo)), "root-thread"
        )
        assert candidate["resolved_filename"] == expected
        assert "caller-repo-em" in candidate["resolved_filename"]
        assert "ambient-repo-em" not in candidate["resolved_filename"]

    def test_unknown_caller_identity_fails_loud_not_engine_fallback(
        self, tmp_path, monkeypatch
    ):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))

        candidate = result["candidates"][0]
        assert candidate["resolved"] is True
        assert "resolved_filename" not in candidate

    def test_topic_only_no_to_has_no_effect(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "topic": "example-topic"}))

        assert result["exit_code"] == 0
        for candidate in result["candidates"]:
            assert "resolved_filename" not in candidate

    def test_unresolved_to_plus_topic_has_no_resolved_filename(
        self, tmp_path, monkeypatch
    ):
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
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(
            _memo_list({"dry_run": True, "to": "example-retrieval-repo-em", "topic": "   "})
        )

        assert result["exit_code"] == 1

    def test_absent_topic_key_still_behaves_as_before(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        result = _run(_memo_list({"dry_run": True, "to": "example-retrieval-repo-em"}))

        assert result["exit_code"] == 0
        candidate = result["candidates"][0]
        assert candidate["resolved"] is True
        assert "resolved_filename" not in candidate


class TestNoWriteProof:
    def test_enumeration_leaves_filesystem_unchanged(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        before = _snapshot(tmp_path)
        _run(_memo_list({"dry_run": True}))
        after = _snapshot(tmp_path)

        assert after == before, (
            "memo.list enumeration must not write/create anything on disk "
            f"(AC2 no-write proof). New paths: {after - before}"
        )

    def test_resolution_leaves_filesystem_unchanged_resolved(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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


class TestNoMemoIndex:

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

        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
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


class TestListAndResolverAgreeOnInboxTarget:
    def _both_targets(self, tmp_path, monkeypatch, repo_path, receiver_em_id):
        from coordinator_core.ops.fleet._memo_resolver import resolve_receiver_inbox

        enum_result = _run(_memo_list({"dry_run": True}))
        receivers = {c["repo_key"]: c for c in _receivers(enum_result["candidates"])}
        list_target = receivers["repos.project_rag"]["target_inbox"]

        inbox_dir, _receiver_repo_path, _all_repos = resolve_receiver_inbox(receiver_em_id)
        resolver_target = str(inbox_dir)

        return list_target, resolver_target

    def test_unmigrated_receiver_list_and_resolver_agree(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        rag_repo.mkdir()
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        list_target, resolver_target = self._both_targets(
            tmp_path, monkeypatch, rag_repo, "example-retrieval-repo-em"
        )

        assert list_target == resolver_target
        assert list_target == str(rag_repo / "cross-repo" / "inbox")

    def test_migrated_receiver_list_and_resolver_agree(self, tmp_path, monkeypatch):
        rag_repo = tmp_path / "project-rag"
        (rag_repo / "state" / "cross-repo").mkdir(parents=True)
        claude_home = _make_claude_home(tmp_path, {"project_rag": str(rag_repo)})
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home))

        list_target, resolver_target = self._both_targets(
            tmp_path, monkeypatch, rag_repo, "example-retrieval-repo-em"
        )

        assert list_target == resolver_target
        # Positive assertion anchored on the MIGRATED root specifically.
        assert list_target == str(rag_repo / "state" / "cross-repo" / "inbox")

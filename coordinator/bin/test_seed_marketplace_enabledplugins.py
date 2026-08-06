#!/usr/bin/env python3
"""
test_seed_marketplace_enabledplugins.py — tests for seed-marketplace-enabledplugins.py.

Mirrors claude-klabauter's shipped `seed_enabled_plugins.py` test shape (9-test
skeleton), extended per the Director of Engineering F2/F3's fixture-matrix mandate: nested manifest,
multi-plugin manifest, malformed manifest (skip+warn+continue), the
`enabled_plugins_key` trap, and the effective-merged-view clobber check
(committed settings.json X:false + local absent -> not seeded).

Extended by C7 for D4/AC9 (marketplace registration): present manifest-bearing
siblings get an `extraKnownMarketplaces` entry in `settings.local.json` AND a
matching entry in `known_marketplaces.json`; manifestless siblings correctly
no-op; re-run is idempotent (no duplicates); an existing entry in either
target file (any shape) is never clobbered. Shares C1/C2's enumeration
fixture substrate (same registry + manifest helpers) since D1 and D4 walk the
same present-sibling list.

Spec backlink: docs/plans/2026-07-16-seed-marketplace-enabledplugins-at-install.md
  § C2 — AC1, AC2, AC3, AC4, AC5, AC8. § C7 — AC9.

Tests:
  1. --help exits 0. Smoke test.
  2. AC1: present sibling with a root manifest is seeded true; a present
     sibling with no discoverable manifest correctly no-ops (no crash, no key).
  3. AC2: idempotent re-run — second run seeds nothing and leaves the file's
     enabledPlugins content byte-identical; an existing explicit false in the
     TARGET file for a present sibling's key is preserved (not flipped true).
  4. AC3: enumeration is manifest-driven — a registry entry with no matching
     directory on disk is silently skipped (not an error); presence in a
     hardcoded table is never consulted (there is none).
  5. AC4: a manifest named "coordinator-claude" (the inline-load marketplace
     name) is never seeded, even when it ships a plugins[] array; only `true`
     is ever written (never `false`) anywhere in the module's own writes.
  6. AC5a: malformed (non-JSON) settings.local.json -> fails loud, exit != 0,
     nothing written (verified: reading the raw bytes back is unchanged).
  7. AC5b: non-dict `enabledPlugins` in settings.local.json -> fails loud,
     nothing written.
  8. AC5c: --check-only computes and reports but writes nothing (settings.local.json
     absent before and after).
  9. AC5d: --settings-path is honored (writes land at the explicit path, not
     the default resolution).
  10. Nested manifest (example-game-repo-shape): manifest only discoverable at
      <repo>/plugin/.claude-plugin/marketplace.json is found and seeded.
  11. Multi-plugin marketplace: one manifest with 3 plugins[] entries seeds 3
      distinct keys, all sharing the same "@marketplace" suffix.
  12. Malformed manifest skip+warn+continue: one sibling's manifest is invalid
      JSON; a second, valid sibling is still seeded; run exits 0 (not abort);
      stderr names the skipped sibling.
  13. enabled_plugins_key trap: a plugins[] entry carries
      install.enabled_plugins_key = "<bare-name>" that differs from its own
      "name" field; the seeded key uses "name@marketplace", never the bare
      enabled_plugins_key value alone.
  14. Effective-merged-view clobber (the Director of Engineering F3): committed settings.json has
      "<key>": false for a present sibling's key; settings.local.json has no
      entry for that key at all. After running, settings.local.json still has
      no entry for that key (seeder does not write true over an elsewhere-false).
  15. AC9: present manifest-bearing sibling gets an extraKnownMarketplaces
      entry in settings.local.json AND a matching known_marketplaces.json
      entry (source/installLocation/lastUpdated); a manifestless sibling
      correctly no-ops on both files.
  16. AC9: idempotent re-run of registration — second run adds no duplicate
      entries and leaves both files' registration content byte-identical.
  17. AC9: an existing extraKnownMarketplaces entry (any shape) in
      settings.local.json, and an existing known_marketplaces.json entry
      (any shape), are both preserved untouched — merge-never-clobber.

Run with: python3 -m pytest coordinator/bin/test_seed_marketplace_enabledplugins.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

# getattr fallback: CREATE_NO_WINDOW only exists on Windows; returns 0 (no-op) on POSIX.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _script_path() -> str:
    """Return the absolute path to seed-marketplace-enabledplugins.py."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "seed-marketplace-enabledplugins.py"
    )


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable — the interpreter running this test script is always a
    valid Python interpreter. Windows-compatible zero-probe pattern (avoids
    FileNotFoundError from subprocess probing python3/python on Windows).
    """
    return sys.executable


def _run_cli(
    args: list[str], env: dict[str, str] | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess:
    """Invoke the CLI as a subprocess.

    Always drives via `python <script>` — mirrors test_coordinator_queue_append.py's
    subprocess pattern. Starts from an EMPTY env (not os.environ) for the seeder-
    relevant vars (CLAUDE_CONFIG_DIR / CLAUDE_HOME / MACHINE_LOCAL_REGISTRY_DIR /
    COORDINATOR_SETTINGS_HOME / CHECK_ONLY) so a developer's real machine-local
    registry / settings never leaks into a test run (AC3's "registry/manifest
    enumeration only" anti-scope, applied defensively here too).
    """
    effective_env = {**os.environ}
    for leak_guard in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_HOME",
        "MACHINE_LOCAL_REGISTRY_DIR",
        "COORDINATOR_SETTINGS_HOME",
        "CHECK_ONLY",
    ):
        effective_env.pop(leak_guard, None)
    if env:
        effective_env.update(env)
    return subprocess.run(
        [_python(), _script_path()] + args,
        env=effective_env,
        capture_output=True,
        text=True,
        cwd=cwd,
        creationflags=_NO_WINDOW,
    )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_registry(registry_dir: str, repos: dict[str, str]) -> None:
    """Write registry.local.toml with quoted-literal `"repos.<slug>"` keys.

    The seeder reads top-level keys starting with the literal string
    "repos." (not TOML dotted-key nesting) — a quoted key
    `"repos.slug" = "value"` is the TOML shape that produces exactly that.
    """
    os.makedirs(registry_dir, exist_ok=True)
    lines = []
    for slug, path in repos.items():
        escaped_path = path.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{slug}" = "{escaped_path}"')
    with open(os.path.join(registry_dir, "registry.local.toml"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_manifest(
    repo_dir: str, plugins: list[dict], marketplace_name: str, nested: bool = False
) -> None:
    """Write a marketplace.json manifest at repo root or the nested plugin/ location."""
    base = os.path.join(repo_dir, "plugin") if nested else repo_dir
    manifest_dir = os.path.join(base, ".claude-plugin")
    os.makedirs(manifest_dir, exist_ok=True)
    manifest = {"name": marketplace_name, "plugins": plugins}
    with open(os.path.join(manifest_dir, "marketplace.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


def _write_malformed_manifest(repo_dir: str) -> None:
    """Write invalid JSON at the root manifest location."""
    manifest_dir = os.path.join(repo_dir, ".claude-plugin")
    os.makedirs(manifest_dir, exist_ok=True)
    with open(os.path.join(manifest_dir, "marketplace.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _base_args(
    settings_path: str,
    committed_path: str,
    registry_dir: str,
    extra: list[str] | None = None,
) -> list[str]:
    args = [
        "--settings-path",
        settings_path,
        "--committed-settings-path",
        committed_path,
        "--registry-dir",
        registry_dir,
    ]
    if extra:
        args += extra
    return args


def _registration_args(
    settings_path: str,
    committed_path: str,
    registry_dir: str,
    known_marketplaces_path: str,
    extra: list[str] | None = None,
) -> list[str]:
    """Like `_base_args` but also pins `--known-marketplaces-path` (D4/C7's
    second write target) for test isolation from the developer's real
    ~/.claude/plugins/known_marketplaces.json."""
    args = _base_args(settings_path, committed_path, registry_dir, extra=extra)
    args += ["--known-marketplaces-path", known_marketplaces_path]
    return args


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_help_smoke() -> None:
    name = "help_smoke"
    result = _run_cli(["--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--help exited {result.returncode}, stderr={result.stderr!r}"))
    if not result.stdout.strip():
        raise AssertionError(f"{name}: " + ("expected non-empty --help output"))


def test_ac1_present_sibling_seeded_manifestless_noop() -> None:
    name = "ac1_present_sibling_seeded_manifestless_noop"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        repo_b = os.path.join(tmp, "repo-b")  # present, no manifest
        os.makedirs(repo_a)
        os.makedirs(repo_b)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a, "repo_b": repo_b})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")

        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        if enabled.get("pluginA@marketA") is not True:
            raise AssertionError(f"{name}: " + (f"expected pluginA@marketA=true, got {enabled}"))
        if len(enabled) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 seeded key (manifestless repo-b no-ops), got {enabled}"))


def test_registry_local_wins_and_empty_tracked_is_a_miss() -> None:
    """DR-071 axis coverage: registry.local.toml must win over a tracked
    registry.toml for the same repos.* slug, and an empty-string declaration
    in the tracked file (no .local override) must not be surfaced as a
    present (empty-path) repo."""
    name = "registry_local_wins_and_empty_tracked_is_a_miss"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        os.makedirs(registry_dir, exist_ok=True)
        with open(os.path.join(registry_dir, "registry.toml"), "w", encoding="utf-8") as f:
            f.write('"repos.repo_a" = "/tracked/wrong/path"\n"repos.repo_b" = ""\n')
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")

        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        if enabled.get("pluginA@marketA") is not True:
            raise AssertionError(f"{name}: " + (f".local repo_a value did not win over tracked: {enabled}"))
        if len(enabled) != 1:
            raise AssertionError(f"{name}: " + (f"empty-string tracked repo_b was surfaced as present: {enabled}"))


def test_ac2_idempotent_and_preserve_existing() -> None:
    name = "ac2_idempotent_and_preserve_existing"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}, {"name": "pluginB"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        # Pre-seed pluginB explicitly false in the TARGET file — must survive.
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({"enabledPlugins": {"pluginB@marketA": False}}, f)

        args = _base_args(settings_path, committed_path, registry_dir)
        r1 = _run_cli(args)
        if r1.returncode != 0:
            raise AssertionError(f"{name}: " + (f"first run exited {r1.returncode}, stderr={r1.stderr!r}"))

        after_first = _read_json(settings_path)
        enabled1 = after_first.get("enabledPlugins", {})
        if enabled1.get("pluginA@marketA") is not True:
            raise AssertionError(f"{name}: " + (f"expected pluginA@marketA=true after first run, got {enabled1}"))
        if enabled1.get("pluginB@marketA") is not False:
            raise AssertionError(f"{name}: " + (f"expected pluginB@marketA to stay false, got {enabled1}"))

        raw_after_first = open(settings_path, encoding="utf-8").read()
        r2 = _run_cli(args)
        if r2.returncode != 0:
            raise AssertionError(f"{name}: " + (f"second run exited {r2.returncode}, stderr={r2.stderr!r}"))
        if "nothing to seed" not in r2.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'nothing to seed' on idempotent re-run, got stdout={r2.stdout!r}"))
        raw_after_second = open(settings_path, encoding="utf-8").read()
        if raw_after_first != raw_after_second:
            raise AssertionError(f"{name}: " + ("re-run mutated settings.local.json content — not idempotent"))


def test_ac3_manifest_driven_enumeration() -> None:
    name = "ac3_manifest_driven_enumeration"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        registered_but_absent = os.path.join(tmp, "repo-never-checked-out")
        _write_registry(registry_dir, {"repo_a": repo_a, "ghost": registered_but_absent})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        if enabled.get("pluginA@marketA") is not True:
            raise AssertionError(f"{name}: " + (f"expected pluginA@marketA=true, got {enabled}"))
        if len(enabled) != 1:
            raise AssertionError(f"{name}: " + (f"a registered-but-not-checked-out sibling must not appear, got {enabled}"))


def test_ac4_no_self_entry_never_false() -> None:
    name = "ac4_no_self_entry_never_false"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        coordinator_repo = os.path.join(tmp, "coordinator-repo")
        os.makedirs(coordinator_repo)
        _write_manifest(
            coordinator_repo, [{"name": "coordinator"}], "coordinator-claude"
        )
        _write_registry(registry_dir, {"coordinator_repo": coordinator_repo})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        if os.path.exists(settings_path):
            data = _read_json(settings_path)
            enabled = data.get("enabledPlugins", {})
            if enabled:
                raise AssertionError(f"{name}: " + (f"expected no coordinator self-entry seeded, got {enabled}"))
            if any(v is False for v in enabled.values()):
                raise AssertionError(f"{name}: " + (f"seeder must never write false, got {enabled}"))


def test_ac5a_failloud_malformed_local() -> None:
    name = "ac5a_failloud_malformed_local"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        malformed = "{ not valid json at all"
        with open(settings_path, "w", encoding="utf-8") as f:
            f.write(malformed)

        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("expected non-zero exit on malformed settings.local.json"))
        raw_after = open(settings_path, encoding="utf-8").read()
        if raw_after != malformed:
            raise AssertionError(f"{name}: " + ("malformed settings.local.json was overwritten — must fail loud without writing"))


def test_ac5b_failloud_nondict_enabledplugins() -> None:
    name = "ac5b_failloud_nondict_enabledplugins"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({"enabledPlugins": "not-a-dict"}, f)
        raw_before = open(settings_path, encoding="utf-8").read()

        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("expected non-zero exit on non-dict enabledPlugins"))
        raw_after = open(settings_path, encoding="utf-8").read()
        if raw_after != raw_before:
            raise AssertionError(f"{name}: " + ("settings.local.json was written despite non-dict enabledPlugins"))


def test_ac5c_check_only_writes_nothing() -> None:
    name = "ac5c_check_only_writes_nothing"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        if os.path.exists(settings_path):
            raise AssertionError(f"{name}: " + ("fixture setup bug: settings.local.json should not pre-exist"))

        result = _run_cli(
            _base_args(settings_path, committed_path, registry_dir, extra=["--check-only"])
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--check-only exited {result.returncode}, stderr={result.stderr!r}"))
        if os.path.exists(settings_path):
            raise AssertionError(f"{name}: " + ("--check-only must not create settings.local.json"))
        if "would seed" not in result.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'would seed' report in stdout, got {result.stdout!r}"))


def test_ac5d_settings_path_honored() -> None:
    name = "ac5d_settings_path_honored"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        explicit_path = os.path.join(tmp, "custom-dir", "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(explicit_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))
        if not os.path.isfile(explicit_path):
            raise AssertionError(f"{name}: " + ("--settings-path was not honored — file not written at explicit path"))


def test_nested_manifest_example_game_repo_shape() -> None:
    name = "nested_manifest_example_game_repo_shape"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "example-game-repo-repo")
        os.makedirs(repo_a)
        _write_manifest(
            repo_a,
            [{"name": "example-game-repo"}, {"name": "example-game-repo-control"}, {"name": "game-dev"}],
            "example-game-workbench-repo",
            nested=True,
        )
        _write_registry(registry_dir, {"example_game_repo_repo": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        expected = {
            "example-game-repo@example-game-workbench-repo",
            "example-game-repo-control@example-game-workbench-repo",
            "game-dev@example-game-workbench-repo",
        }
        if set(enabled.keys()) != expected:
            raise AssertionError(f"{name}: " + (f"expected keys {expected}, got {set(enabled.keys())}"))
        if not all(v is True for v in enabled.values()):
            raise AssertionError(f"{name}: " + (f"expected all values true, got {enabled}"))


def test_multi_plugin_marketplace() -> None:
    name = "multi_plugin_marketplace"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(
            repo_a, [{"name": "pluginX"}, {"name": "pluginY"}], "marketMulti"
        )
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        expected = {"pluginX@marketMulti", "pluginY@marketMulti"}
        if set(enabled.keys()) != expected:
            raise AssertionError(f"{name}: " + (f"expected keys {expected}, got {set(enabled.keys())}"))


def test_malformed_manifest_skip_warn_continue() -> None:
    name = "malformed_manifest_skip_warn_continue"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_bad = os.path.join(tmp, "repo-bad")
        repo_good = os.path.join(tmp, "repo-good")
        os.makedirs(repo_bad)
        os.makedirs(repo_good)
        _write_malformed_manifest(repo_bad)
        _write_manifest(repo_good, [{"name": "pluginGood"}], "marketGood")
        _write_registry(registry_dir, {"repo_bad": repo_bad, "repo_good": repo_good})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"a malformed sibling manifest must not abort the run; exited"
                f" {result.returncode}, stderr={result.stderr!r}"))
        if "repo_bad" not in result.stderr and "repo-bad" not in result.stderr:
            raise AssertionError(f"{name}: " + (f"expected stderr to name the skipped sibling, got {result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        if enabled.get("pluginGood@marketGood") is not True:
            raise AssertionError(f"{name}: " + (f"expected the good sibling still seeded, got {enabled}"))


def test_enabled_plugins_key_trap() -> None:
    name = "enabled_plugins_key_trap"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(
            repo_a,
            [
                {
                    "name": "realName",
                    "install": {"enabled_plugins_key": "bareName"},
                }
            ],
            "marketA",
        )
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        enabled = data.get("enabledPlugins", {})
        if "realName@marketA" not in enabled:
            raise AssertionError(f"{name}: " + (f"expected realName@marketA (the 'name'-derived key), got {enabled}"))
        if "bareName" in enabled or "bareName@marketA" in enabled:
            raise AssertionError(f"{name}: " + (f"enabled_plugins_key ('bareName') must never be used as or in the seeded"
                f" key — got {enabled}"))


def test_effective_merged_view_clobber() -> None:
    name = "effective_merged_view_clobber"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        # Committed (fleet-synced) settings.json explicitly disables pluginA;
        # settings.local.json has no entry for it at all.
        with open(committed_path, "w", encoding="utf-8") as f:
            json.dump({"enabledPlugins": {"pluginA@marketA": False}}, f)

        result = _run_cli(_base_args(settings_path, committed_path, registry_dir))
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        if os.path.exists(settings_path):
            data = _read_json(settings_path)
            enabled = data.get("enabledPlugins", {})
            if "pluginA@marketA" in enabled:
                raise AssertionError(f"{name}: " + (f"seeder must not seed a key that is explicitly false in the"
                    f" committed settings.json, got {enabled}"))
        # committed settings.json must never be written to.
        committed_after = _read_json(committed_path)
        if committed_after != {"enabledPlugins": {"pluginA@marketA": False}}:
            raise AssertionError(f"{name}: " + (f"committed settings.json was mutated: {committed_after}"))


def test_ac9_registration_seeded_manifestless_noop() -> None:
    name = "ac9_registration_seeded_manifestless_noop"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        repo_b = os.path.join(tmp, "repo-b")  # present, no manifest
        os.makedirs(repo_a)
        os.makedirs(repo_b)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a, "repo_b": repo_b})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        known_marketplaces_path = os.path.join(tmp, "plugins", "known_marketplaces.json")

        result = _run_cli(
            _registration_args(settings_path, committed_path, registry_dir, known_marketplaces_path)
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        extra = data.get("extraKnownMarketplaces", {})
        if "marketA" not in extra:
            raise AssertionError(f"{name}: " + (f"expected extraKnownMarketplaces['marketA'], got {extra}"))
        entry = extra["marketA"]
        if entry.get("source", {}).get("source") != "directory":
            raise AssertionError(f"{name}: " + (f"expected directory-source shape, got {entry}"))
        expected_path = str(os.path.realpath(repo_a))
        if entry.get("source", {}).get("path") != expected_path:
            raise AssertionError(f"{name}: " + (f"expected path={expected_path}, got {entry}"))
        if len(extra) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 registered marketplace (repo-b manifestless), got {extra}"))

        if not os.path.isfile(known_marketplaces_path):
            raise AssertionError(f"{name}: " + ("expected known_marketplaces.json to be written"))
        known = _read_json(known_marketplaces_path)
        if "marketA" not in known:
            raise AssertionError(f"{name}: " + (f"expected known_marketplaces.json['marketA'], got {known}"))
        known_entry = known["marketA"]
        if known_entry.get("source", {}).get("source") != "directory":
            raise AssertionError(f"{name}: " + (f"expected directory-source shape, got {known_entry}"))
        if known_entry.get("installLocation") != expected_path:
            raise AssertionError(f"{name}: " + (f"expected installLocation={expected_path}, got {known_entry}"))
        if not known_entry.get("lastUpdated"):
            raise AssertionError(f"{name}: " + (f"expected a non-empty lastUpdated timestamp, got {known_entry}"))
        if len(known) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 known_marketplaces entry, got {known}"))


def test_ac9_idempotent_no_duplicates() -> None:
    name = "ac9_idempotent_no_duplicates"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        known_marketplaces_path = os.path.join(tmp, "plugins", "known_marketplaces.json")
        args = _registration_args(settings_path, committed_path, registry_dir, known_marketplaces_path)

        r1 = _run_cli(args)
        if r1.returncode != 0:
            raise AssertionError(f"{name}: " + (f"first run exited {r1.returncode}, stderr={r1.stderr!r}"))

        raw_settings_after_first = open(settings_path, encoding="utf-8").read()
        raw_known_after_first = open(known_marketplaces_path, encoding="utf-8").read()

        r2 = _run_cli(args)
        if r2.returncode != 0:
            raise AssertionError(f"{name}: " + (f"second run exited {r2.returncode}, stderr={r2.stderr!r}"))
        if "nothing to seed" not in r2.stdout:
            raise AssertionError(f"{name}: " + (f"expected 'nothing to seed' on idempotent re-run, got stdout={r2.stdout!r}"))

        raw_settings_after_second = open(settings_path, encoding="utf-8").read()
        raw_known_after_second = open(known_marketplaces_path, encoding="utf-8").read()
        if raw_settings_after_first != raw_settings_after_second:
            raise AssertionError(f"{name}: " + ("re-run mutated settings.local.json — registration not idempotent"))
        if raw_known_after_first != raw_known_after_second:
            raise AssertionError(f"{name}: " + ("re-run mutated known_marketplaces.json — registration not idempotent"))


def test_ac9_existing_entry_not_clobbered() -> None:
    name = "ac9_existing_entry_not_clobbered"
    with tempfile.TemporaryDirectory() as tmp:
        registry_dir = os.path.join(tmp, "registry")
        repo_a = os.path.join(tmp, "repo-a")
        os.makedirs(repo_a)
        _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
        _write_registry(registry_dir, {"repo_a": repo_a})

        settings_path = os.path.join(tmp, "settings.local.json")
        committed_path = os.path.join(tmp, "settings.json")
        known_marketplaces_path = os.path.join(tmp, "plugins", "known_marketplaces.json")

        # Pre-existing extraKnownMarketplaces entry with a deliberately
        # different shape (e.g. a hand-authored git-source marketplace) —
        # merge-never-clobber must leave it exactly as-is.
        existing_extra_entry = {"source": {"source": "git", "url": "https://example.invalid/mkt.git"}}
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump({"extraKnownMarketplaces": {"marketA": existing_extra_entry}}, f)

        os.makedirs(os.path.dirname(known_marketplaces_path), exist_ok=True)
        existing_known_entry = {
            "source": {"source": "git", "url": "https://example.invalid/mkt.git"},
            "installLocation": "/some/hand-authored/path",
            "lastUpdated": "2020-01-01T00:00:00.000Z",
        }
        with open(known_marketplaces_path, "w", encoding="utf-8") as f:
            json.dump({"marketA": existing_known_entry}, f)

        result = _run_cli(
            _registration_args(settings_path, committed_path, registry_dir, known_marketplaces_path)
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"exited {result.returncode}, stderr={result.stderr!r}"))

        data = _read_json(settings_path)
        extra = data.get("extraKnownMarketplaces", {})
        if extra.get("marketA") != existing_extra_entry:
            raise AssertionError(f"{name}: " + (f"existing extraKnownMarketplaces['marketA'] was clobbered: {extra}"))

        known = _read_json(known_marketplaces_path)
        if known.get("marketA") != existing_known_entry:
            raise AssertionError(f"{name}: " + (f"existing known_marketplaces.json['marketA'] was clobbered: {known}"))


# ---------------------------------------------------------------------------
# WRITE_SURFACE declaration
# ---------------------------------------------------------------------------
#
# Spec backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
# chunk C3b. This writer's surface (which `<plugin>@<marketplace>` /
# `<marketplace>` entries get seeded) depends entirely on what
# `_read_repos_registry` + `_enumerate_present_plugin_keys` find checked out
# on the machine running install. These tests assert the declaration stays
# SHAPED and machine-independent — never that it matches any particular
# machine's actual registry/marketplace state.

import importlib.util as _importlib_util  # noqa: E402


def _load_writer_module():
    """Load seed-marketplace-enabledplugins.py by path.

    The writer's own filename is hyphenated and therefore not importable via
    a plain `import` statement — spec_from_file_location is the only way to
    reach its module-level `WRITE_SURFACE` in-process (this test file is the
    ONLY consumer that needs the live object; every other test above drives
    the writer as a subprocess CLI).
    """
    spec = _importlib_util.spec_from_file_location(
        "seed_marketplace_enabledplugins", _script_path()
    )
    module = _importlib_util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_writer = _load_writer_module()

from coordinator_core.install.write_surface import (  # noqa: E402
    ShapedClause,
    validate,
)


def test_write_surface_declaration_is_valid() -> None:
    errors = validate(_writer.WRITE_SURFACE)
    if errors:
        raise AssertionError(f"write_surface_declaration_is_valid: unexpected errors {errors}")


def test_write_surface_names_the_writer_and_module() -> None:
    if _writer.WRITE_SURFACE.writer_id != "seed-marketplace-enabledplugins":
        raise AssertionError(
            f"write_surface_names_the_writer_and_module: writer_id={_writer.WRITE_SURFACE.writer_id!r}"
        )
    if not _writer.WRITE_SURFACE.source_module:
        raise AssertionError("write_surface_names_the_writer_and_module: empty source_module")


def test_write_surface_has_three_distinct_shaped_clauses() -> None:
    name = "write_surface_has_three_distinct_shaped_clauses"
    clauses = _writer.WRITE_SURFACE.clauses
    if len(clauses) != 3:
        raise AssertionError(f"{name}: expected 3 clauses, got {len(clauses)}")
    for clause in clauses:
        if not isinstance(clause, ShapedClause):
            raise AssertionError(f"{name}: expected every clause to be a ShapedClause, got {type(clause)!r}")
        if not hasattr(clause, "discovered_by") or not clause.discovered_by:
            raise AssertionError(f"{name}: clause missing discovered_by")
    # (path, key) pairs must be pairwise distinct -- two live under the same
    # file with different keys, the third lives in a different file.
    pairs = {(c.entry_template.path, c.entry_template.key) for c in clauses}
    if len(pairs) != 3:
        raise AssertionError(f"{name}: expected 3 distinct (path, key) pairs, got {pairs}")


def test_write_surface_settings_local_clauses_share_file_distinct_keys() -> None:
    name = "write_surface_settings_local_clauses_share_file_distinct_keys"
    clauses = _writer.WRITE_SURFACE.clauses
    settings_local_clauses = [
        c for c in clauses if c.entry_template.path == "settings.local.json"
    ]
    if len(settings_local_clauses) != 2:
        raise AssertionError(f"{name}: expected 2 settings.local.json clauses, got {len(settings_local_clauses)}")
    keys = {c.entry_template.key for c in settings_local_clauses}
    if not any(k.startswith("enabledPlugins.") for k in keys):
        raise AssertionError(f"{name}: no enabledPlugins.* key found in {keys}")
    if not any(k.startswith("extraKnownMarketplaces.") for k in keys):
        raise AssertionError(f"{name}: no extraKnownMarketplaces.* key found in {keys}")


def test_write_surface_known_marketplaces_clause_is_a_separate_file() -> None:
    name = "write_surface_known_marketplaces_clause_is_a_separate_file"
    clauses = _writer.WRITE_SURFACE.clauses
    known_marketplaces_clauses = [
        c for c in clauses if c.entry_template.path == "known_marketplaces.json"
    ]
    if len(known_marketplaces_clauses) != 1:
        raise AssertionError(f"{name}: expected 1 known_marketplaces.json clause, got {len(known_marketplaces_clauses)}")


def test_write_surface_entries_carry_placeholders_not_resolved_values() -> None:
    name = "write_surface_entries_carry_placeholders_not_resolved_values"
    for clause in _writer.WRITE_SURFACE.clauses:
        key = clause.entry_template.key
        if "<" not in key or ">" not in key:
            raise AssertionError(
                f"{name}: entry_template.key {key!r} is not shaped -- looks like a"
                " resolved value, not a machine-independent template"
            )


def test_write_surface_is_independent_of_this_machines_actual_registry() -> None:
    name = "write_surface_is_independent_of_this_machines_actual_registry"
    before = _writer.WRITE_SURFACE

    tmp = tempfile.mkdtemp()
    registry_dir = os.path.join(tmp, "registry")

    # State A: nothing discovered.
    _write_registry(registry_dir, {})
    settings_path = os.path.join(tmp, "a", "settings.local.json")
    committed_path = os.path.join(tmp, "a", "settings.json")
    result_a = _run_cli(_base_args(settings_path, committed_path, registry_dir, extra=["--check-only"]))
    if result_a.returncode != 0:
        raise AssertionError(f"{name}: state A run failed: {result_a.stderr!r}")

    # State B: one present, manifest-bearing sibling.
    repo_a = os.path.join(tmp, "repo-a")
    os.makedirs(repo_a)
    _write_manifest(repo_a, [{"name": "pluginA"}], "marketA")
    _write_registry(registry_dir, {"repo_a": repo_a})
    result_b = _run_cli(_base_args(settings_path, committed_path, registry_dir, extra=["--check-only"]))
    if result_b.returncode != 0:
        raise AssertionError(f"{name}: state B run failed: {result_b.stderr!r}")

    if _writer.WRITE_SURFACE is not before:
        raise AssertionError(f"{name}: WRITE_SURFACE object identity changed across machine states")
    if len(_writer.WRITE_SURFACE.clauses) != 3:
        raise AssertionError(f"{name}: WRITE_SURFACE clause count changed across machine states")


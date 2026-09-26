"""Regression test for dbc-oduffy/claude-klabauter#38.

`coordinator_core.ops.learn_lessons_roots.resolve_roots()` previously probed
ONLY the legacy `<CLAUDE_HOME>/.claude/bin/machine-local` location for the
`machine-local` CLI. `~/.claude/bin`'s 2026-07-28 retirement (see
`coordinator_core.bare_forwarder`'s own negative-spec) left that single-rung
probe permanently unable to find a settings-home-installed CLI -- on any box
whose `machine-local` lives only at `<settings-home>/bin/machine-local`, the
legacy-only probe fails, `_registry_roots()` is skipped entirely, and
`resolve_roots()` silently reports ONLY `$CLAUDE_HOME` as if zero peers were
ever registered -- exactly the symptom the issue reports (`checked: []`,
every registered peer invisible).

This test exercises REAL registry resolution end-to-end: a fixture
`machine-local` CLI backed by a constructed `dump --prefix repos --format
json` response, resolved via `resolve_roots()`'s own settings-home lookup
(`coordinator_core._settings_home.settings_home()`) -- never a monkeypatch of
`resolve_roots` itself, which is the exact seam whose substitution let the
half-landed fix upstream go unnoticed (see `lessons-outbox-drain.py`'s
`_resolve_roots()` docstring for that incident).

Spec backlink: dbc-oduffy/claude-klabauter#38
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.learn_lessons_roots import resolve_roots
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _write_fixture_machine_local(bin_dir: Path, repos: dict) -> Path:
    python_body = f"""
import json
import sys

_REPOS = {repos!r}


def _main():
    args = sys.argv[1:]
    if args and args[0] == "dump":
        prefix = None
        if "--prefix" in args:
            prefix = args[args.index("--prefix") + 1]
        out = {{
            f"repos.{{k}}": v for k, v in _REPOS.items()
            if prefix is None or f"repos.{{k}}".startswith(prefix + ".")
        }}
        print(json.dumps(out))
        return 0
    return 0


sys.exit(_main())
"""
    return write_fake_machine_local(bin_dir, python_body)


class TestSettingsHomeRegistryEnumeration:
    """The rung this issue's fix added: a `machine-local` CLI that exists
    ONLY under `<settings-home>/bin`, never under the legacy
    `<CLAUDE_HOME>/.claude/bin`."""

    def test_registered_peers_enumerated_via_settings_home_only_install(
        self, tmp_path: Path, monkeypatch
    ):
        claude_home_parent = tmp_path / "home"
        claude_home = claude_home_parent / ".claude"
        claude_home.mkdir(parents=True)

        settings_home = tmp_path / "settings-home"
        settings_home.mkdir(parents=True)

        peer_a = tmp_path / "peer-repo-a"
        peer_b = tmp_path / "peer-repo-b"
        peer_a.mkdir()
        peer_b.mkdir()

        _write_fixture_machine_local(
            settings_home / "bin",
            repos={"peer_a": str(peer_a), "peer_b": str(peer_b)},
        )

        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_parent))
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        roots = resolve_roots()

        assert str(claude_home) in roots
        assert str(peer_a) in roots, f"expected {peer_a} in {roots!r}"
        assert str(peer_b) in roots, f"expected {peer_b} in {roots!r}"
        assert roots != [str(claude_home)]

    def test_legacy_only_install_still_works(self, tmp_path: Path, monkeypatch):
        """Parity check: the pre-existing legacy rung (`<CLAUDE_HOME>/.claude/
        bin/machine-local`) still resolves when settings-home has no CLI --
        the fix adds a rung, it does not remove the old one."""
        claude_home_parent = tmp_path / "home"
        claude_home = claude_home_parent / ".claude"
        claude_home.mkdir(parents=True)

        settings_home = tmp_path / "settings-home"
        settings_home.mkdir(parents=True)

        peer = tmp_path / "legacy-peer-repo"
        peer.mkdir()

        _write_fixture_machine_local(claude_home / "bin", repos={"peer": str(peer)})

        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_parent))
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        roots = resolve_roots()

        assert str(claude_home) in roots
        assert str(peer) in roots, f"expected {peer} in {roots!r}"

    def test_settings_home_rung_preferred_over_legacy_when_both_present(
        self, tmp_path: Path, monkeypatch
    ):
        claude_home_parent = tmp_path / "home"
        claude_home = claude_home_parent / ".claude"
        claude_home.mkdir(parents=True)

        settings_home = tmp_path / "settings-home"
        settings_home.mkdir(parents=True)

        settings_home_peer = tmp_path / "settings-home-peer"
        legacy_peer = tmp_path / "legacy-peer"
        settings_home_peer.mkdir()
        legacy_peer.mkdir()

        _write_fixture_machine_local(
            settings_home / "bin", repos={"sh_peer": str(settings_home_peer)}
        )
        _write_fixture_machine_local(
            claude_home / "bin", repos={"legacy_peer": str(legacy_peer)}
        )

        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_parent))
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        roots = resolve_roots()

        assert str(settings_home_peer) in roots
        assert str(legacy_peer) not in roots

    def test_no_machine_local_anywhere_degrades_to_claude_home_only(
        self, tmp_path: Path, monkeypatch
    ):
        claude_home_parent = tmp_path / "home"
        claude_home = claude_home_parent / ".claude"
        claude_home.mkdir(parents=True)

        settings_home = tmp_path / "settings-home"
        settings_home.mkdir(parents=True)

        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_parent))
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        roots = resolve_roots()

        assert roots == [str(claude_home)]

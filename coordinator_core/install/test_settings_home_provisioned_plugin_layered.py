"""Pins that `~/.coordinator-claude-settings` is actually PROVISIONED when the
install runs in the plugin-layered (OSS) shape.

This is claude-klabauter's half of a declare/prove split agreed with DoE-claude on
2026-08-17 (`cross-repo/inbox/2026-08-17-doe-claude-em-install-entrypoint-what-
we-need-from-you.md` § 4b): DoE declares, in `AGENT.md` /
`agent-install-contract.md`, what an OSS consumer's settings-home must contain
after install; claude-klabauter owns a test proving the plugin-layered path provisions it.
The split is on TREE OWNERSHIP, not on who noticed — DoE's own version of this
cited claude-klabauter `file:line` anchors they hold no gate over, and one had already
drifted (`install_health_run.py` moved to `coordinator_core/ops/`) in a plan
committed the same day. Behaviour is provable here and survives our refactors;
a paragraph in their tree citing our line numbers does not.

NEGATIVE SPEC — why this drives the writer instead of reading a declaration.
`substrate.WRITE_SURFACE` already DECLARES `<settings-home>/bin/` and
`<settings-home>/machine-local/` clauses, and asserting against it would be
cheaper and hermetic. It would also be the exact vacuous pass this repo just
finished closing one field over: `_check_point2_declared_paths` certified that
an entry point was *declared* while the thing declared had left the repo
entirely. A test that reads the declaration cannot fail when the writer stops
writing, which is the only failure worth catching here. So this drives
`_install_bin_resolvers` and stats the destination.

NEGATIVE SPEC, second — this does NOT enumerate the required contents.
DoE's declaration is the content SSOT and is not written yet; pinning a list
here would mint a second settings-home contract racing theirs, which is
precisely the divergence risk the machine-first-install-surface plan's reviewer
flagged (two contracts each claiming to be the one). This pins that provisioning
HAPPENS, and that the families claude-klabauter itself owns arrive. When DoE's
post-condition lands, the enumeration belongs there, referenced — not copied.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core._settings_home import settings_home
from coordinator_core.install.substrate import (
    _CH_FAMILY_FILES,
    _install_bin_resolvers,
    _load_bin_templates_manifest,
    _resolve_bin_templates_manifest_root,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _plugin_layered_sources(tmp_path: Path) -> "tuple[Path, Path]":
    """Stand up the two template source dirs an install reads, in the shape a
    plugin-layered consumer presents them: `ml_bin` under a plugin root that is
    NOT a dev clone, `ch_bin` from claude-klabauter's own tree."""
    plugin_root = tmp_path / "plugins" / "cache" / "coordinator-claude" / "2.7.0"
    ml_bin = plugin_root / "templates" / "bin"
    ch_bin = tmp_path / "claude_klabauter_lib" / "claude-home"

    bin_manifest = _load_bin_templates_manifest(_resolve_bin_templates_manifest_root())
    for entry in bin_manifest.install_bin_resolvers_entries():
        _write(ml_bin / entry.name, f"ml-source-content::{entry.name}\n")
    for name, _exec_bit in _CH_FAMILY_FILES:
        _write(ch_bin / name, f"ch-source-content::{name}\n")
    return ml_bin, ch_bin


def test_settings_home_resolves_independently_of_the_plugin_layout(monkeypatch, tmp_path):
    """An OSS consumer's settings-home must not be derived from where the plugin
    happens to be cached. If it were, a plugin-layered install would provision a
    settings-home inside the plugin cache — wiped on the next plugin upgrade.

    This exercises the NO-override path deliberately: `COORDINATOR_SETTINGS_HOME`
    is unset, so `settings_home()` falls through to `_home_dir()`. The regression
    this guards against — the fallback deriving from `CLAUDE_PLUGIN_ROOT` instead
    of `CLAUDE_HOME`/`Path.home()` — can only be exercised on this branch; a test
    that sets `COORDINATOR_SETTINGS_HOME` takes the override branch and never
    reaches it (see review finding 1 on this file's dispatch).

    WHAT IS AND IS NOT LIVE HERE, so a later reader does not over-trust it:
    `_settings_home.py` does not reference `CLAUDE_PLUGIN_ROOT` at all today, so
    setting it below is INERT against current code and the plugin-cache assertion
    cannot fail as things stand. This is a FORWARD guard, not a live behaviour
    check — it fires only if someone teaches the fallback to read that variable,
    which is precisely the regression worth catching, and was confirmed to fail
    under exactly that mutation. What the first assertion pins IS live: that the
    `CLAUDE_HOME` rung is honoured on the no-override path. Second review pass
    caught the original wording implying the plugin-root derivation already
    existed.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    claude_home = tmp_path / "home"
    plugin_root = tmp_path / "plugins" / "cache" / "coordinator-claude" / "2.7.0"
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    resolved = settings_home()

    assert str(resolved).startswith(str(claude_home)), (
        f"settings-home {resolved} did not resolve under CLAUDE_HOME {claude_home}"
    )
    assert "plugins" not in resolved.parts, (
        "settings-home resolved inside the plugin cache — a plugin upgrade would "
        "delete the operator's substrate"
    )


def test_plugin_layered_install_provisions_the_settings_home_bin(monkeypatch, tmp_path):
    """The load-bearing one: run the real writer against a plugin-layered source
    layout and stat the destination.

    Guards the failure DoE's ask names — a settings-home that is empty (or never
    created) after an install that reported success. An `install` whose declared
    post-condition is a populated settings-home, verified only by the installer's
    own exit code, is a green layer that never ran.
    """
    settings_home_dir = tmp_path / ".coordinator-claude-settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    # Accepted, bounded non-hermeticity: this scans the live `coordinator/bin/`
    # tree via COORDINATOR_ENGINE_ROOT rather than a fixture. The assertions
    # below are satisfied entirely from this test's own fixtures (ml_bin/ch_bin),
    # so correctness doesn't depend on that directory's contents — only a
    # concurrent rename/delete under `coordinator/bin/` mid-scan is the narrow
    # failure mode this leaves open (see review finding 3 on this file's dispatch).
    # C14 retired CLAUDE_KLABAUTER_ROOT from Rung 1; deleted rather than left alone so an
    # inherited ancestor-process value cannot reintroduce the retired-name
    # advisory.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_REPO_ROOT))

    ml_bin, ch_bin = _plugin_layered_sources(tmp_path)
    bin_dst = settings_home() / "bin"
    bin_dst.mkdir(parents=True)

    _install_bin_resolvers(
        ml_bin, ch_bin, bin_dst,
        check_only=False,
        python3_cmd_resolved_bin="/usr/bin/python3",
    )

    installed = {p.name for p in bin_dst.iterdir()}
    assert installed, (
        f"plugin-layered install provisioned NOTHING into {bin_dst} — the "
        f"settings-home post-condition an OSS consumer depends on is unmet"
    )

    # The claude-klabauter-owned family specifically: these are sourced from our own tree
    # rather than the plugin's templates, so their arrival proves the install
    # reached across both source roots, not just the one the plugin ships.
    missing_ch = [name for name, _ in _CH_FAMILY_FILES if name not in installed]
    assert not missing_ch, (
        f"claude-klabauter-owned claude-home family absent from the provisioned settings-home: "
        f"{missing_ch} — installed: {sorted(installed)}"
    )


def test_provisioning_is_idempotent_across_a_reinstall(monkeypatch, tmp_path):
    """A second install must leave the settings-home provisioned, not strip it.

    Re-install is the common path (every upgrade), and a writer that provisions
    correctly once but empties the destination on the second pass fails the same
    post-condition later and more confusingly.
    """
    settings_home_dir = tmp_path / ".coordinator-claude-settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home_dir))
    # Accepted, bounded non-hermeticity: this scans the live `coordinator/bin/`
    # tree via COORDINATOR_ENGINE_ROOT rather than a fixture. The assertions
    # below are satisfied entirely from this test's own fixtures (ml_bin/ch_bin),
    # so correctness doesn't depend on that directory's contents — only a
    # concurrent rename/delete under `coordinator/bin/` mid-scan is the narrow
    # failure mode this leaves open (see review finding 3 on this file's dispatch).
    # C14 retired CLAUDE_KLABAUTER_ROOT from Rung 1; deleted rather than left alone so an
    # inherited ancestor-process value cannot reintroduce the retired-name
    # advisory.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(_REPO_ROOT))

    ml_bin, ch_bin = _plugin_layered_sources(tmp_path)
    bin_dst = settings_home() / "bin"
    bin_dst.mkdir(parents=True)

    kwargs = dict(check_only=False, python3_cmd_resolved_bin="/usr/bin/python3")
    _install_bin_resolvers(ml_bin, ch_bin, bin_dst, **kwargs)
    first = {p.name for p in bin_dst.iterdir()}
    first_sizes = {p.name: p.stat().st_size for p in bin_dst.iterdir()}
    assert all(size > 0 for size in first_sizes.values()), (
        f"first pass provisioned zero-byte file(s): "
        f"{[n for n, s in first_sizes.items() if s == 0]}"
    )
    _install_bin_resolvers(ml_bin, ch_bin, bin_dst, **kwargs)
    second = {p.name for p in bin_dst.iterdir()}
    second_sizes = {p.name: p.stat().st_size for p in bin_dst.iterdir()}

    assert first, "first pass provisioned nothing — see the sibling test"
    assert not (first - second), (
        f"re-install REMOVED {sorted(first - second)} from the settings-home"
    )
    shrunk = [
        name for name in first_sizes
        if name in second_sizes and second_sizes[name] < first_sizes[name]
    ]
    assert not shrunk, (
        f"re-install TRUNCATED content for {shrunk} — filenames survived but "
        f"content did not (first-pass sizes: {first_sizes}, second-pass: {second_sizes})"
    )

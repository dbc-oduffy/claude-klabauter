"""
Adversarial + golden-behavior regression tests for
``coordinator_core.install.{substrate,uninstall_legs,_shared}`` — the T4a-g3b
port.

Pins the empty-$HOME safety guard (bash bug-history: `_uninstall_require_home`)
and the settings.json hook-strip / hardware-concern-migration functional
behavior against a sandboxed HOME, per the port recipe's fixture requirements.

Port of: install-substrate.sh (DoE 6fb5fb37, 2026-07-22)
Port of: uninstall-legs.sh (DoE bd5b5a96, 2026-07-19)
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (T4a-g3b chunk).
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

import pytest

from coordinator_core.install import _shared, substrate, uninstall_legs
from coordinator_core.win_portability import is_executable


# ---------------------------------------------------------------------------
# _uninstall_require_home — empty-$HOME "rm -rf /" guard (bug-history fixture)
# ---------------------------------------------------------------------------


_HOME_VARS = ("CLAUDE_HOME", "HOME", "USERPROFILE")


def _strip_all_home_vars(monkeypatch):
    """Clear EVERY home env var, which is what "stripped env" now means.

    USERPROFILE joined the set when require_home() gained its Windows rung:
    HOME is a POSIX convention native Windows shells never set, so a
    CLAUDE_HOME/HOME-only guard refused every native-Windows invocation and made
    the whole uninstall surface unreachable there. These fixtures test the
    genuine no-home-at-all case, so they must strip the new rung too — otherwise
    they would pass on POSIX and silently stop testing anything on Windows,
    where USERPROFILE is always set.
    """
    for var in _HOME_VARS:
        monkeypatch.delenv(var, raising=False)


def test_require_home_raises_when_all_unset(monkeypatch):
    _strip_all_home_vars(monkeypatch)
    with pytest.raises(_shared.RequireHomeError):
        _shared.require_home("test-caller")


def test_require_home_raises_on_empty_string_env(monkeypatch):
    for var in _HOME_VARS:
        monkeypatch.setenv(var, "")
    with pytest.raises(_shared.RequireHomeError):
        _shared.require_home("test-caller")


def test_require_home_uses_userprofile_when_home_absent(monkeypatch):
    """Native-Windows shape: only USERPROFILE is set."""
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", os.path.join(os.sep, "Users", "someone"))

    assert _shared.require_home("test-caller") == os.path.join(os.sep, "Users", "someone")


def test_require_home_precedence_prefers_claude_home_then_home(monkeypatch):
    monkeypatch.setenv("USERPROFILE", os.path.join(os.sep, "up"))
    monkeypatch.setenv("HOME", os.path.join(os.sep, "h"))
    monkeypatch.setenv("CLAUDE_HOME", os.path.join(os.sep, "ch"))
    assert _shared.require_home("test-caller") == os.path.join(os.sep, "ch")

    monkeypatch.delenv("CLAUDE_HOME")
    assert _shared.require_home("test-caller") == os.path.join(os.sep, "h")


@pytest.mark.parametrize("var", _HOME_VARS)
def test_require_home_raises_on_relative_home(monkeypatch, var):
    """A relative home anchored every derived destructive target at the process
    cwd — the same hazard class as the empty home this guard was written for."""
    _strip_all_home_vars(monkeypatch)
    monkeypatch.setenv(var, "relative-home")

    with pytest.raises(_shared.RequireHomeError, match="non-absolute"):
        _shared.require_home("test-caller")


def test_require_home_raises_on_windows_drive_relative_home(monkeypatch):
    """`C:foo` names a path relative to the cwd ON drive C:, not an absolute
    path. Rejected on Windows as drive-relative; on POSIX as an ordinary
    relative name."""
    _strip_all_home_vars(monkeypatch)
    monkeypatch.setenv("CLAUDE_HOME", "C:foo")

    with pytest.raises(_shared.RequireHomeError, match="non-absolute"):
        _shared.require_home("test-caller")


@pytest.mark.parametrize(
    "mode",
    ["remove-substrate", "remove-shim", "strip-settings-hooks", "set-plugin-endstate"],
)
def test_every_leg_refuses_to_mutate_with_home_unset(monkeypatch, mode):
    """Adversarial fixture: no home env at all MUST refuse before any
    destructive path touches disk — pinned per recipe § 6 known bug-history."""
    _strip_all_home_vars(monkeypatch)

    if mode == "remove-substrate":
        assert uninstall_legs.uninstall_remove_substrate() is False
    elif mode == "remove-shim":
        assert uninstall_legs.uninstall_remove_shim() is False
    elif mode == "strip-settings-hooks":
        assert uninstall_legs.uninstall_strip_settings_hooks() is False
    elif mode == "set-plugin-endstate":
        assert uninstall_legs.uninstall_set_plugin_endstate() is False


# ---------------------------------------------------------------------------
# _settings_home_from — the derivation that feeds registry-clearing and rmtree
#
# Both uninstall_remove_substrate() and uninstall_set_plugin_endstate() used to
# inline `${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings`. With no HOME (the
# normal native-Windows state) that produced a drive-relative
# `/.coordinator-claude-settings`, and the `machine-local` dir derived from it,
# pointing destructive operations at the wrong tree.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("home_state", ["unset", "empty-string"])
def test_settings_home_derivation_is_absolute_without_home(monkeypatch, home_state):
    """With CLAUDE_HOME/HOME unset OR set to "", the derived settings home must
    be absolute and must not be drive- or cwd-relative.

    The empty-string case is called out separately because the old site read
    `os.environ.get("HOME", "")` — a set-but-empty HOME never reaches that
    default, so the two states had to be handled by the `or` chain, not the
    `.get` default. Both now route through require_home()'s USERPROFILE rung.
    """
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    if home_state == "unset":
        monkeypatch.delenv("CLAUDE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_HOME", "")
        monkeypatch.setenv("HOME", "")
    monkeypatch.setenv("USERPROFILE", os.path.join(os.sep, "Users", "someone"))

    resolved = uninstall_legs._settings_home_from(_shared.require_home("test-caller"))

    assert os.path.isabs(resolved) or pathlib.Path(resolved).root, resolved
    assert resolved.endswith(".coordinator-claude-settings")
    # The drive-root form the old inline derivation produced: a settings home
    # whose parent IS the root, with no home directory between them.
    parent = pathlib.Path(resolved).parent
    assert parent.name, f"settings home derived directly at a root: {resolved!r}"


def test_settings_home_derivation_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "explicit"))
    assert uninstall_legs._settings_home_from(str(tmp_path / "home")) == str(
        tmp_path / "explicit"
    )


def test_settings_home_derivation_rejects_relative_override(monkeypatch, tmp_path):
    """A relative override anchored the rmtree/registry targets at the cwd."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "relative-settings-home")
    with pytest.raises(_shared.RequireHomeError, match="non-absolute"):
        uninstall_legs._settings_home_from(str(tmp_path / "home"))


@pytest.mark.parametrize(
    "leg", ["remove-substrate", "set-plugin-endstate"]
)
def test_legs_return_false_on_relative_settings_home_override(monkeypatch, tmp_path, leg):
    """The derivation raises RequireHomeError; the legs must convert that to
    their False return contract, not let it escape."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", "relative-settings-home")

    if leg == "remove-substrate":
        assert uninstall_legs.uninstall_remove_substrate() is False
    else:
        assert uninstall_legs.uninstall_set_plugin_endstate() is False


# ---------------------------------------------------------------------------
# uninstall_strip_settings_hooks / settings_hook_identity_inverse_strip
# ---------------------------------------------------------------------------


def test_strip_settings_hooks_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert uninstall_legs.uninstall_strip_settings_hooks() is True


def test_inverse_strip_removes_generated_preserves_other(tmp_path):
    coordinator_root = tmp_path / "coordinator"
    (coordinator_root / "hooks").mkdir(parents=True)
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": f"python3 {coordinator_root}/hooks/scripts/foo.py"},
                    ],
                },
                {
                    "matcher": "Edit",
                    "hooks": [
                        {"type": "command", "command": "bash /some/operator/custom-hook.sh"},
                    ],
                },
            ]
        },
        "enabledPlugins": {"coordinator-claude": True},
    }
    # CLAUDE_HOME names the PARENT of `.claude` and every leg appends that
    # segment (`uninstall_legs._claude_dir`), so the fixture lives where a
    # real install puts it.
    settings_json = tmp_path / ".claude" / "settings.json"
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    settings_json.write_text(json.dumps(settings), encoding="utf-8")
    out_path = tmp_path / "out.json"

    _shared.settings_hook_identity_inverse_strip(str(settings_json), str(coordinator_root), str(out_path))

    result = json.loads(out_path.read_text(encoding="utf-8"))
    pretooluse = result["hooks"]["PreToolUse"]
    assert len(pretooluse) == 1
    assert pretooluse[0]["matcher"] == "Edit"
    # Non-hook top-level key preserved untouched.
    assert result["enabledPlugins"] == {"coordinator-claude": True}


def test_inverse_strip_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        _shared.settings_hook_identity_inverse_strip(
            str(tmp_path / "absent.json"), str(tmp_path), str(tmp_path / "out.json")
        )


def test_full_strip_leg_idempotent_reruns(tmp_path, monkeypatch):
    """Re-run against an already-stripped settings.json is a no-op (zero
    groups match the generated-dir prefix)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    coordinator_root = tmp_path / "coordinator"
    (coordinator_root / "hooks").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ROOT", str(coordinator_root))

    settings = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"python3 {coordinator_root}/hooks/scripts/foo.py"}]},
            ]
        },
        "enabledPlugins": {},
    }
    # CLAUDE_HOME names the PARENT of `.claude` and every leg appends that
    # segment (`uninstall_legs._claude_dir`), so the fixture lives where a
    # real install puts it.
    settings_json = tmp_path / ".claude" / "settings.json"
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    settings_json.write_text(json.dumps(settings), encoding="utf-8")

    assert uninstall_legs.uninstall_strip_settings_hooks() is True
    first = json.loads(settings_json.read_text(encoding="utf-8"))
    assert first["hooks"] == {}

    assert uninstall_legs.uninstall_strip_settings_hooks() is True
    second = json.loads(settings_json.read_text(encoding="utf-8"))
    assert second["hooks"] == {}


# ---------------------------------------------------------------------------
# uninstall_remove_substrate — mode validation
# ---------------------------------------------------------------------------


def test_remove_substrate_rejects_unrecognized_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    assert uninstall_legs.uninstall_remove_substrate("bogus-mode") is False


def test_remove_substrate_full_remove_is_idempotent_on_absent_targets(tmp_path, monkeypatch):
    """Every removal is a no-op (success) when its target is already absent.
    Isolates PATH so a real dev-machine `machine-local` install cannot leak
    into the sandbox (mirrors the bash test harness's PATH-sandboxing)."""
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / ".coordinator-claude-settings"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    assert uninstall_legs.uninstall_remove_substrate("full-remove") is True


# ---------------------------------------------------------------------------
# uninstall_remove_shim — idempotent no-op on absent shim/blocks/wrapper
# ---------------------------------------------------------------------------


def test_remove_shim_noop_on_fresh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("SHELL", raising=False)
    assert uninstall_legs.uninstall_remove_shim() is True


def test_remove_shim_refuses_hand_modified_legacy_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(
        "# --- coordinator maximalist launch ---\n"
        "claude() { echo hand-modified; }\n"
        "# --- end coordinator maximalist launch ---\n",
        encoding="utf-8",
    )
    assert uninstall_legs.uninstall_remove_shim() is False
    # Refused mutation — file unchanged.
    assert "hand-modified" in bashrc.read_text(encoding="utf-8")


def test_remove_shim_strips_matching_legacy_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    expected_repo = f"{tmp_path}/X/DoE-claude"
    expected_bin = f"{expected_repo}/coordinator/bin/claude-doe"
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text(
        "some prior line\n"
        "# --- coordinator maximalist launch ---\n"
        f'claude() {{ REPO_DOE_CLAUDE="{expected_repo}" command bash "{expected_bin}" "$@"; }}\n'
        "# --- end coordinator maximalist launch ---\n"
        "some trailing line\n",
        encoding="utf-8",
    )
    assert uninstall_legs.uninstall_remove_shim() is True
    text = bashrc.read_text(encoding="utf-8")
    assert "coordinator maximalist launch" not in text
    assert "some prior line" in text
    assert "some trailing line" in text


# ---------------------------------------------------------------------------
# substrate._register_hardware_concern — inline + multiline TOML array forms
# ---------------------------------------------------------------------------


def test_register_hardware_concern_inline_array(tmp_path):
    registry = tmp_path / "registry.toml"
    registry.write_text('concerns = ["example_retrieval_repo", "unreal"]\n', encoding="utf-8")
    substrate._register_hardware_concern(registry)
    import tomllib

    data = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert set(data["concerns"]) == {"example_retrieval_repo", "unreal", "hardware"}


def test_register_hardware_concern_multiline_array_preserves_existing(tmp_path):
    registry = tmp_path / "registry.toml"
    registry.write_text(
        "concerns = [\n"
        '  "example_retrieval_repo",\n'
        '  "unreal",\n'
        "]\n"
        "\n"
        "[cockpit]\n"
        'meta_repo_slug = ""\n',
        encoding="utf-8",
    )
    substrate._register_hardware_concern(registry)
    import tomllib

    data = tomllib.loads(registry.read_text(encoding="utf-8"))
    assert set(data["concerns"]) == {"example_retrieval_repo", "unreal", "hardware"}
    # Sibling section survived the migration untouched.
    assert data["cockpit"]["meta_repo_slug"] == ""


def test_register_hardware_concern_noop_when_already_present(tmp_path):
    registry = tmp_path / "registry.toml"
    original = 'concerns = ["hardware", "unreal"]\n'
    registry.write_text(original, encoding="utf-8")
    substrate._register_hardware_concern(registry)
    assert registry.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# _run_hardware_audit (Step 3h) — native in-process call, unconditional (no
# file-existence gate — the former `plugin_root/lib/detect-hardware.sh`
# existence check suppressed a working, file-independent native audit any
# time the DoE-side .sh happened to be absent, which is unconditionally
# post-b644d5a9's executable-surface relocation).
# ---------------------------------------------------------------------------


def test_run_hardware_audit_check_only_is_noop(capsys, monkeypatch):
    called = []
    monkeypatch.setattr(
        "coordinator_core.ops.detect_hardware.main", lambda argv: called.append(argv) or 0
    )
    substrate._run_hardware_audit(check_only=True)
    assert called == []
    assert "would: run hardware audit" in capsys.readouterr().out


def test_run_hardware_audit_calls_detect_hardware_in_process(capsys, monkeypatch):
    called = []
    monkeypatch.setattr(
        "coordinator_core.ops.detect_hardware.main", lambda argv: called.append(list(argv)) or 0
    )
    substrate._run_hardware_audit(check_only=False)
    assert called == [[]]
    assert capsys.readouterr().err == ""


def test_run_hardware_audit_nonzero_rc_warns(capsys, monkeypatch):
    monkeypatch.setattr("coordinator_core.ops.detect_hardware.main", lambda argv: 1)
    substrate._run_hardware_audit(check_only=False)
    assert "hardware audit failed" in capsys.readouterr().err


def test_run_hardware_audit_flushes_stdout_before_detect_hardware(monkeypatch):
    # C9(b): detect_hardware's `machine-local set` children write to the
    # inherited fd directly, bypassing Python's buffered stdout. Any
    # `print()` queued by this module (or a caller) before this call must be
    # flushed first, or it surfaces AFTER the child's unbuffered write under
    # redirection. Proven by call order, not content: real stdout.flush is
    # a C-level FILE flush that emits nothing capsys can see either way.
    order = []
    monkeypatch.setattr(sys.stdout, "flush", lambda: order.append("flush"))
    monkeypatch.setattr(
        "coordinator_core.ops.detect_hardware.main",
        lambda argv: order.append("detect_hardware.main") or 0,
    )
    substrate._run_hardware_audit(check_only=False)
    assert order == ["flush", "detect_hardware.main"]


def test_run_hardware_audit_never_spawns_bash(monkeypatch):
    import subprocess

    monkeypatch.setattr("coordinator_core.ops.detect_hardware.main", lambda argv: 0)

    def _blow_up(*a, **k):
        raise AssertionError("subprocess.run must not be called by _run_hardware_audit")

    monkeypatch.setattr(subprocess, "run", _blow_up)
    substrate._run_hardware_audit(check_only=False)


# ---------------------------------------------------------------------------
# substrate.run — FATAL preconditions
# ---------------------------------------------------------------------------


SEED_WIKI_FIXTURE_PAGE = "stub-seed-wiki.md"
SEED_WIKI_FIXTURE_BODY = "# stub seed wiki\n"


def _build_success_fixture(tmp_path, monkeypatch, *, omit_plugin_root_lib=True):
    """Lay down the MINIMUM dual-anchor tree substrate.run() actually needs to
    reach rc==0: a DoE-side ``plugin_root`` (templates/{bin,setup,machine-local}
    + whoami/ deliberately ABSENT — no-viable-whoami is a non-fatal warning
    branch, not a precondition) and a claude-klabauter-side root (``coordinator/lib`` +
    ``coordinator/bin``) resolved via ``CLAUDE_KLABAUTER_ROOT``, per the dual-anchor
    contract this test pins.

    Returns (plugin_root, claude_klabauter_root) for the caller to mutate/assert against.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "settings-home" / "machine-local"))
    monkeypatch.delenv("HOME", raising=False)

    plugin_root = tmp_path / "plugin-root"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    ml_templates = plugin_root / "templates" / "machine-local"
    ml_bin = plugin_root / "templates" / "bin"
    setup_src = plugin_root / "templates" / "setup"
    for d in (ml_templates, ml_bin, setup_src):
        d.mkdir(parents=True)

    for f in (
        "README.md", ".gitignore", "registry.toml.example",
        "registry.local.toml.example", "unreal.toml.example", "hardware.toml.example",
    ):
        (ml_templates / f).write_text(f"# {f}\nconcerns = []\n", encoding="utf-8")

    # machine-local: a real, trivially-successful CLI stand-in — invoked by
    # run() itself (Step C10a-2 registry-key set/get); must actually execute,
    # not merely exist, once _install_one applies the exec bit.
    (ml_bin / "machine-local").write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8"
    )
    for f in (
        "_machine_local.py", "machine-local.cmd", "python3.cmd",
        "resolve-coordinator-clone", "resolve-coordinator-clone.cmd", "coordinator-settings-home",
        "coordinator-settings-home.cmd", "coordinator-settings-home.ps1",
        "claude_machine_local.py", "claude-machine-local.sh",
        "claude-machine-local.ps1", "platform-localize.py", "platform-localize.cmd",
    ):
        (ml_bin / f).write_text(f"# stub {f}\n", encoding="utf-8")

    (setup_src / "hello.py").write_text("# stub percolated file\n", encoding="utf-8")

    claude_klabauter_root = tmp_path / "claude-klabauter-live-root"
    # C14 closed the dual-read window: CLAUDE_KLABAUTER_ROOT no longer answers Rung 1, so
    # pinning it here resolved nothing and fell through to this machine's real
    # registry ladder. Deleted rather than left set, so an inherited value cannot
    # re-emit the retired-name advisory into these tests' captured stderr.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    claude_klabauter_lib = claude_klabauter_root / "coordinator" / "lib"
    ch_bin = claude_klabauter_lib / "claude-home"
    ch_bin.mkdir(parents=True)
    for f in ("claude-home", "_claude_home.py", "claude-home.cmd"):
        (ch_bin / f).write_text(f"# stub {f}\n", encoding="utf-8")

    (claude_klabauter_lib / "setup-templates-manifest.py").write_text(
        "SETUP_TEMPLATE_FILES = ['hello.py']\n"
        "SETUP_TEMPLATE_EXEC_FILES = []\n"
        "SETUP_TEMPLATE_HOOK_FILES = []\n",
        encoding="utf-8",
    )

    # `_install_bin_resolvers` loads coordinator/lib/bin-templates-manifest.py
    # (C12) off CLAUDE_KLABAUTER_ROOT before its write loops run — this fixture's
    # `ml_bin` stub file list above is exactly the real manifest's
    # `ML_FAMILY_FILES`/`ML_EXPLICIT_FILES`/`PLATFORM_LOCALIZE_FILES` name
    # set, so copy the REAL manifest in rather than hand-authoring a second
    # copy that could drift from it.
    shutil.copyfile(
        pathlib.Path(__file__).resolve().parents[2] / "coordinator" / "lib" / "bin-templates-manifest.py",
        claude_klabauter_lib / "bin-templates-manifest.py",
    )

    agent_bin = claude_klabauter_root / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    # Bare-name CLIs (extensionless on the real tree) + their .cmd twins —
    # _derive_agent_helper_names (M1) derives the installed forwarder SET
    # from this directory listing, so the fixture must provide the bare
    # name, not just the .cmd twin, or the derivation finds nothing.
    for name in (
        "coordinator-doc-new", "coordinator-safe-commit",
        "coordinator-lesson-add", "coordinator-queue-append",
        "cross-repo-memo", "coordinator-initiative",
        "coordinator-lesson-promote", "archive-stamp-cli",
        "session-claim-cli",
    ):
        (agent_bin / name).write_text(f"# stub {name}\n", encoding="utf-8")
        (agent_bin / name).chmod(0o755)
        (agent_bin / f"{name}.cmd").write_text(f"rem stub {name}.cmd\n", encoding="utf-8")
    # mint-deliverable-id: .py + .cmd only — an ordinary .py-suffixed CLI,
    # exercising the plain stem-strip path in _derive_agent_helper_names
    # (the former pinned installed-name override has been retired).
    (agent_bin / "mint-deliverable-id.py").write_text("# stub mint-deliverable-id.py\n", encoding="utf-8")
    (agent_bin / "mint-deliverable-id.py").chmod(0o755)
    (agent_bin / "mint-deliverable-id.cmd").write_text("rem stub mint-deliverable-id.cmd\n", encoding="utf-8")

    # _resolve_claude_klabauter.py: the shared resolve-claude-klabauter-bin ladder module rm_family
    # installs into bin_dst alongside every emitted forwarder.
    resolve_claude_klabauter_lib = claude_klabauter_root / "coordinator" / "lib" / "resolve-claude-klabauter"
    resolve_claude_klabauter_lib.mkdir(parents=True)
    (resolve_claude_klabauter_lib / "_resolve_claude_klabauter.py").write_text(
        "# stub _resolve_claude_klabauter.py\n", encoding="utf-8"
    )

    # schemas/seed-wikis.json + its named page: Step C10b (`7674104db`,
    # 2026-07-30, "install: populate the settings home with the ratified seed
    # wikis") added an `_install_seed_wikis` call that fatal-errors via
    # `_load_seed_wiki_manifest` when this manifest is absent. The manifest
    # alone would reach rc==0 (a manifest entry with no page on disk is a
    # warned skip, never fatal), but a page-less fixture would pin only
    # C10b's WARNING leg and leave the copy itself — the whole point of the
    # step — uncovered, so the fixture ships the page too. The success-path
    # test asserts it lands under <settings_home>/coordinator-claude/docs/wiki/.
    schemas_dir = plugin_root / "schemas"
    schemas_dir.mkdir(parents=True)
    (schemas_dir / "seed-wikis.json").write_text(
        json.dumps({"schema_version": 1, "seed_wikis": [SEED_WIKI_FIXTURE_PAGE]}),
        encoding="utf-8",
    )
    plugin_wiki = plugin_root / "docs" / "wiki"
    plugin_wiki.mkdir(parents=True)
    (plugin_wiki / SEED_WIKI_FIXTURE_PAGE).write_text(
        SEED_WIKI_FIXTURE_BODY, encoding="utf-8",
    )

    if not omit_plugin_root_lib:
        (plugin_root / "lib").mkdir(parents=True)

    # fnm install is an optional, network/brew-dependent side effect entirely
    # orthogonal to the dual-anchor resolution this test pins — never let it
    # run for real inside a unit test.
    monkeypatch.setattr(substrate, "_fnm_step", lambda check_only: None)

    return plugin_root, claude_klabauter_root


def test_substrate_run_success_path_dual_anchor_populated_tree(tmp_path, monkeypatch):
    """Empirical gap closed: before this test, nothing drove ``run()`` to a
    success return against a populated tree — a fixture-manufactured bash
    manifest masked the fact the installer was dead post-relocation. This
    proves rc==0 against a real dual-anchor tree AND asserts the installed
    destination reflects both anchors correctly."""
    plugin_root, claude_klabauter_root = _build_success_fixture(tmp_path, monkeypatch)

    rc = substrate.run()
    assert rc == 0

    settings_home_path = tmp_path / "settings-home"
    ml_dst = settings_home_path / "machine-local"
    bin_dst = settings_home_path / "bin"

    # Step 2 tracked files landed.
    assert (ml_dst / "README.md").is_file()
    assert (ml_dst / "registry.toml").is_file()
    assert (ml_dst / "hardware.toml").is_file()

    # DoE-side (plugin_root) resolvers landed.
    assert (bin_dst / "machine-local").is_file()
    assert is_executable(bin_dst / "machine-local")

    # claude-klabauter-side (coordinator/lib/claude-home) resolvers landed.
    assert (bin_dst / "claude-home").is_file()
    # Review: code-reviewer — Finding 7, exec-bit check was present for
    # machine-local but skipped for claude-home/cross-repo-memo.
    assert is_executable(bin_dst / "claude-home")

    # claude-klabauter-side (coordinator/bin) agent-helper forwarders landed, resolving
    # against CLAUDE_KLABAUTER_ROOT — not a dead plugin_root/bin.
    assert (bin_dst / "cross-repo-memo").is_file()
    forwarder_body = (bin_dst / "cross-repo-memo").read_text(encoding="utf-8")
    assert "coordinator/bin" in forwarder_body

    # THIS FIXTURE'S CLAUDE-KLABAUTER ROOT CARRIES NO ENGINE STAMP, so it is the
    # DOORLESS install shape, and the assertions here changed with the
    # 2026-08-29 ruling (one native entrypoint per platform, and that
    # entrypoint is the door) rather than being relaxed to keep a green bar.
    #
    # No `.cmd` twin is emitted for any name any more -- the writer is
    # deleted. On Windows the consequence, stated rather than hidden, is that
    # the bare extensionless forwarder above is NOT `is_executable`: that
    # predicate answers "would CreateProcess launch this", and for an
    # extensionless path it looks for a PATHEXT-suffixed sibling, of which a
    # doorless root now has none. A doorless root is a broken install --
    # `_write_native_door_forwarder` prints the stamp-the-root remediation per
    # name -- and the correct repair is to stamp the root so the `.exe` door
    # image lands, never to re-emit an interpreter trampoline.
    #
    # The DOOR-BEARING shape (root stamped, `<name>.exe` installed, therefore
    # `is_executable` true and bare-name resolvable) is asserted in
    # `coordinator_core/install/tests/test_forwarder_routes_through_door.py`,
    # which skips when no prebuilt door image is available -- which is why
    # that coverage lives there and not in this installer-shape test.
    assert not (bin_dst / "cross-repo-memo.cmd").exists()
    if sys.platform == "win32":
        assert not is_executable(bin_dst / "cross-repo-memo")
    else:
        assert is_executable(bin_dst / "cross-repo-memo")
    assert (bin_dst / "mint-deliverable-id").is_file(), (
        "installed name is the extensionless stem-stripped form, matching "
        "every other .py-suffixed CLI, targeting mint-deliverable-id.py"
    )

    # Step C10b: the manifest-named seed wiki page was copied out of
    # plugin_root/docs/wiki/ into the settings home — the only path by which
    # a cross-repo wiki citation resolves. Content-checked, not merely
    # existence-checked: `_install_seed_wikis` overwrites unconditionally
    # (derived cache, NOT preserve-on-diff), so a stale/empty destination
    # must not read as success.
    seed_wiki_dst = (
        settings_home_path / "coordinator-claude" / "docs" / "wiki" / SEED_WIKI_FIXTURE_PAGE
    )
    assert seed_wiki_dst.is_file()
    assert seed_wiki_dst.read_text(encoding="utf-8") == SEED_WIKI_FIXTURE_BODY

    # Percolation mechanism (Step 3d) landed under the fake CLAUDE_HOME.
    assert (home_setup := tmp_path / "home" / ".claude" / "setup" / "hello.py").is_file()


def test_substrate_run_success_path_resolves_home_via_userprofile(tmp_path, monkeypatch):
    """Native-Windows condition for `run()`'s install-destination resolution
    (home-resolution-lint bare_home_or_chain fix, 2026-07-29): CLAUDE_HOME and
    HOME both absent, only USERPROFILE set (the PowerShell/cmd.exe shape).
    `run()` now resolves via `require_home()` rather than the old
    `CLAUDE_HOME or HOME` chain, which degraded to a cwd-relative install
    destination in exactly this condition."""
    _build_success_fixture(tmp_path, monkeypatch)

    home = tmp_path / "home"
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))

    rc = substrate.run()
    assert rc == 0
    assert (home / ".claude" / "setup" / "hello.py").is_file()


def test_substrate_run_manifest_resolves_off_claude_klabauter_root_not_plugin_root(tmp_path, monkeypatch):
    """Regression pin: manifest resolution must consult the claude-klabauter root, never
    ``plugin_root/lib`` — this reconstructs a competing, WRONG manifest at
    ``plugin_root/lib`` (a booby-trapped ``SETUP_TEMPLATE_FILES`` naming a file
    that does not exist anywhere on disk) and asserts run() both succeeds AND
    installs the claude-klabauter-live-root manifest's real content, never the booby-trapped
    one — a prior version of this test built an identical fixture to the
    success-path test above (``plugin_root/lib`` absent in both) and so could
    not fail for the regression it named; this seeds a distinguishable
    ``plugin_root/lib`` so choosing the wrong root would be observably wrong.
    # Review: code-reviewer — Finding 6, non-discriminating duplicate test;
    # rebuilt as a real regression pin per suggested fix option (b).
    """
    plugin_root, claude_klabauter_root = _build_success_fixture(
        tmp_path, monkeypatch, omit_plugin_root_lib=False,
    )
    assert (plugin_root / "lib").is_dir()

    (plugin_root / "lib" / "setup-templates-manifest.py").write_text(
        "SETUP_TEMPLATE_FILES = ['should-never-be-installed.py']\n"
        "SETUP_TEMPLATE_EXEC_FILES = []\n"
        "SETUP_TEMPLATE_HOOK_FILES = []\n",
        encoding="utf-8",
    )

    rc = substrate.run()
    assert rc == 0

    settings_home_path = tmp_path / "settings-home"
    home_setup_dir = tmp_path / "home" / ".claude" / "setup"

    # Claude-Klabauter-root manifest's real content landed (SETUP_TEMPLATE_FILES = ['hello.py']).
    assert (home_setup_dir / "hello.py").is_file()

    # The booby-trapped plugin_root/lib manifest was never consulted: its
    # named file was never installed anywhere under the destination tree.
    assert not (home_setup_dir / "should-never-be-installed.py").exists()
    assert not any(
        p.name == "should-never-be-installed.py"
        for p in settings_home_path.rglob("*")
        if settings_home_path.exists()
    )


# ---------------------------------------------------------------------------
# orchestrate_uninstall — CLI flag parsing, ordered-plan printing, --dry-run
# short-circuit, fail-loud leg sequencing (top-level orchestration, C7).
# ---------------------------------------------------------------------------


def test_orchestrate_help_exits_zero_and_prints_usage(capsys):
    rc = uninstall_legs.orchestrate_uninstall(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage: coordinator-uninstall.sh [OPTIONS]" in out
    assert "--keep-marketplace" in out


def test_orchestrate_unrecognized_option_exits_one(capsys):
    rc = uninstall_legs.orchestrate_uninstall(["--bogus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unrecognized option '--bogus'" in err


def test_orchestrate_dry_run_performs_zero_leg_calls(tmp_path, monkeypatch, capsys):
    """--dry-run must never invoke a leg function — patch every leg to raise
    if called, matching the bash oracle's "ZERO filesystem writes and ZERO
    registry-key clears... every mutating call is skipped entirely" guarantee."""
    def _boom(*a, **kw):
        raise AssertionError("dry-run must not call this leg")

    monkeypatch.setattr(uninstall_legs, "uninstall_strip_settings_hooks", _boom)
    monkeypatch.setattr(uninstall_legs, "uninstall_remove_shim", _boom)
    monkeypatch.setattr(uninstall_legs, "uninstall_remove_substrate", _boom)
    monkeypatch.setattr(uninstall_legs, "uninstall_set_plugin_endstate", _boom)

    rc = uninstall_legs.orchestrate_uninstall(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=full-remove purge-operator-config=0 force=0 dry-run=1" in out
    assert "ordered plan:" in out
    assert "ZERO filesystem writes and ZERO registry-key clears" in out


def test_orchestrate_keep_marketplace_dry_run_reports_mode(capsys):
    rc = uninstall_legs.orchestrate_uninstall(["--keep-marketplace", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode=revert-to-marketplace" in out
    assert "6. set plugin end-state (revert-to-marketplace)" in out


def test_orchestrate_stops_at_first_failing_leg(tmp_path, monkeypatch, capsys):
    """Fail-loud-on-ambiguity: a failing leg 1 must short-circuit — legs
    2-4 are never invoked."""
    monkeypatch.setattr(uninstall_legs, "uninstall_strip_settings_hooks", lambda: False)
    for name in ("uninstall_remove_shim", "uninstall_remove_substrate", "uninstall_set_plugin_endstate"):
        monkeypatch.setattr(
            uninstall_legs, name, lambda *a, **kw: (_ for _ in ()).throw(AssertionError(f"{name} must not run"))
        )
    rc = uninstall_legs.orchestrate_uninstall([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "FAILED at surface: settings.json generated hooks (surface #2)" in err
    assert "stopping — not continuing past a fail-loud leg" in err


def test_orchestrate_full_success_sequences_all_four_legs_and_completes(capsys):
    calls = []

    def _ok_strip():
        calls.append("strip")
        return True

    def _ok_shim():
        calls.append("shim")
        return True

    def _ok_substrate(mode, *, purge_operator_config, force, plugin_root):
        calls.append("substrate")
        return True

    def _ok_endstate(mode, *, plugin_root):
        calls.append("endstate")
        return True

    orig = {
        "strip": uninstall_legs.uninstall_strip_settings_hooks,
        "shim": uninstall_legs.uninstall_remove_shim,
        "substrate": uninstall_legs.uninstall_remove_substrate,
        "endstate": uninstall_legs.uninstall_set_plugin_endstate,
    }
    uninstall_legs.uninstall_strip_settings_hooks = _ok_strip
    uninstall_legs.uninstall_remove_shim = _ok_shim
    uninstall_legs.uninstall_remove_substrate = _ok_substrate
    uninstall_legs.uninstall_set_plugin_endstate = _ok_endstate
    try:
        rc = uninstall_legs.orchestrate_uninstall([])
    finally:
        uninstall_legs.uninstall_strip_settings_hooks = orig["strip"]
        uninstall_legs.uninstall_remove_shim = orig["shim"]
        uninstall_legs.uninstall_remove_substrate = orig["substrate"]
        uninstall_legs.uninstall_set_plugin_endstate = orig["endstate"]

    assert rc == 0
    assert calls == ["strip", "shim", "substrate", "endstate"]
    out = capsys.readouterr().out
    assert "coordinator-uninstall: complete (mode=full-remove)." in out


def test_substrate_run_fails_loud_without_plugin_root(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert substrate.run() == 1


def test_substrate_run_fails_loud_on_bad_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert substrate.run() == 1


def test_substrate_run_fails_loud_when_claude_klabauter_lib_missing(tmp_path, monkeypatch):
    """DoE-side layout (plugin_root/templates) satisfied, but claude-klabauter-side
    coordinator/lib is missing at the resolved CLAUDE_KLABAUTER_ROOT — the dual-anchor
    precondition (§ item 3) must fail loud rather than treat the DoE-side
    check alone as sufficient."""
    plugin_root = tmp_path / "plugin-root"
    (plugin_root / "templates").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    claude_klabauter_root = tmp_path / "claude-klabauter-live-root"  # no coordinator/lib under it
    claude_klabauter_root.mkdir()
    # C14 closed the dual-read window: CLAUDE_KLABAUTER_ROOT no longer answers Rung 1, so
    # pinning it here resolved nothing and fell through to this machine's real
    # registry ladder. Deleted rather than left set, so an inherited value cannot
    # re-emit the retired-name advisory into these tests' captured stderr.
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    assert substrate.run() == 1


def test_substrate_run_fails_loud_when_bin_resolvers_step_raises(tmp_path, monkeypatch, capsys):
    """The `.cmd`-twin claude-klabauter-live-root repoint inside `_install_bin_resolvers`
    (~3349) calls the real `coordinator_engine_root_with_class()` a *second*
    time (the first is run()'s own dual-anchor precondition at ~2517) and
    converts a `RuntimeError` from it into `SubstrateFatalError`. `run()`'s
    try/finally around that call must convert it to the documented int-return
    contract (printed stderr + return 1), matching every other in-function
    `SubstrateFatalError` catch site — not leak as an uncaught exception to
    `run()`'s in-process callers (e.g. `bootstrap-substrate.py`). This drives
    the genuine failure path — `coordinator_engine_root_with_class` is the
    thing that actually fails in the field, not `_install_bin_resolvers`
    itself — so a later regression that swallows the error inside
    `_install_bin_resolvers` would still be caught here."""
    plugin_root = tmp_path / "plugin-root"
    (plugin_root / "templates" / "machine-local").mkdir(parents=True)
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    (plugin_root / "templates" / "setup").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    claude_klabauter_root = tmp_path / "claude-klabauter-live-root"
    (claude_klabauter_root / "coordinator" / "lib" / "claude-home").mkdir(parents=True)

    # First call (run()'s own dual-anchor precondition at ~741) succeeds and
    # resolves claude_klabauter_root normally; the second call — inside
    # _install_bin_resolvers, for the agent-forwarder .cmd-twin source dir —
    # is made to fail, so the genuine wiring under test actually executes:
    # claude-klabauter-live-root failure -> SubstrateFatalError inside _install_bin_resolvers
    # -> caught by run()'s except clause.
    calls = {"n": 0}

    def _fake_claude_klabauter_root_with_class():
        calls["n"] += 1
        if calls["n"] == 1:
            return str(claude_klabauter_root), "live-working-tree"
        raise RuntimeError("install-substrate: repos.claude_klabauter unresolvable on second lookup")

    monkeypatch.setattr(substrate, "coordinator_engine_root_with_class", _fake_claude_klabauter_root_with_class)

    # check_only=True now filecmp-gates every family file `_install_bin_resolvers`
    # installs BEFORE it ever reaches the second `coordinator_claude_klabauter_root()` call
    # (commit 5dc11f0f, plan chunk C2, AC2/AC5) — an absent/stale destination
    # raises SubstrateFatalError right there, so the fixture must pre-seed
    # byte-identical source/dest pairs for every family file that step touches
    # (the manifest's ml_family/ch_family/ml_explicit groups, C12) to clear
    # that gate as a no-op and let execution actually reach the second call —
    # the thing this test drives. NOTE: `_install_bin_resolvers` now resolves
    # `coordinator_engine_root_with_class()` (and loads the bin-templates
    # manifest off it) BEFORE these family loops even run, so with THIS test's fake
    # resolver the second call fails before reaching them regardless — this
    # pre-seeding is retained anyway so the fixture stays correct if that
    # internal ordering ever changes back.
    bin_manifest = substrate._load_bin_templates_manifest(
        substrate._resolve_bin_templates_manifest_root()
    )
    settings_home_dir = tmp_path / "settings-home"
    bin_dst = settings_home_dir / "bin"
    bin_dst.mkdir(parents=True)
    for entry in bin_manifest.ml_family:
        f = entry.name
        (plugin_root / "templates" / "bin" / f).write_text(f"stub {f}\n", encoding="utf-8")
        (bin_dst / f).write_text(f"stub {f}\n", encoding="utf-8")
    for f, _exec_bit in substrate._CH_FAMILY_FILES:
        (claude_klabauter_root / "coordinator" / "lib" / "claude-home" / f).write_text(f"stub {f}\n", encoding="utf-8")
        (bin_dst / f).write_text(f"stub {f}\n", encoding="utf-8")
    for entry in bin_manifest.ml_explicit:
        f = entry.name
        (plugin_root / "templates" / "bin" / f).write_text(f"stub {f}\n", encoding="utf-8")
        (bin_dst / f).write_text(f"stub {f}\n", encoding="utf-8")

    # The tracked machine-local seed step's check_only branch asserts these
    # exist in ml_dst, so an unseeded fixture now fails there before reaching
    # the second coordinator_claude_klabauter_root() call this test actually drives.
    ml_dst = settings_home_dir / "machine-local"
    ml_dst.mkdir(parents=True, exist_ok=True)
    for f in ("README.md", ".gitignore", "registry.toml.example", "registry.local.toml.example"):
        (plugin_root / "templates" / "machine-local" / f).write_text(f"stub {f}\n", encoding="utf-8")
        (ml_dst / f).write_text(f"stub {f}\n", encoding="utf-8")

    monkeypatch.setattr(substrate, "_load_setup_template_manifest", lambda root: (["f"], [], []))
    monkeypatch.setattr(substrate, "migrate_substrate_to_settings_home", lambda *a, **k: 0)
    monkeypatch.setattr(substrate, "settings_home", lambda: settings_home_dir)

    assert substrate.run(check_only=True) == 1
    assert calls["n"] == 2, "sanity: fixture must exercise both coordinator_engine_root_with_class call sites"
    assert "unresolvable on second lookup" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# resolve_coordinator_root — fail-loud, never silently strip-zero-and-exit-0
# ---------------------------------------------------------------------------


def test_resolve_coordinator_root_raises_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    # DR-071: resolve_coordinator_root now reads the registry rung via direct
    # tomllib (machine_resolver.registry_get) before the machine-local CLI
    # fallback — isolate both its overrides so this "nothing resolvable" test
    # can't accidentally see a real ambient settings-home registry.
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    with pytest.raises(RuntimeError):
        _shared.resolve_coordinator_root()


def test_resolve_coordinator_root_env_override(tmp_path):
    root = tmp_path / "coord"
    root.mkdir()
    assert _shared.resolve_coordinator_root(str(root)) == str(root)


def test_resolve_coordinator_root_doe_root_pointer_rung_uses_userprofile(tmp_path, monkeypatch):
    """Native-Windows condition for the `.doe-root` pointer-file rung
    (home-resolution-lint bare_home_or_chain fix, 2026-07-29): HOME absent,
    only USERPROFILE set. The rung must resolve via `require_home()` rather
    than degrading to a cwd-relative pointer path when CLAUDE_HOME/HOME are
    both unset."""
    monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    monkeypatch.setattr(_shared, "registry_get", lambda key: None)

    userprofile_home = tmp_path / "winhome"
    userprofile_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(userprofile_home))

    coordinator_dir = userprofile_home / "coordinator"
    coordinator_dir.mkdir()
    (userprofile_home / ".doe-root").write_text(str(userprofile_home), encoding="utf-8")

    assert _shared.resolve_coordinator_root() == str(coordinator_dir)


# ---------------------------------------------------------------------------
# _install_bin_resolvers / uninstall_remove_substrate leg #7 — the compat
# ``~/.claude/bin/`` mirror (former Step 3c-compat) is RETIRED (owns-zero-
# claude-bin retirement, Gate 6, terminal delete-last chunk).
#
# `_install_bin_resolvers` no longer accepts a `compat_bin_dst` parameter and
# no longer writes anything under ``~/.claude/bin/`` — every consumer
# (example-retrieval-repo, example-game-repo, example-retrieval-repo-ue-addon, cockpit, and claude-klabauter's own
# resolvers) was repointed to try settings-home FIRST, with ``~/.claude/bin``
# only as a fallback, before this producer was deleted; on a fresh install
# after this chunk that fallback path is simply never triggered.
#
# leg #7 in uninstall_remove_substrate is retained as LEGACY CLEANUP ONLY —
# it still individually removes the same named artifacts (if present) so a
# full-remove on a machine installed BEFORE this retirement still sweeps its
# old compat-mirror leftovers. This test locks that legacy-cleanup behavior
# (pre-existing files get removed) and separately locks that a fresh
# `_install_bin_resolvers` run writes nothing new into that directory.
# ---------------------------------------------------------------------------


def test_install_bin_resolvers_no_longer_writes_compat_mirror(tmp_path, monkeypatch):
    """`_install_bin_resolvers` takes no `compat_bin_dst` param post-retirement
    and must never create or write into a ``~/.claude/bin/``-shaped directory
    — the Step 3c-compat block that used to do so was deleted outright."""
    would_be_compat_dir = tmp_path / "claude_home" / ".claude" / "bin"
    bin_dst = tmp_path / "bin_dst"
    bin_dst.mkdir()

    # check_only=True now filecmp-gates against real destinations (commit
    # 5dc11f0f, AC2/AC5), so it's no longer a fixture shortcut that lets this
    # test skip standing up a real template tree — run for real (check_only=
    # False) against tmp source dirs instead. The behavior under test (no
    # write ever lands under a ~/.claude/bin/-shaped dir) is orthogonal to
    # check_only either way.
    ml_bin = tmp_path / "ml_bin"
    ch_bin = tmp_path / "ch_bin"
    ml_bin.mkdir()
    ch_bin.mkdir()
    bin_manifest = substrate._load_bin_templates_manifest(
        substrate._resolve_bin_templates_manifest_root()
    )
    for entry in bin_manifest.install_bin_resolvers_entries():
        (ml_bin / entry.name).write_text(f"stub {entry.name}\n", encoding="utf-8")
    for f, _exec_bit in substrate._CH_FAMILY_FILES:
        (ch_bin / f).write_text(f"stub {f}\n", encoding="utf-8")

    fake_claude_klabauter_root = tmp_path / "fake_claude_klabauter_root"
    (fake_claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)  # empty -> no agent-helper forwarders
    resolve_claude_klabauter_lib = fake_claude_klabauter_root / "coordinator" / "lib" / "resolve-claude-klabauter"
    resolve_claude_klabauter_lib.mkdir(parents=True)
    (resolve_claude_klabauter_lib / "_resolve_claude_klabauter.py").write_text("stub\n", encoding="utf-8")
    # `_install_bin_resolvers` now loads the bin-templates manifest (C12) off
    # the resolved claude-klabauter root BEFORE anything else — this fixture's
    # `coordinator_engine_root_with_class` is faked to point at
    # `fake_claude_klabauter_root`, so a real manifest file must exist there too, or
    # the load fails loud before this test's actual assertion (no
    # compat-mirror write) is ever reached. Copy the real manifest verbatim
    # rather than hand-authoring a second copy that could drift from it.
    real_manifest = (
        substrate._resolve_bin_templates_manifest_root()
        / "coordinator" / "lib" / "bin-templates-manifest.py"
    )
    fake_lib_dir = fake_claude_klabauter_root / "coordinator" / "lib"
    fake_lib_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(real_manifest, fake_lib_dir / "bin-templates-manifest.py")

    monkeypatch.setattr(
        substrate, "coordinator_engine_root_with_class",
        lambda: (str(fake_claude_klabauter_root), "live-working-tree"),
    )

    substrate._install_bin_resolvers(
        ml_bin,
        ch_bin,
        bin_dst,
        False,  # check_only=False — real run against real tmp source dirs
        python3_cmd_resolved_bin="python3",
    )

    assert not would_be_compat_dir.exists(), (
        "compat mirror directory must never be created — the Step 3c-compat "
        "block that used to write here was deleted"
    )


def test_uninstall_leg7_removes_legacy_compat_mirror_artifacts(tmp_path, monkeypatch):
    """Machines installed BEFORE the retirement still have a populated
    ``~/.claude/bin/`` compat mirror on disk. full-remove uninstall must
    still sweep those pre-existing artifacts by name, even though nothing
    installs new ones any more.

    Review: code-reviewer — Finding 3, 2026-07-24-codereview-sliceowns-zero-claude-klabauter
    sidecar (option (a)): ``coordinator_engine_root_with_class`` is pointed at a
    REAL fake claude-klabauter root with a nonempty ``coordinator/bin/`` dir (one on-disk CLI file),
    so ``_derive_agent_helper_names`` genuinely returns a nonempty
    ``derived_names`` set and the agent-helper-forwarder half of leg #7's sweep
    (``agent_helper_bin_names``, not just the static ``legacy_names`` tuple) is
    actually exercised — the prior version monkeypatched the claude-klabauter root to a
    nonexistent path, so that sweep sub-leg silently no-op'd (``derived_names``
    was always ``()`` via the ``RuntimeError`` fallback) and was never asserted.
    """
    compat_bin_dst = tmp_path / "claude_home" / ".claude" / "bin"
    compat_bin_dst.mkdir(parents=True)

    legacy_names = (
        "machine-local", "_machine_local.py", "machine-local.cmd", "python3.cmd",
        "claude-home", "_claude_home.py", "claude-home.cmd",
        "resolve-coordinator-clone", "coordinator-settings-home",
        "coordinator-settings-home.cmd", "coordinator-settings-home.ps1",
        "claude_machine_local.py", "claude-machine-local.sh", "claude-machine-local.ps1",
        "resolve-coordinator-clone.cmd", "_resolve_claude_klabauter.py",
    )
    for name in legacy_names:
        (compat_bin_dst / name).write_text("stub", encoding="utf-8")

    # A REAL fake claude-klabauter root with a nonempty coordinator/bin/ dir, so
    # _derive_agent_helper_names(agent_bin) genuinely derives a nonempty
    # name set instead of hitting the RuntimeError fallback (derived_names
    # == ()). "fake-agent-helper" is the derived installed name; its
    # generated ".cmd" twin is swept alongside it (agent_helper_bin_names =
    # derived_names + the .cmd forms — see uninstall_legs.py leg #7).
    fake_claude_klabauter_root = tmp_path / "fake_claude_klabauter_root"
    agent_bin = fake_claude_klabauter_root / "coordinator" / "bin"
    agent_bin.mkdir(parents=True)
    (agent_bin / "fake-agent-helper.py").write_text("stub", encoding="utf-8")

    derived_forwarder_names = uninstall_legs._derive_agent_helper_names(agent_bin)
    assert derived_forwarder_names, "fixture must genuinely derive a nonempty agent-helper name set"
    derived_bin_names = tuple(derived_forwarder_names) + tuple(
        uninstall_legs._agent_cmd_dest_name(name) for name in derived_forwarder_names
    )
    for name in derived_bin_names:
        (compat_bin_dst / name).write_text("stub", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude_home"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")
    monkeypatch.setattr(
        uninstall_legs, "coordinator_engine_root_with_class",
        lambda: (str(fake_claude_klabauter_root), "live-working-tree"),
    )

    assert uninstall_legs.uninstall_remove_substrate("full-remove") is True

    all_names = legacy_names + derived_bin_names
    leaked = [name for name in all_names if (compat_bin_dst / name).exists()]
    assert leaked == [], f"full-remove leg #7 leaked legacy compat bin/ artifacts: {leaked}"


# ---------------------------------------------------------------------------
# C15 parity — the two bash-spawn bridges retired in favor of in-process
# native calls: CLAUDE.local.md re-render byte-compare, and CHECK 5
# tri-file agreement. Both oracles are now themselves thin Python
# trampolines over coordinator_core.ops.render_template
# / coordinator_core.hooks.platform_localize (co-located in this repo), so
# these lock the SAME observable contract (exit shape via ok/errors, file
# side effects) the bash subprocess used to produce -- with zero bash/sh
# dependency, per the naked-Python mandate.
#
# Port of: render-template.sh (DoE 290997c7, 2026-07-22)
# Port of: platform-localize.sh (DoE 6fb5fb37, 2026-07-22)
# ---------------------------------------------------------------------------


def test_purge_operator_config_claude_local_pristine_render_matches_and_deletes(tmp_path, monkeypatch):
    """A pristine (unmodified) CLAUDE.local.md — one that byte-matches a
    fresh in-process render_template.render() of the real template with the
    caller's own operator_name/working-repos — is removed WITHOUT --force.
    This is the parity case for the old bash render-template.sh subprocess:
    same byte-compare-then-unlink shape, no external process."""
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    coordinator_root = tmp_path / "coord_root"
    (coordinator_root / "templates").mkdir(parents=True)
    template_path = coordinator_root / "templates" / "CLAUDE.local.md.tmpl"
    template_path.write_text("Hello {{PM_NAME}}!\nRepos:\n{{WORKING_REPOS}}\n", encoding="utf-8")

    # coordinator-identity.yaml and working-repos.yaml deliberately absent:
    # the leg's own identity-file leg runs (and, on a match, deletes
    # coordinator-identity.yaml) BEFORE this CLAUDE.local.md leg re-reads
    # operator_name from it — so by the time the render-template call under
    # test runs, an already-removed identity file yields operator_name="".
    # Orthogonal pre-existing sequencing, not part of this chunk's bridges;
    # omitting the file keeps the render inputs deterministic.

    rendered_text, rc, err = uninstall_legs.render_template.render(
        str(template_path),
        [("PM_NAME", ""), ("WORKING_REPOS", "")],
    )
    assert rc == 0 and err is None
    claude_local = claude_home / ".claude" / "CLAUDE.local.md"
    claude_local.parent.mkdir(parents=True, exist_ok=True)
    claude_local.write_bytes(rendered_text.encode("utf-8"))

    monkeypatch.setenv("COORDINATOR_ROOT", str(coordinator_root))

    assert uninstall_legs._uninstall_purge_operator_config(str(claude_home), False) is True
    assert not claude_local.exists()


def test_purge_operator_config_claude_local_hand_edited_refused_without_force(tmp_path, monkeypatch, capsys):
    """A hand-edited CLAUDE.local.md (does not byte-match a fresh render)
    is refused without --force — the fail-safe the old bash byte-compare
    enforced, preserved by the native in-process render."""
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    coordinator_root = tmp_path / "coord_root"
    (coordinator_root / "templates").mkdir(parents=True)
    (coordinator_root / "templates" / "CLAUDE.local.md.tmpl").write_text(
        "Hello {{PM_NAME}}!\n", encoding="utf-8"
    )

    (claude_home / ".claude").mkdir()
    (claude_home / ".claude" / "CLAUDE.local.md").write_text(
        "hand-edited content, not a render\n", encoding="utf-8"
    )

    monkeypatch.setenv("COORDINATOR_ROOT", str(coordinator_root))

    assert uninstall_legs._uninstall_purge_operator_config(str(claude_home), False) is False
    assert (claude_home / ".claude" / "CLAUDE.local.md").exists()
    err = capsys.readouterr().err
    assert "possibly hand-edited. Refusing to remove without --force" in err


def test_purge_operator_config_claude_local_force_removes_without_render(tmp_path, monkeypatch):
    """--force bypasses the render-and-compare step entirely (matches the
    bash oracle's force-short-circuit — no render call is even attempted)."""
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()
    (claude_home / ".claude").mkdir()
    (claude_home / ".claude" / "CLAUDE.local.md").write_text(
        "anything\n", encoding="utf-8"
    )

    def _boom(*a, **kw):
        raise AssertionError("render must not be called under --force")

    monkeypatch.setattr(uninstall_legs.render_template, "render", _boom)

    assert uninstall_legs._uninstall_purge_operator_config(str(claude_home), True) is True
    assert not (claude_home / ".claude" / "CLAUDE.local.md").exists()


def test_plugin_endstate_revert_to_marketplace_runs_localize_in_process_no_bash(tmp_path, monkeypatch):
    """revert-to-marketplace's CHECK 5 step calls platform_localize.main()
    in-process — this must succeed (and mutate nothing bash-shaped) even
    with PATH poisoned to a nonexistent dir, proving no subprocess/bash
    spawn remains on this leg."""
    claude_home_dir = tmp_path / "home" / ".claude"
    (claude_home_dir / "plugins" / "coordinator-claude").mkdir(parents=True)

    # CLAUDE_HOME names the PARENT of `.claude`; the resolver appends that
    # segment. Set to `claude_home_dir` itself this resolved every consumer to
    # `<home>/.claude/.claude/`, and passed only because nothing on this leg
    # read a path deep enough to notice.
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home_dir.parent))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "ml-does-not-exist"))
    # Poison PATH so a `bash` subprocess.run, if one still existed on this
    # leg, would fail with FileNotFoundError — the exact Windows-break shape
    # this chunk retires.
    monkeypatch.setenv("PATH", str(tmp_path / "nonexistent-bin-dir"))

    prior_home_env = os.environ.get("HOME")
    result = uninstall_legs.uninstall_set_plugin_endstate("revert-to-marketplace")

    assert result is True
    # env must be restored exactly (no leaked CLAUDE_HOME pop / HOME rewrite)
    assert os.environ.get("CLAUDE_HOME") == str(claude_home_dir.parent)
    assert os.environ.get("HOME") == prior_home_env
    assert (claude_home_dir / "settings.local.json").is_file()


# ---------------------------------------------------------------------------
# C5 — settings-home/bin is the CANONICAL home (DR-071/DR-072) for the
# legacy-name sweep, not merely ~/.claude/bin (the retired compat mirror).
# Pins the directory-bug fix: leg #7's coord_bin_names/agent_helper_bin_names
# loop must itself remove named artifacts from settings-home/bin, not rely
# solely on leg #8's wholesale `_rmtree_target(sh_path / "bin")` to clean it
# up incidentally.
# ---------------------------------------------------------------------------


def test_uninstall_leg7_sweeps_legacy_names_from_settings_home_bin_directly(tmp_path, monkeypatch):
    """Isolated from leg #8's wholesale rmtree by monkeypatching
    `_rmtree_target` to skip the settings-home bin/ subtree specifically —
    so only the per-name sweep under test can account for the legacy
    artifact's removal. Reproduces the real live shape
    (`<settings-home>/bin/platform-localize.sh`, a pre-2026-07-22-rename
    orphan)."""
    settings_home = tmp_path / "settings-home"
    settings_home_bin = settings_home / "bin"
    settings_home_bin.mkdir(parents=True)
    (settings_home_bin / "platform-localize.sh").write_text("stub", encoding="utf-8")
    (settings_home_bin / "coordinator-settings-home").write_text("stub", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude_home"))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")

    orig_rmtree_target = uninstall_legs._rmtree_target

    def _rmtree_target_skip_settings_home_bin(target, label, errors):
        if target == settings_home_bin:
            return
        return orig_rmtree_target(target, label, errors)

    monkeypatch.setattr(uninstall_legs, "_rmtree_target", _rmtree_target_skip_settings_home_bin)

    assert uninstall_legs.uninstall_remove_substrate("full-remove") is True

    assert not (settings_home_bin / "platform-localize.sh").exists(), (
        "leg #7's per-name sweep must itself remove the legacy artifact "
        "from settings-home/bin — not rely solely on leg #8's directory rmtree"
    )
    assert not (settings_home_bin / "coordinator-settings-home").exists()


def test_uninstall_leg7_still_removes_from_retired_claude_bin_mirror_too(tmp_path, monkeypatch):
    """Regression guard: fixing the settings-home/bin directory bug must not
    regress the pre-existing ~/.claude/bin compat-mirror legacy sweep — both
    directories are covered by the same per-name loop now."""
    claude_home = tmp_path / "claude_home"
    claude_bin = claude_home / ".claude" / "bin"
    claude_bin.mkdir(parents=True)
    (claude_bin / "platform-localize.sh").write_text("stub", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir")

    assert uninstall_legs.uninstall_remove_substrate("full-remove") is True

    assert not (claude_bin / "platform-localize.sh").exists()


def test_plugin_endstate_revert_to_marketplace_reports_localize_failure(tmp_path, monkeypatch):
    """A non-zero platform_localize.main() rc surfaces as a leg error
    (function still returns False overall) — the parity case for the old
    bash subprocess's non-zero-returncode branch."""
    claude_home_dir = tmp_path / "home" / ".claude"
    (claude_home_dir / "plugins" / "coordinator-claude").mkdir(parents=True)

    # CLAUDE_HOME names the PARENT of `.claude`; the resolver appends that
    # segment. Set to `claude_home_dir` itself this resolved every consumer to
    # `<home>/.claude/.claude/`, and passed only because nothing on this leg
    # read a path deep enough to notice.
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home_dir.parent))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "ml-does-not-exist"))
    monkeypatch.setenv("PATH", str(tmp_path / "nonexistent-bin-dir"))

    monkeypatch.setattr(uninstall_legs.platform_localize, "main", lambda argv: 1)

    assert uninstall_legs.uninstall_set_plugin_endstate("revert-to-marketplace") is False


# ---------------------------------------------------------------------------
# C8 — installed percolation setup/ dir removal leg (AC9/AC10).
#
# Targets <CLAUDE_HOME>/.claude/setup, the SAME suffix
# substrate.py's setup_dest = Path(install_base) / ".claude" / "setup"
# writes into off the SAME require_home()-resolved home -- so CLAUDE_HOME is
# set here to a $HOME-equivalent root (not directly to a ".claude" dir, as
# some sibling-leg tests above do), and the fixture lays its files down
# under "<CLAUDE_HOME>/.claude/setup".
# ---------------------------------------------------------------------------


def _git_setup(*args, cwd):
    return substrate._run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _seed_setup_dir_git_repo(setup_dir, *, tracked_relpath, tracked_content):
    """A real git repo AT setup_dir with exactly one path committed (so
    `git ls-files` reports it TRACKED) -- a stand-in for a foreign repo
    (e.g. a prime-v3-shaped checkout) sitting at the installed setup/
    location."""
    setup_dir.mkdir(parents=True, exist_ok=True)
    _git_setup("init", "-q", cwd=setup_dir)
    _git_setup("config", "user.email", "test@example.invalid", cwd=setup_dir)
    _git_setup("config", "user.name", "Test", cwd=setup_dir)
    tracked_path = setup_dir / tracked_relpath
    tracked_path.parent.mkdir(parents=True, exist_ok=True)
    tracked_path.write_text(tracked_content, encoding="utf-8")
    _git_setup("add", "-A", cwd=setup_dir)
    _git_setup("commit", "-q", "-m", "seed", cwd=setup_dir)


def _isolate_path_to_git_only(monkeypatch):
    """Poison PATH down to ONLY the directory containing the real `git`
    binary -- keeps the tracked-set probe usable (this test's whole point)
    while still isolating from a dev machine's real `machine-local` CLI, the
    same PATH-sandboxing intent every sibling `uninstall_remove_substrate`
    test above applies via a fully-poisoned PATH."""
    git_bin = shutil.which("git")
    assert git_bin, "test requires a real git binary on PATH"
    monkeypatch.setenv("PATH", os.path.dirname(git_bin))


@pytest.mark.parametrize("mode", ["full-remove", "revert-to-marketplace"])
def test_uninstall_setup_dir_leg_through_remove_substrate_both_modes(tmp_path, monkeypatch, mode):
    """Exercised THROUGH uninstall_remove_substrate itself (not only a direct
    unit call), in BOTH mode values -- AC10's anti-vacuity bar. Mode-specific
    expectation: a stale installed setup/ affects root resolution (rung 3)
    identically regardless of full-remove vs revert-to-marketplace, so this
    leg's delete/report contract is IDENTICAL in both modes (it is not
    mode-gated) -- asserted per-mode below rather than assumed from one run."""
    claude_home = tmp_path / "home"
    setup_dir = claude_home / ".claude" / "setup"

    _seed_setup_dir_git_repo(
        setup_dir, tracked_relpath="tracked_file.txt", tracked_content="foreign-tracked content\n",
    )
    # A synthetic UNTRACKED residual this leg must actually DELETE.
    untracked_file = setup_dir / "untracked_file.txt"
    untracked_file.write_text("untracked residual\n", encoding="utf-8")
    # The always-reported orphan marker -- untracked (no installer writes it)
    # but must survive regardless.
    portable_marker = setup_dir / "publish-targets.portable"
    portable_marker.write_text("9,cols,here\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    _isolate_path_to_git_only(monkeypatch)

    ok = uninstall_legs.uninstall_remove_substrate(mode)
    assert ok is True, f"mode={mode}: reporting a foreign-tracked/orphan residual must not itself be an error"

    # Untracked residual: actually deleted, both modes.
    assert not untracked_file.exists(), f"mode={mode}: untracked residual must be deleted"

    # Foreign-tracked residual: reported, never deleted, both modes.
    assert (setup_dir / "tracked_file.txt").is_file(), (
        f"mode={mode}: foreign-tracked residual must survive, never be deleted"
    )
    assert (setup_dir / "tracked_file.txt").read_text(encoding="utf-8") == "foreign-tracked content\n"

    # publish-targets.portable: ALWAYS reported (never deleted), both modes,
    # even though it is untracked here -- the specific-filename override.
    assert portable_marker.is_file(), f"mode={mode}: publish-targets.portable must never be deleted"


@pytest.mark.parametrize("mode", ["full-remove", "revert-to-marketplace"])
def test_uninstall_setup_dir_leg_reports_foreign_tracked_and_portable_marker(tmp_path, monkeypatch, mode, capsys):
    """Same fixture as above, asserting the REPORT side (stderr content) in
    both modes -- a leg that silently skipped instead of reporting would
    still pass the file-survival assertions above but fail this one."""
    claude_home = tmp_path / "home"
    setup_dir = claude_home / ".claude" / "setup"

    _seed_setup_dir_git_repo(
        setup_dir, tracked_relpath="tracked_file.txt", tracked_content="foreign-tracked content\n",
    )
    (setup_dir / "publish-targets.portable").write_text("9,cols,here\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    _isolate_path_to_git_only(monkeypatch)

    assert uninstall_legs.uninstall_remove_substrate(mode) is True

    err = capsys.readouterr().err
    assert "tracked_file.txt" in err and "tracked by a (foreign) git repo" in err, (
        f"mode={mode}: foreign-tracked residual must be REPORTED"
    )
    assert "publish-targets.portable" in err and "keeps winning root resolution forever" in err, (
        f"mode={mode}: publish-targets.portable must be reported with the rung-3 orphan explanation"
    )


def test_uninstall_setup_dir_leg_noop_when_setup_dir_absent(tmp_path, monkeypatch):
    """Idempotent: no <CLAUDE_HOME>/.claude/setup at all is a no-op, not an
    error -- the leg's is_dir() guard, exercised directly."""
    errors: list = []
    uninstall_legs._uninstall_remove_setup_dir(str(tmp_path / "home"), errors)
    assert errors == []


def test_uninstall_setup_dir_leg_untracked_dir_deletes_everything_but_portable(tmp_path, monkeypatch):
    """Direct unit call (in addition to the through-uninstall_remove_substrate
    tests above): a setup/ dir that is not inside any git repo at all
    (`_resolve_directory_tracked_set` -> empty frozenset, not None) deletes
    every ordinary residual, but publish-targets.portable still survives."""
    claude_home = tmp_path / "home"
    setup_dir = claude_home / ".claude" / "setup"
    setup_dir.mkdir(parents=True)
    (setup_dir / "a.txt").write_text("a\n", encoding="utf-8")
    (setup_dir / "sub").mkdir()
    (setup_dir / "sub" / "b.txt").write_text("b\n", encoding="utf-8")
    (setup_dir / "publish-targets.portable").write_text("9,cols,here\n", encoding="utf-8")

    errors: list = []
    uninstall_legs._uninstall_remove_setup_dir(str(claude_home), errors)

    assert errors == []
    assert not (setup_dir / "a.txt").exists()
    assert not (setup_dir / "sub").exists()
    assert (setup_dir / "publish-targets.portable").is_file()
    # setup/ itself survives (non-empty: the reported marker remains).
    assert setup_dir.is_dir()


# Review: code-reviewer (Finding 1) — setup-overwrite-backups/ cleanup on
# full-remove uninstall only; exercised THROUGH uninstall_remove_substrate,
# parametrized over both mode values, matching the setup/ leg test style above.
@pytest.mark.parametrize("mode", ["full-remove", "revert-to-marketplace"])
def test_uninstall_setup_overwrite_backups_leg_through_remove_substrate_both_modes(
    tmp_path, monkeypatch, mode
):
    """full-remove: the disposable setup-overwrite-backups/ directory (C6's
    pre-overwrite backups, outside the git-tracked setup/ tree) is removed
    entirely. revert-to-marketplace: it survives -- that mode is not a
    teardown, and the backups are the only recovery path for destinations
    this installer overwrote."""
    claude_home = tmp_path / "home"
    backups_dir = claude_home / ".claude" / "setup-overwrite-backups"
    backups_dir.mkdir(parents=True)
    (backups_dir / "some-file.pre-install-20260101T000000000000Z.bak").write_text(
        "old content\n", encoding="utf-8"
    )

    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-does-not-exist"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    _isolate_path_to_git_only(monkeypatch)

    ok = uninstall_legs.uninstall_remove_substrate(mode)
    assert ok is True, f"mode={mode}: leg must not itself be an error"

    if mode == "full-remove":
        assert not backups_dir.exists(), "full-remove must remove setup-overwrite-backups/ entirely"
    else:
        assert backups_dir.is_dir(), "revert-to-marketplace must leave setup-overwrite-backups/ in place"
        assert (backups_dir / "some-file.pre-install-20260101T000000000000Z.bak").is_file()


def test_uninstall_setup_overwrite_backups_leg_noop_when_absent(tmp_path):
    """Idempotent: no setup-overwrite-backups/ at all is a no-op, not an
    error -- the leg's is_dir() guard, exercised directly."""
    errors: list = []
    uninstall_legs._uninstall_remove_setup_overwrite_backups(str(tmp_path / "home"), errors)
    assert errors == []


def test_uninstall_setup_overwrite_backups_leg_reports_foreign_tracked(tmp_path, monkeypatch, capsys):
    """A setup-overwrite-backups/ directory that is (unexpectedly) itself a
    git repo with tracked content is reported and left in place, never
    deleted -- the tracked-ness check is reused rather than assuming this
    directory is always untracked."""
    claude_home = tmp_path / "home"
    backups_dir = claude_home / ".claude" / "setup-overwrite-backups"
    _seed_setup_dir_git_repo(
        backups_dir, tracked_relpath="tracked_file.txt", tracked_content="foreign-tracked content\n",
    )
    _isolate_path_to_git_only(monkeypatch)

    errors: list = []
    uninstall_legs._uninstall_remove_setup_overwrite_backups(str(claude_home), errors)

    assert errors == []
    assert backups_dir.is_dir(), "a git-tracked backups dir must survive, never be deleted"
    assert (backups_dir / "tracked_file.txt").is_file()


# ---------------------------------------------------------------------------
# Convention guard — CLAUDE_HOME is the PARENT of `.claude`
# ---------------------------------------------------------------------------


def test_legs_target_under_dot_claude_never_the_home_itself(tmp_path, monkeypatch):
    """Every destructive target these legs derive lives under `<home>/.claude/`,
    never beside it.

    This is a real regression, not a hypothetical: most call sites here once
    read `Path(claude_home) / "settings.json"` while every writer puts that file
    at `<home>/.claude/settings.json`, so the legs found nothing and reported
    success. `<home>/bin` makes the shape worse than inert — that is an
    operator's own directory on many boxes, not ours to sweep. The decoys below
    are the assertion that earns this test.
    """
    home = tmp_path / "home"
    dot_claude = home / ".claude"
    (dot_claude / "bin").mkdir(parents=True)
    (dot_claude / "bin" / "platform-localize.sh").write_text("ours", encoding="utf-8")
    (dot_claude / ".doe-root").write_text("ours", encoding="utf-8")

    # Decoys directly under the home — an operator's own files, off limits.
    (home / "bin").mkdir()
    (home / "bin" / "platform-localize.sh").write_text("theirs", encoding="utf-8")
    (home / ".doe-root").write_text("theirs", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-absent"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry-absent"))
    monkeypatch.setenv("PATH", str(tmp_path / "nonexistent-bin-dir"))

    assert uninstall_legs.uninstall_remove_substrate("full-remove") is True

    assert not (dot_claude / "bin" / "platform-localize.sh").exists()
    assert not (dot_claude / ".doe-root").exists()
    assert (home / "bin" / "platform-localize.sh").read_text(encoding="utf-8") == "theirs"
    assert (home / ".doe-root").read_text(encoding="utf-8") == "theirs"


def test_strip_settings_hooks_reads_the_installed_settings_json(tmp_path, monkeypatch):
    """The strip leg must find `<home>/.claude/settings.json` — the path
    `gen_settings_hooks.resolve_settings_out_path` actually writes — and must
    not be satisfied by a decoy beside it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    coordinator_root = tmp_path / "coordinator"
    (coordinator_root / "hooks").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_ROOT", str(coordinator_root))

    generated = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"python3 {coordinator_root}/hooks/scripts/foo.py"}]},
            ]
        },
        "enabledPlugins": {},
    }
    installed = home / ".claude" / "settings.json"
    installed.write_text(json.dumps(generated), encoding="utf-8")
    decoy = home / "settings.json"
    decoy.write_text(json.dumps(generated), encoding="utf-8")

    assert uninstall_legs.uninstall_strip_settings_hooks() is True

    assert json.loads(installed.read_text(encoding="utf-8"))["hooks"] == {}
    assert json.loads(decoy.read_text(encoding="utf-8"))["hooks"] != {}, (
        "the leg stripped a file beside .claude — the pre-fix target"
    )

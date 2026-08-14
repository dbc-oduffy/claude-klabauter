"""test_agent_helper_shim_doe_corpus_coverage.py — AC7/AC8 coverage-gate test.

Makes it durable that (a) every `<settings-home>/bin/<cli>`-shaped reference
found anywhere in the DoE-claude doctrine corpus (skills/commands/agents/
pipelines/snippets) resolves to a CLI that the real installer (`substrate.py`'s
`_install_bin_resolvers`) actually produces a forwarder for (AC8 — fail loud
on a corpus/install drift), and (b) every derived agent-helper forwarder has
a macOS/Windows-parity `.cmd` sibling in `<settings-home>/bin/` (AC7).

The former `~/.claude/bin/` compat mirror this test used to ALSO assert
parity against (Step 3c-compat) is retired as of the owns-zero-claude-bin
retirement, Gate 6 — `_install_bin_resolvers` no longer writes there at all,
so AC7 now checks the single settings-home location only.

Three install personas this gate must hold for, on macOS AND Windows, at
arbitrary directories — PM-sharpened constraint, 2026-07-23:
  1. an OSS `coordinator-claude` consumer — NO DoE-claude clone, NO claude-klabauter
     source checkout.
  2. a DoE-claude developer — this repo is the live plugin source.
  3. a claude-klabauter developer — this repo IS where this test runs from.

**AC8's primary assertion is machine-independent by construction — it is
NOT persona 1's problem to solve, because the corpus this test scans does
not exist for persona 1** (no DoE-claude clone at all), so this whole test
module correctly skips for that persona (see `_resolve_doe_root`) rather
than asserting anything. For personas 2/3, where the corpus IS resolvable,
ground truth for "what the installer would produce" is obtained by actually
RUNNING the real installer (`_install_bin_resolvers`) — but always into a
FRESH `tmp_path` scratch directory, never the real machine's settings home.
`tmp_path` is pytest's own directory under the OS temp root; nothing here
ever reads `~/.coordinator-claude-settings/bin/` as ground truth — a test
that passed only because THIS machine happens to have already run an
installer would be a local fix wearing a test costume, and would not catch
the drift this gate exists for on a fresh machine or in CI. Because it's a
fresh scratch install every run, the result is deterministic and identical
across macOS/Windows/CI — it is calling the real generator, not reimplementing
a second, driftable copy of its logic (see `_install_shims_to_scratch`).

A SEPARATE, secondary test additionally cross-checks a REAL installed
settings-home bin dir against the same generator's derived agent-helper
name set, when one is discoverable — but it SKIPS CLEANLY (never fails)
when no settings home is installed or its bin dir is empty, which is the
normal state for persona 1 and for CI. Resolution order for the real
settings-home directory mirrors the corpus's own
`${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}`
ladder: explicit `COORDINATOR_SETTINGS_HOME` env var, then `CLAUDE_HOME`-
relative default, then `$HOME`/`Path.home()`-relative default. No hardcoded
`/Users/...`, no assumption DoE-claude and claude-klabauter are siblings, no
home-relative-layout assumption baked into the PRIMARY (AC8) assertion.

AC7's parity assertion likewise validates the INSTALLER ITSELF, into the
same fresh `tmp_path` scratch directories — never the real machine's
settings home — so it too runs and passes identically on macOS and
Windows, rather than merely asserting about Windows artifacts from a Mac.

Invocation-form coverage (empirically discovered against the live DoE-claude
corpus, 2026-07-23 — see the four forms below). If this test starts silently
under-scanning after a future corpus edit introduces a new alias shape, grep
the corpus for new `="${COORDINATOR_SETTINGS_HOME...}/bin"`-style assignments
and add the new alias name to `_BIN_ALIAS_VARS`.

  1. Direct inline expansion, no intermediate variable:
       ${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/<cli>
  2. `CC_BIN="${COORDINATOR_SETTINGS_HOME...}/bin"` assignment, then `"$CC_BIN/<cli>"`.
  3. `_wsc_scc="${COORDINATOR_SETTINGS_HOME...}/bin"` assignment (workstream-complete's
     own alias name), then `"${_wsc_scc}/<cli>"`.
  4. (Considered, excluded by design) `_mkb_bin`/`resolve-claude-klabauter-bin.md`-style
     resolution hits claude-klabauter's OWN `coordinator/bin/` directly, bypassing the
     settings-home shim entirely by contract — those references need no shim
     and are correctly out of scope for this gate.
  A `~/.claude/bin/<cli>`-shaped fifth form (the compat mirror, referenced
  directly rather than via the settings-home variable) was searched for
  empirically (2026-07-23) and found NOT to occur as a live invocation site
  anywhere in the corpus outside `pickup/SKILL.md` (excluded — see below) —
  every hit is prose/doc text describing the mirror mechanism itself
  (`uninstall.md`, `install.md`, DR-072 warnings not to hardcode it). Nothing
  to scan for there today; if that changes, add a fifth regex here.

`coordinator/skills/pickup/SKILL.md` is excluded from the corpus scan — a
concurrent peer session owns that file as of 2026-07-23.

Spec backlink: M1 (claude-klabauter commit e90904a9, `_derive_agent_helper_target_map` +
  two-location forwarder install), M2 dispatch row of the same cross-repo plan
  (`DoE-claude docs/plans/2026-07-23-skills-carry-no-code-extirpation.md`).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_TESTS_DIR = _THIS.parent
_BIN_DIR = _TESTS_DIR.parent
_CLAUDE_KLABAUTER_ROOT = _BIN_DIR.parent.parent

for _p in (str(_CLAUDE_KLABAUTER_ROOT), str(_BIN_DIR / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from coordinator_core.install.substrate import (  # noqa: E402
    _derive_agent_helper_target_map,
    _agent_cmd_dest_name,
    _install_bin_resolvers,
    _resolve_baked_python_bin,
)

# `coordinator_registry` raises at IMPORT time (not just when `doe_root()` is
# called) when its manifest is unresolvable via any rung of its own
# DOE_ROOT-env / REPO_DOE_CLAUDE-env / machine-local-registry ladder — the
# realistic version of persona 3 (a claude-klabauter developer with no
# DoE-claude sibling clone and no machine-local registry entry) hits exactly
# this. A bare `import` would turn that into a collection ERROR for this
# whole module, not the clean skip the corpus-absent case deserves — so the
# import itself is wrapped, and an import-time failure short-circuits the
# whole module via `pytest.skip(..., allow_module_level=True)` rather than
# reaching `_resolve_doe_root`'s own try/except (which can only catch
# failures from calling `doe_root()`, not from importing the module that
# defines it).
try:
    import coordinator_registry  # noqa: E402
except Exception as _coordinator_registry_import_exc:  # noqa: E402
    pytest.skip(
        "coordinator_registry unimportable (DoE-claude root unresolvable at "
        f"import time): {_coordinator_registry_import_exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# DoE root resolution — skip (not fail) when unresolvable on this machine.
# `coordinator_registry.doe_root()` already implements the load-bearing
# resolution order (env var -> machine-local `repos.doe_claude` registry ->
# raise) — persona 1 (no DoE clone at all) and persona 3 running in CI
# without the sibling checkout correctly land here and skip, never fail.
# ---------------------------------------------------------------------------

def _resolve_doe_root() -> Path:
    try:
        root = coordinator_registry.doe_root()
    except Exception as exc:  # coordinator_registry._DoeUnresolvable, etc.
        pytest.skip(f"DoE-claude root unresolvable via coordinator_registry.doe_root(): {exc}")
    p = Path(root)
    if not (p / "coordinator").is_dir():
        pytest.skip(f"resolved DoE root {p} has no coordinator/ tree; skipping corpus scan")
    return p


# ---------------------------------------------------------------------------
# Corpus scan — extract every <settings-home>/bin/<cli> reference.
# ---------------------------------------------------------------------------

_DOE_CORPUS_DIRS = (
    "coordinator/skills",
    "coordinator/commands",
    "coordinator/agents",
    "coordinator/pipelines",
    "coordinator/snippets",
)

# A concurrent peer session owns this file as of 2026-07-23 — excluded from
# the scan rather than read-and-reported, per that session's own dispatch.
_EXCLUDED_RELPATHS = frozenset({"coordinator/skills/pickup/SKILL.md"})

# Settings-home bin-dir alias variable names in live use in the DoE corpus,
# discovered via `grep -rhoE '^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*="\$\{COORDINATOR_SETTINGS_HOME[^\n]*\}/bin"'`.
# Only CC_BIN and _wsc_scc resolve a `.../bin` (not `.../bin/coordinator`,
# not a bare settings-home) directly from COORDINATOR_SETTINGS_HOME today.
_BIN_ALIAS_VARS = ("CC_BIN", "_wsc_scc")

_CLI_NAME_CHARS = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"

# Form 1 — direct inline expansion ending .../bin/<cli>.
_DIRECT_INLINE_RE = re.compile(
    r"COORDINATOR_SETTINGS_HOME[^\n]*?\}/bin/(" + _CLI_NAME_CHARS + r")"
)


def _alias_ref_re(varname: str) -> "re.Pattern[str]":
    # Matches $VAR/name, ${VAR}/name, "$VAR/name", "${VAR}/name" — but NOT
    # the alias's own assignment line (VAR="...") since '=' immediately
    # follows the name there, never '/' or a closing brace/quote.
    return re.compile(re.escape(varname) + r'"?\}?/(' + _CLI_NAME_CHARS + r")")


def _collect_corpus_files(doe_root: Path) -> "list[Path]":
    files: list[Path] = []
    for d in _DOE_CORPUS_DIRS:
        base = doe_root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(doe_root).as_posix()
            if rel in _EXCLUDED_RELPATHS:
                continue
            files.append(p)
    return files


def _extract_referenced_clis(doe_root: Path) -> "dict[str, list[str]]":
    """Returns {cli_name: [relative file paths referencing it]}."""
    refs: "dict[str, list[str]]" = {}
    alias_res = [_alias_ref_re(v) for v in _BIN_ALIAS_VARS]
    for path in _collect_corpus_files(doe_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names: set = set()
        for m in _DIRECT_INLINE_RE.finditer(text):
            names.add(m.group(1))
        for rex in alias_res:
            for m in rex.finditer(text):
                names.add(m.group(1))
        if not names:
            continue
        rel = path.relative_to(doe_root).as_posix()
        for n in names:
            refs.setdefault(n, []).append(rel)
    return refs


@pytest.fixture(scope="module")
def _doe_root() -> Path:
    return _resolve_doe_root()


# ---------------------------------------------------------------------------
# AC8, primary — corpus references vs. a FRESH scratch install of the real
# installer (`_install_bin_resolvers`, via `_install_shims_to_scratch` below,
# shared with AC7). Ground truth is the full write-out — every install
# family (`ml_family`, `ch_family`, `resolve-claude-klabauter`, the derived
# agent-helper set, `platform-localize`) — not just the agent-helper subset,
# since the corpus invokes CLIs from all of those families
# (`machine-local`, `claude-machine-local.sh`, `verify-cc-root-source-guard-sync`,
# etc. are NOT agent-helper-derived names, but ARE real installed CLIs the
# corpus legitimately calls). A ground truth narrowed to
# `_derive_agent_helper_target_map` alone under-covers and produces false
# positives against those other families.
# ---------------------------------------------------------------------------

_AGENT_BIN = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin"


def test_every_referenced_cli_is_in_generator_installed_forwarder_set(_doe_root, _installed_dirs) -> None:
    """AC8: every `<settings-home>/bin/<cli>` reference in the DoE corpus
    must name a CLI the real installer actually writes a forwarder for, per
    a fresh scratch install (never the real machine's settings home — see
    module docstring). Fails loud, naming the missing CLI(s) and the
    referencing file(s), rather than a bare count — a corpus edit that
    references a CLI the installer does NOT produce (typo, retired CLI,
    `.py`/`.js` suffix mismatch against the generator's stem-dedup rule, or
    a CLI never added under `coordinator/bin/`) must break this test, not
    ship silently.
    """
    refs = _extract_referenced_clis(_doe_root)
    assert refs, (
        "corpus scan found zero <settings-home>/bin/<cli> references across "
        f"{_DOE_CORPUS_DIRS} — this almost certainly means the scan regexes "
        "drifted from the corpus's actual invocation shapes, not that the "
        "corpus stopped invoking any CLI. Investigate before trusting a "
        "green run here."
    )

    bin_dst = _installed_dirs
    expected_names = {p.name for p in bin_dst.iterdir() if p.is_file()}
    assert expected_names, (
        f"fresh scratch install into {bin_dst} produced zero files — either "
        "coordinator/bin/ is unexpectedly empty on this checkout, or the "
        "installer itself regressed. Either way this test cannot validate "
        "AC8 against an empty installed set."
    )

    missing: "dict[str, list[str]]" = {}
    for name, files in refs.items():
        if name not in expected_names:
            missing[name] = files

    assert not missing, (
        "DoE corpus references CLI name(s) a fresh installer run would NOT "
        "produce a forwarder for (i.e. these would 127 on a fresh install "
        "on ANY machine/persona): "
        + ", ".join(
            f"{name!r} (referenced in: {', '.join(files)})"
            for name, files in sorted(missing.items())
        )
    )


# ---------------------------------------------------------------------------
# AC8, secondary (optional) — real installed settings-home cross-check.
# SKIPS CLEANLY (never fails) when no settings home is installed or its bin
# dir is empty — the normal state for an OSS consumer persona and for CI.
# Resolution order mirrors the corpus's own
# ${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}
# ladder: explicit env var, then CLAUDE_HOME-relative default, then
# Path.home()-relative default (cross-platform: resolves %USERPROFILE% on
# Windows, $HOME on macOS/Linux).
# ---------------------------------------------------------------------------

def _resolve_settings_home_bin() -> Path:
    env = os.environ.get("COORDINATOR_SETTINGS_HOME", "").strip()
    if env:
        return Path(env) / "bin"
    claude_home = os.environ.get("CLAUDE_HOME", "").strip()
    base = Path(claude_home) if claude_home else Path.home()
    return base / ".coordinator-claude-settings" / "bin"


def test_real_installed_settings_home_matches_generated_set_when_present() -> None:
    """AC8 cross-check (optional, non-load-bearing): if THIS machine happens
    to have a real settings-home install, its `bin/` dir should not be stale
    against what the generator currently derives. Deliberately skips rather
    than fails when no install is present — persona 1 (OSS consumer with no
    prior install run) and CI both land here every time, and that is
    correct, not a gap: the load-bearing assertion is the machine-
    independent one above.
    """
    bin_dir = _resolve_settings_home_bin()
    if not bin_dir.is_dir():
        pytest.skip(f"no installed settings-home bin dir at {bin_dir} — nothing to cross-check")
    installed_names = {p.name for p in bin_dir.iterdir() if p.is_file()}
    if not installed_names:
        pytest.skip(f"{bin_dir} exists but is empty — nothing to cross-check")

    expected_names = set(_derive_agent_helper_target_map(_AGENT_BIN))
    stale = sorted(expected_names - installed_names)
    assert not stale, (
        f"real installed settings-home bin dir ({bin_dir}) is missing "
        f"forwarder(s) the generator currently derives: {stale} — re-run "
        "the installer to pick up the current coordinator/bin/ set."
    )


# ---------------------------------------------------------------------------
# AC7 — macOS/Windows .cmd parity for every derived agent-helper forwarder,
# in the single `<settings-home>/bin/` write location (the former
# `~/.claude/bin/` compat-mirror second location is retired — see module
# docstring). This validates the INSTALLER itself, so it must actually run
# an install — but always into pytest's own cross-platform `tmp_path`
# scratch directory, never the real machine's settings home. That makes the
# test itself run and pass identically on macOS and Windows, rather than
# merely asserting about Windows artifacts from a Mac.
# ---------------------------------------------------------------------------

def _install_shims_to_scratch(doe_root: Path, tmp_path: Path) -> Path:
    plugin_root = doe_root / "coordinator"
    ml_bin = plugin_root / "templates" / "bin"
    ch_bin = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "lib" / "claude-home"
    if not ml_bin.is_dir():
        pytest.skip(f"DoE templates/bin not found at {ml_bin}; skipping corpus scan")
    if not ch_bin.is_dir():
        pytest.skip(f"claude-klabauter coordinator/lib/claude-home not found at {ch_bin}; skipping corpus scan")

    bin_dst = tmp_path / "settings-home-bin"
    bin_dst.mkdir(parents=True)

    prior = os.environ.get("CLAUDE_KLABAUTER_ROOT")
    os.environ["CLAUDE_KLABAUTER_ROOT"] = str(_CLAUDE_KLABAUTER_ROOT)
    try:
        _install_bin_resolvers(
            ml_bin, ch_bin, bin_dst, False,
            python3_cmd_resolved_bin=_resolve_baked_python_bin(),
        )
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_KLABAUTER_ROOT", None)
        else:
            os.environ["CLAUDE_KLABAUTER_ROOT"] = prior

    return bin_dst


@pytest.fixture(scope="module")
def _installed_dirs(_doe_root, tmp_path_factory) -> Path:
    tmp_path = tmp_path_factory.mktemp("agent-helper-shim-scratch")
    return _install_shims_to_scratch(_doe_root, tmp_path)


def test_derived_agent_helper_forwarders_have_cmd_parity(_installed_dirs) -> None:
    """AC7: for every name in `_derive_agent_helper_target_map()`'s live
    output, a `.cmd` sibling must exist in `<settings-home>/bin/`. A
    forwarder installed with no `.cmd` twin is a live Windows regression.
    """
    bin_dst = _installed_dirs
    derived_names = set(_derive_agent_helper_target_map(_AGENT_BIN))

    assert derived_names, (
        f"_derive_agent_helper_target_map({_AGENT_BIN}) returned zero names — "
        "either coordinator/bin/ is unexpectedly empty on this checkout, or "
        "the derivation itself regressed. Either way this test cannot "
        "validate AC7 parity with an empty derived set."
    )

    missing_parity: "list[str]" = []
    for name in sorted(derived_names):
        cmd_source = _AGENT_BIN / (Path(name).stem + ".cmd")
        if not cmd_source.is_file():
            # No .cmd twin ships for this CLI at all — nothing to check
            # parity against (matches _install_bin_resolvers' own
            # cmd_src.is_file() gate before installing a .cmd sibling).
            continue
        dest_name = _agent_cmd_dest_name(name)
        settings_home_cmd = bin_dst / dest_name
        if not settings_home_cmd.is_file():
            missing_parity.append(f"{name!r} ({dest_name}): missing at {settings_home_cmd}")

    assert not missing_parity, (
        "AC7 macOS/Windows .cmd parity violation — "
        + "; ".join(missing_parity)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

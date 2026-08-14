"""
coordinator_core.ops.verify_templates_bin_sync — byte-identity checker/fixer
between the live install's bin helpers and their coordinator/templates/bin/
mirrors.

Purpose: enforces that a fixed set of small cross-cutting helpers
(claude_machine_local.py, claude-machine-local.sh/.ps1, _machine_local.py,
resolve-coordinator-clone) stay byte-identical between the live install copy
(settings-home/bin/, falling back to the pre-migration ~/.claude/bin/) and
the source-of-truth template copy shipped under coordinator/templates/bin/
— so `coordinator/bin/publish.py` ships an up-to-date mirror to consumer
projects, and so a live install never silently drifts from the fix DoE
shipped in the template. See docs/wiki/portable-code-substrate.md and
docs/wiki/eager-agent-calibration.md § Template mirrors.

Modes:
    verify (default) — diff each pair; print one status line per pair;
        exit 1 if ANY pair is MISMATCH / LIVE_MISSING / TMPL_MISSING.
    --fix — copy template -> live (one-way; the template is the
        source-of-truth, the live install is the derived/repaired copy,
        matching coordinator/lib/bin-templates-manifest.py's declared
        ownership and what `_install_one` already does) for any pair that
        is missing its live copy or byte-differs. Does NOT fix a
        TMPL_MISSING pair (nothing to copy FROM in that direction — the
        template is canonical and there is no template content to install)
        — that pair still contributes to a nonzero exit code even under
        --fix. Copies content only: destination file mode is left
        untouched (neither applied nor cleared) — see negative-spec.
    Both modes — if the templates ROOT itself is absent, exit 1 immediately
        with a single diagnostic before comparing any pair; --fix does NOT
        create a missing templates root (see rationale in main()).

Live bin resolution: primary is `<settings_home()>/bin` (see
coordinator_core._settings_home.settings_home — the
COORDINATOR_SETTINGS_HOME-rooted location the machine-local forwarder chain
actually resolves to and executes from). Falls back to the pre-migration
`_claude_home_bin_dir()` (`${CLAUDE_HOME:-$HOME}/.claude/bin`, Convention A)
ONLY when the settings-home bin directory does not exist as a directory at
all (`os.path.isdir` false) — a settings-home/bin that exists but is merely
missing one or more of the five PAIRS files still wins over the fallback.
This resolution happens ONCE per run, for the whole live_bin, never
re-derived per pair — a hybrid live_bin drawn from two different install
generations (some pairs read from settings-home, others from the
pre-migration home) would be a worse failure mode than either home alone.

When neither side of a pair exists yet, the pair is skipped (NOT_PRESENT,
does not affect exit code) — this graceful-no-files behaviour matches the
no-consumer pattern in other verify-*-sync ports: the verifier is authored
before the files it will eventually verify. This per-file graceful allowance
does NOT extend to an absent templates ROOT: if templates_bin itself does
not resolve to an existing directory, main() short-circuits before the
per-file loop with a single diagnostic line and exit code 1 in BOTH modes
(verify and --fix) — see the templates-root existence check below for why
this must be a hard failure rather than five per-file NOT_PRESENT/TMPL_MISSING
lines.

Port of: verify-templates-bin-sync.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-20-eager-agent-calibration.md § Chunk 1
               docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Repointed + direction-reversed per cross-repo memo:
  cross-repo/archive/2026-08-03-doe-claude-em-templates-bin-sync-gate-watches-the-pre-migration-home.md

Negative-spec:
    - CLAUDE_HOME resolution in the FALLBACK path is Convention A verbatim
      from the bash oracle: `${CLAUDE_HOME:-$HOME}/.claude` — i.e. the
      CLAUDE_HOME env var (when set) is treated as a *$HOME-substitute*,
      NOT a *.claude-substitute*; `/.claude` is unconditionally appended.
      This is the OPPOSITE convention from some other claude-klabauter ops modules
      (e.g. check_arch_audit_staleness._claude_home(), which treats an
      override as a full .claude-substitute with no suffix) — do not
      "fix" this to match that other convention. This convention now
      governs only the migration-window fallback, not the primary
      resolution (see "Live bin resolution" above) — do not conflate the
      two; the settings-home primary path has its own resolver
      (`coordinator_core._settings_home.settings_home`) with no
      CLAUDE_HOME-appends-.claude behaviour at all.
    - Template is canonical for EVERY pair, including `_machine_local.py`.
      There is no longer a live-canonical special case: the retired
      bash oracle's non-enforced live->template polarity for this one pair
      is moot now that the direction is reversed for all pairs uniformly —
      `--fix` copies template->live here exactly like every other pair.
    - `resolve-coordinator-clone` is documented as authored identical on
      both sides by convention (position-independent shim); this module
      does not verify that invariant beyond the same byte-diff check applied
      to every other pair.
    - Does not create the live bin directory unless a --fix INSTALL
      actually needs it (`mkdir -p` only fires in the LIVE_MISSING + --fix
      branch, never as a side effect of verify) — mirrors the oracle's
      conditional `mkdir -p` placement, now on the live side since the
      copy direction reversed.
    - `--fix` copies file CONTENT only — it never applies or clears an
      exec bit. Destination mode is left exactly as it was found (matches
      `_install_one`, which only ORs `0o111` in when `exec_bit` is
      truthy and never clears a bit; DoE's templates are already
      `100755` in git where relevant, so preserving destination mode is
      the correct behaviour here, not an oversight).
    - No reverse (live -> template) flag exists here. Capturing an
      out-of-band hand-edit back into a template is a different operation
      (rewrites a source repo, not an install) and — per the memo — would
      need its own, differently-named entrypoint if ever built; it is not
      a mode of this gate.
    - The bash oracle (verify-templates-bin-sync.sh) has been retired by
      DoE's bash clean-slate migration and no longer exists in DoE-claude
      — there is no live oracle left to stay byte-parity-locked against.
      This module's behaviour is now defined by this docstring and its
      co-located test, not by oracle parity.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

from coordinator_core._settings_home import settings_home
from coordinator_core.session.declared_writes import declare_write

# (live_name, template_name) pairs — relative filenames; both dirs rooted
# above. Mirrors the bash oracle's PAIRS array verbatim, in the same order.
_PAIRS: List[Tuple[str, str]] = [
    ("claude_machine_local.py", "claude_machine_local.py"),
    ("claude-machine-local.sh", "claude-machine-local.sh"),
    ("claude-machine-local.ps1", "claude-machine-local.ps1"),
    # _machine_local.py's canonical source is the TEMPLATE, same as every
    # other pair now that --fix copies template -> live uniformly -- see
    # module negative-spec (the old live-canonical special case is retired).
    ("_machine_local.py", "_machine_local.py"),
    # resolve-coordinator-clone: position-independent shim, byte-identical
    # live<->template by design (it never reads its own BASH_SOURCE). Both
    # copies are authored identical.
    ("resolve-coordinator-clone", "resolve-coordinator-clone"),
]


def _claude_home_bin_dir() -> str:
    """Resolve ~/.claude/bin honouring Convention A verbatim (see negative-spec).

    Migration-window FALLBACK resolver only — see _resolve_live_bin_dir()
    and the module "Live bin resolution" docstring section for the primary
    (settings-home-rooted) resolution this backs up.
    """
    home_substitute = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(home_substitute, ".claude", "bin")


def _resolve_live_bin_dir() -> str:
    """Resolve the live-install bin directory this gate should watch.

    Primary: `<settings_home()>/bin` — resolved ONCE, for the whole run, not
    per-file. Falls back to `_claude_home_bin_dir()` (the pre-migration
    `${CLAUDE_HOME:-$HOME}/.claude/bin`, Convention A) ONLY when the
    settings-home bin directory does not exist as a directory at all
    (`os.path.isdir` false — this covers both "settings-home itself was
    never created" and "settings-home exists but has no bin/ subdir yet");
    a settings-home/bin that exists but is merely missing one or more of the
    five PAIRS files still wins over the fallback (those pairs correctly
    surface as LIVE_MISSING against the settings-home location, not silently
    redirected to a stale pre-migration copy). See module docstring "Live
    bin resolution" for the rationale against per-file fallback.
    """
    settings_bin = os.path.join(str(settings_home()), "bin")
    if os.path.isdir(settings_bin):
        return settings_bin
    return _claude_home_bin_dir()


def _resolve_templates_bin(plugin_root: str) -> str:
    return os.path.join(plugin_root, "templates", "bin")


def main(argv: List[str]) -> int:
    """CLI entry: argv[0] is the plugin root (coordinator/ directory).

    argv[1], if present, selects the mode: "--fix" enables template->live
    copy-on-mismatch (see module docstring "Modes"); anything else (including
    absent) behaves as verify-only, matching the bash oracle's
    `MODE="${1:-verify}"` + `[ "$MODE" = "--fix" ]` comparison exactly (a
    bogus mode string silently falls through to verify, not an error).

    Second positional (argv[1]) mirrors the oracle's own single positional
    arg ($1) -- this trampoline forwards sys.argv[1:] here, so argv[0] in
    THIS function is the plugin_root the trampoline resolved, and argv[1] is
    the oracle's $1.
    """
    if not argv:
        print("verify-templates-bin-sync.sh: missing required plugin_root argument", file=sys.stderr)
        return 2
    plugin_root = argv[0]
    mode = argv[1] if len(argv) > 1 else "verify"

    templates_bin = _resolve_templates_bin(plugin_root)
    live_bin = _resolve_live_bin_dir()

    # An absent templates ROOT is a distinct fact from a per-file TMPL_MISSING
    # and must not be indistinguishable from it: "this one template hasn't
    # been authored yet" (per-file, graceful) vs. "the entire templates root
    # is absent, so nothing was compared" (root-level, almost always a
    # misresolved plugin_root). Hard-fail BEFORE the per-file loop, in BOTH
    # modes — including --fix. A --fix run against an absent root does not
    # repair drift; every pair under a bogus/absent root would spuriously
    # read as TMPL_MISSING (now permanently unfixable — see the per-pair
    # loop below), masking the real problem (a misresolved plugin_root)
    # behind five unrelated-looking failures. Bootstrapping a genuinely-new
    # templates root is a deliberate human `mkdir`, not a side effect of
    # running this gate.
    if not os.path.isdir(templates_bin):
        print(
            f"TEMPLATES_ROOT_MISSING {templates_bin} (templates root does not exist — "
            "verified nothing; plugin_root is likely misresolved)"
        )
        return 1

    exit_code = 0
    any_checked = False

    for live_name, tmpl_name in _PAIRS:
        live_path = os.path.join(live_bin, live_name)
        tmpl_path = os.path.join(templates_bin, tmpl_name)

        live_exists = os.path.isfile(live_path)
        tmpl_exists = os.path.isfile(tmpl_path)

        if not live_exists and not tmpl_exists:
            print(
                f"NOT_PRESENT  {live_name} (neither live nor template exists yet — "
                f"the installer places these under {live_bin})"
            )
            continue

        if not live_exists:
            # LIVE_MISSING: template exists, live absent — FIXABLE (the
            # template is canonical, so --fix installs the live copy from
            # it). Only branch that creates a directory, and only inside
            # --fix: the live bin dir itself may not exist yet.
            if mode == "--fix":
                Path(live_bin).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(tmpl_path, live_path)
                # DR-276: declared AFTER the copy lands, matching the
                # append-integrator-dispositions reference — a report of
                # what was ACTUALLY written, not an intended surface.
                declare_write(live_path)
                print(f"INSTALLED    {live_name} ← templates/bin/{tmpl_name}")
                any_checked = True
            else:
                print(f"LIVE_MISSING {live_name} (template exists but live copy absent at {live_path})")
                exit_code = 1
            continue

        if not tmpl_exists:
            # TMPL_MISSING: live exists, template absent — UNFIXABLE under
            # --fix (nothing to copy FROM; the template is canonical and
            # there is no template content to install). Still contributes
            # to a nonzero exit code in BOTH modes.
            print(f"TMPL_MISSING {live_name} (live exists but template absent at {tmpl_path})")
            exit_code = 1
            continue

        any_checked = True

        if filecmp.cmp(live_path, tmpl_path, shallow=False):
            print(f"OK           {live_name}")
        else:
            if mode == "--fix":
                shutil.copyfile(tmpl_path, live_path)
                declare_write(live_path)
                print(f"FIXED        {live_name} ← templates/bin/{tmpl_name}")
            else:
                print(f"MISMATCH     {live_name}")
                exit_code = 1

    if not any_checked and exit_code == 0:
        print("no files present — nothing to verify (install-substrate.sh creates the live copies at ~/.claude/bin/)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

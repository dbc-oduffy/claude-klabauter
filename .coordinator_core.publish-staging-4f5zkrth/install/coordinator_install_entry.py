"""coordinator-install — the discoverable install entry in the settings-home ``bin/``.

WHY THIS EXISTS: an agent that greps ``install|doctor`` in
``<settings-home>/bin/`` used to get five diagnostics and no installer. Worse
than that, measured on the live box 2026-08-17: ``coordinator-uninstall`` is
present and ``coordinator-install`` is absent, so the highest-affinity hit for a
substring search on ``install`` is the command that REMOVES the installation.
The agent does not come back empty-handed and stop — it comes back holding the
destructive inverse of what it asked for, under a name that reads correct.

Requested by doe-claude-em, who had first rejected the idea on sprawl grounds and
then reversed: sprawl defeats BROWSING, and a grep's result set is unaffected by
807 non-matching entries. One entry changes that grep's answer.
(``cross-repo/inbox/2026-08-17-doe-claude-em-install-entrypoint-what-we-need-from-you.md``
§ 4a.) makima owns it because makima populates that directory — an entry we
generate is not a DoE-side entrypoint, which is what kept the ownership ruling
intact. No file in DoE's tree is involved: the settings-home forwarder is derived
dynamically from makima's own ``coordinator/bin/`` listing
(``substrate._derive_agent_helper_target_map``), so adding the CLI there is the
whole mechanism.

WHAT IT DISPATCHES: makima's OWN declared installer, resolved from makima's own
manifest — never a hardcoded path.

Never a hardcoded path, because hardcoding is the defect the whole 4-series
repairs: DoE's ``b644d5a9b`` moved their entry out of ``coordinator/scripts/``
and every surface naming it by path went stale in the same instant. An entry
generated today against a literal path is the next instance. Reading the manifest
means this survives the next move for free. That half of doe-claude-em's ruling
stands unconditionally and is implemented here.

WHY MAKIMA'S INSTALLER AND NOT COORDINATOR-CLAUDE'S, which reverses the first
answer we were given: doe-claude-em initially ruled the target should be
coordinator-claude's ``programmatic_entry_point``, on the reasoning that
coordinator-claude depends on makima and so a walk rooted at makima's installer
would exit 0 having installed nothing useful. They then retracted that reasoning
in full (``cross-repo/inbox/2026-08-17-doe-claude-em-retracting-my-dependency-
direction-claim.md``) — makima's manifest declares ``coordinator-claude`` a HARD
``direct_deps`` entry, and ``scripts/setup.py``'s Responsibility 2 exits 90 when
it is missing, so the silent-success failure they invoked does not exist. They
explicitly deferred the final call to us, asking one decidable question: does
``coordinator_core.ops.setup_chain_walker`` actually DRIVE coordinator-claude's
install, or only dispatch a single node?

Answered from the walker's own contract, and it settles it: it PROBES. Its
docstring states coordinator-claude "is the terminal node of the OSS
plugin-adoption chain (chain step 5 of 5 — nothing installs 'above' it)"; the
walker probes its one declared dep and terminates with the chain-complete banner,
exiting 90 when the dep is missing. Nothing anywhere installs coordinator-claude
programmatically — it is adopted by cloning the plugin. So the two candidates
converge: neither installs the plugin, and the only one whose target EXISTS is
this one. Measured 2026-08-17: coordinator-claude's declared
``programmatic_entry_point.posix`` (``coordinator/scripts/install-maximalist.py``)
is present in neither their working tree nor the published mirror, so dispatching
it would fail at resolution on every box today.

The resulting behaviour is honest in both states: coordinator-claude present →
the engine installs; coordinator-claude absent → exit 90 naming the missing
dependency, which is the correct answer to "how do I install this" and is
strictly better than the silence that made the agent grep in the first place.

REFUSAL IS A CONFORMING OUTCOME. Exit 96
(``scripts/setup.py::EXIT_INTERPRETER_UNSUPPORTED``) is a designed refusal, not a
failure — an externally-managed (PEP 668) interpreter that the installer will not
override. This module forwards the dispatched exit code verbatim so that
discriminator survives to the caller; it never remaps 96 onto a generic failure,
and never prints text implying the run broke.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

#: Forwarded verbatim rather than remapped — see module docstring.
EXIT_INTERPRETER_UNSUPPORTED = 96

_USAGE = """\
usage: coordinator-install [--check-only] [-- <args>]

Installs coordinator by dispatching this repo's declared installer, read from
its agent-install manifest rather than a hardcoded path.

coordinator-claude is a HARD dependency and is not installed by this command —
nothing installs it programmatically; it is adopted by cloning the plugin. When
it is absent the underlying installer exits 90 naming it, which is the correct
answer, not a defect in this entry.

  --check-only  probe install state without mutating the box
  -- <args>     forwarded verbatim to the underlying installer

Exit codes: 0 success. 96 designed refusal (an interpreter this installer will
not override, e.g. PEP 668 externally-managed) — remediation names a supported
interpreter; this is a conforming outcome, not a breakage. Other non-zero: the
underlying installer's own code, forwarded verbatim.
"""


class InstallEntryError(RuntimeError):
    """Resolution failed before anything was dispatched — fail loud.

    Never raised for a non-zero exit from the dispatched installer: that code is
    the installer's answer and is forwarded, not reinterpreted here.
    """


def _declared_installer(makima_root: Path) -> "tuple[Path, dict]":
    """Read makima's OWN declared installer from its own manifest.

    Returns the resolved script path plus its ``entry_point_contract`` so the
    caller passes the DECLARED flag spellings rather than assuming any — makima
    declares ``--check`` where coordinator-claude declares ``--check-only``, and
    guessing across that difference is how a probe silently becomes a mutation.
    """
    from coordinator_core.install.manifest_reader import (
        _load_manifest,
        resolve_manifest_path,
    )

    manifest_path = resolve_manifest_path(makima_root)
    manifest = _load_manifest(manifest_path)

    block = manifest.get("standalone_setup_script")
    if not isinstance(block, dict):
        raise InstallEntryError(
            f"coordinator-install: {manifest_path} declares no standalone_setup_script.\n"
            f"That field is this repo's declared installer. Remediation: fix the manifest "
            f"at {manifest_path}."
        )

    key = "windows" if os.name == "nt" else "posix"
    declared = block.get(key)
    if not isinstance(declared, str) or not declared:
        raise InstallEntryError(
            f"coordinator-install: standalone_setup_script.{key} in {manifest_path} is "
            f"{type(declared).__name__} {declared!r}, not a path string.\n"
            f"This platform has no declared installer. Remediation: declare "
            f"standalone_setup_script.{key} in that manifest."
        )

    # Manifest paths are repo-root-relative, and the manifest sits in either the
    # nested working-tree layout or the flat publish-mirror layout; resolve
    # against the root the manifest was actually found under, not a guess.
    manifest_repo_root = manifest_path.parent.parent.parent
    script = (manifest_repo_root / declared).resolve()
    if not script.is_file():
        raise InstallEntryError(
            f"coordinator-install: standalone_setup_script.{key} declares '{declared}', "
            f"which does not resolve under {manifest_repo_root}.\n"
            f"The declared installer has moved or was not published. Remediation: "
            f"repoint that field at a file that exists — a declared entry point that "
            f"does not resolve fails the install it claims to describe."
        )

    contract = block.get("entry_point_contract")
    return script, contract if isinstance(contract, dict) else {}


def main(argv: "Optional[list[str]]" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    passthrough: "list[str]" = []
    if "--" in argv:
        cut = argv.index("--")
        passthrough = argv[cut + 1:]
        argv = argv[:cut]

    if "--help" in argv or "-h" in argv:
        print(_USAGE, end="")
        return 0

    check_only = "--check-only" in argv
    argv = [a for a in argv if a != "--check-only"]

    from coordinator_core.engine_root import coordinator_engine_root_with_class

    try:
        makima_root_str, _cls = coordinator_engine_root_with_class()
        script, contract = _declared_installer(Path(makima_root_str))
    except InstallEntryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # resolution failure, not an install failure
        print(f"coordinator-install: could not resolve the install entry: {exc}", file=sys.stderr)
        return 1

    cmd = [sys.executable, str(script)]
    if check_only:
        flag = contract.get("check_only_flag")
        if not isinstance(flag, str) or not flag:
            print(
                f"coordinator-install: --check-only requested, but {script} declares no "
                f"check_only_flag in its entry_point_contract. Refusing to guess a flag "
                f"spelling — a wrong guess turns a probe into a mutation.",
                file=sys.stderr,
            )
            return 1
        cmd.append(flag)
    else:
        flag = contract.get("non_interactive_flag")
        if isinstance(flag, str) and flag:
            cmd.append(flag)
    cmd.extend(passthrough)

    # `no_console_passthrough_kwargs`, NOT `no_console_creationflags`: this is a
    # passthrough delegation to the declared installer and everything it prints is
    # meant for the operator watching the install. The creationflags-only helper
    # would suppress the conhost popup and then bind the child's handles to that
    # fresh window-less console, losing the whole install log on Windows — see that
    # function's own "THE CATCH" note. Gates:
    # `coordinator_core/tests/test_no_bare_hot_path_spawn.py` (the popup) and
    # `test_no_output_swallowing_no_console_spawn.py` (the lost output) — both must
    # hold, and only the passthrough helper satisfies both here.
    from coordinator_core.win_portability import no_console_passthrough_kwargs

    return subprocess.call(cmd, **no_console_passthrough_kwargs())


if __name__ == "__main__":
    sys.exit(main())

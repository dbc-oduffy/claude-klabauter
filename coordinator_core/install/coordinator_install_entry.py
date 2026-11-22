"""coordinator-install — the discoverable install entry in the settings-home ``bin/``.

WHY THIS EXISTS: an agent that greps ``install|doctor`` in
``<settings-home>/bin/`` used to get five diagnostics and no installer. Worse
than that, measured on the live box 2026-08-17: ``coordinator-uninstall`` is
present and ``coordinator-install`` is absent, so the highest-affinity hit for a
substring search on ``install`` is the command that REMOVES the installation.
The agent does not come back empty-handed and stop — it comes back holding the
destructive inverse of what it asked for, under a name that reads correct.

Requested by coordinator-content-repo-em, who had first rejected the idea on sprawl grounds and
then reversed: sprawl defeats BROWSING, and a grep's result set is unaffected by
807 non-matching entries. One entry changes that grep's answer.
(``cross-repo/inbox/2026-08-17-coordinator-content-repo-em-install-entrypoint-what-we-need-from-you.md``
§ 4a.) claude-klabauter owns it because claude-klabauter populates that directory — an entry we
generate is not a DoE-side entrypoint, which is what kept the ownership ruling
intact. No file in DoE's tree is involved: the settings-home forwarder is derived
dynamically from claude-klabauter's own ``coordinator/bin/`` listing
(``substrate._derive_agent_helper_target_map``), so adding the CLI there is the
whole mechanism.

WHAT IT DISPATCHES: claude-klabauter's OWN declared installer, resolved from claude-klabauter's own
manifest — never a hardcoded path.

Never a hardcoded path, because hardcoding is the defect the whole 4-series
repairs: DoE's ``b644d5a9b`` moved their entry out of ``coordinator/scripts/``
and every surface naming it by path went stale in the same instant. An entry
generated today against a literal path is the next instance. Reading the manifest
means this survives the next move for free. That half of coordinator-content-repo-em's ruling
stands unconditionally and is implemented here.

WHY CLAUDE-KLABAUTER'S INSTALLER AND NOT COORDINATOR-CLAUDE'S, which reverses the first
answer we were given: coordinator-content-repo-em initially ruled the target should be
coordinator-claude's ``programmatic_entry_point``, on the reasoning that
coordinator-claude depends on claude-klabauter and so a walk rooted at claude-klabauter's installer
would exit 0 having installed nothing useful. They then retracted that reasoning
in full (``cross-repo/inbox/2026-08-17-coordinator-content-repo-em-retracting-my-dependency-
direction-claim.md``) — claude-klabauter's manifest declares ``coordinator-claude`` a HARD
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

EXIT_INTERPRETER_UNSUPPORTED = 96

_USAGE = """\
usage: coordinator-install [--plan | --check-only] [--answers <json|file>] [--host <hosts>] [-- <args>]

Installs coordinator by dispatching this repo's declared installer, read from
its agent-install manifest rather than a hardcoded path.

coordinator-claude is a HARD dependency and is not installed by this command —
nothing installs it programmatically; it is adopted by cloning the plugin. When
it is absent the underlying installer exits 90 naming it, which is the correct
answer, not a defect in this entry.

  --check-only  probe install state without mutating the box
  --plan        print the default planner without installing
  --answers     shared codex_runtime enrol/skip and literal boolean consent
  --host <list> comma-separated hosts (e.g. codex,agy) configured after base setup
                Bare codex uses legacy host setup. With --answers, enrol
                enrolls the Codex runtime with consent.codex_runtime=true.
                Skip omits Codex setup. Other selected hosts use legacy setup.
  -- <args>     forwarded verbatim to the underlying installer. A forwarded
                --host containing codex conflicts with an explicit skip.

Exit codes: 0 success. 96 designed refusal (an interpreter this installer will
not override, e.g. PEP 668 externally-managed) — remediation names a supported
interpreter; this is a conforming outcome, not a breakage. Other non-zero: the
underlying installer's own code, forwarded verbatim.
"""


class InstallEntryError(RuntimeError):
    pass


def _planner(root: Path):
    """Load the selected engine's installer planner, never an ambient plugin copy."""
    import importlib.util
    name = "_coordinator_install_runtime_selection"
    spec = importlib.util.spec_from_file_location(
        name, root / "coordinator" / "lib" / "install" / "coordinator_install.py")
    if not spec or not spec.origin or not Path(spec.origin).is_file():
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_selection(root: Path, answers_spec: str | None) -> bool | None:
    """Validate supplied consent; None preserves omitted host-only selection."""
    module = _planner(root)
    if module is None:
        if answers_spec:
            raise InstallEntryError("coordinator-install: runtime planner missing from selected engine")
        return None
    raw = module._load_answers(answers_spec)
    requested = module.codex_runtime_requested(raw)
    return requested if "codex_runtime" in raw else None


def _enrollment_roots(script: Path, base_args: list[str]) -> tuple[Path, Path]:
    """Resolve the base setup selection once and bind both subprocesses to it."""
    import runpy

    setup = runpy.run_path(str(script))
    try:
        args = setup["parse_args"](base_args)
    except setup["ArgError"] as exc:
        raise InstallEntryError(str(exc)) from exc
    if (args.help or args.check or args.preflight or args.uninstall_pruner
            or args.deps_only or args.register_only or args.repair or args.hosts):
        raise InstallEntryError(
            "Codex runtime enrollment requires a full base install. Remove the "
            "setup mode after --, or use coordinator-install --help or --check-only."
        )
    source, resolution = setup["_resolve_coordinator_claude_root"](script.parent.parent, args)
    plugin = setup["_resolve_plugin_root_for_machine_local"](source)
    if resolution.is_publish_mirror_rejected or plugin is None:
        raise InstallEntryError(f"cannot enroll Codex from coordinator root {source} ({resolution.display})")
    return source.resolve(), plugin.resolve()


def _declared_installer(claude_klabauter_root: Path) -> "tuple[Path, dict]":
    """Read claude-klabauter's OWN declared installer from its own manifest.

    Returns the resolved script path plus its ``entry_point_contract`` so the
    caller passes the DECLARED flag spellings rather than assuming any — claude-klabauter
    declares ``--check`` where coordinator-claude declares ``--check-only``, and
    guessing across that difference is how a probe silently becomes a mutation.
    """
    from coordinator_core.install.manifest_reader import (
        _load_manifest,
        resolve_manifest_path,
    )

    manifest_path = resolve_manifest_path(claude_klabauter_root)
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
    plan_only = "--plan" in argv
    argv = [a for a in argv if a != "--plan"]
    if "--enroll-runtime" in passthrough:
        print("coordinator-install: enrollment requires validated --answers or explicit enrollment consent", file=sys.stderr)
        return 2

    host_args: "list[str]" = []
    rest: "list[str]" = []
    answers_spec = None
    it = iter(argv)
    for a in it:
        if a == "--answers" or a.startswith("--answers="):
            answers_spec = next(it, None) if a == "--answers" else a.partition("=")[2]
            if not answers_spec or not answers_spec.strip():
                print("coordinator-install: --answers requires JSON or a file", file=sys.stderr)
                return 2
        elif a == "--host" or a.startswith("--host="):
            value = next(it, None) if a == "--host" else a.partition("=")[2]
            if not value or value.startswith("--") or not any(host.strip() for host in value.split(",")):
                print("coordinator-install: --host requires a value, e.g. --host codex,agy", file=sys.stderr)
                return 2
            host_args = ["--host", value]
        else:
            rest.append(a)
    if rest:
        print(
            f"coordinator-install: unsupported option(s): {' '.join(rest)}. "
            "Nothing was run. Supported: --plan, --answers, --check-only, --host <list>, -- <args>, --help.",
            file=sys.stderr,
        )
        return 2

    from coordinator_core.engine_root import _load_shim, coordinator_engine_root_with_class

    try:
        claude_klabauter_root_str, _cls = coordinator_engine_root_with_class()
        if not claude_klabauter_root_str:
            print(_load_shim().unresolved_engine_message(), end="", file=sys.stderr)
            return 1
        script, contract = _declared_installer(Path(claude_klabauter_root_str))
    except InstallEntryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"coordinator-install: could not resolve the install entry: {exc}", file=sys.stderr)
        return 1

    cmd = [sys.executable, str(script)]
    if plan_only:
        planner = _planner(Path(claude_klabauter_root_str))
        if planner is None:
            print("coordinator-install: runtime planner missing from selected engine", file=sys.stderr)
            return 1
        return planner.main(["--plan"])
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
    if check_only:
        cmd.extend(host_args)
    cmd.extend(passthrough)

    from coordinator_core.win_portability import no_console_passthrough_kwargs

    try:
        enroll = False if check_only else _runtime_selection(Path(claude_klabauter_root_str), answers_spec)
        if enroll:
            source, plugin = _enrollment_roots(script, cmd[2:])
            if "--coordinator-root" not in passthrough:
                cmd.extend(["--coordinator-root", str(source)])
    except (InstallEntryError, ValueError, OSError) as exc:
        print(f"coordinator-install: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)

    if enroll is False and not check_only:
        # Stripping the last forwarded host would turn host-only setup into a full install.
        for option, hosts in zip(passthrough, passthrough[1:]):
            if option == "--host" and "codex" in (host.strip() for host in hosts.split(",")):
                print("coordinator-install: codex_runtime=skip conflicts with forwarded "
                      "--host codex. Select other hosts before --.", file=sys.stderr)
                return 2

    if enroll is False and not check_only and host_args:
        remaining = [h for h in host_args[1].split(",") if h.strip().lower() != "codex"]
        host_args = ["--host", ",".join(remaining)] if remaining else []

    rc = subprocess.call(cmd, **no_console_passthrough_kwargs())
    if rc != 0 or check_only or not (host_args or enroll):
        return rc
    # setup.py --host is host-only: the full install runs first, the host step is its own call.
    host_cmd = [sys.executable, str(script)]
    flag = contract.get("non_interactive_flag")
    if isinstance(flag, str) and flag:
        host_cmd.append(flag)
    if enroll:
        # Other requested hosts keep their legacy MCP-only leg.
        others = [h for h in (host_args[1].split(",") if host_args else []) if h.strip() != "codex"]
        if others:
            rc = subprocess.call([*host_cmd, "--host", ",".join(others)], **no_console_passthrough_kwargs())
            if rc:
                return rc
        host_cmd.extend(["--host", "codex", "--enroll-runtime"])
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin)
        return subprocess.call(host_cmd, stdin=subprocess.DEVNULL, env=env, **no_console_passthrough_kwargs())
    host_cmd.extend(host_args)
    return subprocess.call(host_cmd, **no_console_passthrough_kwargs())


if __name__ == "__main__":
    sys.exit(main())

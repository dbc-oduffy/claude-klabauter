"""
coordinator_core.ops.setup_chain_walker — coordinator-claude install-chain walker.

Purpose: full reimplementation of DoE-claude's 922-line bash chain-walker as
a native Python module. coordinator-claude
is the terminal node of the OSS plugin-adoption chain (chain step 5 of 5 —
nothing installs "above" it) but, as of W0.5 Option B+C (2026-07-19),
declares ONE hard direct_dep of its own: claude-klabauter (the engine).
"DAG root" here means "root of the plugin chain", not "zero dependencies" —
this walker probes that one dep (via the claude_klabauter_seam_resolvable probe kind,
which self-confirms rather than sibling-dir-probes — see dep_probe()) and
terminates with the chain-complete banner. It also serves `--check`
(read-only dep probe), `--preflight` (dep probe + machine-environment
prerequisite probe), and `--phase chain-preinstall` (stateful-by-contract
seam, no-op body at the root).

Scope boundary (deliberate, not a gap): this module owns setup.sh's OWN
control flow (arg parsing, override-flag-pair gate, agent-direct short
circuit, phase dispatch, consent gate, unified prereq-row emission/exit-gate).
Manifest reading and dep functional-probing (sibling_dir_exists/file_exists/
file_exists_any/python_import/command_succeeds/claude_klabauter_seam_resolvable) are ported natively
here (self-contained, no bash dependency — see `command_succeeds_native`
for the `command_succeeds` probe kind's narrowed shell-subset evaluator,
C11 de-bash cutover). claude_klabauter_seam_resolvable (added
W0.5 Option B+C, 2026-07-19) is the one probe kind that bypasses the
sibling_dir_exists gate -- see dep_probe()'s inline comment for why a
registry-resolved dep (claude-klabauter itself) cannot be sibling-directory
probed the way ordinary plugin-to-plugin deps are. Machine environment
prerequisite probing (git, python, uv, gh, node, pwsh, ue, clone_auth,
longpaths, git_lfs, shell_login_env) is NOT reimplemented in this module —
it consumes the already-landed native port, `coordinator_core.install.
prereq_probe` (the same module `coordinator_core.ops.normalize_env` cut
over to on 2026-07-21), via `prereq_probe_all_via_bash` (name kept for
call-site stability; the bash subprocess bridge to `scripts/lib/
prereq_probe.sh` was retired in the C11 de-bash cutover — the function now
calls `prereq_probe.probe_all()` in-process).

Port of: setup.sh (DoE 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-06-15-coordinator-install-chain-application-phase-b.md §7 C2
               docs/plans/2026-06-17-coordinator-install-seed-phase-and-manifest-alignment.md §C2
               docs/plans/2026-06-23-clone-auth-semi-hard-step-zero.md §C3

Exit codes (parity-critical — matches the bash oracle exactly):
    0   success (DAG root — no deps to check; or all deps satisfied)
    1   hard probe failure (--preflight / full-install prereq gate, OR a
        --check-mode hard dep that is present but fails its own functional
        probe — present-but-broken, Defect A fix 2026-08-07) or
        unreadable/corrupt manifest, or missing scripts/lib assets
    90  non-interactive/non-TTY hard dep missing, no override flag pair
    91  user declined (interactive double-confirm)
    92  agent-direct invocation without override flag pair
    93  override flag pair incomplete (only one of two flags supplied)
    94  semi-hard probe unverified (clone_auth unauthenticated; suppress with
        --accept-no-git-auth)

Negative-spec:
    - Does NOT probe git/python/uv/gh/node/pwsh/ue/clone_auth/longpaths/
      git_lfs/shell_login_env itself — delegates to `prereq_probe.probe_all()`
      (see Scope boundary above). This is a faithful scope subset, not a
      silent behavior drop: the delegate reproduces the oracle's own NDJSON
      contract byte-for-byte, and callers observe identical rows/exit codes.
    - Does NOT write install-status, manifest, or any persistent state on
      any of the read-only flags (--help --version --phase-list
      --phase seed-install-spinoff --last-status --i-am-agent --check
      --preflight) — matches the oracle's read-only carve-out exactly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from coordinator_core.coordinator_root import _resolve_plugin_root_for_machine_local
from coordinator_core.install import prereq_probe
from coordinator_core.install.manifest_reader import _dep_to_record
from coordinator_core.machine_resolver import registry_get
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()


_SCRIPT_VERSION = "1.0.0"
_SCRIPT_NAME = "coordinator-claude setup"
_CHAIN_STEP = "chain step 5 of 5"
_CHAIN_BANNER = f"coordinator install-chain walker — {_CHAIN_STEP}"

_MANIFEST_REL = "docs/install/agent-install-manifest.json"

READ_ONLY_PHASE_NAMES = {"seed-install-spinoff"}


class _SetupError(Exception):
    """Internal control-flow signal carrying an intended process exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"setup exit {code}")
        self.code = code


# ---------------------------------------------------------------------------
# Manifest reading + dep functional probes — native port, no bash dependency.
# ---------------------------------------------------------------------------

def resolve_manifest_path(repo_root: Path, err=None) -> Path | None:
    """Layout-aware resolution of agent-install-manifest.json.

    Nested (working-tree/mirror): <repo_root>/docs/install/agent-install-manifest.json
    Flat (publish-repo-root):     <repo_root>/../docs/install/agent-install-manifest.json
    """
    err = sys.stderr if err is None else err
    nested = repo_root / _MANIFEST_REL
    flat = repo_root / ".." / _MANIFEST_REL
    if nested.is_file():
        return nested.resolve()
    if flat.is_file():
        return flat.resolve()
    print("ERROR: install manifest not found in either layout location:", file=err)
    print(f"  nested (working-tree/mirror): {nested}", file=err)
    print(f"  flat   (publish-repo-root):   {flat}", file=err)
    print(
        "  Remediation: re-publish BOTH install-surface targets from the meta-repo "
        "(POSIX: python3 / Windows: python) — "
        "python3 coordinator/bin/publish.py coordinator-claude-toplevel-install ; "
        "python3 coordinator/bin/publish.py coordinator-claude",
        file=err,
    )
    return None


def _loaded_manifest_repo_id(manifest_path: Path | None) -> str:
    """Read the ``repo_id`` field out of the LOADED manifest, for the
    ``--check``/``--preflight`` banners (Defect B part 3). Replaces the prior
    hardcoded ``coordinator-claude`` literal, which is what hid Defect B's
    wrong-tree walk: the banner claimed ``repo: coordinator-claude``
    regardless of which manifest was actually loaded, so a walk that had
    silently fallen back to `<claude-klabauter-root>/coordinator`'s OWN manifest
    (`repo_id: claude-klabauter`) still announced itself as walking
    coordinator-claude. Returns a bracketed diagnostic placeholder — never
    the stale literal — when ``manifest_path`` is None or the field/file is
    unreadable, so a caller can never mistake the placeholder for a real
    repo id."""
    if manifest_path is None:
        return "(no manifest resolved)"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(manifest unreadable)"
    repo_id = data.get("repo_id", "")
    return str(repo_id) if repo_id else "(repo_id absent from manifest)"


def _normalize_dep(dep: dict[str, Any]) -> dict[str, Any]:
    """Flatten a dep's nested ``functional_probe: {kind, ...}`` object into
    ``functional_probe_kind``/``functional_probe_args`` via
    ``manifest_reader._dep_to_record`` — the SAME normalizer
    ``coordinator_core.install.dep_check`` uses, reused here rather than
    hand-rolled a second time. Without this, every dep in the real (nested)
    manifest schema probed ``functional_probe_kind == ""`` and silently
    degraded to present-but-broken, INCLUDING the claude_klabauter_seam_resolvable
    bypass this walker's own docstring names as its one real dep probe —
    caught 2026-07-22 (see test_dep_probe_nested_manifest_shape_normalizes).

    Back-compat: a dep already carrying flat ``functional_probe_kind`` and no
    nested ``functional_probe`` key is passed through untouched — some
    callers (tests, possibly a future hand-authored manifest) still author
    the pre-normalization flat shape directly (see
    test_dep_probe_flat_shape_backward_compat)."""
    if "functional_probe" in dep:
        return _dep_to_record(dep)
    return dep


def read_manifest_deps(manifest_path: Path) -> list[dict[str, Any]]:
    """Read direct_deps from the manifest. Raises on corrupt/unreadable JSON.
    Normalizes each dep via ``_normalize_dep`` — see that function's
    docstring for the nested-vs-flat schema history."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [_normalize_dep(dep) for dep in data.get("direct_deps", [])]


def _dep_by_id(deps: list[dict[str, Any]], dep_id: str) -> dict[str, Any] | None:
    for dep in deps:
        if dep.get("id") == dep_id:
            return dep
    return None


def _configurable_location_for(dep_id: str, manifest_path: Path) -> dict[str, Any] | None:
    """Read the FULL manifest JSON (not just ``direct_deps``, which
    ``read_manifest_deps`` narrows to) and return the ``configurable_locations``
    entry whose ``dependency_id`` matches ``dep_id``, or None if the manifest
    declares no override ladder for this dep. Generic over any manifest — the
    walker resolves deps for arbitrary repos, not only coordinator-claude, so
    this never special-cases a dep id string."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in data.get("configurable_locations", []) or []:
        if entry.get("dependency_id") == dep_id:
            return entry
    return None


def _resolve_dep_root(
    dep_id: str,
    sibling_dir: str,
    manifest_path: Path,
    repo_root: Path,
    argv: list[str] | None = None,
) -> tuple[Path, bool]:
    """Resolve a dep's root, honouring the manifest's declared override ladder
    for ``dep_id`` (``configurable_locations[].override.{flag,env}``) ahead of
    the plain sibling-dir default — the SAME rung order
    ``scripts/setup.py::_resolve_coordinator_claude_root`` uses: --<flag> CLI
    arg -> $<ENV> -> sibling dir. Falls back to today's sibling-dir-only
    behaviour when the manifest declares no ``configurable_locations`` entry
    for this dep (e.g. an ordinary plugin-to-plugin dep with no override
    surface of its own).

    ``argv`` is the CLI argv to scan for ``flag_name`` — threaded explicitly
    by every caller in this module (matching ``main``/``parse_args``'s own
    convention), rather than read from process-global ``sys.argv``. A caller
    that constructs its own argv and invokes ``main(argv)`` (every test in
    this file; any future library caller) would otherwise silently see the
    HOST process's real argv instead of the one it passed in. ``None``
    (the default, kept only so direct unit-test callers of this function and
    its ancestors keep working unchanged) falls back to ``sys.argv[1:]``.

    Returns ``(root_path, via_override)`` — ``via_override`` True means the
    ladder resolved via flag/env: an operator-supplied root is authoritative
    by construction (like ``claude_klabauter_seam_resolvable``'s bypass), so the caller
    should NOT apply the sibling_dir_exists "-claude"-strip fallback to it —
    a bad override path is reported missing outright, not silently
    re-resolved onto an unrelated sibling directory.
    """
    location = _configurable_location_for(dep_id, manifest_path)
    if location is not None:
        override = location.get("override", {}) or {}
        flag_name = str(override.get("flag", ""))
        env_name = str(override.get("env", ""))
        if flag_name:
            effective_argv = sys.argv[1:] if argv is None else argv
            for i, tok in enumerate(effective_argv):
                if tok == flag_name and i + 1 < len(effective_argv):
                    return Path(effective_argv[i + 1]), True
                if tok.startswith(flag_name + "="):
                    return Path(tok[len(flag_name) + 1 :]), True
        if env_name and os.environ.get(env_name):
            return Path(os.environ[env_name]), True
    return repo_root.parent / sibling_dir, False


def _upstream_url_dir_name(upstream_url: str) -> str:
    """The directory name a `git clone <upstream_url>` produces — the URL's
    last path segment with any trailing ``.git`` and slash removed. Empty
    string when the URL is absent or yields nothing usable.

    Sanity floor (Review: code-reviewer Finding 4, 2026-08-03): manifest data
    is author-trusted, not attacker-controlled, so this rejects only the
    shapes that would make `_sibling_fallback` hunt an implausible sibling
    directory name rather than any adversarial input — a name containing a
    path separator (a `../../etc`-shaped local path fragment) or a bare
    hostname shape (a dotted, slash-free segment like `github.com`, which is
    what a host-only URL with no path degrades to)."""
    trimmed = upstream_url.strip().rstrip("/")
    if not trimmed:
        return ""
    name = trimmed.rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not name or name in {".", ".."}:
        return ""
    if "/" in name or "\\" in name:
        return ""
    if "." in name:
        return ""
    return name


def _engine_tree_root() -> Path:
    """The claude-klabauter engine's own checkout root (this module's own tree),
    resolved the same way ``_self_resolve_walker_roots`` USED to guess the
    walked repo's root before Defect B's fix — kept here only as the
    rejection floor for ``_sibling_fallback``: a name-matched candidate that
    IS the engine's own tree (or lives under it) is never a legitimate
    coordinator-claude sibling and must not be silently accepted just
    because a directory happens to exist at that name."""
    return Path(__file__).resolve().parents[2]


def _functional_probe_ok(
    candidate: Path,
    probe_kind: str,
    probe_args: dict[str, Any],
    timeout: int = 30,
) -> bool:
    """True iff ``probe_kind`` succeeds against ``candidate``. Shared probe
    evaluator, reused by ``_sibling_fallback`` to vet a name-matched
    candidate BEFORE accepting it (Defect B) — a bare directory-name match
    is not proof the candidate is a working install of the dep; it must pass
    the dep's own functional probe, same as the ``sibling_path`` a normal
    (non-fallback) resolution produces.

    Deliberately silent (no err logging, no "unknown probe kind" warning) —
    a rejected fallback candidate is an expected, unremarkable outcome
    (`_sibling_fallback` just tries the next candidate / reports missing),
    not a diagnostic-worthy event the way an ACCEPTED dep's own final probe
    failure is."""
    if probe_kind == "sibling_dir_exists":
        return candidate.is_dir()
    if probe_kind == "file_exists":
        rel_path = str(probe_args.get("path", ""))
        return (candidate / rel_path).is_file()
    if probe_kind == "file_exists_any":
        rel_paths = probe_args.get("paths", []) or []
        return any((candidate / str(rel_path)).is_file() for rel_path in rel_paths)
    if probe_kind == "python_import":
        expr = str(probe_args.get("expr", ""))
        try:
            res = subprocess.run(
                [sys.executable, "-c", expr],
                cwd=os.getcwd(),
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                **_CREATIONFLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 0
    if probe_kind == "command_succeeds":
        cmd = str(probe_args.get("cmd", ""))
        return command_succeeds_native(cmd, timeout)
    return False


def _sibling_fallback(
    sibling_root: Path,
    sibling_dir: str,
    upstream_url: str,
    probe_kind: str = "",
    probe_args: dict[str, Any] | None = None,
    repo_root: Path | None = None,
    timeout: int = 30,
) -> Path | None:
    """Resolve a dep whose declared ``sibling_dir_name`` is not on disk, by
    trying the other names the same repo is legitimately cloned under.

    Two fallbacks, in order:

    1. The nested-layout ``-claude``-strip (pre-existing behaviour).
    2. The directory name ``upstream_url`` would clone into.

    (2) is what keeps a consumer's repoint a genuine ONE-FIELD change. The
    walker resolves on ``sibling_dir_name`` while ``upstream_url`` only builds
    the remediation hint, so a consumer who repoints the URL alone would leave
    an external adopter cloning `<url-basename>` while the walker hunts the old
    `sibling_dir_name` — reported missing, with a hint telling them to clone the
    URL they already cloned. Accepting both names resolves that loop without
    forcing every consumer into a dual-identity manifest: the declared
    ``sibling_dir_name`` keeps working for anyone who already has that clone.

    Defect B hardening (2026-08-07): a name-matched candidate is no longer
    accepted on directory-existence alone. Two additional gates apply before
    a candidate is returned:

    - It must satisfy the dep's OWN functional probe (``probe_kind``/
      ``probe_args``) — a directory that merely happens to share a plausible
      name but fails every probe path is not a legitimate resolution; the
      caller (``dep_probe``) must see ``missing``, never manufacture
      ``present``/``present-but-broken`` out of a coincidental name match.
      Skipped only when ``probe_kind`` is empty (back-compat for direct
      callers that don't have probe info to hand — none of this module's own
      call sites do that anymore).
    - It must not BE the claude-klabauter engine's own tree, nor a subdirectory of it,
      nor the walked repo's own ``repo_root`` (or a subdirectory of that) —
      the exact coincidental match (`<claude-klabauter-root>/coordinator` resolving as
      a "sibling" of itself) that manufactured Defect A's "found but broken"
      contradiction out of "not found here".

    Returns None when no candidate exists on disk and passes both gates —
    the caller reports the dep missing rather than proceeding on an
    unverified path.
    """
    probe_args = probe_args or {}
    engine_tree = _engine_tree_root().resolve()
    repo_root_resolved = repo_root.resolve() if repo_root is not None else None

    candidates: list[str] = []
    if sibling_dir.endswith("-claude"):
        candidates.append(sibling_dir[: -len("-claude")])
    url_name = _upstream_url_dir_name(upstream_url)
    if url_name and url_name != sibling_dir:
        candidates.append(url_name)
    for name in candidates:
        if not name:
            continue
        candidate = sibling_root / name
        if not candidate.is_dir():
            continue
        candidate_resolved = candidate.resolve()
        if candidate_resolved == engine_tree:
            continue
        try:
            candidate_resolved.relative_to(engine_tree)
            continue  # candidate is a subdirectory of the engine's own tree
        except ValueError:
            pass
        if repo_root_resolved is not None:
            try:
                candidate_resolved.relative_to(repo_root_resolved)
                continue  # candidate IS (or is under) the walked repo's own root
            except ValueError:
                pass
        if probe_kind and not _functional_probe_ok(candidate, probe_kind, probe_args, timeout):
            continue
        return candidate
    return None


def _run_probe_argv(argv: list[str], timeout: int) -> bool:
    """Run one tokenized, shell-free argv; True iff it exits 0.

    Deliberate isolation boundary — do not convert to an in-process call.
    Mechanism: crash containment — argv is a generic, manifest-declared
    probe command whose crash must not take down the walker. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    if not argv:
        return False
    try:
        res = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _run_probe_argv: res = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return res.returncode == 0


def command_succeeds_native(cmd: str, timeout: int) -> bool:
    """C11 de-bash cutover: evaluate a manifest `command_succeeds` probe's
    `cmd` string WITHOUT spawning `bash`/`sh`.

    Narrowed shell-subset, not a general POSIX-shell evaluator (the manifest
    is DoE-owned data, never claude-klabauter-authored, so full shell-grammar parity
    is not a reasonable native-port target — same "faithful subset, not the
    whole grammar" call as `coordinator_core.install.dep_check`'s sibling
    probe for this same manifest probe kind). Handles exactly the shape the
    oracle contract actually authors:
        - a single command (`argv0 arg1 arg2 ...`), OR
        - a `||`-separated fallback chain of such commands (each side tried
          in order; success on the first that exits 0), OR
        - a trailing `2>/dev/null` on a side (stripped; stderr is always
          captured/discarded regardless, so this is a parity no-op here).
    Each side is tokenized via `shlex.split` (POSIX quoting rules) and run
    as a literal argv — no shell metacharacter (`;`, `&&`, `|`, `` ` ``,
    `$(...)`, glob expansion, env-var substitution) is interpreted. A `cmd`
    using any of those beyond the `||`/`2>/dev/null` shapes above will not
    evaluate as the bash oracle would — this is the documented divergence.
    """
    if not cmd.strip():
        return False
    for side in cmd.split("||"):
        side = side.strip()
        if not side:
            continue
        side = side.removesuffix("2>/dev/null").strip()
        try:
            argv = shlex.split(side)
        except ValueError:
            print(f"skip: command_succeeds_native: argv = shlex.split(side) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if _run_probe_argv(argv, timeout):
            return True
    return False


def dep_probe(
    dep_id: str,
    manifest_path: Path,
    repo_root: Path,
    err=None,
    timeout: int = 30,
    argv: list[str] | None = None,
) -> str:
    """Probe a single dep by id. Returns present / missing / present-but-broken.

    ``argv`` threads through to ``_resolve_dep_root`` — see that function's
    docstring; ``None`` falls back to ``sys.argv[1:]`` for direct callers.

    Deliberate isolation boundary — do not convert the functional-probe
    subprocess to an in-process ``exec``/import. Mechanism: import-state
    isolation + crash containment — runs an arbitrary, manifest-supplied
    ``python_import`` expression, which is untrusted input; a failed or
    malicious import must not land in this process's own ``sys.modules``
    or crash it. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    err = sys.stderr if err is None else err
    try:
        deps = read_manifest_deps(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: _co_dep_probe: manifest unreadable or corrupt (dep '{dep_id}'): {exc}", file=err)
        return "missing"

    dep = _dep_by_id(deps, dep_id)
    if dep is None:
        return "missing"

    probe_kind_early = dep.get("functional_probe_kind", "")

    # "claude_klabauter_seam_resolvable": bypasses the sibling_dir_exists gate entirely.
    # Registry-resolved deps (claude-klabauter, declared as coordinator-claude's
    # W0.5 Option B+C hard dep) are NOT sibling-directory-colocated by
    # contract -- the engine root resolves via env var / settings-home pointer
    # file / machine-local registry (cc_invoke.py's four-rung
    # _resolve_claude_klabauter_root ladder), not a path literally adjacent to
    # repo_root.parent. Chicken-egg note: this very module
    # (coordinator_core.ops.setup_chain_walker) is authored INSIDE
    # claude-klabauter and only becomes importable AFTER setup.sh's trampoline
    # (_import_main -> _resolve_claude_klabauter_root) has already resolved the engine root
    # and put coordinator_core on sys.path -- so by the time this probe runs,
    # the seam is proven present by construction (or the trampoline already
    # exited 95 with four-rung remediation before this module could even be
    # imported; see coordinator/scripts/setup.sh's RuntimeError handler).
    # This probe re-confirms via a live find_spec check rather than a bare
    # `return "present"` so a corrupted/partial coordinator_core checkout
    # (module dir present, coordinator_core.invoke submodule missing) still
    # surfaces as present-but-broken instead of a silent tautology.
    if probe_kind_early == "claude_klabauter_seam_resolvable":
        try:
            spec = importlib.util.find_spec("coordinator_core.invoke")
        except (ModuleNotFoundError, ValueError):
            spec = None
        return "present" if spec is not None else "present-but-broken"

    sibling_dir = str(dep.get("sibling_dir_name", ""))
    sibling_path, via_override = _resolve_dep_root(dep_id, sibling_dir, manifest_path, repo_root, argv=argv)

    probe_kind = dep.get("functional_probe_kind", "")
    probe_args_early = dep.get("functional_probe_args", {}) or {}

    # Step 1: implicit sibling_dir_exists probe (nested-layout "-claude"-strip
    # fallback) — skipped when the root came from the override ladder
    # (_resolve_dep_root's via_override contract: a bad override path is
    # reported missing outright, not silently re-resolved onto an unrelated
    # sibling directory).
    if not sibling_path.is_dir():
        if via_override:
            return "missing"
        resolved = _sibling_fallback(
            repo_root.parent,
            sibling_dir,
            str(dep.get("upstream_url", "")),
            probe_kind=probe_kind,
            probe_args=probe_args_early,
            repo_root=repo_root,
            timeout=timeout,
        )
        if resolved is None:
            return "missing"
        sibling_path = resolved
    probe_args = probe_args_early

    if probe_kind == "sibling_dir_exists":
        return "present"
    if probe_kind == "file_exists":
        rel_path = str(probe_args.get("path", ""))
        return "present" if (sibling_path / rel_path).is_file() else "present-but-broken"
    if probe_kind == "file_exists_any":
        rel_paths = probe_args.get("paths", []) or []
        present = any((sibling_path / str(rel_path)).is_file() for rel_path in rel_paths)
        return "present" if present else "present-but-broken"
    if probe_kind == "python_import":
        expr = str(probe_args.get("expr", ""))
        try:
            res = subprocess.run(
                [sys.executable, "-c", expr],
                cwd=os.getcwd(),
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                **_CREATIONFLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"skip: dep_probe: res = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
            return "present-but-broken"
        return "present" if res.returncode == 0 else "present-but-broken"
    if probe_kind == "command_succeeds":
        cmd = str(probe_args.get("cmd", ""))
        return "present" if command_succeeds_native(cmd, timeout) else "present-but-broken"

    print(
        f"WARNING: unknown probe kind '{probe_kind}' for dep '{dep_id}'; treating as present-but-broken",
        file=err,
    )
    return "present-but-broken"


def dep_probe_all(
    manifest_path: Path,
    repo_root: Path,
    err=None,
    argv: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Probe all direct_deps. Returns list of {id, severity, status, sibling_path, hint}.

    ``argv`` threads through to ``_resolve_dep_root``/``dep_probe`` — see
    ``_resolve_dep_root``'s docstring; ``None`` falls back to
    ``sys.argv[1:]`` for direct callers."""
    err = sys.stderr if err is None else err
    try:
        deps = read_manifest_deps(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: dep_probe_all: manifest unreadable or corrupt: {exc}", file=err)
        raise _SetupError(1) from exc

    rows: list[dict[str, Any]] = []
    for dep in deps:
        dep_id = dep.get("id", "")
        severity = dep.get("severity", "")
        sibling_dir = str(dep.get("sibling_dir_name", ""))
        upstream_url = dep.get("upstream_url", "")

        # Review: code-reviewer (Finding 2, 2026-08-03) — call the SAME
        # _sibling_fallback helper dep_probe uses (both candidates: the
        # "-claude"-strip AND the upstream_url-basename), rather than
        # re-deriving a narrower, stale fallback inline. The prior inline
        # copy only tried the "-claude"-strip, so a dep resolved solely via
        # the URL-basename candidate got a correct `status` (dep_probe
        # recomputes it internally) but a stale, nonexistent `sibling_path`
        # and a remediation hint pointing at a directory that was never
        # cloned — exactly the external-adopter case this fallback exists
        # to serve.
        sibling_path, via_override = _resolve_dep_root(dep_id, sibling_dir, manifest_path, repo_root, argv=argv)
        if not via_override and not sibling_path.is_dir():
            resolved = _sibling_fallback(
                repo_root.parent,
                sibling_dir,
                str(upstream_url),
                probe_kind=str(dep.get("functional_probe_kind", "")),
                probe_args=dep.get("functional_probe_args", {}) or {},
                repo_root=repo_root,
            )
            if resolved is not None:
                sibling_path = resolved

        status = dep_probe(dep_id, manifest_path, repo_root, err=err, argv=argv)

        if status == "missing":
            hint = f"Clone from: {upstream_url}"
        elif status == "present-but-broken":
            hint = f"Sibling present but functional probe failed. Re-install or check: {sibling_path}"
        else:
            hint = ""

        rows.append(
            {
                "id": dep_id,
                "severity": severity,
                "status": status,
                "sibling_path": str(sibling_path),
                "hint": hint,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Prereq (machine environment) probes — native (C11 de-bash cutover; consumes
# coordinator_core.install.prereq_probe, the same native port normalize_env.py
# already cut over to). lib_dir/timeout retained in the signature for call-site
# compatibility (dep_consent_banner.txt still reads lib_dir elsewhere in this
# module) but are unused here — no subprocess bridge, so nothing to time out or
# locate on disk.
# ---------------------------------------------------------------------------

def prereq_probe_all_via_bash(lib_dir: Path, err=None, timeout: int = 60) -> list[dict[str, Any]]:
    """Native equivalent of scripts/lib/prereq_probe.sh's `_co_prereq_probe_all`
    (NDJSON out) — calls coordinator_core.install.prereq_probe.probe_all()
    directly instead of bridging to the bash oracle. Name kept for call-site
    stability; behavior is now 100% in-process."""
    del lib_dir, timeout
    err = sys.stderr if err is None else err

    rows: list[dict[str, Any]] = []
    for line in prereq_probe.probe_all():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"skip: prereq_probe_all_via_bash: rows.append(json.loads(line)) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
    return rows


# ---------------------------------------------------------------------------
# _co_pf_emit_row — unified table/NDJSON row emitter. BYTE-IDENTICAL across
# strict and post-consumer modes (severity demotion happens in the caller,
# before this function is invoked — mirrors the bash oracle's own contract).
# ---------------------------------------------------------------------------

_STATUS_WIDTH = 7


def pf_emit_row(
    dep_id: str,
    status: str,
    severity: str,
    hint: str,
    out=None,
    err=None,
) -> tuple[bool, bool]:
    """Print one table row (stderr) + one NDJSON line (stdout).

    Returns (hard_fail, semihard_fail) — whether this row should set the
    caller's _PF_HARD_FAIL / _PF_SEMIHARD_FAIL accumulators.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    hard_fail = False
    semihard_fail = False

    def _row(label: str, extra: str = "") -> None:
        line = f"  {label:<{_STATUS_WIDTH}} {dep_id:<20} ({severity})"
        if extra:
            line += f" — {extra}"
        print(line, file=err)

    if status in ("pass", "present"):
        _row("[PASS]")
    elif status in ("warn", "present-but-broken"):
        if severity == "semi-hard":
            _row("[BLOCK]", hint)
            semihard_fail = True
        else:
            _row("[WARN]", hint)
    elif status in ("fail", "missing"):
        if severity == "hard":
            _row("[FAIL]", hint)
            hard_fail = True
        elif severity == "semi-hard":
            _row("[BLOCK]", hint)
            semihard_fail = True
        else:
            _row("[WARN]", hint)
    elif status == "inconclusive":
        _row("[????]", hint)
    else:
        print(f"  [{status:<6}] {dep_id:<20} ({severity})", file=err)

    ndjson_status = {"present": "pass", "missing": "fail", "present-but-broken": "warn"}.get(status, status)
    row = {"id": dep_id, "status": ndjson_status, "severity": severity, "hint": hint}
    print(json.dumps(row, ensure_ascii=False), file=out)
    return hard_fail, semihard_fail


_DEMOTE_TO_ADVISORY_POST_CONSUMER = {"gh", "node", "git", "clone_auth"}


def run_prereq_gate(
    mode: str,
    lib_dir: Path,
    manifest_path: Path | None,
    repo_root: Path,
    accept_no_git_auth: bool,
    out=None,
    err=None,
    argv: list[str] | None = None,
) -> int:
    """Unified prereq-probe consumption + manifest-dep probe + exit-gate.

    mode: "strict" (--preflight, no demotion) or "post-consumer" (default
    chain-walk body: gh/node/git/clone_auth demoted to advisory; python
    stays hard).

    Returns dep_count (number of manifest dep rows probed) on success.
    Raises _SetupError(1) on hard-probe failure, _SetupError(94) on
    unaccepted semi-hard failure — mirrors the oracle's `exit` calls.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    hard_fail = False
    semihard_fail = False

    if accept_no_git_auth:
        print("[prereq-gate] --accept-no-git-auth: proceeding without verified git-host auth (operator override)", file=err)

    print("  --- environment prerequisite probes ---", file=err)
    prereq_rows = prereq_probe_all_via_bash(lib_dir, err=err)
    for row in prereq_rows:
        name = row.get("name", "")
        status = row.get("status", "")
        severity = row.get("severity", "")
        detail = row.get("detail", "")
        remediation = row.get("remediation", "")
        hint = detail
        if remediation:
            hint = f"{hint} | Remediation: {remediation}" if hint else f"Remediation: {remediation}"

        severity_eff = severity
        if mode == "post-consumer" and name in _DEMOTE_TO_ADVISORY_POST_CONSUMER:
            severity_eff = "advisory"

        hf, sf = pf_emit_row(name, status, severity_eff, hint, out=out, err=err)
        hard_fail = hard_fail or hf
        semihard_fail = semihard_fail or sf

    print("", file=err)

    print("  --- manifest dep probes ---", file=err)
    dep_count = 0
    if manifest_path is not None:
        dep_rows = dep_probe_all(manifest_path, repo_root, err=err, argv=argv)
        dep_count = len(dep_rows)
        for row in dep_rows:
            hf, sf = pf_emit_row(row["id"], row["status"], row["severity"], row["hint"], out=out, err=err)
            hard_fail = hard_fail or hf
            semihard_fail = semihard_fail or sf

    if dep_count == 0:
        print("  (no manifest deps — coordinator-claude is DAG root)", file=err)
    print("", file=err)

    if hard_fail:
        print(f"  {_CHAIN_BANNER}: PREREQ GATE FAILED (hard probe failure — see [FAIL] rows above).", file=err)
        raise _SetupError(1)
    if semihard_fail and not accept_no_git_auth:
        print(f"  {_CHAIN_BANNER}: PREREQ GATE BLOCKED (semi-hard probe unverified — see [BLOCK] rows above).", file=err)
        print("  Suppress with: --accept-no-git-auth (operator override; audited to stderr).", file=err)
        raise _SetupError(94)

    return dep_count


# ---------------------------------------------------------------------------
# Phase-0 consent gate (dual-mode UX; DAG root has no hard deps so the banner
# path is structurally unreachable in production, kept for schema parity).
# ---------------------------------------------------------------------------

def phase_zero_should_run(flags: dict[str, bool]) -> bool:
    read_only = (
        flags.get("help", False)
        or flags.get("version", False)
        or flags.get("phase_list", False)
        or flags.get("last_status", False)
        or flags.get("i_am_agent", False)
        or flags.get("check", False)
    )
    return not read_only


def run_mode_prompt(flags: dict[str, bool], non_interactive: bool, err=None) -> None:
    err = sys.stderr if err is None else err
    if flags.get("i_am_agent", False) or os.environ.get("COORDINATOR_RUN_MODE", "") == "agent":
        print("AGENT_MANIFEST_PATH=docs/install/AGENT.md", file=err)
        print("[setup] Agent-direct invocation detected. Use /coordinator:setup instead.", file=err)
        print("[setup] Agent install guide: docs/install/AGENT.md", file=err)
        raise _SetupError(92)

    if os.environ.get("COORDINATOR_RUN_MODE", "") == "human":
        return

    if non_interactive or not sys.stdin.isatty():
        return

    print("[setup] Are you running this script as an autonomous agent rather than via /coordinator:setup? [y/N] ", end="", file=err)
    try:
        reply = input()
    except EOFError:
        reply = ""
    if reply in ("y", "Y"):
        print("", file=err)
        print("AGENT_MANIFEST_PATH=docs/install/AGENT.md", file=err)
        print("[setup] Agent-direct invocation confirmed. Use /coordinator:setup instead.", file=err)
        print("[setup] Agent install guide: docs/install/AGENT.md", file=err)
        raise _SetupError(92)


def consent_gate(
    skip_dep_check: bool,
    accept_missing_deps_risk: bool,
    non_interactive: bool,
    manifest_path: Path | None,
    repo_root: Path,
    lib_dir: Path,
    err=None,
    argv: list[str] | None = None,
) -> None:
    err = sys.stderr if err is None else err
    if skip_dep_check and not accept_missing_deps_risk:
        print("ERROR: --skip-dep-check requires --accept-missing-deps-risk (exit 93: override-flag-pair-incomplete)", file=err)
        print("  Pass both flags together: --skip-dep-check --accept-missing-deps-risk", file=err)
        raise _SetupError(93)
    if accept_missing_deps_risk and not skip_dep_check:
        print("ERROR: --accept-missing-deps-risk requires --skip-dep-check (exit 93: override-flag-pair-incomplete)", file=err)
        print("  Pass both flags together: --skip-dep-check --accept-missing-deps-risk", file=err)
        raise _SetupError(93)

    if manifest_path is None:
        return
    missing_hard: list[str] = []
    for row in dep_probe_all(manifest_path, repo_root, err=err, argv=argv):
        if row["severity"] == "hard" and row["status"] in ("missing", "present-but-broken"):
            missing_hard.append(f"{row['id']} [{row['status']}]")

    if not missing_hard:
        return

    banner_path = lib_dir / "dep_consent_banner.txt"
    if banner_path.is_file():
        banner_body = banner_path.read_text(encoding="utf-8")
    else:
        banner_body = (
            "================================================================\n"
            "WARNING — UNSAFE INSTALL REQUESTED\n"
            "Missing hard deps; see scripts/lib/dep_consent_banner.txt\n"
            "================================================================"
        )
    dep_list = "\n".join(f"  {entry}" for entry in missing_hard)
    banner_rendered = banner_body.replace("<!-- DEPS_LIST_PLACEHOLDER -->", dep_list)

    if skip_dep_check and accept_missing_deps_risk:
        print(banner_rendered, file=err)
        if not (non_interactive or not sys.stderr.isatty()):
            print("[setup] Override flags accepted: --skip-dep-check --accept-missing-deps-risk. Proceeding.", file=err)
        return

    print(banner_rendered, file=err)

    if non_interactive or not sys.stdin.isatty():
        print("", file=err)
        print("ERROR: Hard dependencies missing in non-interactive context (exit 90).", file=err)
        print("  Run interactively, or pass --skip-dep-check --accept-missing-deps-risk to override.", file=err)
        raise _SetupError(90)

    print("", file=err)
    print("Do you accept the risk of proceeding without the required dependencies? [y/N] ", end="", file=err)
    try:
        reply1 = input()
    except EOFError:
        reply1 = ""
    if reply1 not in ("y", "Y"):
        print("[setup] Aborted: first confirmation declined (exit 91).", file=err)
        raise _SetupError(91)

    print("Specifically, do you accept that running without required deps may produce contract-violation outcomes? [y/N] ", end="", file=err)
    try:
        reply2 = input()
    except EOFError:
        reply2 = ""
    if reply2 not in ("y", "Y"):
        print("[setup] Aborted: second confirmation declined (exit 91).", file=err)
        raise _SetupError(91)

    print("[setup] Both confirmations received. Proceeding with unsafe install.", file=err)


# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
Usage: python3 scripts/setup.sh [OPTIONS]

  --help                     Print this help and exit.
  --version                  Print script version and exit.
  --phase-list               List install phases and exit.
  --phase <name>             Run a named install phase and exit.
                             Read-only phases (no state written):
                               seed-install-spinoff  DAG-root no-op; prints that
                                                      coordinator STITCHES+DRIVES
                                                      and seeds no leg baton for itself.
                             Stateful phases (gated; write install-status at heavy legs):
                               chain-preinstall      pre-restart full-install seam;
                                                      requires $COORDINATOR_CHAIN_PREINSTALL_CONSENT (or the
                                                      override pair) in agent mode; no-op body at root.
                             Unknown phase names exit non-zero with a remediation message.
  --last-status              Print last install status JSON and exit.
  --check                    Read-only dep probe + status report. No state written.
                             coord-specific read-only extension (chain step 5 of 5).
  --preflight                Superset of --check: probes manifest deps AND machine
                             environment prerequisites (python, uv, pwsh, ue,
                             clone_auth, longpaths). Emits unified PASS/FAIL table
                             + NDJSON. No state written. Exits non-zero when a
                             hard probe fails, or when a semi-hard probe fails
                             without --accept-no-git-auth.
  --skip-dep-check           Skip dep-chain consent gate (pair with below).
  --accept-missing-deps-risk Accept risk of proceeding with dep absent.
                             Both flags required together; one alone exits 93.
  --accept-no-git-auth       Suppress semi-hard exit (94) from clone_auth probe.
                             Standalone flag — does not pair with dep-risk flags.
  --coordinator-root <path>  Override ladder rung 1 for the coordinator-claude
                             repo root to walk (rung 2: $COORDINATOR_CLAUDE_ROOT;
                             no registry rung -- repos.coordinator_claude is a
                             RETIRED key that names the publish mirror, not a
                             working checkout). Every candidate must clear two
                             gates: not a registered publish.mirrors.*.path
                             entry, and positive evidence of a real
                             coordinator-claude plugin source checkout. Only
                             consulted by the `python3 -m` entry point when
                             the trampoline's own env vars
                             (COORDINATOR_SETUP_REPO_ROOT/_LIB_DIR) are unset.

Exit codes: 0=ok  90=non-TTY/missing-dep  91=user-declined
            92=agent-direct  93=incomplete-override-pair
            94=semi-hard-git-auth-unverified (suppress with --accept-no-git-auth)

Severity levels (--preflight):
  hard      — exits non-zero unconditionally
  semi-hard — exits 94 unless --accept-no-git-auth supplied
  advisory  — WARN row; never fails exit code"""

_PHASE_LIST_TEXT = """\
Available --phase <name> values:
  seed-install-spinoff  DAG-root no-op (read-only, no state written)
  chain-preinstall      Pre-restart full-install seam (stateful-by-contract; gated by $COORDINATOR_CHAIN_PREINSTALL_CONSENT in agent mode; no-op body at the DAG root)

Informational (NOT --phase <name> values):
  dep-check:  coordinator-claude probes its one hard dep (claude-klabauter); chain-walk terminates here"""


def parse_args(argv: list[str], out=None, err=None) -> dict[str, Any]:
    """Parses argv exactly like the bash while/case loop. Raises _SetupError
    for any exit the oracle would take mid-parse (help/version/phase-list/
    last-status/unknown-arg/malformed --phase)."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    flags: dict[str, Any] = {
        "skip_dep_check": False,
        "accept_missing_deps_risk": False,
        "accept_no_git_auth": False,
        "check": False,
        "preflight": False,
        "help": False,
        "version": False,
        "phase_list": False,
        "last_status": False,
        "i_am_agent": False,
        "run_chain_preinstall": False,
        "coordinator_root": "",
    }

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            flags["help"] = True
            print(_HELP_TEXT, file=out)
            raise _SetupError(0)
        elif arg == "--version":
            flags["version"] = True
            print(f"{_SCRIPT_NAME} version {_SCRIPT_VERSION}", file=out)
            raise _SetupError(0)
        elif arg == "--phase-list":
            flags["phase_list"] = True
            print(_PHASE_LIST_TEXT, file=out)
            raise _SetupError(0)
        elif arg == "--phase":
            if i + 1 >= len(argv):
                print("ERROR: --phase requires a phase name argument.", file=err)
                print("Run with --phase-list to see available phases.", file=err)
                print("Run with --help for full usage.", file=err)
                raise _SetupError(1)
            phase_name = argv[i + 1]
            i += 1
            if phase_name.startswith("--"):
                print(f"ERROR: --phase requires a phase name, but got a flag ('{phase_name}'). Did you forget the phase name?", file=err)
                raise _SetupError(1)
            if phase_name == "seed-install-spinoff":
                print(
                    "coordinator-claude: DAG-root spine — STITCHES+DRIVES the install chain; "
                    "seeds no leg baton for itself (seed-install-spinoff is a no-op by design).",
                    file=out,
                )
                raise _SetupError(0)
            elif phase_name == "chain-preinstall":
                flags["run_chain_preinstall"] = True
            else:
                print(f"ERROR: Unknown --phase value: '{phase_name}'", file=err)
                print("Run with --phase-list to see available phase names.", file=err)
                print("Run with --help for full usage.", file=err)
                raise _SetupError(1)
        elif arg == "--last-status":
            flags["last_status"] = True
            print('{"overall": "no-prior-install"}', file=out)
            raise _SetupError(0)
        elif arg == "--check":
            flags["check"] = True
        elif arg == "--preflight":
            flags["preflight"] = True
        elif arg == "--skip-dep-check":
            flags["skip_dep_check"] = True
        elif arg == "--accept-missing-deps-risk":
            flags["accept_missing_deps_risk"] = True
        elif arg == "--accept-no-git-auth":
            flags["accept_no_git_auth"] = True
        elif arg == "--i-am-agent":
            flags["i_am_agent"] = True
        elif arg == "--coordinator-root":
            if i + 1 >= len(argv):
                print("ERROR: --coordinator-root requires a path argument.", file=err)
                raise _SetupError(1)
            flags["coordinator_root"] = argv[i + 1]
            i += 1
        elif arg.startswith("--coordinator-root="):
            flags["coordinator_root"] = arg[len("--coordinator-root=") :]
        else:
            print(f"ERROR: Unknown argument: {arg}", file=err)
            print("Run with --help for usage.", file=err)
            raise _SetupError(1)
        i += 1

    return flags


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

_COORDINATOR_ROOT_LADDER_REMEDIATION = (
    "  1. --coordinator-root <path>\n"
    "  2. $COORDINATOR_CLAUDE_ROOT\n"
    "  3. engine.working_repos.doe_claude registry key\n"
    "  (repos.coordinator_claude stays RETIRED -- it names the publish-mirror\n"
    "  location, never a working checkout; see publish.mirrors.<name>.path in\n"
    "  the machine-local registry for the mirror this repo actually resolves\n"
    "  that key to. `engine.working_repos.doe_claude` is written by DoE's own\n"
    "  installer / self-heal, never by claude-klabauter -- claude-klabauter only reads this\n"
    "  namespace. Absent: `machine-local set engine.working_repos.doe_claude\n"
    "  <path>`.)\n"
    "  Every rung additionally requires the candidate to (a) NOT be a\n"
    "  registered publish.mirrors.*.path entry, and (b) show positive\n"
    "  evidence of a real coordinator-claude plugin source checkout\n"
    "  (.claude-plugin/plugin.json plus commands/ or hooks/)."
)


def _is_publish_mirror(path: Path) -> bool:
    """True iff `path` resolves under (or equals) any registered
    `publish.mirrors.*.path` entry in the machine-local registry.

    Reuses the SAME mirror-identification mechanism the cross-repo write
    guard already uses to recognize `X:\\coordinator-claude` as a publish
    mirror (`coordinator_core.bash_guards._write_bump_applicability.
    target_is_publish_destination`) rather than inventing a second one here
    -- see that module's own docstring for the closed-set membership test
    (`publish.mirrors.<name>.path`, never a path-pattern guess).

    Fails open (`False`) when the registry has no `publish.mirrors.*`
    entries at all -- the OSS case, where this predicate must be a pure
    no-op and never a new failure mode -- or when the check itself raises
    for any reason (mirrors `target_is_publish_destination`'s own fail-open
    contract one level up)."""
    try:
        from coordinator_core.bash_guards._write_bump_applicability import (
            target_is_publish_destination,
        )
    except ImportError:
        return False
    try:
        return target_is_publish_destination(str(path))
    except Exception:
        return False


def _looks_like_coordinator_claude_source(path: Path) -> bool:
    """Positive evidence `path` is an actual coordinator-claude plugin
    source checkout, not a bare directory: a plugin manifest
    (`.claude-plugin/plugin.json`) AND at least one of `commands/`,
    `hooks/` present. Required before any candidate is accepted (Defect
    fix 2026-08-07, requirement 2) -- a directory that merely exists is not
    proof it is coordinator-claude."""
    if not (path / ".claude-plugin" / "plugin.json").is_file():
        return False
    return (path / "commands").is_dir() or (path / "hooks").is_dir()


def _resolve_coordinator_root_ladder(
    argv: list[str] | None = None, err=None
) -> tuple[Path, str] | None:
    """Resolve the coordinator-claude repo root to walk — the DAG-ROOT repo
    this module probes deps FOR, never assumed to be colocated with the
    claude-klabauter engine (Defect B), and never a registered publish mirror
    (Defect fix 2026-08-07). Ladder, in priority order:

        1. --coordinator-root <path> / --coordinator-root=<path> (this argv)
        2. $COORDINATOR_CLAUDE_ROOT
        3. `engine.working_repos.doe_claude` registry key (via
           `coordinator_core.machine_resolver.registry_get` — never a
           `machine-local` subprocess), passed through C1a's shape
           derivation (`coordinator_core.coordinator_root.
           _resolve_plugin_root_for_machine_local`) before being offered as
           a candidate.

    The machine-local registry rung previously read `repos.coordinator_claude`
    — that key is RETIRED (2026-06-30 registry-publish-vs-working-targets
    migration) and, on operator machines that still carry it, resolves
    straight to the publish mirror location, which `_is_publish_mirror`
    exists to reject; it is never repointed. `engine.working_repos.doe_claude`
    is a DIFFERENT, live-registered key that DOES name a coordinator-claude
    WORKING checkout generically on this fleet (verified live: the raw
    registered value is a repo root one directory ABOVE the actual plugin
    source, which is why the C1a shape derivation is applied before the
    value is offered as a candidate — the raw value alone carries no
    `.claude-plugin/plugin.json`, no `commands/`, no `hooks/`, and would
    never pass gate 2 below). Absent key, empty value, or a derivation that
    returns `None` is a rung MISS, not an error — fail-open, so an OSS
    single-tree box with no such key behaves byte-identically to today.
    This rung is written by DoE's own installer / self-heal via
    `machine-local set engine.working_repos.doe_claude <path>` — claude-klabauter only
    reads this namespace, never writes another repo's registry entry.

    Every candidate from any rung must clear TWO gates, in order, before
    it is accepted:
        1. `_is_publish_mirror` — a registered publish mirror is a generated
           downstream copy, never a walkable source checkout.
        2. `_looks_like_coordinator_claude_source` — positive evidence of an
           actual coordinator-claude plugin source checkout.
    A candidate that fails either gate is reported (to `err`) and rejected
    — the ladder does NOT silently fall through to a lower-priority
    candidate that failed a gate; only the first candidate that clears both
    gates is returned.

    Returns `(candidate, rung_label)` — `rung_label` names which rung
    resolved it, printed by every `--check`/`--preflight`/full-install
    banner per requirement 5 ("every resolution PASS must print the
    resolved path AND the rung that produced it"). Returns `None` when no
    rung resolves to a candidate that clears both gates — the caller (
    `_self_resolve_walker_roots` / `main`) reports a real diagnostic with
    the ladder as remediation instead of falling back to a location-derived
    guess (the guess was the root cause of Defect B: it coincidentally
    matched `<claude-klabauter-root>/coordinator`, claude-klabauter's OWN plugin-source mirror,
    not any real coordinator-claude checkout).
    """
    err = sys.stderr if err is None else err
    effective_argv = sys.argv[1:] if argv is None else argv

    candidates: list[tuple[Path, str]] = []
    i = 0
    while i < len(effective_argv):
        tok = effective_argv[i]
        if tok == "--coordinator-root" and i + 1 < len(effective_argv):
            candidates.append((Path(effective_argv[i + 1]), "--coordinator-root flag"))
            break
        if tok.startswith("--coordinator-root="):
            candidates.append((Path(tok[len("--coordinator-root=") :]), "--coordinator-root flag"))
            break
        i += 1

    env_val = os.environ.get("COORDINATOR_CLAUDE_ROOT", "")
    if env_val:
        candidates.append((Path(env_val), "$COORDINATOR_CLAUDE_ROOT env"))

    registry_val = registry_get("engine.working_repos.doe_claude")
    if registry_val:
        derived = _resolve_plugin_root_for_machine_local(Path(registry_val))
        if derived is not None:
            candidates.append((derived, "engine.working_repos.doe_claude registry key"))

    for candidate, rung in candidates:
        if _is_publish_mirror(candidate):
            print(
                f"  [coordinator-root ladder] {rung}: {candidate} is a registered "
                "publish mirror (publish.mirrors.*.path) -- rejected, not a working checkout.",
                file=err,
            )
            continue
        if not _looks_like_coordinator_claude_source(candidate):
            print(
                f"  [coordinator-root ladder] {rung}: {candidate} does not look like a "
                "coordinator-claude plugin source checkout (missing .claude-plugin/plugin.json "
                "plus commands/ or hooks/) -- rejected.",
                file=err,
            )
            continue
        return candidate, rung

    return None


def _self_resolve_walker_roots(
    argv: list[str] | None = None, err=None
) -> tuple[Path, Path, str] | None:
    """Fallback resolution of (repo_root, lib_dir, rung) for the `python3 -m
    coordinator_core.ops.setup_chain_walker` entry point, used only when the
    trampoline's env vars are absent.

    A module path is an interface and a file path is an implementation
    detail — consumers pinned to `coordinator/scripts/setup.py` cannot stop
    pinning it until the module form actually runs, so `__main__` has to
    resolve both roots itself. Defect B (2026-08-07): the root MUST come from
    `_resolve_coordinator_root_ladder` (an explicit override, never a guess
    derived from this module's own on-disk location) — see that function's
    docstring for the ladder and why the old location-derived guess was
    wrong. Returns None when the ladder resolves to nothing, or resolves to
    a path that is not an actual directory on disk, so the caller reports a
    real diagnostic instead of proceeding on an unverified root.

    Negative-spec: this never re-derives lib_dir when the env vars ARE set.
    The trampoline deliberately sets lib_dir to SCRIPT_DIR/lib rather than
    repo_root/scripts/lib, because the shadow-copy fixtures in
    `coordinator/tests/preflight/` invoke a copy of it from a directory not
    literally named "scripts" — env precedence is what keeps those green.
    lib_dir is NOT required to exist here (the C11 de-bash cutover retired
    the `scripts/lib/prereq_probe.sh` bridge this module used to require —
    the only remaining lib_dir consumer, `dep_consent_banner.txt`, already
    tolerates absence with a built-in default banner).
    """
    resolved = _resolve_coordinator_root_ladder(argv, err=err)
    if resolved is None:
        return None
    coordinator_tree, rung = resolved
    if not coordinator_tree.is_dir():
        return None
    lib_dir = coordinator_tree / "scripts" / "lib"
    return coordinator_tree, lib_dir, rung


def main(argv: list[str], out=None, err=None) -> int:
    """CLI entrypoint. repo_root / lib_dir come from
    COORDINATOR_SETUP_REPO_ROOT / COORDINATOR_SETUP_LIB_DIR when set (the
    trampoline resolves both itself, since they are colocated with the
    script — lib_dir is SCRIPT_DIR/lib, NOT derived from repo_root, exactly
    like the bash oracle's _LIB_DIR="${SCRIPT_DIR}/lib"; re-deriving it from
    repo_root/scripts/lib would break any caller invoking the trampoline
    from a directory not literally named "scripts" — e.g. the shadow-copy
    test fixtures in coordinator/tests/preflight/).

    With neither set — the `python3 -m` entry point — both are self-resolved
    from this module's own location via `_self_resolve_walker_roots`."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    repo_root_env = os.environ.get("COORDINATOR_SETUP_REPO_ROOT", "")
    lib_dir_env = os.environ.get("COORDINATOR_SETUP_LIB_DIR", "")
    if repo_root_env and lib_dir_env:
        repo_root = Path(repo_root_env)
        lib_dir = Path(lib_dir_env)
        coordinator_root_rung = "trampoline env (COORDINATOR_SETUP_REPO_ROOT/COORDINATOR_SETUP_LIB_DIR)"
    else:
        resolved = _self_resolve_walker_roots(argv, err=err)
        if resolved is None:
            print(
                "ERROR: cannot resolve the chain-walker roots. Set "
                "COORDINATOR_SETUP_REPO_ROOT and COORDINATOR_SETUP_LIB_DIR, "
                "or resolve the coordinator-claude root to walk via one of, "
                "in priority order:",
                file=err,
            )
            print(_COORDINATOR_ROOT_LADDER_REMEDIATION, file=err)
            return 1
        repo_root, lib_dir, coordinator_root_rung = resolved

    try:
        flags = parse_args(list(argv), out=out, err=err)
    except _SetupError as e:
        return e.code

    non_interactive = os.environ.get("NON_INTERACTIVE", "true").lower() == "true" or not sys.stdin.isatty()

    try:
        # Override-flag pair integrity check (install runs only; --check/--preflight are read-only).
        if not flags["check"] and not flags["preflight"]:
            if flags["skip_dep_check"] and not flags["accept_missing_deps_risk"]:
                print("ERROR: --skip-dep-check requires --accept-missing-deps-risk (both flags required together).", file=err)
                raise _SetupError(93)
            if flags["accept_missing_deps_risk"] and not flags["skip_dep_check"]:
                print("ERROR: --accept-missing-deps-risk requires --skip-dep-check (both flags required together).", file=err)
                raise _SetupError(93)

        # Agent-direct short-circuit.
        if flags["i_am_agent"] or os.environ.get("COORDINATOR_RUN_MODE", "") == "agent":
            override_pair = flags["skip_dep_check"] and flags["accept_missing_deps_risk"]
            chain_preinstall_consented = flags["run_chain_preinstall"] and bool(
                os.environ.get("COORDINATOR_CHAIN_PREINSTALL_CONSENT", "")
            )
            if not (override_pair or chain_preinstall_consented):
                print("AGENT_MANIFEST_PATH=docs/install/AGENT.md", file=err)
                print("[setup] Agent-direct invocation detected.", file=err)
                print("[setup]   To BOOTSTRAP A MACHINE (machine-local registry, venv, substrate): run /coordinator:install (Phase 3).", file=err)
                print("[setup]   To WALK THE INSTALL CHAIN only (DAG walk, bootstraps nothing): run /coordinator:setup.", file=err)
                print("[setup] This script (setup.sh) is the chain-walker and installs no machine substrate itself.", file=err)
                print("[setup] Agent install guide: docs/install/AGENT.md", file=err)
                if flags["run_chain_preinstall"]:
                    print("[setup] --phase chain-preinstall requires a consented chain walk:", file=err)
                    print("[setup]   set $COORDINATOR_CHAIN_PREINSTALL_CONSENT (the chain-walk token) — or supply the override pair", file=err)
                    print("[setup]   --i-am-agent --skip-dep-check --accept-missing-deps-risk", file=err)
                else:
                    print("[setup] To run non-interactively, supply both:", file=err)
                    print("[setup]   --i-am-agent --skip-dep-check --accept-missing-deps-risk", file=err)
                raise _SetupError(92)

        # chain-preinstall phase body (post-gate; DAG-root no-op).
        if flags["run_chain_preinstall"]:
            print(
                "coordinator-claude: DAG-root spine — nothing to preinstall (chain-preinstall no-op body "
                "at the root; capability install happens at downstream heavy-install legs).",
                file=out,
            )
            raise _SetupError(0)

        # --check mode: read-only dep probe.
        if flags["check"]:
            manifest_path = resolve_manifest_path(repo_root, err=err)
            manifest_repo_id = _loaded_manifest_repo_id(manifest_path)

            print("==========================================================", file=out)
            print(f"  {_CHAIN_BANNER}", file=out)
            print("==========================================================", file=out)
            print(f"  repo:         {manifest_repo_id}", file=out)
            print(f"  repo_root:    {repo_root}", file=out)
            print(f"  root rung:    {coordinator_root_rung}", file=out)
            print(f"  manifest:     {manifest_path if manifest_path is not None else '(not found)'}", file=out)
            print("  mode:         --check (read-only, no state written)", file=out)
            print("", file=out)

            if manifest_path is None:
                print("", file=err)
                print(f"  {_CHAIN_BANNER}: no manifest to probe — exiting 0 (check-only mode).", file=out)
                raise _SetupError(0)

            try:
                dep_rows = dep_probe_all(manifest_path, repo_root, err=err, argv=argv)
            except _SetupError:
                raise

            all_satisfied = True
            for row in dep_rows:
                dep_id, severity, status = row["id"], row["severity"], row["status"]
                if status == "present":
                    print(f"  [{dep_id}] dep satisfied — present ✓", file=out)
                elif status == "present-but-broken":
                    print(f"  [{dep_id}] dep found but functional probe failed", file=err)
                    if severity == "soft":
                        print(f"  WARNING: soft dep {dep_id} present-but-broken — continuing (warn-and-continue).", file=err)
                        print("    Override: re-run with --skip-dep-check --accept-missing-deps-risk to suppress.", file=err)
                        all_satisfied = False
                    else:
                        print(f"ERROR: hard dep [{dep_id}] is present but its functional probe failed.", file=err)
                        raise _SetupError(1)
                elif status == "missing":
                    if severity == "soft":
                        print("", file=err)
                        print(f"  WARNING: soft dep [{dep_id}] is absent (missing).", file=err)
                        print("  To suppress this warning and accept the missing dep:", file=err)
                        print("    python3 scripts/setup.sh --skip-dep-check --accept-missing-deps-risk", file=err)
                        print("", file=err)
                        all_satisfied = False
                    else:
                        print(f"ERROR: hard dep [{dep_id}] is missing.", file=err)
                        raise _SetupError(90)

            print("", file=out)
            if len(dep_rows) == 0:
                print("chain walk complete — coordinator-claude is DAG root", file=out)
            elif all_satisfied:
                print(f"  {_CHAIN_BANNER}: all deps satisfied.", file=out)
            else:
                print(f"  {_CHAIN_BANNER}: soft dep(s) absent or present-but-broken — proceeding (soft dep warn-and-continue).", file=out)
            print("==========================================================", file=out)
            raise _SetupError(0)

        # --preflight mode.
        if flags["preflight"]:
            manifest_path = resolve_manifest_path(repo_root, err=err)
            manifest_repo_id = _loaded_manifest_repo_id(manifest_path)

            print("==========================================================", file=err)
            print(f"  {_CHAIN_BANNER}", file=err)
            print("==========================================================", file=err)
            print(f"  repo:         {manifest_repo_id}", file=err)
            print(f"  repo_root:    {repo_root}", file=err)
            print(f"  root rung:    {coordinator_root_rung}", file=err)
            print(f"  manifest:     {manifest_path if manifest_path is not None else '(not found)'}", file=err)
            print("  mode:         --preflight (read-only; dep probes + env prereq probes)", file=err)
            print("", file=err)

            if manifest_path is None:
                print("", file=err)
                print(f"  {_CHAIN_BANNER}: no manifest found — skipping dep probes.", file=err)

            run_prereq_gate(
                "strict", lib_dir, manifest_path, repo_root, flags["accept_no_git_auth"], out=out, err=err, argv=argv
            )

            print(f"  {_CHAIN_BANNER}: preflight complete (no hard or unaccepted semi-hard failures).", file=err)
            print("==========================================================", file=err)
            raise _SetupError(0)

        # Full install body (non-check mode) — DAG root.
        print("==========================================================", file=out)
        print(f"  {_CHAIN_BANNER}", file=out)
        print("==========================================================", file=out)
        print("  repo:      coordinator-claude", file=out)
        print(f"  repo_root: {repo_root}", file=out)
        print(f"  root rung: {coordinator_root_rung}", file=out)
        print("", file=out)

        phase_zero_flags = {
            "help": flags["help"],
            "version": flags["version"],
            "phase_list": flags["phase_list"],
            "last_status": flags["last_status"],
            "i_am_agent": flags["i_am_agent"],
            "check": flags["check"],
        }
        # Review: code-reviewer (Finding 1, 2026-07-17) — an i_am_agent-True
        # invocation reaching here has already cleared the agent-direct
        # short-circuit's override_pair/chain_preinstall_consented gate
        # above; re-running run_mode_prompt's own unconditional i_am_agent
        # check re-raised exit 92 with no way to clear it, making the
        # documented non-interactive full-install path unreachable. Only
        # invoke run_mode_prompt for the interactive "are you an agent?"
        # TTY prompt, not to re-vet an already-admitted agent invocation.
        if not flags["i_am_agent"] and os.environ.get("COORDINATOR_RUN_MODE", "") != "agent":
            run_mode_prompt(phase_zero_flags, non_interactive, err=err)
        if phase_zero_should_run(phase_zero_flags):
            manifest_path_for_consent = resolve_manifest_path(repo_root, err=err)
            consent_gate(
                flags["skip_dep_check"],
                flags["accept_missing_deps_risk"],
                non_interactive,
                manifest_path_for_consent,
                repo_root,
                lib_dir,
                err=err,
                argv=argv,
            )

        manifest_path = resolve_manifest_path(repo_root, err=err)
        if manifest_path is None:
            print("[setup] WARNING: proceeding without dep probe — coordinator-claude is the DAG root (no direct_deps).", file=err)

        print("[setup] Running prereq gate (post-consumer mode)...", file=out)
        dep_count = run_prereq_gate(
            "post-consumer", lib_dir, manifest_path, repo_root, flags["accept_no_git_auth"], out=out, err=err, argv=argv
        )

        print("", file=out)
        if dep_count == 0:
            print("chain walk complete — coordinator-claude is DAG root", file=out)
        else:
            print(f"[setup] {_CHAIN_BANNER}: complete.", file=out)
        print("  coordinator-claude has no Python/binary install phases.", file=out)
        print("  Use /coordinator:setup to run the full chain-walker via the skill.", file=out)
        print("==========================================================", file=out)
        raise _SetupError(0)

    except _SetupError as e:
        return e.code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

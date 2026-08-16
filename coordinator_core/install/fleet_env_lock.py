"""
coordinator_core.install.fleet_env_lock — assembles the fleet shared
environment's dependency-input file (``docs/install/fleet-env-requirements.in``)
from a declared list of contributing manifests
(``docs/install/fleet-env-sources.toml``).

Purpose: this is the ONLY generator for ``fleet-env-requirements.in``.
Removing a repo from the fleet union is exactly: delete its row(s) from
``fleet-env-sources.toml``, then re-run
``python3 -m coordinator_core.install.fleet_env_lock`` — never a hand-edit
of the ``.in`` file (docs/plans/2026-08-16-one-environment-for-the-fleet.md
§ C2, the PM directive that the union must be cheap to shrink).

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md § C2
Contract: docs/reference/fleet-shared-environment-contract.md
Source data: state/audits/2026-08-16-fleet-venv-survey.md

Sibling repo roots resolve exclusively through
``coordinator_core.machine_resolver.registry_get("repos.<repo>")`` — the
direct-tomllib machine-local registry reader (no subprocess). No hardcoded
drive letter or Dev-Drive assumption appears anywhere in this module.

Does NOT resolve dependencies — no ``uv`` invocation, no ``pip``. This
module assembles uv's INPUT only; C3 runs ``uv lock`` over the emitted
``.in`` file.

Does NOT fold ``fleet-env-overrides.toml`` into the emitted ``.in`` file —
overrides are a resolution-time uv instrument (C3's concern), not an
input-assembly one. This module only certifies the overrides file's shape
(every row carries ``owner`` and ``expires``) as a shared precondition
before writing, per AC3 — the test asserting that shape lives elsewhere,
per the C2 dispatch brief.

Negative-spec:
    - Does NOT write to any sibling repo tree — read-only manifest parsing.
    - Does NOT hand-author ``fleet-env-requirements.in`` — regenerate it
      via ``run()``, never edit it directly.
    - Does NOT hand-author ``fleet-env.lock`` — regenerate it via
      ``generate_lock()`` (``--emit-lock`` on the CLI), never edit it
      directly.

C3 extension (docs/plans/2026-08-16-one-environment-for-the-fleet.md § C3):
``generate_lock()`` is the ONLY generator for ``docs/install/fleet-env.lock``.
It assembles a synthetic, never-installed ``pyproject.toml`` from the
already-generated requirements input plus ``fleet-env-overrides.toml``, and
shells out to ``uv lock`` (universal by construction — never ``uv pip
compile`` without ``--universal``) over it in an isolated temp project
directory. ``uv`` is spawned directly via an argv list (``shell=False``),
never through a shell — this repo's shell-out-carve-outs doc
(``docs/reference/shell-out-carve-outs.md``) governs shell-INTERPRETER
spawns (bash/sh/pwsh/zsh) only, per its own "may Python spawn a shell at
this call-site" framing; ``uv`` is an ordinary CLI tool in the same class as
``git``, already spawned elsewhere in this repo (e.g.
``coordinator_core/install/prereq_probe.py::probe_uv``) with no carve-out
entry required.

The GPU stack resolves through the sanctioned explicit-index pattern only —
``[[tool.uv.index]] explicit = true`` paired with ``[tool.uv.sources]`` —
never ``--index-strategy unsafe-best-match`` or ``--extra-index-url``. The
darwin exclusion marker on ``torch``/``torchvision`` is mandatory, not
discretionary (contract doc § DECISIONS (a)).

A genuine ``uv lock`` resolution failure is surfaced as
``FleetEnvLockResolutionError`` and MUST NOT be silently patched by adding a
new row to ``fleet-env-overrides.toml`` — that file is out of this chunk's
scope, and an override is resolution-time scaffolding, never a fix (see
``docs/reference/fleet-shared-environment-contract.md``).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

from coordinator_core.machine_resolver import registry_get
from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCES_PATH = _REPO_ROOT / "docs" / "install" / "fleet-env-sources.toml"
_REQUIREMENTS_IN_PATH = _REPO_ROOT / "docs" / "install" / "fleet-env-requirements.in"
_OVERRIDES_PATH = _REPO_ROOT / "docs" / "install" / "fleet-env-overrides.toml"
_LOCK_PATH = _REPO_ROOT / "docs" / "install" / "fleet-env.lock"

# Contract doc § DECISIONS "The Python minor" — pinned explicitly, never
# inherited from the authoring machine's own interpreter.
LOCK_PYTHON_MINOR = "3.12"

# Contract doc § DECISIONS (a) "Platform contract, decided" — win32, linux,
# and darwin are all live fleet targets; `uv lock`'s `environments` setting
# is what makes universal resolution actually cover all three rather than
# whatever platform authored the lock.
LOCK_ENVIRONMENTS: Tuple[str, ...] = (
    "sys_platform == 'win32'",
    "sys_platform == 'linux'",
    "sys_platform == 'darwin'",
)

# The sanctioned explicit-index GPU pattern (plan C3 body; verified live on
# disk in example-retrieval-repo/pyproject.toml and example-game-workbench-repo/pyproject.toml).
# `unsafe-best-match` / `--extra-index-url` are forbidden substitutes (AC11).
_CU130_INDEX_NAME = "pytorch-cu130"
_CU130_INDEX_URL = "https://download.pytorch.org/whl/cu130"
_CU130_SOURCED_PACKAGES: Tuple[str, ...] = ("torch", "torchvision")
_DARWIN_EXCLUSION_MARKER = "sys_platform != 'darwin'"

# Generous timeout: resolving ~250 packages across three platforms with a
# cold uv cache on a 50-70-session machine is a slow op, not a hung one
# (CLAUDE.md § Load norm) — this is a cold-path, operator/CI-invoked
# generator, never a request-path call.
_UV_LOCK_TIMEOUT_SECS = 1800

# PM ruling, 2026-08-16 (state/audits/2026-08-16-fleet-venv-survey.md):
# "go for the better (more modern) huggingface" — a first-class floor in the
# union's input, never an override-file escape hatch. See
# docs/reference/fleet-shared-environment-contract.md for why the
# distinction is load-bearing (a standing override is scaffolding with an
# expiry; a floor is a permanent requirement).
FIRST_CLASS_FLOORS: Tuple[str, ...] = ("huggingface_hub>=1.0",)

# First-party sibling packages whose PyPI-style name does NOT equal a
# contributing repo key turned dashes (the case `_first_party_sibling_names`
# already covers). Enumerated, not heuristic — a substring/prefix guess
# would risk matching an unrelated third-party name. EM ruling, C2 defect
# fix (docs/plans/2026-08-16-one-environment-for-the-fleet.md § C2):
# `example-retrieval-repo-symbol-extract` lives under example-retrieval-repo/packaging/symbol-
# extract — first-party sibling wiring, C6's binding-registry concern, same
# ground the repo-key rule already excludes third-party-union membership
# on. Excluded here specifically because claude_klabauter's own `[symbols]`
# extra pins it as a `git+ssh://` source dependency: folding a git+ssh spec
# into the shared lock would make every fleet install require ssh
# credentials to resolve it, a cost paid fleet-wide for one repo's optional
# extra.
_FIRST_PARTY_EXTRA_NAMES: Tuple[str, ...] = ("example-retrieval-repo-symbol-extract",)

_SUPPORTED_MANIFEST_NAMES = ("pyproject.toml", "requirements.txt")

_SPEC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+")


class FleetEnvLockError(RuntimeError):
    """Raised for any condition that must stop generation rather than emit
    a silently-incomplete requirements input: an unresolvable repo, a
    missing/unsupported manifest, or a malformed sources/overrides file."""


class FleetEnvLockResolutionError(FleetEnvLockError):
    """Raised when ``uv lock`` itself fails to resolve the fleet union — a
    genuine declared-range conflict. Never caught-and-patched by adding a
    new ``fleet-env-overrides.toml`` row from this module; that file is
    C2's, out of C3's scope, and an override expands the acceptable set
    rather than fixing anything (see
    docs/reference/fleet-shared-environment-contract.md)."""


def _load_sources(sources_path: Path) -> List[Dict[str, str]]:
    if not sources_path.is_file():
        raise FleetEnvLockError(f"fleet_env_lock: sources file not found: {sources_path}")
    with open(sources_path, "rb") as fh:
        data = tomllib.load(fh)
    rows = data.get("source", [])
    if not isinstance(rows, list) or not rows:
        raise FleetEnvLockError(
            f"fleet_env_lock: {sources_path} declares no [[source]] rows"
        )
    for row in rows:
        if "repo" not in row or "manifest" not in row:
            raise FleetEnvLockError(
                f"fleet_env_lock: malformed source row (needs repo + manifest): {row!r}"
            )
    return rows


def _resolve_repo_root(repo_key: str) -> str:
    root = registry_get(f"repos.{repo_key}")
    if not root:
        raise FleetEnvLockError(
            f"fleet_env_lock: repos.{repo_key} is not set in the machine-local "
            "registry — cannot resolve this contributing repo's root. "
            f"Set it: machine-local set repos.{repo_key} /path/to/repo"
        )
    return root


def _flatten_pyproject_deps(pyproject: dict) -> List[str]:
    """``[project.dependencies]`` plus every ``[project.optional-dependencies]``
    group, flattened. Extras must not be dropped — the fleet's one real
    conflict (example-retrieval-repo's ``chroma`` extra declaring ``chromadb``) lives
    in an optional-dependencies group, not the base list."""
    project = pyproject.get("project", {}) or {}
    specs: List[str] = list(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {}) or {}
    for group_specs in optional.values():
        specs.extend(group_specs or [])
    return specs


def _parse_pyproject(path: Path) -> List[str]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return _flatten_pyproject_deps(data)


def _parse_requirements_txt(path: Path) -> List[str]:
    specs: List[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            # -r/-e/--index-url/... directives are not a plain PEP 508
            # dependency spec this union can carry. The manifest itself is
            # one of the declared contributing sources, so skipping its
            # directives does not silently drop a package spec.
            continue
        specs.append(line)
    return specs


def _parse_manifest(repo_root: str, manifest_rel: str) -> List[str]:
    manifest_path = Path(repo_root) / manifest_rel
    if not manifest_path.is_file():
        raise FleetEnvLockError(
            f"fleet_env_lock: declared manifest not found on disk: {manifest_path}"
        )
    if manifest_path.name == "pyproject.toml":
        return _parse_pyproject(manifest_path)
    if manifest_path.suffix == ".txt":
        return _parse_requirements_txt(manifest_path)
    raise FleetEnvLockError(
        "fleet_env_lock: unsupported manifest type "
        f"(only {_SUPPORTED_MANIFEST_NAMES} are parsed): {manifest_path}"
    )


def _first_party_sibling_names(rows: Sequence[Dict[str, str]]) -> Set[str]:
    """PyPI-style names for the fleet's own contributing repos (registry
    key with underscores turned to dashes, e.g. ``example_retrieval_repo`` ->
    ``example-retrieval-repo``) — used to drop a manifest's bare self-reference to a
    sibling contributing repo (e.g. Example-retrieval-repo-ue-addon's undecorated
    ``example-retrieval-repo`` dependency line), the same "one first-party entry"
    the spike verdict (docs/research/spike-verdicts/2026-08-16-one-
    resolved-dependency-set-for-the-fleet.md) dropped before counting
    195 third-party specs. That sibling wiring is C6's binding-registry
    concern, not a third-party PyPI union member. Also folds in
    ``_FIRST_PARTY_EXTRA_NAMES`` — first-party packages whose name does not
    equal any contributing repo key turned dashes, enumerated explicitly
    rather than matched heuristically."""
    return {row["repo"].replace("_", "-") for row in rows} | set(_FIRST_PARTY_EXTRA_NAMES)


def _spec_name(spec: str) -> str:
    match = _SPEC_NAME_RE.match(spec.strip())
    return match.group(0).lower() if match else ""


def collect_specs(sources_path: Path = _SOURCES_PATH) -> List[Tuple[str, str]]:
    """Return (spec, provenance) pairs for every declared source row, where
    provenance is ``"<repo>:<manifest>"`` — carried into the emitted
    ``.in`` file as a per-line trailing comment so a reader can trace a
    pin back to the manifest that declared it. Drops any spec that is a
    bare self-reference to one of the fleet's own contributing repos (see
    ``_first_party_sibling_names``)."""
    rows = _load_sources(sources_path)
    first_party = _first_party_sibling_names(rows)
    pairs: List[Tuple[str, str]] = []
    for row in rows:
        repo_key = row["repo"]
        manifest_rel = row["manifest"]
        repo_root = _resolve_repo_root(repo_key)
        specs = _parse_manifest(repo_root, manifest_rel)
        provenance = f"{repo_key}:{manifest_rel}"
        for spec in specs:
            if _spec_name(spec) in first_party:
                continue
            pairs.append((spec, provenance))
    return pairs


def render_requirements_in(pairs: Sequence[Tuple[str, str]]) -> str:
    """Deterministic, sorted, one spec per line with its provenance comment
    — cheap to diff after a source row is added or removed. First-class
    floors (``FIRST_CLASS_FLOORS``) are appended last, under their own
    banner, distinct from manifest-derived specs."""
    lines = [
        "# GENERATED by coordinator_core.install.fleet_env_lock — do not hand-edit.",
        "# Regenerate: python3 -m coordinator_core.install.fleet_env_lock",
        "# Source list: docs/install/fleet-env-sources.toml",
        "#",
        "# Removing a repo from the fleet union: delete its row(s) from",
        "# fleet-env-sources.toml and re-run the command above. This file is",
        "# always a full regeneration, never a hand-applied diff.",
        "",
    ]
    for spec, provenance in sorted(set(pairs)):
        lines.append(f"{spec}  # {provenance}")
    lines.append("")
    lines.append(
        "# First-class floors (PM-ruled requirements, not overrides — see"
    )
    lines.append("# docs/reference/fleet-shared-environment-contract.md):")
    for floor in FIRST_CLASS_FLOORS:
        lines.append(floor)
    lines.append("")
    return "\n".join(lines)


def validate_overrides(overrides_path: Path = _OVERRIDES_PATH) -> None:
    """Fail loud if any override row lacks ``owner`` or ``expires``. This
    is the shared precondition the AC3 test also asserts — enforced here
    too so a bad overrides file cannot silently ride along with a
    regenerated requirements input."""
    if not overrides_path.is_file():
        return
    with open(overrides_path, "rb") as fh:
        data = tomllib.load(fh)
    for row in data.get("override", []):
        missing = [field for field in ("owner", "expires") if not row.get(field)]
        if missing:
            raise FleetEnvLockError(
                f"fleet_env_lock: override row missing {missing}: {row!r}"
            )


def run(
    sources_path: Path = _SOURCES_PATH,
    requirements_in_path: Path = _REQUIREMENTS_IN_PATH,
) -> Path:
    """Regenerate ``fleet-env-requirements.in`` from ``sources_path`` —
    always a full overwrite, never an append/merge. That full-overwrite
    shape is what makes removing a contributing repo a one-row-delete-plus-
    one-rerun operation rather than a hand-applied diff."""
    pairs = collect_specs(sources_path)
    content = render_requirements_in(pairs)
    requirements_in_path.write_text(content, encoding="utf-8")
    return requirements_in_path


def _strip_provenance_comment(line: str) -> str:
    """A rendered requirements-input line carries a trailing
    ``  # <repo>:<manifest>`` provenance comment (``render_requirements_in``)
    — strip it to recover the bare PEP 508 spec for the synthetic lock
    project's ``dependencies`` list."""
    idx = line.find("  #")
    return (line if idx == -1 else line[:idx]).strip()


def load_requirements_in_specs(requirements_in_path: Path = _REQUIREMENTS_IN_PATH) -> List[str]:
    """Read back every declared spec from an already-generated
    ``fleet-env-requirements.in`` (comments and first-class-floor banner
    lines stripped), in file order. Duplicate package names are expected
    and preserved as-is — the same shape ``uv lock``'s own dependency
    resolution already combines constraints across, per package, when
    building the synthetic project below."""
    if not requirements_in_path.is_file():
        raise FleetEnvLockError(
            f"fleet_env_lock: requirements input not found: {requirements_in_path} "
            "— run `python3 -m coordinator_core.install.fleet_env_lock` first "
            "to assemble it from fleet-env-sources.toml"
        )
    specs: List[str] = []
    for raw_line in requirements_in_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        spec = _strip_provenance_comment(raw_line)
        if spec:
            specs.append(spec)
    return specs


def load_override_dependency_specs(
    overrides_path: Path = _OVERRIDES_PATH,
) -> List[str]:
    """Translate ``fleet-env-overrides.toml`` rows, plus every
    ``FIRST_CLASS_FLOORS`` entry, into uv's own ``[tool.uv]
    override-dependencies`` resolution primitive.

    Two distinct things land here for one mechanical reason: uv's
    ``override-dependencies`` is the only way to make a forced spec win
    over a *conflicting* declared range anywhere else in the union
    (docs.astral.sh/uv/concepts/resolution/ — "a useful last resort ...
    despite the metadata indicating otherwise"). ``chromadb`` conflicts by
    declared-range disjunction (example-retrieval-repo's ``>=1.5,<1.6`` vs the addon's
    ``>=1.4,<1.5`` — the overrides file's one seeded row, owner + expiry
    per AC3). ``huggingface_hub``'s first-class floor (``>=1.0``) directly
    contradicts ``example_game_workbench_repo``'s declared
    ``huggingface_hub>=0.23,<1.0`` cap already present in the requirements
    input — without forcing it via this same uv primitive, `uv lock` cannot
    resolve at all. This does NOT reclassify the floor as override-file
    scaffolding (fleet_env_lock.py's own ``FIRST_CLASS_FLOORS`` docstring
    and the contract doc both still draw that line at the *ownership*
    layer) — it only says which uv resolution knob makes a forced spec win,
    which is orthogonal to who owns it and whether it expires.
    """
    validate_overrides(overrides_path)
    specs: List[str] = []
    if overrides_path.is_file():
        with open(overrides_path, "rb") as fh:
            data = tomllib.load(fh)
        for row in data.get("override", []):
            specs.append(f"{row['package']}{row['spec']}")
    specs.extend(FIRST_CLASS_FLOORS)
    return specs


def render_lock_pyproject(
    specs: Sequence[str],
    override_specs: Sequence[str],
) -> str:
    """Render a synthetic, never-installed ``pyproject.toml`` whose sole
    purpose is to give ``uv lock`` a project interface to resolve — the
    fleet union has no real ``[project]`` of its own. Carries the mandated
    mechanism shape verbatim from the plan/contract doc: pinned Python
    floor, ``environments`` fixed to win32+linux+darwin (universal
    resolution restricted to exactly the fleet's platform contract), the
    sanctioned explicit cu130 index with the mandatory darwin-exclusion
    marker, and ``override-dependencies`` for the union's declared-range
    conflicts (see ``load_override_dependency_specs``)."""
    dep_lines = "\n".join(f'    "{spec}",' for spec in specs)
    override_lines = "\n".join(f'    "{spec}",' for spec in override_specs)
    env_lines = "\n".join(f'    "{marker}",' for marker in LOCK_ENVIRONMENTS)
    sources_lines = "\n".join(
        f'{pkg} = {{ index = "{_CU130_INDEX_NAME}", marker = "{_DARWIN_EXCLUSION_MARKER}" }}'
        for pkg in _CU130_SOURCED_PACKAGES
    )
    return (
        "# GENERATED by coordinator_core.install.fleet_env_lock —"
        " do not hand-edit.\n"
        "# A synthetic uv project used solely to run `uv lock` over the"
        " fleet\n"
        "# union declared in fleet-env-requirements.in. Never installed,"
        " never\n"
        "# published — exists only inside generate_lock()'s temp project"
        " dir.\n"
        "[project]\n"
        'name = "fleet-env"\n'
        'version = "0.0.0"\n'
        f'requires-python = ">={LOCK_PYTHON_MINOR}"\n'
        "dependencies = [\n"
        f"{dep_lines}\n"
        "]\n"
        "\n"
        "[tool.uv]\n"
        "environments = [\n"
        f"{env_lines}\n"
        "]\n"
        "override-dependencies = [\n"
        f"{override_lines}\n"
        "]\n"
        "\n"
        "[[tool.uv.index]]\n"
        f'name = "{_CU130_INDEX_NAME}"\n'
        f'url = "{_CU130_INDEX_URL}"\n'
        "explicit = true\n"
        "\n"
        "[tool.uv.sources]\n"
        f"{sources_lines}\n"
    )


def generate_lock(
    requirements_in_path: Path = _REQUIREMENTS_IN_PATH,
    overrides_path: Path = _OVERRIDES_PATH,
    lock_path: Path = _LOCK_PATH,
    *,
    uv_executable: str = "uv",
) -> Dict[str, object]:
    """Run ``uv lock`` (always-universal project interface — never ``uv pip
    compile`` without ``--universal``) over the declared fleet union and
    write the result to ``lock_path``. The ONLY generator for
    ``docs/install/fleet-env.lock``; regenerate via this function (or its
    CLI wrapper, ``--emit-lock``), never hand-edit the lock file.

    Spawns ``uv`` directly via an argv list (``shell=False``,
    ``no_console_creationflags()`` on Windows) — never through a shell.
    ``docs/reference/shell-out-carve-outs.md`` governs shell-interpreter
    spawns only (see this module's own docstring); ``uv`` needs no entry
    there, same as this repo's existing unlisted ``git``/``uv`` spawns
    elsewhere (e.g. ``prereq_probe.py::probe_uv``).

    Raises ``FleetEnvLockResolutionError`` on a genuine ``uv lock``
    resolution failure — the caller must report it, never patch it by
    adding a new ``fleet-env-overrides.toml`` row from here (out of this
    module's write scope).
    """
    specs = load_requirements_in_specs(requirements_in_path)
    override_specs = load_override_dependency_specs(overrides_path)
    pyproject_text = render_lock_pyproject(specs, override_specs)

    with tempfile.TemporaryDirectory(prefix="fleet-env-lock-") as tmp_dir:
        project_dir = Path(tmp_dir)
        (project_dir / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
        argv = [
            uv_executable,
            "lock",
            "--project",
            str(project_dir),
            "--python",
            LOCK_PYTHON_MINOR,
        ]
        try:
            result = subprocess.run(
                argv,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                timeout=_UV_LOCK_TIMEOUT_SECS,
                **no_console_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FleetEnvLockError(
                f"fleet_env_lock: could not run `{uv_executable} lock`: {exc}"
            ) from exc

        if result.returncode != 0:
            raise FleetEnvLockResolutionError(
                "fleet_env_lock: `uv lock` failed to resolve the fleet union "
                "— a genuine declared-range conflict, not something to patch "
                "by adding a new fleet-env-overrides.toml row from this "
                "module (out of scope). stderr:\n" + result.stderr.strip()
            )

        generated_lock_path = project_dir / "uv.lock"
        if not generated_lock_path.is_file():
            raise FleetEnvLockError(
                "fleet_env_lock: `uv lock` exited 0 but produced no uv.lock "
                f"at {generated_lock_path}"
            )
        lock_text = generated_lock_path.read_text(encoding="utf-8")
        with open(generated_lock_path, "rb") as fh:
            lock_data = tomllib.load(fh)

    packages = lock_data.get("package", [])
    lock_path.write_text(lock_text, encoding="utf-8")
    return {
        "lock_path": lock_path,
        "package_count": len(packages),
        "packages": sorted(pkg.get("name", "") for pkg in packages),
        "argv": argv,
    }


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=_SOURCES_PATH)
    parser.add_argument("--out", type=Path, default=_REQUIREMENTS_IN_PATH)
    parser.add_argument("--overrides", type=Path, default=_OVERRIDES_PATH)
    parser.add_argument(
        "--emit-lock",
        action="store_true",
        help=(
            "After regenerating the requirements input, also run `uv lock` "
            "over it and write docs/install/fleet-env.lock. Default: only "
            "regenerate the requirements input (original C2 behavior)."
        ),
    )
    parser.add_argument("--lock-out", type=Path, default=_LOCK_PATH)
    args = parser.parse_args(list(argv))
    try:
        validate_overrides(args.overrides)
        out_path = run(sources_path=args.sources, requirements_in_path=args.out)
        lock_result: Dict[str, object] = {}
        if args.emit_lock:
            lock_result = generate_lock(
                requirements_in_path=out_path,
                overrides_path=args.overrides,
                lock_path=args.lock_out,
            )
    except FleetEnvLockError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"fleet_env_lock: wrote {out_path}")
    if args.emit_lock:
        print(
            f"fleet_env_lock: wrote {lock_result['lock_path']} "
            f"({lock_result['package_count']} packages)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

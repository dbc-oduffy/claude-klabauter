"""Heavy-command classifier for guard-heavy-command-admission.

classify(command, tool_input, cwd, vitest_worker_cap) -> Classification. Spawn-free and stdlib
plus the two existing guard modules: the UE guard's segment tokenizer decides the `ue` class, and
the DR-088 suite classifier (`check_test_suite_invocation`) decides `test_tier` and the scoped
carve-out. The suite classifier's configured-command containment leg is not consulted (it
resolves repo frontmatter); shape alone decides.

Shapes that never finish or fan out are heavy whatever their target: a watch flag, a vitest run
without `run`, and a package-manager script run across every workspace (`pnpm -r`). Any vitest
run is heavy, a single scoped file included, unless its workers are capped at or under
heavy_admission.vitest_max_workers by `--maxWorkers` or by the repo's vitest/vite config.

TRAP: a scoped run is not a light run. One scoped vitest file has held an 8.6 GB worker.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from coordinator_core.bash_guards import check_test_suite_invocation as _suite
from coordinator_core.bash_guards import guard_subagent_heavy_ue_launch as _ue
from coordinator_core.bash_guards._heavy_admission_contract import Classification, HeavyClass

#: A command containing none of these (lowercased) is light without tokenizing.
_PREFILTER = (
    "tsc", "typecheck", "build", "test", "jest", "mocha", "jasmine", "ava", "tox", "nox", "make",
    "pester", "cargo", "reindex", "index", "scip-rebuild", "unreal", "runuat", "project_rag",
    "watch", " -w", "recursive", " -r", "workspaces", " -ws",
)

_PM_BASES = frozenset({"npm", "pnpm", "yarn", "bun"})
_PM_RUN_WORDS = frozenset({"run", "run-script"})
_TYPECHECK_BASES = frozenset({"tsc", "vue-tsc"})
_JS_BUILD_BASES = frozenset({"next", "vite"})
_CARGO_BUILD = frozenset({"build", "b"})
_REINDEX_SUBCOMMANDS = frozenset({"reindex", "index", "scip-rebuild"})
_RAG_CLI_BASES = frozenset({"example_retrieval_repo_cli", "project_rag_cli.py"})
_SHELL_WRAPPERS = frozenset({"cmd", "pwsh", "powershell"})
_WRAPPER_FLAGS = frozenset({"/c", "/k", "-c", "-command"})
_MAX_DEPTH = 4
_WATCH_FLAGS = frozenset({"--watch", "--watchall", "-w"})
_WATCH_RUNNERS = frozenset({"jest", "mocha", "vitest", "ava"})
_RECURSIVE_FLAGS = frozenset({"-r", "--recursive", "--workspaces", "-ws"})
_EXEC_LAUNCHERS = frozenset({"npx", "pnpx", "bunx"})
_PM_EXEC_WORDS = frozenset({"exec", "dlx", "x"})
_LAUNCHER_VALUE_FLAGS = frozenset({"-p", "--package", "--prefix", "-c", "--call", "--filter", "-f"})
_VITEST_RUN_WORDS = frozenset({"run"})
_VITEST_WATCH_WORDS = frozenset({"watch", "dev"})
_VITEST_CONFIGS = tuple(
    "%s.config.%s" % (tool, ext)
    for tool in ("vitest", "vite")
    for ext in ("ts", "mts", "cts", "js", "mjs", "cjs")
)
_MAX_WORKERS_FLAG = "--maxworkers"
# vitest flags that take a separate value, so the value is never read as a test filter.
_VITEST_VALUE_FLAGS = frozenset({
    "--project", "-c", "--config", "-r", "--root", "--dir", "--reporter", "--outputfile", "--pool",
    "--environment", "-t", "--testnamepattern", "--maxworkers", "--minworkers", "--shard", "--mode",
    "--exclude", "--browser", "--coverage.provider", "--retry", "--testtimeout", "--hooktimeout",
})
_TEST_FILE_RE = re.compile(r"\.[cm]?[jt]sx?$", re.IGNORECASE)
_TSC_BUILD_FLAGS = frozenset({"-b", "--build"})
_NOEMIT = "noemit"
_UNBOUNDED = "unbounded"
_BARE_PYTEST = "bare_pytest"
_VERIFY_CLASSES = frozenset({HeavyClass.TEST_TIER, HeavyClass.TYPECHECK})
_MAX_WORKERS_CONFIG_RE = re.compile(r"\bmaxWorkers\s*:\s*(\d+)?")


def _positionals(args: Sequence[str]) -> List[str]:
    return [a for a in args if a and not a.startswith("-")]


def _pm_script(args: Sequence[str]) -> Optional[str]:
    pos = _positionals(args)
    if pos and pos[0].lower() in _PM_RUN_WORDS:
        pos = pos[1:]
    return pos[0].lower() if pos else None


def _unwrap_launcher(argv: Sequence[str]) -> List[str]:
    """argv from the launched binary on, for `npx [flags] bin`, `pnpm exec|dlx bin` and the like.

    TRAP: a flag before the binary (`npx --no-install tsc`, `npx -p typescript tsc`) otherwise
    hides the binary and the command classifies light.
    """
    rest = _skip_flags(list(argv))
    while rest:
        base = _suite._base(rest[0]).lower()
        if base in _EXEC_LAUNCHERS:
            tail = rest[1:]
        elif base in _PM_BASES and len(rest) > 1 and rest[1].lower() in _PM_EXEC_WORDS:
            tail = rest[2:]
        else:
            return rest
        tail = _skip_flags(tail)
        if not tail:
            return rest
        rest = tail
    return rest


def _skip_flags(args: List[str]) -> List[str]:
    """args from the first non-flag on. The suite tokenizer strips `npx` but leaves its flags."""
    j = 0
    while j < len(args) and args[j].startswith("-"):
        j += 2 if args[j].lower() in _LAUNCHER_VALUE_FLAGS and "=" not in args[j] else 1
    return args[j:] if j < len(args) else list(args)


def _vitest_args(argv: Sequence[str]) -> Optional[List[str]]:
    """The arguments after `vitest` when argv runs vitest directly or through a launcher."""
    base = _suite._base(argv[0]).lower()
    if base == "vitest":
        return list(argv[1:])
    rest = list(argv[1:])
    if base in _PM_BASES:
        pos = _positionals(rest)
        if pos and pos[0].lower() in _PM_EXEC_WORDS:
            rest = rest[rest.index(pos[0]) + 1:]
        elif not (pos and pos[0].lower() == "vitest"):
            return None
    elif base not in _EXEC_LAUNCHERS:
        return None
    pos = _positionals(rest)
    if pos and _suite._base(pos[0]).lower() == "vitest":
        return rest[rest.index(pos[0]) + 1:]
    return None


def _flag_int(args: Sequence[str], flag: str) -> Optional[int]:
    for j, tok in enumerate(args):
        low = tok.lower()
        if low.startswith(flag + "="):
            raw = tok.split("=", 1)[1]
        elif low == flag and j + 1 < len(args):
            raw = args[j + 1]
        else:
            continue
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def _repo_worker_pin(cwd: Optional[str]) -> Optional[int]:
    """maxWorkers pinned by the nearest vitest/vite config up to the git root: its literal value,
    -1 for a non-literal pin, None for no pin."""
    if not cwd:
        return None
    cur = os.path.abspath(cwd)
    while True:
        for name in _VITEST_CONFIGS:
            try:
                with open(os.path.join(cur, name), encoding="utf-8", errors="replace") as fh:
                    match = _MAX_WORKERS_CONFIG_RE.search(fh.read())
            except OSError:
                continue
            if match is not None:
                return int(match.group(1)) if match.group(1) else -1
        parent = os.path.dirname(cur)
        if os.path.exists(os.path.join(cur, ".git")) or parent == cur:
            return None
        cur = parent


def _vitest_filters(args: Sequence[str]) -> List[str]:
    """Positional filters of a vitest invocation: the run word and every flag value skipped."""
    out: List[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = "=" not in a and a.lower() in _VITEST_VALUE_FLAGS
            continue
        out.append(a)
    if out and out[0].lower() in _VITEST_RUN_WORDS | _VITEST_WATCH_WORDS:
        out = out[1:]
    return out


def _vitest_class(args: Sequence[str], cwd: Optional[str], worker_cap: Optional[int]) -> Optional[HeavyClass]:
    """TEST_TIER unless the run exits on its own, names only explicit test-file paths, and its
    workers are capped under worker_cap.

    TRAP: a vitest filter is a substring match, so a bare word (`vitest run audit`) can select the
    whole suite; only a token naming a script file counts as scoped.
    """
    if not _vitest_bounded(args, cwd, worker_cap):
        return HeavyClass.TEST_TIER
    filters = _vitest_filters(args)
    if not filters or not all(_TEST_FILE_RE.search(f) for f in filters):
        return HeavyClass.TEST_TIER
    return None


def _vitest_bounded(args: Sequence[str], cwd: Optional[str], worker_cap: Optional[int]) -> bool:
    """True when the vitest run exits on its own and its workers are capped at or under worker_cap."""
    low = [a.lower() for a in args]
    pos = [a.lower() for a in _positionals(args)]
    runs_once = (pos and pos[0] in _VITEST_RUN_WORDS) or "--run" in low
    watching = (
        any(a in _WATCH_FLAGS for a in low)
        or (pos and pos[0] in _VITEST_WATCH_WORDS)
        or not runs_once
    )
    if watching or worker_cap is None:
        return False
    workers = _flag_int(args, _MAX_WORKERS_FLAG)
    if workers is None:
        # A flag that is present but not an integer (`50%`) overrides the config pin: unbounded.
        if any(a.lower().split("=", 1)[0] == _MAX_WORKERS_FLAG for a in args):
            return False
        workers = _repo_worker_pin(cwd)
    return workers is not None and workers <= worker_cap


def _jest_unbounded(argv: Sequence[str], worker_cap: Optional[int]) -> bool:
    """True when argv runs jest without --maxWorkers at or under worker_cap."""
    for j, tok in enumerate(argv):
        if _suite._base(tok).lower() == "jest":
            workers = _flag_int(argv[j + 1:], _MAX_WORKERS_FLAG)
            return worker_cap is None or workers is None or workers > worker_cap
    return False


def _watch_or_fanout_class(argv: Sequence[str]) -> Optional[HeavyClass]:
    base = _suite._base(argv[0]).lower()
    args = [a.lower() for a in argv[1:]]
    if base in _WATCH_RUNNERS and any(a in _WATCH_FLAGS for a in args):
        return HeavyClass.TEST_TIER
    if base in _PM_BASES:
        script = _pm_script(argv[1:])
        if script is None:
            return None
        test_script = script.startswith("test")
        if any(a in _RECURSIVE_FLAGS for a in args):
            return HeavyClass.TEST_TIER if test_script else HeavyClass.BUILD
        if test_script and any(a in _WATCH_FLAGS for a in args):
            return HeavyClass.TEST_TIER
    return None


def _is_noemit_tsc(argv: Sequence[str]) -> bool:
    """A one-shot `tsc --noEmit`: no watch, no build mode, and `--noEmit` not set false."""
    if _suite._base(argv[0]) != "tsc":
        return False
    low = [a.lower() for a in argv[1:]]
    if any(a in _WATCH_FLAGS or a in _TSC_BUILD_FLAGS for a in low):
        return False
    if "--noemit=false" in low:
        return False
    i = low.index("--noemit") if "--noemit" in low else -1
    if i < 0:
        return "--noemit=true" in low
    return not (i + 1 < len(low) and low[i + 1] == "false")


def _package_script(cwd: Optional[str], name: str) -> Optional[str]:
    """`scripts.<name>` of the nearest package.json up to the git root; None when absent or unreadable."""
    import json

    if not cwd:
        return None
    cur = os.path.abspath(cwd)
    while True:
        try:
            with open(os.path.join(cur, "package.json"), encoding="utf-8") as fh:
                scripts = json.load(fh).get("scripts")
        except (OSError, ValueError, AttributeError):
            scripts = None
        else:
            value = scripts.get(name) if isinstance(scripts, dict) else None
            return value if isinstance(value, str) else None
        parent = os.path.dirname(cur)
        if os.path.exists(os.path.join(cur, ".git")) or parent == cur:
            return None
        cur = parent


def _is_noemit_pm_typecheck(argv: Sequence[str], cwd: Optional[str]) -> bool:
    """`pnpm typecheck` (or npm/yarn/bun) whose package script is only one-shot `tsc --noEmit`.

    TRAP: a script that chains or calls another script is opaque here, so it stays a plain
    TYPECHECK; only a script every segment of which is a no-emit tsc earns the executor carve-out.
    """
    if _suite._base(argv[0]).lower() not in _PM_BASES or _pm_script(argv[1:]) != "typecheck":
        return False
    if any(a.lower() in _RECURSIVE_FLAGS | _WATCH_FLAGS for a in argv[1:]):
        return False
    script = _package_script(cwd, "typecheck")
    if not script:
        return False
    segments = [_suite._strip_command_prefix(s) for s in _suite._segment_argvs(script)]
    return bool(segments) and all(s and _is_noemit_tsc(_unwrap_launcher(s)) for s in segments)


def _argv_class(argv: Sequence[str]) -> Optional[HeavyClass]:
    base = _suite._base(argv[0])
    args = argv[1:]
    if base in _TYPECHECK_BASES:
        return HeavyClass.TYPECHECK
    fanout = _watch_or_fanout_class(argv)
    if fanout is not None:
        return fanout
    if base in _JS_BUILD_BASES:
        pos = _positionals(args)
        return HeavyClass.BUILD if pos and pos[0].lower() == "build" else None
    if base == "cargo":
        pos = _positionals(args)
        return HeavyClass.BUILD if pos and pos[0].lower() in _CARGO_BUILD else None
    if base in _PM_BASES:
        script = _pm_script(args)
        if script == "typecheck":
            return HeavyClass.TYPECHECK
        if script == "build":
            return HeavyClass.BUILD
        return None
    if base in _RAG_CLI_BASES:
        pos = _positionals(args)
        return HeavyClass.REINDEX if pos and pos[0].lower() in _REINDEX_SUBCOMMANDS else None
    if base.startswith("python") and len(argv) > 1:
        rest = [a for a in args if a.lower() not in ("-u", "-x")]
        if rest and _suite._base(rest[0]) in _RAG_CLI_BASES:
            pos = _positionals(rest[1:])
            if pos and pos[0].lower() in _REINDEX_SUBCOMMANDS:
                return HeavyClass.REINDEX
    return None


def _xdist_workers(args: Sequence[str]) -> bool:
    """True when pytest `args` ask for more than one xdist worker: `-n N`, `-nN`, `--numprocesses N`
    with N an integer above 1, or the open-ended `auto`/`logical`.
    The last count given wins, as in pytest.

    TRAP: a target path does not make an xdist run light; the workers are the footprint.
    """
    heavy = False
    for j, tok in enumerate(args):
        low = tok.lower()
        if low.startswith("--numprocesses="):
            raw = low.split("=", 1)[1]
        elif low == "--numprocesses" or tok == "-n":
            raw = args[j + 1].lower() if j + 1 < len(args) else ""
        elif tok.startswith("-n") and not tok.startswith("--") and len(tok) > 2:
            raw = low[2:].lstrip("=")
        else:
            continue
        if raw in ("auto", "logical"):
            heavy = True
            continue
        try:
            heavy = int(raw) > 1
        except ValueError:
            continue
    return heavy


def _is_xdist_pytest(argv: Sequence[str]) -> bool:
    """argv (prefix already stripped) runs pytest as its command, `pytest ...` or `python -m pytest ...`,
    with xdist workers.

    TRAP: pytest is read only at the runner position, so `echo pytest -n 4` and `tail pytest -n 4`
    stay light."""
    if not argv:
        return False
    base = _suite._base(argv[0]).lower()
    if base in ("pytest", "py.test"):
        return _xdist_workers(argv[1:])
    if base.startswith("python"):
        for j, tok in enumerate(argv[1:-1], start=1):
            if tok == "-m" and _suite._base(argv[j + 1]).lower() == "pytest":
                return _xdist_workers(argv[j + 2:])
    return False


SCOPED_PYTEST_REMEDY = "python -m coordinator_core.testing.scoped_pytest --allow ... -- ..."

_PYTEST_VALUE_FLAGS = frozenset((
    "-n", "--numprocesses", "--dist", "-k", "-m", "-p", "-c", "-o", "-W", "-r", "--tb", "--rootdir", "--maxfail", "--ignore", "--ignore-glob",
    "--deselect", "--junitxml", "--basetemp", "--durations", "--color", "--confcutdir", "--import-mode",
    "--log-level", "--capture", "--override-ini", "--config-file", "--cov", "--timeout",
))


def _pytest_args(argv: Sequence[str]) -> Optional[Sequence[str]]:
    """Arguments after pytest when argv runs it as its command (`pytest` or `python -m pytest`), else None."""
    if not argv:
        return None
    base = _suite._base(argv[0]).lower()
    if base in ("pytest", "py.test"):
        return argv[1:]
    if base.startswith("python"):
        for j, tok in enumerate(argv[1:-1], start=1):
            if tok == "-m" and _suite._base(argv[j + 1]).lower() == "pytest":
                return argv[j + 2:]
    return None


def _pytest_targets(args: Sequence[str]) -> List[str]:
    targets: List[str] = []
    skip = False
    after_dashdash = False
    for tok in args:
        if after_dashdash:
            targets.append(tok)
        elif skip:
            skip = False
        elif tok == "--":
            after_dashdash = True
        elif tok.startswith("-"):
            skip = tok in _PYTEST_VALUE_FLAGS
        else:
            targets.append(tok)
    return targets


def _is_unscoped_plain_pytest(argv: Sequence[str]) -> bool:
    """argv runs pytest as its command with no more than one worker and other than exactly one
    file target (a lone directory is unbounded).

    The collection is the footprint; `SCOPED_PYTEST_REMEDY` is the admitted launcher. TRAP: a lone
    directory target collects everything under it, so only a `.py` file part (nodeid allowed) is
    scoped; pytest is read only at the runner position, so `echo pytest a b` stays light, and the
    launcher's own argv is not a pytest runner here, it admits itself."""
    args = _pytest_args(argv)
    if args is None or _xdist_workers(args):
        return False
    targets = _pytest_targets(args)
    return len(targets) != 1 or not targets[0].split("::", 1)[0].lower().endswith(".py")


def _wrapped_command(argv: Sequence[str]) -> Optional[str]:
    if _suite._base(argv[0]) not in _SHELL_WRAPPERS:
        return None
    for j, tok in enumerate(argv[1:], start=1):
        if tok.lower() in _WRAPPER_FLAGS and j + 1 < len(argv):
            return " ".join(argv[j + 1:])
    return None


def _scan(
    command: str,
    testpaths: Sequence[str],
    cwd: Optional[str],
    depth: int,
    found: List[Any],
    worker_cap: Optional[int],
) -> None:
    for segment in _suite._segment_argvs(command):
        argv = _suite._strip_command_prefix(segment)
        if not argv:
            continue
        inner = _wrapped_command(argv) if depth < _MAX_DEPTH else None
        if inner is not None:
            _scan(inner, testpaths, cwd, depth + 1, found, worker_cap)
            continue
        argv = _unwrap_launcher(argv)
        vitest = _vitest_args(argv)
        if vitest is not None:
            heavy = _vitest_class(vitest, cwd, worker_cap)
            found.append(heavy if heavy is not None else "scoped")
            if not _vitest_bounded(vitest, cwd, worker_cap):
                found.append(_UNBOUNDED)
            continue
        heavy = _argv_class(argv)
        if heavy is not None:
            found.append(heavy)
            if heavy is HeavyClass.TYPECHECK and (
                _is_noemit_tsc(argv) or _is_noemit_pm_typecheck(argv, cwd)
            ):
                found.append(_NOEMIT)
            if _watch_or_fanout_class(argv) is not None or any(
                a.lower() in _WATCH_FLAGS for a in argv[1:]
            ) or _jest_unbounded(argv, worker_cap):
                found.append(_UNBOUNDED)
            continue
        if _is_xdist_pytest(argv) or _is_unscoped_plain_pytest(argv):
            found.append(HeavyClass.TEST_TIER)
            found.append(_UNBOUNDED)
            found.append(_BARE_PYTEST)
        elif _suite._classify_tokens(argv, testpaths, cwd) is not None:
            found.append(HeavyClass.TEST_TIER)
            if _jest_unbounded(argv, worker_cap):
                found.append(_UNBOUNDED)
        elif _suite._runner_recognized(argv):
            found.append("scoped")


_PRECEDENCE = (
    HeavyClass.UE, HeavyClass.TYPECHECK, HeavyClass.BUILD, HeavyClass.REINDEX, HeavyClass.TEST_TIER,
)


def classify(
    command: Any,
    tool_input: Any = None,
    cwd: Optional[str] = None,
    vitest_worker_cap: Optional[int] = None,
) -> Classification:
    """Heavy class of `command`; `scoped` is True only for a command whose sole runner footprint
    is an explicit test-target run or a worker-capped vitest run. `background` mirrors
    `tool_input["run_in_background"]`. `cwd` is the payload's; `vitest_worker_cap` None makes
    every vitest run heavy."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    background = ti.get("run_in_background") is True
    light = Classification(None, False, background)
    if not isinstance(command, str) or not command.strip():
        return light
    low = command.lower()
    if not any(s in low for s in _PREFILTER + _ue._PY_PREFILTER):
        return light
    try:
        if _ue.launches_heavy(command):
            return Classification(HeavyClass.UE, False, background)
        if not isinstance(cwd, str) or not cwd:
            cwd = ti.get("cwd") if isinstance(ti.get("cwd"), str) else os.getcwd()
        found: List[Any] = []
        _scan(command, _suite._read_testpaths(cwd), cwd, 0, found, vitest_worker_cap)
    except Exception:
        return light
    heavies = [f for f in found if isinstance(f, HeavyClass)]
    noemit = bool(heavies) and all(f is HeavyClass.TYPECHECK for f in heavies) and (
        found.count(_NOEMIT) == len(heavies)
    )
    bounded = bool(heavies) and _UNBOUNDED not in found and all(f in _VERIFY_CLASSES for f in heavies)
    for cls in _PRECEDENCE:
        if cls in found:
            return Classification(cls, False, background, noemit, bounded, _BARE_PYTEST in found)
    return Classification(None, "scoped" in found, background)


@dataclass(frozen=True)
class SpawnAdmission:
    """Verdict of `admit_before_spawn`; `reason` is one path-free line safe for a ceremony detail."""

    admitted: bool
    reason: str
    key: str
    peak_gib: float


def _load_box_memory_host() -> Any:
    """`box_memory_host` by file path: it lives in bin/lib, which is not a package. Tries the
    claude-klabauter layout (`coordinator/bin/lib`) then the OSS mirror layout (`bin/lib`)."""
    import importlib.util
    import sys
    from pathlib import Path

    cached = sys.modules.get("box_memory_host")
    if cached is not None and hasattr(cached, "admit"):
        return cached
    root = Path(__file__).resolve().parents[2]
    for lib in (root / "coordinator" / "bin" / "lib", root / "bin" / "lib"):
        path = lib / "box_memory_host.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("box_memory_host", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["box_memory_host"] = module  # its @dataclass classes resolve through it
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop("box_memory_host", None)
                raise
            return module
    raise ImportError("box_memory_host.py not found under coordinator/bin/lib or bin/lib")


def _wait_for_slot(key: str, peak_gib: float, session_id: str, wait_kw: dict) -> Optional[dict]:
    """The Group EM slot wait for a HOLD on a step over the slot threshold; None when the peak is
    at or under it or no session resolves (the HOLD stands)."""
    from coordinator_core.group_em import slot_wait

    if peak_gib * 1024 <= slot_wait.SLOT_THRESHOLD_MB:
        return None
    sid = session_id
    if not sid:
        from coordinator_core.session import core

        sid = core.resolve_session_id()
    if not sid:
        return None
    return slot_wait.request_and_wait(sid, key, peak_gib * 1024, **wait_kw)


def admit_before_spawn(
    key: str,
    default_peak_gib: float,
    host: Any = None,
    register_pid: Optional[int] = None,
    wait_kw: Optional[dict] = None,
    session_id: str = "",
) -> SpawnAdmission:
    """The one admit-before-spawn chokepoint for a heavy subprocess an engine op or ceremony launches.

    Peak is the larger of `default_peak_gib` and the recorded peak for `key`; the verdict is
    `box_memory_host.admit` over measured memory net of peers' reserved headroom, the same
    arithmetic as `box-memory-snapshot --admit-peak`. An unreadable reading refuses (fail-closed).
    Call before the spawn; `register_spawn` after it reserves the child's headroom.
    `register_pid` registers that pid on admit. `host` is a test seam for the host module.
    `wait_kw` (None = refuse at once) is the `slot_wait.request_and_wait` deadline kwargs: on a
    HOLD for a peak over the slot threshold with a resolvable `session_id`, the Group EM's slot
    grant is awaited and admits; a missing grant keeps the refusal."""
    peak_gib = float(default_peak_gib)
    try:
        host = host or _load_box_memory_host()
        adm_path = host.admission_file()
        recorded = host.read_recorded_peak(adm_path, key)
        peak_gib = max(peak_gib, recorded or 0.0)
        status = host.read_memory_status()
        # TRAP: with a pid to register, the reservation read and the START write share one lock,
        # or two callers both read zero reservations and both admit.
        with (host.admission_lock(adm_path) if register_pid is not None else contextlib.nullcontext()):
            reserved = host.reserved_bytes(adm_path)
            status = dataclasses.replace(
                status,
                avail_phys=max(0, status.avail_phys - reserved),
                commit_used=status.commit_used + reserved,
            )
            verdict = host.admit(int(peak_gib * host.GIB), status)
            if verdict.admit and register_pid is not None:
                register_spawn(key, peak_gib, register_pid, host)
    except Exception as exc:
        return SpawnAdmission(False, "memory unreadable (%s)" % type(exc).__name__, key, peak_gib)
    gib = lambda n: round(n / host.GIB, 1)
    if not verdict.admit:
        hold = "HOLD %s: need %sGiB avail, %sGiB free after reservations" % (
            key, gib(verdict.need_avail), gib(status.avail_phys))
        granted = _wait_for_slot(key, peak_gib, session_id, wait_kw) if wait_kw is not None else None
        if not (granted and granted.get("admitted")):
            reason = hold if not granted else "%s; slot wait: %s" % (hold, granted.get("reason"))
            return SpawnAdmission(False, reason, key, peak_gib)
        if register_pid is not None:
            register_spawn(key, peak_gib, register_pid, host)
        return SpawnAdmission(True, "ADMIT %s: slot %s" % (key, granted.get("reason")), key, peak_gib)
    return SpawnAdmission(True, "ADMIT %s: peak %sGiB" % (key, round(peak_gib, 1)), key, peak_gib)


def register_spawn(key: str, peak_gib: float, pid: int, host: Any = None) -> None:
    """Reserve an admitted child's headroom until it exits; never raises."""
    try:
        host = host or _load_box_memory_host()
        host.register_command(host.admission_file(), key, pid, peak_gib)
    except Exception:
        pass

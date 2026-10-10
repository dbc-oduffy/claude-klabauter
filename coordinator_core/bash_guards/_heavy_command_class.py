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

import os
import re
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
            if heavy is HeavyClass.TYPECHECK and _is_noemit_tsc(argv):
                found.append(_NOEMIT)
            if _watch_or_fanout_class(argv) is not None or any(
                a.lower() in _WATCH_FLAGS for a in argv[1:]
            ) or _jest_unbounded(argv, worker_cap):
                found.append(_UNBOUNDED)
            continue
        if _suite._classify_tokens(argv, testpaths, cwd) is not None:
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
    if not any(s in low for s in _PREFILTER):
        return light
    try:
        if _ue._launches_heavy(command):
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
            return Classification(cls, False, background, noemit, bounded)
    return Classification(None, "scoped" in found, background)

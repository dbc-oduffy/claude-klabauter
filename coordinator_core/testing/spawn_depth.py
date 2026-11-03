"""Nesting-depth sentinel and self-inclusion check for every engine-launched pytest.

Leaf module: stdlib only, no subprocess. Top-level pytest is depth 0; a child launched from
it is depth 1; any grandchild is refused. Two hop kinds are counted by one rule:

- an engine spawner stamps the child via ``child_env`` (ENV_KEY = spawner depth + 1 when the
  spawner is inside a session, 0 from a launcher outside any; PYTEST_CURRENT_TEST removed);
- a raw ``subprocess`` pytest launched inside a test inherits PYTEST_CURRENT_TEST, so the
  child's root conftest counts the hop with ``session_depth`` and writes it back to ENV_KEY.

A spawner refuses via ``refusal_for_spawn`` before its first spawn and refuses a selection
that resolves to the calling test's own file (or a directory holding it) via
``selection_includes_spawner``. Read PYTEST_CURRENT_TEST for that check BEFORE ``child_env``.
"""
from __future__ import annotations

import os
import sys
from typing import Mapping, MutableMapping, Optional, Sequence

ENV_KEY = "COORDINATOR_PYTEST_DEPTH"
COUNTED_KEY = "COORDINATOR_PYTEST_DEPTH_COUNTED"
MAX_DEPTH = 1
CURRENT_TEST_KEY = "PYTEST_CURRENT_TEST"
# One BLAS thread per spawned pytest: each extra pool commits per-thread buffers (~750 MiB at 24 cores).
BLAS_THREAD_KEYS = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")

# Pytest options whose NEXT argv token is a value, never a selection target.
_VALUE_OPTIONS = frozenset(
    {
        "-p", "-c", "-k", "-m", "-o", "-n", "-W", "-r",
        "--rootdir", "--basetemp", "--confcutdir", "--ignore", "--ignore-glob",
        "--deselect", "--junitxml", "--maxfail", "--durations", "--import-mode",
        "--override-ini", "--log-file", "--result-log", "--dist", "--numprocesses",
        "--tb", "--color", "--capture", "--log-level", "--log-cli-level", "--show-capture",
    }
)


def current_depth(env: Mapping[str, str]) -> int:
    """Depth recorded in ``env``; absent, garbage or negative is 0."""
    try:
        return max(0, int(env.get(ENV_KEY, "0")))
    except (TypeError, ValueError):
        return 0


def _counted_marker(pid: int, depth: int, in_test: bool) -> str:
    return f"{pid}:{depth}:{int(in_test)}"


def session_depth(env: Mapping[str, str]) -> int:
    """Depth of a pytest session starting under ``env``: one more when launched from inside a test.

    Idempotent for ``os.environ`` within one process: the root conftest and the nested-session
    plugin both call it, and the second call must not add the PYTEST_CURRENT_TEST hop again. The
    first call on ``os.environ`` stamps COUNTED_KEY; a later call finds it valid only while the
    pid, ENV_KEY and the in-test flag are unchanged, so a child process (new pid) always recounts.
    """
    in_test = bool(env.get(CURRENT_TEST_KEY))
    if env is os.environ:
        marker = _counted_marker(os.getpid(), current_depth(env), in_test)
        if env.get(COUNTED_KEY) == marker:
            return current_depth(env)
    depth = current_depth(env) + (1 if in_test else 0)
    if env is os.environ:
        env[COUNTED_KEY] = _counted_marker(os.getpid(), depth, in_test)
    return depth


def enter_session(environ: MutableMapping[str, str]) -> int:
    """Count this session's depth once, write it back to ENV_KEY and return it."""
    depth = session_depth(environ)
    environ[ENV_KEY] = str(depth)
    return depth


def child_depth(env: Mapping[str, str]) -> int:
    """Depth the pytest child launched from ``env`` starts at.

    A spawner inside a session (ENV_KEY stamped by the root conftest, even at 0, or
    PYTEST_CURRENT_TEST set) adds one hop; a launcher outside any session starts a top-level
    run at depth 0.
    """
    if ENV_KEY in env or env.get(CURRENT_TEST_KEY):
        return current_depth(env) + 1
    return 0


_STDLIB_NAMES = frozenset(sys.stdlib_module_names)
_FOLD_CASE = os.name == "nt"
_shipped: Optional[frozenset] = None


def _stdlib_ships(stem: str) -> bool:
    """True when this interpreter's own stdlib provides ``stem``.

    ``stdlib_module_names`` lists every platform's modules, so pyreadline3's site-packages
    ``readline.py`` on Windows (no stdlib readline) fills a gap rather than shadowing one.
    """
    global _shipped
    if _shipped is None:
        import sysconfig

        names = set(sys.builtin_module_names)
        paths = sysconfig.get_paths()
        dirs = {paths.get("stdlib"), paths.get("platstdlib"), os.path.join(sys.base_prefix, "DLLs")}
        for d in filter(None, dirs):
            for d2 in (d, os.path.join(d, "lib-dynload")):
                try:
                    entries = os.listdir(d2)
                except OSError:
                    continue
                names.update(e.split(".", 1)[0] for e in entries)
        _shipped = frozenset(n.lower() if _FOLD_CASE else n for n in names)
    return stem in _shipped


def stdlib_shadow_in_pythonpath(env: Mapping[str, str]) -> Optional[str]:
    """Path of the first PYTHONPATH entry holding ``<stdlib>.py`` or ``<stdlib>/__init__.py``, else None.

    One listdir per entry, no import, no subprocess: a shadowing module there runs in every
    child interpreter at import time, before any conftest can count depth.
    """
    for entry in (env.get("PYTHONPATH") or "").split(os.pathsep):
        if not entry:
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            if _FOLD_CASE:
                stem = stem.lower()
            if stem not in _STDLIB_NAMES or not _stdlib_ships(stem):
                continue
            if ext == ".py":
                return os.path.join(entry, name)
            if not ext:
                init = os.path.join(entry, name, "__init__.py")
                if os.path.isfile(init):
                    return init
    return None


def refusal_for_spawn(env: Mapping[str, str]) -> Optional[str]:
    """Refusal text when the child ``child_env(env)`` would start deeper than MAX_DEPTH, else None.

    A launcher outside a session or in a depth-0 session may start its child; only the
    grandchild is refused. A PYTHONPATH entry that shadows a stdlib module is refused too.
    """
    depth = child_depth(env)
    if depth > MAX_DEPTH:
        return (
            f"refused: pytest nesting depth {current_depth(env)} via {ENV_KEY}; a child would be depth {depth} "
            f"(> {MAX_DEPTH}); a pytest launched from a pytest-launched pytest is not allowed"
        )
    shadow = stdlib_shadow_in_pythonpath(env)
    if shadow:
        return (
            f"refused: PYTHONPATH holds {shadow!r}, which shadows a stdlib module and runs in every "
            f"child interpreter at import time; remove that entry from PYTHONPATH"
        )
    return None


def child_env(env: Mapping[str, str]) -> dict:
    """Copy of ``env`` for a spawned pytest: ``child_depth`` stamped, PYTEST_CURRENT_TEST stripped, BLAS pools at one thread unless set."""
    out = dict(env)
    for key in BLAS_THREAD_KEYS:
        out.setdefault(key, "1")
    out[ENV_KEY] = str(child_depth(env))
    out.pop(CURRENT_TEST_KEY, None)
    out.pop(COUNTED_KEY, None)
    return out


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _resolve(token: str, rootdir: str) -> str:
    return _norm(token if os.path.isabs(token) else os.path.join(rootdir, token))


def spawner_file(env: Mapping[str, str], rootdir: str) -> Optional[str]:
    """Normalised file of the test named by PYTEST_CURRENT_TEST, or None outside a test."""
    raw = env.get(CURRENT_TEST_KEY)
    if not raw:
        return None
    path = raw.split("::", 1)[0].strip()
    if not path:
        return None
    return _resolve(path, rootdir)


def _positional_tokens(selection: Sequence[str]) -> list:
    tokens: list = []
    skip_next = False
    after_dashdash = False
    for tok in selection:
        if skip_next:
            skip_next = False
            continue
        if after_dashdash:
            tokens.append(tok)
        elif tok == "--":
            after_dashdash = True
        elif tok.startswith("-"):
            skip_next = tok in _VALUE_OPTIONS
        else:
            tokens.append(tok)
    return tokens


def selection_includes_spawner(
    selection: Sequence[str], env: Mapping[str, str], rootdir: str
) -> Optional[str]:
    """Refusal text when ``selection`` would collect the calling test's own file, else None.

    A token (nodeid stripped at ``::``) matches when it is the spawner file or a directory
    containing it. A selection naming no target runs the rootdir's testpaths, so it counts as
    the rootdir.
    """
    spawner = spawner_file(env, rootdir)
    if spawner is None:
        return None
    tokens = [t.split("::", 1)[0] for t in _positional_tokens(selection)]
    targets = [_resolve(t, rootdir) for t in tokens if t] or [_norm(rootdir)]
    for target in targets:
        if target == spawner or spawner.startswith(target.rstrip(os.sep) + os.sep):
            return (
                f"refused: pytest selection {target!r} includes the calling test file "
                f"{spawner!r}; a test cannot launch a run that collects itself"
            )
    return None

"""Test-home guard for Unimatrix: a pytest settings home resolves no real far box, key path or Run key.

conftest.py marks an isolated settings home with UNIMATRIX_TEST_HOME=1. Code that would reach a real
far box, a key outside that home or the HKCU Run key calls `refuse_real`, which raises under the
marker and is a no-op outside it. Loadable by path (bin scripts load siblings via spec_from_file_location).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

MARKER_ENV = "UNIMATRIX_TEST_HOME"
ALLOWED_BOXES_ENV = "UNIMATRIX_TEST_ALLOWED_BOXES"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
KINDS = ("key_path", "far_box", "run_key")

_UNIMATRIX_KEY_LINE = re.compile(r"""^\s*["']?unimatrix\.""")
_UNIMATRIX_TABLE = re.compile(r"^\s*\[+\s*unimatrix\b")
_ANY_TABLE = re.compile(r"^\s*\[")


class TestHomeRefusal(RuntimeError):
    """A test settings home was asked to reach something real; carries `kind` and `value`."""

    __test__ = False  # not a pytest class despite the Test prefix

    def __init__(self, kind: str, value) -> None:
        self.kind = kind
        self.value = value
        super().__init__(f"test home refuses real {kind}: {value!r}")


def is_test_home(env=None) -> bool:
    """True when the environment carries the test-home marker."""
    env = os.environ if env is None else env
    return str(env.get(MARKER_ENV, "")).strip() not in ("", "0")


def strip_unimatrix_keys(text: str) -> str:
    """Registry text without any `unimatrix.*` key line or `[unimatrix...]` table body."""
    out = []
    in_table = False
    for line in text.splitlines(keepends=True):
        if _UNIMATRIX_TABLE.match(line):
            in_table = True
            continue
        if in_table and _ANY_TABLE.match(line):
            in_table = False
        if in_table or _UNIMATRIX_KEY_LINE.match(line):
            continue
        out.append(line)
    return "".join(out)


def _host_of(box: str) -> str:
    host = str(box).strip().rsplit("@", 1)[-1]
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _inside(path, root) -> bool:
    try:
        Path(os.path.abspath(os.fspath(path))).relative_to(Path(os.path.abspath(os.fspath(root))))
    except ValueError:
        return False
    return True


def refuse_real(kind: str, value, env=None) -> None:
    """Raise TestHomeRefusal when `value` is real and the environment is a test home; else return.

    key_path: refused unless inside COORDINATOR_SETTINGS_HOME (no home set refuses everything).
    far_box: refused unless a loopback host or listed in UNIMATRIX_TEST_ALLOWED_BOXES (space/comma separated).
    run_key: every write refused.
    """
    env = os.environ if env is None else env
    if not is_test_home(env):
        return
    if kind == "run_key":
        raise TestHomeRefusal(kind, value)
    if kind == "key_path":
        home = env.get("COORDINATOR_SETTINGS_HOME")
        if not home or not _inside(value, home):
            raise TestHomeRefusal(kind, value)
        return
    if kind == "far_box":
        allowed = {a for a in re.split(r"[\s,]+", env.get(ALLOWED_BOXES_ENV, "")) if a}
        if str(value) in allowed or _host_of(value) in LOOPBACK_HOSTS | allowed:
            return
        raise TestHomeRefusal(kind, value)
    raise ValueError(f"unknown test-home kind {kind!r}; expected one of {KINDS}")


def isolated_settings_home(tmp_path_factory):
    """Generator for an autouse pytest fixture: a private settings home that reaches nothing real.

    The one body behind every conftest that isolates (repo root, and coordinator/bin/tests, which ships
    to klabauter and its mirrors). Copies registry.toml and a unimatrix-stripped registry.local.toml
    from the live home, then sets COORDINATOR_SETTINGS_HOME, the test-home marker and AGY_BRAIN_DIR.
    A private MonkeyPatch, never the shared `monkeypatch` fixture: sharing it would defer a test's own
    delenv/setenv undo past every containment check's teardown.
    """
    import pytest  # noqa: PLC0415 (only a pytest caller reaches this)

    mp = pytest.MonkeyPatch()
    live = Path(os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings")
    iso = tmp_path_factory.mktemp("settings-home")
    for name in ("registry.toml", "registry.local.toml"):
        src = live / "machine-local" / name
        if src.is_file():
            (iso / "machine-local").mkdir(exist_ok=True)
            data = src.read_bytes()
            if name == "registry.local.toml":
                data = strip_unimatrix_keys(data.decode("utf-8")).encode("utf-8")
            (iso / "machine-local" / name).write_bytes(data)
    mp.setenv("COORDINATOR_SETTINGS_HOME", str(iso))
    mp.setenv(MARKER_ENV, "1")
    # default_presence_dir() is Path(AGY_BRAIN_DIR).parent / "presence"; unset it falls back to ~/.gemini.
    mp.setenv("AGY_BRAIN_DIR", str(iso / "agy" / "brain"))
    try:
        yield iso
    finally:
        mp.undo()

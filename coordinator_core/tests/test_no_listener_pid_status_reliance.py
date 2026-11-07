"""PORT-OWNER-IS-NOT-A-LISTEN-STATUS tripwire: no engine code may rely on a psutil listener pid or LISTEN status.

On Windows with psutil 7.2 and an unprivileged caller, `net_connections()`
reports listener pid 0 and a bound listener's status as NONE, never LISTEN. A
`status == LISTEN` filter or a port-to-pid kill silently finds nothing.

Scans `coordinator_core` and `coordinator/bin` (`.py`, `.ps1`), excluding this
module. No EXEMPT list: a legitimate need is a ruling, not a suppression.
Pure source scan; needs no privileged listener.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("coordinator_core", "coordinator/bin")
_SUFFIXES = (".py", ".ps1")
_SELF = Path(__file__).resolve()

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("net_connections", re.compile(r"\bnet_connections\b")),
    ("connections() alias", re.compile(r"\.connections\(")),
    ("CONN_LISTEN", re.compile(r"\bCONN_LISTEN\b")),
    ("NetTCPConnection", re.compile(r"\bGet-NetTCPConnection\b|\bNetTCPConnection\b")),
    ("OwningProcess", re.compile(r"\bOwningProcess\b")),
    ("quoted LISTEN status", re.compile(r"[\"']LISTEN(?:ING)?[\"']")),
)


def _scan_text(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _PATTERNS:
            if pattern.search(line):
                hits.append((lineno, label))
    return hits


def _scan_tree() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for root in _SCAN_ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("*")):
            if path.suffix not in _SUFFIXES or "__pycache__" in path.parts:
                continue
            if path.resolve() == _SELF:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(_REPO_ROOT).as_posix()
            found.extend((rel, lineno, label) for lineno, label in _scan_text(text))
    return found


def test_no_code_relies_on_listener_pid_or_listen_status():
    hits = _scan_tree()
    assert not hits, (
        "PORT-OWNER-IS-NOT-A-LISTEN-STATUS: psutil net_connections listener pid/status is unusable on Windows "
        "(psutil 7.2, unprivileged: pid 0, status NONE not LISTEN), so a "
        "LISTEN filter or port-to-pid kill finds nothing. Identify the "
        "daemon by pidfile, lock owner or a connect probe; pid 0 / NONE means owner unknown.\n"
        + "\n".join(f"  {rel}:{line} uses {label}" for rel, line, label in hits)
    )


def test_the_tripwire_catches_each_reliance_shape():
    shapes = {
        "net_connections": "for c in psutil.net_connections(kind='tcp'):",
        "connections() alias": "for c in psutil.Process(pid).connections(kind='inet'):",
        "CONN_LISTEN": "if c.status == psutil.CONN_LISTEN:",
        "NetTCPConnection": "Get-NetTCPConnection -LocalPort 8080",
        "OwningProcess": "(...).OwningProcess",
        "quoted LISTEN status": "if conn.status == 'LISTEN':",
    }
    for label, line in shapes.items():
        assert (1, label) in _scan_text(line), label


def test_the_tripwire_passes_unrelated_code():
    assert _scan_text("pid = int(pidfile.read_text())\nsock.connect((host, port))") == []


def test_scan_roots_exist():
    for root in _SCAN_ROOTS:
        assert (_REPO_ROOT / root).is_dir(), root

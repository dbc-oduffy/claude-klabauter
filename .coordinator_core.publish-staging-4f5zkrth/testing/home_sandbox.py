"""Cross-platform home-directory sandboxing for tests.

``monkeypatch.setenv("HOME", ...)`` alone does NOT isolate the home directory
on Windows: ``os.path.expanduser("~")`` prefers ``USERPROFILE``, then
``HOMEDRIVE``+``HOMEPATH``, and only falls back to ``HOME`` when all three are
absent. Since ``USERPROFILE`` is always set on Windows, a HOME-only sandbox
silently resolves to the real user profile — which is how this suite came to
overwrite the live ``~/.claude/.doe-root`` on three machines (2026-07-20).

Use :func:`sandbox_home` anywhere a test needs the code under test to resolve
``~`` to a directory the test controls. The repo-root ``conftest.py`` applies
the same neutralization to every test as a backstop; this helper is for tests
that need to *name* the sandbox and assert against it.
"""

from __future__ import annotations

from pathlib import Path


def sandbox_home(monkeypatch, home) -> Path:
    """Point every home-resolution env var at ``home`` and return it as a Path.

    Creates ``home`` if absent so callers can pass a bare ``tmp_path / "home"``.
    """
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows: expanduser prefers this
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    return home

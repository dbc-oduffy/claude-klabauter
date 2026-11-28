"""The one place the published engine repo's name lives, new name first, legacy name kept for fallback.

Stdlib only: loaded by file path before the engine is importable, like host_registry.py.
"""
from __future__ import annotations

ENGINE_REPO_NAME = "agent-klabauter"
LEGACY_ENGINE_REPO_NAME = "claude-klabauter"
ENGINE_REPO_NAMES: tuple[str, ...] = (ENGINE_REPO_NAME, LEGACY_ENGINE_REPO_NAME)

_UPSTREAM_OWNER = "dbc-example-operator"


def pointer_file_names() -> tuple[str, ...]:
    """Checkout-pointer dotfile names, new name first."""
    return tuple(f".{name}-root" for name in ENGINE_REPO_NAMES)


def registry_keys() -> tuple[str, ...]:
    """machine-local registry keys, new name first."""
    return tuple(f"repos.{name.replace('-', '_')}" for name in ENGINE_REPO_NAMES)


def upstream_url(name: str | None = None) -> str:
    """HTTPS clone URL of an engine repo name (default: the current name)."""
    return f"https://github.com/{_UPSTREAM_OWNER}/{name or ENGINE_REPO_NAME}"

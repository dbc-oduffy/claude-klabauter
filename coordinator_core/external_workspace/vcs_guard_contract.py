"""Contract between the install step, the reference-transaction hook and the rehydrate op.

Mechanism: a global git config includes INCLUDE_FILE_NAME, which holds one
``includeIf "gitdir:<workspace>/"`` block per registered external workspace; each block
includes HOOKS_CONFIG_FILE_NAME (same directory), which sets ``core.hooksPath`` to the
engine hook dir. Only registered paths pay the hook; the hook refuses a ref write unless
TOKEN_ENV is set, and only the rehydrate op sets it. The token marks the sanctioned route;
it is not a security boundary.

Known limits: the token is read inside the hook's Python, so an unresolvable interpreter
refuses the rehydrate op too; ``gitdir:`` matches case-sensitively, so a registered path
whose case differs from git's real path on Windows/macOS is not scoped.

Spike result (git 2.56): ``includeIf "gitdir:"`` is honoured for ``git clone`` into a
destination that does not yet exist, so no registry-path fallback is needed in the hook.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

HOOK_DIR = Path(__file__).resolve().parent / "hooks"
HOOK_NAME = "reference-transaction"
TOKEN_ENV = "COORDINATOR_EXTERNAL_WS_REHYDRATE"
INCLUDE_FILE_NAME = "external-workspace-git-guard.gitconfig"
HOOKS_CONFIG_FILE_NAME = "external-workspace-git-guard-hooks.gitconfig"


def to_git_posix(path: str | Path) -> str:
    """Return ``path`` in the forward-slash form git config expects, without a trailing slash."""
    text = str(path)
    if len(text) >= 2 and text[1] == ":":
        text = PureWindowsPath(text).as_posix()
    else:
        text = text.replace("\\", "/")
    return text.rstrip("/") or "/"


def render_include_stanza(paths: list[str | Path] | tuple[str | Path, ...]) -> str:
    """Render INCLUDE_FILE_NAME content: one includeIf block per path, in sorted order.

    Each block's ``path`` is relative to the including file, so both files must sit in the
    same directory. A trailing ``/`` on the gitdir pattern makes it match the repo dir and
    everything beneath it, including a clone destination not yet created.
    """
    blocks = []
    for posix in sorted({to_git_posix(p) for p in paths}):
        blocks.append(
            f'[includeIf "gitdir:{posix}/"]\n\tpath = {HOOKS_CONFIG_FILE_NAME}\n'
        )
    return "".join(blocks)


def render_hooks_config(hooks_dir: str | Path = HOOK_DIR) -> str:
    """Render HOOKS_CONFIG_FILE_NAME content: the ``core.hooksPath`` the includeIf blocks apply."""
    return f"[core]\n\thooksPath = {to_git_posix(hooks_dir)}\n"

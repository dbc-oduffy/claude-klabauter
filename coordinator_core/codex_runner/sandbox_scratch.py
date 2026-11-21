"""Pre-create the codex workspace's `<cwd>/scratch` before a sandboxed child starts.

Codex stamps its per-root capability SID onto `<cwd>` with an inheriting ACE; a directory created here,
unsandboxed, inherits it. No ACL is written anywhere: the grantable principal already holds inherited
access and the failing one (codex's random per-root capability SID) is not derivable in-process.
"""

from __future__ import annotations

from pathlib import Path

SCRATCH_DIRNAME = "scratch"
_WARP = Path(SCRATCH_DIRNAME) / "warp"
_CAPABILITY_SID = "per-root capability SID"


class ScratchGrantRefused(RuntimeError):
    """`<cwd>/scratch` could not be created for the sandbox. `.path` is the directory, `.principal` the
    restricted codex sandbox token that needs it."""

    def __init__(self, path: Path, principal: str, cause: BaseException | None = None) -> None:
        self.path = Path(path)
        self.principal = principal
        detail = f" ({cause})" if cause is not None else ""
        super().__init__(f"cannot create {self.path} for {principal}{detail}; create it as the operator")


def _principal_text() -> str:
    try:
        from coordinator_core.warm.election import _configured_sandbox_principals

        names = ", ".join(_configured_sandbox_principals())
    except Exception:  # noqa: BLE001 -- naming the principal never masks the refusal
        names = "CodexSandboxUsers"
    return f"the codex sandbox token ({names}; {_CAPABILITY_SID})"


def ensure_sandbox_scratch(cwd: Path) -> Path:
    """Return `<cwd>/scratch`, creating it and `scratch/warp` plainly (inherited DACL, no ACL call).
    The ready path is one stat and no spawn. Raises `ScratchGrantRefused` when mkdir is refused."""
    root = Path(cwd)
    scratch = root / SCRATCH_DIRNAME
    warp = root / _WARP
    if warp.is_dir():
        return scratch
    try:
        warp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScratchGrantRefused(scratch, _principal_text(), exc) from exc
    return scratch

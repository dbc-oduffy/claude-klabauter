"""Pre-create the directories a sandboxed codex child writes under `<cwd>` before it starts.

Codex stamps its per-root capability SID onto `<cwd>` with an inheriting ACE; a directory created here,
unsandboxed, inherits it. No ACL is written anywhere: the grantable principal already holds inherited
access and the failing one (codex's random per-root capability SID) is not derivable in-process.

The directories: `scratch/warp` (workflow spill files) and the `warp` record home (`state/warp`, the run
record the mint writes). A child that creates either itself is refused WinError 5.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.session.record_homes import home_rel

SCRATCH_DIRNAME = "scratch"
_WARP = Path(SCRATCH_DIRNAME) / "warp"
_PRECREATED = (_WARP, Path(home_rel("warp")))
_CAPABILITY_SID = "per-root capability SID"


class ScratchGrantRefused(RuntimeError):
    """A directory the sandbox needs under `<cwd>` could not be created. `.path` is that directory (the
    `scratch` root for the scratch pair), `.principal` the restricted codex sandbox token that needs it."""

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
    """Return `<cwd>/scratch`, creating `scratch/warp` and `state/warp` plainly (inherited DACL, no ACL
    call). The ready path is a stat per directory and no spawn. Raises `ScratchGrantRefused` when a mkdir
    is refused."""
    root = Path(cwd)
    for rel in _PRECREATED:
        target = root / rel
        if target.is_dir():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ScratchGrantRefused(root / (SCRATCH_DIRNAME if rel == _WARP else rel), _principal_text(), exc) from exc
    return root / SCRATCH_DIRNAME

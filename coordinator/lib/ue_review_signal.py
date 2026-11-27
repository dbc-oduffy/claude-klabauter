"""Decides whether a plan's declared writes touch Unreal Engine code, so the `unreal` review signal is owed."""
from pathlib import Path, PurePosixPath

UNREAL_SIGNAL = "unreal"
_UE_SUFFIXES = (".uplugin", ".uproject")


def _holds(directory: Path, pattern: str) -> bool:
    try:
        return directory.is_dir() and any(directory.glob(pattern))
    except OSError:
        return False


def declares_unreal_engine(repo_root) -> bool:
    """True when `coordinator_core.ue_project_detection.declared_ue` reads `repo_root` as Unreal Engine.

    No declaration or any read failure is False: an unreadable declaration never owes the signal.
    """
    try:
        from coordinator_core.ue_project_detection import declared_ue

        return declared_ue(repo_root) is True
    except Exception:
        return False


def ue_signal_for_writes(writes, repo_root) -> bool:
    """True when any repo-relative write is UE-plugin or .uproject territory.

    Territory is: a `Plugins` path segment (case-insensitive, `\\` normalised), a .uplugin/.uproject
    file, an ancestor directory under `repo_root` holding a *.uplugin (catches a lowercase
    `control/plugin` tree), a `repo_root` that itself holds a *.uproject, or a `repo_root` whose
    UE declaration (`declared_ue`) says unreal.
    """
    root = Path(repo_root)
    if _holds(root, "*.uproject") or declares_unreal_engine(root):
        return True
    for raw in writes or []:
        norm = str(raw).replace("\\", "/")
        parts = [p for p in PurePosixPath(norm).parts if p not in ("/", ".")]
        if not parts:
            continue
        if any(p.lower() == "plugins" for p in parts):
            return True
        if parts[-1].lower().endswith(_UE_SUFFIXES):
            return True
        top = len(parts) if norm.endswith("/") else len(parts) - 1
        for depth in range(top, 0, -1):
            if _holds(root.joinpath(*parts[:depth]), "*.uplugin"):
                return True
    return False

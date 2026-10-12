"""The one read of the version a tree declares.

Not `merge_assemble._declared_package_version`, which reads `pyproject.toml` or
`package.json`: this repo's `pyproject.toml` stayed at 0.1.0 while release tags moved
through v0.6.*, so that file does not follow releases here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.ops.check_version_consistency import locate_surfaces

_NOTES_DIR = "archive/release-notes"
_NOTES_RE = re.compile(r"-v(\d+(?:\.\d+)+)\.md$")
# Each bundle root's marketplace manifest, then the plugin manifests tried under it in order.
_BUNDLES = tuple(
    (
        f"{pre}.claude-plugin/marketplace.json",
        (f"{pre}.claude-plugin/plugin.json", f"{pre}coordinator/.claude-plugin/plugin.json"),
    )
    for pre in ("", "coordinator/", "plugins/coordinator-claude/")
)


def _key(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _highest_note_version(names: List[str]) -> Optional[str]:
    found = []
    for name in names:
        m = _NOTES_RE.search(name.rsplit("/", 1)[-1])
        if m:
            found.append(m.group(1))
    return max(found, key=_key) if found else None


def _manifest_version(text: str) -> Optional[str]:
    try:
        v = json.loads(text).get("version")
    except (ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None


def _git(repo_root: str, args: List[str], stdin: Optional[bytes] = None) -> bytes:
    res = run_git(["-C", repo_root, *args], input=stdin, binary=True)
    return res.stdout_bytes if res.ok else b""


def _cat_file_batch(repo_root: str, treeish: str, paths: List[str]) -> dict:
    """Blob text per path present at `treeish`; one git child."""
    out = _git(
        repo_root,
        ["cat-file", "--batch"],
        "".join(f"{treeish}:{p}\n" for p in paths).encode(),
    )
    blobs, pos = {}, 0
    for p in paths:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            blobs[p] = out[pos : pos + size].decode("utf-8", "replace")
            pos += size + 1
    return blobs


def _declared_at(repo_root: str, treeish: str) -> Optional[str]:
    paths = [p for mk, plugins in _BUNDLES for p in (mk, *plugins)]
    blobs = _cat_file_batch(repo_root, treeish, paths)
    for mk, plugins in _BUNDLES:
        try:
            is_bundle = json.loads(blobs.get(mk, "")).get("name") == "coordinator-claude"
        except (ValueError, AttributeError):
            is_bundle = False
        if not is_bundle:
            continue
        for plugin in plugins:
            if plugin in blobs:
                return _manifest_version(blobs[plugin])
        return None
    listing = _git(repo_root, ["ls-tree", "-r", "-z", "--name-only", treeish, "--", _NOTES_DIR])
    names = [n for n in listing.decode("utf-8", "replace").split("\0") if n]
    return _highest_note_version(names)


def declared_version(repo_root, *, treeish: Optional[str] = None) -> Optional[str]:
    """The version `repo_root` declares, or None.

    With a coordinator-claude bundle: the plugin manifest's version. Without one: the
    highest version named by an `archive/release-notes/*-<tag>.md` file whose name parses
    as a version (a placeholder such as `vX.Y.Z` does not). With `treeish`, the same read
    against that commit via git plumbing (two git children at most) instead of the worktree.
    """
    root = str(repo_root)
    if treeish is not None:
        return _declared_at(root, treeish)
    surfaces = locate_surfaces(root)
    if surfaces is not None:
        try:
            text = Path(surfaces["plugin_json"]).read_text(encoding="utf-8")
        except OSError:
            return None
        return _manifest_version(text)
    notes = Path(root) / _NOTES_DIR
    if not notes.is_dir():
        return None
    return _highest_note_version([p.name for p in notes.iterdir()])

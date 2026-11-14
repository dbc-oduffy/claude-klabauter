"""Doctor layer counting staged paths that sit behind HEAD in every registered repo.

An index-only landing leaves the index (and worktree) at the pre-commit blob for a landed path.
Such an entry is "behind": its blob differs from HEAD's blob for the path yet equals the path's
blob in one of HEAD's first `ANCESTOR_DEPTH` first-parent ancestors. A staged entry at a blob no
ancestor held is new work, never counted.

Zero spawns. A repo whose index cache-tree root equals HEAD's tree is clean without an entry
parse. Otherwise the index is parsed in-process and its directory tree shas are recomputed from
the entries, so only directories whose sha differs from HEAD's subtree are read from the object
store. At most `CANDIDATE_CAP` differing paths per repo are traced through ancestors, so a count
is a floor when more paths differ.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from coordinator_core.ops.doctor import Layer

LAYER_NAME = "Staged paths behind HEAD"
ANCESTOR_DEPTH = 64
CANDIDATE_CAP = 16
_DIR_MODE = 0o40000


def _chain_ends_at_checksum(raw: bytes, start: int) -> bool:
    """True when the extension chain beginning at `start` runs exactly to the trailing checksum."""
    end = len(raw) - 20
    cursor = start
    while cursor + 8 <= end:
        sig = raw[cursor:cursor + 4]
        if not (sig.isalpha() and sig.isupper()):
            return False
        cursor += 8 + struct.unpack(">I", raw[cursor + 4:cursor + 8])[0]
    return cursor == end


def _cache_tree_root(raw: bytes) -> Optional[bytes]:
    """The raw root tree sha of the index's valid TREE extension, else `None`.

    The extension region start is not addressable without walking every entry, so the TREE
    signature is searched from the tail and accepted only when the extension chain from it lands
    exactly on the trailing 20-byte checksum."""
    pos = len(raw)
    for _ in range(8):
        pos = raw.rfind(b"TREE", 12, pos)
        if pos < 0:
            return None
        if not _chain_ends_at_checksum(raw, pos):
            continue
        size = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        data = raw[pos + 8:pos + 8 + size]
        nl = data.find(b"\n")
        if not data.startswith(b"\0") or nl < 0:
            return None
        count = data[1:nl].split(b" ")[0]
        if not count.isdigit() or len(data) < nl + 21:
            return None
        return data[nl + 1:nl + 21]
    return None


def _index_trees(entries: Dict[str, object]) -> Tuple[Dict[str, List[tuple]], Dict[str, List[str]], Dict[str, bytes]]:
    """(files_by_dir, subdirs_by_dir, tree_sha_by_dir) recomputed from index entries; "" is the root.

    A file is `(name, path, mode, sha_hex)`. Tree shas are the raw 20-byte digests git would give
    the tree object written from this index."""
    files: Dict[str, List[tuple]] = {}
    subdirs: Dict[str, List[str]] = {"": []}
    for path, e in entries.items():
        parent, _, name = path.rpartition("/")
        files.setdefault(parent, []).append((name, path, e.mode, e.sha))
    for d in list(files):
        while d and d not in subdirs:
            subdirs[d] = []
            parent, _, name = d.rpartition("/")
            subdirs.setdefault(parent, [])
            subdirs[parent].append(name)
            d = parent
    for d in files:
        subdirs.setdefault(d, [])
    shas: Dict[str, bytes] = {}
    for d in sorted(subdirs, key=lambda x: x.count("/") + 1 if x else 0, reverse=True):
        rows = []
        for name, _path, mode, sha in files.get(d, ()):
            nb = name.encode("utf-8", "surrogateescape")
            rows.append((nb, b"%o %s\0" % (mode, nb) + bytes.fromhex(sha)))
        for name in subdirs[d]:
            nb = name.encode("utf-8", "surrogateescape")
            child = f"{d}/{name}" if d else name
            rows.append((nb + b"/", b"40000 %s\0" % nb + shas[child]))
        rows.sort(key=lambda r: r[0])
        body = b"".join(r[1] for r in rows)
        shas[d] = hashlib.sha1(b"tree %d\0" % len(body) + body).digest()
    return files, subdirs, shas


def _differing(repo: Path, entries: Dict[str, object], head_tree: str) -> List[Tuple[str, str]]:
    """(path, index_sha) for each index entry whose blob is not HEAD's blob at that path."""
    from coordinator_core.git import git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir

    files, subdirs, shas = _index_trees(entries)
    common = resolve_git_common_dir(repo)
    out: List[Tuple[str, str]] = []

    def head_entries(tree_sha: Optional[str]) -> Dict[str, Tuple[int, str]]:
        if tree_sha is None:
            return {}
        got = git_state._read_object(common, tree_sha)
        if got is None or got[0] != "tree":
            raise ValueError(f"unreadable tree {tree_sha}")
        parsed = git_state._parse_tree_entries(got[1])
        if parsed is None:
            raise ValueError(f"malformed tree {tree_sha}")
        return parsed

    def walk(d: str, tree_sha: Optional[str]) -> None:
        head = head_entries(tree_sha)
        for name, path, _mode, sha in files.get(d, ()):
            h = head.get(name)
            if h is not None and h[0] != _DIR_MODE and h[1] == sha:
                continue
            out.append((path, sha))
        for name in subdirs.get(d, ()):
            child = f"{d}/{name}" if d else name
            h = head.get(name)
            if h is not None and h[0] == _DIR_MODE:
                if bytes.fromhex(h[1]) == shas[child]:
                    continue
                walk(child, h[1])
            else:
                walk(child, None)

    if bytes.fromhex(head_tree) == shas[""]:
        return []
    walk("", head_tree)
    return out


_FILE_MODES = (b"100644", b"100755", b"120000", b"160000")


def _tree_entry(payload: bytes, name: bytes) -> Optional[Tuple[bool, str]]:
    """(is_dir, sha_hex) of `name` in a raw tree payload, found by byte search so a large
    directory is never fully parsed; the preceding mode token must be a real git mode."""
    needle = b" " + name + b"\0"
    start = 0
    while True:
        at = payload.find(needle, start)
        if at < 0:
            return None
        tok = payload[max(0, at - 6):at]
        is_dir = tok.endswith(b"40000")
        if is_dir or tok in _FILE_MODES:
            sha_at = at + len(needle)
            return is_dir, payload[sha_at:sha_at + 20].hex()
        start = at + 1


def _blob_at(common: Path, tree_sha: str, path: str) -> Optional[str]:
    """The blob sha at `path` under `tree_sha`, `None` when absent; ValueError on an unreadable tree."""
    from coordinator_core.git import git_state

    parts = [p.encode("utf-8", "surrogateescape") for p in path.split("/")]
    for i, part in enumerate(parts):
        got = git_state._read_object(common, tree_sha)
        if got is None or got[0] != "tree":
            raise ValueError(f"unreadable tree {tree_sha}")
        hit = _tree_entry(got[1], part)
        if hit is None:
            return None
        is_dir, sha = hit
        if i == len(parts) - 1:
            return None if is_dir else sha
        if not is_dir:
            return None
        tree_sha = sha
    return None


def _behind(repo: Path, candidates: List[Tuple[str, str]]) -> List[str]:
    """Candidate paths whose index blob equals the path's blob in a first-parent ancestor of HEAD."""
    from coordinator_core.git import git_state
    from coordinator_core.git.git_dir import resolve_git_common_dir

    common = resolve_git_common_dir(repo)
    head = git_state.head_sha(repo)
    info = git_state.read_commit(repo, head) if head else None
    unresolved = dict(sorted(candidates)[:CANDIDATE_CAP])
    behind: List[str] = []
    cursor = info.parents[0] if info and info.parents else None
    for _ in range(ANCESTOR_DEPTH):
        if cursor is None or not unresolved:
            break
        commit = git_state.read_commit(repo, cursor)
        if commit is None:
            break
        for path in list(unresolved):
            try:
                blob = _blob_at(common, commit.tree, path)
            except ValueError:
                return sorted(behind)
            if blob == unresolved[path]:
                behind.append(path)
                del unresolved[path]
        cursor = commit.parents[0] if commit.parents else None
    return sorted(behind)


def staged_behind(repo: Path) -> Optional[List[str]]:
    """Sorted staged paths of `repo` that sit behind HEAD; `None` when the repo cannot be read."""
    from coordinator_core.git import git_state
    from coordinator_core.git.git_dir import resolve_git_dir
    from coordinator_core.git.git_index import IndexParseError, parse_index_identity

    try:
        head = git_state.head_sha(repo)
        info = git_state.read_commit(repo, head) if head else None
        if info is None:
            return None
        try:
            raw = (resolve_git_dir(repo) / "index").read_bytes()
        except FileNotFoundError:
            return []
        if _cache_tree_root(raw) == bytes.fromhex(info.tree):
            return []
        unmerged: set = set()
        entries = parse_index_identity(repo, with_flags=True, unmerged_out=unmerged)
        entries = {p: e for p, e in entries.items() if not e.intent_to_add}
        candidates = _differing(repo, entries, info.tree)
        return _behind(repo, [c for c in candidates if c[0] not in unmerged])
    except (IndexParseError, OSError, ValueError):
        return None


def staged_behind_layer(repos: Optional[Dict[str, Path]] = None) -> "Layer":
    """ok with one info finding per registered repo holding staged paths behind HEAD; never broken.

    `repos` maps a label to a repo root; omitted, the merged `repos.*` registry is read."""
    from coordinator_core.ops.doctor import Finding, Layer

    if repos is None:
        from coordinator_core.plugin_health.claude_md_handoff_convention import fleet_repos

        repos = fleet_repos()
    findings: List[Finding] = []
    for label, root in sorted(repos.items()):
        root = Path(root)
        if not (root / ".git").exists():
            continue
        paths = staged_behind(root)
        if not paths:
            continue
        sample = paths[:3]
        findings.append(Finding(
            "info",
            f"{label}: {len(paths)} staged path(s) behind HEAD (e.g. {', '.join(sample)})\n"
            f"Fix: git -C {root} reset -q -- {' '.join(sample)}",
        ))
    return Layer(LAYER_NAME, "ok", findings)

"""Contract for `repo.provision_local`: manifest parsing, provision marker, local-only probe.

Shared by the op (writer) and `ask_stage` (reader) so both resolve a provisioned sibling the same
way. Every function here is spawn-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops.fleet._memo_resolver import receiver_em_to_repo_key

OP_KEY = "repo.provision_local"
OP_MODULE = "coordinator_core.ops.sibling_repo_provision"
ROUTE_REMEDY = (
    "Alternative: author the file under this repo, declare it in a sibling-repo manifest, "
    "run repo.provision_local."
)
MARKER_NAME = "coordinator-provision.json"
MANIFEST_SCHEMA = "sibling-repo-manifest"

_REMOTE_SECTION = re.compile(r'^\s*\[remote\s+"', re.MULTILINE | re.IGNORECASE)


class ManifestError(ValueError):
    """A manifest field is malformed; the message names the field."""


@dataclass(frozen=True)
class ManifestFile:
    """One sibling-relative target; exactly one of `source` / `content` is set."""

    path: str
    source: str | None
    content: str | None


@dataclass(frozen=True)
class Manifest:
    name: str
    files: tuple[ManifestFile, ...]
    commit_message: str | None
    adopt: bool = False


def _one_segment(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field}: must be a non-empty string")
    if "/" in value or "\\" in value or value in (".", "..") or ":" in value:
        raise ManifestError(f"{field}: must be one path segment, got {value!r}")
    if value.lower() == ".git" or value != value.strip() or value.endswith("."):
        raise ManifestError(f"{field}: not a usable directory name, got {value!r}")
    return value


def _target_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field}: must be a non-empty string")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute() or ":" in value:
        raise ManifestError(f"{field}: must be sibling-relative, got {value!r}")
    segments = value.replace("\\", "/").split("/")
    if any(s in ("", ".", "..") for s in segments):
        raise ManifestError(f"{field}: empty, '.' or '..' segment in {value!r}")
    if segments[0].lower() == ".git":
        raise ManifestError(f"{field}: may not target .git/, got {value!r}")
    return "/".join(segments)


def parse_manifest(text: str, *, manifest_rel: str) -> Manifest:
    """Parse and validate manifest `text`; `manifest_rel` labels errors. Raises ManifestError."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{manifest_rel}: not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ManifestError(f"{manifest_rel}: not a YAML mapping")
    if doc.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"schema: must be {MANIFEST_SCHEMA!r}, got {doc.get('schema')!r}")
    name = _one_segment(doc.get("name"), "name")
    raw_files = doc.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestError("files: must be a non-empty list")
    files: list[ManifestFile] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_files):
        field = f"files[{i}]"
        if not isinstance(entry, dict):
            raise ManifestError(f"{field}: must be a mapping")
        path = _target_path(entry.get("path"), f"{field}.path")
        if path in seen:
            raise ManifestError(f"{field}.path: duplicate {path!r}")
        seen.add(path)
        has_source, has_content = "source" in entry, "content" in entry
        if has_source == has_content:
            raise ManifestError(f"{field}: needs exactly one of source | content")
        source = content = None
        if has_source:
            source = entry["source"]
            if not isinstance(source, str) or not source.strip():
                raise ManifestError(f"{field}.source: must be a non-empty string")
        else:
            content = entry["content"]
            if not isinstance(content, str):
                raise ManifestError(f"{field}.content: must be a string")
        files.append(ManifestFile(path=path, source=source, content=content))
    message = doc.get("commit_message")
    if message is not None and (not isinstance(message, str) or not message.strip()):
        raise ManifestError("commit_message: must be a non-empty string when present")
    adopt = doc.get("adopt", False)
    if not isinstance(adopt, bool):
        raise ManifestError(f"adopt: must be a bool, got {adopt!r}")
    return Manifest(name=name, files=tuple(files), commit_message=message, adopt=adopt)


def sibling_root(repo_root: str | Path, name: str) -> Path:
    """The sibling directory for `name`: a child of the invoking repo's parent."""
    return Path(repo_root).parent / name


def read_marker(root: str | Path) -> dict | None:
    """The provision marker under `<root>/.git/`, or None (absent, unreadable, or `.git` a file)."""
    marker = Path(root) / ".git" / MARKER_NAME
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def has_remote(root: str | Path) -> bool:
    """True when `<root>/.git/config` carries a `[remote "..."]` section."""
    try:
        text = (Path(root) / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _REMOTE_SECTION.search(text) is not None


def check_assent(repo_root: str | Path, assent: object, name: str) -> str:
    """Validate `<sizing rel path>#<register row id>`: the row must exist and name `name` in its
    `source_text`. Returns the normalized citation; raises ValueError. Re-checked on every call."""
    if not isinstance(assent, str) or assent.count("#") != 1:
        raise ValueError("assent: expected '<sizing path>#<register row id>'")
    sizing_rel, row_id = (part.strip() for part in assent.split("#"))
    if not sizing_rel or not row_id:
        raise ValueError("assent: expected '<sizing path>#<register row id>'")
    from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

    sizing = load_sizing(Path(repo_root), sizing_rel)
    register = sizing.get("requirement_register")
    rows = register.get("rows") if isinstance(register, dict) else None
    row = next(
        (r for r in rows or () if isinstance(r, dict) and str(r.get("id")) == row_id), None
    )
    if row is None:
        raise ValueError(f"assent: no register row {row_id!r} in {sizing_rel}")
    if name not in str(row.get("source_text") or ""):
        raise ValueError(f"assent: row {row_id} in {sizing_rel} does not name {name!r}")
    return f"{sizing_rel}#{row_id}"


def provisioned_target_of(path: str | Path, repo_root: str | Path) -> Path | None:
    """The sibling `<repo_root>.parent/<seg>` containing `path` when it carries the marker.
    File stats only: no spawn, no registry read."""
    child = _sibling_child(path, repo_root)
    if child is not None and read_marker(child) is not None:
        return child
    return None


def remedy_applies(path: str | Path, repo_root: str | Path) -> bool:
    """True when `path` lies under a child of `<repo_root>.parent` that is absent or provisioned."""
    child = _sibling_child(path, repo_root)
    if child is None:
        return False
    return not child.exists() or provisioned_target_of(path, repo_root) is not None


def _sibling_child(path: str | Path, repo_root: str | Path) -> Path | None:
    parent = Path(repo_root).parent
    try:
        rel = Path(path).relative_to(parent)
    except ValueError:
        return None
    if not rel.parts or Path(repo_root).name == rel.parts[0]:
        return None
    return parent / rel.parts[0]


def provisioned_local_root(owner_repo: str) -> Path | None:
    """Registered root of `owner_repo` when it carries a provision marker and still has no remote.
    The marker is trusted as a local file: anyone able to write the sibling's `.git/` can forge it."""
    registered = registry_get(receiver_em_to_repo_key(owner_repo))
    if not registered:
        return None
    root = Path(registered)
    if read_marker(root) is None or has_remote(root):
        return None
    return root

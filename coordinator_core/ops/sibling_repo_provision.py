"""`repo.provision_local`: idempotent create-or-update of a local sibling repo from a manifest.

The target is `<invoking repo root>.parent / <manifest name>`. Git runs as argv-list spawns (no
shell); the repo is never given a remote, and an existing repo is touched only when it carries this
op's marker or the manifest sets `adopt: true`. Spawns: 2 on an identical re-run, 4 on create.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from coordinator_core import machine_resolver
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet import _memo_resolver
from coordinator_core.ops.sibling_repo_provision_contract import (
    MANIFEST_SCHEMA,
    MARKER_NAME,
    OP_KEY,
    Manifest,
    ManifestError,
    check_assent,
    has_remote,
    parse_manifest,
    read_marker,
    sibling_root,
)
from coordinator_core.git.run import GitResult, run_git

_FALLBACK_NAME = "Coordinator"
_FALLBACK_EMAIL = "coordinator@coordinator"
_SECTION = re.compile(r"^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+\"[^\"]*\")?\s*\]\s*(.*)$")
_KEYVAL = re.compile(r"^\s*(name|email)\s*=\s*(.*?)\s*$", re.IGNORECASE)


def _git(args: list[str], *, env: Optional[dict] = None) -> GitResult:
    full_env = {**os.environ, "LC_ALL": "C", **(env or {})}
    return run_with_lock_retry(lambda: run_git(args, env=full_env))


def _git_ok(args: list[str], *, env: Optional[dict] = None) -> GitResult:
    proc = _git(args, env=env)
    if proc.returncode != 0:
        why = "timed out" if proc.timed_out else (proc.stderr or "").strip()
        raise ValueError(f"{OP_KEY}: git {args[0]} failed: {why}")
    return proc


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _load_manifest(params: dict, repo_root: Path) -> tuple[Manifest, str]:
    rel = params.get("manifest")
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError(f"{OP_KEY}: params.manifest is required (invoking-repo-relative path)")
    path = repo_root / rel
    if not _inside(repo_root, path) or not path.is_file():
        raise ValueError(f"{OP_KEY}: params.manifest {rel!r} is not a file inside the invoking repo")
    try:
        manifest = parse_manifest(path.read_text(encoding="utf-8"), manifest_rel=rel)
    except ManifestError as exc:
        raise ValueError(f"{OP_KEY}: {exc}") from exc
    return manifest, rel


def _file_bytes(manifest: Manifest, repo_root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for entry in manifest.files:
        if entry.source is not None:
            src = repo_root / entry.source
            if not _inside(repo_root, src) or not src.is_file():
                raise ValueError(f"{OP_KEY}: source {entry.source!r} is not a file inside the invoking repo")
            out[entry.path] = src.read_bytes()
        else:
            out[entry.path] = (entry.content or "").encode("utf-8")
    return out


def _check_registry(name: str, target: Path) -> tuple[str, Optional[str]]:
    """The registry key for `name` and its current binding; refuses an alias or a foreign binding."""
    shortname = name[:-3] if name.endswith("-em") else name
    if shortname in _memo_resolver.read_receiver_aliases():
        raise ValueError(f"{OP_KEY}: name {name!r} is an alias of another repo")
    key = _memo_resolver.receiver_em_to_repo_key(name)
    bound = machine_resolver.registry_get(key)
    if bound and Path(bound).resolve() != target.resolve():
        raise ValueError(f"{OP_KEY}: registry key {key} is already bound to {bound}")
    return key, bound


def _check_target(target: Path, adopt: bool) -> tuple[bool, bool]:
    """(create, adopting) for `target`; raises on one this op may not touch."""
    if not target.exists():
        return True, False
    if not target.is_dir():
        raise ValueError(f"{OP_KEY}: {target} exists and is not a directory")
    if not (target / ".git").exists():
        if any(target.iterdir()):
            raise ValueError(f"{OP_KEY}: {target} is a non-empty directory without a repo")
        return True, False
    if has_remote(target):
        raise ValueError(f"{OP_KEY}: {target} has a configured remote; provisioning is local-only")
    if read_marker(target) is None:
        if not adopt:
            raise ValueError(
                f"{OP_KEY}: {target} is a repo this op did not provision; set adopt: true in the manifest to admit it"
            )
        return False, True
    return False, False


def _check_invoking_clean(root: Path, paths: list[str]) -> None:
    """The manifest and every source must be tracked and unmodified (a gitignored one is refused)."""
    proc = _git_ok(
        ["-C", str(root), "status", "--porcelain", "--untracked-files=all", "--ignored", "--", *paths]
    )
    if proc.stdout.strip():
        raise ValueError(
            f"{OP_KEY}: manifest and sources must be tracked and unmodified in the invoking repo: "
            + "; ".join(line.strip() for line in proc.stdout.splitlines())
        )


def _dirty_paths(target: Path, paths: list[str]) -> set[str]:
    proc = _git_ok(["-C", str(target), "status", "--porcelain", "-z", "--", *paths])
    return {rec[3:] for rec in proc.stdout.split("\0") if len(rec) > 3}


def _head_sha(target: Path) -> Optional[str]:
    git_dir = target / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        ref_file = git_dir / ref
        if ref_file.is_file():
            return ref_file.read_text(encoding="utf-8").strip()
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


def _user_from_config(path: Path, found: dict[str, str]) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    in_user = False
    for line in text.splitlines():
        section = _SECTION.match(line)
        if section:
            in_user = section.group(1).lower() == "user"
            line = section.group(2)
            if not line:
                continue
        if in_user:
            kv = _KEYVAL.match(line)
            if kv:
                found.setdefault(kv.group(1).lower(), kv.group(2).strip('"'))


def _identity_env(target: Path) -> dict:
    """Commit identity from config text (target, then global), else the bootstrap fallback; no spawn."""
    found: dict[str, str] = {}
    _user_from_config(target / ".git" / "config", found)
    global_cfg = os.environ.get("GIT_CONFIG_GLOBAL")
    candidates = [Path(global_cfg)] if global_cfg else [
        Path.home() / ".gitconfig",
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "config",
    ]
    for cfg in candidates:
        _user_from_config(cfg, found)
    name = found.get("name") or _FALLBACK_NAME
    email = found.get("email") or _FALLBACK_EMAIL
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


@register_op("repo.provision_local")
def _provision_local_op(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Make `<repo_root>.parent/<name>` match the manifest; returns path, created, adopted, changed, commit data."""
    if repo_root is None:
        raise ValueError(f"{OP_KEY}: no repo_root for this request")
    root = Path(repo_root)
    params = params or {}
    manifest, manifest_rel = _load_manifest(params, root)
    assent = check_assent(root, params.get("assent"), manifest.name)
    wanted = _file_bytes(manifest, root)
    target = sibling_root(root, manifest.name)
    create, adopting = _check_target(target, manifest.adopt)
    key, bound = _check_registry(manifest.name, target)

    sources = [e.source for e in manifest.files if e.source is not None]
    _check_invoking_clean(root, list(dict.fromkeys([manifest_rel, *sources])))

    dirty: set[str] = set()
    if not create:
        dirty = _dirty_paths(target, list(wanted))
        for rel in dirty:
            current = target / rel
            if not current.is_file() or current.read_bytes() != wanted.get(rel):
                raise ValueError(f"{OP_KEY}: {rel} has uncommitted changes in {target}")

    if create:
        _git_ok(["init", "-q", str(target)])

    marker = {
        "schema": MANIFEST_SCHEMA,
        "name": manifest.name,
        "manifest": manifest_rel,
        "assent": assent,
        "provisioned_by": OP_KEY,
    }
    if read_marker(target) != marker:
        (target / ".git" / MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    posix = target.resolve().as_posix()
    if bound is None:
        machine_resolver.registry_set(key, posix)

    changed: list[str] = []
    for rel, data in wanted.items():
        dest = target.joinpath(*rel.split("/"))
        if rel in dirty or not dest.is_file() or dest.read_bytes() != data:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            changed.append(rel)

    commit_sha = None
    if changed:
        env = _identity_env(target)
        _git_ok(["-C", str(target), "-c", "core.autocrlf=false", "add", "--", *changed])
        message = manifest.commit_message or f"chore: provision {manifest.name}"
        _git_ok(["-C", str(target), "commit", "-q", "-m", message, "--", *changed], env=env)
        commit_sha = _head_sha(target)

    return {
        "path": posix,
        "created": create,
        "adopted": adopting,
        "changed": changed,
        "committed": bool(changed),
        "commit_sha": commit_sha,
        "registered_key": key,
    }

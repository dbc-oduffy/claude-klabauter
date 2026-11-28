"""Run-delta bundle: HEAD sha plus the run's declared uncommitted delta, and the remote-side apply program."""
from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path, PurePosixPath
from typing import Optional

SCRATCH_KEY = "paths.scratch_root"


def _safe_rel(path: str) -> bool:
    p = PurePosixPath(path.replace("\\", "/"))
    return bool(path) and not p.is_absolute() and ".." not in p.parts and ":" not in path


def _digest(head_sha: str, patch: bytes, files: dict) -> str:
    h = hashlib.sha256()
    h.update(head_sha.encode())
    h.update(b"\0patch\0")
    h.update(patch)
    for path in sorted(files):
        h.update(b"\0file\0" + path.encode() + b"\0")
        h.update(files[path])
    return h.hexdigest()


def bundle(root: Path, run_id: Optional[str]) -> dict:
    """`{"head_sha", "patch", "files", "digest"}`: one `git diff --binary HEAD` over the run's declared paths, plus the bytes of each declared path the patch cannot carry (untracked, or a shared path at HEAD plus only the run's own hunks)."""
    from coordinator_core.ops.test_leg_run import _declared_overlay

    root = Path(root)
    paths, overrides = _declared_overlay(root, run_id)
    paths = [p for p in dict.fromkeys(paths) if _safe_rel(p)]
    overrides = {p: b for p, b in overrides.items() if _safe_rel(p)}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    head_sha = _git(root, ["rev-parse", "HEAD"], flags).decode().strip()
    diff_paths = [p for p in paths if p not in overrides]
    patch = b""
    if diff_paths:
        patch = _git(root, ["diff", "--binary", "HEAD", "--", *diff_paths], flags)
    files: dict = dict(overrides)
    for p in diff_paths:
        header = b"diff --git a/" + p.encode() + b" b/" + p.encode()
        if header in patch:
            continue
        f = root / p
        if f.is_file():
            files[p] = f.read_bytes()
    return {"head_sha": head_sha, "patch": patch, "files": files, "digest": _digest(head_sha, patch, files)}


def _git(root: Path, args: list, flags: int) -> bytes:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, creationflags=flags)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


_APPLY_TEMPLATE = '''\
import base64, io, json, shutil, subprocess, sys, tarfile
from pathlib import Path, PurePosixPath

HEAD_SHA = {head_sha!r}
DIGEST = {digest!r}
PATCH_B64 = {patch_b64!r}
FILES_B64 = {files_b64!r}
KEY = {key!r}


def _safe(rel):
    p = PurePosixPath(rel.replace("\\\\", "/"))
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and ":" not in rel


def resolve_scratch_root(get=None):
    if get is None:
        from coordinator_core import machine_resolver
        get = machine_resolver.registry_get
    raw = get(KEY)
    if not raw:
        raise SystemExit("settings-key-missing: " + KEY)
    return Path(raw)


def main(repo, get=None):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    dest = resolve_scratch_root(get) / ("run-delta-" + DIGEST[:16])
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    arc = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", HEAD_SHA], capture_output=True, creationflags=flags)
    if arc.returncode != 0:
        raise SystemExit("git archive failed: " + arc.stderr.decode(errors="replace"))
    with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tf:
        for m in tf.getmembers():
            if not _safe(m.name) or m.issym() or m.islnk():
                continue
            tf.extract(m, dest)
    patch = base64.b64decode(PATCH_B64)
    if patch:
        ap = subprocess.run(["git", "apply", "--binary", "--whitespace=nowarn", "-"], input=patch, cwd=str(dest), capture_output=True, creationflags=flags)
        if ap.returncode != 0:
            raise SystemExit("git apply failed: " + ap.stderr.decode(errors="replace"))
    for rel, b64 in FILES_B64.items():
        if not _safe(rel):
            raise SystemExit("unsafe path: " + rel)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(b64))
    print(json.dumps({{"export_dir": str(dest), "head_sha": HEAD_SHA, "digest": DIGEST}}))
    return dest


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd())
'''


def apply_script(bundle: dict) -> str:
    """Python program text for the far box: `git archive` the sha into `paths.scratch_root`, apply the patch, write the files; argv[1] is the far repo (default cwd). A missing `paths.scratch_root` exits `settings-key-missing: paths.scratch_root`."""
    return _APPLY_TEMPLATE.format(
        head_sha=bundle["head_sha"],
        digest=bundle["digest"],
        patch_b64=base64.b64encode(bundle["patch"]).decode(),
        files_b64={p: base64.b64encode(b).decode() for p, b in bundle["files"].items()},
        key=SCRATCH_KEY,
    )

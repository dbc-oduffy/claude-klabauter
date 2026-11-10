"""Build-time private-door binding validation. Never an invocation fallback."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import stat

from coordinator_core.session_hub_path import hub_for_common

_FIELDS = {"schema_version", "fire_id", "engine_root", "engine_token", "settings_home", "repo_root", "bindings_path", "server_pid", "server_creation_time_ft", "source_sha", "script_sha256"}

def _path(value: object, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) >= 4096:
        raise ValueError("invalid private path")
    if not re.match(r"^[A-Za-z]:\\", value) or "/" in value:
        raise ValueError("private REQUIRED paths support local drives only")
    path = Path(value)
    if not path.is_absolute() or str(path.resolve(strict=True)) != value:
        raise ValueError("private path must be canonical and existing")
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("private path has reparse ancestry")
    if path.is_dir() != directory or (not directory and not path.is_file()):
        raise ValueError("private path has wrong type")
    return path

def validate_binding(value: dict, *, alias_directory: Path) -> dict:
    """Validate owner-supplied evidence; never mint PID, birth, grants or IDs."""
    if os.name != "nt":
        raise ValueError("private REQUIRED doors are Windows-only")
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("private binding fields differ from schema 1")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported private binding schema")
    for key, length in (("fire_id",32),("engine_token",16),("source_sha",40),("script_sha256",64)):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value[key]):
            raise ValueError("invalid " + key)
    if type(value["server_pid"]) is not int or not 0 < value["server_pid"] <= 0xffffffff:
        raise ValueError("invalid server PID")
    birth = value["server_creation_time_ft"]
    if not isinstance(birth,str) or not re.fullmatch(r"[1-9][0-9]*",birth) or int(birth) > 0xffffffffffffffff:
        raise ValueError("invalid raw server FILETIME")
    root = _path(value["engine_root"],directory=True)
    _path(value["settings_home"],directory=True)
    repo = _path(value["repo_root"],directory=True)
    _path(str(repo / ".git"),directory=True)
    bindings = _path(value["bindings_path"],directory=False)
    expected = hub_for_common(repo / ".git") / "workflow-fires" / "headless" / value["fire_id"]
    if bindings != expected / "worker-bindings.json" or _path(str(alias_directory),directory=True) != expected / "private-bin":
        raise ValueError("private binding is outside its exact fire directory")
    stamp = (root / "coordinator_core" / "_engine_stamp").read_bytes()
    # Token is the existing door derivation, not a new private namespace.
    import hashlib
    if hashlib.sha1(b"engine-stamp:" + stamp).hexdigest()[:16] != value["engine_token"]:
        raise ValueError("engine token does not match published stamp")
    return dict(value)

def write_binding(value: dict, *, alias_directory: Path) -> Path:
    """Write adjacent metadata only after validating actual supplied evidence."""
    row = validate_binding(value,alias_directory=alias_directory)
    sidecar = alias_directory / "door.engine-root.txt"
    if sidecar.read_text(encoding="utf-8").strip() != row["engine_root"]:
        raise ValueError("adjacent engine root differs")
    target = alias_directory / "door.private.json"
    # Exclusive creation avoids silently replacing a lifecycle's binding.
    with target.open("x",encoding="utf-8",newline="\n") as stream:
        json.dump(row,stream,sort_keys=True)
        stream.write("\n")
    return target

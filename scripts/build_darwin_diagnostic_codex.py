"""Build a pinned diagnostic Codex into one host-selected settings profile.

Run on Darwin with --source pointing to a clean dedicated rust-v0.171.0
checkout, --settings-home selecting the launcher profile and --socket-directory
selecting an existing private host directory outside workspace write grants.
The command patches the dedicated checkout. Cargo must have the pinned
dependencies cached. Use --cargo-config for an offline source replacement.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BASE = "ccf9be8cc458bd6b8336fe84d68d8893f2691a5e"
PATCH = Path(__file__).with_name("darwin-private-diagnostics.patch")
# Upstream's release tag changed workspace versions without refreshing Cargo.lock.
RESOLVED_LOCK = "05c5b672b121ec8aac65a2a5d642a6a0a3cb37bfe08d491f62a1808a7c08ad1f"


def build(source, settings, directory, config, *, host_submit=False):
    if sys.platform != "darwin":
        raise SystemExit("darwin-only endpoint")
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise SystemExit("socket directory must be private and host-owned")
    directory = directory.resolve(strict=True)
    if len(os.fsencode(directory / "diagnostic.sock")) >= 104:
        raise SystemExit("socket pathname exceeds 103 bytes")
    git = ["git", "-C", str(source)]
    if (subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip() != BASE
            or subprocess.check_output([*git, "status", "--porcelain", "--untracked-files=all"])):
        raise SystemExit("a clean dedicated checkout of " + BASE + " is required")
    subprocess.run([*git, "apply", str(PATCH)], check=True)
    workspace = source / "codex-rs"
    env = {**os.environ, "CODEX_BUILD_PRIVATE_DIAGNOSTIC_DIRECTORY": str(directory)}
    env.pop("CODEX_BUILD_HOST_SUBMIT_V1", None)
    if host_submit:
        env["CODEX_BUILD_HOST_SUBMIT_V1"] = "1"
    cargo = ["cargo", *(["--config", str(config.resolve())] if config else [])]
    lock = workspace / "Cargo.lock"
    release_lock = lock.read_bytes().replace(b'\nversion = "0.0.0"\n', b'\nversion = "0.171.0"\n')
    if hashlib.sha256(release_lock).hexdigest() != RESOLVED_LOCK:
        raise SystemExit("resolved dependencies differ from the qualified release lock")
    lock.write_bytes(release_lock)
    subprocess.run([*cargo, "build", "--offline", "--locked", "-p", "codex-cli", "--bin", "codex", "-j2"],
                   cwd=workspace, env=env, check=True)
    target = Path(env.get("CARGO_TARGET_DIR", "target"))
    binary = workspace / target / "debug/codex"
    # Publish the executable and its matching compiled socket path as one generation.
    root = settings / ("native-host-submit-v1" if host_submit else "native-diagnostics")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise SystemExit("diagnostic installation directory must be private and host-owned")
    generation = Path(tempfile.mkdtemp(dir=root, prefix="build-"))
    shutil.copy2(binary, generation / "codex")
    if host_submit:
        door = Path(__file__).resolve().parents[1] / "coordinator_core/warm/door"
        subprocess.run(["cc", "-O2", "-std=c11", f'-DPYTHON_BIN="{sys.executable}"', "-o", str(generation / "coordinator-invoke"),
                        str(door / "door_posix.c"), str(door / "door_core.c")], check=True)
    (generation / "socket-directory").write_text(str(directory))
    with tempfile.TemporaryDirectory(dir=root) as staging:
        link = Path(staging) / "current"
        link.symlink_to(generation, target_is_directory=True)
        os.replace(link, root / "current")
    print(json.dumps({"native": str(generation / "codex"), "socket_directory": str(directory), "base": BASE}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("source", "settings-home", "socket-directory"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--cargo-config", type=Path)
    parser.add_argument("--host-submit", action="store_true",
                        help="install the v1 submit client and a native build scoped to submit.sock")
    args = parser.parse_args()
    build(args.source.resolve(), args.settings_home.resolve(), args.socket_directory, args.cargo_config, host_submit=args.host_submit)

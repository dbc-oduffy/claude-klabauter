"""Spike driver for the rebuilt run landing: the cheapest shape that lands a run at the real-session shape.

    python -m coordinator_core.benchmarks.run_landing_spike <dir> [--setup-only | --arm NAME] [--paths N] [--verify]

`SPIKE_PROFILE=1` adds per-phase process-time laps to `spike.json` (they cost time themselves: never a figure of record).

The fixture is the incident's shape (`terminal_commit_real_session_shape`): a session tree of WF_DIRS
`wf_` dirs holding AGENT_METAS settled agent metas, and LANDING_PATHS new product files in a git repo.
`--setup-only` builds it and stops, so a landing's cost is `full - setup-only` through
`process_time.batched_process_time_ms`. Landing modules are imported only inside the landing arm, so
their import closure is part of the net figure. `--verify` checks the result with git spawns, which stay
out of any measured run.

Arms (each lands every product file in one commit, worktree converged, zero git spawns):
  core            the in-process committer `git.commit.commit_paths` called directly with the in-hand path set.
  gated           `core` plus the `leak_gate` import closure and its scan over the same set.
  scratch         one directory scan per landed directory (stat from the DirEntry), one read per file, checkin
                  attributes resolved once per (directory, suffix), one pack, one tree write per changed
                  directory, one index write, one compare-and-swap; no per-path stat, re-read or thread.
  scratch_gate_import  `scratch` plus the `leak_gate` import closure and its wired detectors, no scan.
  scratch_gated   `scratch` plus the `leak_gate` import closure and its scan, fed from the landing's own read
                  buffer and limited to paths whose blob differs from HEAD (never a second read).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home
from coordinator_core.benchmarks import terminal_commit_real_session_shape as shape
from coordinator_core.git.run import run_git

ARMS = ("core", "gated", "scratch", "scratch_gate_import", "scratch_gated")
RESULT_FILE = "spike.json"
PROFILE = bool(os.environ.get("SPIKE_PROFILE"))
PHASES: dict = {}
SESSION_ID = "5e1f0000-0000-4000-8000-0000000000c2"
RUN_ID = "ask-20261114T000000-c20000"
MESSAGE = "spike: land run\n"


def build_fixture(parent: Path, *, wf_dirs: int = shape.WF_DIRS, landing_paths: int = shape.LANDING_PATHS):
    """A seeded repo with `landing_paths` new files under `pkg/` plus the session tree; returns
    `(repo, rels, workflows_dir)`."""
    repo = parent / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
        ["config", "core.autocrlf", "true"],
    ):
        run_git(args, cwd=str(repo))
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("*.cmd text eol=crlf\n", encoding="utf-8")
    run_git(["add", "."], cwd=str(repo))
    run_git(["commit", "-q", "-m", "seed"], cwd=str(repo))
    config_dir = parent / "claude-config"
    session = shape.build_session_tree(config_dir, SESSION_ID, RUN_ID, wf_dirs)
    pkg = repo / "pkg"
    pkg.mkdir()
    rels = []
    for i in range(landing_paths):
        (pkg / f"f{i:05d}.py").write_text(f"v = {i}\n", encoding="utf-8")
        rels.append(f"pkg/f{i:05d}.py")
    return repo, rels, session / "subagents" / "workflows"


def locate_live_dir(workflows: Path) -> str:
    """The run's own `wf_` dir by one scandir (the landing needs no per-dir read of the settled ones)."""
    with os.scandir(workflows) as it:
        for entry in it:
            if entry.name == "wf_live":
                return entry.path
    raise RuntimeError("no live wf_ dir")


def _decode(data):
    return None if data is None else data.decode("utf-8", errors="replace")


def run_leak_gate(repo: Path, rels: list, landed_text=None) -> None:
    from coordinator_core.authoring_leaks import leak_gate

    if landed_text is None:
        def landed_text(rel: str):
            try:
                return (repo / rel).read_text(encoding="utf-8")
            except OSError:
                return None

    outcome = leak_gate(repo, rels, landed_text=landed_text)
    if not outcome.passed:
        raise RuntimeError(f"leak_gate refused the fixture: {outcome.diagnostics[:3]}")


def land_core(repo: Path, rels: list) -> dict:
    from coordinator_core.git.commit import commit_paths

    outcome = commit_paths(repo, rels, MESSAGE)
    return {"sha": outcome.sha, "landed": len(rels)}


_SUFFIX_PATTERN = re.compile(r"^\*(\.[A-Za-z0-9_]+)?$")


def _attr_set_keyed_by_suffix(repo: Path, dirs) -> bool:
    """True when every attribute line that can govern a landed directory names `*` or `*.ext`, so one
    lookup per (directory, suffix) answers for every path in it. A name-specific pattern returns False
    and the caller resolves per path."""
    gitdir = repo / ".git"
    files = [gitdir / "info" / "attributes"]
    for d in dirs:
        parts = d.split("/") if d else []
        files += [repo.joinpath(*parts[:i]) / ".gitattributes" for i in range(len(parts) + 1)]
    for attrs in set(files):
        try:
            text = attrs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            code = line.split("#", 1)[0].split()
            if code and not _SUFFIX_PATTERN.match(code[0]):
                return False
    return True


def _plain_disposition(repo: Path, probe_rel: str):
    """`'auto'` when the attributes leave `core.autocrlf` to decide, `'raw'` when `-text` pins the bytes,
    None when a filter, macro or text pin governs and the path needs the general resolver."""
    from coordinator_core.git import checkin_attrs

    if checkin_attrs.clean_filter_may_apply(repo, probe_rel) is not None:
        return None
    disposition = checkin_attrs.checkin_disposition(repo, probe_rel)
    if disposition == checkin_attrs.UNSET:
        return "auto"
    if disposition == checkin_attrs.BINARY:
        return "raw"
    return None


def _tree_bytes(entries: dict) -> bytes:
    items = sorted(entries.items(), key=lambda kv: kv[0] + "/" if kv[1][0] == 0o40000 else kv[0])
    return b"".join(
        (b"40000" if mode == 0o40000 else oct(mode)[2:].encode("ascii"))
        + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
        for name, (mode, sha) in items
    )


def land_scratch(repo: Path, rels: list, gate: bool = False) -> dict:
    import hashlib
    import struct
    import zlib

    from coordinator_core import dispatch_abandon
    from coordinator_core.git import commit as gc
    from coordinator_core.git import content_hash, git_objects, git_state
    from coordinator_core.git import index_write as iw

    phases: dict = {}
    mark = time.process_time()

    def lap(name: str) -> None:
        nonlocal mark
        if not PROFILE:
            return
        now = time.process_time()
        phases[name] = phases.get(name, 0.0) + (now - mark) * 1000.0
        mark = now

    gitdir = repo / ".git"
    old_head = git_state.head_sha(repo)
    spine = git_state.read_tree_spine(repo, rels)
    if spine is None:
        raise RuntimeError("HEAD spine unreadable")

    pending: dict = {}
    by_dir: dict = {}
    for rel in rels:
        d, _, name = rel.rpartition("/")
        by_dir.setdefault(d, []).append(name)

    index_new: dict = {}
    all_dirs = {""}
    for d in by_dir:
        while d:
            all_dirs.add(d)
            d = d.rpartition("/")[0]
    trees: dict = {d: dict(spine.get(d, {})) for d in all_dirs}
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    autocrlf = gc._autocrlf_once(repo)
    keyed = _attr_set_keyed_by_suffix(repo, by_dir)
    lap("setup")
    kinds: dict = {}
    buffers: dict = {}
    changed: list = []
    for d, names in by_dir.items():
        with os.scandir(os.path.join(str(repo), d)) as it:
            found = {e.name: e for e in it}
        head_dir = spine.get(d, {})
        lap("scandir")
        for name in names:
            entry = found[name]
            st = entry.stat()
            fd = os.open(entry.path, flags)
            try:
                data = os.read(fd, st.st_size + 1)
            finally:
                os.close(fd)
            if len(data) != st.st_size:
                raise RuntimeError(f"{entry.path} changed while it was read")
            lap("read")
            rel = f"{d}/{name}" if d else name
            if b"\r" in data:
                kind = None
                if keyed:
                    suffix = os.path.splitext(name)[1]
                    key = (d, suffix)
                    if key not in kinds:
                        probe = f"{d}/_probe{suffix}" if d else f"_probe{suffix}"
                        kinds[key] = _plain_disposition(repo, probe)
                    kind = kinds[key]
                if kind == "raw":
                    pass
                elif kind == "auto" and autocrlf():
                    data = content_hash._autocrlf_checkin_normalize(data)
                else:
                    data = gc._checkin_bytes(repo, rel, data, autocrlf)
                lap("attrs")
            sha = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
            pending[sha] = (b"blob", data)
            mode = 0o100755 if (st.st_mode & 0o111 and os.name != "nt") else 0o100644
            trees[d][name] = (mode, sha)
            head_entry = head_dir.get(name)
            if head_entry is None or head_entry[1] != sha:
                changed.append(rel)
                buffers[rel] = data
            index_new[rel.encode("utf-8")] = iw._build_entry(rel.encode("utf-8"), mode, sha, st)
            lap("hash")

    if gate:
        run_leak_gate(repo, changed, lambda rel: _decode(buffers.get(rel)))
        lap("gate")

    for d in sorted(trees, key=lambda p: -p.count("/") - (1 if p else 0)):
        payload = _tree_bytes(trees[d])
        sha = hashlib.sha1(b"tree %d\x00" % len(payload) + payload).hexdigest()
        pending[sha] = (b"tree", payload)
        if d:
            parent, _, leaf = d.rpartition("/")
            trees[parent][leaf] = (0o40000, sha)
        else:
            root_tree = sha

    name, email = gc._identity(repo)
    who = f"{name} <{email}> {gc._stamp()}"
    body = f"tree {root_tree}\n" + (f"parent {old_head}\n" if old_head else "") + (
        f"author {who}\ncommitter {who}\n\n{MESSAGE}"
    )
    commit_sha = hashlib.sha1(b"commit %d\x00" % len(body.encode()) + body.encode()).hexdigest()
    pending[commit_sha] = (b"commit", body.encode())
    git_objects._flush_pack(gitdir, pending)

    index_path = gitdir / "index"
    raw = index_path.read_bytes()
    count = struct.unpack_from(">I", raw, 8)[0]
    starts, body_end = iw._walk_entries(raw, count)
    bounds = [*starts, body_end]
    merged = {iw._entry_name(raw, s): raw[s:bounds[i + 1]] for i, s in enumerate(starts)}
    merged.update(index_new)
    head = struct.pack(">4sII", b"DIRC", 2, len(merged))
    digest = hashlib.sha1(head)
    chunks = [head]
    for key in sorted(merged):
        digest.update(merged[key])
        chunks.append(merged[key])
    chunks.append(digest.digest())

    target = gc._cas_target(repo)
    if target is None:
        raise RuntimeError("no CAS target")
    ref_gitdir, ref = target
    if dispatch_abandon.is_abandoned():
        raise RuntimeError("dispatch abandoned before the ref update")
    lock = gitdir / "index.lock"
    fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    prepared = iw.PreparedSplice(lock, index_path)
    try:
        prepared._claim_owner()
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"".join(chunks))
        if not git_objects.cas_ref(ref_gitdir, ref, old_head, commit_sha,
                                   reflog_committer=f"{name} <{email}>", reflog_message="spike: land run",
                                   head_gitdir=gitdir):
            raise RuntimeError("compare-and-swap lost")
    except BaseException:
        prepared.abort()
        raise
    prepared.publish()
    lap("pack_index_cas")
    PHASES.update(phases)
    return {"sha": commit_sha, "landed": len(rels)}


def import_leak_gate() -> None:
    """Import closure of `leak_gate` with its wired detectors resolved, and no scan."""
    from coordinator_core.authoring_leaks import WIRED_DETECTORS, _resolve_default_detectors, leak_gate  # noqa: F401

    _active, failures = _resolve_default_detectors(WIRED_DETECTORS)
    if failures:
        raise RuntimeError(f"leak_gate detectors did not import: {failures}")


def land(arm: str, repo: Path, rels: list, workflows: Path) -> dict:
    from coordinator_core.git.call_io_scope import call_io_scope

    locate_live_dir(workflows)
    with call_io_scope():
        if arm == "scratch_gated":
            return land_scratch(repo, rels, gate=True)
        if arm == "gated":
            run_leak_gate(repo, rels)
        elif arm.endswith("gate_import"):
            import_leak_gate()
        return land_scratch(repo, rels) if arm.startswith("scratch") else land_core(repo, rels)


def verify(repo: Path, out: dict, landed: int) -> bool:
    tip = run_git(["rev-parse", "HEAD"], cwd=str(repo)).stdout.strip()
    status = run_git(["status", "--porcelain", "--untracked-files=no"], cwd=str(repo)).stdout.strip()
    staged = run_git(["diff", "--cached", "--name-only"], cwd=str(repo)).stdout.strip()
    tracked = run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=str(repo)).stdout.split()
    fsck = run_git(["fsck", "--no-dangling", "--no-progress"], cwd=str(repo))
    crlf_blob = run_git(["cat-file", "blob", "HEAD:pkg/f00000.py"], cwd=str(repo)).stdout
    return (
        tip == out["sha"] and status == "" and staged == "" and len(tracked) == landed + 2
        and fsck.returncode == 0 and "\r" not in crlf_blob
    )


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    arm = argv[argv.index("--arm") + 1] if "--arm" in argv else "core"
    paths = int(argv[argv.index("--paths") + 1]) if "--paths" in argv else shape.LANDING_PATHS
    args = [a for a in argv if not a.startswith("--") and a not in (arm, str(paths))]
    if len(args) != 1 or arm not in ARMS:
        print(
            f"usage: run_landing_spike <dir> [--setup-only | --arm {'|'.join(ARMS)}] [--paths N] [--verify]",
            file=sys.stderr,
        )
        return 2
    parent = Path(args[0])
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="spike-", dir=str(parent)))
    try:
        with fixture_settings_home(work):
            repo, rels, workflows = build_fixture(work, landing_paths=paths)
            if "--setup-only" in argv:
                return 0
            started = time.process_time()
            out = land(arm, repo, rels, workflows)
            handler_ms = (time.process_time() - started) * 1000.0
            ok = bool(out["sha"])
            if "--verify" in argv:
                ok = ok and verify(repo, out, len(rels))
            (parent / RESULT_FILE).write_text(
                json.dumps({"arm": arm, "handler_ms": handler_ms, "landed": out["landed"], "ok": ok, "phases": PHASES}),
                encoding="utf-8",
            )
            if not ok:
                print(f"spike did not land cleanly: {out!r}", file=sys.stderr)
                return 1
            return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

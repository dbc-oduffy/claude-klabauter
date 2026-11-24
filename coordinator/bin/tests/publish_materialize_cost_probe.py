"""One-shot cost probe for `publish.py :: _extract_git_archive` (not collected: no `test_` prefix).

Materializes the own-repo scoped pathspec at `--sha` once and prints a JSON line:
python_cpu_s (this process, time.process_time delta), child_cpu_s (CPU of every git child the
call spawned: GetProcessTimes on each child's handle on Windows, RUSAGE_CHILDREN elsewhere;
`child_cpu_method` names which), spawns (Popen count, subprocess.run included), members (files
written), byte_mismatches and mode_mismatches against `git ls-tree -r` of the pathspec. A file whose only
difference from its blob is CRLF (a path's `eol=crlf` attribute) counts in
crlf_smudged_by_eol_attribute, not byte_mismatches.
mode_mismatches is null on Windows, where the exec bit is not on disk.

Run: python coordinator/bin/tests/publish_materialize_cost_probe.py --sha <sha>
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _ChildCpu:
    """Sums child CPU seconds; Windows reads each child's handle, POSIX diffs RUSAGE_CHILDREN."""

    def __init__(self) -> None:
        self.spawns = 0
        self.total = 0.0
        self.method = "GetProcessTimes" if os.name == "nt" else "resource.RUSAGE_CHILDREN"
        self._rusage0 = self._rusage()

    @staticmethod
    def _rusage() -> float:
        if os.name == "nt":
            return 0.0
        import resource  # noqa: PLC0415

        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return r.ru_utime + r.ru_stime

    def harvest(self, proc: subprocess.Popen) -> None:
        if os.name != "nt" or getattr(proc, "_cpu_harvested", False):
            return
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        if proc.returncode is None:
            return
        proc._cpu_harvested = True  # type: ignore[attr-defined]
        ft = [wintypes.FILETIME() for _ in range(4)]
        ok = ctypes.windll.kernel32.GetProcessTimes(  # type: ignore[attr-defined]
            wintypes.HANDLE(int(proc._handle)), *[ctypes.byref(f) for f in ft]  # type: ignore[attr-defined]
        )
        if ok:
            to_s = lambda f: ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7  # noqa: E731
            self.total += to_s(ft[2]) + to_s(ft[3])

    def seconds(self) -> float:
        return self.total if os.name == "nt" else self._rusage() - self._rusage0


def _load_publish():
    sys.path.insert(0, str(_REPO))
    spec = importlib.util.spec_from_file_location("publish_cost_probe_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _install_counter(cpu: _ChildCpu):
    original = subprocess.Popen

    class CountingPopen(original):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            cpu.spawns += 1
            super().__init__(*a, **kw)

        def wait(self, *a, **kw):
            try:
                return super().wait(*a, **kw)
            finally:
                cpu.harvest(self)

        def communicate(self, *a, **kw):
            try:
                return super().communicate(*a, **kw)
            finally:
                cpu.harvest(self)

    subprocess.Popen = CountingPopen  # type: ignore[misc]
    return original


def _verify(toplevel: Path, sha: str, pathspec, shadow: Path) -> dict:
    cmd = ["git", "-C", str(toplevel), "ls-tree", "-r", "-z", sha]
    if pathspec:
        cmd += ["--", *pathspec]
    out = subprocess.run(cmd, capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    members = byte_bad = mode_bad = skipped = crlf_smudged = 0
    first_bad: list[str] = []
    for rec in out.split(b"\0"):
        if not rec:
            continue
        meta, _, rel_b = rec.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        rel = rel_b.decode("utf-8", "surrogateescape")
        if kind != "blob" or mode == "120000":
            skipped += 1
            continue
        path = shadow / rel
        if not path.is_file():
            byte_bad += 1
            first_bad.append(f"missing:{rel}")
            continue
        members += 1
        data = path.read_bytes()
        algo = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        if algo(b"blob %d\0" % len(data) + data).hexdigest() != oid:
            lf = data.replace(b"\r\n", b"\n")
            if lf != data and algo(b"blob %d\0" % len(lf) + lf).hexdigest() == oid:
                crlf_smudged += 1
                continue
            byte_bad += 1
            first_bad.append(f"bytes:{rel}")
        if os.name != "nt" and bool(os.stat(path).st_mode & 0o111) != (mode == "100755"):
            mode_bad += 1
            first_bad.append(f"mode:{rel}")
    return {
        "members": members,
        "skipped_non_blob_or_symlink": skipped,
        "byte_mismatches": byte_bad,
        "crlf_smudged_by_eol_attribute": crlf_smudged,
        "mode_mismatches": None if os.name == "nt" else mode_bad,
        "first_mismatches": first_bad[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", required=True)
    parser.add_argument("--keep", action="store_true", help="leave the shadow in place")
    args = parser.parse_args()

    publish = _load_publish()
    toplevel = _REPO
    sha = subprocess.run(
        ["git", "-C", str(toplevel), "rev-parse", "--verify", f"{args.sha}^{{commit}}"],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    ).stdout.strip()

    cpu = _ChildCpu()
    original_popen = _install_counter(cpu)
    t_cpu = time.process_time()
    t_wall = time.perf_counter()
    try:
        shadow = publish._extract_git_archive(toplevel, sha)
    finally:
        subprocess.Popen = original_popen  # type: ignore[misc]
    python_cpu = time.process_time() - t_cpu
    wall = time.perf_counter() - t_wall
    try:
        pathspec = publish._required_pathspec_for_toplevel(toplevel, sha)
        result = {
            "sha": sha,
            "python_cpu_s": round(python_cpu, 3),
            "child_cpu_s": round(cpu.seconds(), 3),
            "child_cpu_method": cpu.method,
            "spawns": cpu.spawns,
            "wall_s_informational": round(wall, 1),
            **_verify(toplevel, sha, pathspec, shadow),
        }
        print(json.dumps(result))
    finally:
        if not args.keep:
            shutil.rmtree(shadow, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

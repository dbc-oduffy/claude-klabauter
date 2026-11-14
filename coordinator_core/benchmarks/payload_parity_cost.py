"""Cold/warm process-CPU cost of the pre-swap payload parity gate over a real ref pair.

`python coordinator_core/benchmarks/payload_parity_cost.py --json [--dest-ref REF] [--staging-ref REF]`

Materialises the dest ref (default: the newest published source ref in the machine-local publish
provenance record) and the staging ref (default HEAD) with `git archive` under
`cache_dir(repo_root)/payload-parity-bench`, takes `changed_files` as the added/modified/renamed `.py`
files between them (the NEW/UPDATE set publish hands the gate), then runs
`payload_parity.payload_parity_report` twice in one process: cold (memo dir emptied first) and warm.
Clocks are `time.process_time()` deltas, never wall clock. Per-file cost is attributed to each checked
file by the gate's own parse and import-map construction events, so a file the gate skips (a memo
hit) has no per-file row.

The staging tree is a fresh archive, so the repo's serialized token index (whose stamps are
mtime-keyed to the live tree) is not passed to the gate; its presence is reported so the cost is
attributed to the index-absent or index-present path by whoever reads the number.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.percolate import payload_parity as pp
from coordinator_core.win_portability import no_console_creationflags

#: First-party roots the gate indexes (`payload_parity._CHANGED_ONLY_INDEX_ROOTS`).
INDEX_ROOTS: "tuple[str, ...]" = pp._CHANGED_ONLY_INDEX_ROOTS

#: Leaf names under `cache_dir(repo_root)`; the memo leaf is where the gate's persistent memo lives.
BENCH_LEAF = "payload-parity-bench"
MEMO_LEAF = "payload-parity-memo"

PROVENANCE_KEY = "machine-local/publish-provenance.json rows.*.toplevels[<repo_root>]"


class BenchRefusal(RuntimeError):
    """A precondition the harness cannot satisfy; the message names the key or ref at fault."""


def _git(repo_root: Path, args: "Sequence[str]") -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        **no_console_creationflags(),
    )


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def default_dest_ref(repo_root: Path, provenance_path: "Optional[Path]" = None) -> str:
    """Newest sha any published row recorded for `repo_root`'s git toplevel in the publish provenance
    record. Refuses, naming `PROVENANCE_KEY`, when the record or a matching entry is absent."""
    if provenance_path is None:
        from coordinator_core._settings_home import machine_local_dir

        provenance_path = machine_local_dir() / "publish-provenance.json"
    try:
        record = json.loads(provenance_path.read_text(encoding="utf-8"))
        rows = record["rows"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BenchRefusal(f"cannot read {PROVENANCE_KEY} from {provenance_path}: {exc}") from exc
    best: "tuple[str, str]" = ("", "")
    for row in rows.values() if isinstance(rows, dict) else ():
        if not isinstance(row, dict):
            continue
        published = row if row.get("published") else row.get("last_published")
        if not isinstance(published, dict):
            continue
        at = str(published.get("published_at") or published.get("at") or "")
        toplevels = published.get("toplevels")
        if not isinstance(toplevels, dict):
            continue
        for top, sha in toplevels.items():
            if isinstance(sha, str) and _same_path(top, str(repo_root)) and at >= best[0]:
                best = (at, sha)
    if not best[1]:
        raise BenchRefusal(f"no published sha for {repo_root} under {PROVENANCE_KEY} in {provenance_path}")
    return best[1]


def bench_root_for(repo_root: Path) -> Path:
    """`cache_dir(repo_root)/payload-parity-bench`; refuses naming the key when it cannot resolve."""
    from coordinator_core.session import machinery_paths

    try:
        return Path(machinery_paths.cache_dir(str(repo_root))) / BENCH_LEAF
    except Exception as exc:  # noqa: BLE001 - any resolution failure is the same typed refusal
        raise BenchRefusal(f"cannot resolve machinery_paths.cache_dir({repo_root}): {exc}") from exc


def memo_dir_for(repo_root: Path) -> Path:
    """The persistent verdict memo dir the gate reads, `cache_dir(repo_root)/payload-parity-memo`."""
    return bench_root_for(repo_root).parent / MEMO_LEAF


def _roots_at(repo_root: Path, ref: str) -> "list[str]":
    proc = _git(repo_root, ["ls-tree", "--name-only", ref, "--", *INDEX_ROOTS])
    if proc.returncode != 0:
        raise BenchRefusal(f"ref {ref!r} does not resolve in {repo_root}: {proc.stderr.decode(errors='replace').strip()}")
    return [n for n in proc.stdout.decode().split() if n in INDEX_ROOTS]


def materialise(repo_root: Path, ref: str, dest: Path) -> int:
    """`git archive ref` of the index roots into a fresh `dest`, keeping only `.py` members.
    Returns the file count."""
    roots = _roots_at(repo_root, ref)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    proc = subprocess.Popen(
        ["git", "-C", str(repo_root), "archive", "--format=tar", ref, "--", *roots],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **no_console_creationflags(),
    )
    count = 0
    dest_resolved = dest.resolve()
    assert proc.stdout is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for member in tar:
            if not (member.isfile() and member.name.endswith(".py")):
                continue
            target = (dest / member.name).resolve()
            if dest_resolved not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            target.write_bytes(src.read())
            count += 1
    stderr = proc.stderr.read() if proc.stderr else b""
    if proc.wait() != 0:
        raise BenchRefusal(f"git archive {ref} failed: {stderr.decode(errors='replace').strip()}")
    return count


def changed_py_files(repo_root: Path, dest_ref: str, staging_ref: str) -> "list[str]":
    """Added/copied/modified/renamed `.py` paths between the refs: the gate's NEW/UPDATE set."""
    proc = _git(
        repo_root,
        ["diff", "--name-only", "--diff-filter=ACMR", "-z", dest_ref, staging_ref, "--", *INDEX_ROOTS],
    )
    if proc.returncode != 0:
        raise BenchRefusal(f"git diff {dest_ref}..{staging_ref} failed: {proc.stderr.decode(errors='replace').strip()}")
    return sorted(p for p in proc.stdout.decode().split("\0") if p.endswith(".py"))


class _PerFileProbe:
    """Attributes process CPU to each checked file by the gate's own events: a checked file's span
    runs from its `_parse_module` call to the next checked file's, so parse, import-map walk and the
    call loop all land on it. Patches the gate module's names for the duration of one run."""

    def __init__(self) -> None:
        self._parse_at: "dict[int, float]" = {}
        self._starts: "list[tuple[str, float]]" = []
        self.t_begin = 0.0
        self.t_end = 0.0

    def __enter__(self) -> "_PerFileProbe":
        self._orig_parse = pp._parse_module
        self._orig_parse_staged = pp._parse_staged
        self._orig_map = pp._FileImportMap
        probe = self

        def parse(path):  # noqa: ANN001
            t = time.process_time()
            tree = probe._orig_parse(path)
            probe._parse_at[id(tree)] = t
            return tree

        def parse_staged(path, data):  # noqa: ANN001
            # the memo-enabled gate parses checked files here, not through `_parse_module`
            t = time.process_time()
            tree = probe._orig_parse_staged(path, data)
            probe._parse_at[id(tree)] = t
            return tree

        def import_map(tree, module, is_package):  # noqa: ANN001
            last = probe._starts[-1][1] if probe._starts else probe.t_begin
            probe._starts.append((module, max(probe._parse_at.get(id(tree), 0.0), last)))
            return probe._orig_map(tree, module, is_package)

        pp._parse_module = parse
        pp._parse_staged = parse_staged
        pp._FileImportMap = import_map
        self.t_begin = time.process_time()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.t_end = time.process_time()
        pp._parse_module = self._orig_parse
        pp._parse_staged = self._orig_parse_staged
        pp._FileImportMap = self._orig_map

    @property
    def setup_cpu_s(self) -> float:
        return (self._starts[0][1] if self._starts else self.t_end) - self.t_begin

    def per_file(self) -> "list[tuple[str, float]]":
        ends = [s[1] for s in self._starts[1:]] + [self.t_end]
        return [(m, e - s) for (m, s), e in zip(self._starts, ends)]


def run_once(
    dest_dir: Path,
    staging_dir: Path,
    changed_files: "Iterable[str]",
    *,
    rel_root: str = "",
) -> dict:
    """One `payload_parity_report`, timed. Returns cpu_s, setup_cpu_s, per_file, violations, counts."""
    with _PerFileProbe() as probe:
        report = pp.payload_parity_report(dest_dir, staging_dir, rel_root=rel_root, changed_files=tuple(changed_files))
    return {
        "cpu_s": probe.t_end - probe.t_begin,
        "setup_cpu_s": probe.setup_cpu_s,
        "per_file": probe.per_file(),
        "violations": [
            {"call_file": f.call_file, "lineno": f.lineno, "callee": f.callee, "message": f.message}
            for f in report.violations
        ],
        "checked_calls": report.checked_calls,
        "abstained_calls": report.abstained_calls,
    }


def measure_pair(
    dest_dir: Path,
    staging_dir: Path,
    changed_files: "Sequence[str]",
    *,
    memo_dir: "Optional[Path]" = None,
    rel_root: str = "",
    top: int = 20,
    runner: "Callable[..., dict]" = run_once,
) -> dict:
    """Cold run (memo dir emptied), then warm run, over one pair. The JSON-able result."""
    if memo_dir is None:
        cold = runner(dest_dir, staging_dir, changed_files, rel_root=rel_root)
        warm = runner(dest_dir, staging_dir, changed_files, rel_root=rel_root)
    else:
        # The gate resolves its memo from its own checkout; point it at the dir this run empties,
        # so "cold" is cold and a measured repo other than this checkout never reads a stale memo.
        shutil.rmtree(memo_dir, ignore_errors=True)
        original = pp._memo_dir
        pp._memo_dir = lambda: memo_dir
        try:
            cold = runner(dest_dir, staging_dir, changed_files, rel_root=rel_root)
            warm = runner(dest_dir, staging_dir, changed_files, rel_root=rel_root)
        finally:
            pp._memo_dir = original
    cold_by = dict(cold["per_file"])
    warm_by = dict(warm["per_file"])
    names = sorted(cold_by, key=lambda m: -cold_by[m])[:top]
    return {
        "cold_cpu_s": round(cold["cpu_s"], 3),
        "warm_cpu_s": round(warm["cpu_s"], 3),
        "cold_setup_cpu_s": round(cold["setup_cpu_s"], 3),
        "warm_setup_cpu_s": round(warm["setup_cpu_s"], 3),
        "per_file": [
            {
                "module": m,
                "cold_cpu_s": round(cold_by[m], 4),
                "warm_cpu_s": round(warm_by[m], 4) if m in warm_by else None,
            }
            for m in names
        ],
        "checked_files": {"cold": len(cold_by), "warm": len(warm_by)},
        "violations": cold["violations"],
        "violations_match": cold["violations"] == warm["violations"],
        "checked_calls": cold["checked_calls"],
        "abstained_calls": cold["abstained_calls"],
    }


def token_index_present(repo_root: Path) -> bool:
    """True when the repo's serialized token index (`.percolate/token-index.bin`) exists."""
    return (repo_root / ".percolate" / "token-index.bin").is_file()


def bench(repo_root: Path, dest_ref: "Optional[str]" = None, staging_ref: str = "HEAD", *, top: int = 20) -> dict:
    """Materialise both refs, compute the changed set, and `measure_pair` them."""
    dest_ref = dest_ref or default_dest_ref(repo_root)
    root = bench_root_for(repo_root)
    tag_d = dest_ref[:12]
    tag_s = staging_ref[:12]
    dest_dir = root / f"dest-{tag_d}"
    staging_dir = root / f"staging-{tag_s}"
    try:
        n_dest = materialise(repo_root, dest_ref, dest_dir)
        n_stage = materialise(repo_root, staging_ref, staging_dir)
        changed = changed_py_files(repo_root, dest_ref, staging_ref)
        result = measure_pair(dest_dir, staging_dir, changed, memo_dir=memo_dir_for(repo_root), top=top)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    result.update(
        {
            "dest_ref": dest_ref,
            "staging_ref": staging_ref,
            "dest_py_files": n_dest,
            "staging_py_files": n_stage,
            "changed_py_files": len(changed),
            "token_index_present": token_index_present(repo_root),
        }
    )
    return result


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    parser = argparse.ArgumentParser(prog="payload_parity_cost", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-root", default=None, help="repo to measure (default: this checkout)")
    parser.add_argument("--dest-ref", default=None, help="dest ref (default: newest published source ref)")
    parser.add_argument("--staging-ref", default="HEAD", help="staging ref (default HEAD)")
    parser.add_argument("--top", type=int, default=20, help="per-file rows to report (default 20)")
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    args = parser.parse_args(argv)

    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else _repo_root_default()
    try:
        result = bench(repo_root, args.dest_ref, args.staging_ref, top=args.top)
    except BenchRefusal as exc:
        print(f"payload_parity_cost: refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"cold {result['cold_cpu_s']}s  warm {result['warm_cpu_s']}s  "
            f"changed {result['changed_py_files']}  violations {len(result['violations'])}  "
            f"token_index_present {result['token_index_present']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

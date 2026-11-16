"""Parity harness of the declared-blob landing op: replays the last N real landings through `land_blobs` and
compares each landing's tree sha (or refusal kind) with what the commit ledger and run records hold.

    python coordinator_core/benchmarks/land_blobs_parity.py --last 20 [--source-repo R] [--json]

A landing is a ledger commit carrying a `Lands-Chunks:` trailer. Its run is named by the `Inline-Review:`
trailer's stem; its record is `scratch/warp/<run>/commit-request.txt` (the declared paths) plus, when present,
`land-params.json` / `terminal-commit-params.json` (the review params) and `state/warp/<run>/kept.json` (the
recorded set). Each replay builds a throwaway repo whose objects are the source's (alternates), whose HEAD and
index are the landing's first parent, and whose worktree holds only the declared paths at the landing commit's
bytes; the op then lands them. A replay also checks the worktree-sync case: each landed path's worktree equals
the new HEAD, or is reported rebased; a refused sync or an unreported difference is a miss. Exit 2 on any miss,
or when fewer than `--last` landings could be replayed.

Parity classes (the product set is the run's declared writes):
  match             the replayed tree sha equals the recorded one.
  match-bookkeeping the declared paths are identical; the recorded commit also carries undeclared bookkeeping
                    paths (`state/warp/<run>/`, `docs/delivery/`, close-job records), listed in `bookkeeping`.
                    Passes; counted in its own column.
  improved          the op refused with `foreign-hunk` where the recorded legacy landing committed, and every
                    refused path is declared by another run (`foreign_mapping`): the legacy landing swept a
                    sibling's hunk. Passes; class `legacy-foreign-hunk`.
  miss              anything else, including a foreign refusal no other run's declared writes explain.

Limits: the replay sees no peer run's record, so a foreign hunk is refused only against the run's own
`kept.json`; a recorded refusal has no commit to rebuild from, so refusals are compared through `compare`
(the harness test pins that), and a landing whose declared mode is not a regular file is skipped by name.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

MATCH = "match"
MATCH_BOOKKEEPING = "match-bookkeeping"
IMPROVED = "improved"
MISS = "miss"
PASSING = (MATCH, MATCH_BOOKKEEPING, IMPROVED)
LEGACY_FOREIGN_HUNK = "legacy-foreign-hunk"
DEFAULT_LAST = 20
#: Ledger commits scanned per requested landing before the harness reports a shortfall.
SCAN_FACTOR = 8
REPLAY_BRANCH = "refs/heads/replay"
REGULAR_MODES = ("100644", "100755")
LS_TREE_BATCH = 100
MAX_DIFF_PATHS = 10
#: Params the op must not receive from a recorded call: a replay claims no token and runs the product mode.
DROPPED_PARAMS = ("run_token", "ledger_verdict", "doc_paths", "run_outcome", "halted_by", "fanout", "grind")
FIXTURE_CONFIG = (
    "[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n\tlogallrefupdates = true\n"
    "\tautocrlf = false\n[user]\n\tname = parity\n\temail = parity@example.invalid\n[commit]\n\tgpgsign = false\n"
)

_TRAILER_CHUNKS = re.compile(r"^Lands-Chunks:", re.MULTILINE)
_TRAILER_REVIEW = re.compile(r"^Inline-Review: applies (\S+) -- execute-review: (\d+) slices?, (\d+) fix", re.MULTILINE)
_TRAILER_BASE = re.compile(r"^Checkpoint-Run-Closed: ([0-9a-f]{40})\s*$", re.MULTILINE)
_STEM_RUN = re.compile(r"^(.+?)\.review")


@dataclass
class Landing:
    """One landing of the commit ledger."""

    sha: str
    parent: str
    tree: str
    run_id: str
    stem: str
    slices: int
    fixes: int
    run_base: Optional[str]
    subject: str


@dataclass
class Result:
    """One replayed landing: `status` is one of `PASSING` or `MISS`; `recorded` and `replayed` are outcome dicts."""

    run_id: str
    sha: str
    status: str
    reason: str
    recorded: dict
    replayed: dict
    differing_paths: list = field(default_factory=list)
    #: The bookkeeping column: differing paths the run never declared (they rode the recorded commit);
    #: `declared_match` is True when no declared path differs.
    bookkeeping: list = field(default_factory=list)
    declared_match: bool = False
    #: Class of an `improved` landing, and `{path: [other runs declaring it]}` that proves it.
    improved_class: str = ""
    foreign_mapping: dict = field(default_factory=dict)
    #: The worktree-sync case: landed paths left carrying rebased dirt, paths the sync refused, and landed paths
    #: whose worktree differs from HEAD with neither report (`worktree_gap`). Any refusal or gap is a miss.
    worktree_rebased: list = field(default_factory=list)
    worktree_sync_refused: list = field(default_factory=list)
    worktree_gap: list = field(default_factory=list)


@dataclass
class Skip:
    """A ledger landing the harness could not replay, with the reason."""

    run_id: str
    sha: str
    reason: str


@dataclass
class Report:
    last: int
    results: list = field(default_factory=list)
    skipped: list = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def matched(self) -> int:
        """Landings that pass parity: strict, bookkeeping-only and improved alike."""
        return sum(1 for r in self.results if r.status in PASSING)

    @property
    def ok(self) -> bool:
        return len(self.results) == self.last and self.matched == self.last

    def summary(self) -> str:
        return f"{self.matched}/{self.last}"

    def breakdown(self) -> dict:
        return {"strict": self.count(MATCH), "bookkeeping_only": self.count(MATCH_BOOKKEEPING),
                "improved": self.count(IMPROVED), "miss": self.count(MISS)}

    def as_json(self) -> dict:
        return {"last": self.last, "replayed": len(self.results), "matched": self.matched, "summary": self.summary(),
                "ok": self.ok, "breakdown": self.breakdown(), "results": [asdict(r) for r in self.results],
                "skipped": [asdict(s) for s in self.skipped]}


# --- git plumbing (harness side; the op under test spawns nothing by default) -----------------------------


def _git(args: list, cwd: Path, *, stdin: Optional[bytes] = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k == "GIT_CONFIG_GLOBAL"}
    proc = subprocess.run(["git", *args], cwd=str(cwd), input=stdin, capture_output=True, env=env,
                          **no_console_creationflags())
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {cwd}: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def _common_dir(source_root: Path) -> Path:
    out = _git(["rev-parse", "--git-common-dir"], source_root).decode().strip()
    path = Path(out)
    return path if path.is_absolute() else (source_root / path).resolve()


# --- the ledger ------------------------------------------------------------------------------------------


def run_of(stem: str) -> str:
    """The run id a review stem names: the text before `.review`, else the whole stem."""
    match = _STEM_RUN.match(stem)
    return match.group(1) if match else stem


def select_landings(source_root: Path, limit: int) -> list:
    """Up to `limit` newest `Landing`s on HEAD, newest first: one `git log` over commits carrying `Lands-Chunks:`."""
    fmt = "%H%x1f%P%x1f%T%x1f%s%x1f%B%x1e"
    raw = _git(["log", f"--max-count={limit}", "--grep=^Lands-Chunks:", f"--format={fmt}", "HEAD"], source_root)
    out: list = []
    for block in raw.decode("utf-8", "replace").split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        sha, parents, tree, subject, body = block.split("\x1f", 4)
        parent = parents.split()[0] if parents.split() else ""
        review = _TRAILER_REVIEW.search(body)
        base = _TRAILER_BASE.search(body)
        if not parent or not _TRAILER_CHUNKS.search(body):
            continue
        stem = review.group(1) if review else ""
        out.append(Landing(sha=sha, parent=parent, tree=tree, run_id=run_of(stem) if stem else "", stem=stem,
                           slices=int(review.group(2)) if review else 1, fixes=int(review.group(3)) if review else 0,
                           run_base=base.group(1) if base else None, subject=subject))
    return out


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def recorded_params(source_root: Path, landing: Landing) -> dict:
    """The op params the run recorded (`land-params.json`, else `terminal-commit-params.json`), else a minimum
    rebuilt from the landing's own trailers; the run's record fields never reach the op."""
    scratch = source_root / "scratch" / "warp" / landing.run_id
    params = _read_json(scratch / "land-params.json") or _read_json(scratch / "terminal-commit-params.json")
    if not isinstance(params.get("inline_review"), dict):
        params["inline_review"] = {"integration_stem": landing.stem or "parity-stem", "slices": landing.slices,
                                   "fixes": landing.fixes}
    for key in DROPPED_PARAMS:
        params.pop(key, None)
    params["run_id"] = landing.run_id
    params["script_path"] = "run.mjs"
    params["run_base_sha"] = landing.run_base or landing.parent
    params.setdefault("incomplete_chunks", [])
    return params


# --- one replay ------------------------------------------------------------------------------------------


def _ls_tree(source_root: Path, rev: str, paths: list) -> dict:
    """`{path: (mode, blob sha)}` of the declared `paths` present at `rev`; one `ls-tree` per batch."""
    found: dict = {}
    for i in range(0, len(paths), LS_TREE_BATCH):
        raw = _git(["--literal-pathspecs", "ls-tree", "-z", rev, "--", *paths[i:i + LS_TREE_BATCH]], source_root)
        for entry in raw.decode("utf-8", "surrogateescape").split("\0"):
            if not entry:
                continue
            meta, _, name = entry.partition("\t")
            mode, _kind, sha = meta.split(" ")
            found[name] = (mode, sha)
    return found


def _blobs(source_root: Path, shas: list) -> dict:
    """`{sha: bytes}` through one `cat-file --batch`."""
    if not shas:
        return {}
    raw = _git(["cat-file", "--batch"], source_root, stdin=("\n".join(shas) + "\n").encode())
    out: dict = {}
    pos = 0
    while pos < len(raw):
        end = raw.index(b"\n", pos)
        sha, kind, size = raw[pos:end].decode().split(" ")
        pos = end + 1
        out[sha] = raw[pos:pos + int(size)]
        pos += int(size) + 1
    return out


def _declared_paths(marker_text: str, incomplete: set) -> list:
    """Landing paths of the marker, as the op counts them: the done chunks' declared paths, normalised."""
    from coordinator_core.ops.dispatch_emit import run_landing as rl
    from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

    request = parse_marker(marker_text)
    paths: dict = {}
    for chunk in request.chunks:
        if chunk.id in incomplete:
            continue
        for p in chunk.paths:
            if p:
                paths[rl._norm(p)] = None
    return list(paths)


def _own_the_parent_blobs(fixture: Path, blobs: dict) -> None:
    """Write the parent's declared blobs into the fixture's own pack: the worktree sync reads its base blob
    through the process's object reader, which does not follow the fixture's `alternates`."""
    from coordinator_core.git import git_objects

    if blobs:
        git_objects._flush_pack(fixture / ".git", {sha: (b"blob", body) for sha, body in blobs.items()})


def build_fixture(source_root: Path, landing: Landing, fixture: Path, marker_text: str, params: dict) -> Optional[str]:
    """Build the replay repo at `fixture`: the landing's parent as HEAD and index, the declared paths at the
    landing commit's bytes in the worktree, the run's script and `kept.json`. Returns a skip reason, else None."""
    paths = _declared_paths(marker_text, set(params.get("incomplete_chunks") or []))
    at_commit = _ls_tree(source_root, landing.sha, paths)
    irregular = sorted(p for p, (mode, _sha) in at_commit.items() if mode not in REGULAR_MODES)
    if irregular:
        return f"declared path is not a regular file: {irregular[0]}"
    fixture.mkdir(parents=True)
    _git(["init", "-q"], fixture)
    gitdir = fixture / ".git"
    (gitdir / "config").write_text(FIXTURE_CONFIG, encoding="utf-8")
    (gitdir / "HEAD").write_text(f"ref: {REPLAY_BRANCH}\n", encoding="utf-8")
    alternates = gitdir / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_bytes((str(_common_dir(source_root) / "objects").replace("\\", "/") + "\n").encode("utf-8"))
    _git(["update-ref", REPLAY_BRANCH, landing.parent], fixture)
    _git(["read-tree", landing.parent], fixture)
    at_parent = _ls_tree(source_root, landing.parent, paths)
    data = _blobs(source_root, sorted({sha for _mode, sha in (*at_commit.values(), *at_parent.values())}))
    _own_the_parent_blobs(fixture, {sha: data[sha] for _mode, sha in at_parent.values()})
    for rel, (_mode, sha) in at_commit.items():
        target = fixture / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data[sha])
    (fixture / "run.mjs").write_text(marker_text.rstrip("\n") + "\n", encoding="utf-8")
    kept = source_root / "state" / "warp" / landing.run_id / "kept.json"
    if kept.is_file():
        rdir = fixture / "state" / "warp" / landing.run_id
        rdir.mkdir(parents=True)
        shutil.copyfile(kept, rdir / "kept.json")
    return None


def outcome_of(reply: dict, common: Path) -> dict:
    """The comparable outcome of an op reply: landed (with the commit's tree sha), refused, nothing to land, error."""
    if reply.get("committed"):
        from coordinator_core.git import git_objects

        stored = git_objects.read_object(common, reply["sha"])
        tree = ""
        if stored is not None and stored[0] == "commit":
            head = stored[1].split(b"\n", 1)[0].decode()
            tree = head.split(" ", 1)[1] if head.startswith("tree ") else ""
        return {"kind": "landed", "tree": tree}
    if reply.get("refused"):
        return {"kind": "refused", "refused": reply["refused"]}
    if reply.get("nothing_to_commit"):
        return {"kind": "nothing-to-commit"}
    return {"kind": "error", "error": str(reply.get("error", ""))[:300]}


def worktree_sync_state(fixture: Path, reply: dict) -> dict:
    """The worktree-sync case of a replayed landing: after a commit, each landed path's worktree must equal HEAD
    (`git diff HEAD`), or be reported in `worktree_rebased`, or in `worktree_sync_refused`; a path differing with
    neither report is a `gap`. Empty lists for a reply that did not commit."""
    rebased = list(reply.get("worktree_rebased") or [])
    refused = list(reply.get("worktree_sync_refused") or [])
    gap: list = []
    landed = list(reply.get("changed_paths") or []) if reply.get("committed") else []
    for i in range(0, len(landed), LS_TREE_BATCH):
        raw = _git(["--literal-pathspecs", "diff", "--name-only", "-z", "HEAD", "--", *landed[i:i + LS_TREE_BATCH]],
                   fixture)
        gap += [p for p in raw.decode("utf-8", "surrogateescape").split("\0")
                if p and p not in rebased and p not in refused]
    return {"rebased": rebased, "refused": refused, "gap": gap}


def worktree_miss_reason(state: dict) -> str:
    """Why a landing whose tree matched still misses on the worktree sync; `""` when it does not."""
    if state["refused"]:
        return f"worktree sync refused: {', '.join(state['refused'][:MAX_DIFF_PATHS])}"
    if state["gap"]:
        return f"landed path(s) differ from HEAD in the worktree, unreported: {', '.join(state['gap'][:MAX_DIFF_PATHS])}"
    return ""


def compare(recorded: dict, replayed: dict) -> tuple:
    """`(match, reason)` of a replayed outcome against the recorded one. A refusal where the recording landed is
    a miss that names the refusal kind; a landing where the recording refused is a miss too."""
    rk, pk = recorded["kind"], replayed["kind"]
    if rk == "landed" and pk == "landed":
        if recorded["tree"] == replayed["tree"]:
            return True, "same tree sha"
        return False, f"tree differs: recorded {recorded['tree']}, replayed {replayed['tree']}"
    if rk == "refused" and pk == "refused":
        if recorded["refused"] == replayed["refused"]:
            return True, f"same refusal kind {recorded['refused']}"
        return False, f"refusal kind differs: recorded {recorded['refused']}, replayed {replayed['refused']}"
    if rk == "landed" and pk == "refused":
        return False, f"op refused ({replayed['refused']}) where the recorded landing committed"
    if rk == "refused" and pk == "landed":
        return False, f"op landed where the recorded landing refused ({recorded['refused']})"
    return False, f"recorded {rk}, replayed {pk}" + (f": {replayed.get('error')}" if pk == "error" else "")


def _run_declares(run_dir: Path, paths: set) -> set:
    """The `paths` another run declares: its `commit-request.txt` marker, else (or also) its `kept.json` chunks.
    A cheap substring test gates each parse."""
    found: set = set()
    marker = run_dir / "commit-request.txt"
    try:
        text = marker.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if text and any(p in text for p in paths):
        try:
            found |= paths & set(_declared_paths(text, set()))
        except Exception:  # noqa: BLE001 -- an unparseable record maps nothing
            pass
    return found


def foreign_mapping(source_root: Path, run_id: str, reply: dict) -> dict:
    """`{path: [other runs declaring it]}` for each path a `foreign-hunk` refusal names. The runs the refusal
    already names are tried first; a path still unmapped is looked up across every other run's record
    (`scratch/warp/<run>/commit-request.txt`, `state/warp/<run>/kept.json`). A path no other run declares is
    absent from the result, so a refusal is explained only when every foreign path appears."""
    from coordinator_core.ops.dispatch_emit import land_blobs

    paths = set(reply.get("foreign_paths") or [])
    named = [r for r in (reply.get("foreign_runs") or []) if r != run_id]
    for hunks in (reply.get("foreign_hunks") or {}).values():
        named += [h.get("owner") for h in hunks if h.get("owner") not in (None, "unknown", run_id)]
    mapping: dict = {}

    def probe(names) -> None:
        todo = paths - set(mapping)
        for name in names:
            if not todo:
                return
            if name == run_id:
                continue
            hit = _run_declares(source_root / "scratch" / "warp" / name, todo)
            kept = land_blobs._chunk_blobs(source_root / "state" / "warp" / name)
            hit |= {p for p in todo if p in kept}
            for path in hit:
                mapping.setdefault(path, []).append(name)
            todo = paths - set(mapping)

    probe(dict.fromkeys(n for n in named if n))
    if paths - set(mapping):
        everyone: dict = {}
        for base in ("scratch/warp", "state/warp"):
            try:
                everyone.update(dict.fromkeys(e.name for e in os.scandir(source_root / base) if e.is_dir()))
            except OSError:
                pass
        probe(sorted(everyone))
    return {p: sorted(set(mapping[p])) for p in sorted(mapping)}


def replay(source_root: Path, landing: Landing, workdir: Path) -> object:
    """Replay one landing through `dispatch.land_blobs`: a `Result`, or a `Skip` when it cannot be rebuilt.

    A tree differing only in undeclared bookkeeping paths is `match-bookkeeping`; a `foreign-hunk` refusal of a
    landing the recorded run committed is `improved` when `foreign_mapping` explains every refused path."""
    marker_file = source_root / "scratch" / "warp" / landing.run_id / "commit-request.txt"
    if not landing.run_id:
        return Skip("", landing.sha, "the commit carries no Inline-Review stem to name its run")
    if not marker_file.is_file():
        return Skip(landing.run_id, landing.sha, "no scratch/warp/<run>/commit-request.txt record")
    marker_text = marker_file.read_text(encoding="utf-8")
    params = recorded_params(source_root, landing)
    fixture = workdir / f"fx-{landing.sha[:10]}"
    try:
        skip = build_fixture(source_root, landing, fixture, marker_text, params)
        if skip:
            return Skip(landing.run_id, landing.sha, skip)
        from coordinator_core.ops.dispatch_emit import land_blobs

        reply = land_blobs._handler(dict(params), repo_root=fixture / ".git")
        replayed = outcome_of(reply, fixture / ".git")
        recorded = {"kind": "landed", "tree": landing.tree}
        match, reason = compare(recorded, replayed)
        status = MATCH if match else MISS
        diff: list = []
        outside: list = []
        declared_match = match
        improved_class, mapping = "", {}
        if not match and replayed["kind"] == "landed":
            raw = _git(["diff-tree", "-r", "--name-only", "-z", replayed["tree"], landing.tree], fixture)
            diff = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
            declared = set(_declared_paths(marker_text, set(params.get("incomplete_chunks") or [])))
            outside = [p for p in diff if p not in declared]
            declared_match = len(outside) == len(diff)
            if diff:
                shown = ", ".join(diff[:MAX_DIFF_PATHS]) + (f" (+{len(diff) - MAX_DIFF_PATHS} more)"
                                                          if len(diff) > MAX_DIFF_PATHS else "")
                kind = ("declared paths identical; recorded commit also carries undeclared path(s)"
                        if declared_match else "declared path(s) differ")
                reason += f"; {kind}: {shown}"
            if declared_match and diff:
                status, reason = MATCH_BOOKKEEPING, f"product set identical; {reason}"
        elif not match and replayed.get("refused") == "foreign-hunk" and recorded["kind"] == "landed":
            mapping = foreign_mapping(source_root, landing.run_id, reply)
            foreign = list(reply.get("foreign_paths") or [])
            if foreign and all(p in mapping for p in foreign):
                status, improved_class = IMPROVED, LEGACY_FOREIGN_HUNK
                reason = (f"{LEGACY_FOREIGN_HUNK}: refused hunk maps to other runs' declared writes: "
                          + "; ".join(f"{p} <- {', '.join(mapping[p])}" for p in foreign[:MAX_DIFF_PATHS]))
            else:
                unmapped = [p for p in foreign if p not in mapping]
                reason += (f"; foreign paths: {', '.join(foreign[:5])}; runs: {reply.get('foreign_runs')}; "
                           f"no other run declares: {', '.join(unmapped[:5]) or '(no path named)'}")
        sync = worktree_sync_state(fixture, reply)
        sync_reason = worktree_miss_reason(sync)
        if status != MISS and sync_reason:
            status, reason = MISS, f"landed but {sync_reason}"
        return Result(landing.run_id, landing.sha, status, reason, recorded, replayed, diff, outside, declared_match,
                      improved_class, mapping, sync["rebased"], sync["refused"], sync["gap"])
    finally:
        _rmtree(fixture)


def _rmtree(path: Path) -> None:
    """`rmtree` through the hook keyword the running Python spells (`onexc` from 3.12, `onerror` before)."""
    shutil.rmtree(path, **({"onexc": _force_remove} if sys.version_info >= (3, 12) else {"onerror": _force_remove}))


def _force_remove(func, path, _exc) -> None:
    """`rmtree` hook: pack and object files are read-only on Windows."""
    os.chmod(path, 0o700)
    func(path)


def run_parity(source_root: Path, last: int, workdir: Path) -> Report:
    """Replay the newest `last` replayable landings of `source_root`'s HEAD; skipped landings are named, never dropped."""
    report = Report(last=last)
    for landing in select_landings(source_root, last * SCAN_FACTOR):
        if len(report.results) >= last:
            break
        outcome = replay(source_root, landing, workdir)
        (report.results if isinstance(outcome, Result) else report.skipped).append(outcome)
    return report


def render(report: Report) -> str:
    b = report.breakdown()
    lines = [f"parity {report.summary()} (replayed {len(report.results)}, skipped {len(report.skipped)}; "
             f"strict {b['strict']}, bookkeeping-only {b['bookkeeping_only']}, "
             f"improved {b['improved']}, miss {b['miss']})"]
    for r in report.results:
        lines.append(f"  {r.status:17} {r.run_id} {r.sha[:10]}  {r.reason}")
        if r.bookkeeping:
            lines.append(f"    bookkeeping: {', '.join(r.bookkeeping[:MAX_DIFF_PATHS])}")
    for s in report.skipped:
        lines.append(f"  skip  {s.run_id or '-'} {s.sha[:10]}  {s.reason}")
    return "\n".join(lines)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description="Replay the last N landings through dispatch.land_blobs.")
    ap.add_argument("--last", type=int, default=DEFAULT_LAST, metavar="N", help="landings to replay (default 20)")
    ap.add_argument("--source-repo", default=str(_REPO), help="repo whose HEAD ledger and run records are replayed")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)
    if args.last < 1:
        ap.error("--last must be >= 1")
    from coordinator_core.benchmarks import declare_benchmark_origin, fixture_settings_home

    declare_benchmark_origin()
    source = Path(args.source_repo).resolve()
    scratch = source / "scratch"
    scratch.mkdir(exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="land-blobs-parity-", dir=str(scratch)))
    try:
        with fixture_settings_home(workdir):
            report = run_parity(source, args.last, workdir)
    finally:
        _rmtree(workdir)
    print(json.dumps(report.as_json(), indent=2) if args.json else render(report))
    return 0 if report.ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

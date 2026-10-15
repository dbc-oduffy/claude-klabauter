"""Fixture and cold-CLI driver for `coordinator/bin/workweek-complete-doc-staleness.py :: main`.

`--build` creates, once, a repo under a caller-given directory (later invocations reuse it):
HISTORY_COMMITS commits via one `git fast-import` spawn, because the op's cost is
history-depth-sensitive work a one-commit repo cannot show. The fixture follows
docs/research/2026-10-14-doc-staleness-rebuild-spike.md § Q5: README.md's last authored touch is
README_COMMITS_BACK commits behind HEAD with a sweep drive-by commit after it, and INSTALL.md's is
INSTALL_COMMITS_BACK behind, made under its old name and followed by an R100 rename and a sweep
commit, so the op's rename fallback runs. Both docs are the fleet defaults (no registry override).
README lands stale under the registry's default commit threshold.

Merges follow this repo's ratio (about 0.94 percent): one-commit side branches merged the next
commit, plus one long branch forking before README's touch and merging after it, so README's
commits-since (README_EXPECTED_COMMITS_SINCE) exceeds its topological level gap. The commit-graph is
a three-layer split chain without Bloom filters, as this repo carries it.

A plain run calls the trampoline's `main(["--root", fixture])` in-process, cold: a control, not the
gated basis (the standing gate drives the installed door against a warm engine and imports
`ensure_fixture` and `check_report` from here). `--setup-only` stops before the trampoline imports
`coordinator_core.ops.doc_staleness`.

    python -m coordinator_core.benchmarks.doc_staleness_full_path <dir> [--build | --setup-only]
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.workstream_complete_brief_full_path import STAMP_REL, _git
from coordinator_core.git.run import run_git

HISTORY_COMMITS = 40000  # near the real repo's 43520
README_COMMITS_BACK = 8000  # spike § Q5: this repo's registered docs' last authored touches sit 8000 and 4000 back
INSTALL_COMMITS_BACK = 4000
DOC_LINES = 20  # above ops.doc_staleness.DEFAULT_SWEEP_LINES_THRESHOLD (15)
SWEEP_FILES = 12  # above ops.doc_staleness.DEFAULT_SWEEP_FILES_THRESHOLD (10)
SEED_FILES = 12  # the seed commit touches more files than the sweep threshold too
AREAS = ("src", "docs", "state", "tools")
EXPECTED_DOCS = ("README.md", "INSTALL.md")
ENTRYPOINT = "workweek-complete-doc-staleness"
_BIN_SCRIPT = ("coordinator", "bin", f"{ENTRYPOINT}.py")

_README_TOUCH = HISTORY_COMMITS - README_COMMITS_BACK
_INSTALL_TOUCH = HISTORY_COMMITS - INSTALL_COMMITS_BACK

SHORT_MERGE_EVERY = 106  # with the long branch: 378 merges in 40000 commits, 0.945% (this repo: 411 of 43576, 0.94%)
SHORT_MERGE_PHASE = 53
LONG_BRANCH_COMMITS = 5
_LONG_BRANCH = tuple(range(_README_TOUCH - 1000, _README_TOUCH - 1000 + LONG_BRANCH_COMMITS))
_LONG_MERGE = _README_TOUCH + 10
# The long branch's commits precede README's touch but are not its ancestors, so they count
# toward commits-since while a topological level difference does not.
README_EXPECTED_COMMITS_SINCE = README_COMMITS_BACK + LONG_BRANCH_COMMITS


def _blob(parts: list[str], rel: str, body: str) -> None:
    parts.append(f"M 100644 inline {rel}\ndata {len(body.encode())}\n{body}\n")


def _doc_body(doc: str, *, revised: bool = False) -> str:
    tag = "Revised line" if revised else "Line"
    lines = "".join(
        f"{tag if n < 3 else 'Line'} {n} of {doc}.\n" for n in range(DOC_LINES)
    )
    return f"# {doc}\n\n{lines}"


def _sweep(parts: list[str], i: int) -> None:
    for k in range(SWEEP_FILES):
        _blob(parts, f"src/sweep{i}_{k}.txt", f"{k}\n")


def _reserved_positions() -> set[int]:
    return {
        1, _README_TOUCH, _README_TOUCH + 1, _INSTALL_TOUCH, _INSTALL_TOUCH + 1, _INSTALL_TOUCH + 2,
        _LONG_MERGE, *_LONG_BRANCH,
    }


def _short_merge_pairs(count: int) -> set[int]:
    """Positions i where a one-commit side branch forks at i and the merge lands at i + 1."""
    reserved = _reserved_positions()
    return {
        i for i in range(SHORT_MERGE_PHASE, count, SHORT_MERGE_EVERY)
        if not ({i, i + 1} & reserved)
    }


def _history_stream(count: int) -> str:
    """Position i is commit mark :i. Merge shapes (real repos carry ~0.94% merge commits):
    short pairs (side commit at i, merge at i + 1) and one long branch whose commits sit before
    README's touch and merge after it, so commits-since exceeds the topological level gap."""
    parts: list[str] = []
    pairs = _short_merge_pairs(count)
    main_prev = 0
    long_prev = 0
    i = 0
    while i < count:
        i += 1
        message = f"c{i}\n"
        is_long = i in _LONG_BRANCH
        is_side = i in pairs
        ref = "refs/heads/long" if is_long else "refs/heads/side" if is_side else "refs/heads/main"
        parts.append(
            f"commit {ref}\nmark :{i}\n"
            f"committer t <t@example.com> {1700000000 + i} +0000\n"
            f"data {len(message.encode())}\n{message}\n"
        )
        if i == 1:
            _blob(parts, "README.md", _doc_body("README.md"))
            _blob(parts, "docs/old-install.md", _doc_body("INSTALL.md"))
            for k in range(SEED_FILES):
                _blob(parts, f"src/seed{k}.txt", f"{k}\n")
            main_prev = 1
            continue
        if is_long:
            parts.append(f"from :{long_prev or main_prev}\n")
            _blob(parts, f"src/long{i}.txt", f"{i}\n")
            long_prev = i
            continue
        if is_side:
            parts.append(f"from :{main_prev}\n")
            _blob(parts, f"src/side{i}.txt", f"{i}\n")
            continue
        parts.append(f"from :{main_prev}\n")
        if i == _LONG_MERGE:
            parts.append(f"merge :{_LONG_BRANCH[-1]}\n")
        elif i - 1 in pairs:
            parts.append(f"merge :{i - 1}\n")
        main_prev = i
        if i == _README_TOUCH:
            _blob(parts, "README.md", _doc_body("README.md", revised=True))
        elif i == _README_TOUCH + 1:
            _blob(parts, "README.md", _doc_body("README.md", revised=True) + "Sweep line.\n")
            _sweep(parts, i)
        elif i == _INSTALL_TOUCH:
            _blob(parts, "docs/old-install.md", _doc_body("INSTALL.md", revised=True))
        elif i == _INSTALL_TOUCH + 1:
            parts.append("R docs/old-install.md INSTALL.md\n")
        elif i == _INSTALL_TOUCH + 2:
            _blob(parts, "INSTALL.md", _doc_body("INSTALL.md", revised=True) + "Sweep line.\n")
            _sweep(parts, i)
        else:
            _blob(parts, f"{AREAS[i % len(AREAS)]}/w{i % 50}.txt", f"{i}\n")
    return "".join(parts)


def ensure_fixture(parent: Path) -> Path:
    """The fixture repo under `parent`, built once: a present completion stamp means the
    build and its verification already ran, so a measured call spawns nothing here."""
    repo = parent.resolve() / "fixture"
    stamp = repo / STAMP_REL
    if not stamp.is_file():
        if repo.exists():
            shutil.rmtree(repo)
        _build(repo)
        stamp.write_text("built", encoding="ascii")
    return repo


def _build(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "fast-import", "--quiet", stdin=_history_stream(HISTORY_COMMITS))
    _git(repo, "update-ref", "-d", "refs/heads/side")
    _git(repo, "update-ref", "-d", "refs/heads/long")
    _write_split_graph(repo)
    _git(repo, "checkout", "-q", "-f", "main")


def _write_split_graph(repo: Path) -> None:
    """A three-layer split commit-graph chain, the layout this repo carries (git gc --split
    appends layers). No changed-path Bloom filters: this repo's graphs carry none (no BDAT
    chunk). If a ruled provisioning route adds Bloom filters to real repos, add
    `--changed-paths` to these writes the same way (C6 owns that change)."""
    for back in (20000, 8000):
        tip = run_git(["rev-parse", f"HEAD~{back}"], cwd=str(repo))
        if not tip.ok:
            raise RuntimeError(f"git rev-parse HEAD~{back} failed: {tip.stderr}")
        _git(repo, "commit-graph", "write", "--split=no-merge", "--stdin-commits", stdin=tip.stdout)
    _git(repo, "commit-graph", "write", "--reachable", "--split=no-merge")


def check_report(report: dict) -> Optional[str]:
    """None when `report` shows the fixture's shape (README stale at its depth, INSTALL
    reported), else the reason it does not: a gate must never time a wrong or failed result."""
    by_path = {d.get("path"): d for d in report.get("docs", [])}
    readme = by_path.get("README.md", {})
    if not (
        readme.get("status") == "ok"
        and readme.get("stale") is True
        and readme.get("commits_since") == README_EXPECTED_COMMITS_SINCE
    ):
        return f"README.md is not stale at {README_EXPECTED_COMMITS_SINCE} commits: {readme!r}"
    install = by_path.get("INSTALL.md", {})
    if install.get("status") != "ok":
        return f"INSTALL.md is not resolved through its rename: {install!r}"
    return None


def _load_trampoline():
    script = Path(__file__).resolve().parents[2].joinpath(*_BIN_SCRIPT)
    bin_dir = str(script.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("workweek_complete_doc_staleness", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--setup-only", "--build")]
    if len(args) != 1 or unknown:
        print("usage: doc_staleness_full_path <dir> [--build | --setup-only]", file=sys.stderr)
        return 2
    repo = ensure_fixture(Path(args[0]))
    if "--build" in argv:
        return 0
    trampoline = _load_trampoline()
    if "--setup-only" in argv:
        return 0
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = trampoline.main(["--root", str(repo)])
    problem = check_report(json.loads(out.getvalue()))
    if rc != 0 or problem:
        print(f"doc staleness did not report the fixture's shape (rc={rc}): {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

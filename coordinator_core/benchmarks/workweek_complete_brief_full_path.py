"""Driver for `workweek_complete.brief.brief()`, measured as a process tree.

The fixture is a repo built once under a caller-given directory with the shape of what the
brief reads: HISTORY_COMMITS commits via one `git fast-import` spawn (the doc-staleness walk is
history-depth-sensitive), a `coordinator.local.md` doc registry declaring human-facing docs that
the history has left stale, completion records with `status: pending-release` at real count,
a sizing corpus at real count with register-bearing sizings, the plan corpus, and a
`state/week-changelog/HEADER.md` week header. A plain run chdirs to the fixture and calls
`brief()` in-process. `--setup-only` stops before importing the brief, so the cost of the call
(import included) is `full - setup-only`.

    python -m coordinator_core.benchmarks.workweek_complete_brief_full_path <dir> [--build | --setup-only]

`--build` builds and verifies the fixture once and exits; every other invocation reuses it via
the completion stamp.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.workstream_complete_brief_full_path import STAMP_REL, _git

HISTORY_COMMITS = 40000  # near the real repo's 42716
PEER_SESSIONS = 40
SIZINGS = 940  # state/sizings/*.yaml on the real repo
REGISTER_SIZINGS = 24  # of those, carrying a requirement_register
PLANS = 850  # docs/plans/*.md on the real repo
COMPLETIONS = 650  # archive/completed/*/*.md on the real repo
PENDING_RELEASE = 120  # of those, the week's pending-release entries
DOC_LINES = 20  # above ops.doc_staleness.DEFAULT_SWEEP_LINES_THRESHOLD (15)
WEEK_START ="2026-10-05"
DOCS = ("README.md", "INSTALL.md", "CONTEXT.md", "CONTRIBUTING.md", "docs/guide.md")
_REGISTRY = (
    "---\nproject_type: general\n"
    "human_facing_docs: [" + ", ".join(DOCS) + "]\n"
    "---\n\n# fixture\n"
)
_HEADER = f"# Week Changelog\n\n**Week starting:** {WEEK_START}\n"


def _blob(parts: list[str], rel: str, body: str) -> None:
    parts.append(f"M 100644 inline {rel}\ndata {len(body.encode())}\n{body}\n")


def _doc_body(doc: str) -> str:
    """Wider than DEFAULT_SWEEP_LINES_THRESHOLD: the seed commit touches thousands of files, so a
    doc added in fewer lines reads as a sweep, is skipped, and is never reported stale."""
    lines = "".join(f"Line {n} of {doc}.\n" for n in range(DOC_LINES))
    return f"# {doc}\n\n{lines}"


def _seed_tip(parts: list[str]) -> None:
    _blob(parts, "coordinator.local.md", _REGISTRY)
    _blob(parts, "state/week-changelog/HEADER.md", _HEADER)
    for doc in DOCS:
        _blob(parts, doc, _doc_body(doc))
    for k in range(COMPLETIONS):
        status = "pending-release" if k < PENDING_RELEASE else "published"
        nature = "roadmap" if k == 0 else "infra"
        created = "2026-10-08" if k < PENDING_RELEASE else "2026-09-10"
        _blob(
            parts, f"archive/completed/2026-10/2026-10-08-c{k}.md",
            f"---\ntitle: c{k}\ncreated: {created}\nnature: {nature}\nstatus: {status}\n---\n\nbody\n",
        )
    for k in range(SIZINGS):
        register = (
            "requirement_register:\n  rows:\n"
            f"  - id: R{k}\n    source_text: x\n    anchor: t{k}\n    surface: internal\n"
            "    status: open\n    claimed_by: []\n    wired: false\n"
            "    last_progress_at: '2026-10-14T11:09:23Z'\n"
            if k < REGISTER_SIZINGS else ""
        )
        _blob(
            parts, f"state/sizings/2026-01-01-s{k}.yaml",
            f"title: s{k}\nstatus: sized\nroute: dispatch\nplan: null\n{register}",
        )
    for k in range(PLANS):
        _blob(
            parts, f"docs/plans/2026-01-01-p{k}.md",
            f"---\ntitle: p{k}\ncreated: 2026-01-01\nstatus: implemented\n---\n\n# p{k}\n",
        )


def _history_stream(count: int) -> str:
    parts: list[str] = []
    for i in range(1, count + 1):
        sid = f"00000000-0000-0000-0000-{i % PEER_SESSIONS + 1:012d}"
        message = f"c{i}\n\nSession-Id: {sid}\n"
        parts.append(
            f"commit refs/heads/main\nmark :{i}\n"
            f"committer t <t@example.com> {1700000000 + i} +0000\n"
            f"data {len(message.encode())}\n{message}\n"
        )
        if i == 1:
            _seed_tip(parts)
        else:
            parts.append(f"from :{i - 1}\n")
            _blob(parts, f"src/w{i % 50}.txt", f"{i}\n")
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
    _git(repo, "checkout", "-q", "-f", "main")


def main(argv: list[str]) -> int:
    declare_benchmark_origin()
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a not in ("--setup-only", "--build")]
    if len(args) != 1 or unknown:
        print(
            "usage: workweek_complete_brief_full_path <dir> [--build | --setup-only]",
            file=sys.stderr,
        )
        return 2
    repo = ensure_fixture(Path(args[0]))
    if "--build" in argv:
        return 0
    os.chdir(repo)
    if "--setup-only" in argv:
        return 0
    from coordinator_core.workweek_complete import brief

    rc, envelope = brief.brief()
    ids = [d.get("id", "") for d in envelope.get("directives", [])]
    if rc != 0 or not any(i.startswith("d_doc_staleness_ack_") for i in ids):
        print(f"brief did not report the fixture's stale docs (rc={rc}): {ids[:5]!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

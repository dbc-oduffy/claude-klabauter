"""Driver for the close.background job, measured as a process tree.

Each invocation builds a fresh repo under a caller-given directory (one open claimed handoff,
one open inbox memo, a minimal pinboard-only orientation cache, a close-job record queueing all four
legs), then runs the job in-process. `--setup-only` does everything except the job, so the cost
of the job is `full - setup-only`.

    python -m coordinator_core.benchmarks.close_background_full_path <dir> [--setup-only]

The index launchers run with their detached-child spawn stubbed (`_spawn_detached`,
`spawn_ensure`): that child is the launcher's own cost, not the job's (run-close WS1 spike,
background-leg caveats). Their registry lookup spawn stays real. The handoff
sweep archives nothing in this fixture, so that leg is a lower bound.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from coordinator_core.win_portability import no_console_creationflags

RUN_ID = "bench-run"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
BATON = "state/handoffs/h1.md"
MEMO = "m1.md"

_HANDOFF = "---\ntitle: h1\nstatus: open\ndeployment_state: unshipped\n---\n\nbody\n"
_MEMO = (
    "---\ntitle: m\nfrom: a\nto: b\ncreated: 2026-10-13\nstatus: open\n"
    "delivery_mode: receiver-repo\nsummary: s\nkind: fyi\n---\n\nbody\n"
)
_CACHE = "# Orientation cache\n\n## Housekeeping\n\n## Pinboard\n\n- note\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def build_fixture(parent: Path) -> Path:
    from coordinator_core.ops.dispatch_emit import run_close

    repo = parent / uuid.uuid4().hex[:12]
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for rel, text in (
        (BATON, _HANDOFF),
        (f"state/cross-repo/inbox/{MEMO}", _MEMO),
        ("state/orientation_cache.md", _CACHE),
    ):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    run_close.write_close_job(
        repo, RUN_ID, ("baton-drain", "orientation-refresh", "memo-lifecycle", "post-close"),
        datetime.now().astimezone() + timedelta(minutes=10),
    )
    return repo


def _run_job(repo: Path) -> dict:
    from coordinator_core.ops.ceremony import close_background as cb

    load_bin_script = cb._bin_script

    class _Child:
        def wait(self, timeout=None):
            return 0

    def launcher_without_detached_child(name):
        module = load_bin_script(name)
        if hasattr(module, "_spawn_detached"):
            module._spawn_detached = lambda *a, **k: True
        if hasattr(module, "spawn_ensure"):
            module.spawn_ensure = lambda *a, **k: _Child()
        return module

    ctx = cb.JobContext(repo, repo / ".git", RUN_ID, (BATON,), MEMO)
    with mock.patch.object(cb, "_bin_script", launcher_without_detached_child):
        return cb.run_background(ctx)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    unknown = [a for a in argv if a.startswith("--") and a != "--setup-only"]
    if len(args) != 1 or unknown:
        print("usage: close_background_full_path <dir> [--setup-only]", file=sys.stderr)
        return 2
    os.environ["COORDINATOR_SESSION_ID"] = SESSION_ID
    repo = build_fixture(Path(args[0]))
    from coordinator_core.ops.ceremony import close_background  # noqa: F401  (loaded in both modes)

    if "--setup-only" in argv:
        return 0
    outcomes = _run_job(repo)
    must_run = ("baton-drain", "orientation-refresh", "memo-lifecycle")
    bad = {leg: outcomes.get(leg) for leg in must_run if getattr(outcomes.get(leg), "status", None) != "ran"}
    # post-close is recorded `failed` by design (it names the over-budget chain-closure signal); only its presence is checked.
    if bad or "post-close" not in outcomes:
        print(f"job did not complete: {outcomes!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

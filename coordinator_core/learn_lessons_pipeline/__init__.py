"""coordinator_core.learn_lessons_pipeline — the compute-only READ-ONLY half of
the learn-lessons PIPELINE ceremony: `brief()` returns the ORDERED directive
list over the four atomic `coordinator/bin` CLIs
(`extract-lessons.py`/`lessons-outbox-drain.py`/`age-sweep-lessons.py`) plus
this package's own in-process run-stamp write
(`coordinator_core.learn_lessons_pipeline.run_stamp`) that
`skills/learn-lessons/SKILL.md`'s Phase Flow currently hand-sequences — an EM
reads the sequence, types each step, and picks the age-sweep cutoff by eye.

`brief()` never mutates — every action is returned as a `directives[]` entry
naming an existing atomic CLI (`cli:`) or, for the terminal run-stamp step, an
in-package op (`op:`) — see the module-level `directives[]` shape note below
and C4's `apply.py` for the two closed dispatch tables that consume this
list. `learn_lessons_pipeline.apply` (the mutating half, C4) recomputes this
brief in-process and dispatches through
`coordinator_core.contract.apply_base`'s shared directive-execution engine,
one directive at a time, halting on the first non-zero exit.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C3

`directives[]` order, each naming its predecessor in `depends_on`:

    d-extract-lessons     cli: extract-lessons      extract <lessons_path> --shortname <run> [--since <cutoff>] --format yaml -o <extraction>
    d-verify-extraction   cli: extract-lessons      verify <extraction> <routing>
    d-drain-outbox        cli: lessons-outbox-drain read <root>...
    d-assert-outbox-empty cli: lessons-outbox-drain assert-empty <this_repo_root>
    d-age-sweep           cli: age-sweep-lessons     <lessons_path> --before <cutoff> --apply
    d-stamp-run-complete  op: stamp-run-complete     <runs_dir> <run_date>

`d-age-sweep`'s `--before <cutoff>` value is computed by this module from the
COMPLETE-sentinel run-dir tree through `ops.learn_lessons_cutoff.derive_cutoff`
— the ONE shared oracle C1 built — never chosen by a caller: no `decisions`
key and no argument to `brief()` lets a caller supply a date. When
`derive_cutoff` returns `None` (no completed central run reachable),
`d-age-sweep` is withheld entirely and `gates["age_sweep"]` records the
reason, mirroring `learn-lessons-age-sweep cutoff`'s own skip-loud exit
(§ Anti-scope: "Do NOT default the cutoff"). `d-extract-lessons` still fires
in that case, with `--since` omitted rather than defaulted.

`d-verify-extraction`'s `<routing>` argument names the routing-records file
(`state/lessons/records.yaml`, the naming convention
`coordinator/bin/learn-lessons-age-sweep.py check-strip-orphans` already
uses for the same artifact) that the ROUTING step — deliberately outside
this pipeline's six directives, § Anti-scope "Do NOT mechanize Step 5
routing" — writes before this ceremony's `apply()` dispatches verify. This
module never creates, routes, or judges that file's contents; it only names
the conventional path the verify CLI reads, exactly as it does today when an
EM types the same command by hand.

`judgment_points[]` is always `[]` — AC2: extraction and verification are a
deterministic parse and a mechanical grounding gate, never a model call or a
judgment point, preserved by this module making no model call anywhere
(pinned by `tests/test_brief.py :: test_no_model_call_in_package`, an `ast`
sweep over every module in this package).

Negative-spec:
    - Do NOT let `brief()` mutate anything — it reads disk (the COMPLETE-
      sentinel tree via `ops.learn_lessons_cutoff`, nothing else) and
      returns directives, like every sibling assembler in this baton. A
      finding that "the assembler should just run the sweep" belongs in a
      directive, not a code path here.
    - Do NOT default the age-sweep cutoff to today, N days ago, or the
      earliest run — a defaulted date can archive un-promoted universals.
      No completed run reachable means the directive is withheld, not
      defaulted.
    - Do NOT mechanize the routing step (which wiki a drained/extracted
      entry belongs in) — that stays a judgment call made outside this
      pipeline's six directives.
    - Do NOT add `import subprocess`, a `subprocess.*` call, or a model/LLM
      client import anywhere in this package — the extract/verify legs are
      a deterministic parse (AC2), and adding a judgment point or a model
      call there would re-introduce the hand crank in a new costume.
    - Do NOT re-derive the `CLAUDE_HOME`/`HOME`/`USERPROFILE` ladder, the
      COMPLETE-sentinel scan, or the peer-root discovery list here — those
      are `ops.learn_lessons_cutoff.derive_cutoff`/`resolve_runs_dir` and
      `ops.learn_lessons_roots.resolve_roots`'s jobs respectively; this
      module only calls them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Optional

#: `CONSUMES_MANIFEST` uses). `stamp-run-complete` is deliberately NOT a
CONSUMES_MANIFEST: tuple[str, ...] = (
    "extract-lessons",
    "lessons-outbox-drain",
    "age-sweep-lessons",
)

#: `CONSUMES_MANIFEST`'s docstring for why the run-stamp step is not a
#: `CONSUMES_MANIFEST` member.
STAMP_RUN_COMPLETE_OP = "stamp-run-complete"

ROUTING_RECORDS_FILENAME = "records.yaml"


def _shortname_for(repo_root: Path) -> str:
    return repo_root.name


def build_directives(
    repo_root: Path,
    *,
    roots: list[str],
    cutoff: Optional[str],
    run_date: str,
) -> list[dict[str, Any]]:
    shortname = _shortname_for(repo_root)
    lessons_path = repo_root / "state" / "lessons"
    extraction = lessons_path / f"{shortname}-extracted-full.yaml"
    routing = lessons_path / ROUTING_RECORDS_FILENAME

    extract_args = ["extract", str(lessons_path), "--shortname", shortname]
    if cutoff is not None:
        extract_args += ["--since", cutoff]
    extract_args += ["--format", "yaml", "-o", str(extraction)]

    directives: list[dict[str, Any]] = [
        {
            "id": "d-extract-lessons",
            "cli": "extract-lessons",
            "args": extract_args,
            "depends_on": None,
        },
        {
            "id": "d-verify-extraction",
            "cli": "extract-lessons",
            "args": ["verify", str(extraction), str(routing)],
            "depends_on": ["d-extract-lessons"],
        },
        {
            "id": "d-drain-outbox",
            "cli": "lessons-outbox-drain",
            "args": ["read", *roots],
            "depends_on": ["d-verify-extraction"],
        },
        {
            "id": "d-assert-outbox-empty",
            "cli": "lessons-outbox-drain",
            "args": ["assert-empty", str(repo_root)],
            "depends_on": ["d-drain-outbox"],
        },
    ]

    last_id = "d-assert-outbox-empty"
    if cutoff is not None:
        directives.append(
            {
                "id": "d-age-sweep",
                "cli": "age-sweep-lessons",
                "args": [str(lessons_path), "--before", cutoff, "--apply"],
                "depends_on": ["d-assert-outbox-empty"],
            }
        )
        last_id = "d-age-sweep"

    from coordinator_core.ops.learn_lessons_cutoff import resolve_runs_dir  # noqa: PLC0415

    directives.append(
        {
            "id": "d-stamp-run-complete",
            "op": STAMP_RUN_COMPLETE_OP,
            "args": [str(resolve_runs_dir()), run_date],
            "depends_on": [last_id],
        }
    )
    return directives


def brief(repo_root: Path, *, roots: Optional[list[str]] = None) -> dict[str, Any]:
    """Computes and returns the learn-lessons pipeline decision object: the
    8-key envelope whose `directives[]` names extract -> verify ->
    outbox-drain -> assert-outbox-empty -> age-sweep -> run-stamp, in that
    order, each naming its predecessor in `depends_on`. `judgment_points[]`
    is always `[]` (AC2 — no model call on the extract/verify legs).

    `roots` overrides the peer-repo roots `d-drain-outbox` reads (defaults
    to `ops.learn_lessons_roots.resolve_roots()` — never re-derived here).
    Exposed as a parameter so a caller (or a test) can supply a fixture
    root list without needing a real machine-registry on disk.

    The age-sweep cutoff is derived from the COMPLETE-sentinel run-dir tree
    (`ops.learn_lessons_cutoff.derive_cutoff`) — never chosen by this
    function's caller. When no completed central run is reachable,
    `d-age-sweep` is withheld and `gates["age_sweep"]` records why,
    `d-extract-lessons` still fires with `--since` omitted."""
    from coordinator_core.contract.decision_object.envelope import build_envelope  # noqa: PLC0415
    from coordinator_core.ops.learn_lessons_cutoff import (  # noqa: PLC0415
        derive_cutoff,
        resolve_runs_dir,
    )
    from coordinator_core.ops.learn_lessons_roots import resolve_roots  # noqa: PLC0415

    effective_roots = roots if roots is not None else resolve_roots()
    runs_dir = resolve_runs_dir()
    cutoff = derive_cutoff(runs_dir)
    run_date = date.today().isoformat()

    directives = build_directives(
        repo_root,
        roots=effective_roots,
        cutoff=cutoff,
        run_date=run_date,
    )

    gates: dict[str, Any] = {}
    if cutoff is None:
        gates["age_sweep"] = "skipped — no completed central run reachable"

    narration = (
        f"learn-lessons-pipeline brief: {len(directives)} directive(s), "
        f"cutoff={cutoff!r}."
    )

    return build_envelope(
        artifact={"cutoff": cutoff, "run_date": run_date},
        preflight={"consumes_manifest": list(CONSUMES_MANIFEST)},
        gates=gates,
        directives=directives,
        judgment_points=[],
        decisions={},
        narration=narration,
        next_move="Run learn_lessons_pipeline.apply to dispatch these directives in order.",
    )

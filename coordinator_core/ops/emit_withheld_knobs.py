"""
coordinator_core.ops.emit_withheld_knobs — emit claude-klabauter's withheld-knob set as data,
into the DoE-claude coordinator/ tree, beside the manifest that runs the other way.

PURPOSE
Some env knobs are live in the engine but deliberately NOT ADVERTISED on a particular
path — the code reads them elsewhere, and the refusal/guard text on that path withholds
them by design. That fact lives in claude-klabauter and is asserted only behaviourally, inside
Claude-klabauter's own tests. A doctrine surface in DoE (a skill, a wiki page) that prescribes such
a knob as the remedy for that path sends an agent to set something inert, and then to go
hunting for the thing that is not inert. This op emits the set so a DoE-side test can
refuse that prescription mechanically.

WHY AN EXISTENCE CHECK IS THE WRONG INSTRUMENT (the reason this artifact exists at all)
The obvious guard — assert every `COORDINATOR_*` name a doctrine surface cites still
resolves to something live in the engine — PASSES CLEAN on the defect that prompted this.
On 2026-09-04 DoE's `/percolate` skill prescribed `COORDINATOR_LOCK_WAIT_SECS=900` for a
contended percolate round. That knob exists, is read today, and is not dead: it is
narrowing-only, and `2026-08-30-a-second-percolate-round-stops-sleeping` set the default
wait on that path to 0, so raising it changes nothing. A session followed the skill, got a
byte-identical refusal, went looking for why, found `COORDINATOR_ALLOW_PERCOLATE_QUEUE`,
set it, and parked a sleeping process on a box carrying ~50 peers — re-arming exactly the
sleep that plan exists to delete. Nothing about NAME LIVENESS could have seen that. The
checkable property is ADVERTISEMENT on a path, not existence.

NEGATIVE SPEC — what this artifact is NOT
  - NOT an inventory of `COORDINATOR_*` names. An inventory is the instrument measured and
    rejected above; a 105-entry ledger that misses the one bug that prompted it is worse
    than no ledger, because it reads as coverage.
  - NOT a claim that a listed knob is dead, deprecated, or unread. Every knob here is LIVE.
    The entry constrains where it may be PRESCRIBED, nothing else.
  - NOT a second source of truth for any knob's name. Every `knob` value below is read from
    the constant that owns it (see `_registry`), so a rename moves this artifact, claude-klabauter's
    own assertions, and DoE's guard together. A hand-typed name here would be the exact
    drift the artifact exists to stop, wearing the costume of the fix.

CROSS-REPO WRITE (same shape as `emit_artifact_shape_contract`, deliberately)
Writes into a SIBLING repo's working tree: DoE-claude owns `coordinator/withheld-knobs.json`,
Claude-klabauter owns the sole regeneration path. It lands beside `coordinator/doctrine-surfaces.json`,
which runs the other way — DoE emits it, claude-klabauter's `verify_skill_anchor_links` consumes it.
The write is deterministic and uncommitted; claim it with the peer rather than leaving it
for them to find in a diff.

Consumer contract: DoE owns the test that fails their tier when a doctrine surface
prescribes a withheld knob. It is not implemented here and must not be — the surfaces it
scans are theirs.

Reference: docs/reference/withheld-knobs.md
Plan:      docs/plans/2026-09-04-emit-the-withheld-knob-set-as-data.md
Memo:      state/cross-repo/inbox/2026-09-04-doe-claude-em-emit-the-withheld-knob-set-so-doctrine-cannot-prescribe-one.md
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.locked_write import CONTENDED_LOCK_WAIT_ENV
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
from coordinator_core.session.declared_writes import declare_write

#: Bumped only when an EXISTING field changes meaning or leaves. Adding a field, or adding
#: an entry, is additive and does not bump — DoE's consumer reads by key.
SCHEMA_VERSION = 1

#: Output-dir override, mirroring `ARTIFACT_CONTRACT_OUT_DIR`'s role for the sibling op.
#: Exists so the pin test can emit into a tmp_path without a DoE checkout present.
OUT_DIR_ENV = "WITHHELD_KNOBS_OUT_DIR"

_BASENAME = "withheld-knobs.json"


def ensure_percolate_on_path() -> None:
    """Add `coordinator/lib` to `sys.path` so `percolate.*` is importable.

    That directory is not on the path by default, and `coordinator/lib/percolate/` imports
    `coordinator_core`, so `coordinator_core` must not import it at module scope either
    way. Both halves of this shape — the deferred import and the path rung — are lifted
    from `coordinator_core/percolate/round.py :: _is_percolate_package_row`, which does
    exactly this for the same reason.

    Public, and separate from the knob lookup below, because the rung is a precondition
    rather than a side effect of asking for a name: a caller that wants `wire_contract`
    for its own reasons should not have to request a knob to get the path fixed, and a
    test that did so would pass or fail on execution order.
    """
    coordinator_lib = Path(__file__).resolve().parents[2] / "coordinator" / "lib"
    if str(coordinator_lib) not in sys.path:
        sys.path.insert(0, str(coordinator_lib))


def _percolate_queue_env() -> str:
    """The queue-override knob's name, read from the constant `wire_contract` owns."""
    ensure_percolate_on_path()
    from percolate.wire_contract import (  # noqa: PLC0415 - see ensure_percolate_on_path
        COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV,
    )

    return COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV


def _registry() -> List[Dict[str, object]]:
    """The withheld set, one entry per (path, knob).

    Deliberately narrow. The bash-guard OVERRIDE-WITHHOLDING family
    (`bash_guards/block_subagent_destructive_action.py` — no
    `COORDINATOR_OVERRIDE_*`/`COORDINATOR_ALLOW_*` is subagent-reachable) is the same
    signal class and is NOT seeded here: it withholds via a wildcard that carries at
    least one live exception (`block_subagent_plan_body_bash_write.py` DOES honor
    `COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY`), and the fact lives in a docstring rather
    than a constant. A wrong wildcard entry does not fail quietly — it fails a PEER's
    test tier against legitimate doctrine, which is worse than a narrower set. The
    `pattern` field is the seam that family registers through when its owner declares it;
    v1 emits no row that uses it.
    """
    percolate_path = {
        "path_id": "percolate.destination-lock-refusal",
        "path": (
            "the percolate/publish destination-lock refusal — a contended per-destination "
            "lock, reached via percolate-round.py, percolate-push.py, percolate-mirror.py "
            "or publish.py"
        ),
        # Substrings whose presence in a doctrine surface means that surface is talking
        # about THIS path. Without these a consumer can only ban a knob globally, which
        # would be wrong: both knobs below are live and legitimately prescribable
        # elsewhere. This is the field that makes the set checkable rather than merely
        # readable.
        "context_markers": [
            "percolate",
            "percolate-round",
            "percolate-push",
            "held by another round",
            "docs/reference/percolate-lock-contention.md",
        ],
        "authority": "docs/plans/2026-08-30-a-second-percolate-round-stops-sleeping.md",
        "withheld_since": "2026-08-30",
        "mechanism_page": "docs/reference/percolate-lock-contention.md",
        "register_rule": "docs/wiki/guard-messaging.md § B6 (unresolved audience)",
    }

    return [
        {
            **percolate_path,
            "knob": CONTENDED_LOCK_WAIT_ENV,
            "pattern": None,
            "status": "live-but-inert-on-this-path",
            "why": (
                "Narrowing-only, and the default wait on this path is now 0 — so setting it "
                "cannot lengthen a wait, and raising it changes nothing an operator can "
                "observe. Prescribing it sends an agent to set an inert knob and then to go "
                "hunting for the one that is not inert, which is how the queue override gets "
                "found and a sleeping process gets parked on a loaded box."
            ),
            "asserted_by": [
                "coordinator/bin/tests/test_percolate_push.py",
                "coordinator/bin/tests/test_percolate_round.py",
                "coordinator/bin/tests/test_publish_lock_denies_fast.py",
            ],
            "remedy_instead": (
                "Leave it — the next round against this dest carries the commit. The refusal "
                "is immediate by design; there is no wait to tune."
            ),
        },
        {
            **percolate_path,
            "knob": _percolate_queue_env(),
            "pattern": None,
            "status": "live-and-governing-but-withheld",
            "why": (
                "This one DOES govern the path — it opts back into the wait. It is withheld "
                "anyway, and that is the stronger case, not the weaker one: a percolate "
                "refusal's audience is not resolvable at emission (it may reach a dispatched "
                "subagent as readily as a human), so B6's unresolved-audience rule degrades "
                "to silence about the bypass rather than to printing it. Withholding it from "
                "the refusal line and then prescribing it in the doctrine an agent reads "
                "first is one door in, one door out."
            ),
            "asserted_by": [
                "coordinator/bin/tests/test_percolate_push.py",
                "coordinator/bin/tests/test_percolate_round.py",
                "coordinator/bin/tests/test_percolate_round_lock_denies_fast.py",
            ],
            "remedy_instead": (
                "Leave it. Opting back into the wait re-arms the sleep the 2026-08-30 plan "
                "deleted; on a box carrying ~50 peers that cost is paid by everyone queued "
                "behind the sleeping process, not by the session that set it."
            ),
        },
    ]


def build(entries: List[Dict[str, object]]) -> Dict[str, object]:
    """The emitted document. Pure — no I/O, so the pin test can compare it directly.

    Takes `entries` rather than calling `_registry()` itself so a caller that needs to
    inspect the set (main, to refuse an empty one) does not build it twice.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "coordinator_core.ops.emit_withheld_knobs (claude-klabauter)",
        "generator_note": (
            "claude-klabauter owns the sole regeneration path for this file. Do not hand-edit it in "
            "DoE — an edit here is overwritten on the next emit and, worse, silently "
            "disagrees with the engine constants it is derived from in the meantime."
        ),
        "reference": "docs/reference/withheld-knobs.md (claude-klabauter)",
        "means": (
            "Each entry says: on this PATH, this KNOB must not be prescribed as the remedy. "
            "It does NOT say the knob is dead, deprecated, or unread — every knob listed is "
            "live and legitimately prescribable on other paths. Match a doctrine surface to "
            "a path with context_markers before applying an entry to it."
        ),
        "entries": _registry(),
    }


def _out_dir() -> Optional[str]:
    """Resolve the output directory, override first, then the DoE sibling root.

    Returns None when neither resolves — the caller reports that as a config failure
    rather than inventing a path, because the only paths worth inventing here are inside
    somebody else's repo.
    """
    override = os.environ.get(OUT_DIR_ENV)
    if override:
        return os.path.abspath(override)
    doe_root = coordinator_doe_root()
    if not doe_root:
        return None
    return os.path.join(doe_root, "coordinator")


_USAGE = (
    "usage: emit-withheld-knobs\n"
    "\n"
    "Emits coordinator/withheld-knobs.json into the DoE-claude root — the set of env\n"
    "knobs that are LIVE in the engine but must not be prescribed by a doctrine surface\n"
    "for a named path. Takes no arguments.\n"
    "\n"
    "NOTE — this writes into a SIBLING repo's working tree (DoE-claude owns the file;\n"
    "claude-klabauter owns the sole regeneration path). The write is deterministic and\n"
    "uncommitted; claim it with the peer rather than leaving it in their diff.\n"
    "\n"
    f"Env: {OUT_DIR_ENV} (output-dir override; also the seam the pin test emits through).\n"
)


def main(argv: List[str]) -> int:
    """CLI entry. Exit 0 emitted, 1 refused, 2 config/transport failure.

    Takes no arguments and says so: an unrecognised argv is a config failure, never a
    no-op that proceeds to write into a peer's tree anyway. Same reasoning as
    `emit_artifact_shape_contract.main` — for an op whose side effect lands in somebody
    else's checkout, an operator reaching for an interface must not get a write instead.
    """
    unknown = [a for a in argv if a not in ("-h", "--help")]
    if unknown:
        print(f"emit-withheld-knobs: unexpected argument(s): {' '.join(unknown)}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2
    if argv:
        print(_USAGE)
        return 0

    out_dir = _out_dir()
    if out_dir is None:
        print(
            "emit-withheld-knobs: DoE-claude root did not resolve and "
            f"{OUT_DIR_ENV} is unset — refusing to guess a path in a sibling repo.",
            file=sys.stderr,
        )
        return 2

    entries = _registry()
    document = build(entries)
    if not entries:
        print(
            "emit-withheld-knobs: registry is empty — refusing to emit an empty set. "
            "An empty file reads to a consumer as 'nothing is withheld', which is a "
            "stronger and more wrong claim than a missing file.",
            file=sys.stderr,
        )
        return 1

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_file = os.path.join(out_dir, _BASENAME)
    with open(out_file, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    # Declared AFTER the write lands (DR-276): a report of what was actually written.
    declare_write(out_file)

    print(f"emitted {len(entries)} withheld-knob entries → {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Guard: vendored example-doctrine-repo-SSOT schema files carry no private/leaf-consumer codename.

Motivating incident (closed, not live): `coordinator_core/frontmatter/schemas/
handoff.schema.json`'s `origin_goal_id` field description used
`goal-example-game-repo-shipping-velocity` as its worked example -- root coordinator
tooling naming a specific downstream leaf consumer (the `example-game-workbench-repo`
Unreal-engine repo) as if it were a canonical example for a generic field. Fixed
on our side at `cdce042479ef` (dr_allocator.py docstrings) and `6a15f1100c95`
(the origin_goal_id runtime error text) -- but the vendored schema copy could
not be fixed unilaterally, because it is byte-for-byte SSOT from example-doctrine-repo and
`test_handoff_schema_matches_doe_head_after_dr084_revendor` correctly refuses a
one-sided divergence. Example-doctrine-repo fixed their source at `89bd4256b` (token ->
`goal-shipping-velocity`) and this repo re-vendored the one-hunk change; this
guard is green against the current tree.

Why this guard exists despite the incident being closed: the byte-equality
drift check (`test_schema_validate.py`) and a publish-time vocabulary scrub
each individually go green over a token BOTH copies agree should not be there
-- neither notices a leaf-consumer codename that both sides accepted into a
vendored file. Example-doctrine-repo's own sweep for this class of defect found the same string
in five further SSOT files beyond the one vendored here (per their reply). The
real risk this guard is aimed at is forward-looking: a future re-vendor pulling
one of those other leaf-consumer-tainted strings -- or a fresh one -- into this
tree, landing byte-identical to example-doctrine-repo HEAD (so the drift check stays green) and
never touching a publish/percolate path (so the OSS-scrub vocabulary check
never sees it either).

Oracle: `setup/percolate-hooks/percolate-store.yaml`'s `base.depersonalize`
table (loaded via `coordinator_core.percolate.store.load_store`, never
hand-copied) is this repo's live, already-authoritative source of truth for
"this token is a private codename" -- it is the actual table the real
publish-time scrub engine (`coordinator/bin/publish.py`) walks, generated from
`codename_provenance_seed.py`. Candidates considered and rejected:

  - `codename_provenance_seed.py` disclaims being load-bearing in its own
    module docstring ("this module is NOT yet load-bearing ... a faithful
    reference copy, not (yet) the production consumption path") -- using a
    self-declared-non-live table as an oracle for a live guard is exactly the
    two-sources-of-truth risk this file's own docstring warns against.
  - `functional-identifier-redaction-baseline.txt` is a ratchet baseline for
    one narrow collision class (`FunctionalIdentifierRedactionCollisionError`),
    not a general codename/vocabulary oracle -- wrong shape of table entirely.
  - No `check-registry-codename-leak`-shaped checker exists in this repo
    (grepped; no hits).

Narrowing rationale (both required -- oracle tokens alone over-fire on this
corpus):

  1. EXCLUDE coordinator-authoring-fleet self-reference families (claude-klabauter /
     claude-klabauter / claude_klabauter, example-retrieval-repo / example_retrieval_repo, cockpit /
     example-cockpit-repo / example_cockpit_repo, case-insensitive substring match).
     These name claude-klabauter's own identity and its sibling
     coordination-plane repos; the oracle carries them because the OSS-publish
     scrub depersonalizes even OUR OWN name on the way out, but this vendored
     file is an internal, never-published SSOT copy whose provenance prose
     narrates real engineering history by naming these repos constantly (e.g.
     the `x-bump-note` field's own fleet-sweep audit citations). Banning them
     here would fire on nearly every commit note in this file and drown the
     one signal this guard exists to carry.
  2. SCOPE the scan to `properties.*.description` strings only (recursively,
     including nested `items`/`properties`/`$defs`/branches), never the
     schema-root `description` or `x-bump-note` fields. The actual defect
     class -- per example-doctrine-repo-em's PM ruling -- is a codename baked into the
     WORKED EXAMPLE for a generic field's documentation (exactly where
     `goal-example-game-repo-shipping-velocity` lived, inside `origin_goal_id`'s
     `description`). The schema-root `x-bump-note` is a changelog/audit trail
     that legitimately cites real downstream repos it swept for verification
     (e.g. `example-game-workbench-repo`, `example-market-data-repo` appear there in a
     "fleet-wide over 7 coordinator repos" sweep count) -- narrating real
     audit history is not the same defect as minting a private codename into
     a field's generic illustrative example, and scoping to `properties.*`
     descriptions is what tells the two apart without a second hand-authored
     list.

Do NOT hand-author a duplicate token list here -- if this guard needs a new
token, it belongs in the oracle (`percolate-store.yaml` / its generator), not
in this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml

from coordinator_core.percolate.store import load_store

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / 'schemas'
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORE_PATH = _REPO_ROOT / 'setup' / 'percolate-hooks' / 'percolate-store.yaml'

_VENDORED_SCHEMA_FILES = (
    _SCHEMAS_DIR / 'handoff.schema.json',
    _SCHEMAS_DIR / 'handoff-archived.schema.json',
    _SCHEMAS_DIR / 'plan-tasks.schema.json',
)

_FLEET_SELF_REFERENCE_FAMILIES = (
    'claude-klabauter',
    'example-retrieval-repo',
    'example_retrieval_repo',
    'cockpit',
)


def _is_fleet_self_reference(token: str) -> bool:
    lowered = token.lower()
    return any(family in lowered for family in _FLEET_SELF_REFERENCE_FAMILIES)


def _banned_tokens() -> list[str]:
    store = load_store(_STORE_PATH)
    entries = store.get('base', {}).get('depersonalize') or []
    return [
        entry['key']
        for entry in entries
        if not _is_fleet_self_reference(entry['key'])
    ]


def _iter_property_descriptions(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'description' and isinstance(value, str):
                yield value
            else:
                yield from _iter_property_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_property_descriptions(item)


def _field_description_text(schema_path: Path) -> str:
    schema = yaml.safe_load(schema_path.read_text(encoding='utf-8'))
    properties = schema.get('properties', {})
    return '\n'.join(_iter_property_descriptions(properties))


def test_vendored_schema_field_descriptions_carry_no_leaf_consumer_codename():
    """No vendored schema's per-field `description` text names a private/
    leaf-consumer codename from the live percolate-store oracle.

    Scoped to `properties.*.description` only -- see module docstring
    "Narrowing rationale" #2 for why the schema-root `x-bump-note`/
    `description` fields are deliberately excluded from this scan.
    """
    banned = _banned_tokens()
    assert banned, 'oracle produced zero banned tokens -- percolate-store.yaml shape likely changed'

    offenders: list[tuple[str, str]] = []
    for schema_path in _VENDORED_SCHEMA_FILES:
        text = _field_description_text(schema_path)
        for token in banned:
            if token in text:
                offenders.append((schema_path.name, token))

    assert not offenders, (
        'vendored schema field description(s) carry a private/leaf-consumer '
        'codename from the live depersonalize oracle '
        f'(setup/percolate-hooks/percolate-store.yaml base.depersonalize): {offenders}. '
        'A byte-equality drift check and a publish-time vocabulary scrub can each stay '
        'green over this -- see module docstring for the incident this guards against.'
    )
